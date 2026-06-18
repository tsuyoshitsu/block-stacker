"""Hybrid feature extractor combining Set Transformer + Heightmap CNN + Short-Term Memory.

Used as the SB3 features_extractor for a `MultiInputPolicy` SAC model.
The output `features_dim` feeds into SAC's policy and value heads.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    Dict 観測 (blocks + mask + heightmap + tower_top_z + optional 短期記憶)
    を 1 つの特徴ベクトルに統合。設計書 §4 のアーキ図 2 ストリーム→ concat
    → MLP に対応。短期記憶を含む場合は 4 ストリーム構成。

設計上のポイント:
    - 4 ストリーム埋め込み:
        blocks → Set Transformer → 64 dim
        heightmap → CNN → 32 dim
        tower_top_z → Linear → 16 dim
        (optional) 短期記憶 → MLP → 16 dim
      concat (112 or 128 dim) → Linear → features_dim (128 dim) → SAC policy / value head へ。
    - 短期記憶ストリームは観測辞書に "recent_actions" 等が含まれている時のみ作る。
      欠けている場合は従来の 3 ストリーム構成にフォールバック（後方互換）。
    - SB3 の BaseFeaturesExtractor を継承。MultiInputPolicy 経由で自動的に呼ばれる。

    短期記憶の入力構造（env.py の _get_obs で詰めた形）:
      recent_actions: [stm_length, ACTION_DIM=7]
      recent_rewards: [stm_length]
      recent_results: [stm_length]
      recent_mask:    [stm_length]  # 1.0 = valid, 0.0 = padding (履歴未充填)
      → flatten して mask 倍してから小さな MLP に通す。

レビューで見る観点:
    - observation_space.shape からの dim 抽出は spaces.Dict のスキーマ前提。
      env.observation_space と同じ形状（Dict key 名）であることが必須。
    - features_dim を上げると policy head の入力が増えてパラメータ増。
      性能と計算量のトレードオフを training.yaml で調整可能。
    - 短期記憶のオン/オフは observation_space 側の dict key 有無で自動判定。
"""
from __future__ import annotations

import torch
import torch.nn as nn
from gymnasium import spaces
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from torch import Tensor

from block_stacker.policy.heightmap_cnn import HeightmapCNN
from block_stacker.policy.set_transformer import SetEncoder


class HybridFeatureExtractor(BaseFeaturesExtractor):
    """Process a Dict observation with up to four streams:
        - "blocks" + "blocks_mask"  → Set Transformer → 64-d
        - "heightmap"               → small CNN       → 32-d
        - "tower_top_z"             → linear          → 16-d
        - (optional) "recent_*"     → small MLP       → 16-d
    Concatenate → MLP → features_dim.
    """

    def __init__(
        self,
        observation_space: spaces.Dict,
        features_dim: int = 128,
        set_hidden: int = 64,
        set_output: int = 64,
        cnn_output: int = 32,
        scalar_output: int = 16,
        stm_output: int = 16,
        n_set_layers: int = 2,
        n_heads: int = 4,
    ) -> None:
        super().__init__(observation_space, features_dim)

        block_space = observation_space["blocks"]
        hm_space = observation_space["heightmap"]
        scalar_space = observation_space["tower_top_z"]

        per_block_dim = int(block_space.shape[-1])
        hm_channels = int(hm_space.shape[0])
        scalar_dim = int(scalar_space.shape[0])

        self.set_encoder = SetEncoder(
            in_dim=per_block_dim,
            hidden_dim=set_hidden,
            output_dim=set_output,
            n_layers=n_set_layers,
            n_heads=n_heads,
        )
        self.heightmap_cnn = HeightmapCNN(
            in_channels=hm_channels,
            output_dim=cnn_output,
        )
        self.scalar_proj = nn.Sequential(
            nn.Linear(scalar_dim, scalar_output),
            nn.ReLU(inplace=True),
        )

        # 短期記憶ストリーム（観測辞書に recent_actions 等が含まれる時のみ作る）
        self.has_stm = (
            "recent_actions" in observation_space.spaces
            and "recent_rewards" in observation_space.spaces
            and "recent_results" in observation_space.spaces
        )
        concat_dim = set_output + cnn_output + scalar_output
        if self.has_stm:
            ra_space = observation_space["recent_actions"]
            stm_length = int(ra_space.shape[0])
            act_dim = int(ra_space.shape[1])
            # 入力: action(stm*act_dim) + reward(stm) + result(stm) + mask(stm)
            stm_input_dim = stm_length * act_dim + 3 * stm_length
            self.stm_proj = nn.Sequential(
                nn.Linear(stm_input_dim, stm_output * 2),
                nn.ReLU(inplace=True),
                nn.Linear(stm_output * 2, stm_output),
                nn.ReLU(inplace=True),
            )
            concat_dim += stm_output
        else:
            self.stm_proj = None  # type: ignore[assignment]

        self.fuse = nn.Sequential(
            nn.Linear(concat_dim, features_dim),
            nn.ReLU(inplace=True),
        )

    def forward(self, observations: dict[str, Tensor]) -> Tensor:
        blocks = observations["blocks"]
        mask = observations["blocks_mask"]
        heightmap = observations["heightmap"]
        scalar = observations["tower_top_z"]

        set_emb = self.set_encoder(blocks, mask)
        cnn_emb = self.heightmap_cnn(heightmap)
        scalar_emb = self.scalar_proj(scalar)
        parts = [set_emb, cnn_emb, scalar_emb]

        if self.has_stm and self.stm_proj is not None:
            ra = observations["recent_actions"]   # [B, stm_length, act_dim]
            rr = observations["recent_rewards"]   # [B, stm_length]
            rs = observations["recent_results"]   # [B, stm_length]
            rm = observations["recent_mask"]      # [B, stm_length]
            # mask は action にも掛けて padding 部分を 0 化
            ra_masked = ra * rm.unsqueeze(-1)
            stm_in = torch.cat(
                [
                    ra_masked.flatten(start_dim=1),  # [B, stm_length * act_dim]
                    rr * rm,                          # [B, stm_length]
                    rs * rm,                          # [B, stm_length]
                    rm,                               # [B, stm_length]
                ],
                dim=-1,
            )
            stm_emb = self.stm_proj(stm_in)
            parts.append(stm_emb)

        return self.fuse(torch.cat(parts, dim=-1))

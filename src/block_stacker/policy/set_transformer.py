"""Lightweight Set Transformer for variable-length block sets.

Implements SAB (Self-Attention Block) per Lee et al. 2019, plus a small
encoder that wraps SABs with input/output projections and masked mean
pooling. Padding handled via PyTorch MultiheadAttention's `key_padding_mask`.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    ブロック集合（可変長）を順序不変な埋め込みに変換。設計書 §4 の
    Set Transformer 観測ストリーム実装。

設計上のポイント:
    - SAB: self-attention (residual) + MLP (residual) + LayerNorm。Lee et al. 2019 準拠。
    - mask 規約:
        入力 mask: 1.0 = valid, 0.0 = padded（観測パッキングと整合）
        PyTorch の key_padding_mask: True = ignore（反転して渡す）
    - masked mean pooling: 無効スロットを 0 にして平均。divisor は valid 数。
      → 全 padded 時の zero-division は clamp(min=1.0) で回避。
    - **全 padded（valid ブロック 0 個）対策**: 観測が「散布ブロックのみ」なので、
      全部積み終わると valid 0 個の行が発生しうる。その行は MultiheadAttention が
      softmax(全 -inf) で NaN を出すため、SetEncoder.forward で全 masked 行の先頭スロットを
      valid 化して回避する（pooling は元 mask=0 のままなので pooled は 0 = 「ブロック無し」）。
    - 軽量版（MVP 2 用）: ISAB / PMA 等のフル仕様ではなく SAB 2 層のみ。
      max_blocks=8 程度なら十分な表現力。

レビューで見る観点:
    - 全 padded 行は「散布ブロックのみ」観測で実際に発生する（全部積み終わった瞬間など）。
      attention の NaN は SetEncoder で先頭スロット valid 化、pooling は clamp で二重に防御。
    - hidden_dim / output_dim / n_layers / n_heads は HybridFeatureExtractor から
      渡される（設定 features_extractor_kwargs 経由）。
"""
from __future__ import annotations

import torch.nn as nn
from torch import Tensor


class SAB(nn.Module):
    """Set-Attention Block: self-attention + residual + MLP + residual."""

    def __init__(self, dim: int, n_heads: int = 4, mlp_ratio: float = 2.0) -> None:
        super().__init__()
        self.attn = nn.MultiheadAttention(
            embed_dim=dim, num_heads=n_heads, batch_first=True
        )
        self.norm1 = nn.LayerNorm(dim)
        hidden = max(dim, int(dim * mlp_ratio))
        self.ff = nn.Sequential(
            nn.Linear(dim, hidden),
            nn.GELU(),
            nn.Linear(hidden, dim),
        )
        self.norm2 = nn.LayerNorm(dim)

    def forward(self, x: Tensor, key_padding_mask: Tensor | None = None) -> Tensor:
        """
        Args:
            x: (B, N, dim)
            key_padding_mask: (B, N) bool tensor; True = ignore.
        Returns:
            (B, N, dim)
        """
        # MultiheadAttention は、ある行で全 key が masked だと NaN を出す。
        # 観測が「散布ブロックのみ」になったため、全部積み終わると valid ブロック 0 個の
        # 行が発生しうる。SetEncoder.forward 側で全 masked 行の先頭スロットを valid 化して
        # 回避済み（ここに来る kp_mask は各行に最低 1 個 valid がある前提）。
        attn_out, _ = self.attn(
            x, x, x, key_padding_mask=key_padding_mask, need_weights=False
        )
        x = self.norm1(x + attn_out)
        x = self.norm2(x + self.ff(x))
        return x


class SetEncoder(nn.Module):
    """Embed a padded set into a single vector.

    Padded slots are masked out of attention and the final pooling.
    """

    def __init__(
        self,
        in_dim: int,
        hidden_dim: int = 64,
        output_dim: int = 64,
        n_layers: int = 2,
        n_heads: int = 4,
    ) -> None:
        super().__init__()
        self.input_proj = nn.Linear(in_dim, hidden_dim)
        self.blocks = nn.ModuleList(
            [SAB(hidden_dim, n_heads=n_heads) for _ in range(n_layers)]
        )
        self.output_proj = nn.Linear(hidden_dim, output_dim)

    def forward(self, blocks: Tensor, mask: Tensor) -> Tensor:
        """
        Args:
            blocks: (B, N, in_dim)
            mask:   (B, N) float; 1.0 = valid, 0.0 = padded.
        Returns:
            (B, output_dim)
        """
        # MultiheadAttention's key_padding_mask is True where to *ignore*.
        kp_mask = mask < 0.5
        # 全スロットが padded の行（散布ブロック 0 個 = 全部積み終わった等）は、
        # MultiheadAttention が softmax(全 -inf) で NaN を出す。各行で最低 1 スロットを
        # valid 扱いにして回避する。pooling は元の mask（=0）を使うので、その行の pooled は
        # 0 になり「ブロック無し」を表す finite なベクトルになる。
        all_masked = kp_mask.all(dim=1)
        if bool(all_masked.any()):
            kp_mask = kp_mask.clone()
            kp_mask[all_masked, 0] = False
        x = self.input_proj(blocks)
        for block in self.blocks:
            x = block(x, key_padding_mask=kp_mask)
        # Masked mean pooling over the valid positions.
        m = mask.unsqueeze(-1)
        denom = m.sum(dim=1).clamp(min=1.0)
        pooled = (x * m).sum(dim=1) / denom
        return self.output_proj(pooled)

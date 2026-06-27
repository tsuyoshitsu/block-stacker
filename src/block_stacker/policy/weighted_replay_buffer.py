"""Weighted Replay Buffer for SAC with human-like memory dynamics.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    標準 SAC の ReplayBuffer に「人間っぽい記憶のダイナミクス」を持たせる。
    SB3 の DictReplayBuffer を継承して、以下 4 つの機能を追加：

    1. **重要度の差**: イベント種別ごとに初期重みを変える
       崩落 1.0 > 失敗 0.7 > 新記録 0.5 > 成功 0.3 > 無駄手 0.1
       env.py の step() が info["event_type"] を返すので、それを拾う。

       さらに **直前のタワー高さによる補正** を掛け算で乗せる（任意・config で有効化）:
           initial_weight *= clip(1 + coef × (height_before / reference), 1, max_factor)
       高いタワーで起きた経験ほど「強い記憶」に底上げする。event 種別の相対順位は
       掛け算なので保たれ、max_factor で暴走（過学習）を抑える。
       env.py が info["height_before"]（行動前タワー高さ）を返す。

    2. **時間で薄れる**: 各記憶の重みは 1 step ごとに decay_rate 倍に減衰
       current_weight = initial_weight × decay_rate ^ (現在の step - 生まれた step)
       全部一律に薄れるので「強い記憶が長持ち」の関係は保たれる。

    3. **重みつき sampling**: 学習時、重みに比例した確率で記憶を引く
       重要な記憶 = よく参照される、薄れた記憶 = ほぼ引かれない。

    4. **読み出し時のブレ**: action に重みに反比例したノイズを加える
       blur = max_blur × (1 - 現在の重み)
       強い記憶ほど鮮明、弱い記憶ほど曖昧。

    さらに、バッファ満杯時の eviction を min_weight モード（K-tournament で
    重み最小を押し出す）にすることで、「**重要度の低い古い記憶から忘れる**」を実現。

設計上のポイント:
    - 計算量はオンザフライ: 「現在の重み」は引く時に毎回計算する。
      バッファに保存するのは「初期重み」と「生まれた step」だけで OK。
    - weight_floor: decay で実質ゼロになると数値計算で 0 div になりうる。
      下限 0.001 を設けて回避。同時に「**ほぼ忘れた記憶もたまに思い出す**」効果。
    - eviction K-tournament: 全バッファをスキャンせず、K=16 個の候補を取って
      その中で最小を選ぶ。O(K) で擬似的に min-weight eviction を実現。
    - recall noise は action にのみ適用。観測（盤面）にブレを乗せると state
      transition の整合性が壊れて学習が不安定化するため、ここでは保守的に action のみ。

レビューで見る観点:
    - global_step は env の step とは別カウント（このバッファ内のローカル時刻）。
      n_envs 並列でも一意に進む。並列環境で同 step に複数 add される設計だが、
      全エントリが同じ global_step を共有しても OK（相対比較しか使わない）。
    - 重みつき sampling は SB3 の importance sampling 補正を入れていない。
      これは意図的な選択: 子供メタファーでは「重要な記憶を強く学ぶ」のが目的で、
      理論的な unbiased gradient より優先する。

関連:
    - env/env.py: info dict に event_type を出す
    - configs/training.yaml: memory_system: セクション
    - 設計議論: チャット履歴の「重みつき記憶バッファ」議論
"""
from __future__ import annotations

from typing import Any

import numpy as np
import torch as th
from gymnasium import spaces
from stable_baselines3.common.buffers import DictReplayBuffer
from stable_baselines3.common.type_aliases import DictReplayBufferSamples
from stable_baselines3.common.vec_env import VecNormalize

# デフォルト値（training.yaml で上書き）。
DEFAULT_INITIAL_WEIGHTS: dict[str, float] = {
    "collapse": 1.0,
    "failure": 0.7,
    "height_record": 0.5,
    "success": 0.3,
    "no_progress": 0.1,
}


class WeightedReplayBuffer(DictReplayBuffer):
    """DictReplayBuffer with importance weighting + time decay + recall noise.

    使い方:
        SAC のコンストラクタに以下を渡す:
            replay_buffer_class=WeightedReplayBuffer
            replay_buffer_kwargs=dict(
                initial_weights={"collapse": 1.0, "failure": 0.7, ...},
                decay_rate=0.9999,
                coordinate_blur=0.05,
                eviction="min_weight",
                eviction_tournament_k=16,
                weight_floor=0.001,
                recall_noise_enabled=True,
            )
    """

    def __init__(
        self,
        buffer_size: int,
        observation_space: spaces.Dict,
        action_space: spaces.Space,
        device: th.device | str = "auto",
        n_envs: int = 1,
        optimize_memory_usage: bool = False,
        handle_timeout_termination: bool = True,
        *,
        initial_weights: dict[str, float] | None = None,
        decay_rate: float = 0.9999,
        coordinate_blur: float = 0.05,
        eviction: str = "min_weight",
        eviction_tournament_k: int = 16,
        weight_floor: float = 0.001,
        recall_noise_enabled: bool = True,
        height_weighting_enabled: bool = False,
        height_weight_coef: float = 1.0,
        height_reference: float = 0.10,
        height_max_factor: float = 3.0,
    ) -> None:
        super().__init__(
            buffer_size=buffer_size,
            observation_space=observation_space,
            action_space=action_space,
            device=device,
            n_envs=n_envs,
            optimize_memory_usage=optimize_memory_usage,
            handle_timeout_termination=handle_timeout_termination,
        )

        # 設定値
        self.event_initial_weights: dict[str, float] = (
            initial_weights if initial_weights is not None else DEFAULT_INITIAL_WEIGHTS
        )
        self.decay_rate = float(decay_rate)
        self.coordinate_blur = float(coordinate_blur)
        if eviction not in ("min_weight", "fifo"):
            raise ValueError(f"eviction must be 'min_weight' or 'fifo', got {eviction!r}")
        self.eviction = eviction
        self.tournament_k = int(eviction_tournament_k)
        self.weight_floor = float(weight_floor)
        self.recall_noise_enabled = bool(recall_noise_enabled)

        # 直前タワー高さによる重み補正（高いほど強い記憶に）
        self.height_weighting_enabled = bool(height_weighting_enabled)
        self.height_weight_coef = float(height_weight_coef)
        self.height_reference = float(height_reference)
        self.height_max_factor = float(height_max_factor)

        # 追加の per-entry メタデータ（[buffer_size, n_envs] 形状）
        # 初期重みは「no_progress」相当をデフォルトに、後で add() で上書きされる。
        default_w = self.event_initial_weights.get("no_progress", 0.1)
        self.initial_weights_arr = np.full(
            (self.buffer_size, self.n_envs), default_w, dtype=np.float32
        )
        self.birth_steps = np.zeros((self.buffer_size, self.n_envs), dtype=np.int64)

        # このバッファ独自の時刻カウンタ（env step ではない）
        self.global_step: int = 0

    # ---------------------------------------------------------------- helpers

    def _weight_for_event(self, event_type: str) -> float:
        """Event type → 初期重み。未知の event_type は no_progress 相当に。"""
        return float(self.event_initial_weights.get(
            event_type, self.event_initial_weights.get("no_progress", 0.1)
        ))

    def _height_factor(self, height_before: float) -> float:
        """直前タワー高さ → 重み補正倍率（高いほど大きい、>= 1.0）。

        無効時・基準が非正なら 1.0（補正なし）。height_before<0 は 0 に丸める。
        """
        if not self.height_weighting_enabled or self.height_reference <= 0.0:
            return 1.0
        norm = max(0.0, float(height_before)) / self.height_reference
        factor = 1.0 + self.height_weight_coef * norm
        return float(np.clip(factor, 1.0, self.height_max_factor))

    def _current_weights_for_indices(
        self,
        batch_inds: np.ndarray,
        env_indices: np.ndarray,
    ) -> np.ndarray:
        """指定スロットの現在の重みを計算（time decay を適用）。"""
        ages = self.global_step - self.birth_steps[batch_inds, env_indices]
        # decay_rate ** age は ages が大きいとアンダーフローしうる
        # log 計算を経由して安定化
        log_weights = (
            np.log(np.maximum(self.initial_weights_arr[batch_inds, env_indices], 1e-12))
            + ages * np.log(self.decay_rate)
        )
        weights = np.exp(log_weights)
        return np.maximum(weights, self.weight_floor)

    def _current_weights_all_valid(self) -> np.ndarray:
        """全有効スロットの現在の重み、shape [valid_size, n_envs]。"""
        if self.full:
            valid_size = self.buffer_size
        else:
            valid_size = self.pos
        if valid_size == 0:
            return np.zeros((0, self.n_envs), dtype=np.float32)
        ages = self.global_step - self.birth_steps[:valid_size]
        log_weights = (
            np.log(np.maximum(self.initial_weights_arr[:valid_size], 1e-12))
            + ages * np.log(self.decay_rate)
        )
        weights = np.exp(log_weights)
        return np.maximum(weights, self.weight_floor)

    def _find_min_weight_slot(self) -> int:
        """K-tournament: ランダムに K 個の slot を選び、最小重みのものを返す。"""
        if not self.full:
            # まだ満杯でないなら通常通り pos をそのまま使う
            return int(self.pos)

        k = min(self.tournament_k, self.buffer_size)
        candidates = np.random.randint(0, self.buffer_size, size=k)
        # n_envs が複数なら、各 slot の平均重みを比較対象に
        ages = self.global_step - self.birth_steps[candidates]  # [k, n_envs]
        log_weights = (
            np.log(np.maximum(self.initial_weights_arr[candidates], 1e-12))
            + ages * np.log(self.decay_rate)
        )
        weights = np.exp(log_weights)  # [k, n_envs]
        weights = np.maximum(weights, self.weight_floor)
        mean_weights = weights.mean(axis=1)  # [k]
        min_idx = int(np.argmin(mean_weights))
        return int(candidates[min_idx])

    # ---------------------------------------------------------------- API

    def add(  # type: ignore[override]  # DictReplayBuffer がすでに obs 型を dict に変更済み; mypy は ReplayBuffer 基底と比較するため LSP 警告が出る
        self,
        obs: dict[str, np.ndarray],
        next_obs: dict[str, np.ndarray],
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        infos: list[dict[str, Any]],
    ) -> None:
        # 書き込み先 slot を決定（満杯なら min-weight、それ以外は通常通り pos）
        if self.full and self.eviction == "min_weight":
            self.pos = self._find_min_weight_slot()

        # 通常の DictReplayBuffer.add() ロジックをここに展開
        # （super().add() を呼ぶと自分で pos を進めてしまい min_weight eviction と衝突するため）
        for key in self.observations.keys():
            if isinstance(self.observation_space.spaces[key], spaces.Discrete):
                obs[key] = obs[key].reshape((self.n_envs,) + self.obs_shape[key])
            self.observations[key][self.pos] = np.array(obs[key])
        for key in self.next_observations.keys():
            if isinstance(self.observation_space.spaces[key], spaces.Discrete):
                next_obs[key] = next_obs[key].reshape((self.n_envs,) + self.obs_shape[key])
            self.next_observations[key][self.pos] = np.array(next_obs[key])

        action = action.reshape((self.n_envs, self.action_dim))
        self.actions[self.pos] = np.array(action)
        self.rewards[self.pos] = np.array(reward)
        self.dones[self.pos] = np.array(done)
        if self.handle_timeout_termination:
            self.timeouts[self.pos] = np.array(
                [info.get("TimeLimit.truncated", False) for info in infos]
            )

        # 初期重みと生まれた step を記録。
        # event 種別の重みに、直前タワー高さ補正（高いほど強い記憶）を掛ける。
        weights = np.array(
            [
                self._weight_for_event(info.get("event_type", "no_progress"))
                * self._height_factor(info.get("height_before", 0.0))
                for info in infos
            ],
            dtype=np.float32,
        )
        self.initial_weights_arr[self.pos] = weights
        self.birth_steps[self.pos] = self.global_step
        self.global_step += 1

        # pos 進行（min_weight モードでも一旦進めるが、満杯後は次の add で再選択される）
        self.pos += 1
        if self.pos == self.buffer_size:
            self.full = True
            self.pos = 0

    def sample(  # type: ignore[override]  # DictReplayBuffer がすでに戻り型を DictReplayBufferSamples に変更済み; mypy は ReplayBuffer 基底と比較する
        self,
        batch_size: int,
        env: VecNormalize | None = None,
    ) -> DictReplayBufferSamples:
        # 有効スロットの重みを計算
        weights = self._current_weights_all_valid()  # [valid_size, n_envs]
        if weights.size == 0:
            # 空 → 親実装の uniform sampling にフォールバック（学習開始前のガード）
            return super().sample(batch_size, env=env)

        # 全 (slot, env) ペアを flatten して weighted sampling
        flat_weights = weights.reshape(-1).astype(np.float64)
        total = flat_weights.sum()
        if total <= 0 or not np.isfinite(total):
            # 異常時は uniform に退避
            return super().sample(batch_size, env=env)
        probs = flat_weights / total

        # batch_size 個を確率に応じて抽選
        flat_indices = np.random.choice(len(flat_weights), size=batch_size, p=probs)
        n_envs = weights.shape[1]
        batch_inds = (flat_indices // n_envs).astype(np.int64)
        env_indices = (flat_indices % n_envs).astype(np.int64)

        return self._get_samples_weighted(batch_inds, env_indices, env=env)

    def _get_samples_weighted(
        self,
        batch_inds: np.ndarray,
        env_indices: np.ndarray,
        env: VecNormalize | None,
    ) -> DictReplayBufferSamples:
        """DictReplayBuffer._get_samples を、明示 env_indices + recall noise 付きで再実装。"""
        obs_ = self._normalize_obs(
            {key: arr[batch_inds, env_indices, :] for key, arr in self.observations.items()},
            env,
        )
        next_obs_ = self._normalize_obs(
            {key: arr[batch_inds, env_indices, :] for key, arr in self.next_observations.items()},
            env,
        )
        actions = self.actions[batch_inds, env_indices].copy()

        # Recall noise: 重みが小さいほど大きくブレる
        if self.recall_noise_enabled:
            current_weights = self._current_weights_for_indices(batch_inds, env_indices)
            blur_sigma = self.coordinate_blur * (1.0 - current_weights)  # [batch]
            noise = (
                np.random.normal(0, 1, actions.shape).astype(np.float32)
                * blur_sigma[:, None].astype(np.float32)
            )
            actions = np.clip(actions + noise, -1.0, 1.0)

        assert isinstance(obs_, dict)
        assert isinstance(next_obs_, dict)
        observations = {key: self.to_torch(arr) for key, arr in obs_.items()}
        next_observations = {key: self.to_torch(arr) for key, arr in next_obs_.items()}

        return DictReplayBufferSamples(
            observations=observations,
            actions=self.to_torch(actions),
            next_observations=next_observations,
            dones=self.to_torch(
                self.dones[batch_inds, env_indices]
                * (1 - self.timeouts[batch_inds, env_indices])
            ).reshape(-1, 1),
            rewards=self.to_torch(
                self._normalize_reward(
                    self.rewards[batch_inds, env_indices].reshape(-1, 1), env
                )
            ),
        )

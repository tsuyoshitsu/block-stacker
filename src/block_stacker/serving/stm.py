"""Short-term memory helper for inference (ai_server / live_server).

Mirrors the STM deque logic in env.py so the serving layer can build
observations with the same shape as during training, without importing
the full Gym environment.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from block_stacker.env.env import EVENT_TO_RESULT_SCORE


@dataclass
class ShortTermMemory:
    """推論側（ai_server / live_server）で env.py の短期記憶ロジックを再現するヘルパー。

    学習時と完全に同じ観測形状を組み立てるために、
    env.py の _stm_* deque + _get_obs の pack 処理をミラーリングしている。
    エピソード境界がない「持続ワールド」設計のため、deque は崩落時のみクリア。
    """

    length: int
    action_dim: int = 7
    actions: deque = field(default_factory=lambda: deque())
    rewards: deque = field(default_factory=lambda: deque())
    results: deque = field(default_factory=lambda: deque())

    def __post_init__(self) -> None:
        if self.length > 0:
            self.actions = deque(maxlen=self.length)
            self.rewards = deque(maxlen=self.length)
            self.results = deque(maxlen=self.length)

    def record(self, action: np.ndarray, reward: float, event_type: str) -> None:
        if self.length <= 0:
            return
        self.actions.append(np.asarray(action, dtype=np.float32).copy())
        self.rewards.append(float(reward))
        self.results.append(float(EVENT_TO_RESULT_SCORE.get(event_type, 0.0)))

    def clear(self) -> None:
        self.actions.clear()
        self.rewards.clear()
        self.results.clear()

    def pack_into(self, obs: dict[str, np.ndarray]) -> None:
        L = self.length
        actions_arr = np.zeros((L, self.action_dim), dtype=np.float32)
        rewards_arr = np.zeros((L,), dtype=np.float32)
        results_arr = np.zeros((L,), dtype=np.float32)
        mask_arr = np.zeros((L,), dtype=np.float32)
        for i, (a, r, s) in enumerate(zip(
            reversed(self.actions), reversed(self.rewards), reversed(self.results),
            strict=True,
        )):
            actions_arr[i] = a
            rewards_arr[i] = r
            results_arr[i] = s
            mask_arr[i] = 1.0
        obs["recent_actions"] = actions_arr
        obs["recent_rewards"] = rewards_arr
        obs["recent_results"] = results_arr
        obs["recent_mask"] = mask_arr

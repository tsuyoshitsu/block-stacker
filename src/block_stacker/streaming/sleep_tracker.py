"""Sleep / wake tracking for the streaming protocol.

A block is AWAKE if its linear OR angular velocity exceeds the configured
threshold. After `stable_frames` consecutive frames with both velocities
below threshold it transitions to ASLEEP and the tracker emits a
sleep_event. The reverse transition (any velocity above threshold) emits
a wake_event immediately.

Blocks are initialized as AWAKE so the first frames of a fresh world
still get streamed in `snapshot`s until they settle.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    プロトコル上の AWAKE / ASLEEP 状態を追跡。物理エンジン内部の sleep とは
    独立した「配信向け状態」。SleepTracker が ASLEEP と判定したブロックは
    snapshot から除外され、最終ポーズで sleep_event として確定送信される。

設計上のポイント:
    - 速度ベース判定: |v_linear| < lin_threshold AND |v_angular| < ang_threshold
      が stable_frames フレーム連続で続けば ASLEEP に遷移。デフォルト 30 フレーム
      (240Hz 換算で 0.125 秒)。
    - 判定はヒステリシスなし: 任意のフレームで閾値超 → 即 AWAKE 復帰。
      → 微振動でフラフラ wake/sleep する場合は閾値か stable_frames を調整。
    - PyBullet 内部の per-body sleep（create_block で enable_sleeping=False を
      渡すと無効）とは独立。配信デモでは無効化推奨（外部 perturbation を
      物理エンジンが食わないため）。

レビューで見る観点:
    - register していない body_id を step に渡したらどうなる？
      → was_awake=True デフォルト、初回 stable で sleep に遷移する。
    - sleep_events に渡す final_pose は get_pose で都度取得（ASLEEP 遷移時のみ）。
      → 速度が落ちた瞬間のポーズなので、その後の微小ドリフトは送られない。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pybullet as p

from block_stacker.sim.blocks import get_pose


@dataclass
class SleepTracker:
    lin_threshold: float
    ang_threshold: float
    stable_frames: int

    def __post_init__(self) -> None:
        self._awake: dict[int, bool] = {}
        self._stable_count: dict[int, int] = {}

    def register(self, body_ids: list[int], awake: bool = True) -> None:
        """Initialize tracking state for the given block bodies."""
        for b in body_ids:
            self._awake[b] = awake
            self._stable_count[b] = 0

    def step(
        self, body_ids: list[int]
    ) -> tuple[list[tuple[int, tuple[float, ...]]], list[int]]:
        """Inspect velocities; return (sleep_transitions, wake_transitions).

        sleep_transitions: list of (body_id, final_pose=(px,py,pz,qx,qy,qz,qw))
        wake_transitions:  list of body_id
        """
        sleeps: list[tuple[int, tuple[float, ...]]] = []
        wakes: list[int] = []

        for b in body_ids:
            (lvx, lvy, lvz), (avx, avy, avz) = p.getBaseVelocity(b)
            lin_speed = math.sqrt(lvx * lvx + lvy * lvy + lvz * lvz)
            ang_speed = math.sqrt(avx * avx + avy * avy + avz * avz)
            stable = (
                lin_speed < self.lin_threshold and ang_speed < self.ang_threshold
            )

            was_awake = self._awake.get(b, True)

            if stable:
                self._stable_count[b] = self._stable_count.get(b, 0) + 1
                if was_awake and self._stable_count[b] >= self.stable_frames:
                    self._awake[b] = False
                    pos, quat = get_pose(b)
                    sleeps.append((b, (*pos, *quat)))
            else:
                self._stable_count[b] = 0
                if not was_awake:
                    self._awake[b] = True
                    wakes.append(b)

        return sleeps, wakes

    def awake_ids(self) -> set[int]:
        return {b for b, awake in self._awake.items() if awake}

    def is_awake(self, body_id: int) -> bool:
        return self._awake.get(body_id, True)

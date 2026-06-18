"""Soft 'carrier' constraint to transport a block toward a target.

The carrier is the lower-policy implementation: an invisible anchor point
that the block is softly attached to via a point2point joint with limited
maximum force. The block remains a full dynamic body, so collisions during
transport behave naturally (and can knock the carrier off-track).

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    階層化マニピュレーションの「下位ポリシー」実装。AI（上位）は picked block と
    target pose を指定し、carrier が実際の運搬を担当する。

設計上のポイント:
    - point2point 拘束 + maxForce で「ソフト追従」を実現。
      拘束力に上限があるため、衝突時にブロックが弾かれて目標未達もありうる。
      → 「子供っぽい不器用さ」が自然に出る、設計書 §4 のポリシー。
    - grab_block で wake を強制する: PyBullet 内部 sleep が拘束力を食う問題への対処。
      レビュー時に「明示的 wake が必要な理由」を意識する。
    - 軌道は 3 段階: (1) 上昇 (2) 水平移動 (3) 降下。
      水平レーンの高さは plan_three_phase_lift_height でタワー高に応じ動的決定。
    - trajectory は yield ベース → 1 ステップ 1 ウェイポイントで物理ループに
      同期させる前提（mvp3/ai_server の CarrierDriver.tick と対応）。

レビューで見る観点:
    - lift_height が大きすぎるとブロックが天井（z_max）にぶつかる可能性。
      → 設計値: 作業エリア z_max=3.0、Stage 5 の高層タワーでも 1.0m 以下を想定。
    - _linear_segment の dist < 1e-9 ガードはゼロ距離回避。
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pybullet as p

from block_stacker.config import PhysicsConfig


@dataclass
class Carrier:
    """Holds a block via a soft point2point constraint."""
    block_body_id: int
    constraint_id: int
    target_position: np.ndarray

    def update_target(self, position: tuple[float, float, float]) -> None:
        pos_list = [float(position[0]), float(position[1]), float(position[2])]
        self.target_position = np.array(pos_list, dtype=np.float64)
        p.changeConstraint(self.constraint_id, jointChildPivot=pos_list)

    def release(self) -> None:
        p.removeConstraint(self.constraint_id)


def grab_block(
    block_body_id: int,
    grab_position_world: tuple[float, float, float],
    physics: PhysicsConfig,
) -> Carrier:
    """Attach a soft constraint to a block at the given world target.

    The body is forced awake first so PyBullet's per-body sleep does not
    silently swallow constraint motion.
    """
    p.changeDynamics(block_body_id, -1, activationState=p.ACTIVATION_STATE_WAKE_UP)
    constraint_id = p.createConstraint(
        parentBodyUniqueId=block_body_id,
        parentLinkIndex=-1,
        childBodyUniqueId=-1,
        childLinkIndex=-1,
        jointType=p.JOINT_POINT2POINT,
        jointAxis=[0, 0, 0],
        parentFramePosition=[0, 0, 0],
        childFramePosition=list(grab_position_world),
    )
    p.changeConstraint(constraint_id, maxForce=physics.carrier_max_force)
    return Carrier(
        block_body_id=block_body_id,
        constraint_id=constraint_id,
        target_position=np.array(grab_position_world, dtype=np.float64),
    )


def plan_three_phase_lift_height(
    start_z: float,
    end_z: float,
    tower_top_z: float,
    approach_offset: float,
) -> float:
    """Choose a lift height so the horizontal lane of a three-phase trajectory
    clears `tower_top_z` by at least `approach_offset`.

    The horizontal lane sits at `min(start_z, end_z) + lift`, so we need
    lift >= (tower_top + approach_offset) - min(start_z, end_z). Floored at
    `approach_offset` so the block always lifts a little even with no tower.
    """
    lane_floor = min(start_z, end_z)
    required = (tower_top_z + approach_offset) - lane_floor
    return max(approach_offset, required)


def _linear_segment(
    a: np.ndarray, b: np.ndarray, speed: float, dt: float
) -> Iterator[tuple[float, float, float]]:
    """Yield waypoints from a to b at constant speed, spaced by dt."""
    dist = float(np.linalg.norm(b - a))
    if dist < 1e-9:
        yield (float(b[0]), float(b[1]), float(b[2]))
        return
    n_steps = max(1, int(np.ceil(dist / (speed * dt))))
    for i in range(1, n_steps + 1):
        t = i / n_steps
        pt = a + (b - a) * t
        yield (float(pt[0]), float(pt[1]), float(pt[2]))


def trajectory_three_phase(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    lift_height: float,
    speed: float,
    dt: float,
) -> Iterator[tuple[float, float, float]]:
    """3-phase trajectory: lift up, horizontal move, descend.

    Each yielded waypoint is the target for the carrier at that physics step.
    """
    start_arr = np.array(start, dtype=np.float64)
    end_arr = np.array(end, dtype=np.float64)
    top_start = start_arr + np.array([0.0, 0.0, lift_height])
    top_end = end_arr + np.array([0.0, 0.0, lift_height])

    yield from _linear_segment(start_arr, top_start, speed, dt)
    yield from _linear_segment(top_start, top_end, speed, dt)
    yield from _linear_segment(top_end, end_arr, speed, dt)

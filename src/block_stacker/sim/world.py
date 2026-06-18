"""World setup: ground, walls, gravity, solver configuration.

Provides a thin wrapper around PyBullet for spawning a working area
consistent with the configs in `configs/world.yaml` and `configs/physics.yaml`.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    PyBullet クライアントの起動、地面、壁、重力、ソルバ反復回数等の
    シミュレーション基盤を構築。返り値の World dataclass に
    physics_client_id を保持し、テスト後の disconnect で安全に解放。

設計上のポイント:
    - 内部レート 240Hz / ソルバ反復 100 / use_split_impulse=True / enable_cone_friction=1
      は積み重ね安定化のためのチューニング（設計書 §5）。
    - 壁の type:
        "invisible_walls" - 作業エリア周囲に薄い壁を配置（推奨デフォルト）
        その他           - 壁なし
      壁高は config.yaml の boundary.height（デフォルト 1.0m）。
      → AI が place_z を 1.0m 超に出す場合、設計許容範囲を超えるので壁を抜ける可能性あり。

レビューで見る観点:
    - 複数 client 並列化（DummyVecEnv n>1）には未対応。
      PyBullet 関数の physicsClientId を全関数に通す改修が必要。
    - 壁は質量 0 の static body。restitution は physics.yaml の wall 値。
"""
from __future__ import annotations

from dataclasses import dataclass

import pybullet as p

from block_stacker.config import PhysicsConfig, WorldConfig


@dataclass
class World:
    physics_client_id: int
    ground_id: int
    wall_ids: list[int]
    config_world: WorldConfig
    config_physics: PhysicsConfig

    def step(self, n: int = 1) -> None:
        for _ in range(n):
            p.stepSimulation(physicsClientId=self.physics_client_id)

    def disconnect(self) -> None:
        try:
            p.disconnect(physicsClientId=self.physics_client_id)
        except Exception:
            pass


def setup_world(
    config_world: WorldConfig,
    config_physics: PhysicsConfig,
    gui: bool = False,
) -> World:
    """Initialize PyBullet with ground, walls, gravity, and solver tuning."""
    mode = p.GUI if gui else p.DIRECT
    cid = p.connect(mode)

    p.setGravity(*config_physics.gravity, physicsClientId=cid)
    p.setTimeStep(1.0 / config_physics.internal_rate_hz, physicsClientId=cid)

    p.setPhysicsEngineParameter(
        numSolverIterations=config_physics.solver_iterations,
        useSplitImpulse=1 if config_physics.use_split_impulse else 0,
        splitImpulsePenetrationThreshold=-0.02,
        enableConeFriction=1,
        deterministicOverlappingPairs=1,
        physicsClientId=cid,
    )

    ground_id = _spawn_ground(config_world, config_physics, cid)
    wall_ids = _spawn_walls(config_world, config_physics, cid)

    return World(
        physics_client_id=cid,
        ground_id=ground_id,
        wall_ids=wall_ids,
        config_world=config_world,
        config_physics=config_physics,
    )


def _spawn_ground(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, cid: int
) -> int:
    gx, gy = world_cfg.ground_size
    half = [gx / 2.0, gy / 2.0, 0.01]
    col = p.createCollisionShape(p.GEOM_BOX, halfExtents=half, physicsClientId=cid)
    vis = p.createVisualShape(
        p.GEOM_BOX, halfExtents=half, rgbaColor=[0.4, 0.4, 0.4, 1.0], physicsClientId=cid
    )
    body = p.createMultiBody(
        baseMass=0.0,
        baseCollisionShapeIndex=col,
        baseVisualShapeIndex=vis,
        basePosition=[0.0, 0.0, -0.01],
        physicsClientId=cid,
    )
    p.changeDynamics(
        body,
        -1,
        lateralFriction=physics_cfg.friction_block_ground,
        restitution=physics_cfg.restitution_ground,
        physicsClientId=cid,
    )
    return body


def _spawn_walls(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, cid: int
) -> list[int]:
    if world_cfg.boundary_type != "invisible_walls":
        return []

    x_min, x_max = world_cfg.x_range
    y_min, y_max = world_cfg.y_range
    h = world_cfg.boundary_height
    t = 0.02  # wall thickness
    half_h = h / 2.0
    half_x_span = (x_max - x_min) / 2.0 + t
    half_y_span = (y_max - y_min) / 2.0 + t

    wall_specs = [
        # (center, half_extents)
        ((x_min - t / 2, 0.0, half_h), (t / 2, half_y_span, half_h)),
        ((x_max + t / 2, 0.0, half_h), (t / 2, half_y_span, half_h)),
        ((0.0, y_min - t / 2, half_h), (half_x_span, t / 2, half_h)),
        ((0.0, y_max + t / 2, half_h), (half_x_span, t / 2, half_h)),
    ]

    wall_ids: list[int] = []
    for center, half_ext in wall_specs:
        col = p.createCollisionShape(
            p.GEOM_BOX, halfExtents=list(half_ext), physicsClientId=cid
        )
        body = p.createMultiBody(
            baseMass=0.0,
            baseCollisionShapeIndex=col,
            basePosition=list(center),
            physicsClientId=cid,
        )
        p.changeDynamics(
            body,
            -1,
            lateralFriction=physics_cfg.friction_block_wall,
            restitution=physics_cfg.restitution_wall,
            physicsClientId=cid,
        )
        wall_ids.append(body)
    return wall_ids

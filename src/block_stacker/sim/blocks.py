"""Block creation and mass computation.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    積み木ブロックの spawn と幾何プロパティ計算。形状種別ごとの体積から
    密度経由で質量を導出。PyBullet body の作成と物性設定（摩擦・反発・
    ダンピング・接触剛性）を一括で行う。

設計上のポイント:
    - 対応形状: box (cube / cuboid)、cylinder、triangular_prism（直角二等辺三角柱）
    - 三角柱は PyBullet 組込プリミティブが無いので GEOM_MESH で凸包メッシュとして実装。
      頂点 6 個・面 8 個（三角形面 2 + 矩形面 3、矩形は 2 三角形ずつ）の最小構成。
      軸は X、断面（直角二等辺三角形）は YZ 平面。leg-rectangle を下にして安定に置く前提。
    - enable_sleeping パラメータ:
        True  (デフォルト): 訓練効率重視。PyBullet 内部 sleep が有効。
        False (streaming): 外部から resetBaseVelocity や constraint で動かす
                          シーンでは内部 sleep が干渉するので無効化推奨。
    - physics=None でも changeDynamics で activationState だけは設定する。
      （内部 sleep の挙動を呼び出し側でコントロール可能にするため）

レビューで見る観点:
    - 慣性テンソルは PyBullet にデフォルト計算を任せている（明示しない）。
      高層タワー安定化で問題があれば changeDynamics(localInertiaDiagonal=...)
      を追加検討。
    - 円柱は転がりやすいので rolling_friction を physics.yaml に明示する設計。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pybullet as p

from block_stacker.config import PhysicsConfig, ShapeSpec


@dataclass
class Block:
    body_id: int
    shape: ShapeSpec
    mass: float


def compute_mass(shape: ShapeSpec) -> float:
    """Return mass = volume(shape) * density."""
    if shape.type == "box":
        w, h, d = shape.dims
        volume = w * h * d
    elif shape.type == "cylinder":
        r, h = shape.dims
        volume = math.pi * r * r * h
    elif shape.type == "triangular_prism":
        # 直角二等辺三角柱: 断面積 = (1/2) * L^2, 体積 = 断面積 * 長さ
        leg, prism_length = shape.dims
        volume = 0.5 * leg * leg * prism_length
    else:
        raise ValueError(f"Unsupported shape type: {shape.type}")
    return volume * shape.density


def _triangular_prism_vertices(leg: float, prism_length: float) -> list[list[float]]:
    """直角二等辺三角柱の頂点 6 個を生成（centroid 中心、axis 沿い X）。

    断面（YZ 平面）の直角三角形:
        (y, z) = (0, 0), (leg, 0), (0, leg)
        centroid = (leg/3, leg/3) → 中心化のため YZ 全頂点から (leg/3, leg/3) を引く

    結果: 安定姿勢は z = -leg/3 の面（y-leg rectangle）が下、
          y = -leg/3 の面（z-leg rectangle）が側面、
          斜面が上の hypotenuse-rectangle。
    """
    L = float(leg)
    P = float(prism_length) / 2.0
    cy = L / 3.0
    cz = L / 3.0
    return [
        [-P, 0.0 - cy, 0.0 - cz],   # 0: back, 直角コーナー
        [-P, L - cy, 0.0 - cz],     # 1: back, y-leg 先端
        [-P, 0.0 - cy, L - cz],     # 2: back, z-leg 先端
        [ P, 0.0 - cy, 0.0 - cz],   # 3: front, 直角コーナー
        [ P, L - cy, 0.0 - cz],     # 4: front, y-leg 先端
        [ P, 0.0 - cy, L - cz],     # 5: front, z-leg 先端
    ]


def _triangular_prism_indices() -> list[int]:
    """8 三角形（24 indices）で三角柱の表面を構成。

    面の構成:
      - 三角形 2 個（前面・後面）
      - 矩形 3 個（底・側・斜面）、各矩形は 2 三角形に分割
    """
    return [
        # 後面（back triangle, normal -X）
        0, 1, 2,
        # 前面（front triangle, normal +X）
        5, 4, 3,
        # 底面（z = -leg/3 の矩形、y-leg rectangle）
        0, 3, 4, 0, 4, 1,
        # 側面（y = -leg/3 の矩形、z-leg rectangle）
        0, 2, 5, 0, 5, 3,
        # 斜面（hypotenuse rectangle）
        1, 4, 5, 1, 5, 2,
    ]


def create_block(
    shape: ShapeSpec,
    position: tuple[float, float, float],
    orientation_quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    physics: PhysicsConfig | None = None,
    enable_sleeping: bool = True,
) -> Block:
    """Spawn a block. Returns a Block dataclass with PyBullet body id.

    `enable_sleeping=True` (default) keeps PyBullet's per-body sleeping for
    training efficiency. Streaming demos that need to drive bodies via
    `resetBaseVelocity` or constraints should pass `False` so external
    perturbations are not absorbed by the internal sleep state.
    """
    if shape.type == "box":
        half_extents = [d / 2.0 for d in shape.dims]
        col = p.createCollisionShape(p.GEOM_BOX, halfExtents=half_extents)
        vis = p.createVisualShape(
            p.GEOM_BOX, halfExtents=half_extents, rgbaColor=shape.color
        )
    elif shape.type == "cylinder":
        radius, height = shape.dims
        col = p.createCollisionShape(p.GEOM_CYLINDER, radius=radius, height=height)
        vis = p.createVisualShape(
            p.GEOM_CYLINDER, radius=radius, length=height, rgbaColor=shape.color
        )
    elif shape.type == "triangular_prism":
        leg, prism_length = shape.dims
        verts = _triangular_prism_vertices(leg, prism_length)
        indices = _triangular_prism_indices()
        # 衝突形状は頂点のみ渡して PyBullet の凸包構築に任せる（軽量）
        col = p.createCollisionShape(p.GEOM_MESH, vertices=verts)
        # ビジュアルは面構成（indices）も渡してきちんとレンダリング
        vis = p.createVisualShape(
            p.GEOM_MESH, vertices=verts, indices=indices, rgbaColor=shape.color,
        )
    else:
        raise ValueError(f"Unsupported shape type: {shape.type}")

    mass = compute_mass(shape)
    body_id = p.createMultiBody(
        baseMass=mass,
        baseCollisionShapeIndex=col,
        baseVisualShapeIndex=vis,
        basePosition=list(position),
        baseOrientation=list(orientation_quat),
    )

    activation = (
        p.ACTIVATION_STATE_ENABLE_SLEEPING
        if enable_sleeping
        else p.ACTIVATION_STATE_DISABLE_SLEEPING
    )
    if physics is not None:
        p.changeDynamics(
            body_id,
            -1,
            lateralFriction=physics.friction_block_block,
            rollingFriction=physics.rolling_friction,
            spinningFriction=physics.spinning_friction,
            restitution=physics.restitution_block,
            linearDamping=physics.damping_linear,
            angularDamping=physics.damping_angular,
            contactStiffness=physics.contact_stiffness,
            contactDamping=physics.contact_damping,
            activationState=activation,
        )
    else:
        p.changeDynamics(body_id, -1, activationState=activation)

    return Block(body_id=body_id, shape=shape, mass=mass)


def get_pose(body_id: int) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    """Return (position, quaternion) for the given body."""
    pos, quat = p.getBasePositionAndOrientation(body_id)
    return tuple(pos), tuple(quat)


def get_velocity(body_id: int) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return (linear, angular) velocities."""
    lin, ang = p.getBaseVelocity(body_id)
    return tuple(lin), tuple(ang)


def reset_pose(
    body_id: int,
    pos: tuple[float, float, float],
    quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
) -> None:
    """ブロックを指定姿勢へテレポートし、速度をゼロに戻す（デモの再配置などで使う）。"""
    p.resetBasePositionAndOrientation(body_id, pos, quat)
    p.resetBaseVelocity(body_id, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])


def find_nearest_excluding(
    blocks: list[Block],
    query: tuple[float, float, float],
    excluded_ids: set[int],
) -> Block | None:
    """Return the block closest (3D euclidean) to `query` whose body_id is not
    in `excluded_ids`, or None if no candidate exists."""
    best: Block | None = None
    best_dist2 = float("inf")
    for b in blocks:
        if b.body_id in excluded_ids:
            continue
        pos, _ = p.getBasePositionAndOrientation(b.body_id)
        dx = pos[0] - query[0]
        dy = pos[1] - query[1]
        dz = pos[2] - query[2]
        d2 = dx * dx + dy * dy + dz * dz
        if d2 < best_dist2:
            best_dist2 = d2
            best = b
    return best

"""Heightmap computation via downward ray casting.

Produces a 4-channel (H, dz/dx, dz/dy, |grad|) tensor over the work area.
This is the second observation stream alongside the per-block features.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    AI 観測の 2 ストリーム目「空間勾配」を生成。各セルの高度と勾配で
    「どこが平らか」を CNN に伝える（設計書 §4）。

設計上のポイント:
    - 解像度 32×32 over 2m×2m → セルサイズ 6.25cm。
      5cm 立方体は 1 セルに収まらない可能性があるので、観測の精度は粗め。
    - rayTestBatch で 1024 本のレイを一括キャスト。240Hz の物理ループ内で
      頻繁に呼ぶと重いため、AI 観測時 (~3 秒に 1 回) のみ実行。
    - ignore_body_ids: 壁が高度 1.0m として観測されないように除外する。
      MVP 0 でこのバグが出た（壁の上端を地面と誤認）→ ignore_body_ids 追加。
    - 4 ch:
        ch 0  height z(x, y)
        ch 1  dz/dx
        ch 2  dz/dy
        ch 3  |∇z| （平坦度の直接指標）

レビューで見る観点:
    - np.gradient の軸順: numpy では (axis0, axis1) = (d/dy, d/dx) なので注意。
"""
from __future__ import annotations

import numpy as np
import pybullet as p

from block_stacker.config import WorldConfig


def compute_heightmap(
    world_config: WorldConfig,
    resolution: int = 32,
    ignore_body_ids: list[int] | None = None,
) -> np.ndarray:
    """Cast downward rays in a regular grid and return (4, H, W) float32.

    Channels:
        0: height z(x, y)
        1: dz/dx
        2: dz/dy
        3: |grad| = sqrt(dz/dx^2 + dz/dy^2)

    Args:
        ignore_body_ids: bodies whose hits should be treated as "no hit"
            (e.g. invisible walls that would otherwise dominate the map).
    """
    ignore = set(ignore_body_ids or [])

    x_min, x_max = world_config.x_range
    y_min, y_max = world_config.y_range
    z_top = world_config.z_max
    z_bottom = -0.01

    xs = np.linspace(x_min, x_max, resolution)
    ys = np.linspace(y_min, y_max, resolution)

    ray_from: list[list[float]] = []
    ray_to: list[list[float]] = []
    for y in ys:
        for x in xs:
            ray_from.append([float(x), float(y), float(z_top)])
            ray_to.append([float(x), float(y), float(z_bottom)])

    results = p.rayTestBatch(ray_from, ray_to)

    heights = np.zeros((resolution, resolution), dtype=np.float32)
    span = z_top - z_bottom
    for idx, res in enumerate(results):
        i = idx // resolution  # row = y
        j = idx % resolution   # col = x
        hit_body = res[0]
        hit_fraction = res[2]
        if hit_fraction < 1.0 and hit_body not in ignore:
            heights[i, j] = float(z_top - hit_fraction * span)
        else:
            heights[i, j] = 0.0  # no hit (or ignored), ground level

    # np.gradient returns (axis0, axis1) = (d/dy, d/dx) for shape (H, W)
    dz_dy, dz_dx = np.gradient(heights)
    grad_mag = np.sqrt(dz_dx * dz_dx + dz_dy * dz_dy).astype(np.float32)

    return np.stack([heights, dz_dx.astype(np.float32), dz_dy.astype(np.float32), grad_mag], axis=0)

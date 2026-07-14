"""Action decoding module.

7-dim continuous action in [-1, 1]:
    [0..2] pickup_query xyz
    [3..5] place_xyz
    [6]    place_yaw

XY are mapped linearly to work_area ranges.
Z is mapped to [0, place_z_max] — see below for how place_z_max is determined.
Yaw is mapped to [-pi, pi].

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    SAC の連続行動 [-1, 1]^7 を実世界座標 / yaw 角に展開。
    SB3 のガウス方策の出力は範囲外を含むため np.clip で念のため保護。

設計上のポイント:
    - pickup_xyz: 「この座標に最も近い散乱ブロックを拾う」のクエリ。
      実際の選択は env._find_nearest_scattered (find_nearest_excluding) で argmin。
    - place_xyz: 配置目標。z 範囲は [0, place_z_max_eff]。
      place_z_max_eff は以下で算出（壁高クランプあり）:
        place_z_max_eff = min(z_max/2, boundary_height - _APPROACH_OFFSET - _WALL_MARGIN)
      キャリア軌跡のピーク = place_z + lift_height（最小 = approach_offset）。
      ピーク ≤ place_z_max_eff + approach_offset
      = boundary_height - _WALL_MARGIN < boundary_height。
      → AI が壁高を超える配置を指示しても確実に壁内に収まる。
    - place_yaw: cube では使わない（回転対称）。立方体以外の形状で必要。

レビューで見る観点:
    - _APPROACH_OFFSET は physics.yaml の carrier_constraint.approach_height_offset と
      同値にすること（physics_cfg の引き回しを避けるため定数で持つ）。
    - place_z_max は呼び出し側で上書き可能（デモで範囲を絞るケース向け）。
"""
from __future__ import annotations

import math

import numpy as np

from block_stacker.config import WorldConfig

ACTION_DIM = 7

# physics.yaml carrier_constraint.approach_height_offset と同値。
# キャリアが目標より _APPROACH_OFFSET だけ高い地点を経由するため、
# 軌跡ピーク = place_z + lift_height（最小 = _APPROACH_OFFSET）。
_APPROACH_OFFSET: float = 0.05
# ピーク高さと壁高さの最小差分（確実に壁内に収める安全マージン）。
_WALL_MARGIN: float = 0.02


def _to_range(a: float, lo: float, hi: float) -> float:
    return float(lo + (a + 1.0) / 2.0 * (hi - lo))


def decode_action(
    action: np.ndarray,
    world_cfg: WorldConfig,
    place_z_max: float | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
    """Return ((pickup_x, pickup_y, pickup_z), (place_x, place_y, place_z), yaw)."""
    if place_z_max is None:
        z_half = world_cfg.z_max / 2.0
        # 壁高クランプ: ピーク(place_z + approach_offset) が壁未満になるよう上限を設ける。
        wall_cap = world_cfg.boundary_height - _APPROACH_OFFSET - _WALL_MARGIN
        place_z_max = min(z_half, wall_cap)

    a = np.clip(np.asarray(action, dtype=np.float64), -1.0, 1.0)
    x_min, x_max = world_cfg.x_range
    y_min, y_max = world_cfg.y_range

    pickup = (
        _to_range(a[0], x_min, x_max),
        _to_range(a[1], y_min, y_max),
        _to_range(a[2], 0.0, place_z_max),
    )
    place = (
        _to_range(a[3], x_min, x_max),
        _to_range(a[4], y_min, y_max),
        _to_range(a[5], 0.0, place_z_max),
    )
    yaw = float(a[6] * math.pi)
    return pickup, place, yaw

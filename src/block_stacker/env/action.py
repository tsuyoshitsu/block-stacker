"""Action decoding for MVP 1.

7-dim continuous action in [-1, 1]:
    [0..2] pickup_query xyz
    [3..5] place_xyz
    [6]    place_yaw

XY are mapped linearly to work_area ranges.
Z is mapped to [0, z_max / 2] (we don't ask the AI to place above some cap).
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
    - place_xyz: 配置目標。z 範囲は [0, z_max/2]。
      → 壁高 1.0m に対し z_max/2 = 1.5m なので、AI が壁高超に出す可能性あり。
      MVP 3.5 の eval で実際に block が壁を超えた事例を確認（許容、設計書 §5）。
    - place_yaw: cube では使わない（回転対称）。立方体以外の形状で必要。

レビューで見る観点:
    - place_z_max は world_cfg.z_max / 2 がデフォルト。デモで配置範囲を絞りたい
      なら呼び出し側でカスタム値を渡せる。
"""
from __future__ import annotations

import math

import numpy as np

from block_stacker.config import WorldConfig

ACTION_DIM = 7


def _to_range(a: float, lo: float, hi: float) -> float:
    return float(lo + (a + 1.0) / 2.0 * (hi - lo))


def decode_action(
    action: np.ndarray,
    world_cfg: WorldConfig,
    place_z_max: float | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
    """Return ((pickup_x, pickup_y, pickup_z), (place_x, place_y, place_z), yaw)."""
    if place_z_max is None:
        place_z_max = world_cfg.z_max / 2.0

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

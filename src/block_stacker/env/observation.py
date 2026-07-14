"""Observation builders for the block stacking env.

per-block の枠には **散布（タワー未所属）ブロックだけ** を、タワー土台への距離が
近い順に入れる。先頭 `max_blocks` 個を超えた遠い散布ブロックは捨てる（= 子供の狭い視野）。
**積まれたブロックは per-block には入れず、heightmap が山として表現する**。
この設計により、世界の合計ブロック数は `max_blocks` を超えてよい
（同時に枠に映るのは「近い散布ブロック最大 max_blocks 個」だけ）。

Output format: **dict** (Set Transformer + CNN 用):
      "blocks":      (max_blocks, per_block_dims)  float32, padded（散布のみ）
      "blocks_mask": (max_blocks,)                 float32, 1.0 = valid
      "heightmap":   (4, H, W)                     float32, raycast result（積まれた山を表現）
      "tower_top_z": (1,)                          float32

Per-block features (13 + K dims, K = number of known shapes):
    pos_x, pos_y, pos_z                              # 3
    quat_x, quat_y, quat_z, quat_w                   # 4
    distance_to_tower_base                            # 1
    is_in_tower_flag                                  # 1（散布のみなので常に 0。枠の形は維持）
    exp(-distance_to_tower_base / DISTANCE_SCALE)     # 1
    shape_type_one_hot[K]                             # K
    bbox_w, bbox_h, bbox_d                            # 3

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    AI 観測の per-block feature 構築。設計書 §4 の per_block_vector を
    実装したもの。flat と dict の 2 形式を提供。

設計上のポイント:
    - 並び: タワー base からの 2D 距離でソート → top-K に切り詰め。
      Set Transformer は順序不変なので並びは意味的には無関係。
    - distance_to_tower_base:「子供メタファー」に基づき、タワー根本を
      基準に近接特徴を入れる。設計書 §4 参照。
    - exp(-d/scale): 距離の近接バイアス。DISTANCE_SCALE=0.5m で減衰。
      近いブロックを attention で強調しやすくする補助特徴。
    - shape_one_hot[K]: world.yaml の shapes キー順にインデックス。
      world.yaml を編集して shape を追加/削除した場合、訓練済みモデルとの
      互換性が崩れるので注意。
    - bbox: AABB の物理的サイズ。box はそのまま、cylinder は (2r, 2r, h) に変換。
    - NaN/Inf 姿勢のブロック（物理が一時的に破綻したとき）は per-block 枠に入れず除外し、
      有効な散布ブロックだけを観測対象にする（mask も valid 分だけ立つ）。残った配列値は
      _sanitize で最終防御クリップする。

レビューで見る観点:
    - K (n_shapes) は env コンストラクタ時の世界設定で決まる。
      学習時と推論時で同じ世界設定を使うことが前提（serving/ai_server も同様）。
"""
from __future__ import annotations

import math

import numpy as np

from block_stacker.config import ShapeSpec
from block_stacker.sim.blocks import Block, get_pose

# Scale for the proximity bias term exp(-d/scale). Tuned for the ~2m work area.
DISTANCE_SCALE = 0.5

# 観測の安全クリップ上限。物理が一時的に不安定になり、ブロックの姿勢が NaN や極端値に
# なったとき、それが観測→NN に伝播してクラッシュ（torch の Normal 分布が nan を拒否）
# するのを防ぐ防御。正常な観測値（位置 ~±3, 距離 ~3, 高さ ~3, 勾配 ~7）は十分内側なので歪まない。
OBS_CLIP = 50.0


def _sanitize(arr: np.ndarray) -> np.ndarray:
    """NaN/Inf を 0/±OBS_CLIP に置換し、極端値を [-OBS_CLIP, OBS_CLIP] にクリップ（in-place）。"""
    np.nan_to_num(arr, copy=False, nan=0.0, posinf=OBS_CLIP, neginf=-OBS_CLIP)
    np.clip(arr, -OBS_CLIP, OBS_CLIP, out=arr)
    return arr


def _all_finite(values: tuple[float, ...]) -> bool:
    """全成分が有限（NaN/Inf でない）か。NaN 姿勢のブロックを観測から外す判定に使う。"""
    return all(math.isfinite(v) for v in values)

# Fixed per-block dims (excluding the shape one-hot which has variable length).
_FIXED_DIMS = 3 + 4 + 1 + 1 + 1 + 3  # = 13


def per_block_dims(n_shapes: int) -> int:
    return _FIXED_DIMS + n_shapes


def _bbox_dims(shape: ShapeSpec) -> tuple[float, float, float]:
    """Axis-aligned bounding box dimensions in the block's local frame."""
    if shape.type == "box":
        return (float(shape.dims[0]), float(shape.dims[1]), float(shape.dims[2]))
    if shape.type == "cylinder":
        r, h = float(shape.dims[0]), float(shape.dims[1])
        return (2.0 * r, 2.0 * r, h)
    if shape.type == "triangular_prism":
        # 直角二等辺三角柱: 軸沿い X (= prism_length), 断面 YZ (Y = Z = leg)
        leg, prism_length = float(shape.dims[0]), float(shape.dims[1])
        return (prism_length, leg, leg)
    return (0.05, 0.05, 0.05)


def _pack_one(
    block: Block,
    pos: tuple[float, float, float],
    quat: tuple[float, float, float, float],
    dist: float,
    in_tower: float,
    shape_index: dict[str, int],
    n_shapes: int,
    out: np.ndarray,
) -> None:
    """Write one block's features into `out` (slice already sized to per_block_dims)."""
    out[0] = pos[0]
    out[1] = pos[1]
    out[2] = pos[2]
    out[3] = quat[0]
    out[4] = quat[1]
    out[5] = quat[2]
    out[6] = quat[3]
    out[7] = dist
    out[8] = in_tower
    out[9] = math.exp(-dist / DISTANCE_SCALE)
    one_hot_base = 10
    idx = shape_index.get(block.shape.name, -1)
    if 0 <= idx < n_shapes:
        out[one_hot_base + idx] = 1.0
    bbox_base = one_hot_base + n_shapes
    bw, bh, bd = _bbox_dims(block.shape)
    out[bbox_base + 0] = bw
    out[bbox_base + 1] = bh
    out[bbox_base + 2] = bd


def _sorted_block_rows(
    blocks: list[Block],
    tower_block_ids: set[int],
    reference_xy: tuple[float, float],
) -> list[tuple[float, Block, tuple, tuple, float]]:
    """散布（タワー未所属）ブロックだけを、`reference_xy` への 2D 距離の近い順で返す。

    タワーに積まれたブロックは per-block には含めない（積まれた山は heightmap が表現する）。
    呼び出し側は先頭 max_blocks 個に切り詰める → 観測枠は「拾える散布ブロックの近い順トップ K」。
    遠くの散布ブロックは枠からあぶれて見えない＝「子供の狭い視野」メタファー。
    """
    rows = []
    for block in blocks:
        if block.body_id in tower_block_ids:
            continue  # 積まれたブロックは除外（heightmap で表現する）
        pos, quat = get_pose(block.body_id)
        # 物理破綻でブロックの姿勢が NaN/Inf になることがある。観測できない（壊れた）
        # ブロックは枠に入れず無視し、有効な姿勢の散布ブロックだけを観測対象にする。
        # → ゼロ埋めで「原点のブロック」に見せず、NN には見えていないものとして扱う。
        if not (_all_finite(pos) and _all_finite(quat)):
            continue
        dx = pos[0] - reference_xy[0]
        dy = pos[1] - reference_xy[1]
        dist = float(math.sqrt(dx * dx + dy * dy))
        # in_tower は散布ブロックのみなので常に 0.0（特徴の形は維持）。
        rows.append((dist, block, pos, quat, 0.0))
    rows.sort(key=lambda r: r[0])
    return rows


def pack_observation_dict(
    blocks: list[Block],
    tower_block_ids: set[int],
    max_blocks: int,
    tower_top_z: float,
    shape_index: dict[str, int],
    n_shapes: int,
    heightmap: np.ndarray,
    reference_xy: tuple[float, float] = (0.0, 0.0),
) -> dict[str, np.ndarray]:
    """Dict observation: structured for Set Transformer + CNN."""
    pb_dims = per_block_dims(n_shapes)

    blocks_arr = np.zeros((max_blocks, pb_dims), dtype=np.float32)
    mask = np.zeros((max_blocks,), dtype=np.float32)

    rows = _sorted_block_rows(blocks, tower_block_ids, reference_xy)
    for i, (dist, block, pos, quat, in_tower) in enumerate(rows[:max_blocks]):
        _pack_one(block, pos, quat, dist, in_tower, shape_index, n_shapes,
                  blocks_arr[i])
        mask[i] = 1.0

    return {
        "blocks": _sanitize(blocks_arr),
        "blocks_mask": mask,
        "heightmap": _sanitize(heightmap.astype(np.float32)),
        "tower_top_z": _sanitize(np.array([tower_top_z], dtype=np.float32)),
    }



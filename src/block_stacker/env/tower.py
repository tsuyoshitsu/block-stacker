"""Tower detection.

The 'tower' is defined as the highest connected component (via contact
graph) that includes the ground. Updated when blocks settle or the
contact topology changes.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    現在の「タワー」を判定する。設計書 §5 のタワー定義実装。

設計上のポイント:
    - 接触グラフはブロック同士のみ（地面は除外して連結成分分解）。さらに
      **接触法線が縦向きのもの（上下の積み重なり・斜面）だけをエッジにする**ので、
      横並びの塊は別成分に分かれ、タワー＝「縦に連なった構造」になる（VERTICAL_NORMAL_MIN）。
      地面接触するブロックを含む成分のうち、最高 z の成分が「タワー」。
      → 複数の単独ブロックが地面に置かれている状態を全部「タワー」と判定する
      MVP 0 初期のバグを修正した実装（MVP 1 改善時）。
    - height = max(getAABB top_z)。形状の bbox 頂点を取るので、ブロックの
      回転後の高さも正しく取れる。
    - tower_base_xy: タワー所属で z が最低 (1cm 以内) のブロックの xy 重心。
      観測の reference_xy として使う（子供メタファーの "タワー根本"）。

レビューで見る観点:
    - PyBullet の getContactPoints は前フレームの接触結果を返す。
      物理ステップ直後に呼ぶ前提。
    - 縦連結のみ判定: 接触法線 Z でフィルタするので「横並びの塊」は 1 タワーにならない。
      斜面（45°, normal_z≈0.707）に乗ったブロックは縦連結に含む（VERTICAL_NORMAL_MIN=0.5）。
"""
from __future__ import annotations

import pybullet as p

# 縦連結とみなす接触法線 Z の最小値（|normal_z| >= これ で「上下の積み重なり」扱い）。
#   横並びの接触: normal_z ≈ 0.0  → 除外（実測確認済み）
#   縦積みの接触: normal_z ≈ 1.0  → 含む
#   45°斜面の接触: normal_z ≈ 0.707 → 含む（斜面に乗ったブロックは縦連結とする仕様）
# 0.5 = cos(60°) なので、水平から 60° 以上立った接触面を「縦」とみなす。
VERTICAL_NORMAL_MIN = 0.5


def _build_contact_neighbors(node_ids: list[int]) -> dict[int, set[int]]:
    """Return adjacency over the given nodes based on *vertical* PyBullet contacts.

    接触法線の Z 成分でフィルタし、上下の積み重なり（と斜面）だけをエッジにする。
    横並びの側面接触はエッジにしない → タワーは「縦に連なった構造」だけになる。
    """
    graph: dict[int, set[int]] = {n: set() for n in node_ids}
    node_set = set(node_ids)
    for n in node_ids:
        for c in p.getContactPoints(bodyA=n):
            other = c[2] if c[1] == n else c[1]
            if other not in node_set:
                continue
            # c[7] = contactNormalOnB。その Z 成分が縦向きの接触のみ連結とみなす。
            if abs(c[7][2]) >= VERTICAL_NORMAL_MIN:
                graph[n].add(other)
                graph[other].add(n)
    return graph


def _ground_contacting(block_body_ids: list[int], ground_body_id: int) -> set[int]:
    """Return blocks that have at least one contact point with the ground."""
    touching: set[int] = set()
    for b in block_body_ids:
        if p.getContactPoints(bodyA=b, bodyB=ground_body_id):
            touching.add(b)
    return touching


def find_tower_blocks(
    block_body_ids: list[int],
    ground_body_id: int,
) -> set[int]:
    """Return block IDs in the *tallest* connected component that touches
    the ground.

    Definition: build the contact graph among blocks only (ground excluded),
    enumerate connected components, keep those that have at least one block
    touching the ground, then return the component with the greatest top z.

    Multiple unrelated blocks on the ground are NOT one tower — they form
    separate single-block components, and only the tallest single block (or
    actual stack) counts as the "tower".
    """
    if not block_body_ids:
        return set()

    graph = _build_contact_neighbors(block_body_ids)

    components: list[set[int]] = []
    visited: set[int] = set()
    for start in block_body_ids:
        if start in visited:
            continue
        comp: set[int] = set()
        stack: list[int] = [start]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.add(node)
            for neighbor in graph[node]:
                if neighbor not in visited:
                    stack.append(neighbor)
        components.append(comp)

    ground_set = _ground_contacting(block_body_ids, ground_body_id)
    ground_components = [c for c in components if c & ground_set]
    if not ground_components:
        return set()

    def component_top(comp: set[int]) -> float:
        top = 0.0
        for b in comp:
            _, aabb_max = p.getAABB(b)
            if aabb_max[2] > top:
                top = aabb_max[2]
        return top

    return max(ground_components, key=component_top)


def tower_height(tower_body_ids: set[int]) -> float:
    """Return max-z of any AABB top among tower blocks, 0.0 if empty."""
    if not tower_body_ids:
        return 0.0
    max_z = 0.0
    for b in tower_body_ids:
        _, aabb_max = p.getAABB(b)
        if aabb_max[2] > max_z:
            max_z = aabb_max[2]
    return float(max_z)


def tower_base_xy(tower_body_ids: set[int]) -> tuple[float, float]:
    """Return XY centroid of the lowest tower blocks (those near the ground)."""
    if not tower_body_ids:
        return (0.0, 0.0)
    low_blocks: list[tuple[float, float]] = []
    min_z = float("inf")
    block_z: dict[int, float] = {}
    block_xy: dict[int, tuple[float, float]] = {}
    for b in tower_body_ids:
        pos, _ = p.getBasePositionAndOrientation(b)
        block_z[b] = pos[2]
        block_xy[b] = (pos[0], pos[1])
        min_z = min(min_z, pos[2])
    # Include blocks whose center z is within 1cm of the minimum.
    for b, z in block_z.items():
        if z <= min_z + 0.01:
            low_blocks.append(block_xy[b])
    sx = sum(p[0] for p in low_blocks) / len(low_blocks)
    sy = sum(p[1] for p in low_blocks) / len(low_blocks)
    return (sx, sy)

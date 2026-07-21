"""Gymnasium environment wrapping the PyBullet block stacking simulation.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    SAC 学習用の Gym 互換環境。物理シム + ブロック spawn + キャリア運搬 +
    タワー検出 + 報酬計算をひとまとめにし、Gym の step/reset インタフェースを提供。

設計上のポイント:
    - 観測フォーマット: "dict" のみ対応。
        blocks + mask + heightmap + scalar の Dict 空間（Set Transformer + CNN 用）。
    - Stage 別閾値: コンストラクタ stage_h_high / stage_h_low を渡すと
      reward.yaml の collapse_height_threshold / reset_height_threshold を上書き。
      Stage 1 (2 cube) では 0.10 / 0.03 など。
    - 崩落判定 3 条件: H_high 到達フラグ立ち + H_low 以下 + タワー離散率 >= 0.5。
      離散率は prev_tower_ids - new_tower_ids の集合差で計算 → 配置中の自発的
      高さ低下を誤検知しないための重要ガード。
    - max_actions_without_progress: 進捗なし連続 N アクションで truncate。
      timeout_penalty が追加される → 「何もしない方策」防止。
    - reset で毎回 PyBullet を connect/disconnect。並列訓練 (SubprocVecEnv) なら
      OK だが、DummyVecEnv n>1 では client ID 干渉あり（要 sim 層 refactor）。

    新機能（重みつき記憶 + 短期記憶対応）:
    - step() の info dict に "event_type" を出す。崩落 / failure / 新記録 /
      success / no_progress の 5 種。WeightedReplayBuffer がこれを拾って
      初期重みを決定する。
    - observation_format="dict" の場合、Dict 観測に "recent_actions" /
      "recent_rewards" / "recent_results" の短期記憶フィールドを追加可能。
      stm_length > 0 で有効化、deque で直近 N 手を保持。

レビューで見る観点:
    - prev_tower_ids の更新タイミング: step 末尾で new_tower_ids に置き換える。
      reset 時に initial_settle 後の状態で初期化。
    - 報酬重み (reward_cfg) と Stage 設定 (training.yaml) の責務分離は意図的。
    - place_yaw は現在未使用（cube が回転対称のため）。形状が増えたら使う想定。

関連:
    - 観測パッキング: env/observation.py
    - 行動デコード: env/action.py
    - タワー検出: env/tower.py
    - 重みつきバッファ: policy/weighted_replay_buffer.py
    - 設計: 設計書 §4 (AI/RL設計)
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

from block_stacker.config import PhysicsConfig, RewardConfig, ShapeSpec, WorldConfig
from block_stacker.env.action import ACTION_DIM, decode_action
from block_stacker.env.observation import (
    pack_observation_dict,
    per_block_dims,
)
from block_stacker.env.tower import find_tower_blocks, tower_base_xy, tower_height
from block_stacker.sim.blocks import (
    Block,
    create_block,
    find_nearest_excluding,
    get_pose,
    reset_pose,
)
from block_stacker.sim.carrier import (
    grab_block,
    plan_three_phase_lift_height,
    trajectory_three_phase,
)
from block_stacker.sim.heightmap import compute_heightmap
from block_stacker.sim.world import World, setup_world


# Event type 定数（info dict に格納、WeightedReplayBuffer が初期重み決定に使う）。
EVENT_COLLAPSE = "collapse"
EVENT_FAILURE = "failure"             # 進歩なし truncate
EVENT_HEIGHT_RECORD = "height_record"
EVENT_SUCCESS = "success"             # placement 成功（新記録ではない）
EVENT_NO_PROGRESS = "no_progress"     # 何も起きなかった


# 短期記憶の 1 エントリにつき記録する「結果コード」（連続値で feature 化）。
# Event type → スカラー、AI に「直近 5 手でどれくらい強い結果が出たか」を渡す。
EVENT_TO_RESULT_SCORE: dict[str, float] = {
    EVENT_COLLAPSE: -1.0,
    EVENT_FAILURE: -0.5,
    EVENT_NO_PROGRESS: 0.0,
    EVENT_SUCCESS: 0.5,
    EVENT_HEIGHT_RECORD: 1.0,
}


def block_full_height(shape: ShapeSpec) -> float:
    """安定姿勢でのブロックの縦方向の全長（積み上げ高さの計算用）。

    box: 高さ z（dims[2]） / cylinder: 高さ（dims[1]） /
    triangular_prism: leg（安定姿勢の local z 範囲 = leg = dims[0]）。
    """
    if shape.type == "box":
        return float(shape.dims[2])
    if shape.type == "cylinder":
        return float(shape.dims[1])
    if shape.type == "triangular_prism":
        return float(shape.dims[0])
    return 0.05


def inventory_full_stack_height(
    inventory: dict[str, int], shapes: dict[str, ShapeSpec]
) -> float:
    """在庫を全部きれいに縦積みした時の理論高さ（= Σ count × block_full_height）。

    目標高さ = この値 × ratio（target_height_ratio）。在庫が増えれば目標も比例して上がる。
    """
    total = 0.0
    for name, count in inventory.items():
        if name in shapes:
            total += count * block_full_height(shapes[name])
    return float(total)


class BlockStackerEnv(gym.Env):
    metadata = {"render_modes": ["human", None]}

    def __init__(
        self,
        world_cfg: WorldConfig,
        physics_cfg: PhysicsConfig,
        reward_cfg: RewardConfig,
        max_steps: int = 30,
        max_blocks: int = 8,
        inventory_override: dict[str, int] | None = None,
        render_mode: str | None = None,
        settle_steps_per_action: int = 60,
        initial_settle_steps: int = 120,
        stage_h_high: float | None = None,
        stage_h_low: float | None = None,
        target_height_ratio: float = 0.6,
        max_actions_without_progress: int = 10,
        heightmap_resolution: int = 32,
        stm_length: int = 0,
    ) -> None:
        super().__init__()
        self.world_cfg = world_cfg
        self.physics_cfg = physics_cfg
        self.reward_cfg = reward_cfg
        self.max_steps = max_steps
        self.max_blocks = max_blocks
        self.inventory: dict[str, int] = dict(inventory_override or world_cfg.inventory)
        self.render_mode = render_mode
        self.settle_steps_per_action = settle_steps_per_action
        self.initial_settle_steps = initial_settle_steps

        # Collapse thresholds: prefer stage-level overrides, else reward defaults.
        self.h_high: float = (
            stage_h_high if stage_h_high is not None
            else reward_cfg.collapse_height_threshold
        )
        self.h_low: float = (
            stage_h_low if stage_h_low is not None
            else reward_cfg.reset_height_threshold
        )
        self.max_actions_without_progress = max_actions_without_progress
        # 目標高さ = 在庫を全部縦積みした理論高さ × ratio（在庫が増えれば比例して上がる）。
        # 卒業判定②「目標高さ到達の成功率」で使う（①散布0は即卒業で別経路）。
        self.target_height_ratio = float(target_height_ratio)
        self.full_stack_height = inventory_full_stack_height(
            self.inventory, world_cfg.shapes
        )
        self.target_height = self.full_stack_height * self.target_height_ratio

        # Shape index map for the one-hot in observation.
        self.shape_order: list[str] = list(world_cfg.shapes.keys())
        self.shape_index: dict[str, int] = {n: i for i, n in enumerate(self.shape_order)}
        self.n_shapes: int = len(self.shape_order)

        self.heightmap_resolution = heightmap_resolution
        # 短期記憶: 直近 stm_length 手の (action, reward, result_score) を保持。
        # stm_length=0 で無効化（後方互換）。dict 観測の時のみ Dict に追加される。
        self.stm_length = int(stm_length)

        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(ACTION_DIM,), dtype=np.float32
        )
        pb_dim = per_block_dims(self.n_shapes)
        space_dict: dict[str, spaces.Space] = {
            "blocks": spaces.Box(
                low=-np.inf, high=np.inf,
                shape=(max_blocks, pb_dim), dtype=np.float32,
            ),
            "blocks_mask": spaces.Box(
                low=0.0, high=1.0,
                shape=(max_blocks,), dtype=np.float32,
            ),
            "heightmap": spaces.Box(
                low=-np.inf, high=np.inf,
                shape=(4, heightmap_resolution, heightmap_resolution),
                dtype=np.float32,
            ),
            "tower_top_z": spaces.Box(
                low=-np.inf, high=np.inf,
                shape=(1,), dtype=np.float32,
            ),
        }
        if self.stm_length > 0:
            space_dict["recent_actions"] = spaces.Box(
                low=-1.0, high=1.0,
                shape=(self.stm_length, ACTION_DIM), dtype=np.float32,
            )
            space_dict["recent_rewards"] = spaces.Box(
                low=-np.inf, high=np.inf,
                shape=(self.stm_length,), dtype=np.float32,
            )
            space_dict["recent_results"] = spaces.Box(
                low=-1.0, high=1.0,
                shape=(self.stm_length,), dtype=np.float32,
            )
            space_dict["recent_mask"] = spaces.Box(
                low=0.0, high=1.0,
                shape=(self.stm_length,), dtype=np.float32,
            )
        self.observation_space = spaces.Dict(space_dict)

        # Episode state
        self.world: World | None = None
        self.blocks: list[Block] = []
        self.step_count = 0
        self.tower_best_height = 0.0
        self.steps_since_progress = 0
        self.collapse_armed = False
        self.prev_tower_ids: set[int] = set()
        # このエピソードで「散布0（全ブロックが1つの連結成分）」を達成したか（指標用）。
        # 達成すると _rescatter_blocks() でラウンドを仕切り直すので複数回起きうる。
        self._ever_all_placed = False
        self._all_placed_count = 0
        self._all_placed_height = 0.0
        self.rng = np.random.default_rng()

        # 短期記憶バッファ (deque で直近 N 手を保持)。reset で空にする。
        self._stm_actions: deque[np.ndarray] = deque(maxlen=self.stm_length or 1)
        self._stm_rewards: deque[float] = deque(maxlen=self.stm_length or 1)
        self._stm_results: deque[float] = deque(maxlen=self.stm_length or 1)

    # ------------------------------------------------------------------ Gym API

    def reset(
        self,
        *,
        seed: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        super().reset(seed=seed)
        if seed is not None:
            self.rng = np.random.default_rng(seed)

        if self.world is not None:
            self.world.disconnect()
            self.world = None

        self.world = setup_world(
            self.world_cfg, self.physics_cfg,
            gui=(self.render_mode == "human"),
        )
        self.blocks = self._scatter_blocks()
        self.step_count = 0
        self.tower_best_height = 0.0
        self.steps_since_progress = 0
        self.collapse_armed = False
        self._ever_all_placed = False
        self._all_placed_count = 0
        self._all_placed_height = 0.0
        # 短期記憶リセット（新しいエピソード = 新しい遊び）
        self._stm_actions.clear()
        self._stm_rewards.clear()
        self._stm_results.clear()

        if self.initial_settle_steps > 0:
            self.world.step(self.initial_settle_steps)

        self.prev_tower_ids = self._compute_tower_ids()
        return self._get_obs(), self._get_info()

    def step(
        self, action: np.ndarray
    ) -> tuple[dict[str, np.ndarray], float, bool, bool, dict[str, Any]]:
        assert self.world is not None

        pickup_xyz, place_xyz, _place_yaw = decode_action(
            np.asarray(action, dtype=np.float64), self.world_cfg
        )
        # place_yaw is currently unused (cube orientation is symmetric).

        prev_tower_ids = self.prev_tower_ids
        height_before = tower_height(prev_tower_ids)

        target_block = self._find_nearest_scattered(pickup_xyz, prev_tower_ids)
        reward = float(self.reward_cfg.time_penalty)
        terminated = False
        # Event type 判定用フラグ。優先度は collapse > height_record > success > no_progress、
        # 後段で truncate なら failure に上書きされる。
        event_type = EVENT_NO_PROGRESS

        if target_block is not None:
            self._execute_transport(target_block, place_xyz, current_tower_top=height_before)
            self.world.step(self.settle_steps_per_action)

            new_tower_ids = self._compute_tower_ids()
            height_after = tower_height(new_tower_ids)

            # Dispersion: fraction of OLD tower blocks no longer in the tower.
            if prev_tower_ids:
                dispersed = prev_tower_ids - new_tower_ids
                dispersion_ratio = len(dispersed) / len(prev_tower_ids)
            else:
                dispersion_ratio = 0.0

            # Place success: AI's block joined the tower。
            # 案A: place_success を「置いた高さ」で補正する。接地レベルに横付けしただけ
            # (lift≈0) はほぼ無報酬、上段に積むほど満点に近づく。これで「平たく集める／
            # 崩れた分を拾い直す」戦略の旨味を消し、上へ積む方向へ勾配を向ける。
            placed = target_block.body_id in new_tower_ids
            if placed:
                pos, _ = get_pose(target_block.body_id)
                # 接地時の重心高さ (_spawn_height) を基準に、どれだけ持ち上がって積まれたか。
                block_lift = pos[2] - self._spawn_height(target_block.shape)
                height_factor = min(max(block_lift, 0.0) / max(self.full_stack_height, 1e-6), 1.0)
                reward += float(self.reward_cfg.place_success) * height_factor
                event_type = EVENT_SUCCESS

            # Height record (proxies progress for the progress-counter).
            if height_after > self.tower_best_height + 1e-4:
                reward += float(self.reward_cfg.height_record)
                self.tower_best_height = height_after
                self.steps_since_progress = 0
                event_type = EVENT_HEIGHT_RECORD  # 新記録 > 単純 success
            else:
                self.steps_since_progress += 1

            # Collapse detection (design: height + dispersion + not-placing).
            if self._check_collapse(height_before, height_after, dispersion_ratio):
                reward += float(self.reward_cfg.collapse)
                terminated = True
                event_type = EVENT_COLLAPSE  # 崩落は最優先

            # 散布0（全ブロックが1つの連結成分に入った）を記録し、ラウンドを仕切り直す。
            if len(self.blocks) == len(new_tower_ids):
                self._note_all_placed(height_after)
                self._rescatter_blocks()
                new_tower_ids = self.prev_tower_ids

            # Refresh the tower membership snapshot for the next step.
            self.prev_tower_ids = new_tower_ids
        else:
            # 拾える散布ブロックが無く find_nearest_excluding が None を返した。ただし None には
            #   (a) 本当に全ブロックがタワー所属（散布0）
            #   (b) NaN/暴走座標で全ブロックを見落とした、または prev_tower_ids が陳腐化して
            #       いた（崩れて落ちたブロックがまだ除外集合に残る）だけ
            # の2通りがある。(b) を散布0 と誤判定すると物理破綻時に低い高さのまま
            # 「達成」扱いしてしまうため、現在のタワーを再計算して positive 確認する。
            self.world.step(30)
            self.steps_since_progress += 1
            new_tower_ids = self._compute_tower_ids()
            if len(self.blocks) == len(new_tower_ids):
                # (a) 本物の散布0。記録して再配置し、続きを遊ばせる。
                self._note_all_placed(tower_height(new_tower_ids))
                self._rescatter_blocks()
                new_tower_ids = self.prev_tower_ids
            self.prev_tower_ids = new_tower_ids

        self.step_count += 1
        truncated = False
        if not terminated:
            if self.step_count >= self.max_steps:
                truncated = True
            elif self.steps_since_progress >= self.max_actions_without_progress:
                truncated = True
                reward += float(self.reward_cfg.timeout_penalty)
                event_type = EVENT_FAILURE  # 進歩なし truncate は failure

        # 短期記憶を更新（新しい (action, reward, result_score) を追加）。
        # 古いエントリは deque の maxlen で自動的に押し出される。
        result_score = EVENT_TO_RESULT_SCORE.get(event_type, 0.0)
        if self.stm_length > 0:
            self._stm_actions.append(np.asarray(action, dtype=np.float32).copy())
            self._stm_rewards.append(float(reward))
            self._stm_results.append(float(result_score))

        info = self._get_info()
        info["event_type"] = event_type
        info["result_score"] = result_score
        # 行動前（直前）のタワー高さ。WeightedReplayBuffer の高さ補正で使う:
        # 高いタワーで起きた経験ほど「強い記憶」に底上げする。
        info["height_before"] = height_before
        # カリキュラム指標（進行には影響しない。記録専用）:
        #   is_success        = 「目標高さ到達」＝高さ指標。
        #   all_placed        = 「全ブロックが1つの連結成分」＝高さ非依存の構造指標。
        #   all_placed_count  = そのエピソード内での達成回数（達成ごとに再配置して継続する）。
        #   all_placed_height = 達成時のタワー高さの最大値。
        #     **all_placed だけでは本物の塔かレンガ積みかを判別できない**ため必ず併読する
        #     （8 個のレンガ積み・高さ 0.100m でも all_placed は成立する）。
        info["is_success"] = bool(self.tower_best_height >= self.target_height)
        info["all_placed"] = self._ever_all_placed
        info["all_placed_count"] = self._all_placed_count
        info["all_placed_height"] = self._all_placed_height
        return self._get_obs(), reward, terminated, truncated, info

    def close(self) -> None:
        if self.world is not None:
            self.world.disconnect()
            self.world = None

    # ---------------------------------------------------------------- Internals

    def _scatter_blocks(self) -> list[Block]:
        """Spawn blocks per `initial_scatter` config: rejection-sample for spacing
        and keep a central exclusion radius free."""
        blocks: list[Block] = []
        placed_xy: list[tuple[float, float]] = []

        for shape_name, count in self.inventory.items():
            if shape_name not in self.world_cfg.shapes:
                continue
            shape = self.world_cfg.shapes[shape_name]
            for _ in range(count):
                x, y = self._sample_scatter_xy(placed_xy)
                quat = self._random_yaw_quat()
                z0 = self._spawn_height(shape) + 0.02
                block = create_block(
                    shape, (x, y, z0), orientation_quat=quat, physics=self.physics_cfg,
                )
                blocks.append(block)
                placed_xy.append((x, y))
        return blocks

    def _note_all_placed(self, height_at_completion: float) -> None:
        """散布0 の達成を記録する（回数と、その時のタワー高さ）。

        高さを併記するのが重要。`find_tower_blocks` が返すのは「縦接触で連結された成分」
        であって「高い塔」ではないため、**横に広い低い構造でも散布0 は成立する**
        （8 個のレンガ積みで高さ 0.100m でも成立することを実測確認済み）。
        高さを見れば「本物の塔か、低く広がった構造か」を後から判別できる。
        """
        self._ever_all_placed = True
        self._all_placed_count += 1
        # 1 エピソードで複数回起きうるので、最も高かった時の値を残す。
        self._all_placed_height = max(self._all_placed_height, float(height_at_completion))

    def _sample_scatter_xy(
        self, placed_xy: list[tuple[float, float]]
    ) -> tuple[float, float]:
        """散布 xy を rejection sampling で 1 つ返す（中心除外＋最小間隔）。"""
        cfg = self.world_cfg.initial_scatter
        x_min, x_max = self.world_cfg.x_range
        y_min, y_max = self.world_cfg.y_range
        wall_margin = 0.05  # avoid touching walls
        exclude_r_sq = cfg.exclude_radius_from_center ** 2
        min_dist_sq = cfg.min_inter_block_distance ** 2

        x, y = 0.0, 0.0
        for _ in range(200):
            x = float(self.rng.uniform(x_min + wall_margin, x_max - wall_margin))
            y = float(self.rng.uniform(y_min + wall_margin, y_max - wall_margin))
            if (x * x + y * y) < exclude_r_sq:
                continue
            if any((x - px) ** 2 + (y - py) ** 2 < min_dist_sq for px, py in placed_xy):
                continue
            break
        return x, y

    def _random_yaw_quat(self) -> tuple[float, float, float, float]:
        if not self.world_cfg.initial_scatter.random_yaw:
            return (0.0, 0.0, 0.0, 1.0)
        yaw = float(self.rng.uniform(-math.pi, math.pi))
        return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))

    def _rescatter_blocks(self) -> None:
        """全ブロックを新しいランダム位置へ再配置してラウンドを仕切り直す。

        散布0（全部積み切った）を達成した時に呼ぶ。body_id は保持し、姿勢と速度だけ戻す。
        ai_server の rescatter_blocks（デモ側）と同じ挙動を学習側にも用意したもの。

        これが無いと、全部積み切った後は拾えるブロックが無いまま空振りが続き、
        time_penalty を払い続けた末に「進歩なし truncate」で timeout_penalty まで課され、
        **課題を完遂したエピソードが failure として記録される**（強い負の記憶になる）。
        """
        assert self.world is not None
        placed_xy: list[tuple[float, float]] = []
        for block in self.blocks:
            x, y = self._sample_scatter_xy(placed_xy)
            z0 = self._spawn_height(block.shape) + 0.02
            reset_pose(block.body_id, (x, y, z0), self._random_yaw_quat())
            placed_xy.append((x, y))
        # 落下・接触を落ち着かせてからタワー判定をやり直す。
        self.world.step(self.initial_settle_steps)
        self.prev_tower_ids = self._compute_tower_ids()
        # 新しいラウンドなので無進歩カウンタは戻す（空振り扱いさせない）。
        self.steps_since_progress = 0

    def _spawn_height(self, shape: ShapeSpec) -> float:
        """Spawn 時に block の centroid を地面からどれだけ上げるか（local 底面 → 0）。"""
        if shape.type == "box":
            return float(shape.dims[2] / 2.0)
        if shape.type == "cylinder":
            return float(shape.dims[1] / 2.0)
        if shape.type == "triangular_prism":
            # 直角二等辺三角柱は centroid が幾何中心からずれる:
            # 安定姿勢（y-leg 面が下）で local z 範囲は [-leg/3, +2 leg/3]
            # 底面までの距離 = leg/3
            return float(shape.dims[0] / 3.0)
        return 0.05

    def _compute_tower_ids(self) -> set[int]:
        assert self.world is not None
        return find_tower_blocks(
            [b.body_id for b in self.blocks], self.world.ground_id
        )

    def _tower_height(self) -> float:
        return tower_height(self._compute_tower_ids())

    def _find_nearest_scattered(
        self,
        query: tuple[float, float, float],
        excluded: set[int],
    ) -> Block | None:
        return find_nearest_excluding(self.blocks, query, excluded)

    def _execute_transport(
        self,
        block: Block,
        place_xyz: tuple[float, float, float],
        current_tower_top: float,
    ) -> None:
        """Carry `block` to `place_xyz` via a 3-phase trajectory.

        The lift height is chosen so the horizontal lane clears the current
        tower with at least `carrier_approach_offset` margin.
        """
        assert self.world is not None
        physics = self.physics_cfg
        start_pos, _ = get_pose(block.body_id)

        if block.shape.type == "box":
            half_h = block.shape.dims[2] / 2.0
        elif block.shape.type == "cylinder":
            half_h = block.shape.dims[1] / 2.0
        elif block.shape.type == "triangular_prism":
            # 三角柱は centroid 下に leg/3 ぶん地面までの余白がある
            half_h = block.shape.dims[0] / 3.0
        else:
            half_h = 0.025
        target_z = max(place_xyz[2], half_h + 0.005)
        target = (place_xyz[0], place_xyz[1], target_z)

        lift_height = plan_three_phase_lift_height(
            start_pos[2], target_z, current_tower_top, physics.carrier_approach_offset
        )

        carrier = grab_block(block.body_id, start_pos, physics)
        dt = 1.0 / physics.internal_rate_hz
        for wp in trajectory_three_phase(
            start=start_pos,
            end=target,
            lift_height=lift_height,
            speed=physics.carrier_trajectory_speed,
            dt=dt,
        ):
            carrier.update_target(wp)
            self.world.step()
        carrier.release()

    def _check_collapse(
        self,
        height_before: float,
        height_after: float,
        dispersion_ratio: float,
    ) -> bool:
        """Trigger collapse only when:
            - the tower has reached H_high at some earlier point this episode, and
            - the current top is below H_low, and
            - more than `collapse_dispersion_ratio` of the OLD tower has scattered.
        The dispersion check prevents false positives when the AI places a
        single block low (`placing` artifact) while the tower itself is intact.
        """
        if height_before >= self.h_high:
            self.collapse_armed = True
        if (
            self.collapse_armed
            and height_after <= self.h_low
            and dispersion_ratio >= self.physics_cfg.collapse_dispersion_ratio
        ):
            return True
        return False

    def _get_obs(self) -> dict[str, np.ndarray]:
        tower_ids = self.prev_tower_ids if self.prev_tower_ids else self._compute_tower_ids()
        h_top = tower_height(tower_ids)
        ref_xy = tower_base_xy(tower_ids) if tower_ids else (0.0, 0.0)
        wall_ids = self.world.wall_ids if self.world is not None else []
        heightmap = compute_heightmap(
            self.world_cfg,
            resolution=self.heightmap_resolution,
            ignore_body_ids=wall_ids,
        )
        obs = pack_observation_dict(
            self.blocks, tower_ids, self.max_blocks, h_top,
            shape_index=self.shape_index,
            n_shapes=self.n_shapes,
            heightmap=heightmap,
            reference_xy=ref_xy,
        )
        # 短期記憶を観測辞書に追加（stm_length > 0 の時のみ）。
        # 「新しい順」で詰める: index 0 が直近、index N-1 が最古。
        # 履歴が N に満たない場合、recent_mask で有効スロットを示す。
        if self.stm_length > 0:
            actions_arr = np.zeros((self.stm_length, ACTION_DIM), dtype=np.float32)
            rewards_arr = np.zeros((self.stm_length,), dtype=np.float32)
            results_arr = np.zeros((self.stm_length,), dtype=np.float32)
            mask_arr = np.zeros((self.stm_length,), dtype=np.float32)
            # deque は古い→新しい順なので reversed で新しい→古い順に詰め直す
            for i, (a, r, s) in enumerate(zip(
                reversed(self._stm_actions),
                reversed(self._stm_rewards),
                reversed(self._stm_results),
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
        return obs

    def _get_info(self) -> dict[str, Any]:
        return {
            "step_count": self.step_count,
            "tower_height": self._tower_height(),
            "tower_best_height": self.tower_best_height,
            "steps_since_progress": self.steps_since_progress,
            "n_blocks": len(self.blocks),
        }

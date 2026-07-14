"""AI-driven streaming server.

Loads a trained SAC model (from output/training/) and runs it as a slow ambient demo:

    1. observe (Dict obs identical to training)
    2. predict an action
    3. issue a transport: pickup_query → place_xyz, lift over current tower
    4. let the carrier deliver it; settle; check collapse
    5. pause; loop

The physics + broadcast loop lives in `streaming.broadcaster`. This module
contributes only:

    - the AI driver task (observation → predict → transport request)
    - a small "carrier driver" callback the broadcaster invokes per step
    - block spawning per curriculum stage（既定は最終ステージ＝全形状。--stage で変更可）

Run:
    .venv/Scripts/python.exe -m block_stacker.serving.ai_server
    .venv/Scripts/python.exe -m block_stacker.serving.ai_server --port 8765 ^
        --model output/training/fresh/sac_3990_steps.zip --thinking-pause 1.0

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    学習済み SAC モデルをロードし、AI が積み木を積む様子を
    リアルタイムで配信する。

    短期記憶対応:
    - 学習時に stm_length > 0 で訓練されたモデルは観測辞書に "recent_*" を要求する。
    - ai_driver_task が deque を保持して、毎ループで観測に詰める（env と同じロジック）。
    - エピソード境界が無い「持続ワールド」設計のため、deque は崩落時のみクリア。

設計上のポイント:
    - 2 タスク協調: physics_task（PhysicsBroadcaster）と ai_driver_task が
      asyncio.gather で並走。CarrierDriver が共有状態（carrier、waypoints_iter、
      transport_done Event）。
    - pre_step フック (driver.tick) で物理ループの各ステップに「次ウェイポイント
      消費」を注入。AI driver は transport_done Event を待つだけで運搬完了を検知。
    - 観測フォーマット: 学習時と完全に同じ Dict obs（pack_observation_dict）。
      max_blocks / heightmap_resolution / shape_index も学習時設定を再現。
      → モデル shape mismatch を避けるためのキーポイント。
    - 持続ワールド設計: 崩落時もブロック配置をリセットしない。collapse_event
      を broadcast するのみ。「N 回崩落でシャッフル」は将来 MVP で実装。

レビューで見る観点:
    - AI が選んだ place_xyz が境界外/壁外の場合、ブロックが飛んでいく可能性。
      （設計上「迷子ブロックはシャッフルまで放置」許容）
    - model.predict は同期 (PyTorch forward)。AI 駆動 cadence は秒オーダーなので
      イベントループブロックは事実上問題なし。並列訓練インスタンス想定の場合は
      asyncio.to_thread への切り出し検討。

関連:
    - 学習: src/block_stacker/training/train.py（SAC + 重みつき記憶、観測 dim と一致させる必要あり）
    - 共通: src/block_stacker/serving/runtime.py
    - 設計: 設計書 §4 (AI/RL設計) §8 (AWS構成)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from stable_baselines3 import SAC

from block_stacker.config import (
    PhysicsConfig,
    ShapeSpec,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.action import decode_action
from block_stacker.env.env import EVENT_TO_RESULT_SCORE, inventory_full_stack_height
from block_stacker.env.observation import pack_observation_dict
from block_stacker.env.tower import find_tower_blocks, tower_base_xy, tower_height
from block_stacker.serving.runtime import setup_streaming_runtime
from block_stacker.sim.blocks import (
    Block,
    create_block,
    find_nearest_excluding,
    get_pose,
    reset_pose,
)
from block_stacker.sim.carrier import (
    Carrier,
    grab_block,
    plan_three_phase_lift_height,
    trajectory_three_phase,
)
from block_stacker.sim.heightmap import compute_heightmap
from block_stacker.sim.world import World, setup_world
from block_stacker.streaming.broadcaster import PhysicsBroadcaster
from block_stacker.streaming.protocol import pack_collapse_event
from block_stacker.streaming.server import StreamingServer
from block_stacker.training.checkpoint import find_latest_checkpoint
from block_stacker.training.curriculum import resolve_graduation, stage_inventory

LOG = logging.getLogger("serving.ai")


# --------------------------------------------------------------- shared state


@dataclass
class CarrierDriver:
    """Per-step state for advancing the carrier inside the physics loop.

    レビューノート: 2 タスク間の唯一の共有可変状態。AI driver が carrier と
    waypoints_iter を set、physics_task の pre_step で 1 ステップずつ tick され、
    iter が尽きると release + transport_done.set() で完了通知。
    """

    carrier: Carrier | None = None
    waypoints_iter: Iterator[tuple[float, float, float]] | None = None
    transport_done: asyncio.Event = field(default_factory=asyncio.Event)

    def tick(self, step: int, ts: float) -> None:
        # 物理ループ 1 ステップ = ウェイポイント 1 個消費。
        # 240Hz × 0.3m/s × 3 phase trajectory → 数百〜千ステップで完了。
        if self.carrier is None or self.waypoints_iter is None:
            return
        try:
            wp = next(self.waypoints_iter)
            self.carrier.update_target(wp)
        except StopIteration:
            # 全ウェイポイント消化 → 拘束解除 + 完了通知
            self.carrier.release()
            self.carrier = None
            self.waypoints_iter = None
            self.transport_done.set()


# ---------------------------------------------------------------- spawn


def _spawn_z0(shape: ShapeSpec) -> float:
    """Spawn 時の centroid 高さ（local 底面 → 地面）。env._spawn_height と一致させる。"""
    if shape.type == "box":
        return float(shape.dims[2] / 2.0)
    if shape.type == "cylinder":
        return float(shape.dims[1] / 2.0)
    if shape.type == "triangular_prism":
        return float(shape.dims[0] / 3.0)
    return 0.05


def _sample_scatter_xy(
    world_cfg: WorldConfig,
    placed_xy: list[tuple[float, float]],
    rng: random.Random,
    margin: float = 0.1,
) -> tuple[float, float]:
    """散布位置を 1 つ rejection sampling で返す（中心除外＋最小間隔、env._scatter と同条件）。"""
    x_min, x_max = world_cfg.x_range
    y_min, y_max = world_cfg.y_range
    scatter = world_cfg.initial_scatter
    exclude_r_sq = scatter.exclude_radius_from_center ** 2
    min_dist_sq = scatter.min_inter_block_distance ** 2
    x, y = 0.0, 0.0
    for _attempt in range(200):
        x = rng.uniform(x_min + margin, x_max - margin)
        y = rng.uniform(y_min + margin, y_max - margin)
        if (x * x + y * y) < exclude_r_sq:
            continue
        if any((x - px) ** 2 + (y - py) ** 2 < min_dist_sq for px, py in placed_xy):
            continue
        break
    return x, y


def _random_yaw_quat(rng: random.Random) -> tuple[float, float, float, float]:
    yaw = rng.uniform(-math.pi, math.pi)
    return (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))


def spawn_stage_blocks(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, inventory: dict[str, int]
) -> list[Block]:
    """ステージの在庫どおりに全形状を散布する（env._scatter_blocks の配信用ミラー）。

    cube だけでなく cuboid / triangular_prism / cylinder も in inventory なら撒く。
    enable_sleeping=False: 配信デモは外部から wake させる必要があるため。
    """
    blocks: list[Block] = []
    rng = random.Random(0)
    placed_xy: list[tuple[float, float]] = []
    for shape_name, count in inventory.items():
        if shape_name not in world_cfg.shapes:
            continue
        shape = world_cfg.shapes[shape_name]
        for _ in range(count):
            x, y = _sample_scatter_xy(world_cfg, placed_xy, rng)
            z0 = _spawn_z0(shape) + 0.02
            b = create_block(
                shape, (x, y, z0),
                orientation_quat=_random_yaw_quat(rng),
                physics=physics_cfg,
                enable_sleeping=False,  # streaming demos need external wake support
            )
            blocks.append(b)
            placed_xy.append((x, y))
    return blocks


def rescatter_blocks(
    blocks: list[Block], world_cfg: WorldConfig, rng: random.Random
) -> None:
    """全ブロックを新しいランダム位置へ再配置する（body_id は保持、速度0リセット）。

    デモで散布ブロックゼロ（全部積み切った／物理破綻でタワーしか残らない）になったとき、
    ラウンドを仕切り直すために使う。body_id を保つので配信中のクライアント追跡が途切れない。
    MVP では演出なし（将来リセット演出を入れる余地あり）。
    """
    placed_xy: list[tuple[float, float]] = []
    for b in blocks:
        x, y = _sample_scatter_xy(world_cfg, placed_xy, rng)
        z0 = _spawn_z0(b.shape) + 0.02
        reset_pose(b.body_id, (x, y, z0), _random_yaw_quat(rng))
        placed_xy.append((x, y))


def _resolve_model_path(explicit: Path | None) -> Path:
    """既定の推論モデルを解決する。

    --model 明示時はそれを使う。無指定なら fresh/ / played/ の最大ステップ checkpoint を返す。
    どちらも空の場合は明示的にエラー終了する（sac_latest.zip は現命名規則では存在しない）。
    """
    if explicit is not None:
        return explicit
    found = find_latest_checkpoint(Path("output/training"))
    if found is not None:
        return found
    LOG.error(
        "checkpoint not found in output/training/fresh/ or played/. "
        "Run training first: .venv\\Scripts\\python.exe -m block_stacker.training.train "
        "--n-envs 4 --total-timesteps 2000000 --target-stage 4"
    )
    raise SystemExit(1)


# ----------------------------------------------------------- observation/util


def _build_observation(
    blocks: list[Block],
    world: World,
    world_cfg: WorldConfig,
    shape_index: dict[str, int],
    n_shapes: int,
    max_blocks: int,
    heightmap_resolution: int,
    stm: ShortTermMemory | None = None,
) -> tuple[dict, set[int], float]:
    body_ids = [b.body_id for b in blocks]
    tower_ids = find_tower_blocks(body_ids, world.ground_id)
    h_top = tower_height(tower_ids)
    ref_xy = tower_base_xy(tower_ids) if tower_ids else (0.0, 0.0)
    heightmap = compute_heightmap(
        world_cfg, resolution=heightmap_resolution, ignore_body_ids=world.wall_ids,
    )
    obs = pack_observation_dict(
        blocks, tower_ids, max_blocks, h_top,
        shape_index=shape_index, n_shapes=n_shapes,
        heightmap=heightmap, reference_xy=ref_xy,
    )
    if stm is not None and stm.length > 0:
        stm.pack_into(obs)
    return obs, tower_ids, h_top


@dataclass
class ShortTermMemory:
    """env.py の短期記憶ロジックを推論側で再現するためのヘルパー。

    学習時と完全に同じ観測形状を ai_server で組み立てる必要があるので、
    env.py の _stm_* deque + _get_obs の pack 処理をミラーリングしている。
    """

    length: int
    action_dim: int = 7
    actions: deque = field(default_factory=lambda: deque())
    rewards: deque = field(default_factory=lambda: deque())
    results: deque = field(default_factory=lambda: deque())

    def __post_init__(self) -> None:
        if self.length > 0:
            self.actions = deque(maxlen=self.length)
            self.rewards = deque(maxlen=self.length)
            self.results = deque(maxlen=self.length)

    def record(self, action: np.ndarray, reward: float, event_type: str) -> None:
        if self.length <= 0:
            return
        self.actions.append(np.asarray(action, dtype=np.float32).copy())
        self.rewards.append(float(reward))
        self.results.append(float(EVENT_TO_RESULT_SCORE.get(event_type, 0.0)))

    def clear(self) -> None:
        self.actions.clear()
        self.rewards.clear()
        self.results.clear()

    def pack_into(self, obs: dict[str, np.ndarray]) -> None:
        L = self.length
        actions_arr = np.zeros((L, self.action_dim), dtype=np.float32)
        rewards_arr = np.zeros((L,), dtype=np.float32)
        results_arr = np.zeros((L,), dtype=np.float32)
        mask_arr = np.zeros((L,), dtype=np.float32)
        for i, (a, r, s) in enumerate(zip(
            reversed(self.actions), reversed(self.rewards), reversed(self.results),
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


def _half_height(block: Block) -> float:
    """centroid から底面までの距離。env.py の _spawn_height と一致させる。"""
    if block.shape.type == "box":
        return float(block.shape.dims[2] / 2.0)
    if block.shape.type == "cylinder":
        return float(block.shape.dims[1] / 2.0)
    if block.shape.type == "triangular_prism":
        return float(block.shape.dims[0] / 3.0)
    return 0.025


# ---------------------------------------------------------------- AI driver


async def ai_driver_task(
    *,
    blocks: list[Block],
    world: World,
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    shape_index: dict[str, int],
    n_shapes: int,
    max_blocks: int,
    heightmap_resolution: int,
    server: StreamingServer,
    broadcaster: PhysicsBroadcaster,
    driver: CarrierDriver,
    model: SAC,
    h_high: float,
    h_low: float,
    thinking_pause: float,
    settle_seconds: float,
    stm: ShortTermMemory,
) -> None:
    dt = 1.0 / physics_cfg.internal_rate_hz
    collapse_armed = False
    tower_best_height = 0.0
    prev_tower_ids: set[int] = set()
    rescatter_rng = random.Random()  # 毎ラウンド違う配置にする（固定 seed にしない）

    while True:
        # If a prior transport is still running, wait for it.
        if driver.carrier is not None:
            await driver.transport_done.wait()
            driver.transport_done.clear()

        obs, tower_ids, height_before = _build_observation(
            blocks, world, world_cfg, shape_index, n_shapes, max_blocks,
            heightmap_resolution, stm=stm,
        )
        action, _ = model.predict(obs, deterministic=False)
        pickup_xyz, place_xyz, _yaw = decode_action(
            np.asarray(action, dtype=np.float64), world_cfg
        )

        target = find_nearest_excluding(blocks, pickup_xyz, tower_ids)
        if target is None:
            # 散布ブロックゼロ（全部積み切った／物理破綻でタワーしか拾えない）→
            # 全ブロックを再ランダム配置してラウンドを仕切り直す（MVP: 演出なし）。
            LOG.info("散布ブロックゼロ → 全ブロックを再配置してリスタート")
            rescatter_blocks(blocks, world_cfg, rescatter_rng)
            tower_best_height = 0.0
            collapse_armed = False
            prev_tower_ids = set()
            stm.record(action, reward=0.0, event_type="no_progress")
            await asyncio.sleep(settle_seconds)  # broadcaster が再配置後を settle
            continue

        start_pos, _ = get_pose(target.body_id)
        half_h = _half_height(target)
        target_z = max(place_xyz[2], half_h + 0.005)
        end = (place_xyz[0], place_xyz[1], target_z)
        lift = plan_three_phase_lift_height(
            start_pos[2], end[2], height_before, physics_cfg.carrier_approach_offset,
        )

        LOG.info(
            "AI: pickup body=%d (%.2f,%.2f,%.2f) → place (%.2f,%.2f,%.2f), tower=%.3f, lift=%.3f",
            target.body_id, *start_pos, *end, height_before, lift,
        )

        driver.transport_done.clear()
        driver.carrier = grab_block(target.body_id, start_pos, physics_cfg)
        driver.waypoints_iter = trajectory_three_phase(
            start=start_pos, end=end, lift_height=lift,
            speed=physics_cfg.carrier_trajectory_speed, dt=dt,
        )

        await driver.transport_done.wait()
        driver.transport_done.clear()

        await asyncio.sleep(settle_seconds)

        body_ids = [b.body_id for b in blocks]
        new_tower_ids = find_tower_blocks(body_ids, world.ground_id)
        height_after = tower_height(new_tower_ids)
        new_record = height_after > tower_best_height + 1e-4
        if new_record:
            tower_best_height = height_after
        if prev_tower_ids:
            dispersion = len(prev_tower_ids - new_tower_ids) / len(prev_tower_ids)
        else:
            dispersion = 0.0

        # 結果を分類して短期記憶に積む（env.py の event_type ロジックを推論側でも再現）
        placed = target.body_id in new_tower_ids
        if height_before >= h_high:
            collapse_armed = True
        collapsed = (
            collapse_armed
            and height_after <= h_low
            and dispersion >= physics_cfg.collapse_dispersion_ratio
        )
        if collapsed:
            LOG.info(
                "COLLAPSE (h_before=%.3f, h_after=%.3f, dispersion=%.2f)",
                height_before, height_after, dispersion,
            )
            await server.broadcast(pack_collapse_event(broadcaster.now_ts()))
            collapse_armed = False
            tower_best_height = 0.0
            event_type = "collapse"
            stm.clear()  # 崩落 = エピソード相当の境界、短期記憶をリセット
            # Persistent world: blocks stay where they fell.
        elif new_record:
            event_type = "height_record"
        elif placed:
            event_type = "success"
        else:
            event_type = "no_progress"

        # 推論側では報酬は計算しないが、event_type による result_score だけ短期記憶に乗せる
        if not collapsed:  # collapse の時は既に clear 済み
            stm.record(action, reward=0.0, event_type=event_type)

        prev_tower_ids = new_tower_ids
        LOG.info("post-action: tower_h=%.3f, best=%.3f, event=%s",
                 height_after, tower_best_height, event_type)

        await asyncio.sleep(thinking_pause)


# ---------------------------------------------------------------- entry point


async def main_async(args: argparse.Namespace) -> None:
    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    # デモは「常に最終ステージ」で動かす（--stage で上書き可）。
    stages = training_cfg["curriculum"]["stages"]
    if args.stage is not None:
        stage = next((s for s in stages if s.get("id") == args.stage), stages[-1])
    else:
        stage = stages[-1]
    inventory = stage_inventory(stage, world_cfg)
    h_high = args.h_high if args.h_high is not None else float(stage["h_high"])
    h_low = args.h_low if args.h_low is not None else float(stage["h_low"])
    _, _, grad_ratio = resolve_graduation(training_cfg.get("curriculum", {}).get("graduation", {}))
    target_h = inventory_full_stack_height(inventory, world_cfg.shapes) * grad_ratio
    LOG.info("demo stage: id=%s '%s' inventory=%s target=%.3f h_high=%.3f h_low=%.3f",
             stage.get("id"), stage.get("name", ""), inventory,
             target_h, h_high, h_low)

    model_path = _resolve_model_path(args.model)
    LOG.info("loading model: %s", model_path)
    model = SAC.load(str(model_path))
    LOG.info("model loaded: n_params=%d", sum(p.numel() for p in model.policy.parameters()))

    # 学習時と同じ stm_length を推論でも再現する必要あり
    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0
    LOG.info("short-term memory length: %d", stm_length)
    stm = ShortTermMemory(length=stm_length)

    LOG.info("setting up world")
    world = setup_world(world_cfg, physics_cfg, gui=False)
    blocks = spawn_stage_blocks(world_cfg, physics_cfg, inventory)
    LOG.info("spawned %d blocks for stage %s: %s", len(blocks), stage.get("id"), inventory)

    world.step(60)  # initial settle

    server, tracker, t_start = setup_streaming_runtime(
        world_cfg=world_cfg, physics_cfg=physics_cfg, blocks=blocks,
        host=args.host, port=args.port,
    )

    shape_order = list(world_cfg.shapes.keys())
    shape_index = {n: i for i, n in enumerate(shape_order)}

    driver = CarrierDriver()
    broadcaster = PhysicsBroadcaster(
        world=world, tracker=tracker, server=server,
        body_ids=[b.body_id for b in blocks], t_start=t_start,
    )

    physics_task = asyncio.create_task(broadcaster.run(pre_step=driver.tick))
    ai_task = asyncio.create_task(ai_driver_task(
        blocks=blocks, world=world,
        world_cfg=world_cfg, physics_cfg=physics_cfg,
        shape_index=shape_index, n_shapes=len(shape_order),
        max_blocks=args.max_blocks,
        heightmap_resolution=args.heightmap_resolution,
        server=server, broadcaster=broadcaster, driver=driver,
        model=model,
        h_high=h_high, h_low=h_low,
        thinking_pause=args.thinking_pause,
        settle_seconds=args.settle_seconds,
        stm=stm,
    ))
    serve_task = asyncio.create_task(server.serve_forever())

    try:
        if args.duration > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(physics_task, ai_task, serve_task),
                    timeout=args.duration,
                )
            except TimeoutError:
                LOG.info("--duration %.1fs elapsed, shutting down", args.duration)
        else:
            await asyncio.gather(physics_task, ai_task, serve_task)
    finally:
        world.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.serving.ai_server")
    parser.add_argument("--model", type=Path, default=None,
                        help="推論モデル。無指定なら fresh/ / played/ の最大ステップを自動選択")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--stage", type=int, default=None,
                        help="デモするステージ番号。無指定なら最終ステージ")
    parser.add_argument("--max-blocks", type=int, default=8)
    parser.add_argument("--heightmap-resolution", type=int, default=32)
    parser.add_argument("--h-high", type=float, default=None,
                        help="崩落アーム閾値。無指定ならステージ値")
    parser.add_argument("--h-low", type=float, default=None,
                        help="崩落リセット閾値。無指定ならステージ値")
    parser.add_argument("--thinking-pause", type=float, default=2.0)
    parser.add_argument("--settle-seconds", type=float, default=1.5)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="再生秒数。0 または未指定なら無制限（常駐）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()

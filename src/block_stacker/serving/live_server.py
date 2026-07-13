"""Live streaming server -- training and serving fused.

1x real-time WebSocket demo (PhysicsBroadcaster) combined with background SAC
training (Step B+) so the AI visibly grows while the audience watches.

Architecture (full -- Steps A through D):
    Main thread (asyncio)
        PhysicsBroadcaster  240 Hz PyBullet + WebSocket broadcast
        ai_driver_task      serve_model.predict() -> block transport
        StreamingServer     WebSocket handshake + broadcast
        _shutdown_monitor   watches elapsed time -> sets stop_event
    Background thread (Step B+)
        SAC model.learn()   separate SubprocVecEnv (independent PyBullet instances)
        LiveCallback        syncs weights to serve_model every --sync-every steps

Session lifecycle (8 h):
    1. Load snapshot from --snapshot-dir (NN + replay_buffer + resume_state).
    2. Apply time-decay to long-term memory proportional to elapsed wall time.
    3. Stream 1x demo while training runs in background.
    4. After --duration seconds: stop training -> save snapshot -> exit.

Step A (current): serving only.  --n-envs / --sync-every / --no-resume are
                  parsed but not yet wired (annotated inline).

Run:
    .venv/Scripts/python.exe -m block_stacker.serving.live_server ^
        --snapshot-dir output/training --duration 60
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from stable_baselines3 import SAC

from block_stacker.config import WorldConfig, PhysicsConfig, default_configs_dir
from block_stacker.env.env import inventory_full_stack_height
from block_stacker.serving.ai_server import (
    CarrierDriver,
    ShortTermMemory,
    ai_driver_task,
    spawn_stage_blocks,
)
from block_stacker.serving.runtime import setup_streaming_runtime
from block_stacker.sim.world import setup_world
from block_stacker.streaming.broadcaster import PhysicsBroadcaster
from block_stacker.training.checkpoint import find_latest_checkpoint
from block_stacker.training.curriculum import resolve_graduation, stage_inventory

LOG = logging.getLogger("serving.live")


# ----------------------------------------------------------------- model load


def _resolve_model(snapshot_dir: Path, explicit: Path | None) -> Path:
    """Return model path: explicit arg > latest checkpoint in snapshot_dir."""
    if explicit is not None:
        return explicit
    found = find_latest_checkpoint(snapshot_dir)
    if found is not None:
        return found
    raise SystemExit(
        f"スナップショットが見つかりません: {snapshot_dir}\n"
        "初回は --model で明示するか、プリセット生成手順(docs/live_mode.md)を参照してください。"
    )


# ----------------------------------------------------------------- async main


async def main_async(args: argparse.Namespace) -> None:
    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    # Live mode always runs the final curriculum stage (全形状・最難).
    stages = training_cfg["curriculum"]["stages"]
    stage = stages[-1]
    inventory = stage_inventory(stage, world_cfg)
    h_high = float(stage["h_high"])
    h_low = float(stage["h_low"])
    _, _, grad_ratio = resolve_graduation(
        training_cfg.get("curriculum", {}).get("graduation", {})
    )
    target_h = inventory_full_stack_height(inventory, world_cfg.shapes) * grad_ratio
    LOG.info(
        "live stage: id=%s '%s' inventory=%s target_h=%.3f h_high=%.3f h_low=%.3f",
        stage.get("id"), stage.get("name", ""), inventory, target_h, h_high, h_low,
    )

    model_path = _resolve_model(args.snapshot_dir, args.model)
    LOG.info("loading model: %s", model_path)
    serve_model = SAC.load(str(model_path))
    LOG.info(
        "model loaded (serve): n_params=%d",
        sum(p.numel() for p in serve_model.policy.parameters()),
    )

    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0
    stm = ShortTermMemory(length=stm_length)

    LOG.info("setting up world")
    world = setup_world(world_cfg, physics_cfg, gui=False)
    blocks = spawn_stage_blocks(world_cfg, physics_cfg, inventory)
    LOG.info("spawned %d blocks for final stage", len(blocks))
    world.step(60)  # initial settle

    server, tracker, t_start = setup_streaming_runtime(
        world_cfg=world_cfg, physics_cfg=physics_cfg,
        blocks=blocks, host=args.host, port=args.port,
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
        model=serve_model,
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
                LOG.info("--duration %.0fs elapsed, shutting down", args.duration)
        else:
            await asyncio.gather(physics_task, ai_task, serve_task)
    finally:
        world.disconnect()
        LOG.info("world disconnected")


# ----------------------------------------------------------------- entry point


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="block_stacker.serving.live_server",
        description="ライブ配信モード: 1x配信 + バックグラウンド学習の融合サーバ",
    )
    # --- snapshot / model ---
    parser.add_argument(
        "--snapshot-dir", type=Path, default=Path("output/training"),
        help="スナップショット（NN + replay_buffer + resume_state）の読み書き先 (default: output/training)",
    )
    parser.add_argument(
        "--model", type=Path, default=None,
        help="推論モデルを明示（無指定なら --snapshot-dir から自動選択）",
    )
    # --- network ---
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    # --- configs ---
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--max-blocks", type=int, default=8)
    parser.add_argument("--heightmap-resolution", type=int, default=32)
    # --- demo pacing ---
    parser.add_argument("--thinking-pause", type=float, default=2.0,
                        help="行動選択後の演出待機秒数")
    parser.add_argument("--settle-seconds", type=float, default=1.5,
                        help="ブロック落下後の静定待機秒数")
    parser.add_argument("--duration", type=float, default=28800.0,
                        help="配信秒数 (default: 28800 = 8h)。0 = 無制限")
    # --- training (Step B+ で有効) ---
    parser.add_argument("--n-envs", type=int, default=4,
                        help="[Step B+] バックグラウンド学習の並列環境数")
    parser.add_argument("--sync-every", type=int, default=500,
                        help="[Step B+] 学習→配信モデルへの重み同期間隔（学習ステップ単位）")
    # --- resume (Step C+ で有効) ---
    parser.add_argument("--no-resume", action="store_true", default=False,
                        help="[Step C+] スナップショットを無視して新規学習（初回起動専用）")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()

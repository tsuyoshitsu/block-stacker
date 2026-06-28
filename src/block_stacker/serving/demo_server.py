"""MVP 3 demo server: physics + sleep/wake tracking + WebSocket streaming.

No AI. Spawns blocks per `world.yaml`, runs PyBullet in real time,
periodically perturbs random blocks (so awake/sleep transitions actually
happen), and broadcasts to any connected WebSocket clients.

Run:
    uv run python -m block_stacker.serving.demo_server
    uv run python -m block_stacker.serving.demo_server --port 9000 --no-perturb

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    AI を統合する前のプロトコル動作確認用デモサーバ。物理 + 配信パイプラインが
    エンドツーエンドで動くことを検証する役割。

設計上のポイント:
    - enable_sleeping=False で create_block を呼ぶ理由: PyBullet 内部の sleep が
      resetBaseVelocity を打ち消してしまい、perturbation が効かないため。
      SleepTracker が独立してプロトコル sleep を判定する設計と整合。
    - perturb hook は定期的にランダムなブロックを上向き push → wake/sleep の
      transition を観察できる。--no-perturb で停止可能。

レビューで見る観点:
    - シードを固定（rng = Random(0)）しているのは再現性のため。
      初期スポーン位置 / perturb 対象がデモのたびに同じ。
    - PhysicsBroadcaster が物理ループ + 配信を持つので、demo_server は
      spawn + perturb のみに集中。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
from pathlib import Path

import pybullet as p

from block_stacker.config import (
    PhysicsConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.serving.runtime import setup_streaming_runtime
from block_stacker.sim.blocks import Block, create_block
from block_stacker.sim.world import setup_world
from block_stacker.streaming.broadcaster import PhysicsBroadcaster

LOG = logging.getLogger("serving.demo")


def spawn_blocks(world_cfg: WorldConfig, physics_cfg: PhysicsConfig) -> list[Block]:
    """Scatter blocks at varied heights so we get a settle phase + collisions."""
    blocks: list[Block] = []
    rng = random.Random(0)
    x_min, x_max = world_cfg.x_range
    y_min, y_max = world_cfg.y_range
    margin = 0.1
    for shape_name, count in world_cfg.inventory.items():
        if shape_name not in world_cfg.shapes:
            continue
        shape = world_cfg.shapes[shape_name]
        for _ in range(count):
            x = rng.uniform(x_min + margin, x_max - margin)
            y = rng.uniform(y_min + margin, y_max - margin)
            z = rng.uniform(0.15, 0.4)
            yaw = rng.uniform(-math.pi, math.pi)
            quat = (0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2))
            block = create_block(
                shape, (x, y, z),
                orientation_quat=quat,
                physics=physics_cfg,
                enable_sleeping=False,  # streaming demos need external wake support
            )
            blocks.append(block)
    return blocks


def make_perturb_hook(blocks: list[Block], rate: int, every_seconds: float):
    """Build a per-step hook that pushes a random block every `every_seconds`."""
    interval = max(1, int(rate * every_seconds))
    rng = random.Random(1)

    def hook(step: int, ts: float) -> None:
        if step == 0 or step % interval != 0:
            return
        target = rng.choice(blocks)
        # grab_block wakes for us; resetBaseVelocity needs an explicit wake.
        p.changeDynamics(target.body_id, -1, activationState=p.ACTIVATION_STATE_WAKE_UP)
        vx = rng.uniform(-0.5, 0.5)
        vy = rng.uniform(-0.5, 0.5)
        vz = rng.uniform(0.5, 1.5)
        p.resetBaseVelocity(target.body_id, linearVelocity=[vx, vy, vz])
        LOG.info("perturb body=%d v=(%.2f, %.2f, %.2f)", target.body_id, vx, vy, vz)

    return hook


async def main_async(args: argparse.Namespace) -> None:
    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")

    LOG.info("Spawning world...")
    world = setup_world(world_cfg, physics_cfg, gui=False)
    blocks = spawn_blocks(world_cfg, physics_cfg)
    LOG.info("Spawned %d blocks", len(blocks))

    # Initial settle so clients don't see the in-flight spawn poses.
    world.step(60)

    server, tracker, t_start = setup_streaming_runtime(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        blocks=blocks,
        host=args.host,
        port=args.port,
    )

    broadcaster = PhysicsBroadcaster(
        world=world, tracker=tracker, server=server,
        body_ids=[b.body_id for b in blocks], t_start=t_start,
    )

    pre_step = (
        make_perturb_hook(blocks, physics_cfg.internal_rate_hz, every_seconds=5.0)
        if args.perturb else None
    )

    physics_task = asyncio.create_task(broadcaster.run(pre_step=pre_step))
    serve_task = asyncio.create_task(server.serve_forever())

    try:
        await asyncio.gather(physics_task, serve_task)
    finally:
        world.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.serving.demo_server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--perturb", action=argparse.BooleanOptionalAction, default=True,
                        help="periodically push random blocks (default on)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()

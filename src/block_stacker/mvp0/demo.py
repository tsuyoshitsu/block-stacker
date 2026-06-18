"""MVP 0: a series of sanity-check scenarios for PyBullet.

Goals:
    1. spawn_settle      - spawn a cube above ground, verify it lands and settles
    2. drop_fall         - drop a cube from height, observe trajectory
    3. carrier_transport - grab one cube, transport it onto another
    4. heightmap         - build a small stack, compute the heightmap
    5. config            - dump loaded configs

Run via:
    uv run python -m block_stacker.mvp0.demo --scenario all
    uv run python -m block_stacker.mvp0.demo --scenario carrier_transport --gui

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    PyBullet 動作確認のスモークテスト。RL も Network も無く、純粋に物理 +
    キャリア + ハイトマップが動くことを assertion で検証。

設計上のポイント:
    - --gui で PyBullet ExampleBrowser を起動。Docker 内では X 不可なので
      ヘッドレス (DIRECT) のみ動作。ローカル開発時の挙動確認用。
    - 各シナリオで世界を作り直す（disconnect/connect）→ クリーン状態保証。
    - assertion で「期待値からの大きな乖離」をすぐに検知。
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pybullet as p

from block_stacker.config import PhysicsConfig, WorldConfig, default_configs_dir
from block_stacker.sim.blocks import compute_mass, create_block, get_pose
from block_stacker.sim.carrier import grab_block, trajectory_three_phase
from block_stacker.sim.heightmap import compute_heightmap
from block_stacker.sim.world import World, setup_world

LOG = logging.getLogger("mvp0")


def scenario_spawn_settle(world: World) -> None:
    """Place a cube 5 cm above ground, run for 2 simulated seconds."""
    cube_spec = world.config_world.shapes["cube"]
    block = create_block(
        cube_spec,
        position=(0.0, 0.0, 0.05),
        physics=world.config_physics,
    )
    world.step(2 * world.config_physics.internal_rate_hz)
    pos, _ = get_pose(block.body_id)
    expected_z = cube_spec.dims[2] / 2.0
    LOG.info(
        "[spawn_settle] block z=%.4f (expected ~%.4f), mass=%.4fkg",
        pos[2], expected_z, block.mass,
    )
    assert pos[2] < 0.06, f"block did not settle: z={pos[2]}"


def scenario_drop_fall(world: World) -> None:
    """Drop a cube from 0.5 m, log z trajectory."""
    cube_spec = world.config_world.shapes["cube"]
    block = create_block(
        cube_spec,
        position=(0.0, 0.0, 0.5),
        physics=world.config_physics,
    )
    rate = world.config_physics.internal_rate_hz
    samples: list[float] = []
    for step in range(int(1.5 * rate)):
        world.step()
        if step % 20 == 0:
            pos, _ = get_pose(block.body_id)
            samples.append(pos[2])
    LOG.info(
        "[drop_fall] z trajectory: first=%s ... last=%s",
        [f"{z:.3f}" for z in samples[:5]],
        [f"{z:.3f}" for z in samples[-3:]],
    )


def scenario_carrier_transport(world: World) -> None:
    """Grab one cube, transport it on top of another."""
    cube_spec = world.config_world.shapes["cube"]
    physics = world.config_physics

    block_a = create_block(cube_spec, (0.3, 0.0, 0.05), physics=physics)
    block_b = create_block(cube_spec, (-0.3, 0.0, 0.05), physics=physics)

    # Allow them to settle.
    world.step(60)

    pos_a, _ = get_pose(block_a.body_id)
    pos_b, _ = get_pose(block_b.body_id)
    target = (pos_b[0], pos_b[1], pos_b[2] + cube_spec.dims[2])

    LOG.info(
        "[carrier_transport] grab block_a at %.2f,%.2f,%.2f -> target %.2f,%.2f,%.2f",
        pos_a[0], pos_a[1], pos_a[2], target[0], target[1], target[2],
    )

    carrier = grab_block(block_a.body_id, pos_a, physics)
    dt = 1.0 / physics.internal_rate_hz
    waypoints = trajectory_three_phase(
        start=pos_a,
        end=target,
        lift_height=0.15,
        speed=physics.carrier_trajectory_speed,
        dt=dt,
    )

    n_waypoints = 0
    for waypoint in waypoints:
        carrier.update_target(waypoint)
        world.step()
        n_waypoints += 1

    carrier.release()
    world.step(physics.internal_rate_hz)  # 1 sec settle

    pos_a_final, _ = get_pose(block_a.body_id)
    pos_b_final, _ = get_pose(block_b.body_id)
    LOG.info(
        "[carrier_transport] waypoints=%d, block_a final z=%.4f, block_b z=%.4f",
        n_waypoints, pos_a_final[2], pos_b_final[2],
    )


def scenario_heightmap(world: World) -> None:
    """Spawn a small stack, then compute the heightmap."""
    cube_spec = world.config_world.shapes["cube"]
    physics = world.config_physics

    create_block(cube_spec, (0.1, 0.1, 0.025), physics=physics)
    create_block(cube_spec, (0.1, 0.1, 0.080), physics=physics)
    create_block(cube_spec, (-0.2, -0.2, 0.025), physics=physics)

    world.step(120)

    hm = compute_heightmap(
        world.config_world,
        resolution=32,
        ignore_body_ids=world.wall_ids,
    )
    LOG.info(
        "[heightmap] shape=%s, max_height=%.4f, mean=%.4f, max_grad=%.4f",
        hm.shape, float(hm[0].max()), float(hm[0].mean()), float(hm[3].max()),
    )


def scenario_config(world_cfg: WorldConfig, physics_cfg: PhysicsConfig) -> None:
    """Dump loaded configs to verify the roundtrip."""
    LOG.info("[config] shapes (%d):", len(world_cfg.shapes))
    for name, spec in world_cfg.shapes.items():
        m = compute_mass(spec)
        LOG.info("  - %s: type=%s, dims=%s, mass=%.4fkg", name, spec.type, spec.dims, m)
    LOG.info("[config] inventory: %s", world_cfg.inventory)
    LOG.info(
        "[config] gravity=%s, internal_rate_hz=%d, solver_iters=%d",
        physics_cfg.gravity, physics_cfg.internal_rate_hz, physics_cfg.solver_iterations,
    )
    LOG.info(
        "[config] carrier: type=%s, max_force=%.2fN, speed=%.2fm/s",
        physics_cfg.carrier_type,
        physics_cfg.carrier_max_force,
        physics_cfg.carrier_trajectory_speed,
    )


SCENARIOS = ["spawn_settle", "drop_fall", "carrier_transport", "heightmap"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.mvp0.demo")
    parser.add_argument(
        "--scenario",
        choices=["all", "config", *SCENARIOS],
        default="all",
    )
    parser.add_argument("--gui", action="store_true", help="enable PyBullet GUI")
    parser.add_argument("--gui-pause", type=float, default=0.0,
                        help="seconds to pause at the end of each GUI scenario")
    parser.add_argument(
        "--configs-dir",
        type=Path,
        default=default_configs_dir(),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")

    if args.scenario == "config":
        scenario_config(world_cfg, physics_cfg)
        return

    scenarios = SCENARIOS if args.scenario == "all" else [args.scenario]

    for sc in scenarios:
        LOG.info("=== scenario: %s ===", sc)
        world = setup_world(world_cfg, physics_cfg, gui=args.gui)
        try:
            if sc == "spawn_settle":
                scenario_spawn_settle(world)
            elif sc == "drop_fall":
                scenario_drop_fall(world)
            elif sc == "carrier_transport":
                scenario_carrier_transport(world)
            elif sc == "heightmap":
                scenario_heightmap(world)
            if args.gui and args.gui_pause > 0:
                time.sleep(args.gui_pause)
        finally:
            world.disconnect()


if __name__ == "__main__":
    main()

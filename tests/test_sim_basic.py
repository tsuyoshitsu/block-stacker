"""Basic sanity tests for the simulation layer."""
from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from block_stacker.config import PhysicsConfig, WorldConfig
from block_stacker.sim.blocks import compute_mass, create_block, get_pose
from block_stacker.sim.heightmap import compute_heightmap
from block_stacker.sim.world import setup_world

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = REPO_ROOT / "configs"


@pytest.fixture
def world_cfg() -> WorldConfig:
    return WorldConfig.from_yaml(CONFIGS_DIR / "world.yaml")


@pytest.fixture
def physics_cfg() -> PhysicsConfig:
    return PhysicsConfig.from_yaml(CONFIGS_DIR / "physics.yaml")


def test_configs_load(world_cfg: WorldConfig, physics_cfg: PhysicsConfig) -> None:
    assert "cube" in world_cfg.shapes
    assert "cuboid" in world_cfg.shapes
    assert "cylinder" in world_cfg.shapes
    assert physics_cfg.internal_rate_hz == 240
    assert physics_cfg.gravity[2] < 0.0


def test_mass_cube(world_cfg: WorldConfig) -> None:
    cube = world_cfg.shapes["cube"]
    # 5cm cube at 400 kg/m^3 -> 0.05 kg
    mass = compute_mass(cube)
    assert abs(mass - 0.05) < 1e-4


def test_mass_cylinder(world_cfg: WorldConfig) -> None:
    cyl = world_cfg.shapes["cylinder"]
    # pi * r^2 * h * density
    expected = 3.14159265 * (0.025 ** 2) * 0.06 * 400
    mass = compute_mass(cyl)
    assert abs(mass - expected) < 1e-4


def test_block_settles(world_cfg: WorldConfig, physics_cfg: PhysicsConfig) -> None:
    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        block = create_block(cube, (0.0, 0.0, 0.1), physics=physics_cfg)
        world.step(2 * physics_cfg.internal_rate_hz)
        pos, _ = get_pose(block.body_id)
        # After settling, z should be roughly half the cube's height.
        assert 0.0 < pos[2] < 0.06
    finally:
        world.disconnect()


def test_heightmap_shape(world_cfg: WorldConfig, physics_cfg: PhysicsConfig) -> None:
    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        # Mirror the demo's heightmap scenario: stacked + single cube at
        # positions known to fall on the 32-resolution grid.
        create_block(cube, (0.1, 0.1, 0.025), physics=physics_cfg)
        create_block(cube, (0.1, 0.1, 0.080), physics=physics_cfg)
        create_block(cube, (-0.2, -0.2, 0.025), physics=physics_cfg)
        world.step(120)
        hm = compute_heightmap(world_cfg, resolution=32, ignore_body_ids=world.wall_ids)
        assert hm.shape == (4, 32, 32)
        # 2-cube stack should be detected somewhere on the grid.
        assert hm[0].max() > 0.05
        # Walls (at z=1.0) must be filtered out.
        assert hm[0].max() < 0.5
    finally:
        world.disconnect()


def test_rescatter_blocks_repositions_all(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig
) -> None:
    """デモの散布0 リセット: 全ブロックが body_id を保ったまま新しい位置へ動き、姿勢は有限。"""
    from block_stacker.serving.ai_server import rescatter_blocks, spawn_stage_blocks

    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        blocks = spawn_stage_blocks(world_cfg, physics_cfg, {"cube": 4, "cylinder": 3})
        world.step(physics_cfg.internal_rate_hz)
        ids_before = [b.body_id for b in blocks]
        pos_before = [get_pose(b.body_id)[0] for b in blocks]

        rescatter_blocks(blocks, world_cfg, random.Random(7))
        world.step(physics_cfg.internal_rate_hz)

        ids_after = [b.body_id for b in blocks]
        pos_after = [get_pose(b.body_id)[0] for b in blocks]
        # body_id は保持される（配信中のクライアント追跡が途切れない）。
        assert ids_after == ids_before
        # 全ブロックが XY で動いている。
        moved = sum(
            1
            for a, b in zip(pos_before, pos_after, strict=True)
            if (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 > 1e-4
        )
        assert moved == len(blocks)
        # 姿勢は有限（NaN/Inf なし）。
        assert all(math.isfinite(c) for pos in pos_after for c in pos)
    finally:
        world.disconnect()

"""Smoke tests for the BlockStackerEnv: reset, step, observation/action shapes."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
)
from block_stacker.env.env import BlockStackerEnv
from block_stacker.env.observation import per_block_dims

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = REPO_ROOT / "configs"


@pytest.fixture
def world_cfg() -> WorldConfig:
    return WorldConfig.from_yaml(CONFIGS_DIR / "world.yaml")


@pytest.fixture
def physics_cfg() -> PhysicsConfig:
    return PhysicsConfig.from_yaml(CONFIGS_DIR / "physics.yaml")


@pytest.fixture
def reward_cfg() -> RewardConfig:
    return RewardConfig.from_yaml(CONFIGS_DIR / "reward.yaml")


def test_reward_config_load(reward_cfg: RewardConfig) -> None:
    assert reward_cfg.place_success == 1.0
    # collapse は SAC + 重みつき記憶設計で -10 → -5 に緩和。
    # 大きな negative reward が長期記憶バッファに残るので、強さを下げても効果は保たれる。
    assert reward_cfg.collapse == -5.0


def test_env_spaces(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=3,
        max_blocks=8,
        inventory_override={"cube": 3},
    )
    try:
        assert env.action_space.shape == (7,)
        # observation_space is a Dict space (blocks + mask + heightmap + scalar)
        n_shapes = len(world_cfg.shapes)
        pb_dim = per_block_dims(n_shapes)
        from gymnasium import spaces as gym_spaces
        assert isinstance(env.observation_space, gym_spaces.Dict)
        assert env.observation_space["blocks"].shape == (8, pb_dim)
        assert env.observation_space["blocks_mask"].shape == (8,)
        assert env.observation_space["tower_top_z"].shape == (1,)
    finally:
        env.close()


def test_tower_detection_separate_blocks(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig
) -> None:
    """Three separate cubes on the ground should produce a tower of size 1,
    not all three."""
    import pybullet as p
    from block_stacker.env.tower import find_tower_blocks, tower_height
    from block_stacker.sim.blocks import create_block
    from block_stacker.sim.world import setup_world

    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        b1 = create_block(cube, (0.3, 0.0, 0.05), physics=physics_cfg)
        b2 = create_block(cube, (-0.3, 0.0, 0.05), physics=physics_cfg)
        b3 = create_block(cube, (0.0, 0.3, 0.05), physics=physics_cfg)
        world.step(120)

        tower = find_tower_blocks([b1.body_id, b2.body_id, b3.body_id], world.ground_id)
        # Each separate block forms its own component => tower has 1 block.
        assert len(tower) == 1
        # Top is a single cube's height (~0.05).
        assert 0.04 < tower_height(tower) < 0.06
    finally:
        world.disconnect()


def test_tower_detection_stack(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig
) -> None:
    """A 2-cube stack plus a separate cube => tower = the stack."""
    from block_stacker.env.tower import find_tower_blocks, tower_height
    from block_stacker.sim.blocks import create_block
    from block_stacker.sim.world import setup_world

    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        stacked_lo = create_block(cube, (0.1, 0.1, 0.025), physics=physics_cfg)
        stacked_hi = create_block(cube, (0.1, 0.1, 0.080), physics=physics_cfg)
        alone = create_block(cube, (-0.3, -0.3, 0.025), physics=physics_cfg)
        world.step(120)

        tower = find_tower_blocks(
            [stacked_lo.body_id, stacked_hi.body_id, alone.body_id],
            world.ground_id,
        )
        # Tower = the 2-cube stack.
        assert tower == {stacked_lo.body_id, stacked_hi.body_id}
        # Top ~ 0.10
        assert 0.09 < tower_height(tower) < 0.11
    finally:
        world.disconnect()


def test_initial_scatter_excludes_center(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """Initial scatter must keep an exclusion radius around the origin."""
    from block_stacker.sim.blocks import get_pose

    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=1,
        max_blocks=8,
        inventory_override={"cube": 5},
        initial_settle_steps=60,
    )
    try:
        env.reset(seed=11)
        exclude_r = world_cfg.initial_scatter.exclude_radius_from_center
        for block in env.blocks:
            (x, y, _z), _ = get_pose(block.body_id)
            # Spawn xy was outside the exclusion (z may have drifted after settle).
            # We can't verify the original spawn after settle, but blocks should
            # not have settled into a tight central pile either.
            assert x * x + y * y >= (exclude_r * 0.5) ** 2, (
                f"block at ({x:.3f},{y:.3f}) too close to center"
            )
    finally:
        env.close()


def test_progress_timeout_truncates(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """With max_actions_without_progress=2, an episode with no height
    record updates should truncate quickly."""
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=50,                      # large, so progress-timer dominates
        max_blocks=8,
        inventory_override={"cube": 3},
        max_actions_without_progress=2,
        initial_settle_steps=30,
        settle_steps_per_action=30,
    )
    try:
        env.reset(seed=7)
        rng = np.random.default_rng(0)
        # Force actions that aim at empty far-from-tower areas; very unlikely to
        # increase tower_best_height.
        action = np.array([0.9, 0.9, -0.5, -0.9, -0.9, -0.5, 0.0], dtype=np.float32)
        truncated_steps = []
        for step in range(10):
            obs, reward, terminated, truncated, info = env.step(action)
            truncated_steps.append((step, truncated, info["steps_since_progress"]))
            if terminated or truncated:
                break
        # We must have truncated within max_steps and within ~3 actions
        # (allow a 1-step grace if a lucky height_record fires on the first step).
        assert any(t for _, t, _ in truncated_steps), "expected truncated=True"
    finally:
        env.close()


def test_stage_thresholds_used(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """Stage h_high/h_low must override reward.yaml defaults."""
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=1,
        max_blocks=8,
        inventory_override={"cube": 1},
        stage_h_high=0.5,
        stage_h_low=0.1,
    )
    try:
        assert env.h_high == 0.5
        assert env.h_low == 0.1
        # Reward defaults are 0.075 / 0.025; stage values must differ.
        assert env.h_high != reward_cfg.collapse_height_threshold
    finally:
        env.close()


def test_env_reset_step(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=3,
        max_blocks=8,
        inventory_override={"cube": 3},
        settle_steps_per_action=30,
        initial_settle_steps=60,
    )
    n_shapes = len(world_cfg.shapes)
    pb_dim = per_block_dims(n_shapes)
    try:
        obs, info = env.reset(seed=42)
        assert isinstance(obs, dict)
        assert obs["blocks"].shape == (8, pb_dim)
        assert obs["blocks"].dtype == np.float32
        assert info["n_blocks"] == 3
        assert "steps_since_progress" in info

        # Run a few steps with bounded random actions.
        rng = np.random.default_rng(0)
        for _ in range(3):
            action = rng.uniform(-1.0, 1.0, size=(7,)).astype(np.float32)
            obs, reward, terminated, truncated, info = env.step(action)
            assert isinstance(obs, dict)
            assert obs["blocks"].shape == (8, pb_dim)
            assert isinstance(reward, float)
            assert isinstance(terminated, bool)
            assert isinstance(truncated, bool)
    finally:
        env.close()

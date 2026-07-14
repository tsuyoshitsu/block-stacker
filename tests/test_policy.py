"""Tests for the NN components and Dict observation env mode."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import torch
from gymnasium import spaces

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
)
from block_stacker.env.env import BlockStackerEnv
from block_stacker.env.observation import per_block_dims
from block_stacker.policy.feature_extractor import HybridFeatureExtractor
from block_stacker.policy.heightmap_cnn import HeightmapCNN
from block_stacker.policy.set_transformer import SAB, SetEncoder

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


# -------------------------------------------------------------- Set Transformer


def test_sab_forward_shape() -> None:
    sab = SAB(dim=32, n_heads=4)
    x = torch.randn(2, 6, 32)
    mask = torch.tensor([[False] * 6, [False, False, False, True, True, True]])
    out = sab(x, key_padding_mask=mask)
    assert out.shape == (2, 6, 32)


def test_set_encoder_masked_pool() -> None:
    enc = SetEncoder(in_dim=16, hidden_dim=32, output_dim=24, n_layers=2)
    blocks = torch.randn(3, 8, 16)
    mask = torch.zeros(3, 8)
    mask[:, :5] = 1.0  # first 5 are valid
    out = enc(blocks, mask)
    assert out.shape == (3, 24)
    # Zero-out non-valid blocks should not change pooling.
    blocks_modified = blocks.clone()
    blocks_modified[:, 5:] = 99.0
    out2 = enc(blocks_modified, mask)
    # Attention layers will still see the masked positions in their input
    # projection, but key_padding_mask should suppress their influence.
    # Allow a small tolerance for floating-point determinism.
    diff = (out - out2).abs().max().item()
    assert diff < 0.5, f"masked positions affected output: max diff={diff}"


# -------------------------------------------------------------- Heightmap CNN


def test_heightmap_cnn_forward_shape() -> None:
    cnn = HeightmapCNN(in_channels=4, output_dim=32)
    x = torch.randn(4, 4, 32, 32)
    out = cnn(x)
    assert out.shape == (4, 32)


# -------------------------------------------------------------- Feature Extractor


def test_hybrid_feature_extractor_forward() -> None:
    n_shapes = 3
    pb_dim = per_block_dims(n_shapes)
    max_blocks = 8

    obs_space = spaces.Dict({
        "blocks": spaces.Box(
            low=-np.inf, high=np.inf, shape=(max_blocks, pb_dim), dtype=np.float32
        ),
        "blocks_mask": spaces.Box(low=0.0, high=1.0, shape=(max_blocks,), dtype=np.float32),
        "heightmap": spaces.Box(low=-np.inf, high=np.inf, shape=(4, 32, 32), dtype=np.float32),
        "tower_top_z": spaces.Box(low=-np.inf, high=np.inf, shape=(1,), dtype=np.float32),
    })

    fe = HybridFeatureExtractor(obs_space, features_dim=64)

    batch = 4
    obs = {
        "blocks": torch.randn(batch, max_blocks, pb_dim),
        "blocks_mask": torch.cat(
            [torch.ones(batch, 5), torch.zeros(batch, max_blocks - 5)], dim=-1
        ),
        "heightmap": torch.randn(batch, 4, 32, 32),
        "tower_top_z": torch.randn(batch, 1),
    }
    out = fe(obs)
    assert out.shape == (batch, 64)


# -------------------------------------------------------------- Env Dict mode


def test_env_dict_observation_shape(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=2,
        max_blocks=8,
        inventory_override={"cube": 3},
        observation_format="dict",
        heightmap_resolution=16,
        initial_settle_steps=30,
        settle_steps_per_action=30,
    )
    try:
        obs, _ = env.reset(seed=5)
        assert isinstance(obs, dict)
        n_shapes = len(world_cfg.shapes)
        pb_dim = per_block_dims(n_shapes)
        assert obs["blocks"].shape == (8, pb_dim)
        assert obs["blocks_mask"].shape == (8,)
        assert obs["heightmap"].shape == (4, 16, 16)
        assert obs["tower_top_z"].shape == (1,)
        # 観測は「散布ブロック（タワー非所属）」だけを枠に入れる仕様。3 個スポーンのうち
        # 1 個が最も高い地面接地ブロック＝タワー扱いになり、残り 2 個が散布として映る。
        assert obs["blocks_mask"].sum() == 2.0

        # Step also returns Dict obs.
        action = np.zeros(7, dtype=np.float32)
        obs2, _r, _t, _tr, _info = env.step(action)
        assert isinstance(obs2, dict)
        assert obs2["blocks"].shape == (8, pb_dim)
    finally:
        env.close()


def test_no_false_scatter0_on_physics_blowup(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """物理破綻（全ブロックが暴走座標）＋ prev_tower_ids 陳腐化で find_nearest が None を
    返しても、散布0（all_placed）を誤検出しないこと。回帰: これが原因で最難ステージが
    低い高さのまま即卒業していた。"""
    import pybullet as p

    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=5,
        max_blocks=8,
        inventory_override={"cube": 4},
        observation_format="dict",
        heightmap_resolution=16,
        initial_settle_steps=30,
        settle_steps_per_action=30,
    )
    try:
        env.reset(seed=3)
        ids = [b.body_id for b in env.blocks]
        # 物理破綻を模擬: 全ブロックを暴走座標へ飛ばし、除外集合を全 id に陳腐化させる。
        for bid in ids:
            p.resetBasePositionAndOrientation(bid, [1e9, 1e9, 1e9], [0, 0, 0, 1])
        env.prev_tower_ids = set(ids)  # find_nearest_excluding を None にする
        _, _, _, _, info = env.step(np.zeros(7, dtype=np.float32))
        # 全ブロックが本当にタワー所属でない限り散布0 にしてはならない。
        assert info["all_placed"] is False
    finally:
        env.close()


def test_observation_skips_nan_pose_blocks(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """NaN 姿勢のブロック（物理破綻）は観測枠から除外し、有効な散布ブロックだけを観測する。
    ゼロ埋めで原点のブロックに見せない＝mask が減り、blocks に NaN が混ざらない。"""
    import pybullet as p

    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=5,
        max_blocks=8,
        inventory_override={"cube": 4},
        observation_format="dict",
        heightmap_resolution=16,
        initial_settle_steps=30,
        settle_steps_per_action=30,
    )
    try:
        obs, _ = env.reset(seed=5)
        valid_before = int(obs["blocks_mask"].sum())
        assert valid_before >= 1
        # 散布ブロック（タワー未所属）を 1 つ NaN 姿勢にする。
        scattered = [b.body_id for b in env.blocks if b.body_id not in env.prev_tower_ids]
        assert scattered
        p.resetBasePositionAndOrientation(scattered[0], [float("nan")] * 3, [0, 0, 0, 1])
        obs2 = env._get_obs()
        # NaN ブロックは観測から外れ、有効ブロックだけ残る。
        assert int(obs2["blocks_mask"].sum()) == valid_before - 1
        # 観測配列に NaN/Inf は無い。
        assert np.isfinite(obs2["blocks"]).all()
        assert np.isfinite(obs2["heightmap"]).all()
    finally:
        env.close()


def test_env_observation_format_flat_still_works(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig, reward_cfg: RewardConfig
) -> None:
    """Flat format (backward-compat): observation_format='flat' must still return a flat Box obs."""
    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=2,
        max_blocks=8,
        inventory_override={"cube": 3},
        observation_format="flat",
        initial_settle_steps=30,
        settle_steps_per_action=30,
    )
    try:
        obs, _ = env.reset(seed=5)
        assert isinstance(obs, np.ndarray)
        n_shapes = len(world_cfg.shapes)
        assert obs.shape == ((13 + n_shapes) * 8 + 1,)
    finally:
        env.close()

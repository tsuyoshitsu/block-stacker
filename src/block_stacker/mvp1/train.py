"""MVP 1: Train SAC on Stage 1 (cube-only stacking).

Goal: verify the training loop runs end-to-end and produces an updated model.
Use small total_timesteps for quick verification; scale up later.

Run:
    uv run python -m block_stacker.mvp1.train
    uv run python -m block_stacker.mvp1.train --total-timesteps 2000

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    SAC + flat 観測 + MlpPolicy の最小学習ループ動作確認 (MVP 1)。
    MVP 2 で Set Transformer + Heightmap CNN + 短期記憶 に置き換わる前段階。

設計上のポイント:
    - SubprocVecEnv n_envs=8 で env を並列収集（Windows: spawn start method）。
      SAC は off-policy なので n_envs を上げても sample 効率には直接影響しないが、
      物理シムの収集スループットは線形に伸びる。
    - SAC は最大エントロピー方策（ent_coef="auto" で自動温度調整）+ replay buffer
      で「過去の体験を思い出しながらランダムにも試す」を成立。子供メタファーの中核。
    - MVP 1 path は flat 観測 + MlpPolicy のため、重みつき replay buffer や短期
      記憶などの新機能は使わない（後方互換の最小経路）。本格学習は MVP 2 で。
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
from pathlib import Path
from typing import Any, Callable

import yaml
from stable_baselines3 import SAC
from stable_baselines3.common.callbacks import CheckpointCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.env import BlockStackerEnv

LOG = logging.getLogger("mvp1.train")


def _make_env(
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    reward_cfg: RewardConfig,
    max_steps: int,
    max_blocks: int,
    inventory: dict[str, int],
    stage_h_high: float,
    stage_h_low: float,
    max_actions_without_progress: int,
) -> BlockStackerEnv:
    return BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=max_steps,
        max_blocks=max_blocks,
        inventory_override=inventory,
        stage_h_high=stage_h_high,
        stage_h_low=stage_h_low,
        max_actions_without_progress=max_actions_without_progress,
    )


def make_env_factory(**kwargs: Any) -> Callable[[], BlockStackerEnv]:
    def _factory() -> BlockStackerEnv:
        return _make_env(**kwargs)
    return _factory


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.mvp1.train")
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--total-timesteps", type=int, default=None,
                        help="override training.yaml sac.total_timesteps")
    parser.add_argument("--n-envs", type=int, default=None)
    parser.add_argument("--use-subproc", action="store_true", default=True,
                        help="use SubprocVecEnv when n_envs > 1 (default on)")
    parser.add_argument("--output-dir", type=Path, default=Path("./output/mvp1"))
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    sac_cfg = training_cfg["sac"]
    episode_cfg = training_cfg["episode"]
    stage1 = training_cfg["curriculum"]["stages"][0]
    obs_cfg = training_cfg["observation"]

    total_timesteps = args.total_timesteps or sac_cfg["total_timesteps"]
    n_envs = args.n_envs or sac_cfg.get("n_envs", 1)

    stage_inventory = stage1.get("inventory") or {
        name: count
        for name, count in world_cfg.inventory.items()
        if name in set(stage1["shapes_allowed"])
    }

    LOG.info("MVP 1: SAC on Stage 1 (%s)", stage1["name"])
    LOG.info("Inventory: %s, n_envs: %d", stage_inventory, n_envs)
    LOG.info("Total timesteps: %d", total_timesteps)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    factory = make_env_factory(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=episode_cfg["max_steps"],
        max_blocks=obs_cfg["max_blocks"],
        inventory=stage_inventory,
        stage_h_high=float(stage1["h_high"]),
        stage_h_low=float(stage1["h_low"]),
        max_actions_without_progress=int(
            episode_cfg.get("max_actions_without_progress", 10)
        ),
    )

    if args.use_subproc and n_envs > 1:
        vec_env = SubprocVecEnv([factory for _ in range(n_envs)], start_method="spawn")
    else:
        vec_env = DummyVecEnv([factory for _ in range(n_envs)])

    model = SAC(
        "MlpPolicy",
        vec_env,
        buffer_size=int(sac_cfg["buffer_size"]),
        learning_starts=int(sac_cfg["learning_starts"]),
        batch_size=int(sac_cfg["batch_size"]),
        learning_rate=float(sac_cfg["learning_rate"]),
        tau=float(sac_cfg["tau"]),
        gamma=float(sac_cfg["gamma"]),
        train_freq=int(sac_cfg["train_freq"]),
        gradient_steps=int(sac_cfg["gradient_steps"]),
        ent_coef=sac_cfg["ent_coef"],
        target_update_interval=int(sac_cfg["target_update_interval"]),
        verbose=1,
        seed=args.seed,
        tensorboard_log=str(args.output_dir / "tb"),
    )

    save_freq = max(int(sac_cfg["save_freq"]) // max(1, n_envs), 1)
    checkpoint_cb = CheckpointCallback(
        save_freq=save_freq,
        save_path=str(args.output_dir / "checkpoints"),
        name_prefix="sac_stage1",
    )

    LOG.info("Beginning training...")
    model.learn(
        total_timesteps=total_timesteps,
        callback=checkpoint_cb,
        log_interval=int(sac_cfg["log_interval"]),
    )

    final_path = args.output_dir / "sac_stage1_final.zip"
    model.save(str(final_path))
    LOG.info("Saved final model: %s", final_path)

    vec_env.close()


if __name__ == "__main__":
    mp.freeze_support()
    main()

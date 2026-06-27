"""Load a trained MVP 2 SAC model and run a few episodes.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    MVP 2 (Set Transformer + CNN + 短期記憶) で学習したモデルの動作確認 CLI。
    Dict 観測形式と MultiInputPolicy の組み合わせで推論できることを検証。

設計上のポイント:
    - observation_format="dict" を env に明示渡し。
    - stm_length は training.yaml から読む（学習時と一致させる）。
    - inventory_override={"cube": 5} で Stage 1 と同じ分布に揃える。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import yaml
from stable_baselines3 import SAC

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.env import BlockStackerEnv

LOG = logging.getLogger("mvp2.eval")


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.mvp2.eval")
    parser.add_argument("--model", type=Path, default=Path("output/mvp2/sac_final.zip"))
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    # stm_length を training.yaml から拾う（学習時と推論時で観測形状を一致させる）
    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)
    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0

    model = SAC.load(str(args.model))
    n_params = sum(p.numel() for p in model.policy.parameters())
    LOG.info("Model loaded: policy=%s, n_params=%d, stm_length=%d",
             type(model.policy).__name__, n_params, stm_length)

    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=args.max_steps,
        max_blocks=8,
        inventory_override={"cube": 5},
        render_mode="human" if args.gui else None,
        stage_h_high=0.10,
        stage_h_low=0.03,
        observation_format="dict",
        stm_length=stm_length,
    )

    try:
        for ep in range(args.episodes):
            obs, info = env.reset(seed=args.seed + ep)
            total_reward = 0.0
            n_placed = 0
            for step in range(args.max_steps):
                action, _ = model.predict(obs, deterministic=args.deterministic)
                obs, reward, terminated, truncated, info = env.step(action)
                total_reward += reward
                if reward > 0.5:
                    n_placed += 1
                LOG.info(
                    "  ep%d step%d: r=%+.3f, event=%s, tower=%.4f, best=%.4f, term=%s, trunc=%s",
                    ep, step, reward, info.get("event_type", "?"),
                    info["tower_height"], info["tower_best_height"],
                    terminated, truncated,
                )
                if terminated or truncated:
                    break
            LOG.info(
                "ep%d total_reward=%.3f, best_height=%.4fm, placements=%d",
                ep, total_reward, info["tower_best_height"], n_placed,
            )
    finally:
        env.close()


if __name__ == "__main__":
    main()

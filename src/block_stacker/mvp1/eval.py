"""Load a trained SAC model (MVP 1) and run one episode to verify inference works.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    MVP 1 の学習済みモデル動作確認 CLI。Stage 1 を 1〜数エピソード走らせて
    報酬・タワー高・終了条件をログ。

設計上のポイント:
    - observation_format="flat" は env のデフォルトなので明示不要。
    - --gui で PyBullet GUI を起動可能（ローカルのみ）。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from stable_baselines3 import SAC

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.env import BlockStackerEnv

LOG = logging.getLogger("mvp1.eval")


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.mvp1.eval")
    parser.add_argument("--model", type=Path, default=Path("output/mvp1/sac_stage1_final.zip"))
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    model = SAC.load(str(args.model))
    n_params = sum(p.numel() for p in model.policy.parameters())
    LOG.info("Model loaded: policy=%s, n_params=%d", type(model.policy).__name__, n_params)

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

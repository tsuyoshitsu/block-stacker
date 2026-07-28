r"""Load a trained SAC model and run a few episodes headless, printing numbers.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    学習済みモデルを **WebSocket も Godot も使わずに** 走らせ、1 手ごとの報酬・
    event_type・タワー高さと、エピソード合計を標準出力に出す数値評価 CLI。

    ai_server / live_server / tools\replay_checkpoints.ps1 はいずれも「配信して目で見る」
    ための経路で、数値は出さない。「このモデルは本当に良くなったのか」を配信スタックを
    立ち上げずに確かめたいとき、および報酬設計をいじった直後の確認がここの役割。

使い方:
    .venv\Scripts\python.exe -m block_stacker.training.eval            # 最新モデル・最終ステージ
    .venv\Scripts\python.exe -m block_stacker.training.eval --stage 1  # Stage 1 の世界で評価
    .venv\Scripts\python.exe -m block_stacker.training.eval --model <path> --episodes 5 --gui

設計上のポイント:
    - 評価する世界は **training.yaml の curriculum.stages から引く**（既定は最終ステージ）。
      ai_server と同じ解決なので、配信で見える世界と数値評価の世界がズレない。
      （かつては inventory/h_high/h_low をここに直書きしていて、config を変えても
        追従せず、どのステージとも一致しない世界で評価していた）
    - stm_length も training.yaml から読む（学習時と観測形状を一致させる）。
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
from block_stacker.training.checkpoint import find_latest_checkpoint
from block_stacker.training.curriculum import resolve_graduation, stage_inventory

LOG = logging.getLogger("training.eval")


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.training.eval")
    parser.add_argument("--model", type=Path, default=None,
                        help="モデルパス。無指定なら最新 run の最大ステップを自動選択")
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--stage", type=int, default=None,
                        help="評価する世界のステージ id。無指定なら最終ステージ")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=None,
                        help="1 エピソードの最大手数。無指定なら episode.max_steps")
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if args.model is None:
        args.model = find_latest_checkpoint(Path("output/training"))
        if args.model is None:
            raise SystemExit(
                "fresh/ / played/ に checkpoint が見つかりません。学習後に再実行してください。"
            )
        LOG.info("--model 未指定: 自動選択 %s", args.model)

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    # stm_length を training.yaml から拾う（学習時と推論時で観測形状を一致させる）
    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0

    # 評価する世界は config から引く（既定は最終ステージ。ai_server と同じ解決）。
    stages = training_cfg["curriculum"]["stages"]
    if args.stage is not None:
        stage = next((s for s in stages if s.get("id") == args.stage), stages[-1])
    else:
        stage = stages[-1]
    inventory = stage_inventory(stage, world_cfg)
    _, grad_ratio = resolve_graduation(training_cfg.get("curriculum", {}).get("graduation", {}))
    obs_cfg = training_cfg["observation"]
    max_steps = args.max_steps or int(training_cfg["episode"]["max_steps"])

    model = SAC.load(str(args.model))
    n_params = sum(p.numel() for p in model.policy.parameters())
    LOG.info("Model loaded: policy=%s, n_params=%d, stm_length=%d",
             type(model.policy).__name__, n_params, stm_length)
    LOG.info("eval stage: id=%s '%s' inventory=%s h_high=%.3f h_low=%.3f max_steps=%d",
             stage.get("id"), stage.get("name", ""), inventory,
             float(stage["h_high"]), float(stage["h_low"]), max_steps)

    env = BlockStackerEnv(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=max_steps,
        max_blocks=int(obs_cfg["max_blocks"]),
        inventory_override=inventory,
        render_mode="human" if args.gui else None,
        stage_h_high=float(stage["h_high"]),
        stage_h_low=float(stage["h_low"]),
        target_height_ratio=grad_ratio,
        stm_length=stm_length,
    )

    try:
        for ep in range(args.episodes):
            obs, info = env.reset(seed=args.seed + ep)
            total_reward = 0.0
            n_placed = 0
            for step in range(max_steps):
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

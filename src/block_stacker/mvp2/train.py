"""MVP 2: SAC training with Set Transformer + Heightmap CNN + 短期記憶 + 重みつき記憶バッファ。

Differences from MVP 1:
    - Dict observation (blocks + mask + heightmap + scalar + optional 短期記憶)
    - Custom feature extractor (HybridFeatureExtractor)
    - WeightedReplayBuffer による「人間っぽい記憶」ダイナミクス
    - SubprocVecEnv parallel by default (`--use-subproc`)
    - MultiInputPolicy instead of MlpPolicy

Run:
    uv run python -m block_stacker.mvp2.train
    uv run python -m block_stacker.mvp2.train --total-timesteps 30000 --n-envs 8

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    子供メタファーに沿った SAC 学習:
      - 勘  = ニューラルネットの重み
      - 直近の記憶 = 観測辞書に "recent_*" を追加
      - 長期の記憶 = WeightedReplayBuffer（重みつき + 時間減衰 + recall ノイズ）

設計上のポイント:
    - observation_format="dict", stm_length=N で短期記憶を含む Dict 観測に。
    - MultiInputPolicy + HybridFeatureExtractor が短期記憶ストリームを自動で扱う。
    - replay_buffer_class=WeightedReplayBuffer を渡し、kwargs で記憶ダイナミクスを設定。
    - memory_system.enabled=false なら標準 SAC バッファに退化（A/B 比較用）。
    - --use-subproc + n_envs > 1 で SubprocVecEnv 起動（Windows: spawn 方式）。

レビューで見る観点:
    - n_envs × n_steps が batch_size を割り切れる必要は SAC にはない（off-policy）。
    - n_envs=8 SubprocVecEnv は env 並列化のみ。学習側は gradient_steps で調整。
    - 重みつきバッファは sampling 時に O(buffer_size) の重み計算をする。
      buffer_size=50k なら 1 step あたり ms オーダー、無視できるコスト。

関連:
    - 推論: src/block_stacker/mvp3/ai_server.py（同じ Dict 観測 + 短期記憶を再現）
    - 重みつきバッファ: src/block_stacker/policy/weighted_replay_buffer.py
    - 設定: configs/training.yaml の sac: + memory_system: + short_term_memory:
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
from block_stacker.env.env import BlockStackerEnv, inventory_full_stack_height
from block_stacker.mvp2.curriculum import (
    GraduationCallback,
    resolve_graduation,
    stage_inventory,
)
from block_stacker.policy.feature_extractor import HybridFeatureExtractor
from block_stacker.policy.weighted_replay_buffer import WeightedReplayBuffer

LOG = logging.getLogger("mvp2.train")


def _make_env(
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    reward_cfg: RewardConfig,
    max_steps: int,
    max_blocks: int,
    inventory: dict[str, int],
    stage_h_high: float,
    stage_h_low: float,
    target_height_ratio: float,
    max_actions_without_progress: int,
    heightmap_resolution: int,
    stm_length: int,
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
        target_height_ratio=target_height_ratio,
        max_actions_without_progress=max_actions_without_progress,
        observation_format="dict",
        heightmap_resolution=heightmap_resolution,
        stm_length=stm_length,
    )


def make_factory(**kwargs: Any) -> Callable[[], BlockStackerEnv]:
    def _factory() -> BlockStackerEnv:
        return _make_env(**kwargs)
    return _factory


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.mvp2.train")
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--total-timesteps", type=int, default=None)
    parser.add_argument("--n-envs", type=int, default=None)
    parser.add_argument("--use-subproc", action="store_true", default=True,
                        help="use SubprocVecEnv when n_envs > 1 (default on)")
    parser.add_argument("--output-dir", type=Path, default=Path("./output/mvp2"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--curriculum", action=argparse.BooleanOptionalAction, default=True,
                        help="Stage 1→N を順に自動学習（既定 ON）。--no-curriculum で Stage 1 のみ")
    parser.add_argument("--start-stage", type=int, default=1,
                        help="--curriculum 時の開始ステージ番号（1始まり）")
    parser.add_argument("--max-stage", type=int, default=None,
                        help="最終ステージ番号（既定=全ステージ）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    sac_cfg = training_cfg["sac"]
    episode_cfg = training_cfg["episode"]
    obs_cfg = training_cfg["observation"]
    mem_cfg = training_cfg.get("memory_system", {})
    stm_cfg = training_cfg.get("short_term_memory", {})
    curr_cfg = training_cfg.get("curriculum", {})
    all_stages = curr_cfg["stages"]
    grad_cfg = curr_cfg.get("graduation", {})
    # 環境変数 BS_GRADUATION_* > training.yaml > 既定値 で解決。
    grad_window, grad_threshold, grad_ratio = resolve_graduation(grad_cfg)

    total_timesteps = args.total_timesteps or sac_cfg["total_timesteps"]
    n_envs = args.n_envs or sac_cfg.get("n_envs", 1)
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0

    # 実行するステージ列を決める。--curriculum 無指定なら従来通り Stage 1 のみ（後方互換）。
    if args.curriculum:
        start_idx = max(1, args.start_stage) - 1
        end_idx = (
            len(all_stages) if args.max_stage is None
            else min(args.max_stage, len(all_stages))
        )
        run_stages = all_stages[start_idx:end_idx]
        if not run_stages:
            raise SystemExit(
                f"no stages selected: start={args.start_stage}, max={args.max_stage}"
            )
    else:
        run_stages = [all_stages[0]]

    LOG.info("MVP 2: SAC (%s)",
             "curriculum" if args.curriculum else f"single-stage: {all_stages[0]['name']}")
    LOG.info("Total timesteps (全ステージ合計の上限): %d, n_envs: %d, subproc=%s, stm_length=%d",
             total_timesteps, n_envs, args.use_subproc, stm_length)
    LOG.info("Memory system enabled: %s", mem_cfg.get("enabled", False))
    if args.curriculum:
        LOG.info("Curriculum: stages %s, graduate at success_rate >= %.2f over %d eps",
                 [s.get("id") for s in run_stages], grad_threshold, grad_window)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # --- stage 非依存の設定（policy / 重みつき replay buffer）を一度だけ組む ---
    policy_kwargs = {
        "features_extractor_class": HybridFeatureExtractor,
        "features_extractor_kwargs": {
            "features_dim": int(sac_cfg.get("features_dim", 128)),
        },
    }

    replay_buffer_class: Any = None
    replay_buffer_kwargs: dict[str, Any] = {}
    if mem_cfg.get("enabled", False):
        replay_buffer_class = WeightedReplayBuffer
        recall_cfg = mem_cfg.get("recall_noise", {})
        height_cfg = mem_cfg.get("height_weighting", {})
        replay_buffer_kwargs = {
            "initial_weights": mem_cfg.get("initial_weights", {}),
            "decay_rate": float(mem_cfg.get("decay_rate", 0.9999)),
            "coordinate_blur": float(recall_cfg.get("coordinate_sigma", 0.05)),
            "recall_noise_enabled": bool(recall_cfg.get("enabled", True)),
            "eviction": str(mem_cfg.get("eviction", "min_weight")),
            "eviction_tournament_k": int(mem_cfg.get("eviction_tournament_k", 16)),
            "weight_floor": float(mem_cfg.get("weight_floor", 0.001)),
            "height_weighting_enabled": bool(height_cfg.get("enabled", False)),
            "height_weight_coef": float(height_cfg.get("coef", 1.0)),
            "height_reference": float(height_cfg.get("reference", 0.10)),
            "height_max_factor": float(height_cfg.get("max_factor", 3.0)),
        }

    save_freq = max(int(sac_cfg["save_freq"]) // max(1, n_envs), 1)

    def build_vec_env(stage: dict[str, Any]) -> DummyVecEnv | SubprocVecEnv:
        factory = make_factory(
            world_cfg=world_cfg,
            physics_cfg=physics_cfg,
            reward_cfg=reward_cfg,
            max_steps=episode_cfg["max_steps"],
            max_blocks=obs_cfg["max_blocks"],
            inventory=stage_inventory(stage, world_cfg),
            stage_h_high=float(stage["h_high"]),
            stage_h_low=float(stage["h_low"]),
            target_height_ratio=grad_ratio,
            max_actions_without_progress=int(
                episode_cfg.get("max_actions_without_progress", 10)
            ),
            heightmap_resolution=int(obs_cfg.get("heightmap_resolution", 32)),
            stm_length=stm_length,
        )
        if args.use_subproc and n_envs > 1:
            return SubprocVecEnv([factory for _ in range(n_envs)], start_method="spawn")
        return DummyVecEnv([factory for _ in range(n_envs)])

    # チェックポイントは全ステージ通して「ステップ数ごと」に連続記録する（ステージ別にしない）。
    # コールバックを1つ使い回すことで n_calls が連続し、cadence がステージ跨ぎで途切れない。
    # → checkpoints/sac_<手数>_steps.zip。demo_checkpoints.ps1 がステップ順に再生。
    checkpoint_cb = CheckpointCallback(
        save_freq=save_freq,
        save_path=str(args.output_dir / "checkpoints"),
        name_prefix="sac",
    )

    # --- ステージを順に学習。観測空間は全 stage 共通なので、同じ model を
    #     set_env() で env だけ付け替える（NN も記憶バッファも引き継ぐ）。 ---
    model: SAC | None = None
    for stage in run_stages:
        stage_id = stage.get("id", 1)
        # グローバル予算を使い切っていたら、以降のステージは学習しない。
        if model is not None and model.num_timesteps >= total_timesteps:
            LOG.warning("グローバル予算 %d を使い切ったため Stage %s 以降は学習しません。",
                        total_timesteps, stage_id)
            break
        inv = stage_inventory(stage, world_cfg)
        target_h = inventory_full_stack_height(inv, world_cfg.shapes) * grad_ratio
        LOG.info("=== Stage %s: %s ===", stage_id, stage.get("name", ""))
        LOG.info("Inventory: %s, target_height=%.3f (=満積み×%.2f), h_high=%.3f, h_low=%.3f",
                 inv, target_h, grad_ratio, float(stage["h_high"]), float(stage["h_low"]))

        vec_env = build_vec_env(stage)

        if model is None:
            model = SAC(
                "MultiInputPolicy",
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
                policy_kwargs=policy_kwargs,
                replay_buffer_class=replay_buffer_class,
                replay_buffer_kwargs=replay_buffer_kwargs if replay_buffer_class else None,
            )
            reset_timesteps = True
        else:
            model.set_env(vec_env)
            reset_timesteps = False
        # --total-timesteps は「全ステージ通算の上限（グローバル予算）」。
        # 各ステージは通算 num_timesteps がこの値に達するまで（または卒業まで）走る。
        # → 早く卒業した分の残りは次ステージへ回り、総手数は必ず total_timesteps 以下になる。
        learn_target = total_timesteps

        callbacks: list[Any] = [checkpoint_cb]
        grad_cb: GraduationCallback | None = None
        if args.curriculum:
            grad_cb = GraduationCallback(
                window=grad_window, threshold=grad_threshold,
                stage_id=stage_id, verbose=1,
            )
            callbacks.append(grad_cb)

        LOG.info("Beginning training (stage %s; 残り予算 %d / 全体 %d)...",
                 stage_id, total_timesteps - model.num_timesteps, total_timesteps)
        model.learn(
            total_timesteps=learn_target,
            callback=callbacks,
            log_interval=int(sac_cfg["log_interval"]),
            reset_num_timesteps=reset_timesteps,
        )

        vec_env.close()

        if args.curriculum and grad_cb is not None:
            if grad_cb.graduated:
                LOG.info("Stage %s graduated (success_rate=%.2f).",
                         stage_id, grad_cb.success_rate)
            else:
                LOG.warning(
                    "Stage %s did NOT graduate (success_rate=%.2f < %.2f). "
                    "グローバル予算 %d を使い切り中断（予算を増やすか設定見直しを）。",
                    stage_id, grad_cb.success_rate, grad_threshold, total_timesteps,
                )
                break

    assert model is not None
    # 最終モデルのみ保存（ステージごとの最終モデルは保存しない＝checkpoints/ で補完）。
    # 単一ステージ・カリキュラム共通の成果物。ai_server の既定解決もこれを優先。
    final_path = args.output_dir / "sac_final.zip"
    model.save(str(final_path))
    LOG.info("Saved final model: %s (途中は checkpoints/ 参照)", final_path)


if __name__ == "__main__":
    mp.freeze_support()
    main()

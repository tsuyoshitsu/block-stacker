"""MVP 2: SAC training with Set Transformer + Heightmap CNN + 短期記憶 + 重みつき記憶バッファ。

Differences from MVP 1:
    - Dict observation (blocks + mask + heightmap + scalar + optional 短期記憶)
    - Custom feature extractor (HybridFeatureExtractor)
    - WeightedReplayBuffer による「人間っぽい記憶」ダイナミクス
    - SubprocVecEnv parallel by default (`--use-subproc`)
    - MultiInputPolicy instead of MlpPolicy

Run:
    .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000
    .venv\Scripts\python.exe -m block_stacker.mvp2.train --n-envs 6 --total-timesteps 4000 --resume

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
import json
import logging
import multiprocessing as mp
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

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
from block_stacker.mvp2.checkpoint import find_latest_checkpoint
from block_stacker.mvp2.curriculum import (
    GraduationCallback,
    StageMonitorCallback,
    resolve_graduation,
    stage_inventory,
)
from block_stacker.policy.feature_extractor import HybridFeatureExtractor
from block_stacker.policy.weighted_replay_buffer import WeightedReplayBuffer

LOG = logging.getLogger("mvp2.train")


def _compute_elapsed_steps(resume_cfg: dict[str, Any], resume_state: dict[str, Any]) -> int:
    """--resume 時に長期記憶（WeightedReplayBuffer）の global_step へ加算するステップ数を算出。

    優先順位: elapsed_steps（直接指定）> elapsed_days（日数）> timestamp 差から自動算出。
    """
    if resume_cfg.get("elapsed_steps") is not None:
        return max(0, int(resume_cfg["elapsed_steps"]))
    steps_per_day = int(resume_cfg.get("steps_per_day", 5000))
    if resume_cfg.get("elapsed_days") is not None:
        return max(0, int(resume_cfg["elapsed_days"])) * steps_per_day
    # 自動: resume_state.json の timestamp から経過日数を計算
    ts = resume_state.get("timestamp")
    if ts:
        prev = datetime.fromisoformat(ts)
        now = datetime.now()
        if prev.tzinfo is not None:
            prev = prev.replace(tzinfo=None)
        days = max(0, (now - prev).days)
        return days * steps_per_day
    return 0


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
    parser.add_argument("--resume", action="store_true", default=False,
                        help="前回の学習を --output-dir から引き継いで続きから学習する。"
                             "勘（NN重み）と長期記憶（replay_buffer.pkl）を復元し、"
                             "長期記憶には経過日数×steps_per_day 分の時間減衰を適用する。")
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
    resume_cfg = training_cfg.get("resume", {})

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

    # --- fresh/ を今回の学習専用にリセット ---
    # 前回の未再生 checkpoint が fresh/ に残っていれば played/ へ退避してから学習を開始する。
    # played/ へ移すことで --resume 時にも参照可能にする（安全側）。
    fresh_dir = args.output_dir / "fresh"
    played_dir = args.output_dir / "played"
    existing_fresh = sorted(fresh_dir.glob("sac_*_steps.zip")) if fresh_dir.exists() else []
    if existing_fresh:
        played_dir.mkdir(parents=True, exist_ok=True)
        for p in existing_fresh:
            shutil.move(str(p), str(played_dir / p.name))
        LOG.info(
            "学習開始: fresh/ の既存 checkpoint %d 本を played/ へ退避 %s",
            len(existing_fresh), [p.name for p in existing_fresh],
        )
    fresh_dir.mkdir(parents=True, exist_ok=True)

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

    # checkpoint を total_timesteps の等分地点（checkpoint_splits 等分）で fresh/ に保存する。
    # SB3 CheckpointCallback の save_freq は「1 本の環境ストリームあたりのステップ数
    # (= n_calls)」基準のため、n_envs 並列では n_envs で割って実際の通算ステップを合わせる。
    #
    # 例: total_timesteps=4000, splits=5, n_envs=6
    #   save_freq = 4000 // 5 // 6 = 133
    #   実際の保存ステップ: 133*6=798, 266*6=1596, ... ≈ 20/40/60/80/100 %
    #
    # 最終地点（100 %）の checkpoint が最終モデル相当（sac_final.zip は廃止）。
    # advance_day.ps1 / local_loop.ps1 が fresh/ を参照して順に再生する。
    splits = int(sac_cfg.get("checkpoint_splits", 5))
    save_freq = max(total_timesteps // splits // max(1, n_envs), 1)
    LOG.info(
        "Checkpoint: splits=%d, save_freq=%d calls (≈%d total steps per split, n_envs=%d)",
        splits, save_freq, save_freq * n_envs, n_envs,
    )

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
    # → fresh/sac_<手数>_steps.zip。advance_day.ps1 / local_loop.ps1 がステップ順に再生。
    checkpoint_cb = CheckpointCallback(
        save_freq=save_freq,
        save_path=str(args.output_dir / "fresh"),
        name_prefix="sac",
    )

    # 再開状態のトラッキング（終了時に replay_buffer.pkl / resume_state.json へ保存）
    completed_stages: list[int] = []
    last_active_stage_id: int | None = None

    # --- Resume: 前回の学習状態を読み込む ---
    # 勘（NN重み）と長期記憶（WeightedReplayBuffer）を引き継ぐ。
    # 短期記憶 (recent_* deque) は env.reset() で自動クリアされるため何もしない（設計通り）。
    model: SAC | None = None
    if args.resume:
        sac_path = find_latest_checkpoint(args.output_dir)
        if sac_path is None:
            raise SystemExit(
                "--resume 指定だが fresh/ / played/ に checkpoint が見つかりません。"
                "初回学習後に再実行してください。"
            )
        buf_path = args.output_dir / "replay_buffer.pkl"
        state_path = args.output_dir / "resume_state.json"
        resume_state: dict[str, Any] = {}
        if state_path.exists():
            with state_path.open("r", encoding="utf-8-sig") as f:
                resume_state = json.load(f)
            LOG.info(
                "--resume: num_timesteps=%d, next_stage=%s, completed=%s",
                resume_state.get("num_timesteps", "?"),
                resume_state.get("next_stage_id", "?"),
                resume_state.get("completed_stages", []),
            )
        else:
            LOG.warning(
                "resume_state.json が見つかりません: NN重みのみロードし Stage 1 から再開します"
            )
        # カリキュラム進捗を復元: next_stage_id 以降のステージのみ実行
        resume_next = resume_state.get("next_stage_id")
        if resume_next is not None:
            filtered = [s for s in run_stages if s.get("id", 1) >= resume_next]
            if filtered:
                run_stages = filtered
            else:
                LOG.warning(
                    "next_stage_id=%s 以降のステージがありません。全ステージで再開します。",
                    resume_next,
                )
        completed_stages = list(resume_state.get("completed_stages", []))
        # 勘（NN重み・オプティマイザ・num_timesteps）をロード
        LOG.info("--resume: NN重みを %s からロード", sac_path)
        model = SAC.load(str(sac_path))
        # 長期記憶（WeightedReplayBuffer）をロード
        if buf_path.exists():
            model.load_replay_buffer(str(buf_path))
            if isinstance(model.replay_buffer, WeightedReplayBuffer):
                elapsed = _compute_elapsed_steps(resume_cfg, resume_state)
                if elapsed > 0:
                    old_gs = model.replay_buffer.global_step
                    model.replay_buffer.global_step += elapsed
                    LOG.info(
                        "長期記憶: %d ステップ分の時間減衰を適用 "
                        "(global_step %d → %d, decay_rate^%d ≈ %.4f)",
                        elapsed, old_gs, model.replay_buffer.global_step,
                        elapsed, model.replay_buffer.decay_rate ** min(elapsed, 50000),
                    )
                else:
                    LOG.info("長期記憶: 経過0日のため減衰なし")
            else:
                LOG.info("長期記憶: 標準バッファのため減衰スキップ")
        else:
            LOG.warning(
                "replay_buffer.pkl が見つかりません: 長期記憶はゼロから開始します (%s)", buf_path
            )

    # --- ステージを順に学習。観測空間は全 stage 共通なので、同じ model を
    #     set_env() で env だけ付け替える（NN も記憶バッファも引き継ぐ）。 ---
    for stage in run_stages:
        stage_id = stage.get("id", 1)
        last_active_stage_id = stage_id
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
                completed_stages.append(stage_id)
            else:
                LOG.warning(
                    "Stage %s did NOT graduate (success_rate=%.2f < %.2f). "
                    "グローバル予算 %d を使い切り中断（予算を増やすか設定見直しを）。",
                    stage_id, grad_cb.success_rate, grad_threshold, total_timesteps,
                )
                break

    # --- 全ステージ早期卒業後も予算が残っていれば最終ステージで継続学習 ---
    # GraduationCallback が model.learn() を early-exit させるため、
    # 最終ステージが total_timesteps 未満で卒業するとその分の checkpoint が欠落する。
    # ループ後に予算が残っていた場合は最終ステージの環境で消化し、
    # checkpoint を total_timesteps まで埋める。
    # （非卒業で break した場合は num_timesteps >= total_timesteps なのでここは skip される。）
    if model is not None and model.num_timesteps < total_timesteps:
        final_stage = run_stages[-1]
        LOG.info(
            "残り予算 %d steps を Stage %s (最終) で継続学習します（全ステージ早期卒業後）",
            total_timesteps - model.num_timesteps, final_stage.get("id", "?"),
        )
        continuation_env = build_vec_env(final_stage)
        model.set_env(continuation_env)
        last_active_stage_id = final_stage.get("id")
        # GraduationCallback は False を返すと learn() を止めてしまうため使えない。
        # StageMonitorCallback で curriculum/stage・success_rate のみ継続記録する。
        continuation_monitor = StageMonitorCallback(
            stage_id=final_stage.get("id"), window=grad_window,
        )
        model.learn(
            total_timesteps=total_timesteps,
            callback=[checkpoint_cb, continuation_monitor],
            log_interval=int(sac_cfg["log_interval"]),
            reset_num_timesteps=False,
        )
        continuation_env.close()

    assert model is not None
    LOG.info("学習完了: 最終モデル = fresh/ の最大ステップ checkpoint (sac_final.zip は廃止)")

    # 長期記憶（リプレイバッファ）を毎回保存（--resume 時に利用）。
    buf_save_path = args.output_dir / "replay_buffer.pkl"
    model.save_replay_buffer(str(buf_save_path))
    LOG.info("長期記憶を保存: %s", buf_save_path)

    # 再開状態を JSON に保存（次回 --resume で参照）。
    next_stage = (
        last_active_stage_id
        if last_active_stage_id is not None
        else run_stages[0].get("id", 1)
    )
    resume_out: dict[str, Any] = {
        "num_timesteps": int(model.num_timesteps),
        "total_timesteps": int(total_timesteps),
        "buffer_global_step": int(getattr(model.replay_buffer, "global_step", 0)),
        "next_stage_id": next_stage,
        "completed_stages": completed_stages,
        "timestamp": datetime.now().isoformat(),
    }
    state_out_path = args.output_dir / "resume_state.json"
    with state_out_path.open("w", encoding="utf-8") as f:
        json.dump(resume_out, f, indent=2, ensure_ascii=False)
    LOG.info(
        "Resume state saved: %s (next_stage=%s, completed=%s)",
        state_out_path, next_stage, completed_stages,
    )


if __name__ == "__main__":
    mp.freeze_support()
    main()

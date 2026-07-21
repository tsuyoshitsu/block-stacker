"""SAC training with Set Transformer + Heightmap CNN + 短期記憶 + 重みつき記憶バッファ。

Features:
    - Dict observation (blocks + mask + heightmap + scalar + optional 短期記憶)
    - Custom feature extractor (HybridFeatureExtractor)
    - WeightedReplayBuffer による「人間っぽい記憶」ダイナミクス
    - SubprocVecEnv parallel by default (`--use-subproc`)
    - MultiInputPolicy instead of MlpPolicy

Run:
    .venv/Scripts/python.exe -m block_stacker.training.train --n-envs 1

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    子供メタファーに沿った SAC 学習:
      - 勘  = ニューラルネットの重み
      - 直近の記憶 = 観測辞書に "recent_*" を追加
      - 長期の記憶 = WeightedReplayBuffer（重みつき + 時間減衰 + recall ノイズ）

設計上のポイント:
    - stm_length=N で短期記憶を含む Dict 観測に（observation_format は dict 固定）。
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
    - 推論: src/block_stacker/serving/ai_server.py（同じ Dict 観測 + 短期記憶を再現）
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
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.env import BlockStackerEnv, inventory_full_stack_height
from block_stacker.policy.feature_extractor import HybridFeatureExtractor
from block_stacker.policy.weighted_replay_buffer import WeightedReplayBuffer
from block_stacker.training.curriculum import (
    StageMonitorCallback,
    resolve_graduation,
    stage_inventory,
)

LOG = logging.getLogger("training.train")


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
) -> Monitor:
    """1 本の学習用 env を作る。

    Monitor で包むのは必須。SB3 は Monitor が info に載せる "episode" キーから
    rollout/ep_rew_mean・ep_len_mean を算出するため、無しだと **報酬曲線が
    TensorBoard に一切出ない**（rollout/success_rate だけは info["is_success"]
    から出るので、欠落に気づきにくい）。
    """
    env = BlockStackerEnv(
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
        heightmap_resolution=heightmap_resolution,
        stm_length=stm_length,
    )
    return Monitor(env)


def make_factory(**kwargs: Any) -> Callable[[], Monitor]:
    def _factory() -> Monitor:
        return _make_env(**kwargs)
    return _factory


def _retire_fresh_checkpoints(fresh_dir: Path, played_dir: Path) -> None:
    """学習開始前に fresh/ の既存 checkpoint を played/ へ退避する。

    前回の未再生モデルを played/ へ移し、日次配信や live_server から参照可能にする。
    """
    existing = sorted(fresh_dir.glob("sac_*_steps.zip")) if fresh_dir.exists() else []
    if existing:
        played_dir.mkdir(parents=True, exist_ok=True)
        for p in existing:
            shutil.move(str(p), str(played_dir / p.name))
        LOG.info(
            "学習開始: fresh/ の既存 checkpoint %d 本を played/ へ退避 %s",
            len(existing), [p.name for p in existing],
        )
    fresh_dir.mkdir(parents=True, exist_ok=True)


#: `--stage-steps` も stage の `steps:` も無い場合に使うフォールバック。
DEFAULT_STAGE_STEPS = 300_000


def resolve_stage_budgets(
    spec: str | None,
    run_stages: list[dict[str, Any]],
) -> list[int]:
    """各ステージの学習ステップ数（固定予算）を決める。

    優先順位: --stage-steps > configs/training.yaml の stages[].steps > DEFAULT_STAGE_STEPS。

    spec の書式:
        None              未指定。YAML の steps を使う。
        "100000"          単一値 = 全ステージ一括で同じ値。
        "3e5,3e5,35e4"    カンマ区切り = 実行するステージへ順に割当（要素数一致が必須）。

    Raises:
        SystemExit: 値が不正、または要素数が実行ステージ数と一致しない場合。
    """
    n = len(run_stages)
    if spec is None:
        return [
            int(stage.get("steps", DEFAULT_STAGE_STEPS)) for stage in run_stages
        ]

    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        raise SystemExit("--stage-steps が空です")
    try:
        values = [int(float(p)) for p in parts]
    except ValueError as exc:
        raise SystemExit(f"--stage-steps を数値として解釈できません: {spec!r}") from exc
    if any(v <= 0 for v in values):
        raise SystemExit(f"--stage-steps は正の値が必要です: {spec!r}")

    if len(values) == 1:
        return values * n
    if len(values) != n:
        stage_ids = [s.get("id", "?") for s in run_stages]
        raise SystemExit(
            f"--stage-steps の要素数 {len(values)} が実行ステージ数 {n} と一致しません "
            f"(対象ステージ: {stage_ids})。単一値なら一括指定になります。"
        )
    return values


def _run_stage_loop(
    model: SAC | None,
    run_stages: list[dict[str, Any]],
    stage_budgets: list[int],
    build_vec_env: Callable[[dict[str, Any]], DummyVecEnv | SubprocVecEnv],
    total_timesteps: int,
    sac_cfg: dict[str, Any],
    curriculum: bool,
    grad_window: int,
    grad_ratio: float,
    completed_stages: list[int],
    policy_kwargs: dict[str, Any],
    replay_buffer_class: Any,
    replay_buffer_kwargs: dict[str, Any],
    seed: int,
    world_cfg: WorldConfig,
    output_dir: Path,
) -> tuple[SAC, list[int], int | None]:
    """ステージを順に学習するカリキュラムメインループ（固定ステップ制）。

    model が None（初回学習）の場合は最初のステージで SAC を新規作成する。
    以降のステージでは env だけ付け替えて NN・記憶バッファを引き継ぐ。
    観測空間は全ステージ共通のため set_env() での付け替えが可能。

    各ステージは stage_budgets[i] ステップだけ走り、**成績によらず**次へ進む
    （卒業判定は廃止）。グローバル予算 total_timesteps を超える分は切り詰める。

    Returns:
        (model, completed_stages, last_active_stage_id)
    """
    last_active_stage_id: int | None = None

    for stage, budget in zip(run_stages, stage_budgets, strict=True):
        stage_id = stage.get("id", 1)
        last_active_stage_id = stage_id
        # グローバル予算を使い切っていたら、以降のステージは学習しない。
        done_steps = model.num_timesteps if model is not None else 0
        remaining = total_timesteps - done_steps
        if remaining <= 0:
            LOG.warning("グローバル予算 %d を使い切ったため Stage %s 以降は学習しません。",
                        total_timesteps, stage_id)
            break
        if budget > remaining:
            LOG.warning(
                "Stage %s の予算 %d はグローバル残 %d を超えるため %d に切り詰めます。",
                stage_id, budget, remaining, remaining,
            )
            budget = remaining
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
                seed=seed,
                tensorboard_log=str(output_dir / "tb"),
                policy_kwargs=policy_kwargs,
                replay_buffer_class=replay_buffer_class,
                replay_buffer_kwargs=replay_buffer_kwargs if replay_buffer_class else None,
            )
            reset_timesteps = True
        else:
            model.set_env(vec_env)
            reset_timesteps = False
        # ステージ予算は「このステージで追加で走る env ステップ数」。
        # SB3 は reset_num_timesteps=False のとき total_timesteps に num_timesteps を
        # 足してから走る（_setup_learn）。つまり budget をそのまま渡せば budget 分だけ進む。
        monitor_cb = StageMonitorCallback(stage_id=stage_id, window=grad_window)
        callbacks: list[Any] = [monitor_cb]

        LOG.info(
            "Beginning training (stage %s; このステージ %d steps / 通算 %d → %d / 全体 %d)...",
            stage_id, budget, model.num_timesteps, model.num_timesteps + budget,
            total_timesteps,
        )
        model.learn(
            total_timesteps=budget,
            callback=callbacks,
            log_interval=int(sac_cfg["log_interval"]),
            reset_num_timesteps=reset_timesteps,
        )

        vec_env.close()

        if curriculum:
            completed_stages.append(stage_id)
        LOG.info(
            "Stage %s 終了 (%d steps 消化, 通算 %d)。指標: success_rate=%.2f, "
            "tower_height=%.3fm, all_placed=%d回 (直近%d: %.2f, 達成時高さ %.3fm), episodes=%d",
            stage_id, budget, model.num_timesteps, monitor_cb.success_rate,
            monitor_cb.tower_height_mean, monitor_cb.all_placed_total, grad_window,
            monitor_cb.all_placed_rate, monitor_cb.all_placed_height,
            monitor_cb.episodes_seen,
        )

    assert model is not None, "run_stages が空のため model が未作成です"
    return model, completed_stages, last_active_stage_id


def _save_end_state(
    model: SAC,
    output_dir: Path,
    last_active_stage_id: int | None,
    run_stages: list[dict[str, Any]],
    completed_stages: list[int],
    total_timesteps: int,
) -> None:
    """学習終了時に長期記憶と再開状態を永続化する。

    - replay_buffer.pkl: WeightedReplayBuffer（長期記憶）。live_server が起動時に復元する。
    - resume_state.json: num_timesteps, next_stage_id, completed_stages, timestamp。
      live_server がスナップショット引き継ぎ（経過日数の時間減衰）に利用する。
    """
    buf_save_path = output_dir / "replay_buffer.pkl"
    model.save_replay_buffer(str(buf_save_path))
    LOG.info("長期記憶を保存: %s", buf_save_path)

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
    state_out_path = output_dir / "resume_state.json"
    with state_out_path.open("w", encoding="utf-8") as f:
        json.dump(resume_out, f, indent=2, ensure_ascii=False)
    LOG.info(
        "Resume state saved: %s (next_stage=%s, completed=%s)",
        state_out_path, next_stage, completed_stages,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="block_stacker.training.train")
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument(
        "--total-timesteps", type=int, default=None,
        help="全ステージ合計の上限（安全弁）。無指定なら configs/training.yaml の "
             "sac.total_timesteps、それも null ならステージ予算の合計をそのまま使う。",
    )
    parser.add_argument("--n-envs", type=int, default=None)
    parser.add_argument("--use-subproc", action="store_true", default=True,
                        help="use SubprocVecEnv when n_envs > 1 (default on)")
    parser.add_argument("--output-dir", type=Path, default=Path("./output/training"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--curriculum", action=argparse.BooleanOptionalAction, default=True,
                        help="Stage 1→N を順に自動学習（既定 ON）。--no-curriculum で Stage 1 のみ")
    parser.add_argument("--start-stage", type=int, default=1,
                        help="--curriculum 時の開始ステージ番号（1始まり）")
    parser.add_argument("--max-stage", type=int, default=None,
                        help="最後に走るステージ番号（既定=全ステージ）")
    parser.add_argument(
        "--target-stage", type=int, default=4,
        help=(
            "最後に走るステージ番号（既定 4）。--max-stage と同義で、厳しい方が採用される。"
            "全 5 ステージ走らせるなら --target-stage 5。"
            "**卒業判定は廃止されたので「ここまで到達したら終了」ではなく単なる上限**。"
            "--no-curriculum との併用時は無視される。"
        ),
    )
    parser.add_argument(
        "--stage-steps", type=str, default=None,
        help=(
            "各ステージの学習ステップ数（固定予算）。"
            "単一値なら全ステージ一括（例: --stage-steps 20000）、"
            "カンマ区切りなら実行ステージへ順に割当（例: --stage-steps 60000,35000,40000,45000）。"
            "無指定なら configs/training.yaml の stages[].steps を使う。"
        ),
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    # 同一 run の全 checkpoint が共有するタイムスタンプ。ファイル名の先頭に埋め込む。
    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

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
    # threshold は卒業判定の撤去に伴い未使用（window=指標の移動平均幅, ratio=目標高さ係数）。
    grad_window, _grad_threshold_unused, grad_ratio = resolve_graduation(grad_cfg)

    n_envs = args.n_envs or sac_cfg.get("n_envs", 1)
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0

    # --target-stage は curriculum ON のときのみ有効。OFF なら無視してログ警告。
    effective_target: int | None = None
    if args.curriculum:
        effective_target = args.target_stage
    elif args.target_stage is not None:
        LOG.warning(
            "--target-stage=%d が指定されていますが --no-curriculum のため無視されます。",
            args.target_stage,
        )

    # 実行するステージ列を決める。--curriculum 無指定なら従来通り Stage 1 のみ（後方互換）。
    if args.curriculum:
        start_idx = max(1, args.start_stage) - 1
        # --target-stage / --max-stage はどちらも「最後に走るステージ」の上限。厳しい方を採用。
        effective_max = args.max_stage
        if effective_target is not None:
            effective_max = (
                effective_target if effective_max is None
                else min(effective_max, effective_target)
            )
        end_idx = (
            len(all_stages) if effective_max is None
            else min(effective_max, len(all_stages))
        )
        run_stages = all_stages[start_idx:end_idx]
        if not run_stages:
            raise SystemExit(
                f"no stages selected: start={args.start_stage}, "
                f"max={args.max_stage}, target={args.target_stage}"
            )
    else:
        run_stages = [all_stages[0]]

    # ステージ予算（固定ステップ制）。--stage-steps > YAML stages[].steps > 既定。
    stage_budgets = resolve_stage_budgets(args.stage_steps, run_stages)
    budget_sum = sum(stage_budgets)
    # グローバル上限。CLI > YAML(null 可)。null なら「ステージ予算の合計」をそのまま使う。
    cfg_total = sac_cfg.get("total_timesteps")
    total_timesteps = args.total_timesteps or cfg_total or budget_sum

    LOG.info("SAC (%s)",
             "curriculum" if args.curriculum else f"single-stage: {all_stages[0]['name']}")
    LOG.info("Total timesteps (全ステージ合計の上限): %d, n_envs: %d, subproc=%s, stm_length=%d",
             total_timesteps, n_envs, args.use_subproc, stm_length)
    LOG.info("Memory system enabled: %s", mem_cfg.get("enabled", False))
    if args.curriculum:
        LOG.info(
            "Curriculum: 固定ステップ制（卒業判定なし）。ステージ予算 %s (合計 %d)",
            {s.get("id"): b for s, b in zip(run_stages, stage_budgets, strict=True)},
            budget_sum,
        )
        if budget_sum > total_timesteps:
            LOG.warning(
                "ステージ予算の合計 %d がグローバル上限 %d を超えています。"
                "後半のステージが短縮されます。",
                budget_sum, total_timesteps,
            )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _retire_fresh_checkpoints(args.output_dir / "fresh", args.output_dir / "played")

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

    # 定期 checkpoint は保存しない。1 回の学習で fresh/ に残るのは
    # 「全ステージ走破後のプリセット 1 本」だけ（ループ後で明示保存する）。
    # ファイル名: fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip
    #   → run_ts プレフィックスで played/ 蓄積時も衝突しない。
    LOG.info("Model prefix: sac_%s（保存は全ステージ走破後のプリセット 1 本のみ）", run_ts)

    completed_stages: list[int] = []
    model: SAC | None = None

    model, completed_stages, last_active_stage_id = _run_stage_loop(
        model=model,
        run_stages=run_stages,
        stage_budgets=stage_budgets,
        build_vec_env=build_vec_env,
        total_timesteps=total_timesteps,
        sac_cfg=sac_cfg,
        curriculum=args.curriculum,
        grad_window=grad_window,
        grad_ratio=grad_ratio,
        completed_stages=completed_stages,
        policy_kwargs=policy_kwargs,
        replay_buffer_class=replay_buffer_class,
        replay_buffer_kwargs=replay_buffer_kwargs,
        seed=args.seed,
        world_cfg=world_cfg,
        output_dir=args.output_dir,
    )

    # 全ステージ走り切った時点のモデルをプリセットとして保存する。
    # **これが 1 回の学習で fresh/ に残る唯一のモデル**（定期 checkpoint は撤去済み）。
    preset_name = f"sac_{run_ts}_{model.num_timesteps}_steps"
    preset_path = args.output_dir / "fresh" / preset_name
    model.save(str(preset_path))
    LOG.info(
        "全ステージ終了: プリセット保存 → %s.zip (%d steps, 実行ステージ %s)",
        preset_path, model.num_timesteps, completed_stages or "-",
    )

    LOG.info("学習完了: 最終モデル = fresh/ の最大ステップ checkpoint (sac_final.zip は廃止)")
    _save_end_state(
        model=model,
        output_dir=args.output_dir,
        last_active_stage_id=last_active_stage_id,
        run_stages=run_stages,
        completed_stages=completed_stages,
        total_timesteps=total_timesteps,
    )


if __name__ == "__main__":
    mp.freeze_support()
    main()

"""Live streaming server -- training and serving fused.

1x real-time WebSocket demo (PhysicsBroadcaster) combined with background SAC
training so the AI visibly grows while the audience watches.

Architecture:
    Main thread (asyncio)
        PhysicsBroadcaster  240 Hz PyBullet + WebSocket broadcast
        ai_driver_task      serve_model.predict() -> block transport
        StreamingServer     WebSocket handshake + broadcast
    Background thread (Step B+, active when --n-envs > 0)
        SAC train_model.learn()  separate SubprocVecEnv (independent PyBullet)
        LiveCallback             stops on stop_event; pushes weights every sync_every steps
        WeightSyncer             Lock-guarded state_dict copy to serve_model

Session lifecycle (8 h):
    1. Load snapshot from --snapshot-dir (NN weights + replay_buffer + resume_state).
    2. Apply time-decay to long-term memory proportional to elapsed wall time.
    3. Stream 1x demo while training runs in background.
    4. After --duration seconds: stop_event -> join training thread
       -> save snapshot [Step D+] -> exit.

Run:
    .venv/Scripts/python.exe -m block_stacker.serving.live_server ^
        --snapshot-dir output/training --duration 60 --n-envs 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from stable_baselines3 import SAC
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from block_stacker.config import (
    PhysicsConfig,
    RewardConfig,
    WorldConfig,
    default_configs_dir,
)
from block_stacker.env.env import inventory_full_stack_height
from block_stacker.policy.weighted_replay_buffer import WeightedReplayBuffer
from block_stacker.serving.ai_server import (
    CarrierDriver,
    ShortTermMemory,
    ai_driver_task,
    spawn_stage_blocks,
)
from block_stacker.serving.runtime import setup_streaming_runtime
from block_stacker.sim.world import setup_world
from block_stacker.streaming.broadcaster import PhysicsBroadcaster
from block_stacker.training.checkpoint import find_latest_checkpoint
from block_stacker.training.curriculum import resolve_graduation, stage_inventory
from block_stacker.training.train import _compute_elapsed_steps, make_factory

LOG = logging.getLogger("serving.live")


# ----------------------------------------------------------------- weight sync


class WeightSyncer:
    """Thread-safe bridge: training thread pushes weights; asyncio thread pulls them.

    push() clones the state_dict under no lock (clone is safe from the training
    thread since train_model is not shared).  The cloned dict is written to
    self._pending under the lock (pointer swap, nanoseconds).  pull() swaps
    _pending → None under the lock then applies it without holding the lock,
    so the 240 Hz physics loop is never blocked for more than a pointer swap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self.sync_count = 0

    def push(self, model: SAC) -> None:
        """Called from training thread: clone weights into pending slot."""
        state = {k: v.cpu().clone() for k, v in model.policy.state_dict().items()}
        with self._lock:
            self._pending = state
            self.sync_count += 1
        LOG.debug("WeightSyncer.push: queued sync #%d", self.sync_count)

    def pull(self, model: SAC) -> bool:
        """Called from asyncio/physics thread: apply pending weights if any."""
        with self._lock:
            if self._pending is None:
                return False
            state = self._pending
            self._pending = None
        model.policy.load_state_dict(state)
        LOG.info("WeightSyncer.pull: applied sync #%d", self.sync_count)
        return True


# ----------------------------------------------------------------- callback


class LiveCallback(BaseCallback):
    """Monitors stop_event and pushes weights to WeightSyncer every sync_every steps.

    _on_step returns False when stop_event is set, which causes SB3 learn() to
    terminate cleanly (saves the model state so it can be inspected).
    """

    def __init__(
        self,
        stop_event: threading.Event,
        syncer: WeightSyncer,
        sync_every: int,
    ) -> None:
        super().__init__(verbose=0)
        self._stop_event = stop_event
        self._syncer = syncer
        self._sync_every = sync_every
        self._steps_since_sync = 0

    def _on_step(self) -> bool:
        self._steps_since_sync += 1
        if self._steps_since_sync >= self._sync_every:
            self._syncer.push(self.model)
            self._steps_since_sync = 0
        return not self._stop_event.is_set()

    def _on_training_end(self) -> None:
        self._syncer.push(self.model)


# ----------------------------------------------------------------- training setup


def _build_training_vec_env(
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    reward_cfg: RewardConfig,
    training_cfg: dict[str, Any],
    stage: dict[str, Any],
    n_envs: int,
) -> DummyVecEnv | SubprocVecEnv:
    """Build training VecEnv for final stage (runs in background thread)."""
    obs_cfg = training_cfg["observation"]
    episode_cfg = training_cfg["episode"]
    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0
    _, _, grad_ratio = resolve_graduation(
        training_cfg.get("curriculum", {}).get("graduation", {})
    )
    factory = make_factory(
        world_cfg=world_cfg,
        physics_cfg=physics_cfg,
        reward_cfg=reward_cfg,
        max_steps=int(episode_cfg["max_steps"]),
        max_blocks=int(obs_cfg["max_blocks"]),
        inventory=stage_inventory(stage, world_cfg),
        stage_h_high=float(stage["h_high"]),
        stage_h_low=float(stage["h_low"]),
        target_height_ratio=grad_ratio,
        max_actions_without_progress=int(episode_cfg.get("max_actions_without_progress", 10)),
        heightmap_resolution=int(obs_cfg.get("heightmap_resolution", 32)),
        stm_length=stm_length,
    )
    if n_envs > 1:
        return SubprocVecEnv([factory] * n_envs, start_method="spawn")
    return DummyVecEnv([factory])


def _make_train_model(
    model_path: Path,
    training_cfg: dict[str, Any],
) -> SAC:
    """Load train_model from checkpoint with WeightedReplayBuffer if enabled in config."""
    mem_cfg = training_cfg.get("memory_system", {})
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
    custom_objects: dict[str, Any] = {}
    if replay_buffer_class is not None:
        custom_objects["replay_buffer_class"] = replay_buffer_class
        custom_objects["replay_buffer_kwargs"] = replay_buffer_kwargs
    return SAC.load(str(model_path), custom_objects=custom_objects or None)


def _apply_live_resume(
    train_model: SAC,
    snapshot_dir: Path,
    resume_cfg: dict[str, Any],
) -> None:
    """Load replay_buffer.pkl into train_model and apply time-decay if --no-resume not set.

    Mirrors the long-term-memory restore in train.py _apply_resume(), but without
    the NN-weight and curriculum-progress steps (those are handled separately in live mode).
    """
    state_path = snapshot_dir / "resume_state.json"
    resume_state: dict[str, Any] = {}
    if state_path.exists():
        with state_path.open("r", encoding="utf-8-sig") as f:
            resume_state = json.load(f)
        LOG.info(
            "[train] resume_state: num_timesteps=%d, timestamp=%s",
            resume_state.get("num_timesteps", "?"),
            resume_state.get("timestamp", "?"),
        )

    buf_path = snapshot_dir / "replay_buffer.pkl"
    if buf_path.exists():
        LOG.info("[train] loading replay buffer from %s", buf_path)
        train_model.load_replay_buffer(str(buf_path))
        if isinstance(train_model.replay_buffer, WeightedReplayBuffer):
            elapsed = _compute_elapsed_steps(resume_cfg, resume_state)
            if elapsed > 0:
                old_gs = train_model.replay_buffer.global_step
                train_model.replay_buffer.global_step += elapsed
                LOG.info(
                    "[train] time-decay: +%d steps to global_step (%d → %d, "
                    "decay^%d ≈ %.4f)",
                    elapsed, old_gs, train_model.replay_buffer.global_step,
                    elapsed, train_model.replay_buffer.decay_rate ** min(elapsed, 50000),
                )
            else:
                LOG.info("[train] time-decay: 0 elapsed steps, no decay applied")
        else:
            LOG.info("[train] standard ReplayBuffer, time-decay skipped")
    else:
        LOG.info("[train] replay_buffer.pkl not found, training starts fresh (%s)", buf_path)


def _save_live_snapshot(
    train_model: SAC,
    snapshot_dir: Path,
    stage_id: int,
) -> None:
    """Persist NN weights + long-term memory + resume cursor after a live session.

    checkpoint ZIP → fresh/sac_<YYYYMMDD-HHMMSS>_<steps>_steps.zip
    replay_buffer.pkl and resume_state.json → snapshot_dir/ (same as train.py)
    """
    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    steps = int(train_model.num_timesteps)

    fresh_dir = snapshot_dir / "fresh"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    ckpt_stem = f"sac_{run_ts}_{steps}_steps"
    train_model.save(str(fresh_dir / ckpt_stem))
    LOG.info("[train] checkpoint saved: %s/%s.zip", fresh_dir, ckpt_stem)

    buf_path = snapshot_dir / "replay_buffer.pkl"
    train_model.save_replay_buffer(str(buf_path))
    LOG.info("[train] replay buffer saved: %s", buf_path)

    resume_out: dict[str, Any] = {
        "num_timesteps": steps,
        "total_timesteps": steps,
        "buffer_global_step": int(getattr(train_model.replay_buffer, "global_step", 0)),
        "next_stage_id": stage_id,
        "completed_stages": [],
        "timestamp": datetime.now().isoformat(),
    }
    state_path = snapshot_dir / "resume_state.json"
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(resume_out, f, indent=2, ensure_ascii=False)
    LOG.info(
        "[train] resume_state saved: %s (stage=%s, steps=%d)", state_path, stage_id, steps,
    )


def _self_stop_instance(reason: str) -> None:
    """Stub: request the EC2 instance to stop itself at end of session.

    Implement by calling the EC2 metadata service:
        import requests
        token = requests.put("http://169.254.169.254/latest/api/token", ...).text
        requests.put("http://169.254.169.254/latest/meta-data/spot/instance-action",
                     headers={"X-aws-ec2-metadata-token": token})
    Or via boto3: ec2.stop_instances(InstanceIds=[instance_id]).
    """
    LOG.info("_self_stop_instance: reason=%r (stub -- implement for EC2 deploy)", reason)


def _training_thread(
    model_path: Path,
    training_cfg: dict[str, Any],
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    reward_cfg: RewardConfig,
    stage: dict[str, Any],
    stage_id: int,
    n_envs: int,
    stop_event: threading.Event,
    syncer: WeightSyncer,
    sync_every: int,
    snapshot_dir: Path,
    no_resume: bool,
) -> None:
    """Background thread: build env, load train_model, run SAC.learn() until stop_event.

    Saves a snapshot (checkpoint + replay buffer + resume state) to snapshot_dir on exit.
    """
    train_model: SAC | None = None
    vec_env = None
    try:
        LOG.info("[train] building VecEnv (n_envs=%d) for final stage", n_envs)
        vec_env = _build_training_vec_env(
            world_cfg, physics_cfg, reward_cfg, training_cfg, stage, n_envs,
        )
        train_model = _make_train_model(model_path, training_cfg)
        if not no_resume:
            _apply_live_resume(
                train_model, snapshot_dir, training_cfg.get("resume", {}),
            )
        train_model.set_env(vec_env)
        LOG.info(
            "[train] starting SAC.learn() background (n_params=%d)",
            sum(p.numel() for p in train_model.policy.parameters()),
        )
        sac_cfg = training_cfg.get("sac", {})
        live_cb = LiveCallback(stop_event, syncer, sync_every)
        train_model.learn(
            total_timesteps=int(1e9),
            callback=[live_cb],
            log_interval=int(sac_cfg.get("log_interval", 4)),
            reset_num_timesteps=True,
        )
    except Exception:
        LOG.exception("[train] background training thread crashed")
    finally:
        if train_model is not None:
            try:
                _save_live_snapshot(train_model, snapshot_dir, stage_id)
            except Exception:
                LOG.exception("[train] snapshot save failed")
        if vec_env is not None:
            try:
                vec_env.close()
            except Exception:
                pass
        LOG.info("[train] training thread exited")


# ----------------------------------------------------------------- model load


def _resolve_model(snapshot_dir: Path, explicit: Path | None) -> Path:
    """Return model path: explicit arg > latest checkpoint in snapshot_dir."""
    if explicit is not None:
        return explicit
    found = find_latest_checkpoint(snapshot_dir)
    if found is not None:
        return found
    raise SystemExit(
        f"スナップショットが見つかりません: {snapshot_dir}\n"
        "初回は --model で明示するか、プリセット生成手順(docs/live_mode.md)を参照してください。"
    )


# ----------------------------------------------------------------- async main


async def main_async(args: argparse.Namespace) -> None:
    world_cfg = WorldConfig.from_yaml(args.configs_dir / "world.yaml")
    physics_cfg = PhysicsConfig.from_yaml(args.configs_dir / "physics.yaml")
    reward_cfg = RewardConfig.from_yaml(args.configs_dir / "reward.yaml")

    with (args.configs_dir / "training.yaml").open("r", encoding="utf-8") as f:
        training_cfg: dict[str, Any] = yaml.safe_load(f)

    # Live mode always uses the final curriculum stage (全形状・最難).
    stages = training_cfg["curriculum"]["stages"]
    stage = stages[-1]
    inventory = stage_inventory(stage, world_cfg)
    h_high = float(stage["h_high"])
    h_low = float(stage["h_low"])
    _, _, grad_ratio = resolve_graduation(
        training_cfg.get("curriculum", {}).get("graduation", {})
    )
    target_h = inventory_full_stack_height(inventory, world_cfg.shapes) * grad_ratio
    LOG.info(
        "live stage: id=%s '%s' inventory=%s target_h=%.3f h_high=%.3f h_low=%.3f",
        stage.get("id"), stage.get("name", ""), inventory, target_h, h_high, h_low,
    )

    model_path = _resolve_model(args.snapshot_dir, args.model)
    LOG.info("loading serve_model: %s", model_path)
    serve_model = SAC.load(str(model_path))
    LOG.info(
        "serve_model loaded: n_params=%d",
        sum(p.numel() for p in serve_model.policy.parameters()),
    )

    stm_cfg = training_cfg.get("short_term_memory", {})
    stm_length = int(stm_cfg.get("length", 0)) if stm_cfg.get("enabled", False) else 0
    stm = ShortTermMemory(length=stm_length)

    LOG.info("setting up serving world")
    world = setup_world(world_cfg, physics_cfg, gui=False)
    blocks = spawn_stage_blocks(world_cfg, physics_cfg, inventory)
    LOG.info("spawned %d blocks for final stage", len(blocks))
    world.step(60)  # initial settle

    server, tracker, t_start = setup_streaming_runtime(
        world_cfg=world_cfg, physics_cfg=physics_cfg,
        blocks=blocks, host=args.host, port=args.port,
    )

    shape_order = list(world_cfg.shapes.keys())
    shape_index = {n: i for i, n in enumerate(shape_order)}

    # --- Step B: WeightSyncer + background training thread ---------------
    stop_event = threading.Event()
    syncer: WeightSyncer | None = None
    train_thread: threading.Thread | None = None

    if args.n_envs > 0:
        syncer = WeightSyncer()
        train_thread = threading.Thread(
            target=_training_thread,
            name="live-train",
            daemon=True,
            kwargs=dict(
                model_path=model_path,
                training_cfg=training_cfg,
                world_cfg=world_cfg,
                physics_cfg=physics_cfg,
                reward_cfg=reward_cfg,
                stage=stage,
                stage_id=int(stage.get("id", len(stages))),
                n_envs=args.n_envs,
                stop_event=stop_event,
                syncer=syncer,
                sync_every=args.sync_every,
                snapshot_dir=args.snapshot_dir,
                no_resume=args.no_resume,
            ),
        )
        train_thread.start()
        LOG.info("[train] background thread started (n_envs=%d, sync_every=%d)",
                 args.n_envs, args.sync_every)
    else:
        LOG.info("--n-envs=0: background training disabled (serving only)")

    # --- pre_step: drive carrier + pull synced weights at 240 Hz -----------
    driver = CarrierDriver()
    _syncer = syncer  # local alias for closure

    def _pre_step(step: int, ts: float) -> None:
        driver.tick(step, ts)
        if _syncer is not None:
            _syncer.pull(serve_model)

    broadcaster = PhysicsBroadcaster(
        world=world, tracker=tracker, server=server,
        body_ids=[b.body_id for b in blocks], t_start=t_start,
    )

    physics_task = asyncio.create_task(broadcaster.run(pre_step=_pre_step))
    ai_task = asyncio.create_task(ai_driver_task(
        blocks=blocks, world=world,
        world_cfg=world_cfg, physics_cfg=physics_cfg,
        shape_index=shape_index, n_shapes=len(shape_order),
        max_blocks=args.max_blocks,
        heightmap_resolution=args.heightmap_resolution,
        server=server, broadcaster=broadcaster, driver=driver,
        model=serve_model,
        h_high=h_high, h_low=h_low,
        thinking_pause=args.thinking_pause,
        settle_seconds=args.settle_seconds,
        stm=stm,
    ))
    serve_task = asyncio.create_task(server.serve_forever())

    try:
        if args.duration > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(physics_task, ai_task, serve_task),
                    timeout=args.duration,
                )
            except TimeoutError:
                LOG.info("--duration %.0fs elapsed, shutting down", args.duration)
        else:
            await asyncio.gather(physics_task, ai_task, serve_task)
    finally:
        stop_event.set()
        if train_thread is not None and train_thread.is_alive():
            LOG.info("waiting for training thread to stop (timeout 15 s)...")
            train_thread.join(timeout=15.0)
            if train_thread.is_alive():
                LOG.warning("training thread did not stop within 15 s")
        world.disconnect()
        LOG.info("world disconnected")
        _self_stop_instance(reason="duration elapsed")


# ----------------------------------------------------------------- entry point


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="block_stacker.serving.live_server",
        description="ライブ配信モード: 1x配信 + バックグラウンド学習の融合サーバ",
    )
    # --- snapshot / model ---
    parser.add_argument(
        "--snapshot-dir", type=Path, default=Path("output/training"),
        help="スナップショット（NN + replay_buffer + resume_state）の読み書き先",
    )
    parser.add_argument(
        "--model", type=Path, default=None,
        help="推論モデルを明示（無指定なら --snapshot-dir から自動選択）",
    )
    # --- network ---
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    # --- configs ---
    parser.add_argument("--configs-dir", type=Path, default=default_configs_dir())
    parser.add_argument("--max-blocks", type=int, default=8)
    parser.add_argument("--heightmap-resolution", type=int, default=32)
    # --- demo pacing ---
    parser.add_argument("--thinking-pause", type=float, default=2.0,
                        help="行動選択後の演出待機秒数")
    parser.add_argument("--settle-seconds", type=float, default=1.5,
                        help="ブロック落下後の静定待機秒数")
    parser.add_argument("--duration", type=float, default=28800.0,
                        help="配信秒数 (default: 28800 = 8h)。0 = 無制限")
    # --- training ---
    parser.add_argument("--n-envs", type=int, default=4,
                        help="バックグラウンド学習の並列環境数 (0 = 配信のみ)")
    parser.add_argument("--sync-every", type=int, default=500,
                        help="学習→配信モデルへの重み同期間隔（学習ステップ単位）")
    # --- resume ---
    parser.add_argument("--no-resume", action="store_true", default=False,
                        help="スナップショットを無視して新規学習（初回起動専用）")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()

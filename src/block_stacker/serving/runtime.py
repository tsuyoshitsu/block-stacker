"""Shared streaming-server boilerplate for demo_server and ai_server.

Both `demo_server` and `ai_server` follow the same setup sequence:

    1. Build a SleepTracker over the spawned blocks.
    2. Build the WORLD_CONFIG payload (static block descriptors + work area).
    3. Build the INITIAL_STATE provider closure for new-client handshakes.
    4. Start a StreamingServer with the above wired in.

This module exposes one function that returns the assembled
`(server, tracker, t_start)` so callers can stop repeating themselves.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    demo_server と ai_server の共通サーバ初期化を集約。
    handshake 用 world_config と initial_state provider をビルドして
    StreamingServer に紐付ける。

設計上のポイント:
    - initial_state_provider は closure で blocks/tracker を捕獲する。
      クライアントが接続するたびに最新の AWAKE 状態と現ポーズが返る。
    - t_start = time.monotonic() を返すのは、broadcaster や AI driver で
      now_ts() を計算するときの基準にするため。

レビューで見る観点:
    - blocks の生成は呼び出し側の責務（spawn 戦略がデモ/AI で違うため）。
      この関数は「すでに spawn 済の blocks」を受け取る純粋なセットアップ。
"""
from __future__ import annotations

import time

from block_stacker.config import PhysicsConfig, WorldConfig
from block_stacker.sim.blocks import Block
from block_stacker.streaming.protocol import (
    pack_initial_state,
    pack_world_config,
    pose_from_body,
)
from block_stacker.streaming.server import StreamingServer
from block_stacker.streaming.sleep_tracker import SleepTracker


def block_info_payload(blocks: list[Block]) -> list[dict]:
    """Static per-block info for the WORLD_CONFIG JSON."""
    return [
        {
            "id": b.body_id,
            "shape": b.shape.name,
            "type": b.shape.type,
            "dims": list(b.shape.dims),
            "color": list(b.shape.color),
        }
        for b in blocks
    ]


def build_world_config_dict(world_cfg: WorldConfig, blocks: list[Block]) -> dict:
    return {
        "work_area": {
            "x_range": list(world_cfg.x_range),
            "y_range": list(world_cfg.y_range),
            "z_max": world_cfg.z_max,
        },
        "ground": {"size": list(world_cfg.ground_size)},
        "boundary": {
            "type": world_cfg.boundary_type,
            "height": world_cfg.boundary_height,
        },
        "blocks": block_info_payload(blocks),
        "protocol_version": 1,
    }


def setup_streaming_runtime(
    world_cfg: WorldConfig,
    physics_cfg: PhysicsConfig,
    blocks: list[Block],
    host: str,
    port: int,
) -> tuple[StreamingServer, SleepTracker, float]:
    """Build and wire up the tracker + server. Returns (server, tracker, t_start)."""
    tracker = SleepTracker(
        lin_threshold=physics_cfg.sleep_lin_vel,
        ang_threshold=physics_cfg.sleep_ang_vel,
        stable_frames=physics_cfg.sleep_stable_frames,
    )
    tracker.register([b.body_id for b in blocks], awake=True)

    server = StreamingServer(host=host, port=port)
    server.set_world_config(pack_world_config(build_world_config_dict(world_cfg, blocks)))

    t_start = time.monotonic()

    def initial_state_provider() -> bytes:
        rows = [
            (b.body_id, tracker.is_awake(b.body_id), pose_from_body(b.body_id))
            for b in blocks
        ]
        return pack_initial_state(time.monotonic() - t_start, rows)

    server.set_initial_state_provider(initial_state_provider)
    return server, tracker, t_start

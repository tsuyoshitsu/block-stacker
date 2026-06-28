"""Tiny WebSocket client that connects to the MVP 3 demo server and prints
a running summary of received messages.

Run (after starting demo_server):
    uv run python -m block_stacker.serving.test_client
    uv run python -m block_stacker.serving.test_client --uri ws://localhost:8765 --seconds 10

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    プロトコル正常性検証用の最小クライアント。Godot 実装が動かない環境でも
    サーバが期待通り配信しているかを CLI で確認できる。

設計上のポイント:
    - 受信メッセージを type 別にカウント → 終了時にサマリ出力。
      60Hz × N 秒 = 期待 SNAPSHOT 数 を目視確認する。
    - max_block_z を追跡: AI が変な場所にブロックを送り出して飛ばしていないか
      （壁を超えた z > 1.0 などのバグ検出）。
    - hello ペイロードを送って server.handler の hello 待ちタイムアウトを
      skip する → 高速 handshake。

レビューで見る観点:
    - parse_message で raise されたら try/except 内で警告のみ。
    - 通信切断で websockets.ConnectionClosed を握り潰す → サマリは表示される。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter

import websockets

from block_stacker.streaming.protocol import MsgType, parse_message

LOG = logging.getLogger("serving.client")


async def run_client(uri: str, duration: float) -> None:
    LOG.info("connecting to %s", uri)
    async with websockets.connect(uri) as ws:
        # Send a trivial hello so the server can move past the handshake.
        await ws.send(b'{"client_version":"test-client"}')

        counts: Counter[MsgType] = Counter()
        latest_ts: dict[MsgType, float] = {}
        first_world_config: dict | None = None
        first_initial_state: dict | None = None
        last_snapshot_summary: str = ""
        sleep_ids: list[int] = []
        wake_ids: list[int] = []
        max_block_z: float = 0.0
        max_block_z_id: int = -1

        t_start = time.monotonic()
        try:
            while time.monotonic() - t_start < duration:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=duration)
                except TimeoutError:
                    break
                if isinstance(data, str):
                    LOG.warning("unexpected text frame: %s", data[:80])
                    continue
                msg_type, payload = parse_message(data)
                counts[msg_type] += 1
                if "timestamp" in payload:
                    latest_ts[msg_type] = payload["timestamp"]

                if msg_type == MsgType.WORLD_CONFIG and first_world_config is None:
                    first_world_config = payload["config"]
                    LOG.info("WORLD_CONFIG: %d blocks declared, work_area=%s",
                             len(first_world_config["blocks"]),
                             first_world_config["work_area"])
                elif msg_type == MsgType.INITIAL_STATE and first_initial_state is None:
                    first_initial_state = payload
                    n_awake = sum(1 for b in payload["blocks"] if b["awake"])
                    LOG.info("INITIAL_STATE: %d blocks (%d awake) at t=%.3f",
                             len(payload["blocks"]), n_awake, payload["timestamp"])
                elif msg_type == MsgType.SNAPSHOT:
                    last_snapshot_summary = (
                        f"snapshot seq={payload['seq']} t={payload['timestamp']:.3f} "
                        f"awake={len(payload['blocks'])}"
                    )
                    for b in payload["blocks"]:
                        z = b["pose"][2]
                        if z > max_block_z:
                            max_block_z = z
                            max_block_z_id = b["id"]
                elif msg_type == MsgType.SLEEP_EVENT:
                    sleep_ids.append(payload["id"])
                elif msg_type == MsgType.WAKE_EVENT:
                    wake_ids.append(payload["id"])
                elif msg_type == MsgType.HEARTBEAT:
                    LOG.info("HEARTBEAT t=%.3f", payload["timestamp"])
                elif msg_type == MsgType.COLLAPSE_EVENT:
                    LOG.info("COLLAPSE_EVENT t=%.3f", payload["timestamp"])
        except websockets.ConnectionClosed as e:
            LOG.info("connection closed: %s", e)

        LOG.info("--- summary after %.1fs ---", time.monotonic() - t_start)
        for msg_type, n in counts.most_common():
            LOG.info("  %-20s %d", msg_type.name, n)
        if last_snapshot_summary:
            LOG.info("  last %s", last_snapshot_summary)
        LOG.info("  sleep events: %d  (ids=%s)", len(sleep_ids), sleep_ids[:8])
        LOG.info("  wake events:  %d  (ids=%s)", len(wake_ids), wake_ids[:8])
        LOG.info("  max block z observed: %.4f (id=%d)", max_block_z, max_block_z_id)


def main() -> None:
    parser = argparse.ArgumentParser(prog="block_stacker.serving.test_client")
    parser.add_argument("--uri", default="ws://localhost:8765")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    try:
        asyncio.run(run_client(args.uri, args.seconds))
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()

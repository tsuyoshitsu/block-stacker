"""Asyncio WebSocket server.

Push-only: every connected client receives the broadcast stream after the
initial handshake (`hello → world_config → initial_state`). The server
does not interpret client messages.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    WebSocket クライアント接続管理と全クライアントへの fan-out 配信。
    自身は物理状態を持たず、外部から渡された world_config bytes と
    initial_state provider を使ってハンドシェイクを返すだけ。

設計上のポイント:
    - クライアント切断検知: broadcast の asyncio.gather に return_exceptions=True を
      指定し、send で例外を出したクライアントを次回ループで discard する。
      → 一クライアントの切断が他クライアントの配信を止めない。
    - hello はオプション扱い（hello_timeout_s=2.0 でタイムアウト許容）。
      Godot 側の WebSocketPeer は STATE_OPEN になる前に send を予約できるので
      実用上は届くが、届かない場合もハンドシェイク続行。
    - server.serve_forever で await asyncio.Future() → SIGINT まで止まらない。

レビューで見る観点:
    - num_clients が大きくなった時の broadcast コスト。15 クライアントまでは
      問題ないが、それ以上は SFU 化検討（設計書 §2 参照）。
    - 同じメッセージを N 回 send している。zero-copy 化の余地はあるが、
      websockets ライブラリの API はクライアントごとの send 必須なので現状維持。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import websockets
from websockets.asyncio.server import ServerConnection, serve

LOG = logging.getLogger("streaming.server")


class StreamingServer:
    """Stateless WebSocket fan-out.

    Holds the world_config bytes and a callable that returns the current
    initial_state bytes. Both are sent to each new client at connect time;
    after that the client only receives broadcasts produced elsewhere.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._clients: set[ServerConnection] = set()
        self._world_config: bytes = b""
        self._initial_state_provider: Callable[[], bytes] | None = None
        self._hello_timeout_s: float = 2.0

    # ---- setup ------------------------------------------------------------

    def set_world_config(self, blob: bytes) -> None:
        self._world_config = blob

    def set_initial_state_provider(self, fn: Callable[[], bytes]) -> None:
        self._initial_state_provider = fn

    def num_clients(self) -> int:
        return len(self._clients)

    # ---- broadcast --------------------------------------------------------

    async def broadcast(self, msg: bytes) -> None:
        if not self._clients:
            return
        # Use gather with return_exceptions so a single dead client doesn't
        # break the broadcast for everyone else.
        results = await asyncio.gather(
            *(client.send(msg) for client in self._clients),
            return_exceptions=True,
        )
        dead: list[ServerConnection] = []
        for client, result in zip(list(self._clients), results, strict=False):
            if isinstance(result, Exception):
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    # ---- handler ----------------------------------------------------------

    async def _handler(self, websocket: ServerConnection) -> None:
        addr = websocket.remote_address
        LOG.info("client connected from %s", addr)
        self._clients.add(websocket)
        try:
            # Step 1: optional hello (ignored content for MVP 3).
            try:
                await asyncio.wait_for(websocket.recv(), timeout=self._hello_timeout_s)
            except (TimeoutError, websockets.ConnectionClosed):
                pass

            # Step 2: world_config
            if self._world_config:
                await websocket.send(self._world_config)
            # Step 3: initial_state
            if self._initial_state_provider is not None:
                await websocket.send(self._initial_state_provider())

            # Step 4: keep open; ignore further inbound messages.
            try:
                async for _ in websocket:
                    pass
            except websockets.ConnectionClosed:
                pass
        finally:
            self._clients.discard(websocket)
            LOG.info("client disconnected: %s", addr)

    # ---- run --------------------------------------------------------------

    async def serve_forever(self) -> None:
        async with serve(self._handler, self.host, self.port):
            LOG.info("WebSocket server listening on ws://%s:%d", self.host, self.port)
            await asyncio.Future()  # block forever

"""Shared physics + broadcast loop.

Both the MVP 3 (no-AI) and MVP 3.5 (AI-driven) servers run the same
real-time PyBullet loop:

    1. (optional) caller-supplied pre-step hook  ← used by AI driver for carrier
    2. world.step()
    3. SleepTracker.step → emit sleep/wake events
    4. Every `snapshot_div` frames: emit a 60 Hz snapshot of AWAKE bodies
    5. Every `heartbeat_div` frames: emit a 1 Hz heartbeat
    6. Sleep until the next tick to keep simulated time = wall time

This module factors that loop into one place so the demo and AI servers
just configure it and provide their per-step hook.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    PyBullet の物理ループ（240Hz）+ プロトコルブロードキャストの共通実装。
    MVP 3（無 AI）と MVP 3.5（AI 駆動）の両方で同一インスタンスを使う。

設計上のポイント:
    - body_ids はセッション不変。240Hz ループ内で毎回 [b.body_id for b in blocks]
      を再構築すると GC 圧が無駄なので __init__ で一度キャッシュ。
    - pre_step フックで「AI が次ウェイポイントをキャリアに渡す」処理を注入。
      非同期 (Awaitable) も同期コールバックも受け付ける（asyncio.iscoroutine で判定）。
    - スナップショット送出間隔 (snapshot_div) は internal_rate_hz / snapshot_hz。
      設計値 240/60 = 4 ステップに 1 回。
    - 実時間ペーシング: next_tick を dt ずつ進め、await asyncio.sleep(残り) で
      壁時計と物理時計を一致させる。フレームが間に合わない場合は sleep(0) で
      他タスクに譲り、累積誤差は加算式 next_tick で消化する。

レビューで見る観点:
    - SleepTracker.step が毎フレーム呼ばれる前提か？（呼ばれる）
    - run の中で例外が出た時の clean-up は？（現状なし、asyncio.gather 親が拾う）
    - per-step フックが時間のかかる処理をしたらフレーム drop するが、現実装の
      driver.tick は 1 next(iterator) 呼び出しのみで軽量。

関連:
    - 呼び出し元: mvp3/demo_server.py、mvp3/ai_server.py
    - 依存: streaming/sleep_tracker.py、streaming/server.py、streaming/protocol.py
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from block_stacker.sim.world import World
from block_stacker.streaming.protocol import (
    pack_heartbeat,
    pack_snapshot,
    pack_sleep_event,
    pack_wake_event,
    pose_from_body,
)
from block_stacker.streaming.server import StreamingServer
from block_stacker.streaming.sleep_tracker import SleepTracker

PreStepHook = Callable[[int, float], Awaitable[None] | None]


class PhysicsBroadcaster:
    """Tick PyBullet at the internal rate, broadcast state, allow a per-step hook."""

    def __init__(
        self,
        world: World,
        tracker: SleepTracker,
        server: StreamingServer,
        body_ids: list[int],
        t_start: float,
        snapshot_hz: int = 60,
        heartbeat_hz: int = 1,
    ) -> None:
        self.world = world
        self.tracker = tracker
        self.server = server
        # The body id list is invariant for the session — cache it once so the
        # 240 Hz hot loop doesn't rebuild it every frame.
        self.body_ids = list(body_ids)
        self.t_start = t_start
        rate = world.config_physics.internal_rate_hz
        self.rate = rate
        self.snapshot_div = max(1, rate // snapshot_hz)
        self.heartbeat_div = max(1, rate // heartbeat_hz)

    def now_ts(self) -> float:
        return time.monotonic() - self.t_start

    async def run(self, pre_step: PreStepHook | None = None) -> None:
        dt = 1.0 / self.rate
        seq = 0
        step = 0
        next_tick = time.monotonic()

        while True:
            if pre_step is not None:
                result = pre_step(step, self.now_ts())
                if asyncio.iscoroutine(result):
                    await result

            self.world.step()
            step += 1
            ts = self.now_ts()

            sleeps, wakes = self.tracker.step(self.body_ids)
            for bid, final_pose in sleeps:
                await self.server.broadcast(pack_sleep_event(ts, bid, final_pose))
            for bid in wakes:
                await self.server.broadcast(pack_wake_event(ts, bid))

            if step % self.snapshot_div == 0:
                awake = self.tracker.awake_ids()
                rows = [(bid, pose_from_body(bid)) for bid in self.body_ids if bid in awake]
                await self.server.broadcast(pack_snapshot(ts, seq, rows))
                seq += 1

            if step % self.heartbeat_div == 0:
                await self.server.broadcast(pack_heartbeat(ts))

            next_tick += dt
            sleep_amt = next_tick - time.monotonic()
            if sleep_amt > 0:
                await asyncio.sleep(sleep_amt)
            else:
                await asyncio.sleep(0)

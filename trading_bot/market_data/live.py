"""Bounded public WebSocket lifecycle, resubscription and REST gap recovery."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from .feed import FeedProcessor
from .models import DataError, UTC
from .rest import HistoricalClient, RetryPolicy, TransportError

LOG = logging.getLogger(__name__)
WS_URL = "wss://advanced-trade-ws.coinbase.com"


class FeedFailure(RuntimeError):
    pass


def websocket_connection(url):
    # Imported only for live transport; replay and validation need no network.
    from websockets.asyncio.client import connect
    return connect(url, open_timeout=10, close_timeout=5, ping_interval=20,
                   ping_timeout=20, max_size=1_048_576, max_queue=16)


class LiveCollector:
    def __init__(self, processor: FeedProcessor, *, historical: HistoricalClient | None = None,
                 connector=websocket_connection, clock=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic, policy: RetryPolicy = RetryPolicy(), wait=None):
        self.processor = processor
        self.historical = historical or HistoricalClient()
        self.connector, self.clock, self.monotonic, self.policy = connector, clock, monotonic, policy
        self.wait = wait or self._wait
        self.last_repair_attempt = float('-inf')

    @staticmethod
    async def _wait(stop: asyncio.Event, delay: float):
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass

    async def repair(self):
        pending = self.processor.state["repair"]
        now = self.clock()
        closed_end = int(now.timestamp()) // 300 * 300
        if not pending or pending["start"] >= closed_end:
            return
        request_end = min(pending["end"], closed_end, pending["start"] + self.historical.page_limit * 300)
        if self.monotonic() - self.last_repair_attempt < 30:
            return
        self.last_repair_attempt = self.monotonic()
        responses = []
        try:
            # Network reads use a worker thread; SQLite remains on this event-loop thread.
            candles = await asyncio.to_thread(self.historical.fetch, self.processor.product,
                                               datetime.fromtimestamp(pending["start"], UTC),
                                               datetime.fromtimestamp(request_end, UTC), 300, response_log=responses)
            now = self.clock()
            with self.processor.store.transaction():
                statuses = [self.processor.store.put(c, now, finalized=True, source="rest") for c in candles]
                self.processor.store.journal(now, "recovery", self.processor.session or "",
                                             json.dumps({"candles": [c.as_dict() for c in candles], "responses": responses}, sort_keys=True),
                                             {"kind": "backfill", "statuses": statuses})
                if "conflict" in statuses:
                    self.processor.state["integrity_error"] = True
                    self.processor._save()
            self.processor.backfill_complete(now)
        except (TransportError, DataError) as exc:
            now = self.clock()
            with self.processor.store.transaction():
                self.processor.state["last_error"] = type(exc).__name__
                self.processor.store.journal(now, "recovery_failure", self.processor.session or "",
                    json.dumps({"responses": responses, "error": type(exc).__name__}, sort_keys=True),
                    {"status": "recovery_failed", "error": type(exc).__name__})
                self.processor._save()
            LOG.warning("backfill_failed error=%s", type(exc).__name__)

    async def run(self, stop: asyncio.Event, *, max_connections: int | None = None):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        if max_connections is not None and max_connections < 1:
            raise ValueError("max connections must be positive")
        failures, connections = 0, 0
        repair_task = None
        try:
            while not stop.is_set() and (max_connections is None or connections < max_connections):
                connections += 1
                try:
                    async with self.connector(WS_URL) as socket:
                        self.processor.begin_session(uuid.uuid4().hex, self.clock())
                        for message in ({"type": "subscribe", "channel": "heartbeats"},
                                        {"type": "subscribe", "channel": "candles", "product_ids": [self.processor.product]}):
                            await socket.send(json.dumps(message))
                        connected_at = self.monotonic()
                        invalid_count = 0
                        while not stop.is_set():
                            try:
                                raw = await asyncio.wait_for(socket.recv(), timeout=1)
                            except TimeoutError:
                                raw = None
                            if raw is not None:
                                outcome = self.processor.ingest(raw, self.clock())
                                invalid_count = invalid_count + 1 if outcome["status"] in ("invalid", "server_error") else 0
                                if invalid_count >= 3 or outcome["status"] == "server_error":
                                    raise FeedFailure("invalid_or_error_frames")
                            health = self.processor.health(self.clock())
                            if self.monotonic() - connected_at > self.processor.policy.candle_timeout:
                                if health["status"] in ("warming", "stale"):
                                    raise FeedFailure("feed_timeout")
                            if health["status"] == "healthy" and self.monotonic() - connected_at >= 60:
                                failures = 0
                            # REST backfill must not pause consumption of live frames.
                            if repair_task is None or repair_task.done():
                                if repair_task is not None:
                                    repair_task.result()
                                repair_task = asyncio.create_task(self.repair())
                except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, FeedFailure) as exc:
                    self.processor.disconnected(self.clock(), type(exc).__name__)
                    delay = self.policy.delay(failures)
                    failures += 1
                    LOG.warning("websocket_reconnect attempt=%s delay=%s error=%s", connections, delay, type(exc).__name__)
                    if stop.is_set() or (max_connections is not None and connections >= max_connections):
                        break
                    await self.wait(stop, delay)
        finally:
            self.processor.disconnected(self.clock(), "collector_stopped", stopped=True)
            if repair_task is not None:
                await repair_task  # Drain the bounded in-flight request before SQLite closes.

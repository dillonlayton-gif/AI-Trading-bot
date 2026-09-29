"""Single-product public candle stream validation and persistent feed health."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from .models import DataError, integer, json_text, normalize_candle, product_id, timestamp, utc
from .store import MarketStore

MAX_MESSAGE_BYTES = 1_048_576


@dataclass(frozen=True)
class FeedPolicy:
    max_message_age: float = 15.0
    heartbeat_timeout: float = 15.0
    candle_timeout: float = 30.0
    future_tolerance: float = 5.0

    def __post_init__(self):
        if not all(0 < v <= 600 for v in (self.max_message_age, self.heartbeat_timeout, self.candle_timeout, self.future_tolerance)):
            raise ValueError("invalid feed time thresholds")


def health_report(state: dict, now: datetime, policy: FeedPolicy = FeedPolicy()) -> dict:
    now = utc(now)
    def age(key):
        return None if state.get(key) is None else max(0.0, (now - timestamp(state[key])).total_seconds())
    heartbeat_age, candle_age = age("last_heartbeat"), age("last_candle")
    if state["connection"] != "connected":
        status = state["connection"]
    elif heartbeat_age is None or candle_age is None:
        status = "warming"
    elif heartbeat_age > policy.heartbeat_timeout or candle_age > policy.candle_timeout:
        status = "stale"
    elif state.get("repair") or state.get("integrity_error"):
        status = "degraded"
    else:
        status = "healthy"
    return {**state, "status": status, "heartbeat_age_seconds": heartbeat_age, "candle_age_seconds": candle_age,
            "as_of": now.isoformat(), "policy": vars(policy)}


class FeedProcessor:
    """No trading imports. Every decision and raw frame is persisted atomically."""
    def __init__(self, store: MarketStore, product: str, *, policy: FeedPolicy = FeedPolicy()):
        self.store, self.product, self.policy = store, product_id(product), policy
        saved = store.get_meta(self._key)
        self.state = json.loads(saved) if saved else {
            "product": product, "connection": "disconnected", "session": None, "reconnects": 0,
            "last_heartbeat": None, "last_candle": None, "last_error": None, "repair": None,
            "integrity_error": False, "counts": {}}
        self.sequences: dict[str, int] = {}
        self.times: dict[str, datetime] = {}
        self.heartbeat_counter: int | None = None
        self.session: str | None = None

    @property
    def _key(self):
        return "feed:" + self.product

    def _save(self):
        self.store.set_meta(self._key, json_text(self.state))

    def _count(self, key: str, count: int = 1):
        counters = self.state["counts"]
        counters[key] = counters.get(key, 0) + count

    def _repair(self, start: int, end: int):
        if end <= start:
            end = start + 300
        previous = self.state.get("repair")
        if previous:
            start, end = min(start, previous["start"]), max(end, previous["end"])
        self.state["repair"] = {"start": start, "end": end}

    def begin_session(self, session: str, now: datetime):
        now = utc(now)
        if not isinstance(session, str) or not session:
            raise ValueError("session identifier required")
        reconnect = self.state["session"] is not None
        latest = self.store.latest_start(self.product, 300)
        if reconnect or latest is not None:
            first = latest if latest is not None else int(now.timestamp()) // 300 * 300
            self._repair(first, int(now.timestamp()) // 300 * 300)
        self.state.update(connection="connected", session=session, last_heartbeat=None, last_candle=None, policy=vars(self.policy))
        if reconnect:
            self.state["reconnects"] += 1
        self.session = session
        self.sequences.clear()
        self.times.clear()
        self.heartbeat_counter = None
        with self.store.transaction():
            self.store.journal(now, "control", session, json_text({"kind": "connect", "product": self.product, "policy": vars(self.policy)}), {"kind": "connect"})
            self._save()

    def disconnected(self, now: datetime, error: str, *, stopped: bool = False):
        now = utc(now)
        self.state.update(connection="stopped" if stopped else "disconnected", last_error=error)
        if not stopped:
            latest = self.store.latest_start(self.product, 300)
            start = latest if latest is not None else int(now.timestamp()) // 300 * 300
            self._repair(start, int(now.timestamp()) // 300 * 300)
        with self.store.transaction():
            raw = json_text({"kind": "disconnect", "product": self.product, "error": error, "stopped": stopped})
            self.store.journal(now, "control", self.session or "", raw, {"kind": "disconnect"})
            self._save()

    def backfill_complete(self, now: datetime) -> bool:
        """Only clear a repair after all requested completed buckets are archived."""
        pending = self.state["repair"]
        if pending is None:
            return True
        closed_end = min(pending["end"], int(utc(now).timestamp()) // 300 * 300)
        next_start = pending["start"]
        for candle in self.store.candles(self.product, 300, start=next_start, end=closed_end):
            if candle.epoch != next_start:
                break
            next_start += 300
        if next_start == pending["start"]:
            return False
        complete = next_start == pending["end"]
        self.state["repair"] = None if complete else {"start": next_start, "end": pending["end"]}
        kind = "backfill_complete" if complete else "backfill_progress"
        with self.store.transaction():
            self.store.journal(now, "control", self.session or "", json_text({"kind": kind, "product": self.product}), {"kind": kind})
            self._save()
        return complete

    def health(self, now: datetime) -> dict:
        return health_report(self.state, now, self.policy)

    def _record(self, raw: str, now: datetime, outcome: dict) -> dict:
        with self.store.transaction():
            self._count(outcome["status"])
            self.store.journal(now, "ws", self.session or "", raw, outcome)
            self._save()
        return outcome

    def ingest(self, raw: str, received: datetime) -> dict:
        received = utc(received)
        if self.session is None or self.state["connection"] != "connected":
            raise RuntimeError("connect a session before ingesting")
        if not isinstance(raw, str):
            return self._record("<non-text frame>", received, {"status": "invalid", "reason": "non_text_frame"})
        if len(raw.encode()) > MAX_MESSAGE_BYTES:
            return self._record("<oversized frame omitted>", received, {"status": "invalid", "reason": "message_size"})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise DataError("object frame required")
            if body.get("type") == "error" or body.get("channel") == "error":
                return self._record(raw, received, {"status": "server_error"})
            channel = body.get("channel")
            if channel not in ("candles", "heartbeats"):
                return self._record(raw, received, {"status": "ignored"})
            sent = timestamp(body["timestamp"])
            sequence = integer(body["sequence_num"])
            events = body["events"]
            if not isinstance(events, list) or not events or not all(isinstance(event, dict) for event in events):
                raise DataError("nonempty events array required")
            candles, counter = [], None
            if channel == "candles":
                for event in events:
                    if event.get("type") not in ("snapshot", "update") or not isinstance(event.get("candles"), list):
                        raise DataError("invalid candle event")
                    candles.extend(normalize_candle(row, self.product, 300) for row in event["candles"])
                if not candles:
                    raise DataError("empty candles")
                if any(c.epoch > int(sent.timestamp()) + self.policy.future_tolerance for c in candles):
                    raise DataError("future candle bucket")
            else:
                if len(events) != 1:
                    raise DataError("one heartbeat event required")
                counter = integer(events[0]["heartbeat_counter"])
            lag = (received - sent).total_seconds()
            if lag > self.policy.max_message_age:
                return self._record(raw, received, {"status": "stale", "channel": channel})
            if lag < -self.policy.future_tolerance:
                return self._record(raw, received, {"status": "future", "channel": channel})
            previous = self.sequences.get(channel)
            if previous is not None and sequence <= previous:
                return self._record(raw, received, {"status": "duplicate" if sequence == previous else "out_of_order", "channel": channel})
            if channel in self.times and sent < self.times[channel]:
                return self._record(raw, received, {"status": "out_of_order", "channel": channel})
            if counter is not None and self.heartbeat_counter is not None and counter <= self.heartbeat_counter:
                return self._record(raw, received, {"status": "duplicate" if counter == self.heartbeat_counter else "out_of_order", "channel": channel})
        except (DataError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._record(raw, received, {"status": "invalid", "reason": type(exc).__name__})

        gap = previous is not None and sequence != previous + 1
        heartbeat_gap = counter is not None and self.heartbeat_counter is not None and counter != self.heartbeat_counter + 1
        statuses = []
        with self.store.transaction():
            if gap or heartbeat_gap:
                latest = self.store.latest_start(self.product, 300)
                start = latest if latest is not None else int(sent.timestamp()) // 300 * 300
                self._repair(start, int(sent.timestamp()) // 300 * 300)
            if channel == "heartbeats":
                self.state["last_heartbeat"] = sent.isoformat()
                self.heartbeat_counter = counter
            else:
                snapshot = all(event["type"] == "snapshot" for event in events)
                latest = self.store.latest_start(self.product, 300)
                for candle in sorted(candles, key=lambda c: c.epoch):
                    if latest is not None and candle.epoch < latest and not snapshot:
                        status = "out_of_order"
                    else:
                        if latest is not None and candle.epoch > latest:
                            if candle.epoch > latest + 300:
                                self._count("bucket_gap")
                            self._repair(latest, candle.epoch)
                        status = self.store.put(candle, sent, finalized=False, source="ws")
                        if status in ("accepted", "revision", "duplicate"):
                            latest = max(latest or candle.epoch, candle.epoch)
                            if candle.end_epoch > int(sent.timestamp()):
                                self.state["last_candle"] = sent.isoformat()
                    statuses.append(status)
                    self._count("candle_" + status)
                    if status == "conflict":
                        self.state["integrity_error"] = True
                # Historical snapshot buckets are provisional until REST confirms them.
                closed = [c.epoch for c in candles if c.end_epoch <= int(sent.timestamp())]
                if closed:
                    self._repair(min(closed), max(closed) + 300)
            self.sequences[channel], self.times[channel] = sequence, sent
            outcome = {"status": "sequence_gap" if gap or heartbeat_gap else "accepted", "channel": channel,
                       "sequence": sequence, "candles": statuses}
            self._count(outcome["status"])
            self.store.journal(received, "ws", self.session, raw, outcome)
            self._save()
        return outcome

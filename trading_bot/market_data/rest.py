"""Coinbase Advanced Trade public historical candles. No auth inputs."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import DataError, GRANULARITIES, MarketCandle, normalize_candle, product_id, utc
from .store import MarketStore

LOG = logging.getLogger(__name__)
BASE_URL = "https://api.coinbase.com/api/v3/brokerage/market/products"
MAX_RESPONSE_BYTES = 2_000_000


class TransportError(RuntimeError):
    def __init__(self, status: int | None, retry_after: str | None = None):
        super().__init__(f"public candle request failed (status={status})")
        self.status, self.retry_after = status, retry_after


def public_get(url: str, timeout: float) -> str:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "guarded-paper-trader/0.2"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise DataError("oversized public response")
            return data.decode("utf-8")
    except HTTPError as exc:
        raise TransportError(exc.code, exc.headers.get("Retry-After")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise TransportError(None) from exc


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base: float = 1.0
    cap: float = 30.0
    timeout: float = 10.0

    def __post_init__(self):
        if not 1 <= self.attempts <= 10 or not 0 < self.base <= self.cap <= 300 or not 0 < self.timeout <= 60:
            raise ValueError("invalid retry policy")

    def delay(self, attempt: int, retry_after: str | None = None, now: datetime | None = None) -> float:
        delay = min(self.cap, self.base * 2 ** min(attempt, 20))
        if retry_after:
            try:
                requested = float(retry_after)
            except ValueError:
                try:
                    requested = (parsedate_to_datetime(retry_after) - utc(now or datetime.now(timezone.utc))).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    requested = 0.0
            if requested == requested:  # Ignore NaN.
                delay = max(delay, max(0.0, requested))
        return min(self.cap, delay)


class HistoricalClient:
    def __init__(self, *, transport=public_get, sleep=time.sleep, policy: RetryPolicy = RetryPolicy(),
                 clock=lambda: datetime.now(timezone.utc), page_limit: int = 350):
        if not 1 <= page_limit <= 350:
            raise ValueError("page limit must be 1..350")
        self.transport, self.sleep, self.policy, self.clock, self.page_limit = transport, sleep, policy, clock, page_limit

    def _request(self, url: str) -> str:
        for attempt in range(self.policy.attempts):
            try:
                return self.transport(url, self.policy.timeout)
            except TransportError as exc:
                retryable = exc.status is None or exc.status == 429 or (500 <= exc.status < 600)
                if not retryable or attempt + 1 == self.policy.attempts:
                    raise
                delay = self.policy.delay(attempt, exc.retry_after, self.clock())
                LOG.warning("historical_retry status=%s attempt=%s delay=%s", exc.status, attempt + 1, delay)
                self.sleep(delay)
        raise AssertionError("unreachable")

    def fetch(self, product: str, start: datetime, end: datetime, seconds: int = 300,
              *, store: MarketStore | None = None, response_log: list[dict] | None = None) -> list[MarketCandle]:
        product_id(product)
        start, end, observed = utc(start), utc(end), utc(self.clock())
        if seconds not in GRANULARITIES or isinstance(seconds, bool):
            raise DataError("unsupported granularity")
        first, last = int(start.timestamp()), int(end.timestamp())
        if (start.microsecond or end.microsecond or first < 0 or first % seconds or last % seconds
                or last <= first or last > int(observed.timestamp())):
            raise DataError("aligned, completed historical [start,end) range required")
        all_candles: dict[int, MarketCandle] = {}
        for page_start in range(first, last, self.page_limit * seconds):
            page_end = min(last, page_start + self.page_limit * seconds)
            params = urlencode({"start": str(page_start), "end": str(page_end - 1),
                                "granularity": GRANULARITIES[seconds], "limit": self.page_limit})
            raw = self._request(f"{BASE_URL}/{product}/candles?{params}")
            if response_log is not None:
                response_log.append({"raw": raw, "product": product, "start": page_start, "end": page_end, "seconds": seconds})
            try:
                body = json.loads(raw)
                rows = body["candles"]
                if not isinstance(rows, list) or len(rows) > self.page_limit:
                    raise DataError("invalid historical response length")
                normalized = [normalize_candle(row, product, seconds) for row in rows]
                page: dict[int, MarketCandle] = {}
                statuses = []
                for candle in normalized:
                    if not page_start <= candle.epoch < page_end:
                        raise DataError("out-of-range historical candle")
                    if candle.epoch in page:
                        if page[candle.epoch] != candle:
                            raise DataError("conflicting duplicate historical candle")
                        statuses.append("duplicate")
                    page[candle.epoch] = candle
            except (DataError, KeyError, TypeError, json.JSONDecodeError) as exc:
                if store:
                    with store.transaction():
                        store.journal(observed, "rest", "", raw,
                                      {"product": product, "start": page_start, "end": page_end,
                                       "seconds": seconds, "limit": self.page_limit, "status": "invalid"})
                raise DataError("invalid historical response") from exc
            missing = [t for t in range(page_start, page_end, seconds) if t not in page]
            if store:
                with store.transaction():
                    for candle in sorted(page.values(), key=lambda c: c.epoch):
                        statuses.append(store.put(candle, observed, finalized=True, source="rest"))
                    store.journal(observed, "rest", "", raw,
                                  {"product": product, "start": page_start, "end": page_end,
                                   "seconds": seconds, "limit": self.page_limit, "missing": missing, "statuses": statuses})
            if "conflict" in statuses:
                raise DataError("historical candle conflicts with finalized archive")
            all_candles.update(page)
            if missing:
                LOG.warning("historical_gap product=%s count=%s", product, len(missing))
        return [all_candles[key] for key in sorted(all_candles)]

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from trading_bot.market_data.models import DataError, MarketCandle, normalize_candle, timestamp
from trading_bot.market_data.rest import HistoricalClient, RetryPolicy, TransportError
from trading_bot.market_data.store import MarketStore

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(offset=0, **updates):
    result = {"start": str(int(T.timestamp()) + offset), "open": "100.00", "high": "102", "low": "99",
              "close": "101.0", "volume": "1.5"}
    result.update(updates)
    return result


def candle(offset=0, **updates):
    return normalize_candle(row(offset, **updates), "BTC-USD", 300)


class NormalizeTests(unittest.TestCase):
    def test_normalizes_utc_and_exact_decimals(self):
        c = candle()
        self.assertEqual(c.open, Decimal("100"))
        self.assertEqual(c.as_dict()["open"], "100")
        self.assertEqual(timestamp("2026-01-01T02:00:00+02:00"), T)
        self.assertEqual(MarketCandle.from_dict(c.as_dict()), c)

    def test_rejects_invalid_schema_and_nonfinite_numbers(self):
        for invalid in ("NaN", "Infinity", "-1", "0", 100.0, "1e2", ""):
            with self.subTest(invalid=invalid), self.assertRaises(DataError):
                candle(close=invalid)
        for invalid in ("-1", "NaN", 1.5):
            with self.assertRaises(DataError):
                candle(volume=invalid)
        self.assertEqual(candle(volume="0").volume, 0)
        with self.assertRaises(DataError):
            normalize_candle({}, "BTC-USD", 300)

    def test_rejects_ohlc_product_and_alignment_errors(self):
        for update in ({"high": "99"}, {"low": "101"}, {"product_id": "ETH-USD"}, {"start": "1.5"}):
            with self.assertRaises(DataError):
                candle(**update)
        with self.assertRaises(DataError):
            candle(1)
        with self.assertRaises(DataError):
            normalize_candle(row(), "BTC/USD", 300)
        with self.assertRaises(DataError):
            timestamp("2026-01-01T00:00:00")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "market.db"
        self.store = MarketStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_revisions_duplicates_and_immutable_final_candles(self):
        self.assertEqual(self.store.put(candle(), T, finalized=False, source="ws"), "accepted")
        self.assertEqual(self.store.put(candle(close="102", volume="2"), T + timedelta(seconds=1), finalized=False, source="ws"), "revision")
        self.assertEqual(list(self.store.candles("BTC-USD", 300)), [])
        self.assertEqual(self.store.put(candle(close="102", volume="2"), T + timedelta(seconds=2), finalized=False, source="ws"), "duplicate")
        self.assertEqual(self.store.put(candle(close="101", volume="2.5"), T + timedelta(seconds=3), finalized=True, source="rest"), "finalized")
        self.assertEqual(self.store.put(candle(), T + timedelta(seconds=4), finalized=True, source="rest"), "conflict")
        self.assertEqual(next(self.store.candles("BTC-USD", 300)).volume, Decimal("2.5"))

    def test_rejects_backwards_revision(self):
        self.store.put(candle(), T, finalized=False, source="ws")
        self.assertEqual(self.store.put(candle(close="102"), T, finalized=False, source="ws"), "out_of_order")
        self.assertEqual(self.store.put(candle(volume="1"), T + timedelta(seconds=1), finalized=False, source="ws"), "conflict")

    def test_transaction_rolls_back_candle_and_journal(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.put(candle(), T, finalized=True, source="rest")
                self.store.journal(T, "rest", "", "{}", {"ok": True})
                raise RuntimeError("disk write simulation")
        self.assertEqual(list(self.store.candles("BTC-USD", 300)), [])
        self.assertEqual(list(self.store.journal_records()), [])

    def test_restart_readonly_replay_stable_and_sorted(self):
        for start in (600, 0, 300):
            self.store.put(candle(start), T, finalized=True, source="rest")
        lines = list(self.store.replay_jsonl("BTC-USD", 300))
        digest = self.store.replay_digest("BTC-USD", 300)
        self.store.close()
        self.store = MarketStore(self.path, readonly=True)
        self.assertEqual(list(self.store.replay_jsonl("BTC-USD", 300)), lines)
        self.assertEqual(self.store.replay_digest("BTC-USD", 300), digest)
        self.assertEqual([c.epoch for c in self.store.candles("BTC-USD", 300)], sorted(c.epoch for c in self.store.candles("BTC-USD", 300)))
        self.assertEqual(len(list(self.store.candles("BTC-USD", 300, start=int(T.timestamp()) + 300, end=int(T.timestamp()) + 600))), 1)


class HistoryTests(unittest.TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    def client(self, transport, **kwargs):
        return HistoricalClient(transport=transport, clock=lambda: T + timedelta(days=1), **kwargs)

    def test_paginates_and_sorts_public_completed_candles(self):
        urls = []
        def transport(url, timeout):
            urls.append(url)
            params = parse_qs(urlparse(url).query)
            start, end = int(params["start"][0]), int(params["end"][0])
            rows = [row(t - int(T.timestamp())) for t in range(start, end + 1, 300)]
            return json.dumps({"candles": rows[::-1]})
        result = self.client(transport, page_limit=2).fetch("BTC-USD", T, T + timedelta(minutes=15), store=self.store)
        self.assertEqual(len(result), 3)
        self.assertEqual(len(urls), 2)
        self.assertTrue(all('/brokerage/market/products/BTC-USD/candles?' in u for u in urls))
        self.assertEqual(result, list(self.store.candles("BTC-USD", 300)))
        self.assertEqual(len(list(self.store.journal_records())), 2)

    def test_missing_buckets_are_logged_never_fabricated(self):
        result = self.client(lambda *_: json.dumps({"candles": [row()]})).fetch("BTC-USD", T, T + timedelta(minutes=10), store=self.store)
        self.assertEqual(len(result), 1)
        outcome = json.loads(next(self.store.journal_records())["outcome"])
        self.assertEqual(outcome["missing"], [int(T.timestamp()) + 300])

    def test_identical_duplicates_dedupe_conflicts_fail(self):
        result = self.client(lambda *_: json.dumps({"candles": [row(), row()]})).fetch("BTC-USD", T, T + timedelta(minutes=5))
        self.assertEqual(len(result), 1)
        with self.assertRaises(DataError):
            self.client(lambda *_: json.dumps({"candles": [row(), row(close="102")]})).fetch("BTC-USD", T, T + timedelta(minutes=5))

    def test_retries_transient_errors_with_bounded_backoff(self):
        calls, delays = [], []
        def transport(*args):
            calls.append(args)
            if len(calls) < 3:
                raise TransportError(429, "2")
            return json.dumps({"candles": [row()]})
        result = self.client(transport, sleep=delays.append).fetch("BTC-USD", T, T + timedelta(minutes=5))
        self.assertEqual(len(result), 1)
        self.assertEqual(delays, [2, 2])
        self.assertEqual(RetryPolicy().delay(100, "9999"), 30)
        self.assertEqual(RetryPolicy().delay(0, "bad"), 1)
        self.assertEqual(RetryPolicy().delay(0, "Thu, 01 Jan 2026 00:00:10 GMT", T), 10)

    def test_retry_exhaustion_and_permanent_errors(self):
        for status, expected_delays in ((503, 3), (None, 3), (401, 0)):
            delays = []
            def transport(*_, status=status):
                raise TransportError(status)
            with self.assertRaises(TransportError):
                self.client(transport, sleep=delays.append).fetch("BTC-USD", T, T + timedelta(minutes=5))
            self.assertEqual(len(delays), expected_delays)

    def test_bad_ranges_and_malformed_data_fail(self):
        for start, end in ((T, T), (T + timedelta(seconds=1), T + timedelta(minutes=5)), (T, T + timedelta(days=2))):
            with self.assertRaises(DataError):
                self.client(lambda *_: '{}').fetch("BTC-USD", start, end)
        for raw in ('invalid', '{}', '{"candles":{}}', json.dumps({"candles": [row(600)]})):
            with self.assertRaises(DataError):
                self.client(lambda *_: raw).fetch("BTC-USD", T, T + timedelta(minutes=5))

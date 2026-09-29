import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from trading_bot.core import Candle, Intent, Limits, PaperBroker, PaperLedger, PaperRunner, RiskEngine, amount


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.db"
        self.ledger = PaperLedger(self.path, D("1000"))
        self.broker = PaperBroker(self.ledger, RiskEngine(Limits()))
        self.runner = PaperRunner(self.ledger, self.broker)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.candle = Candle(self.now, D("100"), "BTC-USD")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_valid_fill_and_persistence(self):
        self.assertTrue(self.runner.step(self.candle, Intent("BUY", D("0.5"), "BTC-USD")))
        self.assertEqual(self.ledger.get("units"), "0.5")
        self.assertEqual(D(self.ledger.get("cash")), D("949.5994000"))
        self.ledger.close()
        self.ledger = PaperLedger(self.path, D("999999"))
        self.assertEqual(D(self.ledger.get("cash")), D("949.5994000"))

    def test_all_orders_use_risk_gate(self):
        self.runner.step(self.candle)
        for intent in [Intent("BUY", D("2"), "BTC-USD"), Intent("SELL", D("1"), "BTC-USD"),
                       Intent("BUY", D("-1"), "BTC-USD"), Intent("BUY", D("NaN"), "BTC-USD"),
                       Intent("BUY", D("1"), "ETH-USD")]:
            self.assertFalse(self.broker.submit(intent, self.candle))
        self.assertEqual(D(self.ledger.get("cash")), D("1000"))
        self.assertEqual(D(self.ledger.get("units")), D(0))

    def test_kill_switch_and_loss_limit(self):
        self.runner.step(self.candle)
        with self.ledger.db:
            self.ledger.set("day_start", "1100")
        self.assertFalse(self.broker.submit(Intent("BUY", D("0.1"), "BTC-USD"), self.candle))
        with self.ledger.db:
            self.ledger.set("day_start", "1000")
            self.ledger.set("stopped", "1")
        self.assertFalse(self.broker.submit(Intent("BUY", D("0.1"), "BTC-USD"), self.candle))

    def test_replay_rejects_duplicates_and_bad_time(self):
        self.runner.step(self.candle)
        with self.assertRaises(ValueError):
            self.runner.step(self.candle)
        with self.assertRaises(ValueError):
            self.runner.step(Candle(datetime(2026, 1, 2), D("100"), "BTC-USD"))

    def test_invalid_limits_and_decimals(self):
        with self.assertRaises(ValueError):
            Limits(max_position_fraction=D("1.1"))
        with self.assertRaises(ValueError):
            amount("Infinity")

"""Regression checks start fresh Python processes through public package paths."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageEntryTests(unittest.TestCase):
    def run_python(self, *args):
        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, *args], cwd=ROOT, env=environment,
                              capture_output=True, text=True, timeout=15)

    def test_root_and_market_exports_import_in_fresh_process(self):
        result = self.run_python('-c', '''
import sys
import trading_bot
assert 'trading_bot.core' not in sys.modules
assert 'trading_bot.market_data.models' not in sys.modules
from trading_bot.market_data import MarketCandle, MarketStore
from trading_bot.core import PaperBroker, RiskEngine
from trading_bot.market_data.cli import main
assert callable(main)
print('package imports passed')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('package imports passed', result.stdout)

    def test_market_data_cli_module_help(self):
        result = self.run_python('-m', 'trading_bot.market_data.cli', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('historical', 'live', 'health', 'replay-journal'):
            self.assertIn(command, result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_market_data_cli_executes_replay_through_package(self):
        from datetime import datetime, timezone
        from trading_bot.market_data.models import normalize_candle
        from trading_bot.market_data.store import MarketStore
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'market sample.db'
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            candle = normalize_candle({'start':str(int(now.timestamp())), 'open':'100', 'high':'102',
                                      'low':'99', 'close':'101', 'volume':'1.5'}, 'BTC-USD', 300)
            store = MarketStore(database)
            try:
                store.put(candle, now, finalized=True, source='rest')
            finally:
                store.close()
            result = self.run_python('-m', 'trading_bot.market_data.cli', 'replay', '--db', str(database))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), candle.as_dict())

    def test_phase_one_paper_cli_still_executes_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_python('-m', 'trading_bot.cli', 'replay', str(ROOT / 'data/example.csv'),
                                     '--db', str(Path(directory) / 'paper sample.db'), '--buy-quantity', '0.5')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PAPER cash=9949.5994000 units=0.5 stopped=0', result.stdout)

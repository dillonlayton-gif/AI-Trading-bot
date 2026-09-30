"""Known arithmetic, causal behavior and failure atomicity for Phase 3."""
import json
import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext, ROUND_DOWN
from pathlib import Path
import tempfile
from trading_bot.market_data.store import MarketStore

from trading_bot.indicators import FeatureConfig, FeatureEngine, calculate
from trading_bot.market_data.models import DataError, MarketCandle

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CFG = FeatureConfig(sma=3, ema=3, rsi=3, macd_fast=2, macd_slow=4,
                    macd_signal=2, atr=3, vwap=3, momentum=2, volatility=3, volume=3)


def candles(prices, volumes=None):
    return [MarketCandle('BTC-USD', BASE + timedelta(minutes=5*i), 300,
                         D(str(p)), D(str(p))+1, D(str(p))-1, D(str(p)),
                         D(str(volumes[i] if volumes is not None else 2)))
            for i, p in enumerate(prices)]


def rows(prices, volumes=None, config=CFG):
    return calculate(candles(prices, volumes), config, finalized=True)


def vals(row):
    return dict(row.values)


class IndicatorTests(unittest.TestCase):
    def test_sma_known(self):
        self.assertEqual(vals(rows([10, 12, 14])[-1])['sma'], D(12))

    def test_ema_seed_and_update(self):
        result = rows([10, 12, 14, 18])
        self.assertEqual(vals(result[2])['ema'], D(12))
        self.assertEqual(vals(result[3])['ema'], D(15))

    def test_rsi_known_wilder(self):
        result = rows([10, 12, 11, 13, 12])
        self.assertEqual(vals(result[3])['rsi'], D(80))
        self.assertAlmostEqual(float(vals(result[4])['rsi']), 800/13, places=12)

    def test_rsi_flat(self):
        self.assertEqual(vals(rows([10]*8)[-1])['rsi'], D(50))

    def test_rsi_rising(self):
        self.assertEqual(vals(rows([10, 11, 12, 13])[-1])['rsi'], D(100))

    def test_rsi_falling(self):
        self.assertEqual(vals(rows([13, 12, 11, 10])[-1])['rsi'], D(0))

    def test_macd_seed_signal_histogram(self):
        result = rows([10, 12, 14, 16, 18, 20])
        self.assertEqual(vals(result[3])['macd'], D(2))
        self.assertIsNone(vals(result[3])['macd_signal'])
        self.assertEqual(vals(result[4])['macd_signal'], D(2))
        self.assertEqual(vals(result[5])['macd_histogram'], D(0))

    def test_macd_flat(self):
        v = vals(rows([10]*8)[-1])
        self.assertEqual((v['macd'], v['macd_signal'], v['macd_histogram']), (0, 0, 0))

    def test_atr_known_wilder(self):
        result = rows([10, 12, 14, 18])
        self.assertAlmostEqual(float(vals(result[2])['atr']), 8/3, places=12)
        self.assertAlmostEqual(float(vals(result[3])['atr']), 31/9, places=12)

    def test_atr_uses_previous_close(self):
        cfg = replace(CFG, atr=1)
        self.assertEqual(vals(rows([10, 20], config=cfg)[-1])['atr'], D(11))

    def test_vwap_known(self):
        self.assertEqual(vals(rows([10, 12, 14], [1, 2, 3])[-1])['vwap'], D('12.66666666666666666666666666666667'))

    def test_vwap_typical_price(self):
        cs = candles([10])
        cs[0] = replace(cs[0], high=D(16), low=D(8))
        v = vals(calculate(cs, replace(CFG, vwap=1), finalized=True)[0])
        self.assertEqual(v['vwap'], D('11.33333333333333333333333333333333'))

    def test_zero_volume_explicit(self):
        row = rows([10]*5, [0]*5)[-1]
        for name in ('vwap', 'relative_volume', 'volume_vs_prior'):
            self.assertIsNone(vals(row)[name])
            self.assertEqual(dict(row.unavailable)[name], 'zero_volume')
        self.assertEqual(vals(row)['volume_sma'], D(0))

    def test_low_volume_not_discarded(self):
        v = vals(rows([10, 12, 14], ['0.00000001']*3)[-1])
        self.assertEqual(v['vwap'], D(12))
        self.assertEqual(v['relative_volume'], D(1))

    def test_volume_known(self):
        v = vals(rows([10]*4, [1, 2, 3, 6])[-1])
        self.assertEqual(v['volume_sma'], D('3.666666666666666666666666666666667'))
        self.assertAlmostEqual(float(v['relative_volume']), 18/11, places=12)
        self.assertEqual(v['volume_vs_prior'], D(3))

    def test_returns_known(self):
        v = vals(rows([10, 12])[-1])
        self.assertEqual(v['simple_return'], D('0.2'))
        self.assertAlmostEqual(float(v['log_return']), math.log(1.2), places=14)

    def test_momentum_roc_known(self):
        v = vals(rows([10, 11, 14])[-1])
        self.assertEqual(v['momentum'], D(4))
        self.assertEqual(v['roc'], D('0.4'))

    def test_volatility_population_log_returns(self):
        v = vals(rows([10, 11, 13, 12])[-1])
        logs = [math.log(11/10), math.log(13/11), math.log(12/13)]
        avg = sum(logs)/3
        expected = math.sqrt(sum((r-avg)**2 for r in logs)/3)
        self.assertAlmostEqual(float(v['volatility']), expected, places=14)

    def test_flat_prices(self):
        v = vals(rows([10]*10)[-1])
        for name in ('simple_return', 'log_return', 'momentum', 'roc', 'volatility'):
            self.assertEqual(v[name], D(0))

    def test_every_warmup_boundary(self):
        ready = dict(sma=3, ema=3, rsi=4, macd=4, macd_signal=5, macd_histogram=5,
                     atr=3, vwap=3, simple_return=2, log_return=2, momentum=3,
                     roc=3, volatility=4, volume_sma=3, relative_volume=3, volume_vs_prior=4)
        result = rows([10]*8)
        self.assertEqual(set(vals(result[0])), set(ready))
        for name, first in ready.items():
            with self.subTest(name=name):
                for row in result[:first-1]:
                    self.assertIsNone(vals(row)[name])
                    self.assertEqual(dict(row.unavailable)[name], 'warmup')
                self.assertIsNotNone(vals(result[first-1])[name])

    def test_minimum_periods(self):
        cfg = FeatureConfig(sma=1, ema=1, rsi=1, macd_fast=1, macd_slow=2,
                            macd_signal=1, atr=1, vwap=1, momentum=1, volatility=1, volume=1)
        result = rows([10, 12, 11], config=cfg)
        self.assertEqual(vals(result[0])['ema'], D(10))
        self.assertEqual(vals(result[1])['rsi'], D(100))
        self.assertEqual(vals(result[2])['rsi'], D(0))
        self.assertEqual(vals(result[1])['volatility'], D(0))

    def test_empty_batch(self):
        self.assertEqual(calculate([], finalized=True), ())
        with self.assertRaises(DataError):
            calculate([], finalized=False)

    def test_config_invalid(self):
        for value in (0, -1, True, 1.5, '3', 10001):
            for field in ('sma', 'ema', 'rsi', 'macd_fast', 'macd_slow', 'macd_signal',
                          'atr', 'vwap', 'momentum', 'volatility', 'volume'):
                with self.subTest(value=value, field=field), self.assertRaises(DataError):
                    replace(CFG, **{field:value})
        for changes in ({'gap_policy':'ignore'}, {'macd_fast':4}, {'macd_fast':5}):
            with self.assertRaises(DataError):
                replace(CFG, **changes)
        with self.assertRaises(DataError):
            FeatureEngine({})

    def test_invalid_inputs_and_provisional(self):
        engine = FeatureEngine(CFG)
        for value in (None, {}, 1, 'candle'):
            with self.assertRaises(DataError):
                engine.update(value, finalized=True)
        for flag in (False, 1, None):
            with self.assertRaises(DataError):
                engine.update(candles([10])[0], finalized=flag)
        for field, value in [('close', D('NaN')), ('high', D('Infinity')),
                             ('low', D(0)), ('volume', D(-1)), ('open', 10.0),
                             ('high', D(1))]:
            c = candles([10])[0]
            object.__setattr__(c, field, value)
            with self.subTest(field=field), self.assertRaises(DataError):
                engine.update(c, finalized=True)
        self.assertEqual(engine.update(candles([10])[0], finalized=True).segment_count, 1)

    def test_duplicate_revision_out_of_order(self):
        cs = candles([10, 12, 14])
        engine = FeatureEngine(CFG)
        engine.update(cs[0], finalized=True)
        engine.update(cs[1], finalized=True)
        for c in (cs[1], replace(cs[1], close=D(13)), cs[0]):
            with self.assertRaises(DataError):
                engine.update(c, finalized=True)
        actual = engine.update(cs[2], finalized=True)
        self.assertEqual(actual, calculate(cs, CFG, finalized=True)[-1])

    def test_product_and_granularity_alignment(self):
        engine = FeatureEngine(CFG)
        cs = candles([10, 12])
        engine.update(cs[0], finalized=True)
        for c in (replace(cs[1], product='ETH-USD'), replace(cs[1], seconds=60)):
            with self.assertRaises(DataError):
                engine.update(c, finalized=True)
        row = engine.update(cs[1], finalized=True)
        self.assertEqual((row.product, row.start, row.seconds), ('BTC-USD', cs[1].start, 300))
        self.assertEqual(row.available_at, cs[1].start + timedelta(seconds=300))

    def test_gap_rejected_without_state_change(self):
        cs = candles([10, 12, 14])
        engine = FeatureEngine(CFG)
        engine.update(cs[0], finalized=True)
        with self.assertRaises(DataError):
            engine.update(cs[2], finalized=True)
        engine.update(cs[1], finalized=True)
        self.assertEqual(engine.update(cs[2], finalized=True), calculate(cs, CFG, finalized=True)[-1])

    def test_gap_reset_restarts_all_state(self):
        cs = candles([10, 12, 14, 16, 18, 20, 22, 24, 26])
        cfg = replace(CFG, gap_policy='reset')
        result = calculate(cs[:5]+cs[6:], cfg, finalized=True)
        fresh = calculate(cs[6:], cfg, finalized=True)
        self.assertTrue(result[5].gap_reset)
        self.assertEqual(result[5].segment_count, 1)
        for actual, expected in zip(result[5:], fresh):
            self.assertEqual(actual.values, expected.values)
            self.assertEqual(actual.unavailable, expected.unavailable)
        self.assertFalse(result[6].gap_reset)

    def test_no_lookahead_prefix_stability(self):
        cs = candles([10, 12, 11, 14, 16, 13, 18, 20])
        full = calculate(cs, CFG, finalized=True)
        for n in range(1, len(cs)+1):
            self.assertEqual(calculate(cs[:n], CFG, finalized=True), full[:n])
        altered = cs[:5] + [replace(c, high=D(101), close=D(100)) for c in cs[5:]]
        self.assertEqual(calculate(altered, CFG, finalized=True)[:5], full[:5])

    def test_batch_sequential_long_stream(self):
        cs = candles([10 + (i % 17) for i in range(200)])
        engine = FeatureEngine(CFG)
        sequential = tuple(engine.update(c, finalized=True) for c in cs)
        self.assertEqual(sequential, calculate(iter(cs), CFG, finalized=True))
        self.assertLessEqual(len(engine._candles), engine._limit)
        self.assertLessEqual(len(engine._returns), CFG.volatility)
        self.assertEqual(vals(sequential[-1])['sma'], sum(c.close for c in cs[-3:])/3)

    def test_decimal_context_isolated(self):
        cs = candles([10, 12, 11, 14, 16, 13, 18, 20])
        expected = calculate(cs, CFG, finalized=True)
        with localcontext() as ctx:
            ctx.prec = 6
            ctx.rounding = ROUND_DOWN
            ctx.Emax = 9
            ctx.Emin = -9
            self.assertEqual(calculate(cs, CFG, finalized=True), expected)
            self.assertEqual(ctx.prec, 6)
            self.assertEqual(ctx.rounding, ROUND_DOWN)

    def test_immutable_previous_results(self):
        engine = FeatureEngine(CFG)
        cs = candles([10, 12, 14, 16, 18, 20])
        with self.assertRaises(AttributeError):
            engine.config = replace(CFG, sma=5)
        first = engine.update(cs[0], finalized=True)
        serialized = first.as_dict()
        for c in cs[1:]:
            engine.update(c, finalized=True)
        self.assertEqual(first.as_dict(), serialized)
        serialized['values']['sma'] = 'wrong'
        self.assertIsNone(first.as_dict()['values']['sma'])
        with self.assertRaises(AttributeError):
            first.segment_count = 99

    def test_fixture_replay_pinned_hash(self):
        from scripts.validate_indicators import validate
        first = validate()
        self.assertEqual(first, validate())
        self.assertTrue(first['matched'])
        self.assertEqual(first['rows'], 16)

    def test_finalized_archive_integration(self):
        cs = candles([10, 12, 14, 16])
        with tempfile.TemporaryDirectory() as temp:
            store = MarketStore(Path(temp)/'market.db')
            try:
                for c in cs[:3]:
                    store.put(c, c.start + timedelta(seconds=300), finalized=True, source='rest')
                store.put(cs[3], cs[3].start, finalized=False, source='ws')
                archived = list(store.candles('BTC-USD', 300))
                self.assertEqual(archived, cs[:3])
                self.assertEqual(calculate(archived, CFG, finalized=True),
                                 calculate(cs[:3], CFG, finalized=True))
            finally:
                store.close()

    def test_arithmetic_failure_atomic(self):
        engine = FeatureEngine(CFG)
        cs = candles([10, 12])
        first = engine.update(cs[0], finalized=True)
        huge = replace(cs[1], open=D('1e999999'), high=D('1e999999'),
                       low=D('1e999999'), close=D('1e999999'), volume=D('1e999999'))
        with self.assertRaises(DataError):
            engine.update(huge, finalized=True)
        self.assertEqual(engine.update(cs[1], finalized=True), rows([10, 12])[-1])
        self.assertEqual(first.segment_count, 1)

    def test_rolls_windows_without_old_data(self):
        v = vals(rows([10, 12, 14, 16, 18], [100, 100, 1, 1, 1])[-1])
        self.assertEqual(v['sma'], D(16))
        self.assertEqual(v['vwap'], D(16))
        self.assertEqual(v['volume_sma'], D(1))
        self.assertEqual(v['relative_volume'], D(1))

    def test_return_price_drop_and_zero_current_volume(self):
        v = vals(rows([20, 10, 10, 10], [1, 2, 3, 0])[-1])
        self.assertEqual(vals(rows([20, 10])[-1])['simple_return'], D('-0.5'))
        self.assertEqual(v['relative_volume'], D(0))
        self.assertEqual(v['volume_vs_prior'], D(0))

    def test_feature_module_dependency_boundary(self):
        import ast
        root = Path(__file__).resolve().parents[1]/'trading_bot'/'indicators'
        allowed = {'__future__', 'collections', 'dataclasses', 'datetime', 'decimal', 'typing',
                   'trading_bot.market_data.models', 'features'}
        for p in root.glob('*.py'):
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module, allowed)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name, allowed)

    def test_validation_cli_output_bytes(self):
        import hashlib
        import subprocess
        import sys
        from scripts.validate_indicators import EXPECTED_SHA256
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)/'features.jsonl'
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, str(root/'scripts/validate_indicators.py'),
                                     '--output', str(out)], cwd=temp, check=True,
                                    capture_output=True, text=True, timeout=15)
            report = json.loads(result.stdout)
            raw = out.read_bytes()
            self.assertNotIn(b'\r', raw)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), EXPECTED_SHA256)
            self.assertEqual(report['sha256'], EXPECTED_SHA256)
            self.assertEqual(len(raw.splitlines()), 16)

    def test_classic_rsi_fourteen(self):
        prices = ['44.34','44.09','44.15','43.61','44.33','44.83','45.10','45.42',
                  '45.84','46.08','45.89','46.03','45.61','46.28','46.28']
        v = vals(rows(prices, config=replace(CFG, rsi=14))[-1])
        self.assertAlmostEqual(float(v['rsi']), 70.46413502109705, places=12)

    def test_macd_nonlinear_known(self):
        from fractions import Fraction as F
        v = vals(rows([10, 13, 11, 15, 12], config=replace(CFG, macd_slow=3))[-1])
        fast = F(23,2)
        slow = F(34,3)
        fast = fast + F(2,3)*(11-fast)
        first_macd = fast-slow
        fast = fast + F(2,3)*(15-fast)
        slow = slow + F(1,2)*(15-slow)
        second_macd = fast-slow
        signal = (first_macd+second_macd)/2
        fast = fast + F(2,3)*(12-fast)
        slow = slow + F(1,2)*(12-slow)
        macd = fast-slow
        signal = signal + F(2,3)*(macd-signal)
        self.assertAlmostEqual(float(v['macd']), float(macd), places=14)
        self.assertAlmostEqual(float(v['macd_signal']), float(signal), places=14)
        self.assertAlmostEqual(float(v['macd_histogram']), float(macd-signal), places=14)

    def test_constant_growth_volatility(self):
        self.assertEqual(vals(rows([10, 20, 40, 80])[-1])['volatility'], D(0))

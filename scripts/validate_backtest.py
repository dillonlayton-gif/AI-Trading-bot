"""Offline Phase 4 gate: explicit baselines, known trades/metrics and pinned replay."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from trading_bot.backtest import BacktestConfig, BacktestEngine, SMATrend, MACDTrend, run_backtest
from trading_bot.backtest.common import primitive
from trading_bot.indicators import FeatureConfig
from trading_bot.market_data.models import MarketCandle, decimal_text, json_text

EXPECTED_SHA256 = '9fce5de70cf0156afb363e60f59a522170421776d4c013ac1016b9fb0dff09bf'


def build():
    fixture = json.loads((ROOT/'data/backtest_sample.json').read_text(encoding='utf-8'))
    candles = [MarketCandle.from_dict(c) for c in fixture['candles']]
    features = FeatureConfig(**fixture['features'])
    config = BacktestConfig(**{k:Decimal(v) for k,v in fixture['config'].items()})
    payload = {}
    for strategy in (SMATrend(), MACDTrend()):
        result = run_backtest(candles,strategy,features,config,finalized=True)
        engine = BacktestEngine(strategy,features,config)
        for candle in candles:
            engine.step(candle,finalized=True)
        if result.serialize() != engine.result().serialize():
            raise RuntimeError('sequential replay mismatch')
        if result.serialize() != run_backtest(candles,strategy,features,config,finalized=True).serialize():
            raise RuntimeError('repeated replay mismatch')
        expected = fixture['expected'][strategy.name]
        indices = {c.start:i for i,c in enumerate(candles)}
        trades = [[indices[t.entry_time],indices[t.exit_time],decimal_text(t.pnl)] for t in result.trades]
        if trades != expected['trades']:
            raise RuntimeError('known fixture trades mismatch')
        actual = primitive(dict(result.metrics))
        benchmark = primitive(dict(result.benchmark_metrics))
        if any(actual[k] != v for k,v in expected['metrics'].items()):
            raise RuntimeError('known fixture metrics mismatch')
        if any(benchmark[k] != v for k,v in fixture['expected']['benchmark'].items()):
            raise RuntimeError('known benchmark mismatch')
        payload[strategy.name] = json.loads(result.serialize())
    if payload['sma_trend']['benchmark_bars'] != payload['macd_trend']['benchmark_bars']:
        raise RuntimeError('benchmark diverged across strategies')
    return (json_text(payload)+'\n').encode('utf-8')


def validate(*, output: Path | None = None):
    raw = build()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError('backtest pinned hash mismatch: '+digest)
    if output is not None:
        output.write_bytes(raw)
    return {'matched':True, 'sha256':digest, 'candles':16,
            'strategies':['sma_trend','macd_trend'], 'closed_trades':{'sma_trend':4,'macd_trend':3},
            'network':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,help='optional canonical result JSON file')
    args = parser.parse_args()
    print(json.dumps(validate(output=args.output),sort_keys=True))

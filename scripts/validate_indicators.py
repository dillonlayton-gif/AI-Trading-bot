"""Offline deterministic Phase 3 gate, with a pinned canonical JSONL digest."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_bot.indicators import FeatureConfig, FeatureEngine, calculate
from trading_bot.market_data.models import MarketCandle, json_text

EXPECTED_SHA256 = 'a82bc6072b239f76d355d488881cbaffde6dd25188fbca96debfb2ff980b2062'


def canonical(rows):
    return ''.join(json_text(row.as_dict()) + '\n' for row in rows)


def validate(*, output: Path | None = None):
    fixture = json.loads((ROOT / 'data/indicator_sample.json').read_text(encoding='utf-8'))
    config = FeatureConfig(**fixture['config'])
    candles = [MarketCandle.from_dict(row) for row in fixture['candles']]
    batch = calculate(candles, config, finalized=True)
    engine = FeatureEngine(config)
    sequential = tuple(engine.update(candle, finalized=True) for candle in candles)
    text = canonical(batch)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if text != canonical(sequential) or text != canonical(calculate(candles, config, finalized=True)):
        raise RuntimeError('indicator replay diverged')
    if digest != EXPECTED_SHA256:
        raise RuntimeError('indicator regression hash mismatch: ' + digest)
    if output is not None:
        # Binary writing preserves canonical LF bytes on Windows too.
        output.write_bytes(text.encode('utf-8'))
    return {'rows':len(batch), 'features':len(batch[0].values), 'sha256':digest,
            'matched':True, 'network':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='optional canonical feature JSONL file')
    args = parser.parse_args()
    print(json.dumps(validate(output=args.output), sort_keys=True))

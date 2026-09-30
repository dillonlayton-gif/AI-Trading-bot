"""Pinned multi-session public-data paper simulation, restart and reconciliation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from decimal import Decimal
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trading_bot.forward import ForwardConfig, ForwardRunner
from trading_bot.indicators import FeatureConfig
from trading_bot.market_data.models import MarketCandle, timestamp, json_text

EXPECTED_SHA256 = 'f4de9bc4359c96bc3bca55aa3d1069d38dbaf474aee937937feaca2944e6abc7'


def config():
    return ForwardConfig('fixture', 'sma_trend', quantity=Decimal('0.5'), features=FeatureConfig(sma=2))


def action(runner, row):
    now = timestamp(row['as_of'])
    kind = row['kind']
    if kind == 'candle':
        return runner.consume(MarketCandle.from_dict(row['candle']), as_of=now,
                              received_at=timestamp(row['received_at']))
    if kind == 'health': return runner.set_health(row['status'], now)
    if kind == 'kill': return runner.kill(row['enabled'], row['reason'], now)
    return getattr(runner, kind)(now)


def execute(path, fixture, restart=False):
    runner = ForwardRunner(path, config())
    try:
        for index, row in enumerate(fixture['actions']):
            if restart and index == fixture['restart_before']:
                before = runner.summary()
                runner.close(); runner = ForwardRunner(path, config())
                if before != runner.summary(): raise RuntimeError('restart changed finalized session')
            action(runner, row)
        return runner.summary()
    finally: runner.close()


def build():
    fixture = json.loads((ROOT/'data/forward_sample.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as tmp:
        reference = execute(Path(tmp)/'reference', fixture)
        resumed = execute(Path(tmp)/'resume', fixture, True)
        if reference != resumed: raise RuntimeError('forward restart diverged')
        journal = ForwardRunner(Path(tmp)/'journal-replay', config())
        try:
            for event in reference['events']: action(journal, event['operation'])
            if reference != journal.summary(): raise RuntimeError('completed session journal replay diverged')
        finally: journal.close()
        expected = fixture['expected']
        for key, value in expected.items():
            if reference[key] != value: raise RuntimeError('known forward result mismatch: '+key)
        return (json_text(reference)+'\n').encode()


def validate(output=None):
    raw = build(); digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA256: raise RuntimeError('forward regression hash mismatch: '+digest)
    if output is not None: output.write_bytes(raw)
    summary = json.loads(raw)
    return {'matched': True, 'sha256': digest, 'sessions': summary['sessions'],
            'candles': summary['candles'], 'fills': summary['fills'], 'risk_rejections': summary['risk_rejections'],
            'cash': summary['cash'], 'reconciliation': True, 'journal_replay': True, 'network': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    print(json.dumps(validate(parser.parse_args().output), sort_keys=True))

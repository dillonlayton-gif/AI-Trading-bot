"""Deterministic risk-gated paper fixture with uninterrupted/restarted reconciliation."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from trading_bot.recovery import DurablePaperLedger,OrderIntent,RiskLimits
from trading_bot.market_data.models import MarketCandle,json_text

EXPECTED_SHA256='3b0af0752d2265c752125372a590a1883e3226fdbecd955eea1309d8bad8853b'


def execute(path,fixture,cut=None):
    ledger=DurablePaperLedger(path,Decimal(fixture['initial_cash']),RiskLimits())
    try:
        for i,row in enumerate(fixture['rows']):
            if i==cut:
                before=ledger.reconcile().serialize()
                ledger.close()
                ledger=DurablePaperLedger(path,Decimal('999999'),RiskLimits())
                if before!=ledger.reconcile().serialize():
                    raise RuntimeError('restart changed finalized state/events')
            c=MarketCandle.from_dict(row['candle']);now=c.start+timedelta(seconds=c.seconds)
            if 'kill' in row:
                ledger.set_kill(**row['kill'],as_of=now)
            kwargs=dict(as_of=now,received_at=now,health=row['health'],finalized=True)
            if row['intent'] is None:ledger.mark(c,**kwargs)
            else:
                data=row['intent']
                ledger.submit(OrderIntent(data['intent_id'],data['product'],data['side'],Decimal(data['quantity'])),c,**kwargs)
        return dict(reconciliation=json.loads(ledger.reconcile().serialize()),state=ledger.snapshot(),
                    events=[json.loads(event) for event in ledger.events()])
    finally:ledger.close()


def build():
    fixture=json.loads((ROOT/'data/recovery_sample.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as temp:
        reference=execute(Path(temp)/'reference.db',fixture)
        resumed=execute(Path(temp)/'resumed.db',fixture,fixture['restart_after'])
        if reference!=resumed:raise RuntimeError('restart/reconciliation diverged')
        state=reference['state']
        if state['positions'] or state['kill'] or state['daily_locked'] or state['drawdown_locked']:
            raise RuntimeError('unexpected final position/risk state')
        if 'expected' in fixture:
            if any(state[k]!=v for k,v in fixture['expected']['state'].items()):
                raise RuntimeError('known accounting values mismatch')
            reasons=[e['decision']['reason'] for e in reference['events']]
            if reasons!=fixture['expected']['reasons']:
                raise RuntimeError('known risk decisions mismatch')
        return (json_text(reference)+'\n').encode('utf-8')


def validate(*,output:Path|None=None):
    raw=build();digest=hashlib.sha256(raw).hexdigest()
    if digest!=EXPECTED_SHA256:raise RuntimeError('reconciliation regression hash mismatch: '+digest)
    if output is not None:output.write_bytes(raw)
    payload=json.loads(raw)
    return dict(matched=True,sha256=digest,events=len(payload['events']),
                cash=payload['state']['cash'],realized_pnl=payload['state']['realized'],network=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,help='optional canonical result file')
    args=parser.parse_args();print(json.dumps(validate(output=args.output),sort_keys=True))

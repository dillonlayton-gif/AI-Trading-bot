"""Finalized-close paper simulation with an atomic restartable risk handoff."""
from __future__ import annotations
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from trading_bot.backtest import SMATrend, MACDTrend
from trading_bot.backtest.common import arithmetic, primitive
from trading_bot.indicators import FeatureConfig, FeatureEngine
from trading_bot.market_data.models import MarketCandle, DataError, json_text, timestamp, utc, product_id, decimal_text, GRANULARITIES
from trading_bot.recovery import DurablePaperLedger, RiskLimits, OrderIntent, IntegrityError

LOG = logging.getLogger(__name__)
HEALTH = {'healthy', 'warming', 'stale', 'degraded', 'disconnected', 'stopped', 'unavailable'}


@dataclass(frozen=True)
class ForwardConfig:
    run_id: str
    strategy: str
    product: str = 'BTC-USD'
    seconds: int = 300
    quantity: Decimal = Decimal('0.0005')
    initial_cash: Decimal = Decimal('10000')
    finalization_grace: int = 90
    features: FeatureConfig = field(default_factory=FeatureConfig)
    limits: RiskLimits = field(default_factory=RiskLimits)

    def __post_init__(self):
        if not isinstance(self.run_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,50}', self.run_id):
            raise DataError('stable run identifier required')
        if self.strategy not in ('sma_trend', 'macd_trend'):
            raise DataError('explicit supported strategy required')
        product_id(self.product)
        if type(self.seconds) is not int or self.seconds not in GRANULARITIES or self.seconds < 300:
            raise DataError('forward timeframe must be a supported multiple of 300 seconds')
        for x in (self.quantity, self.initial_cash):
            if not isinstance(x, Decimal) or not x.is_finite() or x <= 0:
                raise DataError('positive finite Decimal configuration required')
        if type(self.finalization_grace) is not int or not 1 <= self.finalization_grace <= 300:
            raise DataError('finalization grace must be 1..300 seconds')
        if not isinstance(self.features, FeatureConfig) or self.features.gap_policy != 'reject':
            raise DataError('strict gap-rejecting feature configuration required')
        if not isinstance(self.limits, RiskLimits):
            raise DataError('fixed typed risk limits required')


def aggregate(candles, seconds=300):
    """Aggregate only complete, contiguous UTC-aligned finalized 5m groups.

    A partial leading/trailing group is unavailable, never synthesized. Gaps
    inside the supplied history are errors even when no trade would result.
    """
    if type(seconds) is not int or seconds not in GRANULARITIES or seconds < 300:
        raise DataError('unsupported forward timeframe')
    previous = None
    group = []
    for candle in candles:
        if not isinstance(candle, MarketCandle) or candle.seconds != 300:
            raise DataError('normalized five-minute candles required')
        if previous and (candle.product != previous.product or candle.epoch != previous.end_epoch):
            raise DataError('market history gap or mixed products')
        previous = candle
        if candle.epoch % seconds == 0:
            group = []
        elif not group:
            continue
        group.append(candle)
        if len(group) == seconds//300:
            with arithmetic():
                result = MarketCandle(candle.product, group[0].start, seconds, group[0].open,
                                      max(c.high for c in group), min(c.low for c in group),
                                      candle.close, sum((c.volume for c in group), Decimal(0)))
            yield result
            group = []


class Lease:
    """OS-held single-writer lease, released automatically on process death."""
    def __init__(self, path):
        self.file = open(path, 'a+b')
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            self.file.close()
            raise IntegrityError('forward run already owned or lease unavailable')

    def close(self):
        self.file.close()


class ForwardRunner:
    """One writer; hash-chained immutable operations and replay-derived features.

    A prepared operation is committed before invoking the Phase 5 gate. On
    restart, exactly zero or one matching risk event is permissible. An existing
    matching event is adopted, never resubmitted. Any ambiguity fails closed.
    """
    def __init__(self, directory: Path, config: ForwardConfig, *, crash=None, replay_recovery=False):
        self._config = config
        if type(replay_recovery) is not bool:
            raise DataError('explicit replay recovery flag required')
        self.replay_recovery = replay_recovery
        self.crash = crash or (lambda boundary: None)
        self.failed = False
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.lease = Lease(directory / 'runner.lock')
        self.db = None
        self.ledger = None
        try:
            self.db = sqlite3.connect(directory / 'session.db', isolation_level=None)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.metadata = json_text(primitive(config))
            if not tables:
                self.db.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE config(value TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TABLE pending(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, hash TEXT NOT NULL);
                CREATE TRIGGER event_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER event_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER config_update BEFORE UPDATE ON config BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER config_delete BEFORE DELETE ON config BEGIN SELECT RAISE(ABORT,'immutable'); END;
                COMMIT;''')
                with self.db:
                    self.db.execute('INSERT INTO config VALUES (?)', (self.metadata,))
            self.ledger = DurablePaperLedger(directory / 'risk.db', config.initial_cash, config.limits)
            self._read()
            self._restore()
            self._opening = True
            self._recover()
            self._opening = False
            self.reconcile()
        except BaseException:
            self.close()
            raise

    @property
    def config(self):
        return self._config

    def _read(self):
        if self.db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise IntegrityError('forward journal corrupt')
        if self.db.execute('SELECT value FROM config').fetchall() != [(self.metadata,)]:
            raise IntegrityError('forward configuration changed')
        definitions = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        for name, table, action in [('event_update','events','UPDATE'),('event_delete','events','DELETE'),
                                    ('config_update','config','UPDATE'),('config_delete','config','DELETE')]:
            expected = f"CREATE TRIGGER {name} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END"
            if definitions.get(name) != expected:
                raise IntegrityError('forward immutable journal protection missing')
        self.head = hashlib.sha256(self.metadata.encode()).hexdigest()
        self.records = []
        for seq, payload, previous, digest in self.db.execute('SELECT * FROM events ORDER BY seq'):
            expected = hashlib.sha256((self.head + payload).encode()).hexdigest()
            if seq != len(self.records)+1 or previous != self.head or digest != expected:
                raise IntegrityError('forward event hash/sequence mismatch')
            value = json.loads(payload)
            if json_text(value) != payload:
                raise IntegrityError('noncanonical forward event')
            self.records.append(value); self.head = digest

    def _restore(self):
        self.engine = FeatureEngine(self.config.features)
        self._strategy = SMATrend() if self.config.strategy == 'sma_trend' else MACDTrend()
        self.candles = {}
        self.pending_signal = None
        self.health = 'unavailable'
        self.active = False
        self.session = 0
        self.fault = False
        self.last_time = None
        for event in self.records:
            op = event['operation']
            when = timestamp(op['as_of'])
            if self.last_time and when < self.last_time:
                raise IntegrityError('forward clock moved backwards')
            self.last_time = when
            kind = op['kind']
            if kind == 'start':
                self.session += 1; self.active = True; self.health = 'warming'; self.pending_signal = None
                if op['session_id'] != f'{self.config.run_id}:{self.session:04d}':
                    raise IntegrityError('session identifier mismatch')
            elif kind == 'stop':
                self.active = False; self.health = 'stopped'; self.pending_signal = None
            elif kind == 'health':
                self.health = op['status']
                if self.health not in ('healthy', 'degraded'): self.pending_signal = None
            elif kind == 'fault':
                self.fault = True; self.pending_signal = None
            elif kind == 'candle':
                candle = MarketCandle.from_dict(op['candle'])
                features = self.engine.update(candle, finalized=True)
                signal = self._strategy.decide(candle, features)
                if primitive(features) != op['features'] or primitive(signal) != op['signal']:
                    raise IntegrityError('feature/strategy replay mismatch')
                self.candles[candle.epoch] = candle
                self.pending_signal = op['next_signal']
                decision = event.get('decision')
                if decision and decision['reason'] in ('data_gap', 'out_of_order_data', 'invalid_market_data', 'granularity_mismatch'):
                    self.fault = True; self.pending_signal = None
            elif kind != 'kill':
                raise IntegrityError('unknown forward operation')

    def _risk_anchor(self):
        report = self.ledger.reconcile()
        return {'events': report.events, 'head': report.head}

    def reconcile(self):
        self._read()
        expected = self.records[-1]['risk_after'] if self.records else self._empty_anchor()
        if self._risk_anchor() != expected:
            raise IntegrityError('risk ledger differs from forward journal')
        return {'matched': True, 'events': len(self.records), 'head': self.head,
                'risk': self._risk_anchor(), 'pending_operations': self.db.execute('SELECT count(*) FROM pending').fetchone()[0]}

    def _empty_anchor(self):
        # The initial head is reproducible from the risk ledger's immutable metadata.
        # A previously populated risk DB must not be attached to a fresh session.
        report = self.ledger.reconcile()
        if report.events:
            raise IntegrityError('orphan risk events')
        return {'events': 0, 'head': report.head}

    def _execute(self, request):
        if request['kind'] == 'kill':
            return self.ledger.set_kill(request['enabled'], request['reason'], as_of=timestamp(request['as_of']))
        c = MarketCandle.from_dict(request['candle'])
        kw = dict(as_of=timestamp(request['as_of']), received_at=timestamp(request['received_at']),
                  health=request['health'], finalized=request['finalized'])
        if request['kind'] == 'mark':
            return self.ledger.mark(c, **kw)
        i = request['intent']
        return self.ledger.submit(OrderIntent(i['intent_id'], i['product'], i['side'], Decimal(i['quantity'])), c, **kw)

    def _recover(self):
        rows = self.db.execute('SELECT payload,hash FROM pending').fetchall()
        if not rows: return
        if len(rows) != 1: raise IntegrityError('multiple prepared operations')
        if hashlib.sha256((self.head+rows[0][0]).encode()).hexdigest() != rows[0][1]:
            raise IntegrityError('prepared operation hash mismatch')
        staged = json.loads(rows[0][0])
        expected_before = self.records[-1]['risk_after'] if self.records else None
        before = staged['risk_before']
        if expected_before is not None and before != expected_before:
            raise IntegrityError('prepared risk anchor mismatch')
        if staged['forward_before'] != self.head:
            raise IntegrityError('prepared forward anchor mismatch')
        request = staged['request']
        anchor = self._risk_anchor()
        decision = None
        if anchor == before:
            if request is not None:
                if request['kind'] == 'intent' and not self.replay_recovery and getattr(self, '_opening', False):
                    raise IntegrityError('uncommitted intent on restart: live resume fails closed; offline replay investigation required')
                decision = primitive(self._execute(request))
                self.crash('after_risk_commit')
        elif request is not None and anchor['events'] == before['events'] + 1:
            actual = json.loads(self.ledger.events()[-1])
            if actual['request'] != request:
                raise IntegrityError('interrupted handoff differs from risk event')
            decision = actual['decision']
        else:
            raise IntegrityError('ambiguous interrupted operation')
        event = {'operation': staged['operation'], 'decision': decision, 'risk_after': self._risk_anchor(),
                 'accounting': self.ledger.snapshot(),
                 'lifecycle': ('filled' if decision and decision['fill'] else
                               'rejected' if decision and decision['intent_id'] else 'no_order')}
        payload = json_text(event)
        digest = hashlib.sha256((self.head + payload).encode()).hexdigest()
        self.crash('before_journal_commit')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO events VALUES (?,?,?,?)', (len(self.records)+1, payload, self.head, digest))
            self.db.execute('DELETE FROM pending')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK'); raise
        self._read(); self._restore()
        LOG.info('paper_event %s', payload)

    def _apply(self, operation, request=None):
        if self.failed: raise IntegrityError('runner failed closed; restart/reconcile required')
        if self.last_time and timestamp(operation['as_of']) < self.last_time:
            raise DataError('forward clock moved backwards')
        self.reconcile()
        staged = {'operation': operation, 'request': request, 'risk_before': self._risk_anchor(), 'forward_before': self.head}
        try:
            payload = json_text(staged)
            digest = hashlib.sha256((self.head+payload).encode()).hexdigest()
            self.db.execute('INSERT INTO pending VALUES (1,?,?)', (payload,digest))
            self.crash('after_prepare')
            self._recover()
        except BaseException:
            self.failed = True
            raise
        return self.records[-1]

    def start(self, now):
        if self.active: raise DataError('session already active')
        return self._apply({'kind': 'start', 'as_of': utc(now).isoformat(),
                            'session_id': f'{self.config.run_id}:{self.session+1:04d}'})

    def stop(self, now):
        if not self.active: return None
        return self._apply({'kind': 'stop', 'as_of': utc(now).isoformat()})

    def set_health(self, status, now):
        if status not in HEALTH: raise DataError('unknown feed health')
        if not self.active: raise DataError('inactive session')
        if status != self.health:
            return self._apply({'kind': 'health', 'status': status, 'as_of': utc(now).isoformat()})
        return None

    def kill(self, enabled, reason, now):
        request = {'kind': 'kill', 'enabled': enabled, 'reason': reason, 'as_of': utc(now).isoformat()}
        return self._apply(request, request)

    def consume(self, candle, *, as_of, received_at, finalized=True):
        if not self.active or self.fault: raise DataError('inactive or faulted paper session')
        if not isinstance(candle, MarketCandle) or finalized is not True:
            raise DataError('finalized normalized candle required')
        if candle.product != self.config.product or candle.seconds != self.config.seconds:
            raise DataError('forward product/timeframe mismatch')
        now, received = utc(as_of), utc(received_at)
        if received > now or received.timestamp() < candle.end_epoch or now.timestamp() < candle.end_epoch:
            raise DataError('invalid finalized receipt time')
        if self.last_time and now < self.last_time:
            raise DataError('forward clock moved backwards')
        prior = self.candles.get(candle.epoch)
        if prior is not None:
            if prior != candle:
                self._apply({'kind': 'fault', 'as_of': now.isoformat(), 'reason': 'finalized_conflict'})
                raise IntegrityError('conflicting finalized duplicate')
            return None
        if self.candles and candle.epoch != max(self.candles)+candle.seconds:
            self._apply({'kind': 'fault', 'as_of': now.isoformat(), 'reason': 'market_gap_or_order'})
            raise DataError('missing or out-of-order finalized candle')
        fresh = (self.health == 'healthy' and
                 0 <= now.timestamp()-candle.end_epoch <= self.config.finalization_grace and
                 0 <= (now-received).total_seconds() <= self.config.finalization_grace)
        previous_signal = self.pending_signal
        # Replay rebuilding makes finalized feature rows immutable. The next
        # decision cannot influence this candle's fill.
        features = self.engine.update(candle, finalized=True)
        signal = self._strategy.decide(candle, features)
        packet = {'candle': candle.as_dict(), 'as_of': now.isoformat(), 'received_at': received.isoformat(),
                  'health': self.health, 'finalized': True}
        request = dict(kind='mark', **packet)
        state = self.ledger.snapshot()
        held = Decimal(state['positions'].get(candle.product, {}).get('quantity', '0'))
        if fresh and previous_signal is not None and not (state['daily_locked'] or state['drawdown_locked']):
            target = previous_signal['target']
            side = 'BUY' if target == 'long' and held == 0 else 'SELL' if target == 'flat' and held > 0 else None
            if side:
                qty = self.config.quantity if side == 'BUY' else held
                request = dict(kind='intent', intent={'intent_id': f'{self.config.run_id}:{candle.epoch}:{side}',
                            'product': candle.product, 'side': side, 'quantity': decimal_text(qty)}, **packet)
        next_signal = primitive(signal) if fresh else None
        op = {'kind': 'candle', 'as_of': now.isoformat(), 'received_at': received.isoformat(),
              'candle': candle.as_dict(), 'features': primitive(features), 'signal': primitive(signal),
              'next_signal': next_signal, 'fresh': fresh, 'health': self.health,
              'execution_model': 'prior_signal_next_finalized_close'}
        return self._apply(op, request)

    def summary(self):
        report = self.reconcile()
        state = self.ledger.snapshot()
        decisions = [e['decision'] for e in self.records if e['decision']]
        fills = [d for d in decisions if d['fill']]
        daily = {}
        for event in self.records:
            day = event['operation']['as_of'][:10]
            accounting = event['accounting']
            bucket = daily.setdefault(day, {'candles': 0, 'fills': 0, 'risk_rejections': 0,
                                           'opening_equity': accounting['equity'],
                                           'opening_realized': accounting['realized']})
            bucket['closing_equity'] = accounting['equity']
            with arithmetic():
                bucket['equity_change'] = decimal_text(Decimal(accounting['equity'])-Decimal(bucket['opening_equity']))
                bucket['realized_pnl'] = decimal_text(Decimal(accounting['realized'])-Decimal(bucket['opening_realized']))
            d = event['decision']
            bucket['candles'] += event['operation']['kind'] == 'candle'
            bucket['fills'] += bool(d and d['fill'])
            bucket['risk_rejections'] += bool(d and d['intent_id'] and not d['accepted'])
        with arithmetic():
            drawdown = (Decimal(state['peak'])-Decimal(state['equity']))/Decimal(state['peak'])
            max_drawdown = max(((Decimal(e['accounting']['peak'])-Decimal(e['accounting']['equity']))/Decimal(e['accounting']['peak']) for e in self.records), default=Decimal(0))
        return {'run_id': self.config.run_id, 'sessions': self.session, 'active': self.active,
                'health': self.health, 'fault': self.fault, 'candles': len(self.candles),
                'fills': len(fills), 'risk_rejections': sum(bool(d['intent_id'] and not d['accepted']) for d in decisions),
                'pauses': sum(e['operation']['kind'] == 'health' and e['operation']['status'] != 'healthy' for e in self.records),
                'cash': state['cash'], 'equity': state['equity'], 'realized_pnl': state['realized'],
                'unrealized_pnl': state['unrealized'], 'peak_equity': state['peak'], 'drawdown': decimal_text(drawdown),
                'max_drawdown': decimal_text(max_drawdown), 'daily': daily, 'state': state, 'reconciliation': report, 'events': self.records}

    def close(self):
        if self.ledger is not None: self.ledger.close(); self.ledger = None
        if self.db is not None: self.db.close(); self.db = None
        if getattr(self, 'lease', None) is not None: self.lease.close(); self.lease = None

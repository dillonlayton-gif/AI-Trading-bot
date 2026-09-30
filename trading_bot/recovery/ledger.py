"""Deterministic risk-gated paper ledger with atomic, replay-verified persistence."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, DecimalException
from pathlib import Path

from trading_bot.backtest.common import arithmetic, primitive
from trading_bot.core import Limits
from trading_bot.market_data.models import DataError, MarketCandle, json_text, timestamp, utc, decimal_text

D = Decimal


class IntegrityError(DataError):
    """Persistent state cannot be trusted; do not submit further intents."""


@dataclass(frozen=True)
class RiskLimits:
    max_order_quote: Decimal = Limits().max_order_quote
    max_position_fraction: Decimal = Limits().max_position_fraction
    max_portfolio_fraction: Decimal = D('0.20')
    daily_loss_fraction: Decimal = Limits().daily_loss_fraction
    max_drawdown_fraction: Decimal = D('0.10')
    fee_fraction: Decimal = Limits().fee_fraction
    slippage_fraction: Decimal = Limits().slippage_fraction
    max_open_positions: int = 1
    max_data_age_seconds: int = Limits().max_data_age_seconds

    def __post_init__(self):
        for f in fields(self):
            x = getattr(self, f.name)
            if f.name in ('max_open_positions', 'max_data_age_seconds'):
                if type(x) is not int or x < 1:
                    raise DataError('positive integer risk limit required')
            elif not isinstance(x, Decimal) or not x.is_finite():
                raise DataError('finite Decimal risk limit required')
        if self.max_order_quote <= 0:
            raise DataError('positive order limit required')
        for name in ('max_position_fraction','max_portfolio_fraction'):
            if not 0 < getattr(self,name) <= 1:
                raise DataError('exposure fraction must be in (0,1]')
        for name in ('daily_loss_fraction','max_drawdown_fraction'):
            if not 0 < getattr(self,name) < 1:
                raise DataError('loss fraction must be in (0,1)')
        for name in ('fee_fraction','slippage_fraction'):
            if not 0 <= getattr(self,name) < 1:
                raise DataError('cost fraction must be in [0,1)')


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    product: str
    side: str
    quantity: Decimal

    def __post_init__(self):
        if not isinstance(self.intent_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',self.intent_id):
            raise DataError('stable intent identifier required')
        if not isinstance(self.product,str) or not isinstance(self.side,str) or not isinstance(self.quantity,Decimal):
            raise DataError('typed intent fields required')


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    intent_id: str | None
    fill: tuple[tuple[str,str], ...] = ()


@dataclass(frozen=True)
class Reconciliation:
    matched: bool
    events: int
    head: str
    state_json: str
    pending_operations: int = 0

    def serialize(self):
        return json_text(primitive(self))+'\n'


def _limits(data):
    return RiskLimits(**{f.name:(data[f.name] if f.name in ('max_open_positions','max_data_age_seconds')
                                else D(data[f.name])) for f in fields(RiskLimits)})


def _initial(cash):
    return dict(cash=cash, positions={}, realized=D(0), marks={}, peak=cash,
                equity=cash, unrealized=D(0), day='', day_start=cash, daily_locked=False, drawdown_locked=False,
                kill=False, kill_reason='', last_as_of='', intents={})


def _equity(state):
    return state['cash'] + sum((p['quantity']*state['marks'][product]['price']
                               for product,p in state['positions'].items()),D(0))


def _accounting(state):
    state['equity'] = _equity(state)
    state['unrealized'] = sum((p['quantity']*state['marks'][product]['price']-p['basis']
                               for product,p in state['positions'].items()),D(0))


def _mark(state, request, limits):
    """Only healthy, finalized, fresh monotone packets advance accounting marks."""
    try:
        c = MarketCandle.from_dict(request['candle'])
        now, received = timestamp(request['as_of']), timestamp(request['received_at'])
        if request['finalized'] is not True:
            return 'provisional_data'
        if request['health'] != 'healthy':
            return 'unhealthy_feed'
        end = c.end_epoch
        if received > now or now.timestamp() < end or received.timestamp() < end:
            return 'future_data'
        if (now.timestamp()-end > limits.max_data_age_seconds or
                (now-received).total_seconds() > limits.max_data_age_seconds):
            return 'stale_data'
        if state['last_as_of'] and now < timestamp(state['last_as_of']):
            return 'out_of_order_data'
        previous = state['marks'].get(c.product)
        if previous:
            if c.seconds != previous['seconds']:
                return 'granularity_mismatch'
            if c.epoch <= previous['epoch']:
                return 'duplicate_data' if c.epoch == previous['epoch'] else 'out_of_order_data'
            if c.epoch != previous['epoch']+c.seconds:
                return 'data_gap'
        state['marks'][c.product] = dict(price=c.close, epoch=c.epoch, seconds=c.seconds,
                                          end=c.end_epoch)
        state['last_as_of'] = now.isoformat()
        equity = _equity(state)
        day = now.date().isoformat()
        if day != state['day']:
            state['day'],state['day_start'],state['daily_locked'] = day,equity,False
        state['peak'] = max(state['peak'],equity)
        state['daily_locked'] |= equity <= state['day_start']*(1-limits.daily_loss_fraction)
        state['drawdown_locked'] |= equity <= state['peak']*(1-limits.max_drawdown_fraction)
        _accounting(state)
        return None
    except (KeyError,TypeError,ValueError,OverflowError):
        return 'invalid_market_data'


def _reduce(state, request, limits):
    """The only fill-producing reducer; also used to verify every audit decision."""
    s = deepcopy(state)
    if request['kind'] == 'kill':
        if type(request['enabled']) is not bool or not isinstance(request['reason'],str) or not request['reason']:
            raise DataError('explicit kill-switch state and reason required')
        now = timestamp(request['as_of'])
        if s['last_as_of'] and now < timestamp(s['last_as_of']):
            raise DataError('kill-switch time precedes ledger')
        s['kill'],s['kill_reason'] = request['enabled'],request['reason']
        s['last_as_of'] = now.isoformat()
        return s,Decision(True,'kill_enabled' if s['kill'] else 'kill_disabled',None)
    if request['kind'] not in ('mark','intent'):
        raise DataError('unknown operation')
    intent = request.get('intent')
    identifier = intent['intent_id'] if intent else None
    fingerprint = json_text(intent) if intent else None
    if identifier in s['intents']:
        reason = 'duplicate_intent' if s['intents'][identifier]['fingerprint']==fingerprint else 'intent_id_conflict'
        return s,Decision(False,reason,identifier)
    market_reason = _mark(s,request,limits)
    if intent is None:
        return s,Decision(market_reason is None,market_reason or 'marked',None)
    reason, fill = None, {}
    if s['kill']:
        reason = 'kill_switch'
    elif market_reason:
        reason = market_reason
    elif s['drawdown_locked']:
        reason = 'drawdown_limit'
    elif s['daily_locked']:
        reason = 'daily_loss_limit'
    else:
        try:
            c = MarketCandle.from_dict(request['candle'])
            qty = D(intent['quantity'])
            pos = s['positions'].get(c.product,dict(quantity=D(0),basis=D(0)))
            equity = _equity(s)
            price = c.close*(1+limits.slippage_fraction if intent['side']=='BUY' else 1-limits.slippage_fraction)
            notional, fee = qty*price, qty*price*limits.fee_fraction
            if intent['product'] != c.product:
                reason = 'product_mismatch'
            elif intent['side'] not in ('BUY','SELL') or not qty.is_finite() or qty <= 0:
                reason = 'invalid_intent'
            elif equity <= 0:
                reason = 'no_equity'
            elif any(timestamp(request['as_of']).timestamp()-s['marks'][p]['end'] > limits.max_data_age_seconds for p in s['positions']):
                reason = 'stale_portfolio_data'
            elif notional > limits.max_order_quote:
                reason = 'order_limit'
            elif intent['side']=='SELL' and qty > pos['quantity']:
                reason = 'insufficient_units'
            elif intent['side']=='BUY' and notional+fee > s['cash']:
                reason = 'insufficient_cash'
            elif intent['side']=='BUY' and c.product not in s['positions'] and len(s['positions']) >= limits.max_open_positions:
                reason = 'open_position_limit'
            else:
                next_units = pos['quantity']+(qty if intent['side']=='BUY' else -qty)
                next_cash = s['cash']+(-(notional+fee) if intent['side']=='BUY' else notional-fee)
                next_equity = equity+(next_cash-s['cash'])+(next_units-pos['quantity'])*c.close
                exposure = sum((p['quantity']*s['marks'][name]['price'] for name,p in s['positions'].items()),D(0))+(next_units-pos['quantity'])*c.close
                if intent['side']=='BUY' and next_units*c.close > next_equity*limits.max_position_fraction:
                    reason = 'position_limit'
                elif intent['side']=='BUY' and exposure > next_equity*limits.max_portfolio_fraction:
                    reason = 'portfolio_exposure_limit'
                elif next_equity <= s['day_start']*(1-limits.daily_loss_fraction):
                    reason = 'daily_loss_limit';s['daily_locked'] = True
                elif next_equity <= s['peak']*(1-limits.max_drawdown_fraction):
                    reason = 'drawdown_limit';s['drawdown_locked'] = True
                else:
                    basis = pos['basis']+notional+fee if intent['side']=='BUY' else pos['basis']*(1-qty/pos['quantity'])
                    if intent['side']=='SELL':
                        s['realized'] += notional-fee-pos['basis']*qty/pos['quantity']
                    s['cash'] = next_cash
                    if next_units:
                        s['positions'][c.product] = dict(quantity=next_units,basis=basis)
                    else:
                        s['positions'].pop(c.product,None)
                    fill = primitive(dict(side=intent['side'],quantity=qty,price=price,fee=fee,
                                          product=c.product,cash=next_cash,units=next_units))
        except DecimalException:
            reason = 'invalid_intent'
    decision = Decision(reason is None,reason or 'accepted',identifier,tuple(sorted(fill.items())))
    s['intents'][identifier] = dict(fingerprint=fingerprint,accepted=decision.accepted,reason=decision.reason)
    _accounting(s)
    return s,decision


class DurablePaperLedger:
    """Single SQLite writer; fresh schema only. Startup and every write reconcile.

    No public cash/position setter or fill method. WAL/FULL and BEGIN IMMEDIATE
    commit request, verified decision, hash-chain event and projection together.
    Failed reconciliation latches this instance closed until operator repair.
    """
    def __init__(self,path:Path,initial_cash:Decimal=D('10000'),limits:RiskLimits|None=None):
        self._limits = limits if limits is not None else RiskLimits()
        if not isinstance(self._limits,RiskLimits) or not isinstance(initial_cash,Decimal) or not initial_cash.is_finite() or initial_cash<=0:
            raise DataError('valid initial cash and fixed risk limits required')
        self._failed = False
        self._db = sqlite3.connect(path,isolation_level=None,timeout=10)
        try:
            self._db.execute('PRAGMA journal_mode=WAL');self._db.execute('PRAGMA synchronous=FULL')
            tables={r[0] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                self._db.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE projection(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
                CREATE TABLE audit(seq INTEGER PRIMARY KEY,payload TEXT NOT NULL,previous TEXT NOT NULL,hash TEXT NOT NULL);
                CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
                CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
                CREATE TRIGGER meta_no_update BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
                CREATE TRIGGER meta_no_delete BEFORE DELETE ON metadata BEGIN SELECT RAISE(ABORT,'immutable metadata'); END;
                ''')
                with arithmetic():
                    state = _initial(initial_cash)
                self._db.execute('BEGIN IMMEDIATE')
                try:
                    for k,v in {'schema':'1','initial_cash':decimal_text(initial_cash),'limits':json_text(primitive(self._limits))}.items():
                        self._db.execute('INSERT INTO metadata VALUES (?,?)',(k,v))
                    self._db.execute('INSERT INTO projection VALUES (1,?)',(json_text(primitive(state)),))
                    self._db.execute('COMMIT')
                except BaseException:
                    self._db.execute('ROLLBACK');raise
            self.reconcile()
        except BaseException:
            self._db.close();raise

    @property
    def limits(self):
        return self._limits

    def _reconstruct(self):
        if self._failed:
            raise IntegrityError('ledger instance is fail-closed')
        try:
            if self._db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise IntegrityError('SQLite integrity failure')
            required={'audit_no_update','audit_no_delete','meta_no_update','meta_no_delete'}
            triggers=dict(self._db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            if not required<=triggers.keys():
                raise IntegrityError('immutability protections missing')
            for name in required:
                table = 'audit' if name.startswith('audit') else 'metadata'
                operation = 'UPDATE' if name.endswith('update') else 'DELETE'
                label = 'immutable audit' if table=='audit' else 'immutable metadata'
                expected = f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'{label}'); END"
                normalize = lambda sql: ' '.join(sql.split()).lower().rstrip(';')
                if normalize(triggers[name]) != normalize(expected):
                    raise IntegrityError('immutability protection definition changed')
            meta=dict(self._db.execute('SELECT key,value FROM metadata'))
            if set(meta)!={'schema','initial_cash','limits'} or meta['schema']!='1':
                raise IntegrityError('metadata schema mismatch')
            limits=_limits(json.loads(meta['limits']))
            if json_text(primitive(limits))!=meta['limits'] or limits!=self._limits:
                raise IntegrityError('risk configuration mismatch')
            cash=D(meta['initial_cash'])
            if not cash.is_finite() or cash<=0:
                raise IntegrityError('invalid persisted capital')
            state=_initial(cash);head=hashlib.sha256(json_text(meta).encode()).hexdigest();count=0
            with arithmetic():
                for seq,payload,previous,digest in self._db.execute('SELECT seq,payload,previous,hash FROM audit ORDER BY seq'):
                    count+=1
                    if seq!=count or previous!=head or hashlib.sha256((head+payload).encode()).hexdigest()!=digest:
                        raise IntegrityError('audit chain mismatch')
                    event=json.loads(payload)
                    if set(event)!={'request','decision'} or json_text(event)!=payload:
                        raise IntegrityError('audit event schema mismatch')
                    state,decision=_reduce(state,event['request'],limits)
                    if primitive(decision)!=event['decision']:
                        raise IntegrityError('risk decision/fill mismatch')
                    head=digest
            rows=self._db.execute('SELECT id,value FROM projection').fetchall()
            expected=json_text(primitive(state))
            if rows!=[(1,expected)]:
                raise IntegrityError('projection differs from reconstructed ledger')
            return state,Reconciliation(True,count,head,expected)
        except Exception as exc:
            self._failed=True
            if isinstance(exc,IntegrityError):raise
            raise IntegrityError('unrecoverable ledger inconsistency') from exc

    def reconcile(self):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            _,report=self._reconstruct()
            self._db.execute('COMMIT');return report
        except BaseException:
            self._db.execute('ROLLBACK');raise

    def _apply(self,request):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            state,report=self._reconstruct()
            with arithmetic():state,decision=_reduce(state,request,self._limits)
            payload=json_text(dict(request=request,decision=primitive(decision)))
            digest=hashlib.sha256((report.head+payload).encode()).hexdigest()
            self._db.execute('INSERT INTO audit VALUES (?,?,?,?)',(report.events+1,payload,report.head,digest))
            self._db.execute('UPDATE projection SET value=? WHERE id=1',(json_text(primitive(state)),))
            self._db.execute('COMMIT');return decision
        except BaseException:
            self._db.execute('ROLLBACK');raise

    @staticmethod
    def _packet(candle,as_of,received_at,health,finalized):
        if not isinstance(candle,MarketCandle) or not isinstance(health,str) or type(finalized) is not bool:
            raise DataError('typed normalized market packet required')
        return dict(candle=candle.as_dict(),as_of=utc(as_of).isoformat(),
                    received_at=utc(received_at).isoformat(),health=health,finalized=finalized)

    def submit(self,intent:OrderIntent,candle:MarketCandle,*,as_of:datetime,received_at:datetime,
               health:str,finalized:bool):
        if not isinstance(intent,OrderIntent):raise DataError('OrderIntent required')
        request=dict(kind='intent',intent=dict(intent_id=intent.intent_id,product=intent.product,
                                             side=intent.side,quantity=decimal_text(intent.quantity) if intent.quantity.is_finite() else str(intent.quantity)),
                     **self._packet(candle,as_of,received_at,health,finalized))
        return self._apply(request)

    def mark(self,candle:MarketCandle,*,as_of:datetime,received_at:datetime,health:str,finalized:bool):
        return self._apply(dict(kind='mark',**self._packet(candle,as_of,received_at,health,finalized)))

    def set_kill(self,enabled:bool,reason:str,*,as_of:datetime):
        return self._apply(dict(kind='kill',enabled=enabled,reason=reason,as_of=utc(as_of).isoformat()))

    def snapshot(self):
        return json.loads(self.reconcile().state_json)

    def events(self):
        self.reconcile()
        return tuple(row[0] for row in self._db.execute('SELECT payload FROM audit ORDER BY seq'))

    def close(self):
        self._db.close()

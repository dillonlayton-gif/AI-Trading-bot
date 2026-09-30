"""Forward-paper causality, risk routing, sessions and crash handoff regression."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from trading_bot.forward import ForwardConfig, ForwardRunner, aggregate
from trading_bot.forward.cli import forward
from trading_bot.indicators import FeatureConfig
from trading_bot.market_data.models import MarketCandle, DataError
from trading_bot.market_data.store import MarketStore
from trading_bot.recovery import IntegrityError, RiskLimits
from scripts.validate_forward import build, validate, execute, action, ROOT

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CONFIG = ForwardConfig('test', 'sma_trend', quantity=D('.5'), features=FeatureConfig(sma=2))


def candle(i=0, price=100, **kw):
    p = D(str(price))
    return MarketCandle(kw.get('product','BTC-USD'), BASE+timedelta(seconds=300*i),
                        kw.get('seconds',300), p,p,p,p,D(10))


def end(c): return c.start+timedelta(seconds=c.seconds)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'run'
        self.runner = ForwardRunner(self.path, CONFIG)
        self.runner.start(BASE); self.runner.set_health('healthy', BASE)

    def tearDown(self):
        self.runner.close(); self.tmp.cleanup()

    def feed(self, i, price=100, **kw):
        c = candle(i,price)
        return self.runner.consume(c,as_of=kw.get('as_of',end(c)),received_at=kw.get('received_at',end(c)))

    def restart(self):
        self.runner.close(); self.runner=ForwardRunner(self.path, CONFIG, replay_recovery=True)

    def rise(self):
        self.feed(0,100); self.feed(1,110); return self.feed(2,120)

    def test_warmup_no_order(self):
        self.feed(0); self.assertEqual(self.runner.summary()['fills'],0)
        self.assertEqual(self.runner.pending_signal['target'],'flat')

    def test_prior_signal_fills_next_finalized_close(self):
        event=self.rise(); fill=dict(event['decision']['fill'])
        self.assertTrue(event['decision']['accepted']); self.assertEqual(fill['price'],'120.24')
        self.assertEqual(self.runner.summary()['fills'],1)

    def test_no_current_signal_fill(self):
        self.feed(0); self.feed(1,110)
        self.assertEqual(self.runner.summary()['fills'],0)
        self.feed(2,90)  # prior long still buys; current flat only acts later
        self.assertEqual(self.runner.summary()['fills'],1)
        self.feed(3,80); self.assertEqual(self.runner.summary()['fills'],2)

    def test_flat_no_trades(self):
        for i in range(5): self.feed(i)
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_falling_no_trades(self):
        for i,p in enumerate([100,90,80,70]): self.feed(i,p)
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_every_fill_has_risk_audit(self):
        self.rise()
        events=[json.loads(x) for x in self.runner.ledger.events()]
        self.assertEqual(sum(bool(e['decision']['fill']) for e in events),self.runner.summary()['fills'])
        self.assertTrue(all(e['request']['kind']=='intent' for e in events if e['decision']['fill']))

    def test_fees_slippage_exact(self):
        self.rise(); s=self.runner.summary()
        self.assertEqual(s['cash'],'9939.51928')
        self.assertEqual(s['unrealized_pnl'],'-0.48072')

    def test_position_exit_realized(self):
        self.rise(); self.feed(3,90); self.feed(4,80)
        s=self.runner.summary(); self.assertFalse(s['state']['positions'])
        self.assertEqual(s['realized_pnl'],'-20.80024')

    def test_unhealthy_cannot_fill(self):
        self.feed(0); self.feed(1,110)
        self.runner.set_health('degraded',end(candle(1)))
        event=self.feed(2,120)
        self.assertEqual(event['decision']['reason'],'unhealthy_feed')
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_stale_candle_cannot_fill_even_healthy(self):
        self.feed(0); self.feed(1,110)
        self.feed(2,120,as_of=end(candle(2))+timedelta(seconds=91))
        self.assertEqual(self.runner.summary()['fills'],0)
        self.assertIsNone(self.runner.pending_signal)

    def test_stale_receipt_cannot_fill(self):
        self.feed(0); self.feed(1,110)
        self.feed(2,120,as_of=end(candle(2))+timedelta(seconds=91))
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_disconnect_clears_pending(self):
        self.feed(0); self.feed(1,110)
        self.runner.set_health('disconnected',end(candle(1)))
        self.assertIsNone(self.runner.pending_signal)
        self.runner.set_health('healthy',end(candle(2)))
        self.feed(2,120)
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_restart_open_position(self):
        self.rise(); before=self.runner.summary(); self.restart()
        self.assertEqual(before,self.runner.summary())
        self.feed(3,90); self.feed(4,80)
        self.assertEqual(self.runner.summary()['fills'],2)

    def test_duplicate_delivery_no_event(self):
        self.rise(); before=self.runner.summary(); self.feed(2,120)
        self.assertEqual(before,self.runner.summary())

    def test_duplicate_after_restart(self):
        self.rise(); self.restart(); before=self.runner.summary(); self.feed(2,120)
        self.assertEqual(before,self.runner.summary())

    def test_conflicting_duplicate_rejected(self):
        self.feed(0)
        with self.assertRaises(IntegrityError): self.feed(0,101)
        self.assertTrue(self.runner.fault)
        self.restart();self.assertTrue(self.runner.fault)

    def test_gap_latches_fault(self):
        self.feed(0)
        with self.assertRaises(DataError): self.feed(2)
        self.assertTrue(self.runner.fault)
        self.restart(); self.assertTrue(self.runner.fault)
        with self.assertRaises(DataError): self.feed(1)

    def test_product_mismatch(self):
        c=candle(product='ETH-USD')
        with self.assertRaises(DataError): self.runner.consume(c,as_of=end(c),received_at=end(c))

    def test_provisional_rejected(self):
        c=candle()
        with self.assertRaises(DataError): self.runner.consume(c,as_of=end(c),received_at=end(c),finalized=False)

    def test_future_receipt_rejected(self):
        with self.assertRaises(DataError): self.feed(0,received_at=end(candle())+timedelta(seconds=1))

    def test_preclose_receipt_rejected(self):
        with self.assertRaises(DataError): self.feed(0,received_at=BASE)

    def test_backward_clock_rejected_without_mutation(self):
        self.feed(0); self.runner.set_health('healthy',end(candle(2)))
        # status unchanged is a no-op; explicit transition persists the clock
        self.runner.set_health('stale',end(candle(2)))
        with self.assertRaises(DataError): self.feed(1)
        self.assertEqual(len(self.runner.candles),1)

    def test_kill_persists_and_rejects(self):
        self.feed(0); self.feed(1,110); self.runner.kill(True,'operator',end(candle(1)))
        self.restart(); e=self.feed(2,120)
        self.assertEqual(e['decision']['reason'],'kill_switch')
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_order_limit_rejection(self):
        self.feed(0); self.feed(1,110); e=self.feed(2,300)
        self.assertEqual(e['decision']['reason'],'order_limit')

    def test_daily_loss_lockout(self):
        self.runner.close()
        config=replace(CONFIG,limits=replace(CONFIG.limits,daily_loss_fraction=D('.00001')))
        self.path=Path(self.tmp.name)/'daily'; self.runner=ForwardRunner(self.path,config)
        self.runner.start(BASE);self.runner.set_health('healthy',BASE)
        self.rise(); self.assertTrue(self.runner.summary()['state']['daily_locked'])
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_drawdown_lockout(self):
        self.runner.close()
        config=replace(CONFIG,limits=replace(CONFIG.limits,max_drawdown_fraction=D('.00001')))
        self.path=Path(self.tmp.name)/'drawdown'; self.runner=ForwardRunner(self.path,config)
        self.runner.start(BASE);self.runner.set_health('healthy',BASE)
        self.rise();self.assertTrue(self.runner.summary()['state']['drawdown_locked'])
        self.assertEqual(self.runner.summary()['fills'],0)

    def test_runtime_config_readonly(self):
        with self.assertRaises(AttributeError): self.runner.config=replace(CONFIG,strategy='macd_trend')

    def test_config_change_on_restart_rejected(self):
        self.runner.close()
        with self.assertRaises(IntegrityError): ForwardRunner(self.path,replace(CONFIG,quantity=D('.6')))
        self.runner=ForwardRunner(self.path,CONFIG)

    def test_single_writer(self):
        with self.assertRaises(IntegrityError): ForwardRunner(self.path,CONFIG)

    def test_orphan_risk_event_rejected(self):
        self.runner.ledger.set_kill(True,'outside writer',as_of=BASE)
        with self.assertRaises(IntegrityError): self.runner.reconcile()

    def test_journal_tamper_rejected(self):
        self.runner.db.execute('DROP TRIGGER event_update')
        with self.assertRaises(IntegrityError): self.runner.reconcile()

    def test_risk_corruption_rejected(self):
        db=sqlite3.connect(self.path/'risk.db');db.execute("UPDATE projection SET value='{}'");db.commit();db.close()
        with self.assertRaises(IntegrityError): self.runner.reconcile()

    def test_graceful_stop(self):
        self.rise(); self.runner.stop(end(candle(2)))
        self.assertFalse(self.runner.summary()['active'])
        with self.assertRaises(DataError): self.feed(3)
        self.restart();self.assertEqual(self.runner.summary()['fills'],1)

    def test_session_ids_and_summary(self):
        self.rise();self.runner.stop(end(candle(2)));self.runner.start(end(candle(2)))
        s=self.runner.summary();self.assertEqual(s['sessions'],2)
        self.assertEqual(s['daily']['2026-01-01']['fills'],1)
        self.assertEqual(s['daily']['2026-01-01']['candles'],3)

    def test_macd_explicit_baseline(self):
        path=Path(self.tmp.name)/'macd'
        r=ForwardRunner(path,replace(CONFIG,strategy='macd_trend'))
        try:
            r.start(BASE);r.set_health('healthy',BASE)
            c=candle();r.consume(c,as_of=end(c),received_at=end(c))
            self.assertEqual(r.pending_signal['reason'],'macd_warmup')
        finally:r.close()

    def test_fixture_restart_equivalence(self):
        fixture=json.loads((ROOT/'data/forward_sample.json').read_text())
        self.assertEqual(execute(Path(self.tmp.name)/'a',fixture),execute(Path(self.tmp.name)/'b',fixture,True))

    def test_pinned_fixture(self):
        self.assertTrue(validate()['matched'])

    def test_decimal_context_isolated(self):
        a=build()
        with localcontext() as ctx:
            ctx.prec=6;ctx.rounding=ROUND_DOWN
            self.assertEqual(a,build())

    def test_aggregation_complete(self):
        rows=list(aggregate([candle(i,100+i) for i in range(3)],900))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].volume,D(30))
        self.assertEqual(rows[0].open,D(100));self.assertEqual(rows[0].close,D(102))

    def test_aggregation_partial_unavailable(self):
        self.assertEqual(list(aggregate([candle(1),candle(2)],900)),[])
        self.assertEqual(list(aggregate([candle(0),candle(1)],900)),[])

    def test_aggregation_gap_rejected(self):
        with self.assertRaises(DataError): list(aggregate([candle(0),candle(2)],900))

    def test_unsupported_timeframe_rejected(self):
        with self.assertRaises(DataError): replace(CONFIG,seconds=60)

    def test_invalid_configuration(self):
        for kw in ({'strategy':'automatic'},{'quantity':D('NaN')},{'run_id':'../bad'},
                   {'finalization_grace':0},{'features':FeatureConfig(gap_policy='reset')}):
            with self.subTest(kw=kw),self.assertRaises(DataError): replace(CONFIG,**kw)

    def test_cli_package_path(self):
        result=subprocess.run([sys.executable,'-m','trading_bot.forward.cli','--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0);self.assertIn('--strategy',result.stdout)

    def test_crash_boundaries_do_not_duplicate_fills(self):
        for boundary in ('after_prepare','after_risk_commit','before_journal_commit'):
            with self.subTest(boundary=boundary):
                p=Path(self.tmp.name)/boundary
                ref=Path(self.tmp.name)/(boundary+'-reference')
                def prefix(r):
                    r.start(BASE);r.set_health('healthy',BASE)
                    for i,price in enumerate([100,110]):
                        c=candle(i,price);r.consume(c,as_of=end(c),received_at=end(c))
                reference=ForwardRunner(ref,CONFIG);prefix(reference)
                c=candle(2,120);reference.consume(c,as_of=end(c),received_at=end(c));expected=reference.summary();reference.close()
                r=ForwardRunner(p,CONFIG);prefix(r)
                def crash(point):
                    if point==boundary: raise RuntimeError('simulated process death')
                r.crash=crash
                with self.assertRaises(RuntimeError):r.consume(c,as_of=end(c),received_at=end(c))
                r.close();r=ForwardRunner(p,CONFIG,replay_recovery=True)
                try:self.assertEqual(expected,r.summary())
                finally:r.close()

    def test_rejected_handoff_crash_recovers_once(self):
        self.feed(0);self.feed(1,110);self.runner.kill(True,'operator',end(candle(1)))
        def crash(point):
            if point=='after_risk_commit':raise RuntimeError('crash')
        self.runner.crash=crash
        with self.assertRaises(RuntimeError):self.feed(2,120)
        self.restart();s=self.runner.summary()
        self.assertEqual(s['risk_rejections'],1);self.assertEqual(s['fills'],0)

    def test_ambiguous_handoff_fails_closed(self):
        def crash(point):
            if point=='after_prepare':raise RuntimeError('crash')
        self.runner.crash=crash
        with self.assertRaises(RuntimeError):self.feed(0)
        self.runner.ledger.set_kill(True,'unrelated',as_of=BASE)
        self.runner.close()
        with self.assertRaises(IntegrityError):ForwardRunner(self.path,CONFIG)
        # tearDown tolerates the closed instance

    def test_live_resume_refuses_uncommitted_historical_intent(self):
        self.feed(0); self.feed(1,110)
        def crash(point):
            if point == 'after_prepare': raise RuntimeError('crash')
        self.runner.crash = crash
        with self.assertRaises(RuntimeError): self.feed(2,120)
        self.runner.close()
        with self.assertRaisesRegex(IntegrityError,'uncommitted intent'):
            ForwardRunner(self.path,CONFIG)
        self.runner = ForwardRunner(self.path,CONFIG,replay_recovery=True)
        self.assertEqual(self.runner.summary()['fills'],1)

    def test_pending_corruption_rejected(self):
        def crash(point):
            if point == 'after_prepare': raise RuntimeError('crash')
        self.runner.crash = crash
        with self.assertRaises(RuntimeError): self.feed(0)
        self.runner.db.execute("UPDATE pending SET payload='{}'")
        self.runner.close()
        with self.assertRaises(IntegrityError): ForwardRunner(self.path,CONFIG)

    def test_actual_process_exit_after_fill_recovers_once(self):
        self.feed(0); self.feed(1,110); self.runner.close()
        code = """import os,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from trading_bot.forward import ForwardRunner,ForwardConfig
from trading_bot.indicators import FeatureConfig
from trading_bot.market_data.models import MarketCandle
r=ForwardRunner(Path(sys.argv[1]),ForwardConfig('test','sma_trend',quantity=D('.5'),features=FeatureConfig(sma=2)))
def crash(point):
    if point=='after_risk_commit':os._exit(73)
r.crash=crash
start=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=600)
c=MarketCandle('BTC-USD',start,300,D(120),D(120),D(120),D(120),D(10))
r.consume(c,as_of=start+timedelta(seconds=300),received_at=start+timedelta(seconds=300))
"""
        result = subprocess.run([sys.executable,'-c',code,str(self.path)])
        self.assertEqual(result.returncode,73)
        self.runner = ForwardRunner(self.path,CONFIG)
        self.assertEqual(self.runner.summary()['fills'],1)
        before=self.runner.summary();self.feed(2,120);self.assertEqual(before,self.runner.summary())

    def test_open_position_daily_lockout_blocks_exit(self):
        self.runner.close()
        config=replace(CONFIG,limits=replace(CONFIG.limits,daily_loss_fraction=D('.001')))
        self.path=Path(self.tmp.name)/'open-daily';self.runner=ForwardRunner(self.path,config)
        self.runner.start(BASE);self.runner.set_health('healthy',BASE)
        self.rise();self.feed(3,80);self.feed(4,70)
        s=self.runner.summary();self.assertTrue(s['state']['daily_locked'])
        self.assertTrue(s['state']['positions']);self.assertEqual(s['fills'],1)

    def test_higher_timeframe_runner(self):
        r=ForwardRunner(Path(self.tmp.name)/'900',replace(CONFIG,seconds=900))
        try:
            r.start(BASE);r.set_health('healthy',BASE)
            rows=list(aggregate([candle(i,100+i) for i in range(9)],900))
            for c in rows:r.consume(c,as_of=end(c),received_at=end(c))
            self.assertEqual(r.summary()['fills'],1)
        finally:r.close()

    def test_max_drawdown_and_daily_equity(self):
        self.rise();self.feed(3,80)
        s=self.runner.summary()
        self.assertGreater(D(s['max_drawdown']),0)
        self.assertEqual(s['daily']['2026-01-01']['closing_equity'],s['equity'])

    def test_prefix_causality(self):
        self.rise();prefix=list(self.runner.records)
        self.feed(3,1000);self.feed(4,1)
        self.assertEqual(prefix,self.runner.records[:len(prefix)])

    def test_completed_handoff_mismatch_rejected(self):
        self.feed(0);self.feed(1,110)
        def crash(point):
            if point=='after_risk_commit':raise RuntimeError('crash')
        self.runner.crash=crash
        with self.assertRaises(RuntimeError):self.feed(2,120)
        self.runner.ledger.set_kill(True,'external writer',as_of=end(candle(2)))
        self.runner.close()
        with self.assertRaises(IntegrityError):ForwardRunner(self.path,CONFIG)



class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_live_orchestration_and_shutdown(self):
        with tempfile.TemporaryDirectory() as t:
            r=ForwardRunner(Path(t)/'run',CONFIG);store=MarketStore(Path(t)/'market.db')
            now=[BASE];status=['warming']; stop=asyncio.Event()
            class Processor:
                def __init__(self):self.store=store
                def health(self,clock):return {'status':status[0]}
            class Collector:
                async def run(self,stop):
                    for i,p in enumerate([100,110,120]):
                        c=candle(i,p);now[0]=end(c)
                        store.put(c,now[0],finalized=True,source='mock_rest');status[0]='healthy'
                        await asyncio.sleep(.02)
                    status[0]='disconnected';await asyncio.sleep(.02)
                    status[0]='healthy';await asyncio.sleep(.02)
                    stop.set()
                    await asyncio.sleep(.01) # bounded repair drain
                    self.drained=True
            col=Collector()
            try:
                result=await forward(r,Processor(),collector=col,clock=lambda:now[0],poll=.005,stop=stop)
                self.assertEqual(result['fills'],1);self.assertFalse(result['active']);self.assertTrue(col.drained)
                self.assertEqual(result['pauses'],1)
            finally:r.close();store.close()

    async def test_windows_unsupported_signals_duration(self):
        with tempfile.TemporaryDirectory() as t:
            r=ForwardRunner(Path(t)/'run',CONFIG)
            class P:
                def health(self,now):return {'status':'stale'}
            class C:
                async def run(self,stop):await stop.wait()
            loop=asyncio.get_running_loop()
            try:
                with patch.object(loop,'add_signal_handler',side_effect=NotImplementedError),patch.object(loop,'remove_signal_handler') as remove:
                    result=await forward(r,P(),collector=C(),clock=lambda:BASE,duration=.02,poll=.005)
                    remove.assert_not_called();self.assertFalse(result['active']);self.assertEqual(result['fills'],0)
            finally:r.close()

    async def test_collector_failure_stops_closed(self):
        with tempfile.TemporaryDirectory() as t:
            r=ForwardRunner(Path(t)/'run',CONFIG)
            class P:
                def health(self,now):return {'status':'warming'}
            class C:
                async def run(self,stop):raise DataError('transport failed')
            try:
                with self.assertRaises(DataError):await forward(r,P(),collector=C(),clock=lambda:BASE,poll=.001)
                self.assertFalse(r.active)
            finally:r.close()

    async def test_invalid_duration(self):
        with self.assertRaises(DataError):await forward(None,None,duration=float('nan'))

    async def test_public_websocket_rest_to_risk_pipeline(self):
        from trading_bot.market_data.feed import FeedProcessor, FeedPolicy
        from trading_bot.market_data.live import LiveCollector
        from trading_bot.market_data.rest import HistoricalClient
        with tempfile.TemporaryDirectory() as t:
            r=ForwardRunner(Path(t)/'run',CONFIG);store=MarketStore(Path(t)/'market.db')
            feed=FeedProcessor(store,'BTC-USD',policy=FeedPolicy(heartbeat_timeout=600,candle_timeout=600));now=[BASE];stop=asyncio.Event()
            sent=[]
            class Socket:
                def __init__(self):self.i=0;self.hb=True
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def send(self,raw):sent.append(json.loads(raw))
                async def recv(self):
                    await asyncio.sleep(.025)
                    if self.i>=4:
                        await asyncio.sleep(.15);stop.set()
                        return json.dumps({'channel':'heartbeats','sequence_num':4,
                            'timestamp':now[0].isoformat(),'events':[{'current_time':now[0].isoformat(),'heartbeat_counter':4}]})
                    now[0]=BASE+timedelta(seconds=300*self.i)
                    if self.hb:
                        self.hb=False
                        return json.dumps({'channel':'heartbeats','sequence_num':self.i,
                            'timestamp':now[0].isoformat(),'events':[{'current_time':now[0].isoformat(),'heartbeat_counter':self.i}]})
                    i=self.i;self.i+=1;self.hb=True
                    p=str([100,110,120,125][i])
                    return json.dumps({'channel':'candles','sequence_num':i,'timestamp':now[0].isoformat(),
                        'events':[{'type':'update','candles':[{'product_id':'BTC-USD','start':str(int(now[0].timestamp())),
                        'open':p,'high':p,'low':p,'close':p,'volume':'10'}]}]})
            def transport(url,timeout):
                from urllib.parse import urlparse,parse_qs
                q=parse_qs(urlparse(url).query);start=int(q['start'][0]);finish=int(q['end'][0])
                rows=[]
                for epoch in range(start,finish,300):
                    i=(epoch-int(BASE.timestamp()))//300;p=str([100,110,120,125][i])
                    rows.append({'start':str(epoch),'open':p,'high':p,'low':p,'close':p,'volume':'10'})
                return json.dumps({'candles':rows})
            socket=Socket()
            collector=LiveCollector(feed,connector=lambda url:socket,clock=lambda:now[0],monotonic=lambda:now[0].timestamp(),
                    historical=HistoricalClient(transport=transport,clock=lambda:now[0]))
            try:
                result=await asyncio.wait_for(forward(r,feed,collector=collector,clock=lambda:now[0],poll=.005,stop=stop),3)
                self.assertEqual(result['fills'],1)
                self.assertEqual(result['candles'],3)
                self.assertEqual([x['channel'] for x in sent],['heartbeats','candles'])
                self.assertTrue(all(set(x) <= {'type','product_ids','channel'} and x['type']=='subscribe' for x in sent))
            finally:r.close();store.close()

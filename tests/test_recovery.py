"""Risk boundaries, immutable audit and crash-safe paper reconciliation."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D,localcontext,ROUND_DOWN
from pathlib import Path
from trading_bot.recovery import DurablePaperLedger,IntegrityError,OrderIntent,RiskLimits
from trading_bot.market_data.models import MarketCandle,DataError

BASE=datetime(2026,1,1,tzinfo=timezone.utc)
FREE=RiskLimits(max_order_quote=D(10000),max_position_fraction=D(1),max_portfolio_fraction=D(1),
                daily_loss_fraction=D('.5'),max_drawdown_fraction=D('.5'),fee_fraction=D(0),slippage_fraction=D(0),max_open_positions=3)


def candle(i=0,price='100',product='BTC-USD'):
    p=D(str(price))
    return MarketCandle(product,BASE+timedelta(seconds=i*300),300,p,p,p,p,D(5))


def packet(c,**changes):
    now=c.start+timedelta(seconds=c.seconds)
    return dict(as_of=changes.get('as_of',now),received_at=changes.get('received_at',now),
                health=changes.get('health','healthy'),finalized=changes.get('finalized',True))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.db'
        self.limits=FREE;self.ledger=DurablePaperLedger(self.path,D(1000),self.limits)

    def tearDown(self):
        self.ledger.close();self.temp.cleanup()

    def reset(self,limits,cash=D(1000)):
        self.ledger.close();self.path.unlink()
        self.limits=limits;self.ledger=DurablePaperLedger(self.path,cash,limits)

    def restart(self):
        self.ledger.close();self.ledger=DurablePaperLedger(self.path,D(999999),self.limits)

    def submit(self,i=0,side='BUY',quantity='1',price='100',product='BTC-USD',identifier=None,**changes):
        c=candle(i,price,product)
        return self.ledger.submit(OrderIntent(identifier or f'{product}:{i}',product,side,D(quantity)),c,**packet(c,**changes))

    def test_defaults_preserve_paper_costs_and_limits(self):
        self.reset(RiskLimits())
        self.assertTrue(self.submit(quantity='.5').accepted)
        s=self.ledger.snapshot()
        self.assertEqual(D(s['cash']),D('949.5994'))
        self.assertEqual(D(s['positions']['BTC-USD']['quantity']),D('.5'))
        self.assertEqual(D(s['unrealized']),D('-.4006'))

    def test_order_boundary(self):
        self.reset(replace(FREE,max_order_quote=D(100)))
        self.assertTrue(self.submit(quantity='1').accepted)
        self.assertEqual(self.submit(1,quantity='1.0001').reason,'order_limit')

    def test_position_boundary(self):
        self.reset(replace(FREE,max_position_fraction=D('.2')))
        self.assertTrue(self.submit(quantity='2').accepted)
        self.assertEqual(self.submit(1,quantity='.0001').reason,'position_limit')

    def test_portfolio_boundary(self):
        self.reset(replace(FREE,max_portfolio_fraction=D('.2')))
        self.assertTrue(self.submit(quantity='1').accepted)
        self.assertTrue(self.submit(product='ETH-USD',quantity='1').accepted)
        self.assertEqual(self.submit(1,product='ETH-USD',quantity='.0001').reason,'portfolio_exposure_limit')

    def test_open_position_count(self):
        self.reset(replace(FREE,max_open_positions=1))
        self.assertTrue(self.submit().accepted)
        self.assertEqual(self.submit(product='ETH-USD').reason,'open_position_limit')
        self.assertTrue(self.submit(1,side='SELL').accepted)
        self.assertTrue(self.submit(1,product='ETH-USD').accepted)

    def test_insufficient_cash_no_leverage(self):
        self.assertEqual(self.submit(quantity='10.0001').reason,'insufficient_cash')
        self.assertEqual(self.ledger.snapshot()['positions'],{})

    def test_no_shorting(self):
        self.assertEqual(self.submit(side='SELL').reason,'insufficient_units')
        self.assertEqual(self.ledger.snapshot()['positions'],{})

    def test_daily_lockout_exact_boundary(self):
        self.reset(replace(FREE,daily_loss_fraction=D('.03')))
        self.assertTrue(self.submit(quantity='5').accepted)
        self.assertEqual(self.submit(1,price='94',side='SELL').reason,'daily_loss_limit')
        self.assertTrue(self.ledger.snapshot()['daily_locked'])
        self.assertEqual(self.submit(2,price='100').reason,'daily_loss_limit')
        self.restart();self.assertTrue(self.ledger.snapshot()['daily_locked'])

    def test_daily_reset_next_utc_day(self):
        self.reset(replace(FREE,daily_loss_fraction=D('.03'),max_data_age_seconds=100000))
        self.submit(quantity='5');self.submit(1,price='94',side='SELL')
        c=candle(2,'100');now=BASE+timedelta(days=1)
        d=self.ledger.submit(OrderIntent('next-day','BTC-USD','SELL',D(1)),c,**packet(c,as_of=now,received_at=now))
        self.assertTrue(d.accepted);self.assertFalse(self.ledger.snapshot()['daily_locked'])

    def test_drawdown_lockout_persists_after_recovery(self):
        self.reset(replace(FREE,max_drawdown_fraction=D('.03')))
        self.submit(quantity='5')
        self.assertEqual(self.submit(1,price='94',side='SELL').reason,'drawdown_limit')
        self.restart();self.assertEqual(self.submit(2,price='100').reason,'drawdown_limit')

    def test_peak_equity_drawdown(self):
        self.reset(replace(FREE,max_drawdown_fraction=D('.05')))
        self.submit(quantity='5');self.submit(1,price='120',quantity='.1')
        self.assertEqual(self.submit(2,price='100',side='SELL').reason,'drawdown_limit')
        self.assertGreater(D(self.ledger.snapshot()['peak']),D(1000))

    def test_projected_fees_trip_daily_breaker(self):
        self.reset(replace(FREE,daily_loss_fraction=D('.001'),fee_fraction=D('.01')))
        self.assertEqual(self.submit(quantity='1').reason,'daily_loss_limit')
        self.assertEqual(self.ledger.snapshot()['cash'],'1000')
        self.assertTrue(self.ledger.snapshot()['daily_locked'])

    def test_stale_market_boundary(self):
        self.reset(replace(FREE,max_data_age_seconds=60))
        c=candle();end=c.start+timedelta(seconds=300)
        self.assertTrue(self.submit(as_of=end+timedelta(seconds=60)).accepted)
        self.assertEqual(self.submit(1,as_of=end+timedelta(seconds=361)).reason,'stale_data')

    def test_stale_receipt(self):
        self.reset(replace(FREE,max_data_age_seconds=60))
        end=BASE+timedelta(seconds=300)
        self.assertEqual(self.submit(as_of=end+timedelta(seconds=61)).reason,'stale_data')

    def test_unhealthy_feed_states(self):
        for index,health in enumerate(('warming','stale','degraded','disconnected','stopped','unavailable','invalid')):
            self.assertEqual(self.submit(identifier=f'health:{index}',health=health).reason,'unhealthy_feed')
        self.assertEqual(self.ledger.snapshot()['positions'],{})
        self.assertEqual(len(self.ledger.events()),7)

    def test_future_and_provisional_data(self):
        self.assertEqual(self.submit(identifier='provisional',finalized=False).reason,'provisional_data')
        self.assertEqual(self.submit(identifier='future',as_of=BASE).reason,'future_data')

    def test_invalid_market_is_audited(self):
        c=candle();object.__setattr__(c,'close',D('NaN'))
        d=self.ledger.submit(OrderIntent('invalid','BTC-USD','BUY',D(1)),c,**packet(c))
        self.assertEqual(d.reason,'invalid_market_data')
        self.assertEqual(len(self.ledger.events()),1)
        self.assertTrue(self.ledger.reconcile().matched)

    def test_invalid_intent_reasons(self):
        for i,(side,q) in enumerate((('short','1'),('BUY','0'),('BUY','-1'),('BUY','NaN'),('BUY','Infinity'))):
            self.assertEqual(self.submit(i,side=side,quantity=q).reason,'invalid_intent')
        self.assertEqual(self.ledger.snapshot()['positions'],{})

    def test_product_mismatch(self):
        c=candle()
        d=self.ledger.submit(OrderIntent('mismatch','ETH-USD','BUY',D(1)),c,**packet(c))
        self.assertEqual(d.reason,'product_mismatch')

    def test_gaps_and_monotonicity(self):
        self.submit()
        self.assertEqual(self.submit(2).reason,'data_gap')
        self.assertEqual(self.submit(0,identifier='new-id').reason,'duplicate_data')
        self.assertTrue(self.submit(1).accepted)

    def test_kill_switch_persistence(self):
        self.ledger.set_kill(True,'operator',as_of=BASE)
        self.restart()
        self.assertEqual(self.submit().reason,'kill_switch')
        self.assertTrue(self.ledger.snapshot()['kill'])
        self.ledger.set_kill(False,'operator_resume',as_of=BASE+timedelta(seconds=300))
        self.assertTrue(self.submit(1).accepted)

    def test_kill_does_not_clear_drawdown(self):
        self.reset(replace(FREE,max_drawdown_fraction=D('.03')))
        self.submit(quantity='5');self.submit(1,price='94',side='SELL')
        self.ledger.set_kill(True,'stop',as_of=BASE+timedelta(seconds=600))
        self.ledger.set_kill(False,'resume',as_of=BASE+timedelta(seconds=600))
        self.assertEqual(self.submit(2).reason,'drawdown_limit')

    def test_restart_open_position_and_basis(self):
        self.submit(quantity='2');before=self.ledger.snapshot();self.restart()
        self.assertEqual(self.ledger.snapshot(),before)
        self.assertTrue(self.submit(1,side='SELL',quantity='1',price='110').accepted)
        s=self.ledger.snapshot();self.assertEqual(s['realized'],'10');self.assertEqual(s['positions']['BTC-USD']['basis'],'100')

    def test_weighted_basis_partial_sell(self):
        self.submit();self.submit(1,price='120')
        self.submit(2,side='SELL',price='130')
        s=self.ledger.snapshot();self.assertEqual(s['realized'],'20');self.assertEqual(s['positions']['BTC-USD']['basis'],'110')

    def test_restart_after_full_close(self):
        self.submit();self.submit(1,side='SELL',price='110');self.restart()
        s=self.ledger.snapshot();self.assertEqual(s['positions'],{});self.assertEqual(s['cash'],'1010');self.assertEqual(s['realized'],'10')

    def test_duplicate_after_restart(self):
        self.submit(identifier='once');self.restart()
        self.assertEqual(self.submit(identifier='once').reason,'duplicate_intent')
        self.assertEqual(self.ledger.snapshot()['cash'],'900')
        self.assertEqual(len(self.ledger.events()),2)

    def test_intent_identifier_conflict(self):
        self.submit(identifier='once')
        self.assertEqual(self.submit(quantity='2',identifier='once').reason,'intent_id_conflict')
        self.assertEqual(self.ledger.snapshot()['cash'],'900')

    def test_rejected_id_is_not_reusable(self):
        self.submit(identifier='bad',health='stale')
        self.assertEqual(self.submit(identifier='bad').reason,'duplicate_intent')
        self.assertEqual(self.ledger.snapshot()['positions'],{})

    def test_immutable_audit_and_metadata(self):
        self.submit()
        conn=sqlite3.connect(self.path)
        try:
            for statement in ('UPDATE audit SET payload=payload','DELETE FROM audit','UPDATE metadata SET value=value','DELETE FROM metadata'):
                with self.assertRaises(sqlite3.IntegrityError):conn.execute(statement)
        finally:conn.close()

    def test_corrupt_projection_fails_closed(self):
        self.submit()
        with sqlite3.connect(self.path) as conn:conn.execute("UPDATE projection SET value='{}'")
        with self.assertRaises(IntegrityError):self.ledger.reconcile()
        with self.assertRaises(IntegrityError):self.submit(1)
        with self.assertRaises(IntegrityError):self.restart()
        self.ledger=DurablePaperLedger(Path(self.temp.name)/'fresh.db',D(1000),FREE)

    def test_incomplete_audit_operation_fails_closed(self):
        self.submit()
        with sqlite3.connect(self.path) as conn:conn.execute("INSERT INTO audit VALUES (2,'{}','wrong','wrong')")
        with self.assertRaises(IntegrityError):self.ledger.reconcile()

    def test_missing_immutability_trigger_fails_closed(self):
        with sqlite3.connect(self.path) as conn:conn.execute('DROP TRIGGER audit_no_update')
        with self.assertRaises(IntegrityError):self.ledger.reconcile()

    def test_limits_never_silently_change(self):
        self.submit();self.ledger.close()
        with self.assertRaises(IntegrityError):DurablePaperLedger(self.path,D(1000),replace(FREE,max_order_quote=D(100)))
        self.ledger=DurablePaperLedger(self.path,D(999999),FREE)
        with self.assertRaises(AttributeError):self.ledger.limits=FREE
        with self.assertRaises(AttributeError):self.ledger.limits.fee_fraction=D(0)

    def test_reconciliation_stable_and_no_extra_restart_events(self):
        self.submit();before=self.ledger.reconcile().serialize();events=self.ledger.events()
        self.restart();self.assertEqual(self.ledger.reconcile().serialize(),before)
        self.assertEqual(self.ledger.events(),events)

    def test_interrupted_transaction_rolls_back(self):
        self.submit();before=self.ledger.reconcile().serialize()
        conn=sqlite3.connect(self.path)
        conn.execute('BEGIN IMMEDIATE');conn.execute("UPDATE projection SET value='broken'")
        conn.close();self.restart()
        self.assertEqual(self.ledger.reconcile().serialize(),before)

    def test_process_crash_during_uncommitted_write(self):
        self.submit();before=self.ledger.reconcile().serialize()
        code="import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE projection SET value='interrupted'\"); os._exit(17)"
        child=subprocess.run([sys.executable,'-c',code,str(self.path)],timeout=15)
        self.assertEqual(child.returncode,17);self.restart()
        self.assertEqual(self.ledger.reconcile().serialize(),before)

    def test_incomplete_initialization_rejected(self):
        path=Path(self.temp.name)/'incomplete.db'
        with sqlite3.connect(path) as conn:conn.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
        with self.assertRaises(IntegrityError):DurablePaperLedger(path,D(1000),FREE)

    def test_legacy_database_not_guessed_or_migrated(self):
        path=Path(self.temp.name)/'legacy.db'
        with sqlite3.connect(path) as conn:conn.execute('CREATE TABLE state(key TEXT,value TEXT)')
        with self.assertRaises(IntegrityError):DurablePaperLedger(path,D(1000),FREE)

    def test_config_schema_validation(self):
        for field in ('max_order_quote','max_position_fraction','max_portfolio_fraction','daily_loss_fraction','max_drawdown_fraction','fee_fraction','slippage_fraction'):
            for value in (D('NaN'),D('Infinity'),1.0,True):
                with self.assertRaises(DataError):replace(FREE,**{field:value})
        for field in ('max_open_positions','max_data_age_seconds'):
            for value in (0,-1,True,1.0):
                with self.assertRaises(DataError):replace(FREE,**{field:value})
        for changes in ({'max_order_quote':D(0)},{'max_position_fraction':D('1.1')},{'daily_loss_fraction':D(1)},{'max_drawdown_fraction':D(0)},{'fee_fraction':D(-1)}):
            with self.assertRaises(DataError):replace(FREE,**changes)

    def test_bad_schema_identifiers(self):
        for identifier in ('','bad space',None,'x'*129):
            with self.assertRaises(DataError):OrderIntent(identifier,'BTC-USD','BUY',D(1))
        with self.assertRaises(DataError):self.ledger.submit({},candle(),**packet(candle()))
        with self.assertRaises(DataError):self.ledger.set_kill(1,'reason',as_of=BASE)

    def test_decimal_context_isolation(self):
        self.reset(RiskLimits());expected=self.submit(quantity='.5');state=self.ledger.snapshot()
        path=Path(self.temp.name)/'other.db'
        with localcontext() as ctx:
            ctx.prec=5;ctx.rounding=ROUND_DOWN
            other=DurablePaperLedger(path,D('1000.00'),RiskLimits())
            try:
                d=other.submit(OrderIntent('BTC-USD:0','BTC-USD','BUY',D('.5')),candle(),**packet(candle()))
                self.assertEqual(d,expected);self.assertEqual(other.snapshot(),state)
                self.assertEqual(other.reconcile().serialize(),self.ledger.reconcile().serialize())
            finally:other.close()

    def test_restart_fixture_pinned(self):
        from scripts.validate_recovery import validate
        report=validate();self.assertEqual(report,validate());self.assertTrue(report['matched'])

    def test_every_restart_cut_matches_reference(self):
        from scripts.validate_recovery import execute,ROOT
        fixture=json.loads((ROOT/'data/recovery_sample.json').read_text())
        reference=execute(Path(self.temp.name)/'reference.db',fixture)
        for cut in range(len(fixture['rows'])):
            with self.subTest(cut=cut):
                self.assertEqual(execute(Path(self.temp.name)/f'cut-{cut}.db',fixture,cut),reference)

    def test_atomic_audit_insert_rolls_back_on_projection_failure(self):
        self.submit();before=self.ledger.reconcile().serialize()
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER inject_failure BEFORE UPDATE ON projection BEGIN SELECT RAISE(ABORT,'test crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.submit(1)
        with sqlite3.connect(self.path) as conn:conn.execute('DROP TRIGGER inject_failure')
        self.assertEqual(self.ledger.reconcile().serialize(),before)
        self.assertTrue(self.submit(1).accepted)

    def test_forged_noop_immutability_trigger_detected(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute('DROP TRIGGER audit_no_update')
            conn.execute('CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT 1; END')
        with self.assertRaises(IntegrityError):self.ledger.reconcile()

    def test_stale_other_position_blocks_new_order(self):
        self.reset(replace(FREE,max_data_age_seconds=60))
        self.assertTrue(self.submit(product='ETH-USD').accepted)
        self.assertEqual(self.submit(1,product='BTC-USD').reason,'stale_portfolio_data')
        self.assertNotIn('BTC-USD',self.ledger.snapshot()['positions'])

    def test_projected_drawdown_breaker(self):
        self.reset(replace(FREE,max_drawdown_fraction=D('.001'),fee_fraction=D('.01')))
        self.assertEqual(self.submit().reason,'drawdown_limit')
        self.assertTrue(self.ledger.snapshot()['drawdown_locked'])

    def test_semantic_quantity_duplicate(self):
        self.submit(quantity='0.50',identifier='semantic')
        self.assertEqual(self.submit(quantity='.5',identifier='semantic').reason,'duplicate_intent')

    def test_validation_subprocess_and_portable_bytes(self):
        import hashlib
        from scripts.validate_recovery import ROOT,EXPECTED_SHA256
        output=Path(self.temp.name)/'report.json'
        r=subprocess.run([sys.executable,str(ROOT/'scripts/validate_recovery.py'),'--output',str(output)],
                         cwd=self.temp.name,check=True,capture_output=True,text=True,timeout=15)
        self.assertTrue(json.loads(r.stdout)['matched'])
        raw=output.read_bytes();self.assertNotIn(b'\r',raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),EXPECTED_SHA256)

    def test_two_connections_serialize_duplicate_fills(self):
        other=DurablePaperLedger(self.path,D(1000),FREE)
        try:
            self.assertTrue(self.submit(identifier='shared').accepted)
            d=other.submit(OrderIntent('shared','BTC-USD','BUY',D(1)),candle(),**packet(candle()))
            self.assertEqual(d.reason,'duplicate_intent')
            self.assertEqual(other.snapshot()['cash'],'900')
            self.assertEqual(other.reconcile(),self.ledger.reconcile())
        finally:other.close()

    def test_mark_only_latches_loss_without_order(self):
        self.reset(replace(FREE,max_drawdown_fraction=D('.03')))
        self.submit(quantity='5');c=candle(1,'94')
        self.assertTrue(self.ledger.mark(c,**packet(c)).accepted)
        self.assertTrue(self.ledger.snapshot()['drawdown_locked'])
        self.restart();self.assertEqual(self.submit(2).reason,'drawdown_limit')

    def test_backward_operator_timestamp_rejected(self):
        self.submit();before=self.ledger.reconcile().serialize()
        with self.assertRaises(DataError):self.ledger.set_kill(True,'stop',as_of=BASE)
        self.assertEqual(before,self.ledger.reconcile().serialize())

    def test_dependency_scope_no_execution_or_ai(self):
        import ast
        folder=Path(__file__).resolve().parents[1]/'trading_bot'/'recovery'
        allowed={'__future__','hashlib','json','re','sqlite3','copy','dataclasses','datetime','decimal','pathlib',
                 'trading_bot.backtest.common','trading_bot.core','trading_bot.market_data.models','ledger'}
        for path in folder.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):
                    self.assertIn(node.module,allowed)
                    if node.module=='trading_bot.core':self.assertEqual([x.name for x in node.names],['Limits'])
                elif isinstance(node,ast.Import):
                    for alias in node.names:self.assertIn(alias.name,allowed)

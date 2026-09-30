"""Known-value Phase 4 tests, including causal next-open execution and accounting."""
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext, ROUND_DOWN
from pathlib import Path

from trading_bot.backtest import (BacktestConfig, BacktestEngine, MACDTrend, SMATrend,
                                  Signal, run_backtest)
from trading_bot.backtest.engine import performance
from trading_bot.core import Limits
from trading_bot.indicators import FeatureConfig, calculate
from trading_bot.market_data.models import DataError, MarketCandle

BASE = datetime(2026,1,1,tzinfo=timezone.utc)
FC = FeatureConfig(sma=2, ema=2, rsi=2, macd_fast=2, macd_slow=3, macd_signal=2,
                   atr=2, vwap=2, momentum=2, volatility=2, volume=2)
ZERO = BacktestConfig(initial_cash=D(1000), quantity=D(1), fee_fraction=D(0), slippage_fraction=D(0))


def candles(prices, opens=None):
    result=[]
    for i,p in enumerate(prices):
        p=D(str(p)); o=D(str(opens[i])) if opens is not None else p
        result.append(MarketCandle('BTC-USD',BASE+timedelta(seconds=300*i),300,
                                   o,max(o,p)+1,min(o,p)-1,p,D(5)))
    return result


@dataclass
class Targets:
    targets: tuple
    index: int = 0
    name: str = 'scripted_test'

    def decide(self,candle,features):
        target=self.targets[min(self.index,len(self.targets)-1)]
        self.index+=1
        return Signal(target,'test')


def run(prices, targets=None, config=ZERO, opens=None, strategy=None):
    return run_backtest(candles(prices,opens),strategy or Targets(tuple(targets or ['flat'])),
                        FC,config,finalized=True)


def metrics(result):
    return dict(result.metrics)


class StrategyTests(unittest.TestCase):
    def test_sma_exact_rules(self):
        cs=candles([10,12,12,10])
        fs=calculate(cs,FC,finalized=True)
        signals=[SMATrend().decide(c,f) for c,f in zip(cs,fs)]
        self.assertEqual([s.target for s in signals],['flat','long','flat','flat'])
        self.assertEqual(signals[0].reason,'sma_warmup')

    def test_macd_exact_rules(self):
        cs=candles([10,12,11,15,12,10,16,16])
        fs=calculate(cs,FC,finalized=True)
        targets=[MACDTrend().decide(c,f).target for c,f in zip(cs,fs)]
        self.assertEqual(targets,['flat','flat','flat','long','flat','flat','long','long'])

    def test_baseline_warmup(self):
        cs=candles([10]*40)
        fs=calculate(cs,FeatureConfig(),finalized=True)
        for cls,first in ((SMATrend,20),(MACDTrend,34)):
            for c,f in zip(cs[:first-1],fs[:first-1]):
                s=cls().decide(c,f)
                self.assertEqual(s.target,'flat')
                self.assertIn('warmup',s.reason)
            self.assertNotIn('warmup',cls().decide(cs[first-1],fs[first-1]).reason)

    def test_feature_alignment_rejected(self):
        c=candles([10])[0]; f=calculate([c],FC,finalized=True)[0]
        for bad in (replace(f,product='ETH-USD'),replace(f,start=f.start+timedelta(seconds=300)),
                    replace(f,seconds=60),replace(f,available_at=f.start)):
            for strategy in (SMATrend(),MACDTrend()):
                with self.assertRaises(DataError):strategy.decide(c,bad)

    def test_invalid_signal(self):
        for target,reason in [('short','x'),('buy','x'),('long',''),('flat',None)]:
            with self.assertRaises(DataError):Signal(target,reason)


class BacktestTests(unittest.TestCase):
    def test_no_same_close_execution(self):
        result=run([10,100,12],['long','flat','flat'],opens=[10,11,13])
        self.assertEqual([(f.side,f.reference_price) for f in result.fills],[('BUY',D(11)),('SELL',D(13))])
        self.assertEqual(result.fills[0].signal_time,result.bars[0].end)
        self.assertEqual(result.fills[0].time,result.bars[1].start)
        self.assertEqual(metrics(result)['realized_pnl'],D(2))

    def test_last_signal_unfilled(self):
        result=run([10,12],['flat','long'])
        self.assertEqual(result.fills,())
        self.assertEqual(result.bars[-1].target,'long')

    def test_position_transitions_no_rebalancing(self):
        result=run([10,11,12,13,14,15],['long','long','flat','flat','long','flat'])
        self.assertEqual([f.side for f in result.fills],['BUY','SELL','BUY'])
        self.assertEqual([b.units for b in result.bars],[0,1,1,0,0,1])
        self.assertEqual(len(result.trades),1)

    def test_realized_and_unrealized(self):
        result=run([10,11,14,15],['long','long','flat','flat'])
        self.assertEqual(result.bars[2].unrealized_pnl,D(3))
        self.assertEqual(result.bars[2].realized_pnl,D(0))
        self.assertEqual(result.bars[3].realized_pnl,D(4))
        self.assertEqual(result.bars[3].unrealized_pnl,D(0))
        self.assertEqual(metrics(result)['ending_cash'],D(1004))

    def test_cash_equity_identity(self):
        result=run([10,11,14,15,12,9],['long','long','flat','long','flat','flat'],config=replace(ZERO,fee_fraction=D('.006'),slippage_fraction=D('.002')))
        for c,b in zip(candles([10,11,14,15,12,9]),result.bars):
            self.assertEqual(b.equity,b.cash+b.units*c.close)
            self.assertAlmostEqual(float(b.equity-D(1000)),float(b.realized_pnl+b.unrealized_pnl),places=12)
            self.assertGreaterEqual(b.cash,0)
            self.assertGreaterEqual(b.units,0)

    def test_open_position_not_liquidated(self):
        result=run([10,11,15],['long'])
        self.assertEqual(len(result.fills),1)
        self.assertEqual(len(result.trades),0)
        self.assertEqual(metrics(result)['unrealized_pnl'],D(4))
        self.assertEqual(metrics(result)['ending_units'],D(1))

    def test_fee_slippage_known_round_trip(self):
        cfg=replace(ZERO,fee_fraction=D('.006'),slippage_fraction=D('.002'))
        result=run([10,14,10],['long','flat','flat'],config=cfg)
        trade=result.trades[0]
        self.assertEqual(trade.entry_price,D('14.028'))
        self.assertEqual(trade.exit_price,D('9.980'))
        self.assertEqual(trade.entry_fee,D('.084168'))
        self.assertEqual(trade.exit_fee,D('.059880'))
        self.assertEqual(trade.pnl,D('-4.192048'))
        self.assertEqual(metrics(result)['ending_equity'],D('995.807952'))

    def test_fee_assumptions_match_paper_engine(self):
        self.assertEqual(BacktestConfig().fee_fraction,Limits().fee_fraction)
        self.assertEqual(BacktestConfig().slippage_fraction,Limits().slippage_fraction)

    def test_costs_reduce_identical_price_returns(self):
        no_cost=run([10]*3,['long','flat','flat'])
        cost=run([10]*3,['long','flat','flat'],config=BacktestConfig(initial_cash=D(1000),quantity=D(1)))
        self.assertEqual(metrics(no_cost)['realized_pnl'],D(0))
        self.assertLess(metrics(cost)['realized_pnl'],0)

    def test_zero_trades_metrics(self):
        result=run([10,12,11,14])
        m=metrics(result)
        self.assertEqual(m['trade_count'],0)
        self.assertEqual(m['total_return'],0)
        self.assertEqual(m['max_drawdown'],0)
        self.assertEqual(m['exposure'],0)
        for key in ('win_rate','loss_rate','average_win','average_loss','expectancy','profit_factor','sharpe','sortino'):
            self.assertIsNone(m[key])

    def test_one_bar_metrics(self):
        result=run([10])
        for key in ('return_volatility','annualized_volatility','sharpe','sortino'):
            self.assertIsNone(metrics(result)[key])

    def test_profitable_closed_trade(self):
        m=metrics(run([10,11,15],['long','flat','flat']))
        self.assertEqual(m['win_rate'],1)
        self.assertEqual(m['loss_rate'],0)
        self.assertEqual(m['average_win'],4)
        self.assertEqual(m['expectancy'],4)
        self.assertIsNone(m['profit_factor'])

    def test_losing_closed_trade(self):
        m=metrics(run([10,15,11],['long','flat','flat']))
        self.assertEqual(m['win_rate'],0)
        self.assertEqual(m['loss_rate'],1)
        self.assertEqual(m['average_loss'],-4)
        self.assertEqual(m['profit_factor'],0)

    def test_mixed_metrics_known(self):
        result=run([10,10,14,14,12],['long','flat','long','flat','flat'])
        m=metrics(result)
        self.assertEqual([t.pnl for t in result.trades],[4,-2])
        self.assertEqual(m['trade_count'],2)
        self.assertEqual(m['fill_count'],4)
        self.assertEqual(m['win_rate'],D('.5'))
        self.assertEqual(m['loss_rate'],D('.5'))
        self.assertEqual(m['expectancy'],1)
        self.assertEqual(m['profit_factor'],2)
        self.assertEqual(m['total_return'],D('.002'))
        self.assertEqual(m['max_drawdown'],D('0.001992031872509960159362549800796813'))
        self.assertEqual(m['exposure'],D('.4'))
        self.assertEqual(m['time_in_market_seconds'],600)

    def test_breakeven_metrics(self):
        m=metrics(run([10]*3,['long','flat','flat']))
        self.assertEqual(m['breakeven_count'],1)
        self.assertEqual(m['win_rate'],0)
        self.assertEqual(m['loss_rate'],0)
        self.assertEqual(m['expectancy'],0)

    def test_volatility_adjusted_metrics_known(self):
        result=run([10,10,14,14,12],['long','flat','long','flat','flat'])
        returns=[0,0,.004,0,1002/1004-1]
        avg=sum(returns)/5
        std=math.sqrt(sum((r-avg)**2 for r in returns)/5)
        annual=math.sqrt(365*86400/300)
        m=metrics(result)
        self.assertAlmostEqual(float(m['return_volatility']),std,places=14)
        self.assertAlmostEqual(float(m['sharpe']),avg/std*annual,places=10)
        downside=math.sqrt(sum(min(r,0)**2 for r in returns)/5)
        self.assertAlmostEqual(float(m['sortino']),avg/downside*annual,places=10)

    def test_benchmark_buy_first_open(self):
        result=run([10,15,20])
        self.assertEqual(len(result.benchmark_fills),1)
        self.assertEqual(result.benchmark_fills[0].time,BASE)
        self.assertIsNone(result.benchmark_fills[0].signal_time)
        m=dict(result.benchmark_metrics)
        self.assertEqual(m['ending_cash'],990)
        self.assertEqual(m['ending_units'],1)
        self.assertEqual(m['ending_equity'],1010)
        self.assertEqual(m['total_return'],D('.01'))
        self.assertEqual(m['unrealized_pnl'],10)
        self.assertEqual(m['exposure'],1)

    def test_benchmark_uses_same_costs(self):
        result=run([10,15,10],config=BacktestConfig(initial_cash=D(1000),quantity=D(1)))
        m=dict(result.benchmark_metrics)
        self.assertEqual(m['ending_cash'],D('989.919880'))
        self.assertEqual(m['ending_equity'],D('999.919880'))
        self.assertEqual(m['total_fees'],D('.060120'))
        self.assertEqual(m['unrealized_pnl'],D('-.080120'))

    def test_flat_rising_falling_baselines(self):
        for strategy in (SMATrend(),MACDTrend()):
            flat=run([10]*12,strategy=strategy)
            self.assertEqual(flat.fills,())
            falling=run(list(range(25,10,-1)),strategy=strategy)
            self.assertEqual(falling.fills,())
        rising=run(list(range(10,25)),strategy=SMATrend())
        self.assertEqual(len(rising.fills),1)
        self.assertGreater(metrics(rising)['total_return'],0)

    def test_gaps_and_missing_candles_reject(self):
        cs=candles([10,12,14])
        engine=BacktestEngine(SMATrend(),FC,ZERO)
        engine.step(cs[0],finalized=True)
        with self.assertRaises(DataError):engine.step(cs[2],finalized=True)
        engine.step(cs[1],finalized=True)
        engine.step(cs[2],finalized=True)
        self.assertEqual(engine.result(),run_backtest(cs,SMATrend(),FC,ZERO,finalized=True))
        with self.assertRaises(DataError):BacktestEngine(SMATrend(),replace(FC,gap_policy='reset'),ZERO)

    def test_duplicates_revisions_earlier_reject(self):
        cs=candles([10,12,14])
        engine=BacktestEngine(SMATrend(),FC,ZERO)
        engine.step(cs[0],finalized=True);engine.step(cs[1],finalized=True)
        for c in (cs[0],cs[1],replace(cs[1],close=D(13))):
            with self.assertRaises(DataError):engine.step(c,finalized=True)
        engine.step(cs[2],finalized=True)
        self.assertEqual(engine.result(),run_backtest(cs,SMATrend(),FC,ZERO,finalized=True))

    def test_product_granularity_alignment(self):
        cs=candles([10,12]); engine=BacktestEngine(SMATrend(),FC,ZERO)
        engine.step(cs[0],finalized=True)
        for c in (replace(cs[1],product='ETH-USD'),replace(cs[1],seconds=60)):
            with self.assertRaises(DataError):engine.step(c,finalized=True)
        bar=engine.step(cs[1],finalized=True)
        self.assertEqual((bar.product,bar.start,bar.end),('BTC-USD',cs[1].start,cs[1].start+timedelta(seconds=300)))

    def test_empty_provisional_invalid_input(self):
        for cs,flag in (([],True),([],False),(candles([10]),False),([None],True)):
            with self.assertRaises(DataError):run_backtest(cs,SMATrend(),FC,ZERO,finalized=flag)
        with self.assertRaises(DataError):BacktestEngine(SMATrend(),FC,ZERO).result()

    def test_invalid_config(self):
        for field in ('initial_cash','quantity','fee_fraction','slippage_fraction'):
            for value in (D('NaN'),D('Infinity'),0.1,True,'1'):
                with self.assertRaises(DataError):replace(ZERO,**{field:value})
        for changes in ({'initial_cash':D(0)},{'quantity':D(0)},{'fee_fraction':D(-1)},
                        {'fee_fraction':D(1)},{'slippage_fraction':D(-1)},{'slippage_fraction':D(1)}):
            with self.assertRaises(DataError):replace(ZERO,**changes)
        for strategy in (None,{},'strategy'):
            with self.assertRaises(DataError):BacktestEngine(strategy)
        with self.assertRaises(DataError):BacktestEngine(SMATrend(),FC,{})

    def test_insufficient_cash_atomic(self):
        engine=BacktestEngine(Targets(('long',)),FC,replace(ZERO,initial_cash=D(20)))
        cs=candles([10,100,12]);engine.step(cs[0],finalized=True)
        before=engine.result().serialize()
        with self.assertRaises(DataError):engine.step(cs[1],finalized=True)
        self.assertEqual(engine.result().serialize(),before)
        corrected=replace(cs[1],open=D(11),high=D(101),low=D(10))
        engine.step(corrected,finalized=True)
        self.assertEqual(engine.result().bars[-1].units,1)
        self.assertGreaterEqual(engine.result().bars[-1].cash,0)

    def test_invalid_strategy_output_atomic(self):
        @dataclass
        class Bad:
            name:str='bad'
            def decide(self,c,f):return 'long'
        engine=BacktestEngine(Bad(),FC,ZERO)
        with self.assertRaises(DataError):engine.step(candles([10])[0],finalized=True)
        with self.assertRaises(DataError):engine.result()

    def test_prefixes_and_altered_future(self):
        cs=candles([10,12,11,14,16,13,18,20])
        full=run_backtest(cs,SMATrend(),FC,ZERO,finalized=True)
        for n in range(1,len(cs)+1):
            prefix=run_backtest(cs[:n],SMATrend(),FC,ZERO,finalized=True)
            self.assertEqual(prefix.bars,full.bars[:n])
            self.assertEqual(prefix.fills,tuple(f for f in full.fills if f.time<=cs[n-1].start))
            self.assertEqual(prefix.trades,tuple(t for t in full.trades if t.exit_time<=cs[n-1].start))
        altered=cs[:5]+[replace(c,open=D(100),high=D(101),low=D(99),close=D(100)) for c in cs[5:]]
        result=run_backtest(altered,SMATrend(),FC,ZERO,finalized=True)
        self.assertEqual(result.bars[:5],full.bars[:5])
        self.assertEqual(result.fills[:len([f for f in full.fills if f.time<=cs[4].start])],tuple(f for f in full.fills if f.time<=cs[4].start))

    def test_batch_sequential_and_repeated_bytes(self):
        cs=candles([10+i%7 for i in range(100)])
        for strategy in (SMATrend(),MACDTrend()):
            batch=run_backtest(cs,strategy,FC,ZERO,finalized=True)
            engine=BacktestEngine(strategy,FC,ZERO)
            for c in cs:engine.step(c,finalized=True)
            self.assertEqual(batch.serialize(),engine.result().serialize())
            self.assertEqual(batch.serialize(),run_backtest(iter(cs),strategy,FC,ZERO,finalized=True).serialize())

    def test_snapshot_immutable_and_external_strategy_unchanged(self):
        strategy=Targets(('long','flat','flat'))
        engine=BacktestEngine(strategy,FC,ZERO);cs=candles([10,12,14])
        first=engine.step(cs[0],finalized=True); snapshot=engine.result();before=snapshot.serialize()
        for c in cs[1:]:engine.step(c,finalized=True)
        self.assertEqual(snapshot.serialize(),before)
        self.assertEqual(strategy.index,0)
        with self.assertRaises(AttributeError):first.cash=D(0)

    def test_context_independence(self):
        args=([10,12,11,14,16,13],)
        expected=run(*args,strategy=SMATrend()).serialize()
        with localcontext() as ctx:
            ctx.prec=5;ctx.rounding=ROUND_DOWN;ctx.Emax=9;ctx.Emin=-9
            self.assertEqual(run(*args,strategy=SMATrend()).serialize(),expected)

    def test_zero_volume_kept_without_fabricated_liquidity_filter(self):
        cs=[replace(c,volume=D(0)) for c in candles([10,12,14])]
        result=run_backtest(cs,SMATrend(),FC,ZERO,finalized=True)
        self.assertEqual(len(result.fills),1)
        self.assertEqual(result.fills[0].time,cs[2].start)

    def test_fixture_pinned_replay(self):
        from scripts.validate_backtest import validate
        first=validate()
        self.assertEqual(first,validate())
        self.assertTrue(first['matched'])

    def test_validation_subprocess_output(self):
        from scripts.validate_backtest import EXPECTED_SHA256
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'result.json'
            result=subprocess.run([sys.executable,str(root/'scripts/validate_backtest.py'),'--output',str(out)],
                                  cwd=temp,check=True,capture_output=True,text=True,timeout=15)
            report=json.loads(result.stdout);raw=out.read_bytes()
            self.assertNotIn(b'\r',raw)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),EXPECTED_SHA256)
            self.assertEqual(report['sha256'],EXPECTED_SHA256)

    def test_archive_excludes_provisional_candles(self):
        from trading_bot.market_data import MarketStore
        cs=candles([10,12,14,16])
        with tempfile.TemporaryDirectory() as temp:
            store=MarketStore(Path(temp)/'market.db')
            try:
                for c in cs[:3]:store.put(c,c.start+timedelta(seconds=300),finalized=True,source='rest')
                store.put(cs[3],cs[3].start,finalized=False,source='ws')
                result=run_backtest(store.candles('BTC-USD',300),SMATrend(),FC,ZERO,finalized=True)
                self.assertEqual(result,run_backtest(cs[:3],SMATrend(),FC,ZERO,finalized=True))
            finally:store.close()

    def test_benchmark_independent_of_strategy(self):
        sma=run([10,12,14,12,10,14],strategy=SMATrend())
        macd=run([10,12,14,12,10,14],strategy=MACDTrend())
        self.assertEqual(sma.benchmark_bars,macd.benchmark_bars)
        self.assertEqual(sma.benchmark_metrics,macd.benchmark_metrics)
        self.assertEqual(metrics(sma)['excess_total_return'],
                         metrics(sma)['total_return']-dict(sma.benchmark_metrics)['total_return'])

    def test_future_features_not_supplied(self):
        @dataclass
        class Check:
            name:str='check'
            count:int=0
            def decide(self,c,f):
                self.count+=1
                if f.segment_count!=self.count or f.start!=c.start:
                    raise RuntimeError('unexpected history/future feature')
                return Signal('flat','check')
        result=run([10,12,14,16,18],strategy=Check())
        self.assertEqual(result.fills,())

    def test_custom_strategy_cannot_mutate_execution_candle(self):
        @dataclass
        class Mutating:
            name:str='mutating_test'
            def decide(self,c,f):
                object.__setattr__(c,'open',D(1))
                return Signal('long','test')
        cs=candles([10,12,14])
        result=run_backtest(cs,Mutating(),FC,ZERO,finalized=True)
        self.assertEqual(result.fills[0].reference_price,D(12))
        self.assertEqual(cs[1].open,D(12))

    def test_round_trip_ledger_product_quantity_alignment(self):
        result=run([10,12,14,12,10],['long','flat','long','flat','flat'])
        for fill in result.fills:
            self.assertEqual(fill.product,'BTC-USD')
            self.assertEqual(fill.quantity,D(1))
            self.assertGreaterEqual(fill.time,fill.signal_time)
        for trade in result.trades:
            self.assertEqual(trade.product,'BTC-USD')
            self.assertGreater(trade.exit_time,trade.entry_time)
            self.assertEqual(trade.quantity,D(1))

    def test_arithmetic_failure_preserves_state(self):
        engine=BacktestEngine(Targets(('long',)),FC,ZERO)
        cs=candles([10,12])
        engine.step(cs[0],finalized=True);before=engine.result().serialize()
        huge=replace(cs[1],open=D('1e999999'),high=D('1e999999'),low=D('1e999999'),close=D('1e999999'),volume=D('1e999999'))
        with self.assertRaises(DataError):engine.step(huge,finalized=True)
        self.assertEqual(engine.result().serialize(),before)
        engine.step(cs[1],finalized=True)
        self.assertEqual(engine.result().bars[-1].units,1)

    def test_dependency_scope(self):
        import ast
        folder=Path(__file__).resolve().parents[1]/'trading_bot'/'backtest'
        allowed={'__future__','copy','dataclasses','datetime','decimal','typing',
                 'trading_bot.indicators','trading_bot.market_data.models','common','strategies','engine'}
        for path in folder.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom):self.assertIn(node.module,allowed)
                elif isinstance(node,ast.Import):
                    for alias in node.names:self.assertIn(alias.name,allowed)

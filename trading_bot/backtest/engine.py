"""Offline next-open simulator. Does not submit intents to any broker."""
from __future__ import annotations
from copy import copy, deepcopy
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Iterable

from trading_bot.indicators import FeatureConfig, FeatureEngine
from trading_bot.market_data.models import DataError, MarketCandle
from .common import arithmetic, serialized
from .strategies import Signal, Strategy

D = Decimal


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = D('10000')
    quantity: Decimal = D('0.5')
    fee_fraction: Decimal = D('0.006')
    slippage_fraction: Decimal = D('0.002')

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise DataError('finite Decimal backtest configuration required')
        if self.initial_cash <= 0 or self.quantity <= 0:
            raise DataError('cash and fixed quantity must be positive')
        if not 0 <= self.fee_fraction < 1 or not 0 <= self.slippage_fraction < 1:
            raise DataError('invalid fees/slippage')


@dataclass(frozen=True)
class Fill:
    product: str
    time: datetime
    signal_time: datetime | None
    side: str
    quantity: Decimal
    reference_price: Decimal
    price: Decimal
    fee: Decimal
    cash: Decimal
    units: Decimal


@dataclass(frozen=True)
class Trade:
    product: str
    entry_time: datetime
    exit_time: datetime
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    pnl: Decimal
    return_fraction: Decimal


@dataclass(frozen=True)
class Bar:
    product: str
    start: datetime
    end: datetime
    target: str
    reason: str
    cash: Decimal
    units: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    invested: bool


class _Portfolio:
    def __init__(self, config):
        self.config = config
        self.cash, self.units = config.initial_cash, D(0)
        self.basis, self.realized, self.fees = D(0), D(0), D(0)
        self.entry = None
        # Persistent reverse ledgers allow O(1) append and atomic shallow state copies.
        self._fills, self._trades = None, None

    @staticmethod
    def _ledger(node):
        values = []
        while node is not None:
            value, node = node
            values.append(value)
        return tuple(reversed(values))

    @property
    def fills(self):
        return self._ledger(self._fills)

    @property
    def trades(self):
        return self._ledger(self._trades)

    def transition(self, candle, target, signal_time):
        if (target == 'long') == bool(self.units):
            return
        side = 'BUY' if target == 'long' else 'SELL'
        cfg = self.config
        qty = cfg.quantity if side == 'BUY' else self.units
        price = candle.open * (1 + cfg.slippage_fraction if side == 'BUY' else 1 - cfg.slippage_fraction)
        fee = qty * price * cfg.fee_fraction
        if side == 'BUY':
            basis = qty * price + fee
            if basis > self.cash:
                raise DataError('insufficient cash for fixed-size entry; no leverage allowed')
            self.cash -= basis
            self.units, self.basis = qty, basis
        else:
            proceeds = qty * price - fee
            pnl = proceeds - self.basis
            self.cash += proceeds
            self.realized += pnl
            e = self.entry
            trade = Trade(candle.product, e.time, candle.start, qty, e.price,
                          price, e.fee, fee, pnl, pnl / self.basis)
            self._trades = (trade, self._trades)
            self.units, self.basis = D(0), D(0)
        self.fees += fee
        fill = Fill(candle.product, candle.start, signal_time, side, qty, candle.open,
                    price, fee, self.cash, self.units)
        self._fills = (fill, self._fills)
        self.entry = fill if side == 'BUY' else None

    def mark(self, candle, signal):
        market_value = self.units * candle.close
        return Bar(candle.product, candle.start, candle.start + timedelta(seconds=candle.seconds),
                   signal.target, signal.reason, self.cash, self.units, self.cash + market_value,
                   self.realized, market_value - self.basis, self.fees, bool(self.units))


@dataclass(frozen=True)
class BacktestResult:
    schema: int
    strategy: str
    config: BacktestConfig
    feature_config: FeatureConfig
    bars: tuple[Bar, ...]
    fills: tuple[Fill, ...]
    trades: tuple[Trade, ...]
    benchmark_bars: tuple[Bar, ...]
    benchmark_fills: tuple[Fill, ...]
    metrics: tuple[tuple[str, Decimal | int | None], ...]
    benchmark_metrics: tuple[tuple[str, Decimal | int | None], ...]

    def serialize(self):
        return serialized(self)


def performance(bars, trades, fills, initial_cash, seconds):
    """Closed-trade statistics and close-marked equity metrics, zero risk-free rate."""
    with arithmetic():
        if not bars:
            raise DataError('performance requires at least one candle')
        equities = [initial_cash] + [bar.equity for bar in bars]
        if any(x <= 0 for x in equities):
            raise DataError('positive equity required')
        returns = [b / a - 1 for a, b in zip(equities, equities[1:])]
        peak, drawdown = initial_cash, D(0)
        for equity in equities[1:]:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl < 0]
        count = len(trades)
        mean = sum(returns, D(0)) / len(returns)
        centered = [x - returns[0] for x in returns]
        center_mean = sum(centered, D(0)) / len(centered)
        std = (sum(((x - center_mean) ** 2 for x in centered), D(0)) / len(centered)).sqrt()
        downside = (sum((min(x, D(0)) ** 2 for x in returns), D(0)) / len(returns)).sqrt()
        annualizer = (D(365 * 86400) / seconds).sqrt()
        metrics = dict(total_return=equities[-1] / initial_cash - 1, max_drawdown=drawdown,
                       trade_count=count, fill_count=len(fills), win_rate=D(len(wins))/count if count else None,
                       loss_rate=D(len(losses))/count if count else None,
                       breakeven_count=count-len(wins)-len(losses),
                       average_win=sum(wins,D(0))/len(wins) if wins else None,
                       average_loss=sum(losses,D(0))/len(losses) if losses else None,
                       expectancy=sum((t.pnl for t in trades),D(0))/count if count else None,
                       profit_factor=sum(wins,D(0))/-sum(losses,D(0)) if losses else None,
                       exposure=D(sum(b.invested for b in bars))/len(bars),
                       time_in_market_seconds=sum(b.invested for b in bars)*seconds,
                       realized_pnl=bars[-1].realized_pnl, unrealized_pnl=bars[-1].unrealized_pnl,
                       ending_cash=bars[-1].cash, ending_units=bars[-1].units, ending_equity=equities[-1],
                       total_fees=bars[-1].fees, return_volatility=std if len(returns)>=2 else None,
                       annualized_volatility=std*annualizer if len(returns)>=2 else None,
                       sharpe=mean/std*annualizer if len(returns)>=2 and std else None,
                       sortino=mean/downside*annualizer if len(returns)>=2 and downside else None)
        return tuple(sorted(metrics.items()))


class BacktestEngine:
    """Transactional sequential research replay, with fixed quantity and no shorts.

    Caller certifies finalization. No candle gaps, no same-close fills, no implicit
    final liquidation. All state, including custom strategy state, is copied before
    each step and committed only on success. Results are frozen snapshots.
    """
    def __init__(self, strategy: Strategy, features: FeatureConfig | None = None,
                 config: BacktestConfig | None = None):
        if not callable(getattr(strategy, 'decide', None)) or not isinstance(getattr(strategy,'name',None),str):
            raise DataError('named strategy interface required')
        self._strategy = deepcopy(strategy)
        self._features = FeatureEngine(features)
        if self._features.config.gap_policy != 'reject':
            raise DataError('backtest requires contiguous candles; gap reset not allowed')
        self._config = config if config is not None else BacktestConfig()
        if not isinstance(self._config, BacktestConfig):
            raise DataError('BacktestConfig required')
        self._portfolio = _Portfolio(self._config)
        self._benchmark = _Portfolio(self._config)
        self._pending, self._signal_time = 'flat', None
        self._bars, self._benchmark_bars = [], []

    def step(self, candle: MarketCandle, *, finalized: bool) -> Bar:
        # FeatureEngine strictly validates schema, alignment, monotonicity and gaps.
        features = deepcopy(self._features)
        feature_row = features.update(candle, finalized=finalized)
        strategy = deepcopy(self._strategy)
        account, benchmark = copy(self._portfolio), copy(self._benchmark)
        try:
            with arithmetic():
                signal = strategy.decide(deepcopy(candle), feature_row)
                if not isinstance(signal, Signal):
                    raise DataError('strategy must return a Signal')
                Signal(signal.target, signal.reason)
                account.transition(candle, self._pending, self._signal_time)
                # Buy-and-hold is a prespecified target, executed at the first open.
                benchmark.transition(candle, 'long', None)
                bar = account.mark(candle, signal)
                bench_bar = benchmark.mark(candle, Signal('long','buy_and_hold'))
                if not bar.equity.is_finite() or bar.equity <= 0 or bench_bar.equity <= 0:
                    raise DataError('invalid simulated equity')
        except DecimalException as exc:
            raise DataError('backtest arithmetic failed') from exc
        self._features, self._strategy = features, strategy
        self._portfolio, self._benchmark = account, benchmark
        self._pending = signal.target
        self._signal_time = feature_row.available_at
        self._bars.append(bar)
        self._benchmark_bars.append(bench_bar)
        return bar

    def result(self) -> BacktestResult:
        if not self._bars:
            raise DataError('backtest requires nonempty finalized input')
        cfg, p, b = self._config, self._portfolio, self._benchmark
        seconds = self._bars[0].end - self._bars[0].start
        strategy_metrics = dict(performance(self._bars, p.trades, p.fills, cfg.initial_cash,
                                            int(seconds.total_seconds())))
        benchmark_metrics = performance(self._benchmark_bars, b.trades, b.fills, cfg.initial_cash,
                                        int(seconds.total_seconds()))
        with arithmetic():
            strategy_metrics['excess_total_return'] = (strategy_metrics['total_return']
                                                       - dict(benchmark_metrics)['total_return'])
        return BacktestResult(1, self._strategy.name, cfg, self._features.config,
                              tuple(self._bars), tuple(p.fills), tuple(p.trades),
                              tuple(self._benchmark_bars), tuple(b.fills),
                              tuple(sorted(strategy_metrics.items())), benchmark_metrics)


def run_backtest(candles: Iterable[MarketCandle], strategy: Strategy,
                 features: FeatureConfig | None = None, config: BacktestConfig | None = None,
                 *, finalized: bool) -> BacktestResult:
    if finalized is not True:
        raise DataError('finalized historical input required')
    engine = BacktestEngine(strategy,features,config)
    for candle in candles:
        engine.step(candle,finalized=True)
    return engine.result()

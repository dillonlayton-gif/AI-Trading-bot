"""Causal, bounded-state indicators using an isolated 34-digit Decimal context."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import (Context, Decimal, DecimalException, DivisionByZero,
                     InvalidOperation, Overflow, ROUND_HALF_EVEN, Underflow, localcontext)
from typing import Iterable

from trading_bot.market_data.models import DataError, MarketCandle, decimal_text

D = Decimal


@dataclass(frozen=True)
class FeatureConfig:
    sma: int = 20
    ema: int = 20
    rsi: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr: int = 14
    vwap: int = 20
    momentum: int = 10
    volatility: int = 20
    volume: int = 20
    gap_policy: str = 'reject'

    def __post_init__(self):
        for field in fields(self):
            if field.name == 'gap_policy':
                continue
            value = getattr(self, field.name)
            if type(value) is not int or not 1 <= value <= 10000:
                raise DataError('indicator periods must be integers in [1, 10000]')
        if self.macd_fast >= self.macd_slow:
            raise DataError('MACD fast period must be below slow period')
        if self.gap_policy not in ('reject', 'reset'):
            raise DataError('gap policy must be reject or reset')


@dataclass(frozen=True)
class FeatureRow:
    product: str
    start: datetime
    available_at: datetime
    seconds: int
    segment_count: int
    gap_reset: bool
    values: tuple[tuple[str, Decimal | None], ...]
    unavailable: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict:
        return dict(product=self.product, start=self.start.isoformat(),
                    available_at=self.available_at.isoformat(), seconds=self.seconds,
                    segment_count=self.segment_count, gap_reset=self.gap_reset,
                    values={k: None if v is None else decimal_text(v) for k, v in self.values},
                    unavailable=dict(self.unavailable))


def mean(values):
    return sum(values, D(0)) / len(values)


def ema_step(previous, prices, period):
    if previous is None:
        return mean(prices[-period:]) if len(prices) >= period else None
    alpha = D(2) / (period + 1)
    return previous + alpha * (prices[-1] - previous)


class FeatureEngine:
    """One product/granularity stream. Caller must certify each candle as finalized.

    Rejects revisions, duplicates and earlier timestamps. Gap reset is opt-in.
    State is committed only after successful arithmetic; previous rows are immutable.
    """
    def __init__(self, config: FeatureConfig | None = None):
        self._config = config if config is not None else FeatureConfig()
        if not isinstance(self.config, FeatureConfig):
            raise DataError('FeatureConfig required')
        self._limit = max(getattr(self.config, f.name) for f in fields(self.config)
                          if f.name != 'gap_policy') + 1
        self._candles = deque(maxlen=self._limit)
        self._returns = deque(maxlen=self.config.volatility)
        self._state = {}
        self._count = 0

    @property
    def config(self) -> FeatureConfig:
        """Configuration is fixed for the lifetime of this stream."""
        return self._config

    def update(self, candle: MarketCandle, *, finalized: bool) -> FeatureRow:
        if finalized is not True:
            raise DataError('only finalized candles may produce features')
        if not isinstance(candle, MarketCandle):
            raise DataError('normalized MarketCandle required')
        # Revalidate even externally constructed/tampered dataclass instances.
        if type(candle.seconds) is not int:
            raise DataError('integer candle granularity required')
        candle = MarketCandle(**{f.name: getattr(candle, f.name) for f in fields(MarketCandle)})
        previous = self._candles[-1] if self._candles else None
        gap = False
        if previous:
            if (candle.product, candle.seconds) != (previous.product, previous.seconds):
                raise DataError('product/granularity mismatch')
            if candle.start <= previous.start:
                raise DataError('duplicate or out-of-order finalized candle')
            gap = candle.epoch != previous.end_epoch
            if gap and self.config.gap_policy == 'reject':
                raise DataError('missing candle: repair data or explicitly reset warm-up')
        candles = list(self._candles) if not gap else []
        returns = list(self._returns) if not gap else []
        state = dict(self._state) if not gap else {}
        count = (self._count if not gap else 0) + 1
        previous = candles[-1] if candles else None
        candles.append(candle)
        try:
            with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-999999,
                                      Emax=999999, capitals=1, clamp=0, flags=[],
                                      traps=[InvalidOperation, DivisionByZero, Overflow, Underflow])):
                row = self._compute(candles, returns, state, count, gap, previous)
                if any(v is not None and not v.is_finite() for _, v in row.values):
                    raise DataError('nonfinite indicator result')
        except DecimalException as exc:
            raise DataError('indicator arithmetic failed') from exc
        self._candles = deque(candles, maxlen=self._limit)
        self._returns = deque(returns, maxlen=self.config.volatility)
        self._state, self._count = state, count
        return row

    def _compute(self, candles, returns, state, count, gap, previous):
        cfg = self.config
        c = candles[-1]
        prices = [x.close for x in candles]
        values, unavailable = {}, {}

        def put(name, value, reason='warmup'):
            values[name] = value
            if value is None:
                unavailable[name] = reason

        put('sma', mean(prices[-cfg.sma:]) if count >= cfg.sma else None)
        state['ema'] = ema_step(state.get('ema'), prices, cfg.ema)
        put('ema', state['ema'])
        change = c.close - previous.close if previous else None
        gains, losses = state.get('gain_sum', D(0)), state.get('loss_sum', D(0))
        if change is not None:
            gain, loss = max(change, D(0)), max(-change, D(0))
            if count <= cfg.rsi + 1:
                gains, losses = gains + gain, losses + loss
                state['gain_sum'], state['loss_sum'] = gains, losses
                if count == cfg.rsi + 1:
                    state['gain'], state['loss'] = gains / cfg.rsi, losses / cfg.rsi
            else:
                state['gain'] = (state['gain'] * (cfg.rsi - 1) + gain) / cfg.rsi
                state['loss'] = (state['loss'] * (cfg.rsi - 1) + loss) / cfg.rsi
        rsi = None
        if 'gain' in state:
            g, l = state['gain'], state['loss']
            rsi = D(50) if g == l == 0 else D(100) if l == 0 else D(100) - D(100) / (1 + g / l)
        put('rsi', rsi)
        for name, period in (('fast', cfg.macd_fast), ('slow', cfg.macd_slow)):
            state[name] = ema_step(state.get(name), prices, period)
        macd = state['fast'] - state['slow'] if state['slow'] is not None else None
        put('macd', macd)
        if macd is not None:
            signal_seed = list(state.get('signal_seed', ()))
            signal_seed.append(macd)
            signal_seed = signal_seed[-cfg.macd_signal:]
            state['signal'] = ema_step(state.get('signal'), signal_seed, cfg.macd_signal)
            state['signal_seed'] = tuple(signal_seed)
        put('macd_signal', state.get('signal'))
        put('macd_histogram', macd - state['signal'] if state.get('signal') is not None else None)
        tr = max(c.high - c.low, abs(c.high - previous.close), abs(c.low - previous.close)) if previous else c.high - c.low
        if count <= cfg.atr:
            state['tr_sum'] = state.get('tr_sum', D(0)) + tr
            if count == cfg.atr:
                state['atr'] = state['tr_sum'] / cfg.atr
        else:
            state['atr'] = (state['atr'] * (cfg.atr - 1) + tr) / cfg.atr
        put('atr', state.get('atr'))
        window = candles[-cfg.vwap:]
        total_volume = sum((x.volume for x in window), D(0))
        vwap = sum((((x.high + x.low + x.close) / 3) * x.volume for x in window), D(0)) / total_volume if total_volume else None
        put('vwap', vwap if count >= cfg.vwap else None,
            'zero_volume' if count >= cfg.vwap and not total_volume else 'warmup')
        simple = c.close / previous.close - 1 if previous else None
        log = (c.close / previous.close).ln() if previous else None
        put('simple_return', simple)
        put('log_return', log)
        if log is not None:
            returns.append(log)
            del returns[:-cfg.volatility]
        ready = count > cfg.momentum
        put('momentum', c.close - prices[-cfg.momentum-1] if ready else None)
        put('roc', c.close / prices[-cfg.momentum-1] - 1 if ready else None)
        volatility = None
        if len(returns) >= cfg.volatility:
            # Center before averaging to preserve exact zero for identical returns.
            centered = [x - returns[0] for x in returns]
            avg = mean(centered)
            volatility = mean([(x - avg) ** 2 for x in centered]).sqrt()
        put('volatility', volatility)
        vols = [x.volume for x in candles[-cfg.volume:]]
        volume_avg = mean(vols) if count >= cfg.volume else None
        put('volume_sma', volume_avg)
        put('relative_volume', c.volume / volume_avg if volume_avg else None,
            'zero_volume' if volume_avg == 0 else 'warmup')
        prior_vols = [x.volume for x in candles[-cfg.volume-1:-1]]
        prior_avg = mean(prior_vols) if count > cfg.volume else None
        put('volume_vs_prior', c.volume / prior_avg if prior_avg else None,
            'zero_volume' if prior_avg == 0 else 'warmup')
        return FeatureRow(c.product, c.start, c.start + timedelta(seconds=c.seconds), c.seconds,
                          count, gap, tuple(sorted(values.items())), tuple(sorted(unavailable.items())))


def calculate(candles: Iterable[MarketCandle], config: FeatureConfig | None = None, *, finalized: bool) -> tuple[FeatureRow, ...]:
    """Batch facade over the same sequential algorithm; no sorting or future reads."""
    if finalized is not True:
        raise DataError('only finalized candles may produce features')
    engine = FeatureEngine(config)
    return tuple(engine.update(c, finalized=finalized) for c in candles)

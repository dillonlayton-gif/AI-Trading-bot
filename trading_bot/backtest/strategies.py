"""Explicit stateless long/flat baselines; no optimization or strategy selection."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from trading_bot.indicators import FeatureRow
from trading_bot.market_data.models import DataError, MarketCandle


@dataclass(frozen=True)
class Signal:
    target: str
    reason: str

    def __post_init__(self):
        if self.target not in ('long', 'flat') or not isinstance(self.reason, str) or not self.reason:
            raise DataError('valid long/flat signal and reason required')


class Strategy(Protocol):
    """Decide using only one current finalized candle and its causal feature row.

    A custom implementation must be deterministic and deepcopy-compatible. Never
    access external state, future input or network. The engine supplies no future rows.
    """
    name: str

    def decide(self, candle: MarketCandle, features: FeatureRow) -> Signal: ...


def aligned(candle, features):
    if (features.product, features.start, features.seconds) != (candle.product, candle.start, candle.seconds):
        raise DataError('strategy feature alignment mismatch')
    if features.available_at != candle.start + timedelta(seconds=candle.seconds):
        raise DataError('strategy feature availability mismatch')


@dataclass(frozen=True)
class SMATrend:
    """Long iff close > SMA; otherwise flat. Flat until the SMA period is ready."""
    name: str = 'sma_trend'

    def decide(self, candle, features):
        aligned(candle, features)
        sma = dict(features.values)['sma']
        if sma is None:
            return Signal('flat', 'sma_warmup')
        return Signal('long' if candle.close > sma else 'flat', 'close_vs_sma')


@dataclass(frozen=True)
class MACDTrend:
    """Long iff MACD > signal EMA; otherwise flat. Flat until both are ready."""
    name: str = 'macd_trend'

    def decide(self, candle, features):
        aligned(candle, features)
        values = dict(features.values)
        macd, signal = values['macd'], values['macd_signal']
        if macd is None or signal is None:
            return Signal('flat', 'macd_warmup')
        return Signal('long' if macd > signal else 'flat', 'macd_vs_signal')

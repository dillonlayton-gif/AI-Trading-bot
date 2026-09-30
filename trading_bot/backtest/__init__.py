"""Deterministic offline strategy research; no broker or live-order capability."""
from .engine import BacktestConfig, BacktestEngine, BacktestResult, run_backtest
from .strategies import MACDTrend, SMATrend, Signal, Strategy

__all__ = ['BacktestConfig', 'BacktestEngine', 'BacktestResult', 'run_backtest',
           'MACDTrend', 'SMATrend', 'Signal', 'Strategy']

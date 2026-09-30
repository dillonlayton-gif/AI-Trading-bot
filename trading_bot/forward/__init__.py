"""Fixed-strategy, risk-gated forward paper sessions. No exchange orders."""
from .runner import ForwardConfig, ForwardRunner, aggregate
__all__ = ['ForwardConfig', 'ForwardRunner', 'aggregate']

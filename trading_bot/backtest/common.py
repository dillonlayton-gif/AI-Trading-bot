"""Canonical serialization and isolated arithmetic for offline research."""
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import (Context, Decimal, DivisionByZero, InvalidOperation, Overflow,
                     ROUND_HALF_EVEN, Underflow, localcontext)
from trading_bot.market_data.models import json_text, decimal_text


def arithmetic():
    return localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN, Emin=-999999,
                               Emax=999999, capitals=1, clamp=0, flags=[],
                               traps=[InvalidOperation, DivisionByZero, Overflow, Underflow]))


def primitive(value):
    if is_dataclass(value):
        return primitive(asdict(value))
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: primitive(v) for key, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    return value


def serialized(value):
    return json_text(primitive(value)) + '\n'

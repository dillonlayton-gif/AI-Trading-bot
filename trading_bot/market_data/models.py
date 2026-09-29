"""Strict Coinbase normalization without binary floating-point prices."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

UTC = timezone.utc
GRANULARITIES = {60: "ONE_MINUTE", 300: "FIVE_MINUTE", 900: "FIFTEEN_MINUTE",
                 1800: "THIRTY_MINUTE", 3600: "ONE_HOUR", 7200: "TWO_HOUR",
                 14400: "FOUR_HOUR", 21600: "SIX_HOUR", 86400: "ONE_DAY"}


class DataError(ValueError):
    """Invalid or inconsistent exchange data; must never be consumed silently."""


def product_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{1,20}-[A-Z0-9]{1,20}", value):
        raise DataError("invalid product identifier")
    return value


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataError("timezone-aware datetime required")
    return value.astimezone(UTC)


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise DataError("timestamp must be a string")
    try:
        return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (ValueError, OverflowError) as exc:
        raise DataError("invalid UTC timestamp") from exc


def integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,12}", str(value)):
        raise DataError("nonnegative integer required")
    return int(value)


def decimal(value: Any, *, zero: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise DataError("plain decimal string required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise DataError("invalid decimal") from exc
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise DataError("invalid price or volume")
    return result


def decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class MarketCandle:
    product: str
    start: datetime
    seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self):
        product_id(self.product)
        start = utc(self.start)
        if self.seconds not in GRANULARITIES or isinstance(self.seconds, bool):
            raise DataError("unsupported granularity")
        if start.microsecond or int(start.timestamp()) < 0 or int(start.timestamp()) % self.seconds:
            raise DataError("candle start is not bucket-aligned")
        object.__setattr__(self, "start", start)
        for value in (self.open, self.high, self.low, self.close, self.volume):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise DataError("finite Decimal values required")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise DataError("nonpositive price or negative volume")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise DataError("inconsistent OHLC bounds")

    @property
    def epoch(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_epoch(self) -> int:
        return self.epoch + self.seconds

    def as_dict(self) -> dict:
        return {"product": self.product, "start": self.start.isoformat(), "seconds": self.seconds,
                **{key: decimal_text(getattr(self, key)) for key in ("open", "high", "low", "close", "volume")}}

    @classmethod
    def from_dict(cls, row: dict) -> MarketCandle:
        return cls(row["product"], timestamp(row["start"]), integer(row["seconds"]),
                   *(decimal(row[key], zero=key == "volume") for key in ("open", "high", "low", "close", "volume")))


def normalize_candle(row: dict, product: str, seconds: int) -> MarketCandle:
    try:
        if not isinstance(row, dict) or row.get("product_id", product) != product:
            raise DataError("candle product mismatch")
        start = datetime.fromtimestamp(integer(row["start"]), UTC)
        return MarketCandle(product, start, seconds,
                            *(decimal(row[key], zero=key == "volume") for key in ("open", "high", "low", "close", "volume")))
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise DataError("invalid candle schema") from exc

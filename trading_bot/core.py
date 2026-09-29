"""Deterministic paper execution with a mandatory risk gate."""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

LOG = logging.getLogger(__name__)
D = Decimal


def amount(value: str | Decimal) -> Decimal:
    try:
        result = D(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("non-finite decimal")
    return result


@dataclass(frozen=True)
class Candle:
    time: datetime
    price: Decimal
    product: str


@dataclass(frozen=True)
class Intent:
    side: str
    quantity: Decimal
    product: str


@dataclass(frozen=True)
class Limits:
    max_order_quote: Decimal = D("100")
    max_position_fraction: Decimal = D("0.20")
    daily_loss_fraction: Decimal = D("0.03")
    fee_fraction: Decimal = D("0.006")
    slippage_fraction: Decimal = D("0.002")
    max_data_age_seconds: int = 3600

    def __post_init__(self):
        if not (self.max_order_quote > 0 and D(0) < self.max_position_fraction <= 1
                and D(0) < self.daily_loss_fraction < 1 and D(0) <= self.fee_fraction < 1
                and D(0) <= self.slippage_fraction < 1 and self.max_data_age_seconds > 0):
            raise ValueError("invalid risk limits")


class PaperLedger:
    def __init__(self, path: Path, initial_cash: Decimal):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)")
        with self.db:
            if self.get("cash") is None:
                if initial_cash <= 0:
                    raise ValueError("initial cash must be positive")
                for key, value in {"cash": initial_cash, "units": D(0), "day": "", "day_start": initial_cash,
                                   "last_candle": "", "product": "", "stopped": "0"}.items():
                    self.set(key, str(value))
        self.db.commit()

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))

    def event(self, time: datetime, kind: str, **detail):
        self.db.execute("INSERT INTO events(time,kind,detail) VALUES (?,?,?)",
                        (time.isoformat(), kind, json.dumps(detail, sort_keys=True)))
        LOG.info("%s %s", kind, json.dumps(detail, sort_keys=True))

    def close(self):
        self.db.close()


class RiskEngine:
    def __init__(self, limits: Limits):
        self.limits = limits

    def check(self, intent: Intent, candle: Candle, ledger: PaperLedger) -> str | None:
        if ledger.get("stopped") == "1":
            return "kill_switch"
        if intent.product != candle.product or ledger.get("product") not in ("", intent.product):
            return "product_mismatch"
        if intent.side not in ("BUY", "SELL") or not intent.quantity.is_finite() or intent.quantity <= 0:
            return "invalid_intent"
        if candle.price <= 0 or not candle.price.is_finite():
            return "invalid_price"
        cash, units = amount(ledger.get("cash")), amount(ledger.get("units"))
        equity = cash + units * candle.price
        if equity <= 0:
            return "no_equity"
        if equity <= amount(ledger.get("day_start")) * (1 - self.limits.daily_loss_fraction):
            return "daily_loss_limit"
        fill = candle.price * (1 + self.limits.slippage_fraction if intent.side == "BUY" else 1 - self.limits.slippage_fraction)
        notional = intent.quantity * fill
        if notional > self.limits.max_order_quote:
            return "order_limit"
        if intent.side == "BUY":
            if notional * (1 + self.limits.fee_fraction) > cash:
                return "insufficient_cash"
            if (units + intent.quantity) * candle.price > equity * self.limits.max_position_fraction:
                return "position_limit"
        elif intent.quantity > units:
            return "insufficient_units"
        return None


class PaperBroker:
    """Only execution capability; risk checking occurs inside submit."""
    def __init__(self, ledger: PaperLedger, risk: RiskEngine):
        self.ledger, self.risk = ledger, risk

    def submit(self, intent: Intent, candle: Candle) -> bool:
        reason = self.risk.check(intent, candle, self.ledger)
        with self.ledger.db:
            if reason:
                self.ledger.event(candle.time, "rejected", reason=reason, side=intent.side, quantity=str(intent.quantity))
                return False
            limits = self.risk.limits
            fill = candle.price * (1 + limits.slippage_fraction if intent.side == "BUY" else 1 - limits.slippage_fraction)
            notional = intent.quantity * fill
            fee = notional * limits.fee_fraction
            cash = amount(self.ledger.get("cash")) + (-(notional + fee) if intent.side == "BUY" else notional - fee)
            units = amount(self.ledger.get("units")) + (intent.quantity if intent.side == "BUY" else -intent.quantity)
            self.ledger.set("cash", str(cash))
            self.ledger.set("units", str(units))
            self.ledger.event(candle.time, "fill", side=intent.side, quantity=str(intent.quantity),
                              price=str(fill), fee=str(fee), cash=str(cash), units=str(units))
        return True


class PaperRunner:
    def __init__(self, ledger: PaperLedger, broker: PaperBroker):
        self.ledger, self.broker = ledger, broker

    def step(self, candle: Candle, intent: Intent | None = None):
        if candle.time.tzinfo is None or candle.time.utcoffset() is None:
            raise ValueError("timestamp must have timezone")
        if candle.price <= 0 or not candle.price.is_finite():
            raise ValueError("invalid candle price")
        last = self.ledger.get("last_candle")
        stamp = candle.time.astimezone(timezone.utc).isoformat()
        if last and stamp <= last:
            raise ValueError("duplicate or out-of-order candle")
        if self.ledger.get("product") not in ("", candle.product):
            raise ValueError("product changed")
        day = candle.time.astimezone(timezone.utc).date().isoformat()
        with self.ledger.db:
            if self.ledger.get("day") != day:
                self.ledger.set("day", day)
                self.ledger.set("day_start", str(amount(self.ledger.get("cash")) + amount(self.ledger.get("units")) * candle.price))
            self.ledger.set("product", candle.product)
            self.ledger.set("last_candle", stamp)
            self.ledger.event(candle.time, "mark", product=candle.product, price=str(candle.price))
        if intent is not None:
            return self.broker.submit(intent, candle)
        return None


def read_candles(path: Path, product: str):
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"time", "price"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV requires time,price columns")
        for row in reader:
            yield Candle(datetime.fromisoformat(row["time"].replace("Z", "+00:00")), amount(row["price"]), product)

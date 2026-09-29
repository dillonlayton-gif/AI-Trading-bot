"""Transactional public-data journal and canonical, finalized candle replay."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator
from .models import MarketCandle, json_text, utc

LOG = logging.getLogger(__name__)


class MarketStore:
    def __init__(self, path: Path, *, readonly: bool = False):
        self.db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro" if readonly else path,
                                  uri=readonly, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS md_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS md_candles(
                    product TEXT NOT NULL, seconds INTEGER NOT NULL, start INTEGER NOT NULL,
                    payload TEXT NOT NULL, finalized INTEGER NOT NULL CHECK(finalized IN (0,1)),
                    observed_at TEXT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(product,seconds,start));
                CREATE TABLE IF NOT EXISTS md_journal(
                    id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, source TEXT NOT NULL,
                    session TEXT NOT NULL, raw TEXT NOT NULL, outcome TEXT NOT NULL);
                INSERT OR IGNORE INTO md_meta VALUES ('schema_version','1');
            """)
        row = self.db.execute("SELECT value FROM md_meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != "1":
            raise ValueError("unsupported market-data schema version")

    def transaction(self):
        return _Transaction(self.db)

    def close(self):
        self.db.close()

    def journal(self, received: datetime, source: str, session: str, raw: str, outcome: dict) -> int:
        cursor = self.db.execute("INSERT INTO md_journal(received_at,source,session,raw,outcome) VALUES (?,?,?,?,?)",
                                 (utc(received).isoformat(), source, session, raw, json_text(outcome)))
        LOG.info("market_data %s", json_text(outcome))
        return cursor.lastrowid

    def get(self, product: str, seconds: int, start: int):
        return self.db.execute("SELECT * FROM md_candles WHERE product=? AND seconds=? AND start=?",
                               (product, seconds, start)).fetchone()

    def latest_start(self, product: str, seconds: int) -> int | None:
        return self.db.execute("SELECT MAX(start) FROM md_candles WHERE product=? AND seconds=?",
                               (product, seconds)).fetchone()[0]

    def put(self, candle: MarketCandle, received: datetime, *, finalized: bool, source: str) -> str:
        payload = json_text(candle.as_dict())
        row = self.get(candle.product, candle.seconds, candle.epoch)
        if row:
            if row["finalized"]:
                return "duplicate" if payload == row["payload"] else "conflict"
            if finalized:
                result = "finalized"
            elif payload == row["payload"]:
                return "duplicate"
            elif utc(received).isoformat() <= row["observed_at"]:
                return "out_of_order"
            else:
                previous = MarketCandle.from_dict(json.loads(row["payload"]))
                if (candle.open != previous.open or candle.high < previous.high or candle.low > previous.low
                        or candle.volume < previous.volume):
                    return "conflict"
                result = "revision"
        else:
            result = "finalized" if finalized else "accepted"
        self.db.execute("INSERT OR REPLACE INTO md_candles VALUES (?,?,?,?,?,?,?)",
                        (candle.product, candle.seconds, candle.epoch, payload, int(finalized),
                         utc(received).isoformat(), source))
        return result

    def candles(self, product: str, seconds: int, *, finalized: bool = True,
                start: int | None = None, end: int | None = None) -> Iterator[MarketCandle]:
        query = "SELECT payload FROM md_candles WHERE product=? AND seconds=?"
        args: list = [product, seconds]
        if finalized:
            query += " AND finalized=1"
        if start is not None:
            query += " AND start>=?"
            args.append(start)
        if end is not None:
            query += " AND start<?"
            args.append(end)
        query += " ORDER BY start ASC"
        for row in self.db.execute(query, args):
            yield MarketCandle.from_dict(json.loads(row[0]))

    def replay_jsonl(self, product: str, seconds: int) -> Iterator[str]:
        """Completed candles only; no wall clock, network, or execution dependency."""
        for candle in self.candles(product, seconds):
            yield json_text(candle.as_dict()) + "\n"

    def replay_digest(self, product: str, seconds: int) -> str:
        digest = hashlib.sha256()
        for line in self.replay_jsonl(product, seconds):
            digest.update(line.encode())
        return digest.hexdigest()

    def journal_records(self):
        yield from self.db.execute("SELECT * FROM md_journal ORDER BY id")

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO md_meta VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM md_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


class _Transaction:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, kind, value, trace):
        self.db.execute("ROLLBACK" if kind else "COMMIT")

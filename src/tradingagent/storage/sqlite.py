"""SQLite persistence for validated OHLCV candles."""
from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable

from tradingagent.models import Candle


class CandleStore:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS candles (
                exchange TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
                low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'ccxt', complete INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(exchange, symbol, timeframe, timestamp_ms))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(candles)")}
            if "source" not in columns:
                db.execute("ALTER TABLE candles ADD COLUMN source TEXT NOT NULL DEFAULT 'ccxt'")
            if "complete" not in columns:
                db.execute("ALTER TABLE candles ADD COLUMN complete INTEGER NOT NULL DEFAULT 1")
            db.execute("CREATE INDEX IF NOT EXISTS ix_candles_time ON candles(symbol,timeframe,timestamp_ms)")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_configs (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backtest_results (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ticker_snapshots (id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_ticker_symbol_time ON ticker_snapshots(exchange,symbol,timestamp_ms);
                CREATE TABLE IF NOT EXISTS orderbook_snapshots (id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_orderbook_symbol_time ON orderbook_snapshots(exchange,symbol,timestamp_ms);
                CREATE TABLE IF NOT EXISTS market_data_runs (id INTEGER PRIMARY KEY, exchange TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, requested_since_ms INTEGER, requested_until_ms INTEGER, fetched_at_ms INTEGER NOT NULL, source TEXT NOT NULL, complete INTEGER NOT NULL, record_count INTEGER NOT NULL, provenance TEXT NOT NULL, quality TEXT NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15)

    def upsert(self, exchange: str, symbol: str, timeframe: str, candles: Iterable[Candle], source: str | None = None, complete: bool | None = None) -> int:
        rows = [(exchange, symbol, timeframe, c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume,
                 source or (c.source if c.source != "unknown" else exchange),
                 int(c.complete if complete is None else complete)) for c in candles]
        with self._connect() as db:
            db.executemany("INSERT OR REPLACE INTO candles (exchange,symbol,timeframe,timestamp_ms,open,high,low,close,volume,source,complete) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def load(self, exchange: str, symbol: str, timeframe: str, limit: int | None = 1000,
             include_incomplete: bool = False) -> list[Candle]:
        complete_filter = "" if include_incomplete else " AND complete=1"
        limit_filter = "" if limit is None else " LIMIT ?"
        parameters = (exchange, symbol, timeframe) if limit is None else (exchange, symbol, timeframe, limit)
        with self._connect() as db:
            rows = db.execute(f"""SELECT timestamp_ms,open,high,low,close,volume,source,complete FROM candles
                WHERE exchange=? AND symbol=? AND timeframe=?{complete_filter} ORDER BY timestamp_ms DESC{limit_filter}""",
                parameters).fetchall()
        return [Candle(*row[:6], row[6], bool(row[7])) for row in reversed(rows)]

    def load_before(self, exchange: str, symbol: str, timeframe: str, before_ms: int,
                    limit: int, include_incomplete: bool = False) -> list[Candle]:
        """Load a bounded page strictly older than a timestamp, in chronological order."""
        if before_ms < 0 or limit <= 0:
            raise ValueError("before_ms must be nonnegative and limit positive")
        complete_filter = "" if include_incomplete else " AND complete=1"
        with self._connect() as db:
            rows = db.execute(f"""SELECT timestamp_ms,open,high,low,close,volume,source,complete FROM candles
                WHERE exchange=? AND symbol=? AND timeframe=? AND timestamp_ms<?{complete_filter}
                ORDER BY timestamp_ms DESC LIMIT ?""", (exchange,symbol,timeframe,before_ms,limit)).fetchall()
        return [Candle(*row[:6],row[6],bool(row[7])) for row in reversed(rows)]

    def latest_timestamp(self, exchange: str, symbol: str, timeframe: str,
                         include_incomplete: bool = True) -> int | None:
        clause = "" if include_incomplete else " AND complete=1"
        with self._connect() as db:
            row = db.execute(f"SELECT MAX(timestamp_ms) FROM candles WHERE exchange=? AND symbol=? AND timeframe=?{clause}",
                             (exchange, symbol, timeframe)).fetchone()
        return row[0] if row else None

    def sources(self, exchange: str, symbol: str, timeframe: str) -> tuple[str, ...]:
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT source FROM candles WHERE exchange=? AND symbol=? AND timeframe=?",
                              (exchange, symbol, timeframe)).fetchall()
        return tuple(row[0] for row in rows)

    def save_payload(self, table: str, symbol: str, timeframe: str, timestamp_ms: int, source: str, payload: object) -> int:
        """Persist a signal, paper trade, or backtest result as an auditable JSON record."""
        if table not in {"signals", "paper_trades", "backtest_results"}:
            raise ValueError("unsupported payload table")
        with self._connect() as db:
            cursor = db.execute(
                f"INSERT INTO {table}(symbol,timeframe,timestamp_ms,source,complete,payload) VALUES (?,?,?,?,1,?)",
                (symbol, timeframe, timestamp_ms, source, json.dumps(payload, separators=(",", ":"), default=str)),
            )
            return int(cursor.lastrowid)

    def save_strategy_config(self, name: str, payload: object) -> int:
        with self._connect() as db:
            cursor = db.execute("INSERT INTO strategy_configs(name,created_at,payload) VALUES (?,?,?)",
                                (name, datetime.now(timezone.utc).isoformat(), json.dumps(payload, default=str)))
            return int(cursor.lastrowid)

    def save_ticker(self, exchange: str, symbol: str, timestamp_ms: int,
                    source: str, payload: object) -> int:
        with self._connect() as db:
            cursor = db.execute("INSERT INTO ticker_snapshots(exchange,symbol,timestamp_ms,source,complete,payload) VALUES (?,?,?,?,1,?)",
                                (exchange, symbol, timestamp_ms, source, json.dumps(payload, default=str)))
            return int(cursor.lastrowid)

    def save_order_book(self, exchange: str, symbol: str, timestamp_ms: int,
                        source: str, payload: object) -> int:
        with self._connect() as db:
            cursor = db.execute("INSERT INTO orderbook_snapshots(exchange,symbol,timestamp_ms,source,complete,payload) VALUES (?,?,?,?,1,?)",
                                (exchange, symbol, timestamp_ms, source, json.dumps(payload, default=str)))
            return int(cursor.lastrowid)

    def record_market_data_run(self, exchange: str, symbol: str, timeframe: str,
                               requested_since_ms: int | None, requested_until_ms: int | None,
                               fetched_at_ms: int, source: str, complete: bool,
                               record_count: int, provenance: object, quality: object) -> int:
        with self._connect() as db:
            cursor = db.execute("""INSERT INTO market_data_runs
                (exchange,symbol,timeframe,requested_since_ms,requested_until_ms,fetched_at_ms,source,complete,record_count,provenance,quality)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (exchange, symbol, timeframe, requested_since_ms, requested_until_ms, fetched_at_ms,
                 source, int(complete), record_count, json.dumps(provenance, default=str), json.dumps(quality, default=str)))
            return int(cursor.lastrowid)

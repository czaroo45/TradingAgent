"""Persistent simulated long/short portfolio with explicit signal/fill separation."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from tradingagent.models import (Candle, ExecutionRole, ExecutionSide, PositionSide,
                                 StrategySignal)
from tradingagent.market_data import timeframe_to_ms
from tradingagent.risk import (InstrumentConstraints, OpenPosition, PortfolioSnapshot,
                               RiskManager, RiskRequest)


@dataclass(frozen=True, slots=True)
class PaperPosition:
    symbol: str
    quantity: float
    average_entry: float
    stop_loss: float
    targets: tuple[float, ...]
    next_target: int
    entry_fees: float
    opened_at_ms: int
    source: str
    timeframe: str
    strategy: str
    regime: str = "unknown"
    position_side: PositionSide = "LONG"

    def __post_init__(self) -> None:
        if self.position_side not in {"LONG", "SHORT"}:
            raise ValueError(f"unsupported paper position side: {self.position_side!r}")


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: int
    symbol: str
    side: ExecutionSide
    quantity: float
    price: float
    fee: float
    slippage_bps: float
    spread_bps: float
    timestamp_ms: int
    reason: str
    realized_pnl: float
    position_side: PositionSide | None = None
    role: ExecutionRole | None = None

    def __post_init__(self) -> None:
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"unsupported execution side: {self.side!r}")
        if self.position_side is not None and self.position_side not in {"LONG", "SHORT"}:
            raise ValueError(f"unsupported position side: {self.position_side!r}")
        if self.role is not None and self.role not in {"ENTRY", "EXIT"}:
            raise ValueError(f"unsupported execution role: {self.role!r}")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp_ms: int
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float


@dataclass(frozen=True, slots=True)
class PaperSnapshot:
    account_id: str
    quote_currency: str
    initial_capital: float
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    positions: tuple[PaperPosition, ...]
    prices: dict[str, float]


class PaperTradeRejected(RuntimeError):
    """A proposed paper action failed its validation or risk approval."""


def _event_timestamp_ms(value) -> int | None:
    if isinstance(value,(int,float)):
        number=float(value)
        if not math.isfinite(number): return None
        return int(number*1000 if abs(number)<100_000_000_000 else number)
    if isinstance(value,str):
        try: return _event_timestamp_ms(float(value))
        except ValueError:
            try: stamp=datetime.fromisoformat(value.replace("Z","+00:00"))
            except ValueError: return None
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
            return int(stamp.astimezone(timezone.utc).timestamp()*1000)
    return None


def _register_event_timestamp_function(db: sqlite3.Connection) -> None:
    db.create_function("event_timestamp_ms",1,_event_timestamp_ms,deterministic=True)


class PaperTradingEngine:
    """SQLite-backed paper account. It has no exchange order submission path."""

    def __init__(self, db_path: str, account_id: str, initial_capital: float,
                 quote_currency: str = "USDT", fee_bps: float = 10.0,
                 slippage_bps: float = 5.0, spread_bps: float = 0.0,
                 initial_timestamp_ms: int | None = None,
                 short_term_exit_minutes: int | None = None):
        if not account_id or not quote_currency or not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("account id, quote currency, and positive initial capital are required")
        if any(not math.isfinite(x) or x < 0 for x in (fee_bps, slippage_bps, spread_bps)):
            raise ValueError("paper execution costs must be finite and nonnegative")
        self.db_path, self.account_id = db_path, account_id
        self._memory_uri = None
        self._anchor = None
        if db_path == ":memory:":
            self._memory_uri = f"file:paper-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor = sqlite3.connect(self._memory_uri, uri=True, timeout=20)
        self.initial_capital, self.quote_currency = initial_capital, quote_currency.upper()
        self.fee_bps, self.slippage_bps, self.spread_bps = fee_bps, slippage_bps, spread_bps
        self.short_term_exit_ms = None if short_term_exit_minutes is None else max(0, int(short_term_exit_minutes)) * 60_000
        if initial_timestamp_ms is not None and (not isinstance(initial_timestamp_ms,int) or initial_timestamp_ms < 0):
            raise ValueError("initial timestamp must be a nonnegative UTC millisecond value")
        self.initial_timestamp_ms=initial_timestamp_ms
        self._lock = RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self._memory_uri or self.db_path,
                             uri=self._memory_uri is not None, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_accounts (
              account_id TEXT PRIMARY KEY, quote_currency TEXT NOT NULL,
              initial_capital REAL NOT NULL, cash REAL NOT NULL,
              realized_pnl REAL NOT NULL DEFAULT 0, fees_paid REAL NOT NULL DEFAULT 0,
              created_at_ms INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_positions (
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(account_id,symbol));
            CREATE TABLE IF NOT EXISTS paper_events (
              id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL,
              symbol TEXT, timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_fills (
              id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
              fee REAL NOT NULL, slippage_bps REAL NOT NULL, timestamp_ms INTEGER NOT NULL,
              reason TEXT NOT NULL, realized_pnl REAL NOT NULL, payload TEXT NOT NULL,
              spread_bps REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS paper_equity_curve (
              id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
              cash REAL NOT NULL, equity REAL NOT NULL, realized_pnl REAL NOT NULL,
              unrealized_pnl REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_roundtrips (
              id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
              strategy TEXT NOT NULL, regime TEXT NOT NULL, quantity REAL NOT NULL,
              opened_at_ms INTEGER NOT NULL, closed_at_ms INTEGER NOT NULL,
              net_pnl REAL NOT NULL, fees REAL NOT NULL, slippage_cost REAL NOT NULL,
              reason TEXT NOT NULL, position_side TEXT NOT NULL DEFAULT 'LONG');
            CREATE TABLE IF NOT EXISTS paper_processed_bars (
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
              PRIMARY KEY(account_id,symbol,timestamp_ms));
            CREATE TABLE IF NOT EXISTS paper_processed_cycles (
              account_id TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
              candle_timestamp_ms INTEGER NOT NULL, processed_at_ms INTEGER NOT NULL,
              PRIMARY KEY(account_id,symbol,timeframe,candle_timestamp_ms));
            CREATE INDEX IF NOT EXISTS ix_paper_events_account_time ON paper_events(account_id,timestamp_ms);
            CREATE INDEX IF NOT EXISTS ix_paper_fills_account_time ON paper_fills(account_id,timestamp_ms);
            """)
            fill_columns={r[1] for r in db.execute("PRAGMA table_info(paper_fills)")}
            if "spread_bps" not in fill_columns:
                db.execute("ALTER TABLE paper_fills ADD COLUMN spread_bps REAL NOT NULL DEFAULT 0")
            trade_columns={r[1] for r in db.execute("PRAGMA table_info(paper_roundtrips)")}
            if "spread_cost" not in trade_columns:
                db.execute("ALTER TABLE paper_roundtrips ADD COLUMN spread_cost REAL NOT NULL DEFAULT 0")
            if "position_side" not in trade_columns:
                # Old paper roundtrips were all long positions; preserve them as such.
                db.execute("ALTER TABLE paper_roundtrips ADD COLUMN position_side TEXT NOT NULL DEFAULT 'LONG'")
            row = db.execute("SELECT * FROM paper_accounts WHERE account_id=?", (self.account_id,)).fetchone()
            if row is None:
                now = (self.initial_timestamp_ms if self.initial_timestamp_ms is not None else
                       int(datetime.now(timezone.utc).timestamp() * 1000))
                db.execute("INSERT INTO paper_accounts VALUES (?,?,?,?,?,?,?)",
                    (self.account_id, self.quote_currency, self.initial_capital,
                     self.initial_capital, 0.0, 0.0, now))
                db.execute("INSERT INTO paper_equity_curve(account_id,timestamp_ms,cash,equity,realized_pnl,unrealized_pnl) VALUES (?,?,?,?,?,?)",
                           (self.account_id,now,self.initial_capital,self.initial_capital,0.0,0.0))
            elif row["quote_currency"] != self.quote_currency or abs(row["initial_capital"] - self.initial_capital) > 1e-8:
                raise ValueError("existing account id has different quote currency or initial capital")

    @staticmethod
    def _position(payload: str) -> PaperPosition:
        data = json.loads(payload)
        data["targets"] = tuple(data["targets"])
        return PaperPosition(**data)

    @staticmethod
    def _position_payload(position: PaperPosition) -> str:
        """Serialize all position identity fields; old payloads default to LONG on read."""
        return json.dumps(asdict(position), separators=(",", ":"))

    def _positions(self, db) -> tuple[PaperPosition, ...]:
        rows = db.execute("SELECT payload FROM paper_positions WHERE account_id=? ORDER BY symbol", (self.account_id,)).fetchall()
        return tuple(self._position(row[0]) for row in rows)

    def _account(self, db):
        row = db.execute("SELECT * FROM paper_accounts WHERE account_id=?", (self.account_id,)).fetchone()
        if row is None:
            raise RuntimeError("paper account disappeared")
        return row

    def snapshot(self, prices: dict[str, float] | None = None) -> PaperSnapshot:
        prices = dict(prices or {})
        with self._lock, self._connect() as db:
            account = self._account(db)
            positions = self._positions(db)
            unrealized = 0.0
            equity = account["cash"]
            for pos in positions:
                mark = prices.get(pos.symbol, pos.average_entry)
                if not math.isfinite(mark) or mark <= 0:
                    raise ValueError(f"invalid mark price for {pos.symbol}")
                prices[pos.symbol] = mark
                if pos.position_side == "LONG":
                    position_value = pos.quantity * mark
                    position_pnl = pos.quantity * (mark - pos.average_entry)
                else:
                    # Short sale proceeds are already included in cash. The open
                    # liability is the current cost to buy back the borrowed units.
                    position_value = -pos.quantity * mark
                    position_pnl = pos.quantity * (pos.average_entry - mark)
                equity += position_value
                unrealized += position_pnl
            return PaperSnapshot(self.account_id, self.quote_currency,
                account["initial_capital"], account["cash"], equity,
                account["realized_pnl"], unrealized, account["fees_paid"], positions, prices)

    def portfolio_for_risk(self, prices: dict[str, float] | None = None,
                           timestamp_ms: int | None = None) -> PortfolioSnapshot:
        snap = self.snapshot(prices)
        marked = tuple(OpenPosition(p.symbol, p.quantity, snap.prices[p.symbol]) for p in snap.positions)
        with self._connect() as db:
            account = self._account(db)
            today = datetime.fromtimestamp((timestamp_ms or int(datetime.now(timezone.utc).timestamp()*1000))/1000, timezone.utc).date().isoformat()
            events = db.execute("SELECT COUNT(*) FROM paper_events WHERE account_id=? AND kind='entry' AND date(timestamp_ms/1000,'unixepoch')=?",
                                (self.account_id, today)).fetchone()[0]
            # Entry fills have zero realized PnL. Summing all fills includes
            # long exits (sell) and short covers (buy) without counting short entries.
            daily_realized = db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM paper_fills WHERE account_id=? AND date(timestamp_ms/1000,'unixepoch')=?",
                                        (self.account_id,today)).fetchone()[0]
            peak = db.execute("SELECT MAX(equity) FROM paper_equity_curve WHERE account_id=?", (self.account_id,)).fetchone()[0]
        return PortfolioSnapshot(snap.equity, snap.quote_currency, marked,
            daily_realized, snap.unrealized_pnl, peak or snap.equity, int(events))

    def record_signal(self, signal: StrategySignal) -> int:
        """Persist a signal as a signal event; this does not execute a fill."""
        timestamp = int(signal.timestamp.timestamp() * 1000)
        payload = json.dumps(asdict(signal), default=lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x), separators=(",", ":"))
        with self._lock, self._connect() as db:
            cur = db.execute("INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                             (self.account_id, "signal", signal.symbol, timestamp, payload))
            return int(cur.lastrowid)

    def record_wait_reason(self, symbol: str, timestamp_ms: int, reason: str,
                           timeframe: str | None = None,
                           candle_timestamp_ms: int | None = None) -> int:
        """Persist a real paper-analysis wait reason in the paper journal."""
        if (not symbol or not reason or not isinstance(timestamp_ms, int) or timestamp_ms < 0
                or (candle_timestamp_ms is not None and
                    (not isinstance(candle_timestamp_ms, int) or candle_timestamp_ms < 0))):
            raise ValueError("paper wait event requires a symbol, reason, and valid timestamps")
        payload = {"reason": reason}
        if timeframe:
            payload["timeframe"] = timeframe
        if candle_timestamp_ms is not None:
            payload["candle_timestamp_ms"] = candle_timestamp_ms
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                (self.account_id, "wait", symbol, timestamp_ms,
                 json.dumps(payload, separators=(",", ":"))))
            return int(cursor.lastrowid)

    def market_event_processed(self, symbol: str, timeframe: str,
                               candle_timestamp_ms: int) -> bool:
        """Return whether this closed-candle analysis ran for this account."""
        with self._connect() as db:
            row = db.execute("""SELECT 1 FROM paper_processed_cycles
                WHERE account_id=? AND symbol=? AND timeframe=? AND candle_timestamp_ms=?""",
                (self.account_id, symbol, timeframe, candle_timestamp_ms)).fetchone()
        return row is not None

    def mark_market_event_processed(self, symbol: str, timeframe: str,
                                    candle_timestamp_ms: int,
                                    processed_at_ms: int) -> bool:
        """Persist an idempotency key for one market candle after a valid analysis."""
        if min(candle_timestamp_ms, processed_at_ms) < 0:
            raise ValueError("market event timestamps must be nonnegative")
        with self._lock, self._connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO paper_processed_cycles
                (account_id,symbol,timeframe,candle_timestamp_ms,processed_at_ms)
                VALUES (?,?,?,?,?)""",
                (self.account_id, symbol, timeframe, candle_timestamp_ms, processed_at_ms))
            return cursor.rowcount == 1

    def record_risk_decision(self, decision, timestamp_ms: int) -> int:
        """Audit risk approvals/rejections alongside paper execution history."""
        if not isinstance(timestamp_ms,int) or timestamp_ms<0:
            raise ValueError("risk decision timestamp must be nonnegative UTC milliseconds")
        payload=json.dumps(asdict(decision),separators=(",",":"),default=str)
        with self._lock,self._connect() as db:
            cursor=db.execute("INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                (self.account_id,"risk_decision",decision.symbol,timestamp_ms,payload))
            return int(cursor.lastrowid)

    def record_entry_rejection(self, signal: StrategySignal, timestamp_ms: int,
                               stage: str, reason: str) -> int:
        """Persist why an actionable candidate did not become a paper fill."""
        if not isinstance(timestamp_ms, int) or timestamp_ms < 0 or not stage or not reason:
            raise ValueError("entry rejection timestamp, stage, and reason are required")
        payload = {"strategy": signal.strategy, "candidate_direction": signal.position_side,
                   "stage": stage, "reason": reason}
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                (self.account_id, "entry_rejected", signal.symbol, timestamp_ms,
                 json.dumps(payload, separators=(",", ":"))))
            return int(cursor.lastrowid)

    def execute_entry(self, signal: StrategySignal, risk_manager: RiskManager,
                      risk_request: RiskRequest, execution_bar: Candle,
                      amount_step: float, prices: dict[str, float] | None = None,
                      regime: str = "unknown") -> PaperFill:
        """Fill an actionable LONG or SHORT signal at the next bar's open."""
        if signal.direction not in {"LONG", "SHORT"} or signal.status != "conditions_met":
            raise PaperTradeRejected("only actionable LONG/SHORT signals can open paper positions")
        if not math.isfinite(amount_step) or amount_step <= 0 or abs(amount_step-risk_request.instrument.amount_step) > 1e-15:
            raise PaperTradeRejected("fill precision must match the instrument amount step")
        signal_time = int(signal.timestamp.timestamp() * 1000)
        entry_timeframe = signal.timeframes[-1] if signal.timeframes else None
        try:
            next_open_ms = signal_time + timeframe_to_ms(entry_timeframe) if entry_timeframe else None
        except ValueError as exc:
            raise PaperTradeRejected("signal is missing a supported entry timeframe") from exc
        if execution_bar.timestamp_ms != next_open_ms:
            raise PaperTradeRejected("entry must use exactly the next candle open; delayed or same-bar fills are prohibited")
        values=(execution_bar.open,execution_bar.high,execution_bar.low,execution_bar.close,execution_bar.volume)
        if (execution_bar.source != signal.source or any(not math.isfinite(x) for x in values) or
                execution_bar.open <= 0 or execution_bar.volume < 0 or
                execution_bar.high < max(execution_bar.open,execution_bar.close,execution_bar.low) or
                execution_bar.low > min(execution_bar.open,execution_bar.close,execution_bar.high)):
            raise PaperTradeRejected("execution candle provenance or OHLCV is invalid")
        if (signal.entry_zone is None or len(signal.entry_zone) != 2 or
                any(not math.isfinite(x) for x in signal.entry_zone) or
                not signal.entry_zone[0] <= execution_bar.open <= signal.entry_zone[1]):
            raise PaperTradeRejected("next candle open is outside the proposed entry zone")
        side = signal.direction
        is_long = side == "LONG"
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM paper_positions WHERE account_id=? AND symbol=?",
                          (self.account_id, signal.symbol)).fetchone():
                raise PaperTradeRejected("symbol already has an open paper position")
            risk_portfolio = self.portfolio_for_risk(prices, execution_bar.timestamp_ms)
            spread_cost = self.spread_bps
            slip_bps = self.slippage_bps + spread_cost / 2
            entry_slippage = slip_bps / 10_000
            if is_long:
                fill_price = execution_bar.open * (1 + entry_slippage)
                fill_side = "buy"
                cash_delta = -1.0
            else:
                fill_price = execution_bar.open * (1 - entry_slippage)
                fill_side = "sell"
                cash_delta = 1.0
            fee = fill_price * self.fee_bps / 10_000
            transaction_cost_bps = 2*self.fee_bps + 2*self.slippage_bps + spread_cost
            stop = signal.stop_loss
            target = signal.targets[0] if signal.targets else None
            if is_long:
                net_rr = ((target-fill_price-fill_price*transaction_cost_bps/10_000) /
                          (fill_price-stop+fill_price*transaction_cost_bps/10_000)) if target and fill_price > stop else 0
            else:
                net_rr = ((fill_price-target-fill_price*transaction_cost_bps/10_000) /
                          (stop-fill_price+fill_price*transaction_cost_bps/10_000)) if target and fill_price < stop else 0
            adjusted = replace(signal, entry_zone=(fill_price, fill_price), risk_reward=net_rr,
                               estimated_cost_bps=transaction_cost_bps,
                               estimated_slippage_bps=slip_bps)
            request = replace(risk_request, signal=adjusted, portfolio=risk_portfolio,
                              fee_bps_round_trip=2*self.fee_bps,
                              slippage_bps_entry=self.slippage_bps+spread_cost/2,
                              slippage_bps_stop=self.slippage_bps+spread_cost/2,
                              data_age_ms=risk_request.data_age_ms)
            decision = risk_manager.evaluate(request)
            if not decision.accepted:
                raise PaperTradeRejected("risk gate rejected paper entry: " + "; ".join(decision.reasons))
            amount = decision.quantity
            account = self._account(db)
            if is_long:
                affordable = account["cash"] / (fill_price + fee)
                max_amount = min(amount, affordable)
                cash_after = account["cash"] - (max_amount * fill_price + max_amount * fee)
                if cash_after < -1e-8:
                    raise PaperTradeRejected("paper cash balance would become negative")
            else:
                max_amount = amount
                cash_after = account["cash"] + (max_amount * fill_price - max_amount * fee)
            amount = math.floor(max_amount / amount_step + 1e-12) * amount_step
            if amount <= 0:
                raise PaperTradeRejected("insufficient paper cash after fees")
            fee_paid = amount * fee
            gross = amount * fill_price
            if is_long:
                if gross + fee_paid > account["cash"] + 1e-8:
                    raise PaperTradeRejected("paper cash balance would become negative")
                db.execute("UPDATE paper_accounts SET cash=cash-?,fees_paid=fees_paid+? WHERE account_id=?",
                           (gross+fee_paid, fee_paid, self.account_id))
                cash_event = "debit"
            else:
                if account["cash"] + gross - fee_paid < -1e-8:
                    raise PaperTradeRejected("paper cash balance would become negative")
                db.execute("UPDATE paper_accounts SET cash=cash+?,fees_paid=fees_paid+? WHERE account_id=?",
                           (gross-fee_paid, fee_paid, self.account_id))
                cash_event = "credit"
            pos = PaperPosition(signal.symbol, amount, fill_price, signal.stop_loss,
                tuple(signal.targets), 0, fee_paid, execution_bar.timestamp_ms,
                signal.source, signal.timeframes[-1] if signal.timeframes else "unknown", signal.strategy,
                regime, position_side=side)
            db.execute("INSERT INTO paper_positions VALUES (?,?,?)",
                       (self.account_id, signal.symbol, self._position_payload(pos)))
            event = {"signal_timestamp_ms": signal_time, "execution_open": execution_bar.open,
                     "fill_price": fill_price, "quantity": amount, "risk_decision": asdict(decision),
                     "position_side": side, "execution_role": "ENTRY", "cash_event": cash_event}
            db.execute("INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                       (self.account_id,"entry",signal.symbol,execution_bar.timestamp_ms,
                        json.dumps(event, default=lambda obj: obj.__dict__ if hasattr(obj,"__dict__") else str(obj))))
            fill_id = db.execute("INSERT INTO paper_fills(account_id,symbol,side,quantity,price,fee,slippage_bps,timestamp_ms,reason,realized_pnl,payload,spread_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.account_id,signal.symbol,fill_side,amount,fill_price,fee_paid,self.slippage_bps,
                 execution_bar.timestamp_ms,"signal_entry",0,json.dumps(event,default=str),spread_cost)).lastrowid
            return PaperFill(fill_id, signal.symbol, fill_side, amount, fill_price, fee_paid,
                             self.slippage_bps, spread_cost, execution_bar.timestamp_ms,
                             "signal_entry", 0.0, side, "ENTRY")

    def close_position(self, symbol: str, quantity: float, reference_price: float,
                       timestamp_ms: int, reason: str, amount_step: float = 1e-8,
                       slippage_bps: float | None = None, spread_bps: float | None = None) -> PaperFill:
        if (not all(math.isfinite(x) for x in (quantity, reference_price, amount_step)) or
                min(quantity, reference_price, amount_step) <= 0 or not isinstance(timestamp_ms,int) or timestamp_ms < 0):
            raise ValueError("close quantity, price, and amount step must be positive and finite")
        slip = self.slippage_bps if slippage_bps is None else slippage_bps
        spread = self.spread_bps if spread_bps is None else spread_bps
        if any(not math.isfinite(x) or x < 0 for x in (slip,spread)):
            raise ValueError("execution costs must be nonnegative")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM paper_positions WHERE account_id=? AND symbol=?",
                             (self.account_id,symbol)).fetchone()
            if row is None:
                raise PaperTradeRejected(f"no open paper position for {symbol}")
            pos = self._position(row[0])
            if timestamp_ms <= pos.opened_at_ms:
                raise PaperTradeRejected("close fill timestamp must be after the paper entry")
            amount = min(pos.quantity, math.floor(quantity/amount_step+1e-12)*amount_step)
            if amount <= 0:
                raise ValueError("close quantity rounds to zero")
            fill_side = "sell" if pos.position_side == "LONG" else "buy"
            adverse_slippage = slip + spread/2
            if pos.position_side == "LONG":
                fill_price = reference_price * (1-adverse_slippage/10_000)
                fee = amount * fill_price * self.fee_bps/10_000
                allocated_entry_fee = pos.entry_fees * (amount/pos.quantity)
                realized = amount*(fill_price-pos.average_entry)-allocated_entry_fee-fee
                db.execute("UPDATE paper_accounts SET cash=cash+?,realized_pnl=realized_pnl+?,fees_paid=fees_paid+? WHERE account_id=?",
                           (amount*fill_price-fee, realized, fee, self.account_id))
            else:
                fill_price = reference_price * (1+adverse_slippage/10_000)
                fee = amount * fill_price * self.fee_bps/10_000
                allocated_entry_fee = pos.entry_fees * (amount/pos.quantity)
                realized = amount*(pos.average_entry-fill_price)-allocated_entry_fee-fee
                db.execute("UPDATE paper_accounts SET cash=cash-?,realized_pnl=realized_pnl+?,fees_paid=fees_paid+? WHERE account_id=?",
                           (amount*fill_price+fee, realized, fee, self.account_id))
            remaining = pos.quantity-amount
            if remaining <= max(1e-12, amount_step*1e-8):
                db.execute("DELETE FROM paper_positions WHERE account_id=? AND symbol=?", (self.account_id,symbol))
            else:
                updated = replace(pos, quantity=remaining, entry_fees=pos.entry_fees-allocated_entry_fee)
                db.execute("UPDATE paper_positions SET payload=? WHERE account_id=? AND symbol=?",
                           (self._position_payload(updated), self.account_id, symbol))
            payload = {"reason":reason,"reference_price":reference_price,"fill_price":fill_price,
                       "allocated_entry_fee":allocated_entry_fee,
                       "position_side":pos.position_side,"execution_role":"EXIT"}
            cur = db.execute("INSERT INTO paper_fills(account_id,symbol,side,quantity,price,fee,slippage_bps,timestamp_ms,reason,realized_pnl,payload,spread_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.account_id,symbol,fill_side,amount,fill_price,fee,slip,timestamp_ms,reason,realized,json.dumps(payload),spread))
            if remaining <= max(1e-12, amount_step*1e-8):
                trade_fills = db.execute("SELECT side,quantity,price,fee,slippage_bps,spread_bps,realized_pnl FROM paper_fills WHERE account_id=? AND symbol=? AND timestamp_ms>=? ORDER BY id",
                    (self.account_id,symbol,pos.opened_at_ms)).fetchall()
                total_fees = sum(r["fee"] for r in trade_fills)
                slip_cost = sum(r["quantity"]*r["price"]*r["slippage_bps"]/10_000 for r in trade_fills)
                spread_cost = sum(r["quantity"]*r["price"]*r["spread_bps"]/20_000 for r in trade_fills)
                opening_side = "buy" if pos.position_side == "LONG" else "sell"
                total_quantity = sum(r["quantity"] for r in trade_fills if r["side"] == opening_side)
                total_pnl = sum(r["realized_pnl"] for r in trade_fills)
                db.execute("INSERT INTO paper_roundtrips(account_id,symbol,strategy,regime,quantity,opened_at_ms,closed_at_ms,net_pnl,fees,slippage_cost,reason,spread_cost,position_side) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.account_id,symbol,pos.strategy,pos.regime,total_quantity,pos.opened_at_ms,
                     timestamp_ms,total_pnl,total_fees,slip_cost,reason,spread_cost,pos.position_side))
            db.execute("INSERT INTO paper_events(account_id,kind,symbol,timestamp_ms,payload) VALUES (?,?,?,?,?)",
                       (self.account_id,"exit",symbol,timestamp_ms,json.dumps(payload)))
            return PaperFill(cur.lastrowid,symbol,fill_side,amount,fill_price,fee,slip,spread,
                             timestamp_ms,reason,realized,pos.position_side,"EXIT")

    def execute_signal_exit(self, signal: StrategySignal, execution_bar: Candle,
                            amount_step: float = 1e-8) -> PaperFill:
        position = self.position(signal.symbol)
        if signal.direction != "EXIT" or position is None:
            raise PaperTradeRejected("EXIT must reference an explicit signal and an open position")
        try:
            expected= int(signal.timestamp.timestamp()*1000)+timeframe_to_ms(position.timeframe)
        except ValueError as exc:
            raise PaperTradeRejected("position entry timeframe is unsupported") from exc
        if execution_bar.timestamp_ms != expected or execution_bar.source != signal.source:
            raise PaperTradeRejected("EXIT must execute at the next bar open from the signal source")
        return self.close_position(signal.symbol, position.quantity, execution_bar.open,
                                   execution_bar.timestamp_ms, "strategy_exit", amount_step)

    def process_bar(self, symbol: str, candle: Candle, amount_step: float = 1e-8) -> tuple[PaperFill, ...]:
        """Apply stop before targets if both lie in one OHLC bar; stop wins conservatively."""
        return self.process_symbol_bar(symbol, candle, amount_step)

    def process_symbol_bar(self, symbol: str, candle: Candle,
                           amount_step: float = 1e-8) -> tuple[PaperFill, ...]:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM paper_positions WHERE account_id=? AND symbol=?",
                             (self.account_id,symbol)).fetchone()
        if row is None:
            return ()
        pos = self._position(row[0])
        if not candle.complete:
            return ()
        values=(candle.open,candle.high,candle.low,candle.close,candle.volume)
        if (candle.source != pos.source or any(not math.isfinite(x) for x in values) or
                min(candle.open,candle.high,candle.low,candle.close) <= 0 or candle.volume < 0 or
                candle.high < max(candle.open,candle.close,candle.low) or
                candle.low > min(candle.open,candle.close,candle.high)):
            raise PaperTradeRejected("position-management candle has invalid OHLCV or mismatched source")
        with self._connect() as db:
            latest=db.execute("SELECT MAX(timestamp_ms) FROM paper_processed_bars WHERE account_id=? AND symbol=?",
                              (self.account_id,symbol)).fetchone()[0]
            if latest is not None and candle.timestamp_ms <= latest:
                return ()
            cursor=db.execute("INSERT OR IGNORE INTO paper_processed_bars VALUES (?,?,?)",
                              (self.account_id,symbol,candle.timestamp_ms))
            if cursor.rowcount == 0:
                return ()
        if self.short_term_exit_ms is not None and candle.timestamp_ms - pos.opened_at_ms >= self.short_term_exit_ms:
            reference = candle.open if math.isfinite(candle.open) else pos.average_entry
            fill = self.close_position(symbol, pos.quantity, reference, candle.timestamp_ms + 1,
                f"scalp_exit_{int(self.short_term_exit_ms / 60_000)}m", amount_step)
            return (fill,)
        if pos.position_side == "LONG":
            if candle.low <= pos.stop_loss:
                reference = min(candle.open, pos.stop_loss)
                fill = self.close_position(symbol,pos.quantity,reference,candle.timestamp_ms+
                    1,"stop_loss",amount_step)
                return (fill,)
            if pos.next_target < len(pos.targets):
                target = pos.targets[pos.next_target]
                if candle.high >= target:
                    reference = max(candle.open,target)
                    quantity = pos.quantity if pos.next_target >= len(pos.targets)-1 else pos.quantity/2
                    fill = self.close_position(symbol,quantity,reference,candle.timestamp_ms+1,
                        f"take_profit_{pos.next_target+1}",amount_step)
                    remaining = self.position(symbol)
                    if remaining and fill.quantity < pos.quantity:
                        self._set_next_target(symbol,pos.next_target+1)
                    return (fill,)
            return ()
        if candle.high >= pos.stop_loss:
            reference = max(candle.open, pos.stop_loss)
            fill = self.close_position(symbol,pos.quantity,reference,candle.timestamp_ms+
                1,"stop_loss",amount_step)
            return (fill,)
        if pos.next_target < len(pos.targets):
            target = pos.targets[pos.next_target]
            if candle.low <= target:
                reference = min(candle.open,target)
                quantity = pos.quantity if pos.next_target >= len(pos.targets)-1 else pos.quantity/2
                fill = self.close_position(symbol,quantity,reference,candle.timestamp_ms+1,
                    f"take_profit_{pos.next_target+1}",amount_step)
                remaining = self.position(symbol)
                if remaining and fill.quantity < pos.quantity:
                    self._set_next_target(symbol,pos.next_target+1)
                return (fill,)
        return ()

    def _set_next_target(self, symbol: str, next_target: int) -> None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM paper_positions WHERE account_id=? AND symbol=?",
                             (self.account_id,symbol)).fetchone()
            if row is None: return
            pos = replace(self._position(row[0]), next_target=next_target)
            db.execute("UPDATE paper_positions SET payload=? WHERE account_id=? AND symbol=?",
                (self._position_payload(pos), self.account_id, symbol))

    def position(self, symbol: str) -> PaperPosition | None:
        with self._connect() as db:
            row=db.execute("SELECT payload FROM paper_positions WHERE account_id=? AND symbol=?",
                           (self.account_id,symbol)).fetchone()
        return self._position(row[0]) if row else None

    def fills(self) -> tuple[PaperFill, ...]:
        with self._connect() as db:
            rows=db.execute("SELECT * FROM paper_fills WHERE account_id=? ORDER BY timestamp_ms,id",
                            (self.account_id,)).fetchall()
        def restored(row):
            payload = json.loads(row["payload"] or "{}")
            side = payload.get("position_side", "LONG")
            role = payload.get("execution_role")
            if role is None:
                role = "ENTRY" if row["reason"] == "signal_entry" else "EXIT"
            return PaperFill(row["id"],row["symbol"],row["side"],row["quantity"],row["price"],
                row["fee"],row["slippage_bps"],row["spread_bps"],row["timestamp_ms"],
                row["reason"],row["realized_pnl"],side,role)
        return tuple(restored(row) for row in rows)

    def roundtrips(self) -> tuple[dict, ...]:
        with self._connect() as db:
            rows=db.execute("SELECT symbol,strategy,regime,quantity,opened_at_ms,closed_at_ms,net_pnl,fees,slippage_cost,spread_cost,reason,position_side FROM paper_roundtrips WHERE account_id=? ORDER BY closed_at_ms,id",
                            (self.account_id,)).fetchall()
        return tuple(dict(row) for row in rows)

    def events(self) -> tuple[dict, ...]:
        with self._connect() as db:
            _register_event_timestamp_function(db)
            rows=db.execute("SELECT id,kind,symbol,timestamp_ms,payload FROM paper_events WHERE account_id=? "
                            "ORDER BY event_timestamp_ms(timestamp_ms),id",
                            (self.account_id,)).fetchall()
        return tuple({**dict(row),"timestamp_ms":_event_timestamp_ms(row["timestamp_ms"]),
                      "payload":json.loads(row["payload"])} for row in rows)

    def recent_events_since(self, min_timestamp_ms: int, limit: int = 100) -> tuple[dict, ...]:
        """Return the newest event-time window in display chronology, without deleting history."""
        if min_timestamp_ms < 0 or limit < 1:
            raise ValueError("event cutoff must be nonnegative and limit positive")
        with self._connect() as db:
            _register_event_timestamp_function(db)
            rows=db.execute("SELECT id,kind,symbol,timestamp_ms,payload FROM paper_events "
                "WHERE account_id=? AND event_timestamp_ms(timestamp_ms)>=? "
                "ORDER BY event_timestamp_ms(timestamp_ms) DESC,id DESC LIMIT ?",
                (self.account_id,min_timestamp_ms,limit)).fetchall()
        return tuple({**dict(row),"timestamp_ms":_event_timestamp_ms(row["timestamp_ms"]),
                      "payload":json.loads(row["payload"])} for row in reversed(rows))

    def recent_events_page(self, min_timestamp_ms: int, limit: int = 100) -> tuple[tuple[dict, ...], int]:
        """Read initial recent rows and their ID high-water mark from one SQLite snapshot."""
        if min_timestamp_ms < 0 or limit < 1:
            raise ValueError("event cutoff must be nonnegative and limit positive")
        with self._connect() as db:
            db.execute("BEGIN")
            _register_event_timestamp_function(db)
            latest=db.execute("SELECT COALESCE(MAX(id),0) FROM paper_events WHERE account_id=?",
                              (self.account_id,)).fetchone()
            rows=db.execute("SELECT id,kind,symbol,timestamp_ms,payload FROM paper_events "
                "WHERE account_id=? AND event_timestamp_ms(timestamp_ms)>=? "
                "ORDER BY event_timestamp_ms(timestamp_ms) DESC,id DESC LIMIT ?",
                (self.account_id,min_timestamp_ms,limit)).fetchall()
        events=tuple({**dict(row),"timestamp_ms":_event_timestamp_ms(row["timestamp_ms"]),
                      "payload":json.loads(row["payload"])} for row in reversed(rows))
        return events,int(latest[0] or 0)

    def latest_event_since(self, min_timestamp_ms: int) -> dict | None:
        with self._connect() as db:
            _register_event_timestamp_function(db)
            row=db.execute("SELECT id,kind,symbol,timestamp_ms FROM paper_events "
                "WHERE account_id=? AND event_timestamp_ms(timestamp_ms)>=? "
                "ORDER BY event_timestamp_ms(timestamp_ms) DESC,id DESC LIMIT 1",
                (self.account_id,min_timestamp_ms)).fetchone()
        return {**dict(row),"timestamp_ms":_event_timestamp_ms(row["timestamp_ms"])} if row else None

    def events_page_after(self, event_id: int, limit: int, min_timestamp_ms: int) -> tuple[tuple[dict, ...], int]:
        """Scan an insertion-ID page, return visible recent rows sorted by event time, and advance safely.

        The cursor advances across expired rows too, so a backlog of old events cannot starve
        current events. All rows remain persisted in SQLite.
        """
        if event_id < 0 or limit < 1 or min_timestamp_ms < 0:
            raise ValueError("event cursor/cutoff must be nonnegative and limit positive")
        with self._connect() as db:
            rows=db.execute("SELECT id,kind,symbol,timestamp_ms,payload FROM paper_events "
                "WHERE account_id=? AND id>? ORDER BY id LIMIT ?",
                (self.account_id,event_id,limit)).fetchall()
        scanned=[{**dict(row),"timestamp_ms":_event_timestamp_ms(row["timestamp_ms"]),
                  "payload":json.loads(row["payload"])} for row in rows]
        visible=[event for event in scanned if event["timestamp_ms"] is not None and
                 event["timestamp_ms"]>=min_timestamp_ms]
        visible.sort(key=lambda event:(event["timestamp_ms"],event["id"]))
        next_id=int(rows[-1]["id"]) if rows else event_id
        return tuple(visible),next_id

    def events_after(self, event_id: int, limit: int = 100) -> tuple[dict, ...]:
        """Return newly persisted account events in stable insertion order."""
        if event_id < 0 or limit < 1:
            raise ValueError("event cursor must be nonnegative and limit positive")
        with self._connect() as db:
            rows=db.execute("SELECT id,kind,symbol,timestamp_ms,payload FROM paper_events "
                            "WHERE account_id=? AND id>? ORDER BY id LIMIT ?",
                            (self.account_id,event_id,limit)).fetchall()
        return tuple({**dict(row),"payload":json.loads(row["payload"])} for row in rows)

    def latest_event_id(self) -> int:
        with self._connect() as db:
            row=db.execute("SELECT MAX(id) FROM paper_events WHERE account_id=?",
                           (self.account_id,)).fetchone()
        return int(row[0] or 0)

    def latest_event(self) -> dict | None:
        with self._connect() as db:
            row=db.execute("SELECT id,kind,symbol,timestamp_ms FROM paper_events "
                            "WHERE account_id=? ORDER BY id DESC LIMIT 1",
                            (self.account_id,)).fetchone()
        return dict(row) if row else None

    def equity_curve(self) -> tuple[EquityPoint, ...]:
        with self._connect() as db:
            rows=db.execute("SELECT timestamp_ms,cash,equity,realized_pnl,unrealized_pnl FROM paper_equity_curve WHERE account_id=? ORDER BY timestamp_ms,id",
                            (self.account_id,)).fetchall()
        return tuple(EquityPoint(*tuple(r)) for r in rows)

    def mark_to_market(self, prices: dict[str, float], timestamp_ms: int) -> PaperSnapshot:
        snap=self.snapshot(prices)
        point=EquityPoint(timestamp_ms,snap.cash,snap.equity,snap.realized_pnl,snap.unrealized_pnl)
        with self._connect() as db:
            db.execute("INSERT INTO paper_equity_curve(account_id,timestamp_ms,cash,equity,realized_pnl,unrealized_pnl) VALUES (?,?,?,?,?,?)",
                       (self.account_id,timestamp_ms,point.cash,point.equity,point.realized_pnl,point.unrealized_pnl))
        return snap

    def reset(self, initial_capital: float | None = None,
              quote_currency: str | None = None) -> None:
        """Reset this simulated account, optionally changing its initial balance/currency."""
        new_capital = self.initial_capital if initial_capital is None else initial_capital
        new_currency = self.quote_currency if quote_currency is None else quote_currency.upper()
        if not math.isfinite(new_capital) or new_capital <= 0 or not new_currency:
            raise ValueError("reset capital and quote currency must be valid")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db)
            for table in ("paper_positions", "paper_events", "paper_fills",
                          "paper_equity_curve", "paper_roundtrips", "paper_processed_bars",
                          "paper_processed_cycles"):
                db.execute(f"DELETE FROM {table} WHERE account_id=?", (self.account_id,))
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            db.execute("UPDATE paper_accounts SET quote_currency=?,initial_capital=?,cash=?,realized_pnl=0,fees_paid=0,created_at_ms=? WHERE account_id=?",
                       (new_currency,new_capital,new_capital,now,self.account_id))
            db.execute("INSERT INTO paper_equity_curve(account_id,timestamp_ms,cash,equity,realized_pnl,unrealized_pnl) VALUES (?,?,?,?,?,?)",
                       (self.account_id, now, new_capital, new_capital, 0.0, 0.0))
        self.initial_capital = new_capital
        self.quote_currency = new_currency


__all__ = ["EquityPoint", "PaperFill", "PaperPosition", "PaperSnapshot", "PaperTradeRejected", "PaperTradingEngine"]

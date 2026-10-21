"""Bounded in-memory market state and incremental trade-to-candle aggregation."""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from typing import Any

from tradingagent.market_data.ccxt_provider import timeframe_to_ms
from tradingagent.models import Candle, MarketTicker


@dataclass(slots=True)
class _LiveCandle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int = 1

    def update(self, price: float, amount: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += amount
        self.trades += 1

    def candle(self) -> Candle:
        return Candle(self.timestamp_ms, self.open, self.high, self.low,
                      self.close, self.volume, "ccxt", False)


@dataclass(slots=True)
class _MarketState:
    connection_state: str = "starting"
    source: str | None = None
    last_update_ms: int | None = None
    last_exchange_timestamp_ms: int | None = None
    last_successful_update_ms: int | None = None
    connected_at_ms: int | None = None
    last_disconnected_ms: int | None = None
    last_reconnected_ms: int | None = None
    reconnect_attempts: int = 0
    event_count: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    invalid_events: int = 0
    last_error: str | None = None
    latest_price: float | None = None
    latest_bid: float | None = None
    latest_ask: float | None = None
    live_candles: dict[str, _LiveCandle] = field(default_factory=dict)
    eligible_candle_start: dict[str, int] = field(default_factory=dict)


class MarketEventMonitor:
    """Apply delivered public trades once and emit only observed closed candles.

    The first candle in every timeframe after connecting is skipped because a stream
    can join mid-candle. Missing candles are never synthesized. Both event IDs and
    in-memory histories are bounded.
    """

    def __init__(self, symbols: tuple[str, ...], timeframes: tuple[str, ...],
                 *, max_history: int = 1000, dedup_capacity: int = 20_000,
                 max_data_age_ms: int = 120_000, clock_ms=None) -> None:
        if max_history < 50 or dedup_capacity < 1 or max_data_age_ms < 1:
            raise ValueError("invalid market monitor bounds")
        self.symbols = tuple(symbols)
        self.timeframes = tuple(timeframes)
        self.max_history = max_history
        self.dedup_capacity = dedup_capacity
        self.max_data_age_ms = max_data_age_ms
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._markets = {symbol: _MarketState() for symbol in self.symbols}
        self._history: dict[tuple[str, str], deque[Candle]] = {
            (symbol, timeframe): deque(maxlen=max_history)
            for symbol in self.symbols for timeframe in self.timeframes
        }
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._last_event_timestamp: dict[str, int] = {}
        self._closed_candle_count: dict[tuple[str, str], int] = {}
        self._closed_candles: deque[tuple[str, str, Candle]] = deque(maxlen=500)

    def seed_history(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        """Replace a bounded history with persisted closed candles for warm-up."""
        key = (symbol, timeframe)
        if key not in self._history:
            return
        rows = sorted((c for c in candles if c.complete), key=lambda c: c.timestamp_ms)
        unique = {c.timestamp_ms: c for c in rows}
        with self._lock:
            history = self._history[key]
            history.clear()
            history.extend(list(unique.values())[-self.max_history:])

    def history(self, symbol: str, timeframe: str) -> list[Candle]:
        with self._lock:
            return list(self._history.get((symbol, timeframe), ()))

    def connection_started(self, now_ms: int | None = None) -> None:
        now_ms = self._clock_ms() if now_ms is None else now_ms
        with self._lock:
            for state in self._markets.values():
                if state.connection_state != "reconnecting":
                    state.connection_state = "connecting"
                state.connected_at_ms = now_ms
                state.live_candles.clear()
                state.eligible_candle_start = {
                    tf: (now_ms // timeframe_to_ms(tf) + 1) * timeframe_to_ms(tf)
                    for tf in self.timeframes
                }

    def connection_ready(self, now_ms: int | None = None) -> None:
        now_ms = self._clock_ms() if now_ms is None else now_ms
        with self._lock:
            for state in self._markets.values():
                if state.connection_state == "reconnecting":
                    state.last_reconnected_ms = now_ms
                state.connection_state = "websocket"
                state.last_error = None

    def connection_failed(self, error: str, *, retrying: bool = True) -> None:
        now_ms = self._clock_ms()
        with self._lock:
            for state in self._markets.values():
                state.connection_state = "reconnecting" if retrying else "rest_fallback"
                state.reconnect_attempts += 1
                state.last_disconnected_ms = now_ms
                state.last_error = error[:240]
                state.live_candles.clear()
                state.eligible_candle_start = {
                    tf: (now_ms // timeframe_to_ms(tf) + 1) * timeframe_to_ms(tf)
                    for tf in self.timeframes
                }

    def websocket_unavailable(self, reason: str = "CCXT Pro unavailable") -> None:
        with self._lock:
            for state in self._markets.values():
                state.connection_state = "rest_fallback"
                state.source = "REST ticker snapshots"
                state.last_error = reason[:240]

    def apply_rest_ticker(self, ticker: MarketTicker) -> bool:
        with self._lock:
            state = self._markets.get(ticker.symbol)
            if state is None or ticker.last is None or ticker.stale:
                return False
            if (ticker.exchange_timestamp_ms is not None and
                    state.last_exchange_timestamp_ms is not None and
                    ticker.exchange_timestamp_ms < state.last_exchange_timestamp_ms):
                state.out_of_order_events += 1
                return False
            state.latest_price = ticker.last
            state.latest_bid, state.latest_ask = ticker.bid, ticker.ask
            if ticker.exchange_timestamp_ms is not None:
                state.last_exchange_timestamp_ms = ticker.exchange_timestamp_ms
            state.last_update_ms = ticker.received_timestamp_ms
            state.last_successful_update_ms = ticker.received_timestamp_ms
            state.source = "REST ticker snapshots"
            if state.connection_state not in {"websocket", "reconnecting"}:
                state.connection_state = "rest_fallback"
            return True

    @staticmethod
    def _trade_fields(trade: dict[str, Any]) -> tuple[str | None, int, float, float]:
        trade_id, timestamp = trade.get("id"), trade.get("timestamp")
        price, amount = trade.get("price"), trade.get("amount")
        if timestamp is None or price is None or amount is None:
            raise ValueError("trade must contain timestamp, price, and amount")
        timestamp_ms, price, amount = int(timestamp), float(price), float(amount)
        if (timestamp_ms < 0 or not math.isfinite(price) or price <= 0 or
                not math.isfinite(amount) or amount <= 0):
            raise ValueError("trade has invalid timestamp, price, or amount")
        return (str(trade_id) if trade_id is not None else None,
                timestamp_ms, price, amount)

    def apply_trade(self, symbol: str, trade: dict[str, Any],
                    received_at_ms: int | None = None) -> tuple[tuple[str, Candle], ...]:
        """Apply one delivered trade and return any timeframe candles it closed."""
        if symbol not in self._markets:
            return ()
        try:
            trade_id, timestamp_ms, price, amount = self._trade_fields(trade)
        except ValueError as exc:
            with self._lock:
                state = self._markets[symbol]
                state.invalid_events += 1
                state.last_error = str(exc)[:240]
            raise
        received_at_ms = self._clock_ms() if received_at_ms is None else received_at_ms
        with self._lock:
            state = self._markets[symbol]
            if state.connected_at_ms is not None and timestamp_ms < state.connected_at_ms:
                state.out_of_order_events += 1
                return ()
            event_id = trade_id or f"{timestamp_ms}:{price:.16g}:{amount:.16g}"
            dedup_key = (symbol, event_id)
            if dedup_key in self._seen:
                state.duplicate_events += 1
                return ()
            if timestamp_ms < self._last_event_timestamp.get(symbol, timestamp_ms):
                state.out_of_order_events += 1
                return ()
            self._seen[dedup_key] = None
            while len(self._seen) > self.dedup_capacity:
                self._seen.popitem(last=False)
            self._last_event_timestamp[symbol] = timestamp_ms
            state.latest_price = price
            state.last_exchange_timestamp_ms = timestamp_ms
            state.last_update_ms = received_at_ms
            state.last_successful_update_ms = received_at_ms
            state.event_count += 1
            state.connection_state = "websocket"
            state.source = "websocket trades"
            state.last_error = None
            closed: list[tuple[str, Candle]] = []
            for timeframe in self.timeframes:
                duration = timeframe_to_ms(timeframe)
                bucket = timestamp_ms // duration * duration
                eligible = state.eligible_candle_start.get(timeframe, 0)
                active = state.live_candles.get(timeframe)
                if active is None:
                    if bucket >= eligible:
                        state.live_candles[timeframe] = _LiveCandle(
                            bucket, price, price, price, price, amount)
                    continue
                if bucket < active.timestamp_ms:
                    state.out_of_order_events += 1
                    continue
                if bucket == active.timestamp_ms:
                    active.update(price, amount)
                    continue
                candle = Candle(active.timestamp_ms, active.open, active.high,
                    active.low, active.close, active.volume, "ccxt", True)
                if self._append_closed(symbol, timeframe, candle):
                    closed.append((timeframe, candle))
                if bucket >= eligible:
                    state.live_candles[timeframe] = _LiveCandle(
                        bucket, price, price, price, price, amount)
                else:
                    state.live_candles.pop(timeframe, None)
            return tuple(closed)

    def _append_closed(self, symbol: str, timeframe: str, candle: Candle) -> bool:
        history = self._history[(symbol, timeframe)]
        if history and candle.timestamp_ms <= history[-1].timestamp_ms:
            return False
        history.append(candle)
        key = (symbol, timeframe)
        self._closed_candle_count[key] = self._closed_candle_count.get(key, 0) + 1
        self._closed_candles.append((symbol, timeframe, candle))
        return True

    def advance_time(self, now_ms: int | None = None) -> tuple[tuple[str, str, Candle], ...]:
        """Close elapsed observed candles without inventing empty intervals."""
        now_ms = self._clock_ms() if now_ms is None else now_ms
        emitted: list[tuple[str, str, Candle]] = []
        with self._lock:
            for symbol, state in self._markets.items():
                for timeframe, active in tuple(state.live_candles.items()):
                    duration = timeframe_to_ms(timeframe)
                    if active.timestamp_ms + duration > now_ms:
                        continue
                    candle = Candle(active.timestamp_ms, active.open, active.high,
                        active.low, active.close, active.volume, "ccxt", True)
                    if self._append_closed(symbol, timeframe, candle):
                        emitted.append((symbol, timeframe, candle))
                    state.live_candles.pop(timeframe, None)
                    state.eligible_candle_start[timeframe] = active.timestamp_ms + duration
        return tuple(emitted)

    def rest_failed(self, symbol: str, error: str) -> None:
        """Record a public REST failure without changing the last successful update."""
        with self._lock:
            state = self._markets.get(symbol)
            if state is None:
                return
            state.last_error = error[:240]
            if state.connection_state != "websocket":
                state.connection_state = "rest_fallback"
                state.source = "REST ticker snapshots"

    def drain_closed_candles(self) -> tuple[tuple[str, str, Candle], ...]:
        with self._lock:
            rows = tuple(self._closed_candles)
            self._closed_candles.clear()
            return rows

    def snapshot(self, now_ms: int | None = None) -> list[dict[str, Any]]:
        now_ms = self._clock_ms() if now_ms is None else now_ms
        result = []
        with self._lock:
            for symbol, state in self._markets.items():
                age = None if state.last_update_ms is None else max(0, now_ms - state.last_update_ms)
                stale = age is None or age > self.max_data_age_ms
                result.append({
                    "symbol": symbol,
                    "connection_state": state.connection_state,
                    "source": state.source,
                    "last_update_ms": state.last_update_ms,
                    "last_exchange_timestamp_ms": state.last_exchange_timestamp_ms,
                    "last_successful_update_ms": state.last_successful_update_ms,
                    "last_disconnected_ms": state.last_disconnected_ms,
                    "last_reconnected_ms": state.last_reconnected_ms,
                    "age_ms": age,
                    "stale": stale,
                    "warning": ((f"Brak świeżej aktualizacji rynku; {state.last_error}"
                                 if state.last_error else "Brak świeżej aktualizacji rynku")
                                if stale else state.last_error),
                    "reconnect_attempts": state.reconnect_attempts,
                    "event_count": state.event_count,
                    "duplicate_events": state.duplicate_events,
                    "out_of_order_events": state.out_of_order_events,
                    "invalid_events": state.invalid_events,
                    "last_error": state.last_error,
                    "latest_price": state.latest_price,
                    "bid": state.latest_bid,
                    "ask": state.latest_ask,
                    "live_candles": {tf: asdict(candle.candle())
                                     for tf, candle in state.live_candles.items()},
                    "closed_candle_counts": {
                        tf: self._closed_candle_count.get((symbol, tf), 0)
                        for tf in self.timeframes
                    },
                    "last_closed_candle_ms": {
                        tf: (self._history[(symbol, tf)][-1].timestamp_ms
                             if self._history[(symbol, tf)] else None)
                        for tf in self.timeframes
                    },
                })
        return result


__all__ = ["MarketEventMonitor"]

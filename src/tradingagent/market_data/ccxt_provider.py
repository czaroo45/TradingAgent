"""Synchronous public-market-data adapter with validation, cache and bounded retries."""
from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter
from dataclasses import replace
from threading import BoundedSemaphore
from typing import Any, Callable

try:
    import ccxt
except ImportError:  # cached/offline workflows can import the package without CCXT installed
    ccxt = None  # type: ignore[assignment]

from tradingagent.models import Candle, DataQuality, MarketTicker, OrderBookSnapshot

log = logging.getLogger(__name__)
TIMEFRAME_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                "4h": 14_400_000, "1d": 86_400_000}
_TIMEFRAME_UNITS_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000,
                       "d": 86_400_000, "w": 604_800_000}


def timeframe_to_ms(timeframe: str) -> int:
    if timeframe in TIMEFRAME_MS:
        return TIMEFRAME_MS[timeframe]
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", timeframe)
    if not match:
        raise ValueError(f"Unsupported normalized timeframe: {timeframe}")
    return int(match.group(1)) * _TIMEFRAME_UNITS_MS[match.group(2)]


class MarketDataError(RuntimeError):
    """Raised when market data cannot be safely retrieved or validated."""


class CcxtMarketData:
    def __init__(self, exchange_id: str = "binance", retries: int = 3,
                 timeout_ms: int = 15_000, max_concurrency: int = 2,
                 cache_ttl_seconds: float = 5.0, sleep: Callable[[float], None] = time.sleep,
                 clock_ms: Callable[[], int] | None = None) -> None:
        if ccxt is None:
            raise RuntimeError("CCXT is required for online market data; install the project dependencies")
        if min(retries, timeout_ms, max_concurrency) <= 0 or cache_ttl_seconds < 0:
            raise ValueError("retries, timeout, concurrency must be positive and cache TTL nonnegative")
        exchange_type = getattr(ccxt, exchange_id, None)
        if exchange_type is None or exchange_id not in ccxt.exchanges:
            raise ValueError(f"Unsupported CCXT exchange: {exchange_id}")
        # Keep the exchange's own endpoint-specific rate limit enabled. Do not override it
        # with a global guessed request rate: CCXT costs vary by endpoint and venue.
        self.exchange: Any = exchange_type({"enableRateLimit": True, "timeout": timeout_ms})
        self.retries = retries
        self.cache_ttl_seconds = cache_ttl_seconds
        self._sleep = sleep
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._semaphore = BoundedSemaphore(max_concurrency)
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._markets_loaded = False

    def _load_markets(self) -> None:
        if self._markets_loaded:
            return
        self._call(self.exchange.load_markets)
        self._markets_loaded = True

    def _call(self, fn: Callable[..., Any], *args: Any, cache_key: tuple[Any, ...] | None = None,
              **kwargs: Any) -> Any:
        if cache_key is not None and cache_key in self._cache:
            cached_at, value = self._cache[cache_key]
            if time.monotonic() - cached_at <= self.cache_ttl_seconds:
                return value
        transient = (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout,
                     ccxt.RateLimitExceeded, ccxt.DDoSProtection)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._semaphore:
                    value = fn(*args, **kwargs)
                if cache_key is not None:
                    if len(self._cache) > 512:
                        expiry = time.monotonic() - self.cache_ttl_seconds
                        self._cache = {key: entry for key, entry in self._cache.items() if entry[0] >= expiry}
                    self._cache[cache_key] = (time.monotonic(), value)
                return value
            except transient as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    delay = min(0.5 * (2 ** attempt), 8.0)
                    headers = getattr(exc, "headers", None) or {}
                    retry_after = headers.get("Retry-After") if isinstance(headers, dict) else None
                    retry_after = retry_after or getattr(exc, "retry_after", None)
                    if retry_after is not None:
                        try:
                            delay = min(max(delay, float(retry_after)), 30.0)
                        except (TypeError, ValueError):
                            pass
                    log.warning("Retry %d/%d after exchange/network limit for %s: %s",
                                attempt + 1, self.retries, self.exchange.id, exc)
                    self._sleep(delay)
            except ccxt.BaseError as exc:
                raise MarketDataError(f"Exchange {self.exchange.id} rejected request: {exc}") from exc
        raise MarketDataError(f"Request to {self.exchange.id} failed after {self.retries} attempts: {last_error}") from last_error

    def _check_market(self, symbol: str, timeframe: str | None = None) -> None:
        self._load_markets()
        if symbol not in self.exchange.markets:
            raise MarketDataError(f"{self.exchange.id} does not list market {symbol}")
        if timeframe is not None:
            supported = self.exchange.timeframes or {}
            if timeframe not in supported:
                raise MarketDataError(f"{self.exchange.id} does not support timeframe {timeframe}")
            if not self.exchange.has.get("fetchOHLCV"):
                raise MarketDataError(f"{self.exchange.id} does not support OHLCV")

    def market_order_limits(self, symbol: str) -> dict[str, float]:
        """Return published base-amount and quote-cost minima for a listed market.

        Missing or malformed exchange metadata is omitted; callers may then apply
        their configured amount step and cannot mistake an unknown minimum for a
        known exchange constraint.
        """
        self._check_market(symbol)
        market = self.exchange.markets[symbol]
        limits = market.get("limits") or {}
        result: dict[str, float] = {}
        for key, section in (("min_amount", limits.get("amount")),
                             ("min_notional", limits.get("cost"))):
            value = section.get("min") if isinstance(section, dict) else None
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed) and parsed > 0:
                result[key] = parsed
        return result

    def fetch_ticker(self, symbol: str) -> MarketTicker:
        self._check_market(symbol)
        if not self.exchange.has.get("fetchTicker"):
            raise MarketDataError(f"{self.exchange.id} does not support ticker")
        raw = self._call(self.exchange.fetch_ticker, symbol,
                         cache_key=("ticker", symbol))
        if not isinstance(raw, dict):
            raise MarketDataError(f"Malformed ticker response from {self.exchange.id}")
        def valid_price(value: Any) -> float | None:
            if value is None:
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) and number > 0 else None
        last = valid_price(raw.get("last") if raw.get("last") is not None else raw.get("close"))
        bid, ask = valid_price(raw.get("bid")), valid_price(raw.get("ask"))
        if bid is not None and ask is not None and ask < bid:
            raise MarketDataError(f"Crossed ticker quote from {self.exchange.id} for {symbol}")
        spread = ask - bid if ask is not None and bid is not None and ask >= bid else None
        def valid_volume(value: Any) -> float | None:
            if value is None:
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) and number >= 0 else None
        stamp = raw.get("timestamp")
        received = self._clock_ms()
        try:
            exchange_stamp = int(stamp) if stamp is not None else None
        except (TypeError, ValueError, OverflowError):
            exchange_stamp = None
        stale = exchange_stamp is not None and (received - exchange_stamp > 120_000 or exchange_stamp - received > 60_000)
        return MarketTicker(self.exchange.id, symbol, last, bid, ask, spread,
                            valid_volume(raw.get("baseVolume")), valid_volume(raw.get("quoteVolume")),
                            exchange_stamp, received, stale, valid_volume(raw.get("bidVolume")),
                            valid_volume(raw.get("askVolume")))

    def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBookSnapshot | None:
        """Fetch public depth when supported; None means the venue exposes no unified endpoint."""
        self._check_market(symbol)
        if not self.exchange.has.get("fetchOrderBook"):
            return None
        if not 1 <= limit <= 1000:
            raise ValueError("order book limit must be between 1 and 1000")
        raw = self._call(self.exchange.fetch_order_book, symbol, limit=limit,
                         cache_key=("orderbook", symbol, limit))
        if not isinstance(raw, dict):
            raise MarketDataError(f"Malformed order book response from {self.exchange.id}")
        def levels(name: str) -> tuple[tuple[float, float], ...]:
            parsed = []
            for item in raw.get(name) or []:
                try:
                    if len(item) < 2:
                        raise ValueError("expected price and amount")
                    price, amount = float(item[0]), float(item[1])
                except (TypeError, ValueError, IndexError, OverflowError) as exc:
                    raise MarketDataError(f"Malformed {name} level from {self.exchange.id}") from exc
                if not math.isfinite(price) or not math.isfinite(amount) or price <= 0 or amount <= 0:
                    raise MarketDataError(f"Invalid {name} level from {self.exchange.id}")
                parsed.append((price, amount))
            return tuple(parsed)
        bids, asks = levels("bids"), levels("asks")
        if any(bids[i][0] < bids[i + 1][0] for i in range(len(bids) - 1)):
            raise MarketDataError("bid levels are not ordered best-to-worst")
        if any(asks[i][0] > asks[i + 1][0] for i in range(len(asks) - 1)):
            raise MarketDataError("ask levels are not ordered best-to-worst")
        stamp = raw.get("timestamp")
        received = self._clock_ms()
        try:
            exchange_stamp = int(stamp) if stamp is not None else None
        except (TypeError, ValueError, OverflowError):
            exchange_stamp = None
        stale = exchange_stamp is not None and (received - exchange_stamp > 120_000 or exchange_stamp - received > 60_000)
        if bids and asks and asks[0][0] < bids[0][0]:
            raise MarketDataError(f"Crossed order book from {self.exchange.id} for {symbol}")
        return OrderBookSnapshot(self.exchange.id, symbol, bids, asks,
                                 exchange_stamp, received, stale)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[Candle]:
        """Return only fully closed candles for existing call sites."""
        candles, _ = self.fetch_candles(symbol, timeframe, limit)
        return [c for c in candles if c.complete]

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 300) -> tuple[list[Candle], DataQuality]:
        self._check_market(symbol, timeframe)
        if not 1 <= limit <= 1000:
            raise ValueError("OHLCV limit must be between 1 and 1000")
        raw = self._call(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit,
                         cache_key=("ohlcv", symbol, timeframe, limit))
        return validate_ohlcv(raw, self.exchange.id, symbol, timeframe, self._clock_ms())

    @staticmethod
    def _parse(row: list[Any]) -> Candle:
        candles, quality = validate_ohlcv([row], "unknown", "unknown", "1d", 0)
        if quality.invalid_rows or not candles:
            raise MarketDataError(quality.invalid_rows[0] if quality.invalid_rows else "invalid OHLCV row")
        return candles[0]

    def fetch_history(self, symbol: str, timeframe: str, since_ms: int, until_ms: int,
                      page_limit: int = 500, max_pages: int = 100) -> tuple[list[Candle], DataQuality]:
        """Fetch a bounded UTC time range using since-based pages and timestamp deduplication."""
        self._check_market(symbol, timeframe)
        duration = timeframe_to_ms(timeframe)
        if since_ms < 0 or until_ms <= since_ms or not 1 <= page_limit <= 1000 or max_pages <= 0:
            raise ValueError("invalid history range or pagination limits")
        cursor = since_ms
        rows: list[list[Any]] = []
        truncated = False
        for _ in range(max_pages):
            if cursor >= until_ms:
                break
            page = self._call(self.exchange.fetch_ohlcv, symbol, timeframe=timeframe,
                              since=cursor, limit=page_limit)
            if not page:
                break
            valid_stamps: list[int] = []
            for row in page:
                try:
                    stamp_value = float(row[0])
                    if not math.isfinite(stamp_value) or int(stamp_value) != stamp_value:
                        rows.append(row)  # preserve malformed response for quality reporting
                        continue
                    stamp = int(stamp_value)
                    valid_stamps.append(stamp)
                    if since_ms <= stamp < until_ms:
                        rows.append(row)
                except (TypeError, ValueError, IndexError, OverflowError):
                    rows.append(row)
            if not valid_stamps:
                break
            next_cursor = max(valid_stamps) + duration
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(page) < page_limit:
                break
        else:
            truncated = cursor < until_ms
        checked_at_ms = self._clock_ms()
        closure_cutoff_ms = min(until_ms, checked_at_ms)
        candles, quality = validate_ohlcv(rows, self.exchange.id, symbol, timeframe,
                                          checked_at_ms, staleness_reference_ms=closure_cutoff_ms)
        if truncated:
            quality = replace(quality, pagination_truncated=True)
        # Historical windows are not stale just because they end in the past. Still report
        # closure accurately and let gap/duplicate validation decide whether data is usable.
        return [c for c in candles if c.timestamp_ms < until_ms], quality

    def close(self) -> None:
        close = getattr(self.exchange, "close", None)
        if callable(close):
            close()


def validate_ohlcv(rows: list[list[Any]], exchange: str, symbol: str, timeframe: str,
                   now_ms: int, stale_after_periods: int = 3,
                   staleness_reference_ms: int | None = None) -> tuple[list[Candle], DataQuality]:
    """Normalize bars while reporting defects instead of silently concealing them."""
    duration = timeframe_to_ms(timeframe)
    timestamps: list[int] = []
    parsed: list[Candle] = []
    invalid: list[str] = []
    for index, row in enumerate(rows):
        try:
            if len(row) < 6:
                raise ValueError("expected six fields")
            values = [float(v) for v in row[:6]]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("non-finite number")
            stamp, op, high, low, close, volume = values
            if stamp < 0 or int(stamp) != stamp or volume < 0 or min(op, high, low, close) <= 0:
                raise ValueError("timestamp, price, or volume outside bounds")
            if high < max(op, close, low) or low > min(op, close, high):
                raise ValueError("OHLC bounds violated")
            timestamps.append(int(stamp))
            parsed.append(Candle(int(stamp), op, high, low, close, volume, exchange,
                                 int(stamp) + duration <= now_ms))
        except (TypeError, ValueError, OverflowError) as exc:
            invalid.append(f"row {index}: {exc}")
    counts = Counter(c.timestamp_ms for c in parsed)
    duplicates = tuple(sorted(stamp for stamp, count in counts.items() if count > 1))
    non_monotonic = any(b <= a for a, b in zip(timestamps, timestamps[1:]))
    unique = {c.timestamp_ms: c for c in parsed}
    ordered = [unique[key] for key in sorted(unique)]
    gaps = tuple((a.timestamp_ms, b.timestamp_ms) for a, b in zip(ordered, ordered[1:])
                 if b.timestamp_ms - a.timestamp_ms > duration)
    latest_closed = next((c for c in reversed(ordered) if c.complete), None)
    freshness_reference = now_ms if staleness_reference_ms is None else staleness_reference_ms
    stale_by = max(0, freshness_reference - (latest_closed.timestamp_ms + duration)) if latest_closed else freshness_reference
    stale = latest_closed is None or stale_by > duration * stale_after_periods
    incomplete = bool(ordered and not ordered[-1].complete)
    quality = DataQuality(exchange, symbol, timeframe, now_ms, len(ordered), duplicates,
                          non_monotonic, gaps, tuple(invalid), stale, stale_by, incomplete)
    return ordered, quality

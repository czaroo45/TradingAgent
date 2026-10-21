"""Optional asynchronous CCXT Pro public trade/OHLCV stream adapter."""
from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

try:
    import ccxt
except ImportError:  # module remains importable for installations without streaming support
    ccxt = None  # type: ignore[assignment]

from tradingagent.market_data.ccxt_provider import MarketDataError, validate_ohlcv
from tradingagent.models import Candle, DataQuality


class CcxtProMarketData:
    def __init__(self, exchange_id: str = "binance", timeout_ms: int = 15_000,
                 reconnect_attempts: int = 5) -> None:
        if ccxt is None:
            raise RuntimeError("CCXT is required for WebSocket market data")
        try:
            ccxtpro = importlib.import_module("ccxt.pro")
        except ImportError as exc:
            raise RuntimeError("WebSocket feed requires CCXT Pro (`import ccxt.pro`) support") from exc
        exchange_type = getattr(ccxtpro, exchange_id, None)
        if exchange_type is None:
            raise ValueError(f"CCXT Pro does not provide exchange {exchange_id}")
        if timeout_ms <= 0 or reconnect_attempts <= 0:
            raise ValueError("timeout and reconnect attempts must be positive")
        self.exchange: Any = exchange_type({"enableRateLimit": True, "timeout": timeout_ms})
        self.reconnect_attempts = reconnect_attempts
        self._markets_loaded = False

    async def _load_markets(self) -> None:
        if not self._markets_loaded:
            await self.exchange.load_markets()
            self._markets_loaded = True

    @property
    def supports_trade_stream(self) -> bool:
        return bool(self.exchange.has.get("watchTradesForSymbols") or
                    self.exchange.has.get("watchTrades"))

    async def watch_trades_for_symbols(self, symbols: tuple[str, ...] | list[str]) -> list[dict]:
        """Return the next public trade batch; no credentials or private streams are used."""
        await self._load_markets()
        missing = set(symbols) - set(self.exchange.markets)
        if missing:
            raise MarketDataError(f"{self.exchange.id} does not list markets: {', '.join(sorted(missing))}")
        if self.exchange.has.get("watchTradesForSymbols"):
            trades = await self.exchange.watch_trades_for_symbols(list(symbols))
            return list(trades or [])
        if len(symbols) == 1 and self.exchange.has.get("watchTrades"):
            trades = await self.exchange.watch_trades(symbols[0])
            return list(trades or [])
        raise MarketDataError(f"{self.exchange.id} does not support a multi-symbol trade stream")

    async def watch_ohlcv(self, symbol: str, timeframe: str) -> tuple[list[Candle], DataQuality]:
        await self._load_markets()
        if symbol not in self.exchange.markets:
            raise MarketDataError(f"{self.exchange.id} does not list market {symbol}")
        if timeframe not in (self.exchange.timeframes or {}):
            raise MarketDataError(f"{self.exchange.id} does not support timeframe {timeframe}")
        if not self.exchange.has.get("watchOHLCV"):
            raise MarketDataError(f"{self.exchange.id} does not support WebSocket OHLCV for this market")
        error: Exception | None = None
        for attempt in range(self.reconnect_attempts):
            try:
                rows = await self.exchange.watch_ohlcv(symbol, timeframe)
                now_ms = int(time.time() * 1000)
                return validate_ohlcv(rows, self.exchange.id, symbol, timeframe, now_ms)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout,
                    ccxt.RateLimitExceeded) as exc:
                error = exc
                if attempt + 1 < self.reconnect_attempts:
                    await asyncio.sleep(min(0.5 * (2 ** attempt), 8.0))
        raise MarketDataError(f"WebSocket feed failed after {self.reconnect_attempts} attempts: {error}") from error

    async def close(self) -> None:
        await self.exchange.close()

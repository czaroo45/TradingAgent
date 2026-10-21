from typing import Protocol
from tradingagent.models import Candle


class MarketDataProvider(Protocol):
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> list[Candle]: ...

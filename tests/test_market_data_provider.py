import json
from pathlib import Path
from types import SimpleNamespace

from tradingagent.market_data import CcxtMarketData
import tradingagent.market_data.ccxt_provider as provider_module


class NetworkError(Exception): pass
class ExchangeNotAvailable(NetworkError): pass
class RequestTimeout(NetworkError): pass
class RateLimitExceeded(NetworkError): pass
class DDoSProtection(NetworkError): pass
class BaseError(Exception): pass


class FakeExchange:
    def __init__(self, _options):
        self.id = "fake"
        self.has = {"fetchOHLCV": True, "fetchTicker": True, "fetchOrderBook": True}
        self.timeframes = {"5m": "5m"}
        self.markets = {"BTC/USDT": {}}
        self.calls = 0
        self.failures = 0
        self.ticker_calls = 0

    def load_markets(self):
        return self.markets

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=100):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            response = json.loads((Path(__file__).parent / "fixtures/api_error_429.json").read_text())
            raise RateLimitExceeded(f"HTTP {response['status']}: {response['message']}")
        if since is None:
            return [[0,100,102,99,101,1],[300000,101,103,100,102,2]]
        if since == 0:
            return [[0,100,102,99,101,1],[300000,101,103,100,102,2]]
        return [[600000,102,104,101,103,3],[900000,103,105,102,104,4]]

    def fetch_ticker(self, symbol):
        self.ticker_calls += 1
        return {"last": 101, "bid": 100, "ask": 102, "baseVolume": 10,
                "quoteVolume": 1000, "timestamp": 1_000_000}

    def fetch_order_book(self, symbol, limit=20):
        return {"timestamp": 1_000_000, "bids": [[100, 1], [99, 2]],
                "asks": [[101, 1], [102, 2]]}

    def close(self): pass


def fake_ccxt():
    return SimpleNamespace(fake=FakeExchange, exchanges=["fake"], NetworkError=NetworkError,
                           ExchangeNotAvailable=ExchangeNotAvailable, RequestTimeout=RequestTimeout,
                           RateLimitExceeded=RateLimitExceeded, DDoSProtection=DDoSProtection,
                           BaseError=BaseError)


def test_retries_rate_limit_and_ticker_cache(monkeypatch):
    monkeypatch.setattr(provider_module, "ccxt", fake_ccxt())
    sleeps = []
    client = CcxtMarketData("fake", sleep=sleeps.append, clock_ms=lambda: 1_000_001)
    client.exchange.failures = 1
    candles, report = client.fetch_candles("BTC/USDT", "5m", 2)
    assert len(candles) == 2 and report.acceptable
    assert sleeps == [0.5]
    one = client.fetch_ticker("BTC/USDT")
    two = client.fetch_ticker("BTC/USDT")
    assert one.spread_bps > 0
    assert one.base_volume == 10 and one.last == 101
    assert two == one
    assert client.exchange.ticker_calls == 1
    book = client.fetch_order_book("BTC/USDT")
    assert book is not None and book.spread_bps > 0
    assert book.estimate_slippage_bps("buy", 150) > 0
    assert book.estimate_slippage_bps("sell", 150) > 0


def test_market_order_limits_read_published_minimum_amount_and_cost(monkeypatch):
    monkeypatch.setattr(provider_module, "ccxt", fake_ccxt())
    client=CcxtMarketData("fake")
    client.exchange.markets["BTC/USDT"]["limits"]={
        "amount":{"min":0.001},"cost":{"min":5}}
    assert client.market_order_limits("BTC/USDT")=={"min_amount":0.001,"min_notional":5.0}
    client.exchange.markets["BTC/USDT"]["limits"]={"amount":{"min":None},"cost":{}}
    assert client.market_order_limits("BTC/USDT")=={}


def test_date_range_history_paginates_and_deduplicates(monkeypatch):
    monkeypatch.setattr(provider_module, "ccxt", fake_ccxt())
    client = CcxtMarketData("fake", clock_ms=lambda: 1_200_000)
    candles, report = client.fetch_history("BTC/USDT", "5m", 0, 1_200_000, page_limit=2)
    assert [c.timestamp_ms for c in candles] == [0, 300_000, 600_000, 900_000]
    assert report.acceptable
    assert client.exchange.calls == 2


def test_pagination_cap_is_reported_instead_of_presenting_partial_history_as_complete(monkeypatch):
    monkeypatch.setattr(provider_module, "ccxt", fake_ccxt())
    client = CcxtMarketData("fake", clock_ms=lambda: 1_200_000)
    candles, report = client.fetch_history("BTC/USDT", "5m", 0, 2_000_000,
                                           page_limit=2, max_pages=1)
    assert len(candles) == 2
    assert report.pagination_truncated
    assert not report.acceptable

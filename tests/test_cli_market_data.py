import json

from tradingagent.cli import _jsonable, _timestamp
from tradingagent.models import DataQuality, MarketTicker


def test_market_json_includes_quality_and_derived_spread_fields():
    ticker = MarketTicker("demo", "BTC/USDT", 100, 99, 101, 2, 10, 1000,
                          1_000, 1_001, False)
    quality = DataQuality("demo", "BTC/USDT", "5m", 1_001, 2, incomplete_latest=True)
    payload = json.loads(json.dumps(_jsonable({"ticker": ticker, "quality": quality})))
    assert abs(payload["ticker"]["spread_bps"] - 200) < 2.1
    assert payload["quality"]["acceptable"]
    assert "latest candle is incomplete" in payload["quality"]["warnings"]


def test_cli_date_is_parsed_as_utc_epoch():
    assert _timestamp("1970-01-01T00:00:01Z") == 1000

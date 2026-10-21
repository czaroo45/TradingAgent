import pytest
from tradingagent.market_data import CcxtMarketData, MarketDataError


def test_invalid_ohlcv_rows_are_rejected():
    with pytest.raises(MarketDataError):
        CcxtMarketData._parse([1, 1, 0, 2, 1, 1])
    with pytest.raises(MarketDataError):
        CcxtMarketData._parse([1, float("nan"), 2, 1, 1, 1])

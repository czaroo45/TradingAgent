import math
from tradingagent.models import DataQuality

from tradingagent.analysis import analyze, calculate_indicators
from tradingagent.models import Candle


def candles_from_closes(closes):
    return [Candle(i * 3_600_000, p, p * 1.0005, p * .9995, p, 100 + i) for i, p in enumerate(closes)]


def test_insufficient_data_has_no_scenario():
    result = analyze("BTC/USDT", "1h", candles_from_closes(range(1, 20)))
    assert result.regime == "insufficient_data"
    assert result.scenario is None


def test_uptrend_is_bullish_and_has_conditional_setup():
    result = analyze("BTC/USDT", "1h", candles_from_closes([100 + i + 5 * math.sin(i * 0.8) for i in range(80)]))
    assert result.regime == "bullish"
    assert result.indicators is not None
    assert result.scenario is not None
    assert result.scenario.direction == "long"
    assert result.scenario.targets[0] > result.scenario.entry_high


def test_indicators_are_finite():
    indicators = calculate_indicators(candles_from_closes([100 + i * .1 for i in range(60)]))
    assert 0 <= indicators.rsi <= 100
    assert indicators.atr > 0


def test_rsi_uses_wilder_smoothing_instead_of_only_last_14_changes():
    closes=[100.0]
    for i in range(1,60):
        closes.append(closes[-1]+(1.0 if i%3 else -0.8))
    changes=[b-a for a,b in zip(closes,closes[1:])]
    gains=[max(change,0) for change in changes]
    losses=[max(-change,0) for change in changes]
    avg_gain=sum(gains[:14])/14
    avg_loss=sum(losses[:14])/14
    for gain,loss in zip(gains[14:],losses[14:]):
        avg_gain=(avg_gain*13+gain)/14
        avg_loss=(avg_loss*13+loss)/14
    expected=100-100/(1+avg_gain/avg_loss)
    assert calculate_indicators(candles_from_closes(closes)).rsi == expected


def test_indicator_calculation_rejects_incomplete_and_non_monotonic_history():
    import pytest
    rows=candles_from_closes([100+i*.1 for i in range(60)])
    rows[-2]=Candle(rows[-2].timestamp_ms,rows[-2].open,rows[-2].high,
                    rows[-2].low,rows[-2].close,rows[-2].volume,"binance",False)
    with pytest.raises(ValueError,match="closed candles"):
        calculate_indicators(rows)
    rows=candles_from_closes([100+i*.1 for i in range(60)])
    rows[20]=Candle(rows[19].timestamp_ms,rows[20].open,rows[20].high,
                    rows[20].low,rows[20].close,rows[20].volume,"binance",True)
    with pytest.raises(ValueError,match="strictly increasing"):
        calculate_indicators(rows)


def test_incomplete_latest_candle_cannot_confirm_scenario():
    rows = candles_from_closes([100 + i + 5 * math.sin(i * 0.8) for i in range(80)])
    rows[-1] = Candle(rows[-1].timestamp_ms, rows[-1].open, rows[-1].high,
                      rows[-1].low, rows[-1].close, rows[-1].volume,
                      "binance", False)
    result = analyze("BTC/USDT", "1h", rows)
    assert result.scenario is None
    assert "still forming" in result.explanation[0]


def test_unacceptable_data_quality_blocks_scenario():
    rows = candles_from_closes([100 + i + 5 * math.sin(i * 0.8) for i in range(80)])
    quality = DataQuality("binance", "BTC/USDT", "1h", 1, 80, stale=True, stale_by_ms=999)
    result = analyze("BTC/USDT", "1h", rows, quality=quality)
    assert result.scenario is None
    assert result.regime == "insufficient_data"

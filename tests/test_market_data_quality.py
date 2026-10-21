import json
from pathlib import Path

from tradingagent.market_data import timeframe_to_ms, validate_ohlcv

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_valid_candles_have_clean_quality():
    candles, report = validate_ohlcv(fixture("valid_5m.json"), "demo", "BTC/USDT", "5m", 900_000)
    assert len(candles) == 3
    assert report.acceptable
    assert all(c.complete for c in candles)
    assert all(c.source == "demo" for c in candles)
    assert timeframe_to_ms("30m") == 1_800_000


def test_gap_and_duplicate_are_reported_without_fabricated_candles():
    candles, gap_report = validate_ohlcv(fixture("gap_5m.json"), "demo", "BTC/USDT", "5m", 900_000)
    assert len(candles) == 2
    assert gap_report.gaps == ((0, 600_000),)
    assert not gap_report.acceptable
    _, duplicate_report = validate_ohlcv(fixture("duplicate_5m.json"), "demo", "BTC/USDT", "5m", 900_000)
    assert duplicate_report.duplicate_timestamps == (300_000,)
    assert duplicate_report.non_monotonic


def test_invalid_stale_and_incomplete_data_are_explicit():
    candles, invalid_report = validate_ohlcv(fixture("invalid_ohlcv.json"), "demo", "BTC/USDT", "5m", 600_000)
    assert len(candles) == 1
    assert invalid_report.invalid_rows
    _, stale_report = validate_ohlcv(fixture("stale_5m.json"), "demo", "BTC/USDT", "5m", 3_000_000)
    assert stale_report.stale
    candles, incomplete = validate_ohlcv(fixture("incomplete_5m.json"), "demo", "BTC/USDT", "5m", 700_000)
    assert not candles[-1].complete
    assert incomplete.incomplete_latest
    assert incomplete.acceptable  # incomplete bar is visible but excluded from close-confirmed signals


def test_historical_range_freshness_is_measured_at_requested_end_not_wall_clock():
    _, report = validate_ohlcv(fixture("valid_5m.json"), "demo", "BTC/USDT", "5m",
                               10_000_000, staleness_reference_ms=900_000)
    assert not report.stale
    assert report.checked_at_ms == 10_000_000

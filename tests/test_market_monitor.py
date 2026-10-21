from tradingagent.market_data import MarketEventMonitor
from tradingagent.models import Candle, MarketTicker


def _ticker(symbol: str, timestamp: int) -> MarketTicker:
    return MarketTicker("binance", symbol, 100.0, 99.9, 100.1, 20.0,
                        10.0, 1000.0, timestamp, timestamp, False)


def test_deduplicates_events_and_ignores_out_of_order_trades():
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m",), clock_ms=lambda: 120_000)
    monitor.connection_started(0)
    monitor.connection_ready()
    trade = {"id": "1", "timestamp": 60_000, "price": 100, "amount": 2}
    assert monitor.apply_trade("BTC/USDT", trade, 60_001) == ()
    assert monitor.apply_trade("BTC/USDT", trade, 60_002) == ()
    assert monitor.apply_trade("BTC/USDT", {"id": "3", "timestamp": 59_999,
        "price": 99, "amount": 1}, 60_003) == ()
    state = monitor.snapshot(60_004)[0]
    assert state["event_count"] == 1
    assert state["duplicate_events"] == 1
    assert state["out_of_order_events"] == 1
    assert state["live_candles"]["1m"]["volume"] == 2


def test_invalid_trade_is_counted_without_advancing_market_state():
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m",))
    try:
        monitor.apply_trade("BTC/USDT", {"id": "bad", "timestamp": 1,
            "price": float("nan"), "amount": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid market event should be rejected")
    state = monitor.snapshot()[0]
    assert state["invalid_events"] == 1
    assert state["event_count"] == 0
    assert state["last_error"]


def test_candle_boundaries_and_multiple_timeframes_emit_only_closed_candles():
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m", "5m"), clock_ms=lambda: 600_000)
    monitor.connection_started(10_000)
    monitor.apply_trade("BTC/USDT", {"id": "a", "timestamp": 20_000,
        "price": 100, "amount": 1}, 20_001)
    monitor.apply_trade("BTC/USDT", {"id": "b", "timestamp": 60_000,
        "price": 101, "amount": 2}, 60_001)
    monitor.apply_trade("BTC/USDT", {"id": "c", "timestamp": 119_999,
        "price": 103, "amount": 3}, 120_000)
    assert monitor.snapshot(119_999)[0]["live_candles"]["1m"]["complete"] is False
    assert monitor.advance_time(119_999) == ()
    emitted = monitor.advance_time(120_000)
    assert [(sym, tf, candle.timestamp_ms) for sym, tf, candle in emitted] == [
        ("BTC/USDT", "1m", 60_000)]
    candle = monitor.history("BTC/USDT", "1m")[-1]
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == (101, 103, 101, 103, 5)
    assert candle.complete is True
    # No new observed trade means no next candle is invented.
    assert monitor.snapshot(120_001)[0]["live_candles"] == {}
    assert monitor.history("BTC/USDT", "5m") == []


def test_staleness_rest_health_and_reconnect_reset_are_explicit():
    now = [1_000]
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m",), max_data_age_ms=100,
                                 clock_ms=lambda: now[0])
    monitor.connection_started(now[0])
    monitor.websocket_unavailable()
    assert monitor.apply_rest_ticker(_ticker("BTC/USDT", 1_000))
    assert monitor.snapshot()[0]["connection_state"] == "rest_fallback"
    now[0] = 1_101
    assert monitor.snapshot()[0]["stale"] is True
    monitor.connection_failed("timeout")
    state = monitor.snapshot()[0]
    assert state["reconnect_attempts"] == 1
    assert state["last_successful_update_ms"] == 1_000
    assert state["connection_state"] == "reconnecting"
    monitor.connection_started(1_101)
    monitor.connection_ready(1_102)
    assert monitor.snapshot(1_102)[0]["last_disconnected_ms"] == 1_101
    assert monitor.snapshot(1_102)[0]["last_reconnected_ms"] == 1_102
    monitor.apply_trade("BTC/USDT", {"id": "late", "timestamp": 1_105,
        "price": 101, "amount": 1}, 1_106)
    assert monitor.snapshot()[0]["live_candles"] == {}


def test_rest_warmup_recovers_missed_stream_candle_without_inventing_bars():
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m",), clock_ms=lambda: 240_000)
    monitor.connection_started(0)
    monitor.apply_trade("BTC/USDT", {"id": "a", "timestamp": 60_000,
        "price": 100, "amount": 1}, 60_001)
    monitor.apply_trade("BTC/USDT", {"id": "b", "timestamp": 239_999,
        "price": 104, "amount": 2}, 240_000)
    # The stream observed the bar at 1m but missed the 2m interval.
    assert [c.timestamp_ms for c in monitor.history("BTC/USDT", "1m")] == [60_000]
    rest_rows = [
        Candle(60_000, 100, 101, 99, 100, 1, "ccxt", True),
        Candle(120_000, 100, 103, 100, 102, 7, "ccxt", True),
    ]
    monitor.seed_history("BTC/USDT", "1m", rest_rows)
    assert [c.timestamp_ms for c in monitor.history("BTC/USDT", "1m")] == [60_000, 120_000]
    assert monitor.history("BTC/USDT", "1m")[-1].volume == 7


def test_warmup_keeps_only_closed_bounded_history():
    monitor = MarketEventMonitor(("BTC/USDT",), ("1m",), max_history=50)
    rows = [Candle(i * 60_000, 1, 2, 1, 2, 1, "ccxt", True) for i in range(55)]
    monitor.seed_history("BTC/USDT", "1m", rows + [
        Candle(55 * 60_000, 1, 2, 1, 2, 1, "ccxt", False)])
    history = monitor.history("BTC/USDT", "1m")
    assert len(history) == 50
    assert history[0].timestamp_ms == 5 * 60_000
    assert history[-1].timestamp_ms == 54 * 60_000
    assert all(candle.complete for candle in history)

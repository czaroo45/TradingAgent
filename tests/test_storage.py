from tradingagent.models import Candle
from tradingagent.storage import CandleStore


def test_upsert_and_load_are_ordered_and_idempotent(tmp_path):
    store = CandleStore(str(tmp_path / "market.sqlite"))
    rows = [Candle(2000, 2, 3, 1, 2, 10), Candle(1000, 1, 2, .5, 1, 8)]
    assert store.upsert("x", "BTC/USDT", "1h", rows) == 2
    assert store.upsert("x", "BTC/USDT", "1h", rows) == 2
    assert [c.timestamp_ms for c in store.load("x", "BTC/USDT", "1h")] == [1000, 2000]


def test_candle_metadata_and_payload_records(tmp_path):
    import sqlite3

    path = str(tmp_path / "market.sqlite")
    store = CandleStore(path)
    store.upsert("x", "BTC/USDT", "1h", [Candle(1000, 1, 2, .5, 1, 3)], source="fixture")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT source,complete FROM candles").fetchone() == ("fixture", 1)
    record_id = store.save_payload("signals", "BTC/USDT", "1h", 1000, "analysis", {"side": "long"})
    assert record_id > 0
    assert store.save_ticker("x", "BTC/USDT", 1000, "ccxt", {"last": 1}) > 0
    assert store.record_market_data_run("x", "BTC/USDT", "1h", 1000, 2000,
                                        2000, "ccxt", True, 1, {"page_limit": 10},
                                        {"acceptable": True}) > 0


def test_incomplete_candles_are_excluded_by_default_and_source_is_retained(tmp_path):
    store = CandleStore(str(tmp_path / "market.sqlite"))
    rows = [Candle(1000, 1, 2, .5, 1, 3, "venue-a", True),
            Candle(2000, 1, 2, .5, 1, 3, "venue-a", False)]
    store.upsert("venue-a", "BTC/USDT", "5m", rows)
    assert [row.timestamp_ms for row in store.load("venue-a", "BTC/USDT", "5m")] == [1000]
    all_rows = store.load("venue-a", "BTC/USDT", "5m", include_incomplete=True)
    assert all_rows[-1].source == "venue-a"
    assert not all_rows[-1].complete


def test_legacy_candle_table_is_migrated_without_losing_rows(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy.sqlite")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE candles (
            exchange TEXT NOT NULL,symbol TEXT NOT NULL,timeframe TEXT NOT NULL,
            timestamp_ms INTEGER NOT NULL,open REAL NOT NULL,high REAL NOT NULL,
            low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,
            PRIMARY KEY(exchange,symbol,timeframe,timestamp_ms))""")
        db.execute("INSERT INTO candles VALUES ('x','BTC/USDT','1h',1000,1,2,.5,1,3)")
    store = CandleStore(path)
    row = store.load("x", "BTC/USDT", "1h")[0]
    assert row.timestamp_ms == 1000 and row.complete
    assert row.source == "ccxt"


def test_load_before_pages_older_rows_in_chronological_order(tmp_path):
    store = CandleStore(str(tmp_path / "market.db"))
    rows = [Candle(i * 60_000, 100, 101, 99, 100, 1) for i in range(8)]
    store.upsert("binance", "BTC/USDT", "1m", rows)
    page = store.load_before("binance", "BTC/USDT", "1m", 6 * 60_000, 3)
    assert [c.timestamp_ms for c in page] == [3 * 60_000, 4 * 60_000, 5 * 60_000]
    assert store.load_before("binance", "BTC/USDT", "1m", 0, 3) == []

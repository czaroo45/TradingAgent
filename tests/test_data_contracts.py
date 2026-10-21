"""Compatibility tests for signal, position, and execution data contracts."""
from dataclasses import asdict
from datetime import datetime, timezone
import json

import pytest

from tradingagent.models import ScoreBreakdown, StrategySignal
from tradingagent.paper_trading.engine import PaperFill, PaperPosition, PaperTradingEngine


def legacy_signal(direction="LONG"):
    # Keep the historical positional constructor intact.
    score = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1, 100)
    return StrategySignal("BTC/USDT", "binance", datetime(2025, 1, 1, tzinfo=timezone.utc),
        "fixture", direction, "conditions_met", (99, 101), "calculated", 98,
        "calculated", 98, (104,), 2, 10, 2, ("4h", "15m"), (), (), (), 90, score)


@pytest.mark.parametrize("side", ("LONG", "SHORT"))
def test_strategy_signal_separates_entry_action_from_position_side(side):
    signal = legacy_signal(side)
    assert signal.direction == side
    assert signal.action == "ENTRY"
    assert signal.position_side == side
    assert json.loads(json.dumps(asdict(signal), default=str))["position_side"] == side


def test_exit_and_no_trade_are_actions_not_position_sides():
    exit_signal = legacy_signal("EXIT")
    no_trade = legacy_signal("NO TRADE")
    assert exit_signal.action == "EXIT" and exit_signal.position_side is None
    assert no_trade.action == "NO_TRADE" and no_trade.position_side is None


@pytest.mark.parametrize("kwargs", (
    {"direction": "SIDEWAYS"},
    {"direction": "LONG", "action": "EXIT"},
    {"direction": "NO TRADE", "position_side": "SHORT"},
    {"direction": "SHORT", "position_side": "LONG"},
))
def test_signal_contract_rejects_invalid_or_conflicting_values(kwargs):
    base = legacy_signal()
    fields = asdict(base)
    fields.update(kwargs)
    with pytest.raises(ValueError):
        StrategySignal(**fields)


def test_paper_position_short_payload_roundtrip_and_legacy_long_default(tmp_path):
    engine = PaperTradingEngine(str(tmp_path / "paper.sqlite"), "contracts", 1000)
    short = PaperPosition("BTC/USDT", 1, 100, 110, (80,), 0, 0, 1,
                          "binance", "15m", "fixture", "bearish", "SHORT")
    restored = engine._position(engine._position_payload(short))
    assert restored == short

    legacy_payload = json.dumps({key: value for key, value in asdict(short).items()
                                  if key != "position_side"})
    assert engine._position(legacy_payload).position_side == "LONG"


def test_paper_position_and_fill_validate_side_and_execution_role():
    with pytest.raises(ValueError, match="position side"):
        PaperPosition("BTC/USDT", 1, 100, 90, (120,), 0, 0, 1,
                      "binance", "15m", "fixture", position_side="BUY")
    with pytest.raises(ValueError, match="execution side"):
        PaperFill(1, "BTC/USDT", "LONG", 1, 100, 0, 0, 0, 1, "test", 0)
    with pytest.raises(ValueError, match="execution role"):
        PaperFill(1, "BTC/USDT", "buy", 1, 100, 0, 0, 0, 1, "test", 0,
                  "SHORT", "OPEN")


def test_legacy_roundtrip_rows_survive_additive_metadata_migration(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy-paper.sqlite")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE paper_positions (account_id TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_id,symbol))")
        db.execute("INSERT INTO paper_positions VALUES ('legacy','ETH/USDT',?)", (json.dumps({
            "symbol":"ETH/USDT", "quantity":2, "average_entry":100, "stop_loss":90,
            "targets":[120], "next_target":0, "entry_fees":1, "opened_at_ms":10,
            "source":"binance", "timeframe":"15m", "strategy":"old", "regime":"bullish"
        }),))
        db.execute("""CREATE TABLE paper_roundtrips (
            id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
            strategy TEXT NOT NULL, regime TEXT NOT NULL, quantity REAL NOT NULL,
            opened_at_ms INTEGER NOT NULL, closed_at_ms INTEGER NOT NULL,
            net_pnl REAL NOT NULL, fees REAL NOT NULL, slippage_cost REAL NOT NULL,
            reason TEXT NOT NULL)""")
        db.execute("INSERT INTO paper_roundtrips VALUES (1,'legacy','BTC/USDT','old','bullish',1,10,20,5,1,1,'target')")
        db.execute("""CREATE TABLE paper_fills (
            id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, symbol TEXT NOT NULL,
            side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
            fee REAL NOT NULL, slippage_bps REAL NOT NULL, timestamp_ms INTEGER NOT NULL,
            reason TEXT NOT NULL, realized_pnl REAL NOT NULL, payload TEXT NOT NULL)""")
        db.execute("INSERT INTO paper_fills VALUES (1,'legacy','ETH/USDT','buy',2,100,1,0,10,'signal_entry',0,'{}')")

    engine = PaperTradingEngine(path, "legacy", 1000)
    assert engine.position("ETH/USDT").position_side == "LONG"
    rows = engine.roundtrips()
    assert len(rows) == 1
    assert rows[0]["net_pnl"] == 5
    assert rows[0]["position_side"] == "LONG"
    legacy_fill = engine.fills()[0]
    assert legacy_fill.position_side == "LONG" and legacy_fill.role == "ENTRY"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM paper_roundtrips").fetchone()[0] == 1

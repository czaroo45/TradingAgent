from tradingagent.config import Settings
from tradingagent.models import Candle, ScoreBreakdown, StrategySignal
from tradingagent.paper_trading import PaperTradingEngine, RiskManager, run_paper_cycle


class EligibleStrategy:
    def __init__(self, config):
        self.config = config

    def evaluate(self, symbol, source, candles, analyses, qualities):
        entry_tf = self.config.entry_timeframe
        higher_tf = self.config.higher_timeframe
        closed = candles[entry_tf][-1]
        price = closed.close
        score = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1, 100)
        return (StrategySignal(symbol, source, closed.timestamp, "fixture", "LONG",
            "conditions_met", (price - 1, price + 1), "calculated", price - 5,
            "calculated", price - 5, (price + 12, price + 18), 2.0, 30.0, 5.0,
            (higher_tf, entry_tf), (), ("controlled eligible setup",), (), 95.0, score),)


def _histories(now_ms, *, gap=False):
    entry_ms, higher_ms = 900_000, 14_400_000
    entry_closed_ts = (now_ms // entry_ms) * entry_ms - entry_ms
    higher_closed_ts = (now_ms // higher_ms) * higher_ms - higher_ms
    entry = []
    for index in range(60):
        if gap and index == 40:
            continue
        stamp = entry_closed_ts - (59 - index) * entry_ms
        price = 100 + index * 0.01
        entry.append(Candle(stamp, price, price + 1, price - 1,
            price + 0.05, 50_000, "binance", True))
    higher = []
    for index in range(60):
        stamp = higher_closed_ts - (59 - index) * higher_ms
        price = 100 + index * 0.02
        higher.append(Candle(stamp, price, price + 1, price - 1,
            price + 0.05, 50_000, "binance", True))
    if not gap:
        latest = entry[-1]
        entry.append(Candle(latest.timestamp_ms + entry_ms, latest.close,
            latest.close + 1, latest.close - 1, latest.close + 0.05,
            50_000, "binance", False))
    return {"15m": entry, "4h": higher}


def test_new_eligible_closed_candle_reaches_paper_fill(tmp_path, monkeypatch):
    import tradingagent.paper_trading.runner as runner

    monkeypatch.setattr(runner, "StrategyEngine", EligibleStrategy)
    settings = Settings(db_path=str(tmp_path / "runner-open.db"),
        timeframes=("15m", "4h"), strategy_entry_timeframe="15m",
        strategy_higher_timeframe="4h", fee_bps=1, slippage_bps=1,
        paper_spread_bps=0, min_risk_reward=1.2,
        max_transaction_cost_bps=100)
    now_ms = (1_700_000_000_000 // 900_000) * 900_000 + 30_000
    engine = PaperTradingEngine(settings.db_path, "runner-test", 10_000, "USDT",
        fee_bps=settings.fee_bps, slippage_bps=settings.slippage_bps, spread_bps=0)
    cycle = run_paper_cycle(symbol="BTC/USDT", exchange="binance",
        histories=_histories(now_ms), settings=settings, engine=engine,
        risk_manager=RiskManager.from_settings(settings), amount_step=0.001,
        now_ms=now_ms)
    assert cycle["entry_fill"] is not None, (cycle["issues"], cycle["entry_results"],
                                              {key: value.reasons for key, value in cycle["data_quality"].items()})
    assert cycle["entry_results"][0]["status"] == "opened"
    assert engine.position("BTC/USDT") is not None
    assert engine.position("BTC/USDT").position_side == "LONG"
    assert len(engine.fills()) == 1
    assert engine.fills()[0].role == "ENTRY"


def test_unhealthy_candle_history_blocks_candidate_before_execution(tmp_path, monkeypatch):
    import tradingagent.paper_trading.runner as runner

    monkeypatch.setattr(runner, "StrategyEngine", EligibleStrategy)
    settings = Settings(db_path=str(tmp_path / "runner-reject.db"),
        timeframes=("15m", "4h"), strategy_entry_timeframe="15m",
        strategy_higher_timeframe="4h")
    now_ms = (1_700_000_000_000 // 900_000) * 900_000 + 30_000
    engine = PaperTradingEngine(settings.db_path, "runner-test", 10_000, "USDT")
    cycle = run_paper_cycle(symbol="BTC/USDT", exchange="binance",
        histories=_histories(now_ms, gap=True), settings=settings, engine=engine,
        risk_manager=RiskManager.from_settings(settings), amount_step=0.001,
        now_ms=now_ms)
    assert cycle["entry_fill"] is None
    assert cycle["entry_results"][0]["status"] == "rejected"
    assert cycle["entry_results"][0]["reason"] == "entry blocked: unhealthy data quality"
    assert engine.position("BTC/USDT") is None
    assert not engine.fills()

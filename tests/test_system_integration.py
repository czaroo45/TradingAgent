"""Offline orchestration test: stored closed candles through rules and paper persistence."""
from datetime import datetime, timezone

from tradingagent.config import Settings
from tradingagent.paper_trading import PaperTradingEngine, RiskManager, run_paper_cycle
from tradingagent.storage import CandleStore
from tradingagent.models import Candle


def test_unambiguous_offline_pipeline_emits_no_trade_without_fabricating_fill(tmp_path):
    settings=Settings(db_path=str(tmp_path/"integration.db"),timeframes=("15m","4h"),
        strategy_entry_timeframe="15m",strategy_higher_timeframe="4h")
    store=CandleStore(settings.db_path)
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    entry_end=(now//900_000)*900_000-900_000
    higher_end=(now//14_400_000)*14_400_000-14_400_000
    entry=[Candle(entry_end-i*900_000,100,101,99,100,100,"binance",True) for i in range(59,-1,-1)]
    higher=[Candle(higher_end-i*14_400_000,100,101,99,100,100,"binance",True) for i in range(59,-1,-1)]
    # CLI/API persist provider provenance as "ccxt", distinct from venue.
    store.upsert("binance","BTC/USDT","15m",entry,source="ccxt")
    store.upsert("binance","BTC/USDT","4h",higher,source="ccxt")
    engine=PaperTradingEngine(settings.db_path,"offline",10000,"USDT")
    cycle=run_paper_cycle(symbol="BTC/USDT",exchange="binance",
        histories={"15m":store.load("binance","BTC/USDT","15m",None),
                   "4h":store.load("binance","BTC/USDT","4h",None)},
        settings=settings,engine=engine,risk_manager=RiskManager.from_settings(settings),
        amount_step=.0001,now_ms=now)
    assert cycle["signals"]
    assert all(signal.direction=="NO TRADE" for signal in cycle["signals"])
    assert {signal.candidate_direction for signal in cycle["signals"]} == {"LONG", "SHORT"}
    for strategy in settings.enabled_strategies:
        assert {signal.candidate_direction for signal in cycle["signals"]
                if signal.strategy==strategy} == {"LONG", "SHORT"}
    assert len(cycle["entry_results"]) == len(cycle["signals"])
    assert {result["status"] for result in cycle["entry_results"]} == {"no_setup"}
    assert cycle["entry_fill"] is None
    assert cycle["wait_reasons"]
    assert any("complete" in reason.lower() or "data" in reason.lower() for reason in cycle["wait_reasons"])
    assert engine.snapshot().cash==10000
    assert not engine.snapshot().positions
    assert engine.events()  # signal audit persisted; no fictional price/fill generated

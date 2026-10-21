from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from tradingagent.backtesting import (BacktestConfig, BacktestEngine,
    chronological_split)
from tradingagent.models import Analysis, Candle, Indicators, ScoreBreakdown, StrategySignal


BASE=1_700_000_000_000


def history(gap_at=None, source="binance"):
    entry=[]
    for i in range(880):
        if i==gap_at: continue
        ts=BASE+i*900_000
        entry.append(Candle(ts,100,101,99,100,100,source,True))
    higher=[Candle(BASE+i*14_400_000,100,101,99,100,100,source,True)
            for i in range(55)]
    return {"15m":entry,"4h":higher}


def analysis(symbol,timeframe,candles,*args,**kwargs):
    return Analysis(symbol,timeframe,"bullish",70,(),Indicators(101,100,55,1,
        candles[-1].close,100),None,candles[-1].timestamp)


class StubStrategyEngine:
    def __init__(self,config): pass
    def evaluate(self,symbol,source,candles,analyses,qualities,*,now):
        row=candles["15m"][-1]
        assert row.timestamp_ms+900_000<=int(now.timestamp()*1000)
        assert candles["4h"][-1].timestamp_ms+14_400_000<=int(now.timestamp()*1000)
        sig=StrategySignal(symbol,source,row.timestamp,"stub","LONG","conditions_met",
            (99,101),"calculated",90,"calculated",90,(120,130),2,32,6,
            ("4h","15m"),(),("deterministic test fixture",),(),90,
            ScoreBreakdown(1,1,1,1,1,1,1,1,90))
        return (sig,)


def run_backtest(tmp_path, gap_at=None):
    data=history(gap_at)
    config=BacktestConfig("BTC/USDT","binance",start_ms=BASE,
        end_ms=BASE+880*900_000,initial_capital=1000,
        fee_bps_per_side=1,slippage_bps=1,spread_bps=2,
        max_volume_participation=.1)
    with patch("tradingagent.backtesting.StrategyEngine",StubStrategyEngine), \
         patch("tradingagent.backtesting.analyze",analysis):
        return BacktestEngine().run(data,config)


def test_backtest_uses_closed_bar_then_next_open_and_is_reproducible(tmp_path):
    first=run_backtest(tmp_path)
    second=run_backtest(tmp_path)
    assert first.run_id==second.run_id
    assert first.fills==second.fills
    buy=next(fill for fill in first.fills if fill["side"]=="buy")
    assert buy["timestamp_ms"]>first.signal_events[0]["timestamp_ms"]
    assert first.metrics["trade_count"]==1
    assert first.metrics["sample_size"]==1
    assert first.metrics["fees_paid"]>0
    assert first.metrics["slippage_cost_estimate"]>0
    assert first.metrics["spread_cost_estimate"]>0
    assert "buy_and_hold" in first.metrics["benchmarks"]
    assert first.provenance["same_bar_stop_and_target"].startswith("stop first")


def test_missing_bar_forces_exit_and_resets_warmup_without_fabricated_candle(tmp_path):
    result=run_backtest(tmp_path,gap_at=810)
    assert result.quality["15m"]["gaps"]
    assert any(fill["reason"]=="data_gap_exit" for fill in result.fills)
    assert len(result.fills)<10


def test_backtest_exports_json_csv_and_equity_drawdown_charts(tmp_path):
    result=run_backtest(tmp_path)
    result.export_json(str(tmp_path/"report.json"))
    result.export_csv(str(tmp_path/"trades.csv"))
    result.export_fills_csv(str(tmp_path/"fills.csv"))
    equity,drawdown=result.export_charts(str(tmp_path/"charts"))
    assert '"sha256"' in (tmp_path/"report.json").read_text()
    assert (tmp_path/"trades.csv").is_file()
    assert (tmp_path/"fills.csv").is_file()
    assert "<svg" in open(equity,encoding="utf-8").read()
    assert "Drawdown" in open(drawdown,encoding="utf-8").read()


def test_chronological_split_is_ordered_and_test_is_disjoint():
    rows=[Candle(i,100,101,99,100,1,"binance",True) for i in range(100)]
    split=chronological_split(rows)
    assert max(x.timestamp_ms for x in split.design)<min(x.timestamp_ms for x in split.validation)
    assert max(x.timestamp_ms for x in split.validation)<min(x.timestamp_ms for x in split.final_test)
    assert len(split.final_test)>0
    with pytest.raises(ValueError):
        chronological_split(rows,.8,.3)


def test_backtest_rejects_same_timeframe_data_relabelled_as_higher_timeframe():
    data=history()
    config=BacktestConfig("BTC/USDT","binance",start_ms=BASE,
        end_ms=BASE+880*900_000,amount_step=1e-6)
    mislabeled={"15m":data["15m"],"4h":data["15m"]}
    with pytest.raises(ValueError,match="incompatible timeframe cadence"):
        BacktestEngine().run(mislabeled,config)


def test_backtest_accepts_ccxt_provider_provenance_and_keeps_it_in_report(tmp_path):
    data=history(source="ccxt")
    config=BacktestConfig("BTC/USDT","binance",start_ms=BASE,
        end_ms=BASE+880*900_000,initial_capital=1000,max_volume_participation=.1)
    with patch("tradingagent.backtesting.StrategyEngine",StubStrategyEngine), \
         patch("tradingagent.backtesting.analyze",analysis):
        result=BacktestEngine().run(data,config)
    assert result.provenance["exchange"]=="binance"
    assert result.provenance["timeframes"]["15m"]["source"]=="ccxt"


def test_backtest_rejects_mixed_provider_sources():
    data=history(source="ccxt")
    data["4h"][0]=Candle(data["4h"][0].timestamp_ms,100,101,99,100,100,"binance",True)
    config=BacktestConfig("BTC/USDT","binance")
    with pytest.raises(ValueError,match="one consistent data source"):
        BacktestEngine().run(data,config)


def test_comparison_runs_each_strategy_on_same_history_and_costs(tmp_path):
    engine=BacktestEngine()
    data=history()
    config=BacktestConfig("BTC/USDT","binance",start_ms=BASE,
        end_ms=BASE+880*900_000,initial_capital=1000,max_volume_participation=.1)
    with patch("tradingagent.backtesting.StrategyEngine",StubStrategyEngine), \
         patch("tradingagent.backtesting.analyze",analysis):
        results=engine.compare(data,config,("trend_following","momentum"))
    assert set(results)=={"trend_following","momentum"}
    assert results["trend_following"].run_id!=results["momentum"].run_id
    assert results["trend_following"].config["fee_bps_per_side"]==results["momentum"].config["fee_bps_per_side"]
    assert results["trend_following"].provenance["sha256"]==results["momentum"].provenance["sha256"]


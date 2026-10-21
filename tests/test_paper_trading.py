from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tradingagent.models import Candle, ScoreBreakdown, StrategySignal
from tradingagent.paper_trading import PaperTradeRejected, PaperTradingEngine
from tradingagent.risk import InstrumentConstraints, PortfolioSnapshot, RiskManager, RiskRequest


def make_signal(direction="LONG"):
    score=ScoreBreakdown(1,1,1,1,1,1,1,1,100)
    return StrategySignal("BTC/USDT","binance",datetime.fromtimestamp(0,timezone.utc),
        "fixture",direction,"conditions_met",(999,1001),"calculated",990,"calculated",990,
        (1030,1040),2,32,6,("4h","15m"),(),("fixture signal",),(),90,score)


def enter(engine):
    sig=make_signal()
    request=RiskRequest(sig,PortfolioSnapshot(10_000),InstrumentConstraints("USDT",amount_step=.1),
        0,True,fee_bps_round_trip=20,slippage_bps_entry=6,slippage_bps_stop=6)
    bar=Candle(900_000,1000,1001,999,1000,100,"binance",True)
    fill=engine.execute_entry(sig,RiskManager(),request,bar,.1,regime="bullish")
    return sig,fill


def test_signal_record_is_separate_from_fill_and_restart_restores_portfolio(tmp_path):
    path=str(tmp_path/"paper.sqlite")
    engine=PaperTradingEngine(path,"demo",10_000,fee_bps=10,slippage_bps=5,spread_bps=2)
    signal=make_signal()
    engine.record_signal(signal)
    assert engine.snapshot().positions == ()
    _,fill=enter(engine)
    assert fill.side=="buy" and fill.price>1000 and fill.fee>0 and fill.spread_bps==2
    restored=PaperTradingEngine(path,"demo",10_000,fee_bps=10,slippage_bps=5,spread_bps=2)
    assert restored.position("BTC/USDT").quantity==fill.quantity
    assert restored.position("BTC/USDT").position_side=="LONG"
    assert len(restored.fills())==1
    assert restored.fills()[0].spread_bps==2
    assert restored.fills()[0].side=="buy"
    assert restored.fills()[0].position_side=="LONG"
    assert restored.fills()[0].role=="ENTRY"
    assert [e["kind"] for e in restored.events()]==["signal","entry"]


def test_paper_events_can_be_read_incrementally_without_duplicates(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper-events.sqlite"),"demo",10_000)
    engine.record_signal(make_signal())
    first_id=engine.latest_event_id()
    assert first_id==1
    _,fill=enter(engine)
    added=engine.events_after(first_id,limit=10)
    assert len(added)==1
    assert added[0]["id"]==first_id+1
    assert added[0]["kind"]=="entry"
    assert engine.events_after(engine.latest_event_id())==()
    assert engine.latest_event()["timestamp_ms"]==added[-1]["timestamp_ms"]
    assert fill.fill_id>0


def test_incremental_paper_event_cursor_is_account_scoped(tmp_path):
    path=str(tmp_path/"paper-events.sqlite")
    first=PaperTradingEngine(path,"first",10_000)
    second=PaperTradingEngine(path,"second",10_000)
    first.record_signal(make_signal())
    assert first.latest_event_id()==1
    assert second.latest_event_id()==0
    assert second.events_after(0)==()


@pytest.mark.parametrize(("event_id","limit"), [(-1,1),(0,0)])
def test_incremental_paper_events_validate_cursor_and_limit(tmp_path,event_id,limit):
    engine=PaperTradingEngine(str(tmp_path/"paper-events.sqlite"),"demo",10_000)
    with pytest.raises(ValueError,match="event cursor"):
        engine.events_after(event_id,limit)


def test_next_open_outside_entry_zone_and_same_candle_fill_are_rejected(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper.sqlite"),"demo",10_000)
    sig=make_signal()
    req=RiskRequest(sig,PortfolioSnapshot(10_000),InstrumentConstraints("USDT"),0,True)
    with pytest.raises(PaperTradeRejected,match="same-bar"):
        engine.execute_entry(sig,RiskManager(),req,Candle(0,1000,1001,999,1000,1,"binance"),1e-8)
    with pytest.raises(PaperTradeRejected,match="outside"):
        engine.execute_entry(sig,RiskManager(),req,Candle(900_000,1010,1011,1009,1010,1,"binance"),1e-8)


def test_open_position_prevents_duplicate_entry_for_same_symbol(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"duplicate.sqlite"),"demo",10_000)
    signal,entry=enter(engine)
    request=RiskRequest(signal,PortfolioSnapshot(10_000),
        InstrumentConstraints("USDT",amount_step=.1),0,True,
        fee_bps_round_trip=20,slippage_bps_entry=6,slippage_bps_stop=6)
    bar=Candle(900_000,1000,1001,999,1000,100,"binance",True)
    with pytest.raises(PaperTradeRejected,match="already has an open paper position"):
        engine.execute_entry(signal,RiskManager(),request,bar,.1)
    assert engine.position("BTC/USDT").quantity == entry.quantity


def test_conservative_stop_wins_when_bar_also_touches_take_profit(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper.sqlite"),"demo",10_000,slippage_bps=10)
    _,entry=enter(engine)
    bar=Candle(1_800_000,1000,1040,980,1020,100,"binance",True)
    exits=engine.process_bar("BTC/USDT",bar,.1)
    assert len(exits)==1 and exits[0].reason=="stop_loss"
    assert exits[0].price < 990
    assert engine.position("BTC/USDT") is None
    assert len(engine.roundtrips())==1
    assert engine.snapshot().realized_pnl<0


def test_partial_target_then_final_target_closes_position_and_accounts_costs(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper.sqlite"),"demo",10_000)
    _,entry=enter(engine)
    first=engine.process_bar("BTC/USDT",Candle(1_800_000,1000,1035,995,1020,100,"binance",True),.1)
    assert first[0].reason=="take_profit_1"
    assert first[0].side=="sell" and first[0].position_side=="LONG" and first[0].role=="EXIT"
    assert engine.process_bar("BTC/USDT",Candle(1_800_000,1000,1040,995,1020,100,"binance",True),.1)==()
    remaining=engine.position("BTC/USDT")
    assert remaining is not None and remaining.quantity<entry.quantity
    second=engine.process_bar("BTC/USDT",Candle(2_700_000,1030,1050,1020,1040,100,"binance",True),.1)
    assert second[0].reason=="take_profit_2"
    assert engine.position("BTC/USDT") is None
    assert engine.roundtrips()[0]["net_pnl"]>0
    assert engine.roundtrips()[0]["position_side"]=="LONG"
    assert engine.snapshot().fees_paid>0


def test_manual_partial_close_uses_adverse_execution_costs(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper.sqlite"),"demo",10_000)
    _,entry=enter(engine)
    fill=engine.close_position("BTC/USDT",entry.quantity/2,1010,1_800_000,"manual_partial",.1)
    assert fill.side=="sell" and fill.price<1010
    assert engine.position("BTC/USDT").quantity<entry.quantity
    assert fill.fee>0


def test_short_signal_can_open_and_close_a_short_position(tmp_path):
    engine=PaperTradingEngine(str(tmp_path/"paper.sqlite"),"demo",10_000)
    short=StrategySignal("ETH/USDT","binance",datetime.fromtimestamp(900,timezone.utc),
        "fixture","SHORT","conditions_met",(990,1010),"calculated",1015,"calculated",
        1015,(950,920),2.5,30,5,("4h","15m"),(),("short setup",),(),90,
        ScoreBreakdown(1,1,1,1,1,1,1,1,96))
    request=RiskRequest(short,PortfolioSnapshot(10_000),InstrumentConstraints("USDT",amount_step=.1),
        0,True,fee_bps_round_trip=20,slippage_bps_entry=6,slippage_bps_stop=6)
    bar=Candle(1_800_000,1000,1002,998,1000,100,"binance",True)
    fill=engine.execute_entry(short,RiskManager(),request,bar,.1,regime="bearish")
    assert fill.side=="sell" and fill.position_side=="SHORT"
    assert engine.position("ETH/USDT").position_side == "SHORT"
    close=engine.close_position("ETH/USDT",fill.quantity,995,2_700_000,"short_target",.1)
    assert close.side=="buy" and close.position_side=="SHORT"
    assert engine.position("ETH/USDT") is None
    assert close.realized_pnl > 0


@pytest.mark.parametrize(("exit_price", "expected_per_unit"), [(96.0, 4.0), (102.0, -2.0)])
def test_short_snapshot_and_realized_pnl_use_short_price_direction(tmp_path, exit_price, expected_per_unit):
    engine=PaperTradingEngine(str(tmp_path/"short-pnl.sqlite"),"demo",10_000,
                              fee_bps=0,slippage_bps=0,spread_bps=0)
    short=StrategySignal("BTC/USDT","binance",datetime.fromtimestamp(0,timezone.utc),
        "fixture","SHORT","conditions_met",(99,101),"calculated",102,"calculated",
        102,(96,94),2,32,6,("15m",),(),("short setup",),(),90,
        ScoreBreakdown(1,1,1,1,1,1,1,1,90))
    request=RiskRequest(short,PortfolioSnapshot(10_000),
        InstrumentConstraints("USDT",amount_step=.1),0,True,
        fee_bps_round_trip=0,slippage_bps_entry=0,slippage_bps_stop=0)
    entry=engine.execute_entry(short,RiskManager(),request,
        Candle(900_000,100,100.5,99.5,100,1000,"binance",True),.1)
    quantity=entry.quantity
    marked_price=96 if expected_per_unit > 0 else 102
    snapshot=engine.snapshot({"BTC/USDT":marked_price})
    assert snapshot.unrealized_pnl == pytest.approx(quantity*expected_per_unit)
    assert snapshot.equity == pytest.approx(10_000+quantity*expected_per_unit)
    close=engine.close_position("BTC/USDT",quantity,marked_price,1_800_000,"fixture_exit",.1)
    assert close.realized_pnl == pytest.approx(quantity*expected_per_unit)
    assert engine.snapshot().realized_pnl == pytest.approx(quantity*expected_per_unit)


@pytest.mark.parametrize(("bar", "expected_reason"), [
    (Candle(1_800_000,100,103,99,102,1000,"binance",True), "stop_loss"),
    (Candle(1_800_000,100,101,95,98,1000,"binance",True), "take_profit_1"),
])
def test_short_stop_and_target_close_with_buy_fills(tmp_path, bar, expected_reason):
    engine=PaperTradingEngine(str(tmp_path/f"short-{expected_reason}.sqlite"),"demo",10_000,
                              fee_bps=0,slippage_bps=0,spread_bps=0)
    short=StrategySignal("BTC/USDT","binance",datetime.fromtimestamp(0,timezone.utc),
        "fixture","SHORT","conditions_met",(99,101),"calculated",102,"calculated",
        102,(96,94),2,32,6,("15m",),(),("short setup",),(),90,
        ScoreBreakdown(1,1,1,1,1,1,1,1,90))
    request=RiskRequest(short,PortfolioSnapshot(10_000),
        InstrumentConstraints("USDT",amount_step=.1),0,True,
        fee_bps_round_trip=0,slippage_bps_entry=0,slippage_bps_stop=0)
    engine.execute_entry(short,RiskManager(),request,
        Candle(900_000,100,100.5,99.5,100,1000,"binance",True),.1)
    exits=engine.process_bar("BTC/USDT",bar,.1)
    assert exits and exits[0].reason == expected_reason
    assert exits[0].side == "buy" and exits[0].position_side == "SHORT"


def test_reset_clears_only_the_selected_paper_account(tmp_path):
    path = str(tmp_path / "paper.db")
    first = PaperTradingEngine(path, "first", 5000, "USDT")
    second = PaperTradingEngine(path, "second", 7000, "USDT")
    first.record_signal(make_signal())
    second.record_signal(make_signal())
    first.reset(initial_capital=6000)
    assert first.snapshot().cash == 6000
    assert first.events() == ()
    assert len(first.equity_curve()) == 1
    assert len(second.events()) == 1
    assert second.snapshot().cash == 7000


def test_closed_market_event_checkpoint_is_durable_and_account_scoped(tmp_path):
    path=str(tmp_path/"paper-events.sqlite")
    first=PaperTradingEngine(path,"first",5000,"USDT")
    second=PaperTradingEngine(path,"second",5000,"USDT")
    assert not first.market_event_processed("BTC/USDT","15m",900_000)
    assert first.mark_market_event_processed("BTC/USDT","15m",900_000,901_000)
    assert not first.mark_market_event_processed("BTC/USDT","15m",900_000,902_000)
    restored=PaperTradingEngine(path,"first",5000,"USDT")
    assert restored.market_event_processed("BTC/USDT","15m",900_000)
    assert not restored.market_event_processed("BTC/USDT","15m",1_800_000)
    assert restored.mark_market_event_processed("BTC/USDT","15m",1_800_000,1_801_000)
    assert not second.market_event_processed("BTC/USDT","15m",900_000)


from datetime import datetime, timezone
import math
import pytest
from unittest.mock import patch

from tradingagent.models import Analysis, Candle, DataQuality, Indicators
from tradingagent.scoring import ScoreWeights
from tradingagent.strategies import StrategyConfig, StrategyEngine
from tradingagent.strategies.engine import _confirmed_breakout_level


def fixture_inputs(count=80):
    rows = []
    price = 100.0
    for i in range(count):
        price += 0.25
        rows.append(Candle(i * 900_000, price - .1, price + .2, price - .2,
                           price, 100.0, "binance", True))
    analysis = Analysis("BTC/USDT", "15m", "bullish", 70, (),
                        Indicators(119, 117, 60, 2, price, 100), None,
                        rows[-1].timestamp)
    return {"15m": rows, "4h": rows}, {"15m": analysis,
                                       "4h": Analysis("BTC/USDT", "4h", "bullish", 70, (),
                                                      analysis.indicators, None, rows[-1].timestamp)}


def valid_quality(candles):
    checked_at = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {tf: DataQuality("binance", "BTC/USDT", tf, checked_at, len(rows))
            for tf, rows in candles.items()}


def test_trend_following_can_emit_calculated_long_scenario():
    candles, analyses = fixture_inputs()
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119, 117, 60, 2, candles["15m"][-1].close, 100)):
        signals = StrategyEngine(StrategyConfig(enabled=("trend_following",),
            entry_timeframe="15m", higher_timeframe="4h", fee_bps_per_side=1,
            slippage_bps_per_side=.5, spread_bps=2)).evaluate(
                "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    signal = signals[0]
    assert signal.direction == "LONG"
    assert signal.status == "conditions_met"
    assert signal.entry_zone_basis == signal.invalidation_basis == "calculated"
    assert signal.stop_loss < signal.entry_zone[0] < signal.targets[0]
    assert signal.score.total == signal.confidence
    assert {item.candidate_direction for item in signals} == {"LONG", "SHORT"}
    assert sum(item.candidate_direction == "SHORT" for item in signals) == 1
    assert next(item for item in signals if item.candidate_direction == "SHORT").direction == "NO TRADE"


def test_incomplete_provider_tail_does_not_zero_scores_when_strategy_gets_closed_bars_only():
    """Reproduce live CLI: q sees a forming 4h tail, strategy input excludes it."""
    from dataclasses import replace
    candles, analyses = fixture_inputs()
    reports=valid_quality(candles)
    reports["4h"]=replace(reports["4h"],incomplete_latest=True)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119,117,60,2,candles["15m"][-1].close,100)):
        signals=StrategyEngine(StrategyConfig(enabled=(
            "trend_following","pullback","breakout_retest","momentum"),
            fee_bps_per_side=1,slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,reports)
    assert {signal.strategy for signal in signals}=={
        "trend_following","pullback","breakout_retest","momentum"}
    assert all(signal.confidence > 0 for signal in signals)
    assert all(not signal.missing_data for signal in signals)
    assert signals[0].direction=="LONG"


@pytest.mark.parametrize("strategy",("trend_following","pullback","breakout_retest","momentum"))
def test_each_enabled_strategy_can_emit_only_when_its_entry_conditions_are_met(strategy):
    candles,analyses=fixture_inputs()
    rows=candles["15m"]
    previous=rows[-2]
    rows[-2]=Candle(previous.timestamp_ms,previous.open,previous.high,previous.low,
                    previous.close,150,previous.source,True)
    current=rows[-1]
    rows[-1]=Candle(current.timestamp_ms,119.85,120.05,119.8,120.0,150,current.source,True)
    def controlled_ema(values,period):
        if period==20:return [rows[-1].close]*len(values)
        if period==12:return [float(index) for index in range(len(values))]
        if period==26:return [0.0]*len(values)
        if period==9:return [0.0]*len(values)
        raise AssertionError(f"unexpected EMA period {period}")
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119,117,60,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=controlled_ema):
        signal=StrategyEngine(StrategyConfig(enabled=(strategy,),fee_bps_per_side=1,
            slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction=="LONG"
    assert signal.status=="conditions_met"
    assert signal.confidence>0
    assert signal.action == "ENTRY"
    assert signal.position_side == "LONG"
    assert signal.stop_loss < signal.entry_zone[0]
    assert all(target > signal.entry_zone[1] for target in signal.targets)
    assert signal.risk_reward is not None and signal.risk_reward > 0


def test_insufficient_data_is_zero_scored_and_explains_the_blocker():
    candles,analyses=fixture_inputs(count=40)
    signals=StrategyEngine(StrategyConfig(enabled=("trend_following",))).evaluate(
        "BTC/USDT","binance",candles,analyses,valid_quality(candles))
    assert signals[0].direction=="NO TRADE"
    assert signals[0].confidence==0
    assert any("at least 50 candles" in reason for reason in signals[0].rejection_reasons)


def test_incomplete_candle_blocks_all_enabled_strategies():
    candles, analyses = fixture_inputs()
    last = candles["15m"][-1]
    candles["15m"][-1] = Candle(last.timestamp_ms, last.open, last.high, last.low,
                               last.close, last.volume, last.source, False)
    signals = StrategyEngine().evaluate("BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert all(s.direction == "NO TRADE" for s in signals)
    assert all(any("incomplete" in item for item in s.missing_data) for s in signals)


def test_stale_quality_and_mixed_sources_block_entries():
    candles, analyses = fixture_inputs()
    stale = DataQuality("binance", "BTC/USDT", "15m", 0, 80, stale=True, stale_by_ms=5000)
    reports = valid_quality(candles)
    reports["15m"] = stale
    signals = StrategyEngine().evaluate("BTC/USDT", "binance", candles, analyses, reports)
    assert all(s.direction == "NO TRADE" for s in signals)
    candles["15m"][-1] = Candle(candles["15m"][-1].timestamp_ms, 119, 120, 118,
                               119.5, 100, "other-exchange")
    signals = StrategyEngine().evaluate("BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert all(s.direction == "NO TRADE" for s in signals)
    assert any("source mismatch" in item for s in signals for item in s.missing_data)


def test_pullback_ema_touch_alone_does_not_trigger():
    candles, analyses = fixture_inputs()
    last = candles["15m"][-1]
    candles["15m"][-1] = Candle(last.timestamp_ms, 120, 120.1, 117.8, 119.8, 100, "binance")
    signals = StrategyEngine(StrategyConfig(enabled=("pullback",))).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "NO TRADE"
    assert "EMA contact alone is insufficient" in signals[0].explanation[0]


def test_mean_reversion_is_gated_by_sideways_regime():
    candles, analyses = fixture_inputs()
    signals = StrategyEngine(StrategyConfig(enabled=("mean_reversion",))).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "NO TRADE"
    assert "sideways regime" in signals[0].explanation[0]


def test_breakout_requires_prior_volume_confirmed_close_and_retest():
    candles, analyses = fixture_inputs()
    signals = StrategyEngine(StrategyConfig(enabled=("breakout_retest",))).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "NO TRADE"
    assert "previous volume-confirmed close" in signals[0].explanation[0]


def test_breakout_level_uses_only_bars_before_confirmed_break_and_excludes_latest_bar():
    rows=[Candle(i*900_000,10,11,9,10,100,"binance",True) for i in range(25)]
    rows[21]=Candle(21*900_000,11,12,10.8,11.5,200,"binance",True)
    assert _confirmed_breakout_level(rows,1.3)==11
    # If the breakout is only the most recent bar, it is not a prior confirmed break.
    rows[21]=Candle(21*900_000,10,11,9,10,100,"binance",True)
    rows[-1]=Candle(rows[-1].timestamp_ms,11,12,10.8,11.5,200,"binance",True)
    assert _confirmed_breakout_level(rows,1.3) is None


def test_conflicting_timeframe_regimes_veto_entry():
    candles, analyses = fixture_inputs()
    from dataclasses import replace
    analyses["15m"] = replace(analyses["15m"], regime="bearish")
    signals = StrategyEngine(StrategyConfig(enabled=("trend_following",))).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "NO TRADE"


def test_aggressive_mode_allows_entry_when_regimes_conflict():
    candles, analyses = fixture_inputs()
    from dataclasses import replace
    analyses["4h"] = replace(analyses["4h"], regime="bearish")
    analyses["15m"] = replace(analyses["15m"], regime="bullish")
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119, 117, 60, 2, candles["15m"][-1].close, 100)):
        signals = StrategyEngine(StrategyConfig(enabled=("trend_following",), aggressive_mode=True)).evaluate(
            "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "LONG"
    assert signals[0].status == "conditions_met"


def test_momentum_rejects_weak_relative_volume():
    candles, analyses = fixture_inputs()
    signals = StrategyEngine(StrategyConfig(enabled=("momentum",))).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles))
    assert signals[0].direction == "NO TRADE"
    assert "relative volume ≥1.2" in signals[0].explanation[0]


def test_costs_can_reject_an_otherwise_valid_entry():
    candles, analyses = fixture_inputs()
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119, 117, 60, 2, candles["15m"][-1].close, 100)):
        signal = StrategyEngine(StrategyConfig(enabled=("trend_following",),
            spread_bps=300, min_net_risk_reward=1.5)).evaluate(
                "BTC/USDT", "binance", candles, analyses, valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert "costs" in signal.explanation[0]
    assert any("reward/risk" in reason for reason in signal.rejection_reasons)


def test_atr_risk_limit_rejects_entry_with_specific_reason():
    candles,analyses=fixture_inputs()
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(119,117,60,2,candles["15m"][-1].close,100)):
        signal=StrategyEngine(StrategyConfig(enabled=("trend_following",),
            max_risk_atr=.1,fee_bps_per_side=1,slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction=="NO TRADE"
    assert any("ATR risk" in reason for reason in signal.rejection_reasons)


def test_disabled_strategies_are_not_evaluated_and_weights_are_configurable():
    candles, analyses = fixture_inputs()
    assert StrategyEngine(StrategyConfig(enabled=())).evaluate(
        "BTC/USDT", "binance", candles, analyses, valid_quality(candles)) == ()
    weights = ScoreWeights(trend_alignment=2, data_quality=3)
    assert weights.trend_alignment == 2
    try:
        ScoreWeights(trend_alignment=math.nan)
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite scoring weights must be rejected")


def _bearish_inputs(count=80):
    rows=[]
    price=120.0
    for i in range(count):
        price -= .2
        rows.append(Candle(i*900_000, price+.1, price+.3, price-.2,
                           price, 100.0, "binance", True))
    # A confirmed two-left/two-right pivot precedes the later pullback setups.
    pivot=50
    old=rows[pivot]
    rows[pivot]=Candle(old.timestamp_ms,109.9,112.0,109.7,109.8,
                       old.volume,old.source,True)
    a=Analysis("BTC/USDT","15m","bearish",75,(),
        Indicators(price,price+2,42,2,price,100),None,rows[-1].timestamp)
    h=Analysis("BTC/USDT","4h","bearish",75,(),a.indicators,None,rows[-1].timestamp)
    return {"15m":rows,"4h":rows},{"15m":a,"4h":h}


def _short_ema(values, period):
    if period == 20:
        return [100.0]*len(values)
    if period == 12:
        return [-index*index*.01 for index in range(len(values))]
    if period == 26:
        return [0.0]*len(values)
    if period == 9:
        return [0.0]*len(values)
    raise AssertionError(f"unexpected EMA period {period}")


def _audited_pullback_short(*, correction_high=110.0, with_swing=True):
    candles,analyses=_bearish_inputs()
    rows=candles["15m"]
    if with_swing:
        # A closed two-left/two-right pivot at exactly 110, with all four
        # neighboring highs lower. The pivot precedes the correction by bars.
        pivot_rows=(
            (48,109.8,109.9,109.6,109.7),
            (49,109.7,109.95,109.5,109.6),
            (50,109.8,110.0,109.4,109.7),
            (51,109.7,109.8,109.3,109.5),
            (52,109.5,109.7,109.2,109.4),
        )
        for index,op,hi,lo,close in pivot_rows:
            old=rows[index]
            rows[index]=Candle(old.timestamp_ms,op,hi,lo,close,100,"binance",True)
    else:
        old=rows[50]
        rows[50]=Candle(old.timestamp_ms,old.open,old.close+.3,old.low,old.close,
                        old.volume,"binance",True)
    for idx,op,hi,lo,close,volume in (
        (-3,108.5,108.9,108.3,108.8,100),
        (-2,108.8,correction_high,108.7,109.0,100),
        (-1,109.0,110.0,108.2,108.5,180),
    ):
        old=rows[idx]
        rows[idx]=Candle(old.timestamp_ms,op,hi,lo,close,volume,"binance",True)
    return candles,analyses


def _evaluate_audited_pullback(candles,analyses):
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(109.5,111,48,2,candles["15m"][-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",
               side_effect=lambda values,period: [109.5]*len(values) if period==20 else _short_ema(values,period)):
        return StrategyEngine(StrategyConfig(enabled=("pullback",),fee_bps_per_side=1,
            slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]


def test_pullback_short_rejects_a_correction_that_swept_prior_swing_high():
    candles,analyses=_audited_pullback_short(correction_high=110.2)
    signal=_evaluate_audited_pullback(candles,analyses)
    assert signal.direction == "NO TRADE"
    assert signal.action == "NO_TRADE" and signal.position_side is None
    assert any("pullback high 110.2 exceeded confirmed prior swing high 110" in reason
               for reason in signal.rejection_reasons)


def test_pullback_short_touching_swing_high_without_exceeding_it_remains_valid():
    candles,analyses=_audited_pullback_short(correction_high=110.0)
    signal=_evaluate_audited_pullback(candles,analyses)
    assert signal.direction == "SHORT"
    assert signal.action == "ENTRY" and signal.position_side == "SHORT"
    assert signal.stop_loss == pytest.approx(110.5)
    assert signal.stop_loss > signal.entry_zone[1]
    assert all(target < signal.entry_zone[0] for target in signal.targets)


def test_pullback_short_rejects_when_history_has_no_confirmed_swing_high():
    candles,analyses=_audited_pullback_short(with_swing=False)
    signal=_evaluate_audited_pullback(candles,analyses)
    assert signal.direction == "NO TRADE"
    assert any("insufficient prior closed candles to establish a confirmed swing high" in reason
               for reason in signal.rejection_reasons)


@pytest.mark.parametrize("strategy",("trend_following","pullback","breakout_retest","momentum"))
def test_each_strategy_can_emit_directional_short_with_short_specific_setup(strategy):
    candles, analyses = _bearish_inputs()
    rows=candles["15m"]
    if strategy == "pullback":
        # An upward corrective candle reaches EMA20, followed by a bearish
        # rejection that closes below the prior candle.
        rows[-3]=Candle(rows[-3].timestamp_ms,101.0,101.2,100.8,101.0,100,"binance")
        rows[-2]=Candle(rows[-2].timestamp_ms,101.0,101.4,100.9,101.2,100,"binance")
        rows[-1]=Candle(rows[-1].timestamp_ms,101.0,101.1,99.5,99.8,180,"binance")
    elif strategy == "breakout_retest":
        # Prior 20-bar range is 99..101. Bar 77 closes below it on volume;
        # bar 79 retests the broken support from below and rejects it.
        for i in range(55,75):
            rows[i]=Candle(rows[i].timestamp_ms,100,101,99,100,100,"binance")
        rows[75]=Candle(rows[75].timestamp_ms,100,101,99,100,100,"binance")
        rows[76]=Candle(rows[76].timestamp_ms,100,101,99,100,100,"binance")
        rows[77]=Candle(rows[77].timestamp_ms,99,99.2,97.8,98.2,200,"binance")
        rows[78]=Candle(rows[78].timestamp_ms,98.2,98.4,98.0,98.3,100,"binance")
        rows[79]=Candle(rows[79].timestamp_ms,99.1,99.2,98.0,98.3,180,"binance")
    else:
        last=rows[-1]
        rows[-1]=Candle(last.timestamp_ms,last.close+.2,last.close+.3,
                        last.close-.5,last.close-.4,180,"binance")

    indicator_rsi=42 if strategy != "pullback" else 48
    ema_fn = _short_ema
    if strategy == "momentum":
        def ema_fn(values, period):
            if period == 20:
                return [106.0]*len(values)
            return _short_ema(values,period)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,indicator_rsi,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=ema_fn):
        signal=StrategyEngine(StrategyConfig(enabled=(strategy,),fee_bps_per_side=1,
            slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "SHORT", signal.rejection_reasons
    assert signal.action == "ENTRY"
    assert signal.position_side == "SHORT"
    assert signal.stop_loss > signal.entry_zone[1]
    assert all(target < signal.entry_zone[0] for target in signal.targets)
    assert signal.risk_reward is not None and signal.risk_reward > 0


def test_short_score_does_not_reward_bullish_macd_and_is_bounded():
    from tradingagent.scoring import directional_score_parts
    short_parts=directional_score_parts(trend_alignment=1,price_structure=1,
        macd_alignment=0,rsi_alignment=1,relative_volume=1.5,
        timeframe_alignment=1,risk_reward=1,data_quality=1,strategy_conditions=1)
    assert short_parts[2] == .5
    assert short_parts[3] == 1.0


def test_directional_scoring_rewards_only_momentum_aligned_with_candidate_side():
    from tradingagent.scoring import directional_score_parts
    common=dict(trend_alignment=1,price_structure=1,rsi_alignment=1,
        relative_volume=1,timeframe_alignment=1,risk_reward=1,
        data_quality=1,strategy_conditions=1)
    long_with_bullish_macd=directional_score_parts(macd_alignment=1,**common)
    long_with_bearish_macd=directional_score_parts(macd_alignment=0,**common)
    short_with_bearish_macd=directional_score_parts(macd_alignment=1,**common)
    short_with_bullish_macd=directional_score_parts(macd_alignment=0,**common)
    assert long_with_bullish_macd[2] > long_with_bearish_macd[2]
    assert short_with_bearish_macd[2] > short_with_bullish_macd[2]


def test_strategy_score_uses_macd_alignment_for_short_candidate():
    candles,analyses=_bearish_inputs()
    def bullish_histogram(values,period):
        if period==20:return [100.0]*len(values)
        if period==12:return [index*index*.01 for index in range(len(values))]
        if period in (26,9):return [0.0]*len(values)
        raise AssertionError(period)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,42,2,candles["15m"][-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=_short_ema):
        aligned=StrategyEngine(StrategyConfig(enabled=("trend_following",),
            fee_bps_per_side=1,slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,42,2,candles["15m"][-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=bullish_histogram):
        opposed=StrategyEngine(StrategyConfig(enabled=("trend_following",),
            fee_bps_per_side=1,slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert aligned.direction == opposed.direction == "SHORT"
    assert aligned.score.momentum > opposed.score.momentum


def _set_regime(analyses, timeframe, regime):
    from dataclasses import replace
    analyses[timeframe] = replace(analyses[timeframe], regime=regime)


def _range_breakdown(candles, *, break_volume=200, retest=True, invalidated=False,
                     prior_retest=False):
    rows=candles["15m"]
    for i in range(55,77):
        rows[i]=Candle(rows[i].timestamp_ms,100,101,99,100,100,"binance",True)
    rows[77]=Candle(rows[77].timestamp_ms,99,99.2,97.8,98.2,break_volume,"binance",True)
    if prior_retest:
        rows[78]=Candle(rows[78].timestamp_ms,99.1,99.2,98.0,98.3,120,"binance",True)
    elif invalidated:
        rows[78]=Candle(rows[78].timestamp_ms,98.2,100.2,98.0,99.2,120,"binance",True)
    else:
        rows[78]=Candle(rows[78].timestamp_ms,98.2,98.4,98.0,98.3,100,"binance",True)
    if retest:
        rows[79]=Candle(rows[79].timestamp_ms,99.1,99.2,98.0,98.3,180,"binance",True)
    else:
        rows[79]=Candle(rows[79].timestamp_ms,98.3,98.4,97.8,98.0,100,"binance",True)
    return candles


def test_trend_following_short_requires_structure_confirmation_and_no_trade_has_no_side():
    candles,analyses=_bearish_inputs()
    rows=candles["15m"]
    last=rows[-1]
    rows[-1]=Candle(last.timestamp_ms,last.close-.1,last.close+.5,last.low-.1,
                    last.close+.4,last.volume,"binance",True)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,42,2,rows[-1].close,100)):
        signal=StrategyEngine(StrategyConfig(enabled=("trend_following",))).evaluate(
            "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert signal.action == "NO_TRADE"
    assert signal.position_side is None
    assert any("lower-high / lower-low" in reason or "latest close did not fall" in reason
               for reason in signal.rejection_reasons)


@pytest.mark.parametrize("regimes",[("sideways","sideways"),("bearish","bullish")])
def test_trend_following_rejects_sideways_and_conflicting_regimes(regimes):
    candles,analyses=fixture_inputs()
    _set_regime(analyses,"4h",regimes[0])
    _set_regime(analyses,"15m",regimes[1])
    signal=StrategyEngine(StrategyConfig(enabled=("trend_following",))).evaluate(
        "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert signal.action == "NO_TRADE" and signal.position_side is None
    assert signal.rejection_reasons


def test_pullback_short_requires_a_prior_upward_corrective_move():
    candles,analyses=_bearish_inputs()
    rows=candles["15m"]
    # Three descending closes end near EMA: contact and bearish confirmation
    # are present, but the required prior upward correction is absent.
    for idx, close in zip((-3,-2,-1),(104.6,104.4,104.0)):
        old=rows[idx]
        rows[idx]=Candle(old.timestamp_ms,close+.1,close+.3,close-.2,close,150,"binance",True)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(105,107,48,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",
               side_effect=lambda values,period: [105.0]*len(values) if period==20 else _short_ema(values,period)):
        signal=StrategyEngine(StrategyConfig(enabled=("pullback",))).evaluate(
            "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert any("no preceding upward corrective move" in reason
               for reason in signal.rejection_reasons)


def test_valid_pullback_short_is_still_rejected_when_net_reward_risk_is_too_low():
    candles,analyses=_bearish_inputs()
    rows=candles["15m"]
    rows[-3]=Candle(rows[-3].timestamp_ms,101.0,101.2,100.8,101.0,100,"binance",True)
    rows[-2]=Candle(rows[-2].timestamp_ms,101.0,101.4,100.9,101.2,100,"binance",True)
    rows[-1]=Candle(rows[-1].timestamp_ms,101.0,101.1,99.5,99.8,180,"binance",True)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,48,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=_short_ema):
        signal=StrategyEngine(StrategyConfig(enabled=("pullback",),min_net_risk_reward=100,
            fee_bps_per_side=1,slippage_bps_per_side=.5,spread_bps=2)).evaluate(
                "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert any("net reward/risk" in reason for reason in signal.rejection_reasons)


@pytest.mark.parametrize("kwargs,expected",[
    ({"retest":False},"did not retest"),
    ({"invalidated":True},"closed back across"),
    ({"prior_retest":True},"already had a retest"),
    ({"break_volume":100},"no previous volume-confirmed"),
])
def test_breakout_short_rejects_missing_retest_invalidation_reuse_or_volume(kwargs,expected):
    candles,analyses=_bearish_inputs()
    _range_breakdown(candles,**kwargs)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,42,2,candles["15m"][-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=_short_ema):
        signal=StrategyEngine(StrategyConfig(enabled=("breakout_retest",))).evaluate(
            "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert signal.action == "NO_TRADE" and signal.position_side is None
    assert any(expected in reason for reason in signal.rejection_reasons)


def test_short_breakout_level_uses_prior_range_not_future_bars():
    from tradingagent.strategies.engine import _confirmed_breakout_event
    candles,analyses=_bearish_inputs()
    _range_breakdown(candles,retest=False)
    rows=candles["15m"]
    event=_confirmed_breakout_event(rows,1.3)
    assert event == ("SHORT",99,77)
    # Future bars with extreme lows cannot retroactively move the breakout range.
    rows[-1]=Candle(rows[-1].timestamp_ms,98,98.2,1,98,100,"binance",True)
    assert _confirmed_breakout_event(rows,1.3) == event


def test_short_momentum_rejects_bullish_macd_and_wrong_rsi():
    candles,analyses=_bearish_inputs()
    rows=candles["15m"]
    last=rows[-1]
    rows[-1]=Candle(last.timestamp_ms,last.close+.2,last.close+.3,
                    last.close-.5,last.close-.4,180,"binance",True)
    def bullish_macd(values,period):
        if period==20:return [106.0]*len(values)
        if period==12:return [index*index*.01 for index in range(len(values))]
        if period in (26,9):return [0.0]*len(values)
        raise AssertionError(period)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,42,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=bullish_macd):
        signal=StrategyEngine(StrategyConfig(enabled=("momentum",))).evaluate(
            "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert any("MACD histogram is not negative and falling" in reason
               for reason in signal.rejection_reasons)

    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(100,102,60,2,rows[-1].close,100)), \
         patch("tradingagent.strategies.engine._ema_series",side_effect=_short_ema):
        signal=StrategyEngine(StrategyConfig(enabled=("momentum",))).evaluate(
            "BTC/USDT","binance",candles,analyses,valid_quality(candles))[0]
    assert signal.direction == "NO TRADE"
    assert any("outside the SHORT momentum range" in reason
               for reason in signal.rejection_reasons)


def test_high_score_cannot_override_trend_structure_hard_gate():
    candles,analyses=fixture_inputs()
    rows=candles["15m"]
    for old in rows[-6:]:
        rows[rows.index(old)]=Candle(old.timestamp_ms,120,120.2,119.8,120,100,"binance",True)
    weights=ScoreWeights(trend_alignment=1,data_quality=1,
        price_structure=0,momentum=0,volume=0,timeframe_alignment=0,
        risk_reward=0,strategy_conditions=0)
    with patch("tradingagent.strategies.engine.calculate_indicators",
               return_value=Indicators(120,119,60,2,120,100)):
        signal=StrategyEngine(StrategyConfig(enabled=("trend_following",),
            weights=weights)).evaluate("BTC/USDT","binance",candles,analyses,
                                       valid_quality(candles))[0]
    assert signal.score.total == 100
    assert signal.direction == "NO TRADE"
    assert any("structure" in reason for reason in signal.rejection_reasons)


def test_missing_data_produces_zero_score_not_a_directional_signal():
    candles,analyses=fixture_inputs(count=40)
    signals=StrategyEngine(StrategyConfig(enabled=("trend_following","momentum"))).evaluate(
        "BTC/USDT","binance",candles,analyses,valid_quality(candles))
    assert all(s.direction == "NO TRADE" and s.action == "NO_TRADE" for s in signals)
    assert all(s.position_side is None and s.score.total == 0 for s in signals)


@pytest.mark.parametrize("direction,entry,invalidation,stop,targets,reason",[
    ("LONG",(99.9,100.1),100.0,100.0,(102,103),"LONG invalidation and stop-loss"),
    ("LONG",(99.9,100.1),100.2,100.2,(102,103),"LONG invalidation and stop-loss"),
    ("LONG",(99.9,100.1),99.0,99.0,(100.1,103),"every LONG take-profit"),
    ("LONG",(99.9,100.1),99.0,99.0,(99.0,103),"every LONG take-profit"),
    ("SHORT",(99.9,100.1),100.1,100.1,(98,97),"SHORT invalidation and stop-loss"),
    ("SHORT",(99.9,100.1),99.8,99.8,(98,97),"SHORT invalidation and stop-loss"),
    ("SHORT",(99.9,100.1),101.0,101.0,(99.9,97),"every SHORT take-profit"),
    ("SHORT",(99.9,100.1),101.0,101.0,(101,97),"every SHORT take-profit"),
    ("LONG",(99.9,100.1),99.0,99.0,(102,101),"strictly ascending"),
    ("SHORT",(99.9,100.1),101.0,101.0,(98,99),"strictly descending"),
])
def test_level_geometry_boundary_rejects_invalid_stop_invalidation_or_any_target(
        direction,entry,invalidation,stop,targets,reason):
    from tradingagent.strategies.engine import _level_geometry_error
    assert reason in _level_geometry_error(direction,entry,invalidation,stop,targets)


@pytest.mark.parametrize("direction,entry,invalidation,stop,targets",[
    ("LONG",(99.9,100.1),99,99,(102,103)),
    ("SHORT",(99.9,100.1),101,101,(98,97)),
])
def test_level_geometry_boundary_accepts_valid_levels_for_both_sides(
        direction,entry,invalidation,stop,targets):
    from tradingagent.strategies.engine import _level_geometry_error
    assert _level_geometry_error(direction,entry,invalidation,stop,targets) is None



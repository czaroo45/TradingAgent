from datetime import datetime, timezone

import pytest

from tradingagent.models import ScoreBreakdown, StrategySignal
from tradingagent.paper_trading import PaperTradeRejected, authorize_paper_entry
from tradingagent.risk import (
    CircuitBreaker, InstrumentConstraints, OpenPosition, PortfolioSnapshot,
    RiskLimits, RiskManager, RiskRequest, position_size, stop_execution_stress,
)

_DEFAULT_STOP = object()


def signal(symbol="BTC/USDT", *, stop=_DEFAULT_STOP, cost=20.0, rr=2.0, direction="LONG",
           action=None, position_side=None, invalidation=None, entry_zone=(999.0,1001.0),
           targets=None):
    score = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1, 100)
    if stop is _DEFAULT_STOP:
        stop = 1010.0 if direction == "SHORT" else 990.0
    if invalidation is None:
        invalidation = stop
    if targets is None:
        targets = (970.0, 950.0) if direction == "SHORT" else (1030.0, 1050.0)
    return StrategySignal(symbol, "binance", datetime.now(timezone.utc), "test",
        direction, "conditions_met", entry_zone, "calculated", invalidation,
        "calculated", stop, targets, rr, cost, 5.0, ("15m", "4h"),
        ("ATR14",), ("test conditional setup",), (), 100.0, score,
        action=action, position_side=position_side)


def request(*, candidate=None, portfolio=None, instrument=None, age=1000,
            data_ok=True, fee=10, slip_entry=5, slip_stop=5, stress_slip=25):
    return RiskRequest(candidate or signal(), portfolio or PortfolioSnapshot(10_000),
        instrument or InstrumentConstraints("USDT", amount_step=.1), age,
        data_ok, fee, slip_entry, slip_stop, stress_slip)


def permissive_limits(**overrides):
    values = dict(max_position_risk_fraction=.01,
        max_total_exposure_fraction=.9, max_asset_exposure_fraction=.9,
        max_correlated_exposure_fraction=.9, max_daily_loss_fraction=.1,
        max_drawdown_fraction=.5, max_trades_per_day=20,
        min_risk_reward=1.5, max_transaction_cost_bps=50,
        max_data_age_ms=10_000)
    values.update(overrides)
    return RiskLimits(**values)


def test_size_uses_stop_distance_costs_and_precision():
    manager = RiskManager(permissive_limits())
    result = manager.evaluate(request())
    assert result.accepted
    assert result.quantity == 8.3
    assert result.estimated_loss_at_stop <= result.risk_budget
    assert result.transaction_cost_bps == 20
    assert result.position_side == "LONG"
    assert result.stop_distance == 10
    assert result.risk_per_unit > result.stop_distance
    assert result.net_risk_reward >= 1.5


def test_risk_gate_rejects_order_below_exchange_minimum_notional():
    result=RiskManager(permissive_limits()).evaluate(request(
        instrument=InstrumentConstraints("USDT",amount_step=.1,min_notional=9_000)))
    assert not result.accepted
    assert any("notional is below the instrument minimum" in reason for reason in result.reasons)


def test_risk_gate_rejects_quantity_below_exchange_minimum_amount():
    result=RiskManager(permissive_limits()).evaluate(request(
        instrument=InstrumentConstraints("USDT",amount_step=.1,min_amount=9)))
    assert not result.accepted
    assert any("below the instrument minimum" in reason for reason in result.reasons)


def test_valid_short_entry_is_accepted_with_nonnegative_directional_risk():
    result=RiskManager(permissive_limits()).evaluate(request(candidate=signal(direction="SHORT")))
    assert result.accepted
    assert result.position_side == "SHORT"
    assert result.simulated_only
    assert result.quantity == 8.3
    assert result.stop_distance == 10
    assert result.risk_per_unit > 0
    assert result.estimated_loss_at_stop <= result.risk_budget
    assert result.stop_stress is not None
    assert result.stop_stress.planned_loss == pytest.approx(result.estimated_loss_at_stop)
    assert result.stop_stress.stressed_loss > result.stop_stress.planned_loss


def test_short_position_obeys_gross_correlated_exposure_limit():
    portfolio=PortfolioSnapshot(10_000,
        positions=(OpenPosition("ETH/USDT",31,100),))
    result=RiskManager(RiskLimits()).evaluate(request(
        candidate=signal(direction="SHORT"),portfolio=portfolio))
    assert not result.accepted
    assert any("exposure" in reason for reason in result.reasons)


def test_short_position_size_and_stress_match_long_distance_without_negative_risk():
    long_qty=position_size(10_000,100,90,.01,cost_bps=20,amount_step=.01)
    short_qty=position_size(10_000,100,110,.01,cost_bps=20,amount_step=.01,
                            position_side="SHORT")
    assert short_qty == long_qty
    long_stress=stop_execution_stress(100,90,1,20,25)
    short_stress=stop_execution_stress(100,110,1,20,25,position_side="SHORT")
    assert short_stress.planned_loss > 0
    assert short_stress.planned_loss > long_stress.planned_loss
    assert short_stress.stressed_loss > short_stress.planned_loss
    with pytest.raises(ValueError,match="SHORT stop"):
        position_size(10_000,100,90,.01,position_side="SHORT")
    with pytest.raises(ValueError,match="SHORT stop"):
        stop_execution_stress(100,90,1,20,25,position_side="SHORT")


def test_short_cost_components_are_not_double_counted_and_quantity_is_floored():
    candidate=signal(direction="SHORT",cost=35)
    result=RiskManager(permissive_limits()).evaluate(request(
        candidate=candidate,instrument=InstrumentConstraints("USDT",amount_step=.1)))
    assert result.accepted
    assert result.transaction_cost_bps == 35
    assert result.quantity == 7.4
    assert result.estimated_loss_at_stop <= result.risk_budget
    assert result.stop_stress.planned_loss == pytest.approx(result.estimated_loss_at_stop)
    assert result.fee_bps_round_trip == 10
    assert result.slippage_bps_entry == result.slippage_bps_stop == 5


def test_liquidity_and_asset_quote_caps_reduce_position():
    instrument = InstrumentConstraints("USDT", amount_step=.01,
                                       liquidity_notional_cap=500)
    result = RiskManager(permissive_limits()).evaluate(request(instrument=instrument))
    assert result.accepted
    assert result.quote_notional <= 500
    assert result.quantity == .5


def test_existing_same_asset_exposure_caps_additional_position():
    portfolio = PortfolioSnapshot(10_000,
        positions=(OpenPosition("BTC/USDT", 1.5, 1000),))
    result = RiskManager(RiskLimits()).evaluate(request(portfolio=portfolio))
    assert result.accepted
    assert result.quote_notional <= 500 + 1e-8


def test_overlapping_correlated_positions_limit_new_exposure():
    portfolio = PortfolioSnapshot(10_000,
        positions=(OpenPosition("ETH/USDT", 29, 1000),))
    result = RiskManager(RiskLimits()).evaluate(request(portfolio=portfolio))
    assert not result.accepted
    assert any("exposure" in reason for reason in result.reasons)


def test_daily_loss_drawdown_and_trade_count_limits_reject():
    manager = RiskManager(RiskLimits())
    for portfolio in (
        PortfolioSnapshot(10_000, daily_realized_pnl=-400),
        PortfolioSnapshot(8_000, peak_equity=10_000),
        PortfolioSnapshot(10_000, trades_today=10),
    ):
        result = manager.evaluate(request(portfolio=portfolio))
        assert not result.accepted


def test_stale_or_failed_data_trips_circuit_breaker():
    manager = RiskManager(permissive_limits())
    result = manager.evaluate(request(age=20_000))
    assert not result.accepted
    assert manager.breaker.tripped
    assert not manager.evaluate(request()).accepted


def test_invalid_stop_cost_and_non_actionable_signal_are_rejected():
    manager = RiskManager(permissive_limits())
    wrong_stop = manager.evaluate(request(candidate=signal(stop=1000)))
    assert not wrong_stop.accepted
    wrong_direction = RiskManager(permissive_limits()).evaluate(
        request(candidate=signal(direction="NO TRADE")))
    assert not wrong_direction.accepted
    too_costly = RiskManager(permissive_limits(max_transaction_cost_bps=10)).evaluate(request())
    assert not too_costly.accepted


@pytest.mark.parametrize("direction,action,position_side,expected",[
    ("NO TRADE","NO_TRADE",None,"NO_TRADE"),
    ("EXIT","EXIT",None,"EXIT"),
])
def test_no_trade_and_exit_are_never_accepted_as_new_entries(direction,action,position_side,expected):
    candidate=signal(direction=direction,action=action,position_side=position_side)
    result=RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert result.position_side is None
    assert any(expected in reason for reason in result.reasons)


def test_entry_action_requires_conditions_met_status_and_model_rejects_conflicting_side():
    observation=signal(direction="SHORT")
    from dataclasses import replace
    result=RiskManager(permissive_limits()).evaluate(request(
        candidate=replace(observation,status="observation")))
    assert not result.accepted
    assert any("conditions_met" in reason for reason in result.reasons)
    with pytest.raises(ValueError,match="position_side"):
        signal(direction="SHORT",action="ENTRY",position_side="LONG")


def test_claimed_rr_cannot_override_net_rr_after_directional_costs():
    candidate=signal(direction="SHORT",stop=1010,targets=(998,),rr=20)
    result=RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert result.net_risk_reward is not None and result.net_risk_reward < 1.5
    assert any("net reward/risk" in reason for reason in result.reasons)


@pytest.mark.parametrize("direction,stop,targets,invalidation,reason",[
    ("LONG",1000,(1030,),990,"LONG stop-loss"),
    ("LONG",990,(1000,),990,"every LONG profit target"),
    ("LONG",990,(1030,1020),990,"strictly ascending"),
    ("LONG",990,(1030,),1002,"LONG invalidation"),
    ("SHORT",1000,(970,),1010,"SHORT stop-loss"),
    ("SHORT",1010,(1000,),1010,"every SHORT profit target"),
    ("SHORT",1010,(970,980),1010,"strictly descending"),
    ("SHORT",1010,(970,),998,"SHORT invalidation"),
])
def test_directional_level_validation_rejects_invalid_stop_targets_and_invalidation(
        direction,stop,targets,invalidation,reason):
    candidate=signal(direction=direction,stop=stop,targets=targets,invalidation=invalidation)
    result=RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert any(reason in item for item in result.reasons)


@pytest.mark.parametrize("direction,entry_zone,stop,targets,invalidation,reason",[
    ("LONG",None,990,(1030,),990,"missing entry"),
    ("LONG",(999,1001),None,(1030,),990,"missing entry"),
    ("LONG",(999,1001),990,(),990,"missing entry"),
    ("LONG",(999,float("nan")),990,(1030,),990,"finite, positive"),
    ("SHORT",(999,1001),1010,(970,float("inf")),1010,"finite, positive"),
    ("SHORT",(999,1001),1010,(970,),float("nan"),"invalidation"),
])
def test_missing_or_nonfinite_directional_levels_are_rejected(
        direction,entry_zone,stop,targets,invalidation,reason):
    candidate=signal(direction=direction,stop=stop,entry_zone=entry_zone,
                     targets=targets,invalidation=invalidation)
    result=RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert any(reason in item for item in result.reasons)


def test_malformed_entry_zone_is_rejected_without_crashing_invalidation_check():
    from dataclasses import replace

    candidate = replace(signal(), entry_zone=1000.0)
    result = RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert any("entry zone must contain exactly two" in reason
               for reason in result.reasons)


def test_malformed_targets_are_rejected_without_crashing():
    from dataclasses import replace

    candidate = replace(signal(), targets=1030.0)
    result = RiskManager(permissive_limits()).evaluate(request(candidate=candidate))
    assert not result.accepted
    assert any("missing entry, stop-loss, or target" in reason
               for reason in result.reasons)


def test_worse_stop_fill_is_reported_without_repricing_the_signal():
    stress = stop_execution_stress(100, 90, 2, 20, 100)
    assert stress.stressed_loss > stress.planned_loss
    assert stress.additional_loss > 0
    result = RiskManager(permissive_limits()).evaluate(request())
    assert result.stop_stress is not None
    assert result.stop_stress.stressed_loss > result.stop_stress.planned_loss


def test_emergency_breaker_and_paper_gate_fail_closed():
    breaker = CircuitBreaker()
    manager = RiskManager(permissive_limits(), breaker)
    breaker.trip("operator emergency stop")
    assert not manager.evaluate(request()).accepted
    with pytest.raises(PaperTradeRejected):
        authorize_paper_entry(manager, request())
    breaker.reset()
    assert manager.evaluate(request()).accepted


def test_position_size_rejects_extreme_values_and_wrong_stop_side():
    assert position_size(10_000, 100, 90, .01, cost_bps=20, amount_step=.01) > 0
    with pytest.raises(ValueError):
        position_size(float("inf"), 100, 90, .01)
    with pytest.raises(ValueError):
        position_size(10_000, 100, 110, .01)


def test_currency_and_data_health_must_match():
    mismatch = RiskManager(permissive_limits()).evaluate(request(
        portfolio=PortfolioSnapshot(10_000, "USD")))
    assert not mismatch.accepted
    unhealthy = RiskManager(permissive_limits()).evaluate(request(data_ok=False))
    assert not unhealthy.accepted


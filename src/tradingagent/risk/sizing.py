"""Independent risk gate and cost-aware sizing for paper-trading consumers.

All notional amounts and equity are denominated in the configured quote currency.
No function in this module sends orders or silently adjusts proposed signal levels.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from threading import RLock
from tradingagent.models import StrategySignal


@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    quantity: float
    mark_price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.mark_price


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    equity: float
    quote_currency: str = "USDT"
    positions: tuple[OpenPosition, ...] = ()
    daily_realized_pnl: float = 0.0
    daily_unrealized_pnl: float = 0.0
    peak_equity: float | None = None
    trades_today: int = 0

    @property
    def drawdown_fraction(self) -> float:
        if self.peak_equity is None or self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)


@dataclass(frozen=True, slots=True)
class InstrumentConstraints:
    quote_currency: str
    amount_step: float = 1e-8
    min_amount: float = 0.0
    max_amount: float | None = None
    liquidity_notional_cap: float | None = None
    min_notional: float = 0.0

    def __post_init__(self) -> None:
        values = (self.amount_step, self.min_amount, self.min_notional)
        if not self.quote_currency or any(not math.isfinite(x) or x < 0 for x in values) or self.amount_step <= 0:
            raise ValueError("instrument currency, precision, and minimum amount/notional are invalid")
        if self.max_amount is not None and (not math.isfinite(self.max_amount) or self.max_amount <= 0):
            raise ValueError("max amount must be positive")
        if self.liquidity_notional_cap is not None and (not math.isfinite(self.liquidity_notional_cap) or self.liquidity_notional_cap <= 0):
            raise ValueError("liquidity cap must be positive")


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_position_risk_fraction: float = 0.01
    max_total_exposure_fraction: float = 0.50
    max_asset_exposure_fraction: float = 0.20
    max_correlated_exposure_fraction: float = 0.30
    correlated_groups: tuple[tuple[str, ...], ...] = (("BTC", "ETH", "SOL", "XRP"),)
    max_daily_loss_fraction: float = 0.03
    max_drawdown_fraction: float = 0.15
    max_trades_per_day: int = 10
    min_risk_reward: float = 1.5
    max_transaction_cost_bps: float = 50.0
    max_data_age_ms: int = 120_000
    max_risk_fraction_ceiling: float = 0.02

    def __post_init__(self) -> None:
        fractions = (self.max_position_risk_fraction, self.max_total_exposure_fraction,
                     self.max_asset_exposure_fraction, self.max_correlated_exposure_fraction,
                     self.max_daily_loss_fraction, self.max_drawdown_fraction,
                     self.max_risk_fraction_ceiling)
        if any(not math.isfinite(x) or not 0 < x <= 1 for x in fractions):
            raise ValueError("risk and exposure fractions must be finite and in (0, 1]")
        if self.max_risk_fraction_ceiling > 0.02:
            raise ValueError("hard per-position risk ceiling cannot exceed 2%")
        if self.max_position_risk_fraction > self.max_risk_fraction_ceiling:
            raise ValueError("per-position risk exceeds the configured hard ceiling")
        if self.max_trades_per_day <= 0 or self.max_data_age_ms < 0:
            raise ValueError("trade count must be positive and max data age nonnegative")
        if not math.isfinite(self.min_risk_reward) or self.min_risk_reward <= 0:
            raise ValueError("minimum risk/reward must be positive")
        if not math.isfinite(self.max_transaction_cost_bps) or self.max_transaction_cost_bps < 0:
            raise ValueError("maximum transaction cost must be nonnegative")


@dataclass(frozen=True, slots=True)
class RiskRequest:
    signal: StrategySignal
    portfolio: PortfolioSnapshot
    instrument: InstrumentConstraints
    data_age_ms: int
    data_healthy: bool = False
    fee_bps_round_trip: float = 20.0
    slippage_bps_entry: float = 5.0
    slippage_bps_stop: float = 5.0
    adverse_stop_slippage_bps: float = 25.0


@dataclass(frozen=True, slots=True)
class StopStress:
    planned_loss: float
    stressed_loss: float
    additional_loss: float
    adverse_slippage_bps: float


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    symbol: str
    quantity: float
    quote_notional: float
    risk_budget: float
    estimated_loss_at_stop: float
    transaction_cost_bps: float
    reasons: tuple[str, ...]
    stop_stress: StopStress | None = None
    simulated_only: bool = True
    position_side: str | None = None
    entry_price: float | None = None
    stop_distance: float | None = None
    risk_per_unit: float | None = None
    fee_bps_round_trip: float = 0.0
    slippage_bps_entry: float = 0.0
    slippage_bps_stop: float = 0.0
    net_risk_reward: float | None = None


class CircuitBreaker:
    """Latched fail-closed gate; reset is an explicit operator action."""

    def __init__(self) -> None:
        self._reason: str | None = None
        self._lock = RLock()

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def trip(self, reason: str) -> None:
        with self._lock:
            self._reason = reason.strip() or "unspecified critical risk event"

    def reset(self) -> None:
        with self._lock:
            self._reason = None


def _asset(symbol: str) -> str:
    return symbol.split("/", 1)[0].upper()


def _floor_step(quantity: float, step: float) -> float:
    q = Decimal(str(quantity))
    quantum = Decimal(str(step))
    return float((q / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum)


def stop_execution_stress(entry: float, stop: float, quantity: float,
                          fee_bps_round_trip: float, adverse_slippage_bps: float,
                          *, slippage_entry_bps: float = 0.0,
                          slippage_stop_bps: float = 0.0,
                          additional_cost_bps: float = 0.0,
                          position_side: str = "LONG") -> StopStress:
    """Show directional planned stop loss and an adverse stop-fill stress."""
    values = (entry, stop, quantity, fee_bps_round_trip, adverse_slippage_bps,
              slippage_entry_bps, slippage_stop_bps, additional_cost_bps)
    if any(not math.isfinite(v) for v in values) or min(entry, stop, quantity) <= 0 or min(fee_bps_round_trip, adverse_slippage_bps, slippage_entry_bps, slippage_stop_bps, additional_cost_bps) < 0:
        raise ValueError("stress inputs must be finite with positive prices/quantity and nonnegative costs")
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError("position_side must be LONG or SHORT")
    distance = entry - stop if position_side == "LONG" else stop - entry
    if distance <= 0:
        raise ValueError(f"{position_side} stop is on the wrong side of entry")
    # Split round-trip fees between entry and stop notional; slippage is charged
    # on the respective fill. This remains an estimate, not a loss ceiling.
    planned = quantity * (distance +
        entry * (fee_bps_round_trip / 2 + slippage_entry_bps) / 10_000 +
        stop * (fee_bps_round_trip / 2 + slippage_stop_bps) / 10_000 +
        entry * additional_cost_bps / 10_000)
    stressed = planned + quantity * stop * adverse_slippage_bps / 10_000
    return StopStress(planned, stressed, stressed - planned, adverse_slippage_bps)


class RiskManager:
    def __init__(self, limits: RiskLimits | None = None,
                 breaker: CircuitBreaker | None = None):
        self.limits = limits or RiskLimits()
        self.breaker = breaker or CircuitBreaker()

    @classmethod
    def from_settings(cls, settings, breaker: CircuitBreaker | None = None) -> "RiskManager":
        """Build live policy values from validated app settings."""
        return cls(RiskLimits(
            max_position_risk_fraction=settings.max_risk_per_trade,
            max_total_exposure_fraction=settings.max_total_exposure,
            max_asset_exposure_fraction=settings.max_asset_exposure,
            max_correlated_exposure_fraction=settings.max_correlated_exposure,
            correlated_groups=settings.correlated_asset_groups,
            max_daily_loss_fraction=settings.max_daily_loss,
            max_drawdown_fraction=settings.max_drawdown,
            max_trades_per_day=settings.max_trades_per_day,
            min_risk_reward=settings.min_risk_reward,
            max_transaction_cost_bps=settings.max_transaction_cost_bps,
            max_data_age_ms=settings.max_market_data_age_ms), breaker)

    def evaluate(self, request: RiskRequest) -> RiskDecision:
        signal, portfolio, instrument = request.signal, request.portfolio, request.instrument
        reasons: list[str] = []
        side = signal.position_side
        direction = signal.direction
        side_valid = direction in {"LONG", "SHORT"} and side == direction
        if (signal.action != "ENTRY" or signal.status != "conditions_met" or not side_valid):
            if signal.action == "EXIT" or direction == "EXIT":
                reasons.append("EXIT signals are not new position entries")
            elif signal.action == "NO_TRADE" or direction == "NO TRADE":
                reasons.append("NO_TRADE signals cannot open a position")
            elif not side_valid:
                reasons.append("signal action, direction, and position side are inconsistent")
            else:
                reasons.append("entry signal status must be conditions_met")
        if self.breaker.tripped:
            reasons.append(f"circuit breaker is active: {self.breaker.reason}")
        if not request.data_healthy:
            reasons.append("market data quality is unhealthy")
        if not isinstance(request.data_age_ms, int) or request.data_age_ms < 0:
            reasons.append("market data age must be a nonnegative integer")
        elif request.data_age_ms > self.limits.max_data_age_ms:
            reasons.append(f"market data age {request.data_age_ms} ms exceeds limit {self.limits.max_data_age_ms} ms")
        if not math.isfinite(portfolio.equity) or portfolio.equity <= 0:
            reasons.append("portfolio equity must be finite and positive")
        if (not math.isfinite(portfolio.daily_realized_pnl) or
                not math.isfinite(portfolio.daily_unrealized_pnl) or
                (portfolio.peak_equity is not None and
                 (not math.isfinite(portfolio.peak_equity) or portfolio.peak_equity <= 0))):
            reasons.append("portfolio PnL and peak equity must be finite")
        if any(not math.isfinite(p.quantity) or not math.isfinite(p.mark_price) or
               p.quantity < 0 or p.mark_price <= 0 for p in portfolio.positions):
            reasons.append("open-position quantities and marks must be finite and valid")
        if portfolio.quote_currency.upper() != instrument.quote_currency.upper():
            reasons.append("portfolio and instrument quote currencies do not match")
        if "/" not in signal.symbol or signal.symbol.split("/", 1)[1].upper() != instrument.quote_currency.upper():
            reasons.append("instrument quote currency does not match signal symbol")
        if portfolio.trades_today >= self.limits.max_trades_per_day:
            reasons.append("daily trade count limit reached")
        if portfolio.daily_realized_pnl + portfolio.daily_unrealized_pnl <= -max(0.0, portfolio.equity) * self.limits.max_daily_loss_fraction:
            reasons.append("daily loss limit reached")
        if portfolio.drawdown_fraction >= self.limits.max_drawdown_fraction:
            reasons.append("maximum drawdown limit reached")

        entry = stop = low = high = 0.0
        geometry_valid = True
        entry_bounds_valid = False
        targets_well_formed = isinstance(signal.targets, (tuple, list)) and bool(signal.targets)
        if signal.entry_zone is None or signal.stop_loss is None or not targets_well_formed:
            reasons.append("signal is missing entry, stop-loss, or target levels")
            geometry_valid = False
        elif not isinstance(signal.entry_zone, (tuple, list)) or len(signal.entry_zone) != 2:
            reasons.append("entry zone must contain exactly two price bounds")
            geometry_valid = False
        else:
            low, high = signal.entry_zone
            raw_values = (low, high, signal.stop_loss, *signal.targets)
            if (any(not isinstance(x, (int, float)) or isinstance(x, bool) or
                    not math.isfinite(x) or x <= 0 for x in raw_values) or low > high):
                reasons.append("signal entry, stop, and target levels must be finite, positive, and ordered")
                geometry_valid = False
            else:
                entry = (low + high) / 2
                stop = signal.stop_loss
                entry_bounds_valid = True
                if side not in {"LONG", "SHORT"}:
                    reasons.append("position side is required to validate directional levels")
                    geometry_valid = False
                elif side == "LONG":
                    if stop >= low:
                        reasons.append("LONG stop-loss must be below the complete entry zone")
                        geometry_valid = False
                    if any(target <= high for target in signal.targets):
                        reasons.append("every LONG profit target must be above the complete entry zone")
                        geometry_valid = False
                    if any(a >= b for a, b in zip(signal.targets, signal.targets[1:])):
                        reasons.append("LONG profit targets must be strictly ascending")
                        geometry_valid = False
                else:
                    if stop <= high:
                        reasons.append("SHORT stop-loss must be above the complete entry zone")
                        geometry_valid = False
                    if any(target >= low for target in signal.targets):
                        reasons.append("every SHORT profit target must be below the complete entry zone")
                        geometry_valid = False
                    if any(a <= b for a, b in zip(signal.targets, signal.targets[1:])):
                        reasons.append("SHORT profit targets must be strictly descending")
                        geometry_valid = False
        if signal.entry_zone_basis != "calculated" or signal.invalidation_basis != "calculated":
            reasons.append("risk gate requires market-calculated, non-illustrative levels")
        invalidation_valid = (isinstance(signal.invalidation, (int, float)) and
            not isinstance(signal.invalidation, bool) and math.isfinite(signal.invalidation) and
            signal.invalidation > 0)
        if not invalidation_valid:
            reasons.append("scenario invalidation is missing or is not a finite positive price")
            geometry_valid = False
        elif side == "LONG" and entry_bounds_valid:
            if signal.invalidation >= low or signal.invalidation > stop:
                reasons.append("LONG invalidation must be below entry and at or below stop-loss")
                geometry_valid = False
        elif side == "SHORT" and entry_bounds_valid:
            if signal.invalidation <= high or signal.invalidation < stop:
                reasons.append("SHORT invalidation must be above entry and at or above stop-loss")
                geometry_valid = False
        if (signal.risk_reward is None or not isinstance(signal.risk_reward, (int, float)) or
                isinstance(signal.risk_reward, bool) or not math.isfinite(signal.risk_reward) or
                signal.risk_reward < self.limits.min_risk_reward):
            reasons.append("signal reward/risk is missing or below the configured minimum")

        costs = (request.fee_bps_round_trip, request.slippage_bps_entry,
                 request.slippage_bps_stop, request.adverse_stop_slippage_bps)
        costs_valid = all(isinstance(x, (int, float)) and not isinstance(x, bool) and
                          math.isfinite(x) and x >= 0 for x in costs)
        if not costs_valid:
            reasons.append("fee and slippage estimates must be finite and nonnegative")
        clean_costs = tuple(x if isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 else 0.0 for x in costs)
        request_cost_bps = sum(clean_costs[:3])
        transaction_cost_bps = request_cost_bps
        signal_cost_valid = (isinstance(signal.estimated_cost_bps, (int, float)) and
            not isinstance(signal.estimated_cost_bps, bool) and math.isfinite(signal.estimated_cost_bps) and
            signal.estimated_cost_bps >= 0)
        if not signal_cost_valid:
            reasons.append("signal transaction cost estimate is missing")
        else:
            transaction_cost_bps = max(transaction_cost_bps, signal.estimated_cost_bps)
        if transaction_cost_bps > self.limits.max_transaction_cost_bps:
            reasons.append("estimated transaction costs exceed the configured maximum")

        existing_exposure = sum(p.notional for p in portfolio.positions)
        if not math.isfinite(existing_exposure):
            reasons.append("open-position gross exposure cannot be calculated reliably")
            existing_exposure = math.inf
        asset_exposure = sum(p.notional for p in portfolio.positions if _asset(p.symbol) == _asset(signal.symbol))
        group = next(({item.upper() for item in items} for items in self.limits.correlated_groups
                      if _asset(signal.symbol) in {item.upper() for item in items}), {_asset(signal.symbol)})
        correlated_exposure = sum(p.notional for p in portfolio.positions if _asset(p.symbol) in group)
        max_total = portfolio.equity * self.limits.max_total_exposure_fraction
        max_asset = portfolio.equity * self.limits.max_asset_exposure_fraction
        max_group = portfolio.equity * self.limits.max_correlated_exposure_fraction
        exposure_capacity = min(max_total-existing_exposure, max_asset-asset_exposure, max_group-correlated_exposure)
        if exposure_capacity <= 0:
            reasons.append("portfolio, asset, or correlated-asset exposure limit is already reached")

        risk_budget = (portfolio.equity * self.limits.max_position_risk_fraction
                       if math.isfinite(portfolio.equity) and portfolio.equity > 0 else 0.0)
        quantity = 0.0
        estimated_loss = 0.0
        stress = None
        stop_distance = abs(entry - stop) if entry > 0 and stop > 0 else 0.0
        risk_per_unit = 0.0
        extra_signal_cost_bps = max(0.0, transaction_cost_bps - request_cost_bps)
        costs_per_unit = 0.0
        net_risk_reward: float | None = None
        if geometry_valid and side in {"LONG", "SHORT"} and stop_distance > 0 and costs_valid and signal_cost_valid:
            costs_per_unit = (
                entry * (clean_costs[0] / 2 + clean_costs[1]) / 10_000 +
                stop * (clean_costs[0] / 2 + clean_costs[2]) / 10_000 +
                entry * extra_signal_cost_bps / 10_000)
            risk_per_unit = stop_distance + costs_per_unit
            target = signal.targets[0]
            gross_reward = target - entry if side == "LONG" else entry - target
            reward_cost = (
                entry * (clean_costs[0] / 2 + clean_costs[1]) / 10_000 +
                target * (clean_costs[0] / 2 + clean_costs[2]) / 10_000 +
                entry * extra_signal_cost_bps / 10_000)
            net_reward = gross_reward - reward_cost
            if risk_per_unit > 0 and math.isfinite(risk_per_unit) and math.isfinite(net_reward):
                net_risk_reward = net_reward / risk_per_unit
            if net_risk_reward is None or net_risk_reward < self.limits.min_risk_reward:
                reasons.append("net reward/risk after estimated entry and exit costs is below the configured minimum")
            if risk_per_unit > 0 and math.isfinite(risk_per_unit) and risk_budget > 0:
                quantity = risk_budget / risk_per_unit
                max_notional = max(0.0, exposure_capacity)
                if instrument.liquidity_notional_cap is not None:
                    max_notional = min(max_notional, instrument.liquidity_notional_cap)
                quantity = min(quantity, max_notional / entry)
                if instrument.max_amount is not None:
                    quantity = min(quantity, instrument.max_amount)
                quantity = _floor_step(quantity, instrument.amount_step)
                notional = quantity * entry
                estimated_loss = quantity * risk_per_unit
                if quantity < instrument.min_amount or quantity <= 0:
                    reasons.append("risk-sized amount is below the instrument minimum or rounds to zero")
                    quantity = 0.0
                    notional = 0.0
                    estimated_loss = 0.0
                elif notional + 1e-12 < instrument.min_notional:
                    reasons.append("risk-sized order notional is below the instrument minimum")
                    quantity = 0.0
                    notional = 0.0
                    estimated_loss = 0.0
                else:
                    if notional > exposure_capacity:
                        reasons.append("rounded quantity exceeds remaining portfolio exposure capacity")
                    stress = stop_execution_stress(entry, stop, quantity,
                        clean_costs[0], clean_costs[3],
                        slippage_entry_bps=clean_costs[1],
                        slippage_stop_bps=clean_costs[2],
                        additional_cost_bps=extra_signal_cost_bps,
                        position_side=side)
                    if estimated_loss > risk_budget + 1e-8:
                        reasons.append("estimated stop loss including costs exceeds the position risk budget")
            else:
                reasons.append("risk per unit is invalid or risk budget is unavailable")
        else:
            notional = 0.0
            if geometry_valid and stop_distance <= 0:
                reasons.append("entry-to-stop distance must be positive to size the position")
            elif geometry_valid and (not costs_valid or not signal_cost_valid):
                reasons.append("cost estimates are unavailable for safe position sizing")
        if reasons:
            breaker_reasons = [reason for reason in reasons if any(term in reason for term in (
                "circuit breaker", "market data quality", "market data age", "daily loss",
                "drawdown", "trade count limit", "exposure limit", "transaction costs exceed",
                "equity must", "quote currencies do not match", "open-position quantities"))]
            if breaker_reasons:
                self.breaker.trip("; ".join(breaker_reasons))
            # Attach the computed net RR even to a rejected, otherwise well-formed
            # candidate so the decision remains auditable.
            return RiskDecision(False, signal.symbol, 0.0, 0.0,
                risk_budget, 0.0, transaction_cost_bps, tuple(reasons), stress,
                True, side if side in {"LONG", "SHORT"} else None,
                entry if entry > 0 else None, stop_distance if stop_distance > 0 else None,
                risk_per_unit if risk_per_unit > 0 else None,
                clean_costs[0], clean_costs[1], clean_costs[2], net_risk_reward)
        return RiskDecision(True, signal.symbol, quantity, notional, risk_budget,
                            estimated_loss, transaction_cost_bps, (), stress,
                            True, side, entry, stop_distance, risk_per_unit,
                            clean_costs[0], clean_costs[1], clean_costs[2], net_risk_reward)


def position_size(equity: float, entry: float, stop: float, risk_fraction: float,
                  *, cost_bps: float = 0.0, amount_step: float = 1e-8,
                  max_notional: float | None = None,
                  position_side: str = "LONG") -> float:
    """Compatibility helper: directionally valid stop distance sizes either side."""
    values = (equity, entry, stop, risk_fraction, cost_bps, amount_step)
    if any(not math.isfinite(x) for x in values) or equity <= 0 or entry <= 0 or stop <= 0 or not 0 < risk_fraction <= 0.02 or cost_bps < 0 or amount_step <= 0:
        raise ValueError("equity/prices must be positive; risk fraction in (0, 0.02]; costs/step valid")
    if position_side not in {"LONG", "SHORT"}:
        raise ValueError("position_side must be LONG or SHORT")
    distance = entry - stop if position_side == "LONG" else stop - entry
    if distance <= 0:
        raise ValueError(f"{position_side} stop must be on the loss side of entry")
    quantity = equity * risk_fraction / (distance + entry * cost_bps / 10_000)
    if max_notional is not None:
        if not math.isfinite(max_notional) or max_notional <= 0:
            raise ValueError("max_notional must be positive")
        quantity = min(quantity, max_notional / entry)
    return _floor_step(quantity, amount_step)


__all__ = ["CircuitBreaker", "InstrumentConstraints", "OpenPosition", "PortfolioSnapshot",
           "RiskDecision", "RiskLimits", "RiskManager", "RiskRequest", "StopStress", "position_size",
           "stop_execution_stress"]

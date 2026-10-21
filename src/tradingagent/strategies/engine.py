"""Deterministic multi-timeframe strategy rules; output is advisory, never orders."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from tradingagent.analysis import AnalysisError, calculate_indicators
from tradingagent.models import (Analysis, Candle, DataQuality, StrategySignal)
from tradingagent.scoring import (ScoreWeights, directional_score_parts,
                                  score_signal)

@dataclass(frozen=True, slots=True)
class StrategyConfig:
    enabled: tuple[str, ...] = ("trend_following", "pullback", "breakout_retest", "momentum")
    aggressive_mode: bool = False
    entry_timeframe: str = "15m"
    higher_timeframe: str = "4h"
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 5.0
    spread_bps: float | None = None
    min_net_risk_reward: float = 1.5
    max_risk_atr: float = 3.0
    min_score: float = 60.0
    breakout_volume_multiple: float = 1.3
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def __post_init__(self) -> None:
        allowed = {"trend_following", "pullback", "breakout_retest", "momentum", "mean_reversion"}
        if set(self.enabled) - allowed:
            raise ValueError("unknown strategy in enabled list")
        if self.fee_bps_per_side < 0 or self.slippage_bps_per_side < 0:
            raise ValueError("fee and slippage must be nonnegative")
        if self.min_net_risk_reward <= 0 or self.max_risk_atr <= 0 or not 0 <= self.min_score <= 100:
            raise ValueError("strategy risk/scoring thresholds are invalid")


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    seed = sum(values[:period]) / min(period, len(values))
    out = [seed] * min(period, len(values))
    for value in values[period:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _structure(rows: list[Candle]) -> tuple[bool, bool]:
    if len(rows) < 6:
        return False, False
    recent = rows[-6:]
    highs = [c.high for c in recent]
    lows = [c.low for c in recent]
    return highs[-1] > max(highs[:-1]) and lows[-1] >= min(lows[:-1]), highs[-1] < max(highs[:-1]) and lows[-1] < min(lows[:-1])


def _confirmed_swing_high_before(rows: list[Candle], before_index: int,
                                *, wing: int = 2, lookback: int = 50) -> float | None:
    """Return the newest confirmed two-sided pivot high before ``before_index``.

    A pivot must be strictly higher than the two bars on each side. The bars
    confirming the right side must themselves precede the pullback, so no
    signal or pullback candle participates in selecting its invalidation level.
    """
    if wing < 1 or before_index > len(rows) or before_index < wing * 2 + 1:
        return None
    first = max(wing, before_index - lookback)
    last = before_index - wing - 1
    for pivot in range(last, first - 1, -1):
        left = rows[pivot-wing:pivot]
        right = rows[pivot+1:pivot+wing+1]
        if len(left) != wing or len(right) != wing:
            continue
        level = rows[pivot].high
        if all(level > candle.high for candle in (*left, *right)):
            return level
    return None


def _level_geometry_error(direction: str, entry_zone: tuple[float, float],
                          invalidation: float, stop_loss: float,
                          targets: tuple[float, ...]) -> str | None:
    """Validate entry, invalidation, stop and every target before emitting ENTRY."""
    values = (*entry_zone, invalidation, stop_loss, *targets)
    if (direction not in {"LONG", "SHORT"} or len(entry_zone) != 2 or
            not targets or any(not math.isfinite(value) or value <= 0 for value in values)):
        return "levels must be finite positive prices with a LONG/SHORT direction"
    low, high = entry_zone
    if low > high:
        return "entry zone lower bound exceeds its upper bound"
    if direction == "LONG":
        if invalidation >= low or stop_loss >= low:
            return "LONG invalidation and stop-loss must be below the entry zone"
        if any(target <= high for target in targets):
            return "every LONG take-profit must be above the entry zone"
        if any(a >= b for a, b in zip(targets, targets[1:])):
            return "LONG take-profit targets must be strictly ascending"
    else:
        if invalidation <= high or stop_loss <= high:
            return "SHORT invalidation and stop-loss must be above the entry zone"
        if any(target >= low for target in targets):
            return "every SHORT take-profit must be below the entry zone"
        if any(a <= b for a, b in zip(targets, targets[1:])):
            return "SHORT take-profit targets must be strictly descending"
    return None


def _confirmed_breakout_level(rows: list[Candle], volume_multiple: float) -> float | None:
    """Compatibility helper returning the level of a recent upside breakout."""
    event = _confirmed_breakout_event(rows, volume_multiple)
    return event[1] if event and event[0] == "LONG" else None


def _confirmed_breakout_event(rows: list[Candle], volume_multiple: float
                              ) -> tuple[str, float, int] | None:
    """Find the newest prior closed-bar breakout, with its direction and index.

    The range excludes its breakout candle; the current candle is excluded from
    breakout detection and can only serve as the retest candle.
    """
    for index in reversed(range(max(20, len(rows)-4), len(rows)-1)):
        window = rows[index-20:index]
        if len(window) != 20:
            continue
        resistance = max(c.high for c in window)
        support = min(c.low for c in window)
        average_volume = sum(c.volume for c in window) / 20
        candle = rows[index]
        if average_volume <= 0 or candle.volume <= average_volume * volume_multiple:
            continue
        if candle.close > resistance:
            return "LONG", resistance, index
        if candle.close < support:
            return "SHORT", support, index
    return None


def _no_trade(symbol: str, source: str, now: datetime, name: str, tfs: tuple[str, ...],
              reason: str, missing: tuple[str, ...], score, indicators: tuple[str, ...] = (),
              status: str = "observation", opposing: tuple[str, ...] = (),
              rejection_reasons: tuple[str, ...] = (),
              candidate_direction: str | None = None) -> StrategySignal:
    return StrategySignal(symbol, source, now, name, "NO TRADE", status, None, None,
                          None, None, None, (), None, None, None, tfs, indicators,
                          (reason,), opposing, score.total, score, missing, rejection_reasons,
                          candidate_direction=candidate_direction)


class StrategyEngine:
    """Evaluate configured strategies against closed candles and existing analysis."""

    NAMES = ("trend_following", "pullback", "breakout_retest", "momentum", "mean_reversion")

    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def evaluate(self, symbol: str, source: str, candles: dict[str, list[Candle]],
                 analyses: dict[str, Analysis], qualities: dict[str, DataQuality] | None = None,
                 *, now: datetime | None = None) -> tuple[StrategySignal, ...]:
        qualities = qualities or {}
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        estimated_cost = (2 * self.config.fee_bps_per_side +
                          2 * self.config.slippage_bps_per_side +
                          (self.config.spread_bps or 0.0))
        entry_tf = self.config.entry_timeframe
        higher_tf = self.config.higher_timeframe
        tfs = tuple(dict.fromkeys((higher_tf, entry_tf)))
        entry = candles.get(entry_tf, [])
        higher = candles.get(higher_tf, [])
        missing: list[str] = []
        for tf in tfs:
            if tf not in analyses:
                missing.append(f"analysis:{tf}")
            if len(candles.get(tf, [])) < 50:
                missing.append(f"at least 50 candles required on {tf}")
            if any(not c.complete for c in candles.get(tf, [])[-1:]):
                missing.append(f"latest {tf} candle is incomplete")
            quality = qualities.get(tf)
            if quality is None:
                missing.append(f"data quality report unavailable on {tf}")
            elif quality.exchange != source or quality.symbol != symbol or quality.timeframe != tf:
                missing.append(f"data quality report provenance mismatch on {tf}")
            # `incomplete_latest` describes the provider response, not necessarily
            # the candles handed to this engine. The CLI intentionally removes a
            # forming bar before analysis. The actual candle completeness check
            # above remains a hard block when an incomplete bar is passed in.
            if quality and not quality.acceptable:
                missing.append(f"data quality rejected on {tf}: {', '.join(quality.reasons)}")
        sources = {c.source for tf in tfs for c in candles.get(tf, [])}
        if len(sources) > 1 or sources != {source}:
            missing.append("candle source mismatch")
        common_parts = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                        1.0 if not missing else 0.0, 0.0)
        if missing or entry_tf not in analyses or higher_tf not in analyses:
            score = score_signal(common_parts, self.config.weights)
            blocked = tuple(_no_trade(symbol, source, now, name, tfs,
                "Entry is blocked because required closed candles, Market Analysis results, or acceptable data quality are missing.",
                tuple(missing), score, rejection_reasons=tuple(missing),
                candidate_direction=side)
                for name in self.config.enabled for side in ("LONG", "SHORT"))
            return tuple(replace(s, estimated_cost_bps=estimated_cost,
                                 estimated_slippage_bps=self.config.slippage_bps_per_side) for s in blocked)

        higher_analysis, entry_analysis = analyses[higher_tf], analyses[entry_tf]
        if higher_analysis.regime == "insufficient_data" or entry_analysis.regime == "insufficient_data":
            missing.append("Market Analysis reports insufficient data")
        try:
            ind = calculate_indicators(entry)
        except (AnalysisError, ValueError, ZeroDivisionError):
            missing.append("entry indicators could not be calculated")
            ind = None
        if missing or ind is None:
            score = score_signal(common_parts, self.config.weights)
            blocked = tuple(_no_trade(symbol, source, now, name, tfs,
                "Required analysis is insufficient.", tuple(missing), score,
                rejection_reasons=tuple(missing), candidate_direction=side)
                for name in self.config.enabled for side in ("LONG", "SHORT"))
            return tuple(replace(s, estimated_cost_bps=estimated_cost,
                                 estimated_slippage_bps=self.config.slippage_bps_per_side) for s in blocked)

        closes = [c.close for c in entry]
        volumes = [c.volume for c in entry]
        e20 = _ema_series(closes, 20)
        e12, e26 = _ema_series(closes, 12), _ema_series(closes, 26)
        macd = [a - b for a, b in zip(e12, e26)]
        macd_signal = _ema_series(macd, 9)
        hist = [a - b for a, b in zip(macd, macd_signal)]
        current, prev = entry[-1], entry[-2]
        up_structure, down_structure = _structure(entry)
        high_regime, entry_regime = higher_analysis.regime, entry_analysis.regime
        regimes_match = high_regime == entry_regime
        regime_side = {"bullish": "LONG", "bearish": "SHORT"}
        if self.config.aggressive_mode and high_regime in regime_side and entry_regime in regime_side:
            aligned_direction = regime_side.get(entry_regime)
            conflict = not regimes_match
        else:
            aligned_direction = regime_side.get(high_regime) if regimes_match else None
            conflict = not regimes_match
        volume_avg = sum(volumes[-21:-1]) / min(20, len(volumes[-21:-1]))
        relative_volume = current.volume / volume_avg if volume_avg > 0 else 0.0
        resistance = max(c.high for c in entry[-21:-1])
        support = min(c.low for c in entry[-21:-1])
        spread = self.config.spread_bps
        cost = 2 * self.config.fee_bps_per_side + 2 * self.config.slippage_bps_per_side + (spread or 0)
        opposing: list[str] = []
        if conflict:
            opposing.append(f"timeframe regimes conflict (higher={high_regime}, entry={entry_regime})")
        if high_regime == "sideways" and entry_regime == "sideways":
            opposing.append("both timeframes are sideways; only a confirmed breakout setup is eligible")
        if spread is None:
            opposing.append("spread unavailable; cost excludes spread")

        if ind.atr <= 0 or not math.isfinite(ind.atr):
            missing.append("ATR14 must be finite and positive to define directional risk")
            score = score_signal((0.0, 0.0, 0.0, 0.0, float(regimes_match),
                                  0.0, 1.0 if not missing else 0.0, 0.0), self.config.weights)
            return tuple(_no_trade(symbol, source, now, name, tfs,
                "Entry is blocked because volatility does not support a valid stop distance.",
                tuple(missing), score, rejection_reasons=tuple(missing),
                candidate_direction=side)
                for name in self.config.enabled for side in ("LONG", "SHORT"))

        long_macd = hist[-1] > hist[-2] and hist[-1] > 0
        short_macd = hist[-1] < hist[-2] and hist[-1] < 0
        long_rsi = 45 <= ind.rsi <= 70
        short_rsi = 30 <= ind.rsi <= 55
        extension = current.close - e20[-1]
        extension_atr = extension / ind.atr
        prior_up_move = prev.close > entry[-3].close
        prior_down_move = prev.close < entry[-3].close
        correction_start = len(entry) - 2
        while (correction_start > 1 and
               entry[correction_start].close > entry[correction_start - 1].close):
            correction_start -= 1
        prior_swing_high = _confirmed_swing_high_before(entry, correction_start)
        correction_high = max(c.high for c in entry[correction_start:-1]) if correction_start < len(entry)-1 else None
        correction_breached_swing = (prior_swing_high is not None and correction_high is not None and
                                     correction_high > prior_swing_high)
        swing_breach_reason = (f"pullback high {correction_high:g} exceeded confirmed prior swing high {prior_swing_high:g}"
                               if correction_breached_swing else
                               "pullback exceeded the confirmed prior swing high")
        near_ema_long = (abs(current.low - e20[-1]) <= 0.6 * ind.atr or
                         abs(current.close - e20[-1]) <= 0.4 * ind.atr)
        near_ema_short = (abs(current.high - e20[-1]) <= 0.6 * ind.atr or
                          abs(current.close - e20[-1]) <= 0.4 * ind.atr)

        breakout = _confirmed_breakout_event(entry, self.config.breakout_volume_multiple)
        breakout_side = breakout[0] if breakout else None
        breakout_level = breakout[1] if breakout else None
        breakout_index = breakout[2] if breakout else None
        breakout_age = len(entry) - 1 - breakout_index if breakout_index is not None else 0
        breakout_regime_ok = (regimes_match and (
            aligned_direction == breakout_side or
            (high_regime == "sideways" and entry_regime == "sideways"))) if breakout_side else False
        if breakout_side and breakout_index is not None:
            intermediate = entry[breakout_index + 1:-1]
            if breakout_side == "LONG":
                prior_retest = any(c.low <= breakout_level + .25*ind.atr and c.close > breakout_level
                                   for c in intermediate)
                invalidated_break = any(c.close <= breakout_level for c in intermediate)
                touched = current.low <= breakout_level + .25*ind.atr
                held = current.close > breakout_level
                directional_bar = current.close > current.open
                strong_close = current.high > current.low and current.close >= current.low + .7*(current.high-current.low)
            else:
                prior_retest = any(c.high >= breakout_level - .25*ind.atr and c.close < breakout_level
                                   for c in intermediate)
                invalidated_break = any(c.close >= breakout_level for c in intermediate)
                touched = current.high >= breakout_level - .25*ind.atr
                held = current.close < breakout_level
                directional_bar = current.close < current.open
                strong_close = current.high > current.low and current.close <= current.high - .7*(current.high-current.low)
        else:
            prior_retest = invalidated_break = touched = held = directional_bar = strong_close = False
        breakout_active = bool(
            breakout and 1 <= breakout_age <= 3 and breakout_regime_ok and
            not prior_retest and not invalidated_break and touched and held and
            directional_bar and strong_close and relative_volume >= 1.0)

        sample = closes[-20:]
        mean = sum(sample) / len(sample)
        sd = math.sqrt(sum((value-mean)**2 for value in sample) / len(sample))
        lower_band = mean - 2*sd
        sideways = high_regime == entry_regime == "sideways"
        mean_revert = (sideways and ind.rsi < 30 and prev.close < lower_band and
                       current.close > prev.close and current.close > current.open)

        # Each strategy reports one independent candidate per side. Directional
        # trend rules still require matching regimes; the opposite candidate gets
        # an explicit rejection instead of disappearing from diagnostics.
        outcomes: dict[tuple[str, str], tuple[bool, str, tuple[str, ...], tuple[str, ...]]] = {}
        evaluation_sides = ((aligned_direction, "SHORT" if aligned_direction == "LONG" else "LONG")
                            if aligned_direction else ("LONG", "SHORT"))
        for side in evaluation_sides:
            if aligned_direction != side:
                trend_failures = (f"timeframes do not align with {side} (higher={high_regime}, entry={entry_regime})",)
                pullback_failures = (f"pullback {side} requires matching {side.lower()} timeframes (higher={high_regime}, entry={entry_regime})",)
                momentum_failures = (f"momentum {side} requires matching {side.lower()} timeframes (higher={high_regime}, entry={entry_regime})",)
                trend_active = pullback_active = momentum_active = False
            elif side == "LONG":
                trend_failures = tuple(reason for failed, reason in (
                    (not up_structure, "higher-high / higher-low price structure was not confirmed"),
                    (not 50 <= ind.rsi <= 70, f"RSI14={ind.rsi:.2f} is outside the LONG trend range 50–70"),
                    (current.close <= prev.close, "latest close did not exceed the previous close")) if failed)
                trend_active = up_structure and 50 <= ind.rsi <= 70 and current.close > prev.close
                pullback_failures = tuple(reason for failed, reason in (
                    (not near_ema_long, "price is outside the configured EMA20 pullback zone"),
                    (current.close <= current.open, "pullback candle did not close bullish"),
                    (current.close <= prev.close, "recovery close did not exceed the previous close"),
                    (ind.rsi < 45, f"RSI14={ind.rsi:.2f} is below the pullback minimum 45")) if failed)
                pullback_active = near_ema_long and current.close > current.open and current.close > prev.close and ind.rsi >= 45
                momentum_failures = tuple(reason for failed, reason in (
                    (not long_macd, "MACD histogram is not positive and rising"),
                    (not 50 <= ind.rsi <= 68, f"RSI14={ind.rsi:.2f} is outside the LONG momentum range 50–68"),
                    (relative_volume < 1.2, f"relative volume={relative_volume:.2f} is below 1.2"),
                    (not 0 <= extension_atr <= 2.0, f"EMA20 extension={extension_atr:.2f} ATR is outside LONG range 0–2")) if failed)
                momentum_active = long_macd and 50 <= ind.rsi <= 68 and relative_volume >= 1.2 and 0 <= extension_atr <= 2.0
            else:
                trend_failures = tuple(reason for failed, reason in (
                    (not down_structure, "lower-high / lower-low price structure was not confirmed"),
                    (not 30 <= ind.rsi <= 50, f"RSI14={ind.rsi:.2f} is outside the SHORT trend range 30–50"),
                    (current.close >= prev.close, "latest close did not fall below the previous close")) if failed)
                trend_active = down_structure and 30 <= ind.rsi <= 50 and current.close < prev.close
                pullback_failures = tuple(reason for failed, reason in (
                    (not near_ema_short, "corrective rally did not reach the ATR-bounded EMA20 zone"),
                    (not prior_up_move, "no preceding upward corrective move was confirmed"),
                    (prior_up_move and prior_swing_high is None, "insufficient prior closed candles to establish a confirmed swing high before the pullback"),
                    (correction_breached_swing, swing_breach_reason),
                    (current.close >= current.open or current.close >= prev.close, "no bearish rejection close confirmed"),
                    (ind.rsi > 55, f"RSI14={ind.rsi:.2f} is too strong for SHORT continuation"),
                    (extension_atr < -2.0, f"price is already {extension_atr:.2f} ATR below EMA20")) if failed)
                pullback_active = (near_ema_short and prior_up_move and current.close < current.open and
                    current.close < prev.close and ind.rsi <= 55 and extension_atr >= -2.0 and
                    prior_swing_high is not None and not correction_breached_swing)
                momentum_failures = tuple(reason for failed, reason in (
                    (not short_macd, "MACD histogram is not negative and falling"),
                    (not 32 <= ind.rsi <= 50, f"RSI14={ind.rsi:.2f} is outside the SHORT momentum range 32–50"),
                    (relative_volume < 1.2, f"relative volume={relative_volume:.2f} is below 1.2"),
                    (not -2.0 <= extension_atr <= 0, f"EMA20 extension={extension_atr:.2f} ATR is outside SHORT range -2–0")) if failed)
                momentum_active = short_macd and 32 <= ind.rsi <= 50 and relative_volume >= 1.2 and -2.0 <= extension_atr <= 0
            outcomes["trend_following", side] = (trend_active,
                "Trend following requires matching directional regimes, confirmed swing structure, a non-exhausted RSI and a confirming closed candle.",
                ("RSI14", "MACD histogram", "ATR14", "higher/lower timeframe regime", "directional swing structure"), trend_failures)
            outcomes["pullback", side] = (pullback_active,
                "Pullback requires an aligned trend, an ATR-bounded EMA area and a closed recovery/rejection candle; EMA contact alone is insufficient.",
                ("EMA20 pullback distance in ATR", "confirmed prior swing high", "RSI14", "ATR14", "trend regime", "closed confirmation candle"), pullback_failures)
            outcomes["momentum", side] = (momentum_active,
                "Momentum requires directionally aligned MACD, a non-exhausted RSI, relative volume ≥1.2 and an EMA20 extension no greater than 2 ATR.",
                ("RSI14", "MACD histogram", "relative volume", "EMA20 extension in ATR", "trend regime"), momentum_failures)

            if breakout is None:
                breakout_failures = ("no previous volume-confirmed close breakout outside the prior 20-bar range",)
            elif breakout_side != side:
                breakout_failures = (f"confirmed breakout is {breakout_side}; no {side} breakout setup exists",)
            else:
                breakout_failures = tuple(reason for failed, reason in (
                    (breakout_age < 1 or breakout_age > 3, "confirmed breakout is outside the 1–3 candle retest window"),
                    (not breakout_regime_ok, f"breakout direction conflicts with timeframe regimes (higher={high_regime}, entry={entry_regime})"),
                    (invalidated_break, "price closed back across the breakout level before retest"),
                    (prior_retest, "this confirmed breakout already had a retest; it cannot be reused"),
                    (not touched, "price did not retest the breakout level within 0.25 ATR"),
                    (not held, "retest candle did not close back on the breakout side"),
                    (not directional_bar, "retest candle did not close in the breakout direction"),
                    (not strong_close, "retest candle close was not in the directional 30% of its range"),
                    (relative_volume < 1.0, f"retest relative volume={relative_volume:.2f} is below 1.0")) if failed)
            outcomes["breakout_retest", side] = (breakout_side == side and breakout_active,
                "Breakout/retest requires a previous volume-confirmed close outside the prior 20-bar range, one timely retest and a directional close with volume.",
                ("20-bar prior range", "breakout close and volume", "relative volume", "retest and hold", "ATR14"), breakout_failures)

            upper_band = mean + 2*sd
            if side == "LONG":
                mean_active = mean_revert
                mean_failures = tuple(reason for failed, reason in (
                    (not sideways, f"mean reversion requires sideways regimes (higher={high_regime}, entry={entry_regime})"),
                    (ind.rsi >= 30, f"RSI14={ind.rsi:.2f} is not below 30"),
                    (prev.close >= lower_band, "previous close did not pierce the 20-bar lower band"),
                    (current.close <= prev.close or current.close <= current.open, "no bullish rejection followed the lower-band excursion")) if failed)
            else:
                mean_active = (sideways and ind.rsi > 70 and prev.close > upper_band and
                               current.close < prev.close and current.close < current.open)
                mean_failures = tuple(reason for failed, reason in (
                    (not sideways, f"mean reversion requires sideways regimes (higher={high_regime}, entry={entry_regime})"),
                    (ind.rsi <= 70, f"RSI14={ind.rsi:.2f} is not above 70"),
                    (prev.close <= upper_band, "previous close did not pierce the 20-bar upper band"),
                    (current.close >= prev.close or current.close >= current.open, "no bearish rejection followed the upper-band excursion")) if failed)
            outcomes["mean_reversion", side] = (mean_active,
                f"Mean reversion {side} requires sideways regimes, an outer-band excursion and a directional rejection candle.",
                ("sideways regime on both timeframes", "RSI14", "20-bar Bollinger band", "rejection candle"), mean_failures)

        def score_for(direction: str, strategy_name: str, active: bool,
                      rr_component: float = 0.0) -> object:
            side = direction
            bullish = side == "LONG"
            matching_regime = aligned_direction == side
            trend_component = float(matching_regime)
            if strategy_name == "breakout_retest" and sideways:
                trend_component = 0.5
            structure_component = float(up_structure if bullish else down_structure)
            if strategy_name == "breakout_retest" and breakout_side == side:
                structure_component = 1.0
            macd_component = float(long_macd if bullish else short_macd)
            rsi_ranges = {
                "trend_following": ((50, 70), (30, 50)),
                "pullback": ((45, 70), (30, 55)),
                "breakout_retest": ((45, 70), (30, 55)),
                "momentum": ((50, 68), (32, 50)),
                "mean_reversion": ((0, 30), (70, 100)),
            }
            long_range, short_range = rsi_ranges[strategy_name]
            selected_range = long_range if bullish else short_range
            rsi_component = float(selected_range[0] <= ind.rsi <= selected_range[1])
            return score_signal(directional_score_parts(
                trend_alignment=trend_component,
                price_structure=structure_component,
                macd_alignment=macd_component,
                rsi_alignment=rsi_component,
                relative_volume=relative_volume,
                timeframe_alignment=float(regimes_match),
                risk_reward=rr_component,
                data_quality=1.0,
                strategy_conditions=float(active)), self.config.weights)

        results: list[StrategySignal] = []
        price = current.close
        for name in self.config.enabled:
          for direction in evaluation_sides:
            active, condition_text, used_indicators, rule_rejections = outcomes[name, direction]
            score = score_for(direction, name, active)
            if not active:
                reasons = rule_rejections or ("strategy setup conditions were not met",)
                results.append(_no_trade(symbol, source, now, name, tfs, condition_text,
                    (), score, used_indicators, opposing=tuple(opposing),
                    rejection_reasons=reasons, candidate_direction=direction))
                continue

            is_long = direction == "LONG"
            invalidation = (min(support, current.low - .25*ind.atr) if is_long else
                            max(max(c.high for c in entry[-6:-1]), current.high + .25*ind.atr))
            risk = price - invalidation if is_long else invalidation - price
            if risk <= 0 or risk > self.config.max_risk_atr * ind.atr:
                reason = "invalidation distance is nonpositive or exceeds configured ATR risk"
                score = score_for(direction, name, active)
                results.append(_no_trade(symbol, source, now, name, tfs,
                    "Risk cannot be defined within configured ATR limits.", (), score,
                    used_indicators, opposing=tuple(opposing), rejection_reasons=(reason,),
                    candidate_direction=direction))
                continue

            # The breakout candle itself is not a valid opposing target level: it
            # is part of the move being traded. With no independent level beyond
            # the retest, use the configured R multiple as the target.
            breakout_trade = name == "breakout_retest"
            obstacle = resistance if is_long and resistance > price and not breakout_trade else (
                support if not is_long and support < price else None)
            if breakout_trade:
                obstacle = None
            obstacle_room = obstacle - price if is_long and obstacle else (
                price - obstacle if not is_long and obstacle else None)
            if obstacle_room is not None and obstacle_room < 1.5*risk:
                reason = (f"opposing level at {obstacle:g} is less than 1.5R away")
                score = score_for(direction, name, active)
                results.append(_no_trade(symbol, source, now, name, tfs,
                    "An opposing support/resistance level leaves insufficient room for the configured risk/reward.",
                    (), score, used_indicators, rejection_reasons=(reason,), candidate_direction=direction))
                continue

            target = obstacle if obstacle is not None and obstacle_room > risk else (
                price + 2*risk if is_long else price - 2*risk)
            target2 = price + 3*risk if is_long else price - 3*risk
            if target <= 0 or target2 <= 0:
                reason = "calculated take-profit level is nonpositive"
                results.append(_no_trade(symbol, source, now, name, tfs,
                    "Directional targets cannot be represented as positive prices.", (),
                    score_for(direction, name, active), used_indicators,
                    rejection_reasons=(reason,), candidate_direction=direction))
                continue
            reward = target - price if is_long else price - target
            cost_amount = price * cost / 10_000
            rr = (reward - cost_amount) / (risk + cost_amount)
            if rr < self.config.min_net_risk_reward:
                reason = f"net reward/risk {rr:.2f} is below configured {self.config.min_net_risk_reward:.2f}"
                results.append(_no_trade(symbol, source, now, name, tfs,
                    "Potential reward is too small after estimated costs.", (),
                    score_for(direction, name, active,
                        max(0.0, min(1.0, rr/self.config.min_net_risk_reward)),),
                    used_indicators, opposing=(reason,), rejection_reasons=(reason,),
                    candidate_direction=direction))
                continue

            rr_component = min(1.0, rr / (self.config.min_net_risk_reward * 1.5))
            score = score_for(direction, name, active, rr_component)
            if score.total < self.config.min_score:
                reason = f"weighted score {score.total:.1f} is below configured threshold {self.config.min_score:.1f}"
                results.append(_no_trade(symbol, source, now, name, tfs,
                    f"Weighted score {score.total:.1f} is below configured threshold {self.config.min_score:.1f}.",
                    (), score, used_indicators, rejection_reasons=(reason,), candidate_direction=direction))
                continue

            entry_zone = (price - .1*ind.atr, price + .1*ind.atr)
            stop = invalidation
            target_levels = (target, target2)
            geometry_error = _level_geometry_error(direction, entry_zone, invalidation, stop, target_levels)
            if geometry_error:
                reason = f"calculated {direction} stop/target geometry is invalid: {geometry_error}"
                results.append(_no_trade(symbol, source, now, name, tfs,
                    "Calculated levels fail the directional price geometry check.", (),
                    score, used_indicators, rejection_reasons=(reason,), candidate_direction=direction))
                continue

            side_words = ("below local support" if is_long else "above local resistance")
            results.append(StrategySignal(symbol, source, current.timestamp, name, direction,
                "conditions_met", entry_zone, "calculated", invalidation, "calculated",
                stop, target_levels, round(rr, 3), round(cost, 2),
                self.config.slippage_bps_per_side, tfs, used_indicators,
                (condition_text,
                 f"{direction} entry centered on last closed price {price:g}; stop/invalidation {side_words} with an ATR buffer.",
                 f"Estimated round-trip costs {cost:.1f} bps; reward/risk is net of configured cost estimate."),
                tuple(opposing), score.total, score, action="ENTRY", position_side=direction,
                candidate_direction=direction))

        return tuple(replace(s, estimated_cost_bps=estimated_cost,
                             estimated_slippage_bps=self.config.slippage_bps_per_side)
                     if s.estimated_cost_bps is None else s for s in results)


__all__ = ["StrategyConfig", "StrategyEngine"]

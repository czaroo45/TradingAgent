"""Configurable, explainable signal scoring (not a probability estimate)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from tradingagent.models import ScoreBreakdown


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    trend_alignment: float = 1.0
    price_structure: float = 1.0
    momentum: float = 1.0
    volume: float = 0.75
    timeframe_alignment: float = 1.0
    risk_reward: float = 1.0
    data_quality: float = 1.0
    strategy_conditions: float = 1.0

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value < 0 for value in self.values()) or sum(self.values()) <= 0:
            raise ValueError("score weights must be nonnegative and have a positive sum")

    def values(self) -> tuple[float, ...]:
        return (self.trend_alignment, self.price_structure, self.momentum,
                self.volume, self.timeframe_alignment, self.risk_reward,
                self.data_quality, self.strategy_conditions)


def score_signal(parts: tuple[float, ...], weights: ScoreWeights = ScoreWeights()) -> ScoreBreakdown:
    if len(parts) != 8 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in parts):
        raise ValueError("score components must contain eight values in [0, 1]")
    total_weight = sum(weights.values())
    total = 100 * sum(value * weight for value, weight in zip(parts, weights.values())) / total_weight
    return ScoreBreakdown(*parts, round(total, 2))


def directional_score_parts(*, trend_alignment: float, price_structure: float,
                            macd_alignment: float, rsi_alignment: float,
                            relative_volume: float, timeframe_alignment: float,
                            risk_reward: float, data_quality: float,
                            strategy_conditions: float) -> tuple[float, ...]:
    """Build the existing eight score components for one candidate direction.

    Volume remains a magnitude measure, while direction is represented by regime,
    structure, MACD and RSI alignment. ATR is reflected in the risk/reward value
    supplied by the strategy after it has set an ATR/structure-based invalidation.
    """
    values = (trend_alignment, price_structure,
              (macd_alignment + rsi_alignment) / 2,
              min(1.0, max(0.0, relative_volume) / 1.5),
              timeframe_alignment, risk_reward, data_quality,
              strategy_conditions)
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise ValueError("directional score components must be finite and in [0, 1]")
    return values


__all__ = ["ScoreWeights", "directional_score_parts", "score_signal"]

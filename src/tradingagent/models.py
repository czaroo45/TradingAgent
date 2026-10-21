"""Shared immutable domain models for market analysis."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = "unknown"
    complete: bool = True

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class Indicators:
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    latest_close: float
    average_volume: float


Regime = Literal["bullish", "bearish", "sideways", "insufficient_data"]
Direction = Literal["long", "short"]
PositionSide = Literal["LONG", "SHORT"]
SignalAction = Literal["ENTRY", "EXIT", "NO_TRADE"]
ExecutionSide = Literal["buy", "sell"]
ExecutionRole = Literal["ENTRY", "EXIT"]


@dataclass(frozen=True, slots=True)
class TradeScenario:
    direction: Direction
    entry_low: float
    entry_high: float
    invalidation: float
    stop_loss: float
    targets: tuple[float, ...]
    estimated_cost_bps: float
    risk_reward: float


@dataclass(frozen=True, slots=True)
class Analysis:
    symbol: str
    timeframe: str
    regime: Regime
    signal_quality: int
    explanation: tuple[str, ...]
    indicators: Indicators | None
    scenario: TradeScenario | None
    candle_time: datetime | None


@dataclass(frozen=True, slots=True)
class MarketTicker:
    exchange: str
    symbol: str
    last: float | None
    bid: float | None
    ask: float | None
    spread: float | None
    base_volume: float | None
    quote_volume: float | None
    exchange_timestamp_ms: int | None
    received_timestamp_ms: int
    stale: bool = False
    bid_volume: float | None = None
    ask_volume: float | None = None

    @property
    def spread_bps(self) -> float | None:
        if self.spread is None or self.bid is None or self.ask is None:
            return None
        mid = (self.bid + self.ask) / 2
        return self.spread / mid * 10_000 if mid > 0 else None


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    exchange: str
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    exchange_timestamp_ms: int | None
    received_timestamp_ms: int
    stale: bool = False

    @property
    def spread_bps(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        mid = (self.bids[0][0] + self.asks[0][0]) / 2
        return (self.asks[0][0] - self.bids[0][0]) / mid * 10_000 if mid > 0 else None

    def estimate_slippage_bps(self, side: str, quote_notional: float) -> float | None:
        """Estimate book-walk impact against best price for a quote-currency order size."""
        if side not in {"buy", "sell"} or quote_notional <= 0:
            raise ValueError("side must be buy/sell and quote notional must be positive")
        if self.stale:
            return None
        levels = self.asks if side == "buy" else self.bids
        if not levels:
            return None
        remaining = quote_notional
        base_filled = 0.0
        quote_filled = 0.0
        for price, amount in levels:
            available_quote = price * amount
            used_quote = min(remaining, available_quote)
            base_filled += used_quote / price
            quote_filled += used_quote
            remaining -= used_quote
            if remaining <= 1e-9:
                break
        if remaining > 1e-9 or base_filled <= 0:
            return None
        vwap = quote_filled / base_filled
        best = levels[0][0]
        return abs(vwap - best) / best * 10_000


@dataclass(frozen=True, slots=True)
class DataQuality:
    exchange: str
    symbol: str
    timeframe: str
    checked_at_ms: int
    candle_count: int
    duplicate_timestamps: tuple[int, ...] = ()
    non_monotonic: bool = False
    gaps: tuple[tuple[int, int], ...] = ()
    invalid_rows: tuple[str, ...] = ()
    stale: bool = False
    stale_by_ms: int = 0
    incomplete_latest: bool = False
    source_mismatch: bool = False
    pagination_truncated: bool = False

    @property
    def acceptable(self) -> bool:
        return not (self.duplicate_timestamps or self.non_monotonic or self.gaps or
                    self.invalid_rows or self.stale or self.source_mismatch or
                    self.pagination_truncated)

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = []
        if self.duplicate_timestamps: reasons.append("duplicate timestamps")
        if self.non_monotonic: reasons.append("non-monotonic timestamps")
        if self.gaps: reasons.append("missing candles")
        if self.invalid_rows: reasons.append("invalid OHLCV rows")
        if self.stale: reasons.append(f"stale data by {self.stale_by_ms} ms")
        if self.source_mismatch: reasons.append("source mismatch")
        if self.pagination_truncated: reasons.append("history pagination limit reached")
        return tuple(reasons)

    @property
    def warnings(self) -> tuple[str, ...]:
        return ("latest candle is incomplete",) if self.incomplete_latest else ()


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    trend_alignment: float
    price_structure: float
    momentum: float
    volume: float
    timeframe_alignment: float
    risk_reward: float
    data_quality: float
    strategy_conditions: float
    total: float


@dataclass(frozen=True, slots=True)
class StrategySignal:
    symbol: str
    source: str
    timestamp: datetime
    strategy: str
    # `direction` is retained as the legacy public field. New consumers should
    # use `action` and `position_side`, which keep an exit separate from a side.
    direction: Literal["LONG", "SHORT", "EXIT", "NO TRADE"]
    status: Literal["observation", "conditions_met", "invalidated"]
    entry_zone: tuple[float, float] | None
    entry_zone_basis: Literal["calculated", "illustrative"] | None
    invalidation: float | None
    invalidation_basis: Literal["calculated", "illustrative"] | None
    stop_loss: float | None
    targets: tuple[float, ...]
    risk_reward: float | None
    estimated_cost_bps: float | None
    estimated_slippage_bps: float | None
    timeframes: tuple[str, ...]
    indicators: tuple[str, ...]
    explanation: tuple[str, ...]
    opposing_factors: tuple[str, ...]
    confidence: float
    score: ScoreBreakdown
    missing_data: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    action: SignalAction | None = None
    position_side: PositionSide | None = None
    # Direction evaluated when the outcome is NO_TRADE. This lets analysis
    # report LONG and SHORT independently without making rejected candidates
    # actionable or changing the legacy `direction` field.
    candidate_direction: PositionSide | None = None

    def __post_init__(self) -> None:
        directions = {"LONG", "SHORT", "EXIT", "NO TRADE"}
        statuses = {"observation", "conditions_met", "invalidated"}
        actions = {"ENTRY", "EXIT", "NO_TRADE"}
        sides = {"LONG", "SHORT"}
        if self.direction not in directions:
            raise ValueError(f"unsupported legacy signal direction: {self.direction!r}")
        if self.status not in statuses:
            raise ValueError(f"unsupported signal status: {self.status!r}")

        inferred_action = {
            "LONG": "ENTRY", "SHORT": "ENTRY", "EXIT": "EXIT", "NO TRADE": "NO_TRADE"
        }[self.direction]
        action = inferred_action if self.action is None else self.action
        side = self.position_side
        candidate = self.candidate_direction
        if side is None and self.direction in sides:
            side = self.direction
        if action not in actions:
            raise ValueError(f"unsupported signal action: {action!r}")
        if side is not None and side not in sides:
            raise ValueError(f"unsupported position side: {side!r}")
        if action != inferred_action:
            raise ValueError("signal action conflicts with the legacy direction field")
        if action == "ENTRY" and side != self.direction:
            raise ValueError("entry signal position_side must match LONG or SHORT direction")
        if action == "NO_TRADE" and side is not None:
            raise ValueError("NO_TRADE signals cannot carry a position side")
        if candidate is not None and candidate not in sides:
            raise ValueError("candidate_direction must be LONG or SHORT")
        if action == "ENTRY" and candidate not in {None, side}:
            raise ValueError("entry candidate_direction must match its position side")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "position_side", side)

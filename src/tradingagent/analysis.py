"""Deterministic indicators, regime labels, and conditional trade scenarios."""
from __future__ import annotations

import math
from datetime import timezone

from tradingagent.models import Analysis, Candle, DataQuality, Indicators, MarketTicker, TradeScenario


class AnalysisError(ValueError):
    pass


def _ema(values: list[float], period: int) -> float:
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1 - alpha) * value
    return value


def calculate_indicators(candles: list[Candle]) -> Indicators:
    if len(candles) < 50:
        raise AnalysisError("At least 50 candles are required")
    if any(not candle.complete for candle in candles):
        raise AnalysisError("Indicators require closed candles only")
    if any(b.timestamp_ms <= a.timestamp_ms for a,b in zip(candles,candles[1:])):
        raise AnalysisError("Candle timestamps must be strictly increasing")
    if any(not all(math.isfinite(value) for value in
                   (c.open,c.high,c.low,c.close,c.volume)) or
           min(c.open,c.high,c.low,c.close) <= 0 or c.volume < 0 or
           c.high < max(c.open,c.close,c.low) or c.low > min(c.open,c.close,c.high)
           for c in candles):
        raise AnalysisError("Candles contain invalid OHLCV values")
    closes = [c.close for c in candles]
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    period = 14
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain, avg_loss = sum(gains[:period]) / period, sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    true_ranges = [max(current.high-current.low, abs(current.high-previous.close),
                        abs(current.low-previous.close))
                   for previous,current in zip(candles,candles[1:])]
    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return Indicators(fast, slow, rsi, atr, closes[-1], sum(c.volume for c in candles[-20:]) / 20)


def analyze(symbol: str, timeframe: str, candles: list[Candle], fee_bps: float = 10,
            slippage_bps: float = 5, max_spread_bps: float = 20,
            quality: DataQuality | None = None, ticker: MarketTicker | None = None,
            estimated_slippage_bps: float | None = None) -> Analysis:
    if quality is not None and not quality.acceptable:
        return Analysis(symbol, timeframe, "insufficient_data", 0,
                        ("Signal blocked by market data quality: " + ", ".join(quality.reasons),),
                        None, None, None)
    if ticker is not None and ticker.stale:
        return Analysis(symbol, timeframe, "insufficient_data", 0,
                        ("Signal blocked: ticker quote is stale.",), None, None, None)
    if ticker is not None and (ticker.symbol != symbol or
                               (quality is not None and ticker.exchange != quality.exchange)):
        return Analysis(symbol, timeframe, "insufficient_data", 0,
                        ("Signal blocked: ticker provenance does not match the analysis.",), None, None, None)
    if len(candles) < 50:
        return Analysis(symbol, timeframe, "insufficient_data", 0, ("At least 50 complete candles are required.",), None, None, None)
    if not candles[-1].complete:
        return Analysis(symbol, timeframe, "insufficient_data", 0,
                        ("Signal blocked: latest candle is still forming.",), None, None, None)
    ind = calculate_indicators(candles)
    spread = (candles[-1].high - candles[-1].low) / ind.latest_close * 10_000
    quote_spread_bps = None
    if ticker and ticker.bid and ticker.ask and ticker.ask >= ticker.bid:
        midpoint = (ticker.ask + ticker.bid) / 2
        quote_spread_bps = (ticker.ask - ticker.bid) / midpoint * 10_000
    effective_spread_bps = quote_spread_bps if quote_spread_bps is not None else spread
    effective_slippage = slippage_bps if estimated_slippage_bps is None else estimated_slippage_bps
    cost_bps = 2 * fee_bps + 2 * effective_slippage + effective_spread_bps
    gap = (ind.ema_fast - ind.ema_slow) / ind.latest_close
    if gap > 0.003 and ind.latest_close > ind.ema_slow:
        regime = "bullish"
    elif gap < -0.003 and ind.latest_close < ind.ema_slow:
        regime = "bearish"
    else:
        regime = "sideways"
    reasons = [f"EMA12/EMA26 relative gap: {gap:.2%}.", f"RSI14: {ind.rsi:.1f}; ATR14: {ind.atr:.6g}.",
               f"Estimated round-trip costs: {cost_bps:.1f} bps (fee, slippage and spread)."]
    if estimated_slippage_bps is None:
        reasons.append(f"Slippage uses configured allowance: {slippage_bps:.1f} bps per side (book depth unavailable).")
    else:
        reasons.append(f"Slippage is estimated from order book depth: {effective_slippage:.2f} bps per side.")
    scenario = None
    quality = 0
    direction = "long" if regime == "bullish" else "short" if regime == "bearish" else None
    rsi_ok = (direction == "long" and 50 <= ind.rsi < 75) or (direction == "short" and 25 <= ind.rsi <= 50)
    if direction and rsi_ok and ind.atr > 0:
        close = ind.latest_close
        entry_low, entry_high = close - 0.2 * ind.atr, close + 0.2 * ind.atr
        stop = close - 1.5 * ind.atr if direction == "long" else close + 1.5 * ind.atr
        risk = abs(close - stop)
        targets = (close + 2 * risk, close + 3 * risk) if direction == "long" else (close - 2 * risk, close - 3 * risk)
        # Spread is approximated by candle range. This is deliberately conservative,
        # not an order-book quote; the scenario is withheld when that proxy is wide.
        if effective_spread_bps <= max_spread_bps:
            scenario = TradeScenario(direction, entry_low, entry_high, stop, stop, targets, cost_bps, (2 * risk - close * cost_bps / 10_000) / (risk + close * cost_bps / 10_000))
            quality = min(90, 55 + (15 if abs(gap) > 0.006 else 0) + (10 if 40 <= ind.rsi <= 60 else 0) + (10 if ind.average_volume > 0 else 0))
            spread_origin = "ticker bid/ask" if quote_spread_bps is not None else "candle range proxy (bid/ask unavailable)"
            reasons.append(f"Conditional {direction} setup: entry near ATR zone, invalidation/stop at 1.5 ATR, targets at 2R and 3R.")
            reasons.append(f"Estimated spread: {effective_spread_bps:.2f} bps from {spread_origin}.")
        else:
            reasons.append("No scenario: recent candle range is too wide relative to configured spread allowance.")
    else:
        reasons.append("No entry scenario: regime/RSI conditions are not aligned; no forecast is asserted.")
    return Analysis(symbol, timeframe, regime, quality, tuple(reasons), ind, scenario,
                    candles[-1].timestamp.astimezone(timezone.utc))

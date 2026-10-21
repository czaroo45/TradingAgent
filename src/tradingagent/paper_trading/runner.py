"""Fail-closed orchestration from validated closed candles into paper execution."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from tradingagent.analysis import analyze
from tradingagent.market_data import timeframe_to_ms, validate_ohlcv
from tradingagent.models import Candle
from tradingagent.paper_trading.engine import PaperTradeRejected
from tradingagent.risk import InstrumentConstraints, RiskManager, RiskRequest
from tradingagent.scoring import ScoreWeights
from tradingagent.strategies import StrategyConfig, StrategyEngine


def run_paper_cycle(*, symbol: str, exchange: str, histories: dict[str,list[Candle]], settings,
                    engine, risk_manager: RiskManager, amount_step: float,
                    market_limits: dict[str, float] | None = None,
                    now_ms: int | None = None) -> dict:
    """Validate -> indicators/analysis -> rules/scoring -> risk -> paper fills.

    Histories can include the current forming entry candle solely as next-open fill data;
    every signal and indicator uses complete bars only. This function never reaches an exchange.
    """
    now_ms=now_ms or int(datetime.now(timezone.utc).timestamp()*1000)
    entry_tf=settings.strategy_entry_timeframe; higher_tf=settings.strategy_higher_timeframe
    required=tuple(dict.fromkeys((entry_tf,higher_tf)))
    closed={};qualities={};analyses={};issues=[]
    for tf in required:
        rows=histories.get(tf,[])
        closed_rows=[c for c in rows if c.complete]
        if not closed_rows:
            issues.append(f"no complete {tf} candles");continue
        # The storage layer records provider provenance (ccxt), while analysis
        # records the venue (for example, binance). Reject mixed provider data,
        # then normalize validated bars to the venue identity used by strategies.
        sources={c.source for c in rows}
        validated,q=validate_ohlcv([[c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume] for c in closed_rows],
            exchange,symbol,tf,now_ms)
        if len(sources)!=1 or not sources.issubset({"ccxt",exchange}):
            q=replace(q,source_mismatch=True)
        qualities[tf]=q;closed[tf]=[replace(c,source=exchange) for c in validated if c.complete]
        if not q.acceptable:
            issues.extend(f"{tf}: {reason}" for reason in q.reasons)
            risk_manager.breaker.trip(f"unhealthy {tf} market data for {symbol}")
        if q.acceptable and len(closed[tf])>=50:
            analyses[tf]=analyze(symbol,tf,closed[tf],settings.fee_bps,settings.slippage_bps,
                                 settings.max_spread_bps,quality=q)
    for symbol_bar in histories.get(entry_tf,[]):
        if symbol_bar.complete and qualities.get(entry_tf) and qualities[entry_tf].acceptable:
            try: engine.process_symbol_bar(symbol,replace(symbol_bar,source=exchange),amount_step)
            except Exception as exc: issues.append(f"position management rejected: {type(exc).__name__}")
    config=StrategyConfig(enabled=settings.enabled_strategies,aggressive_mode=settings.strategy_aggressive_mode,
        entry_timeframe=entry_tf,higher_timeframe=higher_tf,fee_bps_per_side=settings.fee_bps,
        slippage_bps_per_side=settings.slippage_bps,spread_bps=settings.paper_spread_bps,
        min_net_risk_reward=settings.strategy_min_net_risk_reward,min_score=settings.strategy_min_score,
        weights=ScoreWeights(*settings.score_weights))
    signals=StrategyEngine(config).evaluate(symbol,exchange,closed,analyses,qualities)
    entry_rows=histories.get(entry_tf,[])
    closed_entry=closed.get(entry_tf,[])
    fill=None;decisions=[];entry_results=[]
    next_bar=next((replace(c,source=exchange) for c in entry_rows if closed_entry and
        c.timestamp_ms==closed_entry[-1].timestamp_ms+timeframe_to_ms(entry_tf)),None)
    for generated in signals:
        signal=replace(generated,timestamp=closed_entry[-1].timestamp) if closed_entry else generated
        engine.record_signal(signal)
        candidate = signal.candidate_direction or signal.position_side
        if signal.direction not in {"LONG","SHORT"} or signal.status!="conditions_met":
            entry_results.append({"strategy":signal.strategy,"candidate_direction":candidate,
                "status":"no_setup","reason":"; ".join(signal.rejection_reasons or signal.missing_data) or "strategy conditions were not met"})
            continue
        def reject(stage: str, reason: str) -> None:
            entry_results.append({"strategy":signal.strategy,"candidate_direction":candidate,
                "status":"rejected","reason":reason})
            engine.record_entry_rejection(signal,now_ms,stage,reason)
        if not all(qualities.get(tf) and qualities[tf].acceptable for tf in required):
            reason="entry blocked: unhealthy data quality"
            issues.append(reason);reject("data_quality",reason);continue
        if next_bar is None:
            reason="entry pending: next candle open unavailable"
            issues.append(reason);reject("next_bar",reason);continue
        age=now_ms-(closed_entry[-1].timestamp_ms+timeframe_to_ms(entry_tf))
        if age<0:
            reason="entry blocked: next candle timestamp is in the future"
            issues.append(reason);reject("timestamp",reason);continue
        if fill is not None or engine.position(symbol) is not None:
            reason="entry blocked: this symbol already has an open paper position; hedging is disabled"
            issues.append(reason);reject("position_limit",reason);continue
        prices={symbol:next_bar.open}
        request=RiskRequest(signal,engine.portfolio_for_risk(prices,now_ms),
            InstrumentConstraints(settings.paper_quote_currency,amount_step,
                min_amount=(market_limits or {}).get("min_amount",0.0),
                min_notional=(market_limits or {}).get("min_notional",0.0),
                liquidity_notional_cap=next_bar.volume*next_bar.open*0.01),
            max(0,age),data_healthy=True,fee_bps_round_trip=2*settings.fee_bps,
            slippage_bps_entry=settings.slippage_bps+settings.paper_spread_bps/2,
            slippage_bps_stop=settings.slippage_bps+settings.paper_spread_bps/2)
        decision=risk_manager.evaluate(request)
        decisions.append(decision)
        engine.record_risk_decision(decision,now_ms)
        if not decision.accepted:
            reasons="; ".join(decision.reasons)
            issues.extend(f"risk rejected: {reason}" for reason in decision.reasons)
            entry_results.append({"strategy":signal.strategy,"candidate_direction":candidate,
                "status":"rejected","reason":reasons or "risk manager rejected the candidate"})
            continue
        try:
            fill=engine.execute_entry(signal,risk_manager,request,next_bar,amount_step,prices,
                analyses.get(entry_tf).regime if analyses.get(entry_tf) else "unknown")
            entry_results.append({"strategy":signal.strategy,"candidate_direction":candidate,
                "status":"opened","reason":"paper fill completed","fill_id":fill.fill_id,
                "timestamp_ms":fill.timestamp_ms})
        except PaperTradeRejected as exc:
            reason=str(exc)
            issues.append(f"paper execution rejected: {reason}")
            reject("execution",reason)
        except Exception as exc:
            reason=f"paper execution failed: {type(exc).__name__}"
            issues.append(reason);reject("execution",reason)
    wait_reasons=[]
    for signal in signals:
        if signal.direction == "NO TRADE":
            side=signal.candidate_direction or "direction"
            for candidate in (*signal.rejection_reasons, *signal.missing_data, *signal.explanation):
                if candidate:
                    wait_reasons.append(f"{signal.strategy} {side}: {candidate}")
    for issue in issues:
        if issue:
            wait_reasons.append(issue)
    dedup=[]
    seen=set()
    for reason in wait_reasons:
        key=reason.lower().strip()
        if key and key not in seen:
            dedup.append(reason)
            seen.add(key)
    if not any("data" in reason.lower() or "complete" in reason.lower() for reason in dedup):
        dedup.insert(0, "Waiting for complete market data and a confirmed directional setup before opening a paper position.")
    summary='; '.join(dedup[:3]) if dedup else 'Brak aktywnych sygnałów — rynek nie spełnia warunków wejścia.'
    return {"signals":signals,"risk_decisions":tuple(decisions),"entry_fill":fill,
            "issues":tuple(issues),"data_quality":qualities,"wait_reasons":dedup,
            "wait_summary":summary,"entry_results":tuple(entry_results),
            "analyzed_at_ms":now_ms}

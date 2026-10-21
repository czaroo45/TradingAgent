"""Chronological, closed-bar backtesting with auditable assumptions."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from tradingagent.analysis import AnalysisError, analyze
from tradingagent.market_data import timeframe_to_ms, validate_ohlcv
from tradingagent.models import Candle, DataQuality
from tradingagent.paper_trading import PaperTradingEngine
from tradingagent.risk import (InstrumentConstraints, RiskLimits, RiskManager,
                               RiskRequest)
from tradingagent.strategies import StrategyConfig, StrategyEngine


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    symbol: str
    exchange: str
    entry_timeframe: str = "15m"
    higher_timeframe: str = "4h"
    start_ms: int | None = None
    end_ms: int | None = None
    initial_capital: float = 10_000.0
    quote_currency: str = "USDT"
    fee_bps_per_side: float = 10.0
    slippage_bps: float = 5.0
    spread_bps: float = 2.0
    amount_step: float = 1e-6
    max_volume_participation: float = 0.01
    strategy: StrategyConfig = StrategyConfig()
    risk_limits: RiskLimits = RiskLimits()
    version: str = "strategy-engine-v1"

    def __post_init__(self):
        if not self.symbol or not self.exchange or not self.quote_currency:
            raise ValueError("symbol, exchange, and quote currency are required")
        if "/" not in self.symbol or self.symbol.split("/",1)[1].upper()!=self.quote_currency.upper():
            raise ValueError("symbol quote currency must match the backtest portfolio")
        if not math.isfinite(self.initial_capital) or self.initial_capital <= 0:
            raise ValueError("initial capital must be finite and positive")
        if self.start_ms is not None and self.start_ms < 0:
            raise ValueError("start_ms must be nonnegative")
        if self.end_ms is not None and (self.end_ms <= 0 or (self.start_ms is not None and self.end_ms <= self.start_ms)):
            raise ValueError("end_ms must be after start_ms")
        if any(not math.isfinite(x) or x < 0 for x in (self.fee_bps_per_side,self.slippage_bps,self.spread_bps)):
            raise ValueError("costs must be finite and nonnegative")
        if not math.isfinite(self.amount_step) or self.amount_step <= 0:
            raise ValueError("amount_step must be positive")
        if not 0 < self.max_volume_participation <= 1:
            raise ValueError("volume participation must be in (0,1]")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    config: dict
    metrics: dict
    trades: tuple[dict, ...]
    fills: tuple[dict, ...]
    equity_curve: tuple[dict, ...]
    quality: dict
    provenance: dict
    signal_events: tuple[dict, ...]
    strategy_signals: int
    skipped_entries: int

    def export_json(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2, sort_keys=True), encoding="utf-8")

    def export_csv(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        rows = list(self.trades)
        columns = sorted({key for row in rows for key in row})
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=columns or ["symbol"])
            writer.writeheader()
            writer.writerows(rows)

    def export_fills_csv(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        rows=list(self.fills)
        columns=sorted({key for row in rows for key in row})
        with open(path,"w",newline="",encoding="utf-8") as file:
            writer=csv.DictWriter(file,fieldnames=columns or ["symbol"])
            writer.writeheader(); writer.writerows(rows)

    def export_charts(self, directory: str) -> tuple[str, str]:
        """Write dependency-free SVG equity and drawdown charts."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        points = self.equity_curve
        values = [float(p["equity"]) for p in points]
        high = []
        peak = -math.inf
        drawdown = []
        for value in values:
            peak = max(peak, value)
            high.append(peak)
            drawdown.append(0 if peak <= 0 else 100*(value/peak-1))
        equity_path = out / f"{self.run_id}-equity.svg"
        drawdown_path = out / f"{self.run_id}-drawdown.svg"
        _write_svg(equity_path, values, "Portfolio equity")
        _write_svg(drawdown_path, drawdown, "Drawdown (%)")
        return str(equity_path), str(drawdown_path)


@dataclass(frozen=True, slots=True)
class ChronologicalSplit:
    design: tuple[Candle, ...]
    validation: tuple[Candle, ...]
    final_test: tuple[Candle, ...]


def chronological_split(candles: list[Candle] | tuple[Candle, ...],
                        design_fraction: float = .60,
                        validation_fraction: float = .20) -> ChronologicalSplit:
    """Partition in time order; final_test is reserved and never used for tuning."""
    if not 0 < design_fraction < 1 or not 0 < validation_fraction < 1 or design_fraction+validation_fraction >= 1:
        raise ValueError("design and validation fractions must leave a nonempty final test")
    ordered = tuple(sorted(candles, key=lambda candle: candle.timestamp_ms))
    if len(ordered) < 3 or len({c.timestamp_ms for c in ordered}) != len(ordered):
        raise ValueError("at least three unique timestamped candles are required")
    d = max(1, int(len(ordered)*design_fraction))
    v = max(d+1, int(len(ordered)*(design_fraction+validation_fraction)))
    if v >= len(ordered):
        v = len(ordered)-1
    return ChronologicalSplit(ordered[:d], ordered[d:v], ordered[v:])


def _wilson(successes: int, total: int) -> tuple[float, float] | None:
    if total == 0: return None
    z=1.96; p=successes/total; den=1+z*z/total
    center=(p+z*z/(2*total))/den
    radius=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return max(0,center-radius),min(1,center+radius)


def _metrics(trades: tuple[dict,...], fills: tuple[dict,...], curve: tuple[dict,...], initial: float) -> dict:
    final=curve[-1]["equity"] if curve else initial
    values=[float(p["equity"]) for p in curve]
    peak=initial; max_dd=0.0
    for value in values:
        peak=max(peak,value)
        if peak: max_dd=max(max_dd,(peak-value)/peak)
    pnls=[float(t["net_pnl"]) for t in trades]
    wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
    gross_profit=sum(wins); gross_loss=abs(sum(losses))
    fees=sum(float(fill["fee"]) for fill in fills)
    slippage=sum(float(fill["quantity"])*float(fill["price"])*float(fill["slippage_bps"])/10_000 for fill in fills)
    spread=sum(float(fill["quantity"])*float(fill["price"])*float(fill["spread_bps"])/20_000 for fill in fills)
    durations=[(t["closed_at_ms"]-t["opened_at_ms"])/3_600_000 for t in trades]
    return {"net_profit":final-initial,"roi_pct":(final/initial-1)*100,
        "max_drawdown_pct":max_dd*100,"win_rate_pct":100*len(wins)/len(pnls) if pnls else None,
        "profit_factor":gross_profit/gross_loss if gross_loss else (None if not gross_profit else "unbounded_no_losses"),
        "average_win":sum(wins)/len(wins) if wins else None,
        "average_loss":sum(losses)/len(losses) if losses else None,
        "expectancy_per_trade":sum(pnls)/len(pnls) if pnls else None,
        "trade_count":len(trades),"fees_paid":fees,"slippage_cost_estimate":slippage,
        "spread_cost_estimate":spread,
        "average_duration_hours":sum(durations)/len(durations) if durations else None,
        "win_rate_95pct_wilson":_wilson(len(wins),len(pnls)),"sample_size":len(pnls)}


def _write_svg(path: Path, values: list[float], title: str) -> None:
    w,h=900,420; pad=45
    if not values: values=[0.0]
    low=min(values); high=max(values); spread=high-low or 1.0
    coords=[]
    for i,val in enumerate(values):
        x=pad+(w-2*pad)*(i/max(1,len(values)-1))
        y=h-pad-(h-2*pad)*(val-low)/spread
        coords.append(f"{x:.1f},{y:.1f}")
    svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
         f'<rect width="100%" height="100%" fill="white"/><text x="{pad}" y="28" font-size="18">{title}</text>'
         f'<polyline fill="none" stroke="#1769aa" stroke-width="2" points="{" ".join(coords)}"/>'
         f'<text x="{pad}" y="{h-12}" font-size="12">{low:.4g}</text><text x="{w-pad-60}" y="{h-12}" font-size="12">{high:.4g}</text></svg>')
    path.write_text(svg,encoding="utf-8")


class BacktestEngine:
    def __init__(self, persistence=None):
        self.persistence = persistence

    def run(self, candles: Mapping[str, list[Candle]], config: BacktestConfig) -> BacktestResult:
        entry_tf,higher_tf=config.entry_timeframe,config.higher_timeframe
        if entry_tf == higher_tf:
            raise ValueError("backtest requires distinct entry and higher timeframes")
        if entry_tf not in candles or higher_tf not in candles:
            raise ValueError("entry and higher timeframe candle histories are required")
        normalized: dict[str,list[Candle]]={}; quality_payload={}; source_material=[]
        observed_sources = {c.source for tf in (entry_tf, higher_tf) for c in candles[tf]}
        if len(observed_sources) != 1:
            raise ValueError("backtest histories must use one consistent data source")
        data_source = next(iter(observed_sources))
        for tf in sorted(set((entry_tf,higher_tf))):
            supplied=list(candles[tf])
            if any(c.source != data_source for c in supplied):
                raise ValueError(f"source mismatch in {tf} history")
            # Keep pre-start history for indicator warm-up, but never candles after the test end.
            if config.end_ms is not None:
                supplied=[c for c in supplied if c.timestamp_ms+timeframe_to_ms(tf) <= config.end_ms]
            raw=[[c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume] for c in supplied]
            tf_ms=timeframe_to_ms(tf)
            deltas=[b.timestamp_ms-a.timestamp_ms for a,b in zip(supplied,supplied[1:])]
            if any(delta>0 and (delta<tf_ms or delta%tf_ms!=0) for delta in deltas):
                raise ValueError(f"{tf} history contains bars with incompatible timeframe cadence")
            validated,q=validate_ohlcv(raw,config.exchange,config.symbol,tf,
                (supplied[-1].timestamp_ms+tf_ms) if supplied else 0,
                staleness_reference_ms=(supplied[-1].timestamp_ms+tf_ms) if supplied else 0)
            if q.duplicate_timestamps or q.non_monotonic or q.invalid_rows:
                raise ValueError(f"unusable {tf} history: {q.reasons}")
            # Retain only originally complete bars and actual validated OHLCV rows.
            complete_stamps={c.timestamp_ms for c in supplied if c.complete}
            # `source` identifies the provider (for example, ccxt); StrategyEngine's
            # source argument identifies the venue. Keep both identities distinct.
            rows=[replace(c, source=config.exchange) for c in validated if c.timestamp_ms in complete_stamps]
            normalized[tf]=rows
            quality_payload[tf]={"candle_count":len(rows),"duplicates":q.duplicate_timestamps,
                "non_monotonic":q.non_monotonic,"gaps":q.gaps,"invalid_rows":q.invalid_rows,
                "incomplete_rows_excluded":len(supplied)-len(complete_stamps),"acceptable_for_entry":not q.invalid_rows and not q.duplicate_timestamps}
            source_material.extend((tf,c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume) for c in rows)
        entry_rows=normalized[entry_tf]
        if not entry_rows:
            raise ValueError("no complete entry-timeframe candles in requested range")
        duration=timeframe_to_ms(entry_tf)
        if config.start_ms is None: start_ms=entry_rows[0].timestamp_ms
        else: start_ms=config.start_ms
        end_ms=config.end_ms if config.end_ms is not None else entry_rows[-1].timestamp_ms+duration
        test_rows=[c for c in entry_rows if c.timestamp_ms>=start_ms and c.timestamp_ms+duration<=end_ms]
        if not test_rows:
            raise ValueError("requested range contains no complete entry-timeframe candles")
        digest=hashlib.sha256(json.dumps(source_material,separators=(",",":" )).encode()).hexdigest()
        identity=json.dumps(asdict(config),sort_keys=True,separators=(",",":"),default=str)
        run_digest=hashlib.sha256((digest+identity).encode()).hexdigest()
        run_id=f"{config.symbol.replace('/','-')}-{start_ms}-{end_ms}-{run_digest[:10]}"
        strategy=replace(config.strategy,entry_timeframe=entry_tf,higher_timeframe=higher_tf)
        with tempfile.TemporaryDirectory(prefix="tradingagent-backtest-") as temp:
            paper=PaperTradingEngine(str(Path(temp)/"paper.sqlite3"),run_id,
                config.initial_capital,config.quote_currency,config.fee_bps_per_side,
                config.slippage_bps,config.spread_bps,initial_timestamp_ms=start_ms)
            manager=RiskManager(config.risk_limits)
            signals_engine=StrategyEngine(strategy)
            pending=None; signal_count=0; skipped=0; prev_ts=None; entry_segment=[]
            all_high=normalized[higher_tf]
            for bar in entry_rows:
                gap=prev_ts is not None and bar.timestamp_ms-prev_ts != duration
                if gap:
                    if pending is not None:
                        skipped+=1; pending=None
                    open_pos=paper.position(config.symbol)
                    if open_pos:
                        paper.close_position(config.symbol,open_pos.quantity,bar.open,
                            bar.timestamp_ms,"data_gap_exit",config.amount_step)
                    entry_segment=[]
                if pending is not None:
                    signal, regime=pending
                    liquidity=bar.volume*bar.open*config.max_volume_participation
                    instrument=InstrumentConstraints(config.quote_currency,config.amount_step,
                        liquidity_notional_cap=liquidity if liquidity>0 else None)
                    snapshot=paper.portfolio_for_risk({config.symbol:bar.open},bar.timestamp_ms)
                    request=RiskRequest(signal,snapshot,instrument,0,True,
                        fee_bps_round_trip=2*config.fee_bps_per_side,
                        slippage_bps_entry=config.slippage_bps+config.spread_bps/2,
                        slippage_bps_stop=config.slippage_bps+config.spread_bps/2)
                    try:
                        paper.execute_entry(signal,manager,request,bar,config.amount_step,
                                            {config.symbol:bar.open},regime)
                    except (ValueError, RuntimeError):
                        skipped+=1
                    pending=None
                paper.process_symbol_bar(config.symbol,bar,config.amount_step)
                asof=bar.timestamp_ms+duration
                in_test=bar.timestamp_ms >= start_ms and bar.timestamp_ms < end_ms
                if in_test:
                    paper.mark_to_market({config.symbol:bar.close},asof)
                entry_segment.append(bar)
                if prev_ts is None or not gap:
                    pass
                prev_ts=bar.timestamp_ms
                if not in_test or not bar.complete or paper.position(config.symbol) is not None:
                    continue
                high_segment=[c for c in all_high if c.timestamp_ms+timeframe_to_ms(higher_tf)<=asof]
                last_gap=next((i for i in range(len(high_segment)-1,0,-1)
                    if high_segment[i].timestamp_ms-high_segment[i-1].timestamp_ms != timeframe_to_ms(higher_tf)),0)
                high_segment=high_segment[last_gap:]
                entry_segment=[c for c in entry_segment if c.timestamp_ms+duration<=asof]
                if len(entry_segment)<50 or len(high_segment)<50:
                    continue
                asof_ms=asof
                entry_quality=DataQuality(config.exchange,config.symbol,entry_tf,asof_ms,len(entry_segment))
                high_quality=DataQuality(config.exchange,config.symbol,higher_tf,asof_ms,len(high_segment))
                try:
                    analyses={entry_tf:analyze(config.symbol,entry_tf,entry_segment,config.fee_bps_per_side,
                        config.slippage_bps,quality=entry_quality),
                        higher_tf:analyze(config.symbol,higher_tf,high_segment,config.fee_bps_per_side,
                        config.slippage_bps,quality=high_quality)}
                except (AnalysisError,ValueError,ZeroDivisionError):
                    continue
                quality_map={entry_tf:entry_quality,higher_tf:high_quality}
                candidates=signals_engine.evaluate(config.symbol,config.exchange,
                    {entry_tf:entry_segment,higher_tf:high_segment},analyses,quality_map,
                    now=datetime.fromtimestamp(asof_ms/1000,timezone.utc))
                for candidate in candidates:
                    paper.record_signal(candidate); signal_count+=1
                active=sorted((s for s in candidates if s.direction=="LONG" and s.status=="conditions_met"),
                              key=lambda s:s.confidence,reverse=True)
                if active:
                    pending=(active[0],analyses[entry_tf].regime)
            final_position=paper.position(config.symbol)
            if final_position:
                final_bar=test_rows[-1]
                paper.close_position(config.symbol,final_position.quantity,final_bar.close,
                    final_bar.timestamp_ms+duration,"backtest_end",config.amount_step)
            final_bar=test_rows[-1]
            final_snap=paper.mark_to_market({config.symbol:final_bar.close},
                max(end_ms,final_bar.timestamp_ms+duration))
            trades=paper.roundtrips(); fills=tuple(asdict(fill) for fill in paper.fills()); curve=paper.equity_curve()
            trade_by_symbol={config.symbol:len(trades)}
            by_symbol={config.symbol:{"trade_count":len(trades),"net_profit":sum(t["net_pnl"] for t in trades)}}
            regimes={}
            for regime in sorted({t["regime"] for t in trades}):
                rows=[t for t in trades if t["regime"]==regime]
                regimes[regime]={"trade_count":len(rows),"net_profit":sum(t["net_pnl"] for t in rows),
                                 "win_rate_pct":100*sum(t["net_pnl"]>0 for t in rows)/len(rows)}
            metrics=_metrics(trades,fills,tuple(asdict(p) for p in curve),config.initial_capital)
            metrics["by_symbol"]=by_symbol; metrics["by_regime"]=regimes
            first_price=test_rows[0].open; last_price=final_bar.close
            buy_fill=first_price*(1+(config.slippage_bps+config.spread_bps/2)/10_000)
            buy_qty=max(0,config.initial_capital/(buy_fill*(1+config.fee_bps_per_side/10_000)))
            sell_fill=last_price*(1-(config.slippage_bps+config.spread_bps/2)/10_000)
            hold_value=buy_qty*sell_fill*(1-config.fee_bps_per_side/10_000)
            metrics["benchmarks"]={"cash": {"net_profit":0.0,"roi_pct":0.0},
                "buy_and_hold": {"net_profit":hold_value-config.initial_capital,
                    "roi_pct":(hold_value/config.initial_capital-1)*100,
                    "assumption":"invested at first test-bar open and liquidated at last test-bar close, same fee/spread/slippage"}}
            provenance={"exchange":config.exchange,"symbol":config.symbol,
                "timeframes":{tf:{"count":len(rows),"first_ms":rows[0].timestamp_ms if rows else None,
                    "last_ms":rows[-1].timestamp_ms if rows else None,"source":data_source}
                    for tf,rows in normalized.items()},"sha256":digest,
                "signal_timing":"signal after closed bar; fill at next bar open",
                "same_bar_stop_and_target":"stop first; conservative",
                "missing_bars":"no interpolation; force-close at next observed open and reset warmup",
                "strategy_version":config.version,"final_snapshot":asdict(final_snap)}
            result=BacktestResult(run_id,asdict(config),metrics,trades,
                fills,tuple(asdict(point) for point in curve),
                quality_payload,provenance,tuple(event for event in paper.events() if event["kind"]=="signal"),
                signal_count,skipped)
        if self.persistence is not None:
            self.persistence.save_strategy_config(f"backtest:{run_id}",asdict(config))
            self.persistence.save_payload("backtest_results",config.symbol,entry_tf,end_ms,
                config.exchange,{"result":asdict(result),"trades":result.trades,"fills":result.fills})
        return result

    def compare(self, candles: Mapping[str,list[Candle]], config: BacktestConfig,
                strategy_names: tuple[str,...]) -> dict[str,BacktestResult]:
        """Run isolated portfolios on exactly the same immutable candle history."""
        if not strategy_names: raise ValueError("at least one strategy must be selected")
        return {name:self.run(candles,replace(config,strategy=replace(config.strategy,enabled=(name,))))
                for name in strategy_names}

    def run_universe(self, histories: Mapping[str,Mapping[str,list[Candle]]],
                     config: BacktestConfig) -> dict[str,BacktestResult]:
        """Report per-asset outcomes with equal isolated starting capital per asset."""
        supported={"BTC","ETH","XRP","SOL"}
        unknown={symbol for symbol in histories if symbol.split("/",1)[0].upper() not in supported or "/" not in symbol}
        if unknown:
            raise ValueError(f"unsupported backtest symbols: {sorted(unknown)}")
        if not histories:
            raise ValueError("at least one asset history is required")
        allocation=config.initial_capital/len(histories)
        return {symbol:self.run(history,replace(config,symbol=symbol,initial_capital=allocation))
                for symbol,history in sorted(histories.items())}


__all__=["BacktestConfig","BacktestEngine","BacktestResult","ChronologicalSplit","chronological_split"]

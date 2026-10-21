"""Local API and dashboard controls for market analysis and simulated trading."""
from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from tradingagent.analysis import analyze, calculate_indicators
from tradingagent.config import Settings
from tradingagent.market_data import (CcxtProMarketData, MarketEventMonitor,
    timeframe_to_ms, validate_ohlcv)
from tradingagent.models import Candle
from tradingagent.paper_trading import PaperTradingEngine, RiskManager, run_paper_cycle
from tradingagent.strategies import StrategyConfig, StrategyEngine
from tradingagent.storage import CandleStore
from tradingagent.market_data import CcxtMarketData, MarketDataError
from tradingagent.backtesting import BacktestConfig, BacktestEngine
from tradingagent.scoring import ScoreWeights


def _load_web_overrides(db_path: str) -> dict:
    if db_path == ":memory:" or not Path(db_path).is_file():
        return {}
    try:
        with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT payload FROM web_settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}


def _utc_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


_LOG_WINDOW_MS = 30 * 60 * 1000


def _event_timestamp_ms(value) -> int | None:
    """Normalize an event timestamp to UTC epoch milliseconds for comparisons."""
    if isinstance(value, (int, float)):
        number=float(value)
        if not math.isfinite(number):
            return None
        return int(number * 1000 if abs(number) < 100_000_000_000 else number)
    if isinstance(value, str):
        try:
            number=float(value)
        except ValueError:
            try:
                parsed=datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed=parsed.replace(tzinfo=timezone.utc)
            return int(parsed.astimezone(timezone.utc).timestamp()*1000)
        return _event_timestamp_ms(number)
    return None


def _register_event_timestamp_function(db: sqlite3.Connection) -> None:
    db.create_function("event_timestamp_ms", 1, _event_timestamp_ms, deterministic=True)


def _persist_paper_wait_events(engine: PaperTradingEngine, symbol: str,
                               cycle: dict, timeframe: str,
                               candle_timestamp_ms: int) -> tuple[int, ...]:
    """Persist actual wait reasons from one completed analysis cycle."""
    timestamp_ms = cycle.get("analyzed_at_ms")
    if not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        timestamp_ms = _utc_ms()
    return tuple(engine.record_wait_reason(
        symbol, timestamp_ms, reason, timeframe, candle_timestamp_ms
    ) for reason in cycle.get("wait_reasons", ())[:8] if reason)


def _parse_utc(value: str) -> int:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("Podaj datę ISO-8601 ze strefą czasu") from exc
    if dt.tzinfo is None:
        raise ValueError("Data musi zawierać strefę czasową")
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _paper_amount_steps(settings: Settings) -> dict[str,float]:
    """Read explicit per-symbol lot steps; allow legacy scalar only for one asset."""
    raw=os.getenv("TRADINGAGENT_PAPER_AMOUNT_STEPS","").strip()
    if raw:
        parsed={}
        try:
            for item in raw.split(","):
                symbol,value=item.split("=",1)
                symbol=symbol.strip()
                step=float(value.strip())
                if not symbol or symbol in parsed or not math.isfinite(step) or step<=0:
                    return {}
                parsed[symbol]=step
        except (ValueError,TypeError):
            return {}
        return parsed if set(parsed)==set(settings.symbols) else {}
    if len(settings.symbols)==1:
        try: step=float(os.getenv("TRADINGAGENT_PAPER_AMOUNT_STEP","0"))
        except ValueError: return {}
        return {settings.symbols[0]:step} if math.isfinite(step) and step>0 else {}
    return {}


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Trading Agent API", version="0.3.0",
                  description="Local trading analysis, backtesting and simulated paper portfolio.")
    initial_settings = settings or Settings.from_env()
    overrides = _load_web_overrides(initial_settings.db_path)
    if overrides:
        allowed = {key: value for key, value in overrides.items() if key in {
            "symbols", "timeframes", "enabled_strategies", "strategy_aggressive_mode",
            "strategy_entry_timeframe", "strategy_higher_timeframe", "strategy_min_score",
            "strategy_min_net_risk_reward", "max_risk_per_trade", "max_total_exposure",
            "max_asset_exposure", "max_correlated_exposure", "max_daily_loss", "max_drawdown",
            "max_trades_per_day", "fee_bps", "slippage_bps", "paper_spread_bps",
            "paper_short_trade_window_minutes", "paper_initial_capital",
            "paper_quote_currency", "paper_account_id", "display_timezone", "limit"}}
        if "symbols" in allowed: allowed["symbols"] = tuple(allowed["symbols"])
        if "timeframes" in allowed: allowed["timeframes"] = tuple(allowed["timeframes"])
        if "enabled_strategies" in allowed: allowed["enabled_strategies"] = tuple(allowed["enabled_strategies"])
        try:
            initial_settings = replace(initial_settings, **allowed)
            initial_settings.validate()
        except (TypeError, ValueError):
            overrides = {}
    app.state.settings = initial_settings
    app.state.started_at = datetime.now(timezone.utc)
    app.state.sync_status={"state":"starting","last_success":None,"errors":0}
    app.state.sync_task=None
    app.state.cycle_lock = threading.Lock()
    app.state.market_monitor = MarketEventMonitor(initial_settings.symbols,
        initial_settings.timeframes, max_data_age_ms=initial_settings.max_market_data_age_ms)
    app.state.market_stream_task = None
    app.state.market_poll_task = None
    app.state.market_finalize_task = None
    app.state.market_last_persisted_ticker = {}
    app.state.analysis_running = False
    app.state.paused = False
    app.state.paper_running = False
    app.state.market_connection = "unknown"
    app.state.last_operation = None
    app.state.operation_events = []
    app.state.backtest = {"state": "idle", "job_id": None, "error": None, "result_ids": []}
    app.state.backtest_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tradingagent-backtest")
    app.state.paper_auto_enabled=(app.state.settings.mode=="paper" and
        os.getenv("TRADINGAGENT_PAPER_AUTO_ENABLED","false").lower() in {"1","true","yes","on"})
    app.state.paper_wait_reasons = {}
    app.state.paper_last_cycle = {"wait_reasons": [], "wait_summary": "Brak aktywnych sygnałów."}
    app.state.paper_last_cycle_by_symbol = {}
    saved_steps=overrides.get("amount_steps",{}) if isinstance(overrides,dict) else {}
    app.state.paper_amount_steps=(saved_steps if isinstance(saved_steps,dict) and set(saved_steps)==set(app.state.settings.symbols)
        and all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in saved_steps.values())
        else _paper_amount_steps(app.state.settings))
    app.state.paper_engine=None
    app.state.paper_risk_manager=RiskManager.from_settings(app.state.settings)
    if app.state.paper_auto_enabled and app.state.paper_amount_steps:
        s=app.state.settings
        app.state.paper_engine=PaperTradingEngine(s.db_path,s.paper_account_id,s.paper_initial_capital,
            s.paper_quote_currency,s.fee_bps,s.slippage_bps,s.paper_spread_bps,
            short_term_exit_minutes=s.paper_short_trade_window_minutes)
    elif app.state.paper_auto_enabled:
        app.state.paper_auto_enabled=False
        app.state.paper_setup_warning="paper auto-run blocked: configure a valid per-symbol TRADINGAGENT_PAPER_AMOUNT_STEPS mapping"
    app.state.paper_running = bool(app.state.paper_auto_enabled)

    def sync_once():
        if not app.state.cycle_lock.acquire(blocking=False):
            return {"state": "busy"}
        s=app.state.settings; provider=None; db=store()
        paper_issues={}; request_errors=0; successful_requests=0
        try:
            provider=CcxtMarketData(s.exchange,timeout_ms=s.timeout_ms,
                max_concurrency=s.max_concurrency,cache_ttl_seconds=s.market_cache_seconds)
            for symbol in s.symbols:
                for tf in s.timeframes:
                    try:
                        latest=db.latest_timestamp(s.exchange,symbol,tf)
                        if latest is None:
                            rows,q=provider.fetch_candles(symbol,tf,s.limit)
                        else:
                            rows,q=provider.fetch_history(symbol,tf,latest,
                                int(datetime.now(timezone.utc).timestamp()*1000)+1,page_limit=min(s.limit,1000))
                        successful_requests+=1
                        if rows:
                            db.upsert(s.exchange,symbol,tf,rows,source="ccxt")
                            normalized,quality=validate_ohlcv([[c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume] for c in rows],
                                s.exchange,symbol,tf,int(datetime.now(timezone.utc).timestamp()*1000))
                            db.record_market_data_run(s.exchange,symbol,tf,rows[0].timestamp_ms,
                                rows[-1].timestamp_ms,int(datetime.now(timezone.utc).timestamp()*1000),
                                "ccxt",q.acceptable and quality.acceptable,len(rows),{"provider":"ccxt","purpose":"dashboard_refresh"},asdict(quality))
                        app.state.market_monitor.seed_history(
                            symbol, tf, db.load(s.exchange, symbol, tf, 1000))
                    except (MarketDataError,ValueError,OSError) as exc:
                        request_errors+=1
                        record_event("error","market_data",f"Świece {symbol} {tf}: {type(exc).__name__}")
                        continue
                if app.state.paper_engine is not None and app.state.paper_running and not app.state.paused:
                    try:
                        entry_tf=s.strategy_entry_timeframe
                        candle_ms=db.latest_timestamp(s.exchange,symbol,entry_tf,include_incomplete=False)
                        if candle_ms is None or app.state.paper_engine.market_event_processed(symbol,entry_tf,candle_ms):
                            continue
                        histories={tf:db.load(s.exchange,symbol,tf,300,include_incomplete=True) for tf in
                                   dict.fromkeys((s.strategy_entry_timeframe,s.strategy_higher_timeframe))}
                        cycle=run_paper_cycle(symbol=symbol,exchange=s.exchange,histories=histories,
                            settings=s,engine=app.state.paper_engine,risk_manager=app.state.paper_risk_manager,
                            amount_step=app.state.paper_amount_steps[symbol],
                            market_limits=provider.market_order_limits(symbol))
                        app.state.paper_last_cycle_by_symbol[symbol]=cycle
                        required=(s.strategy_entry_timeframe,s.strategy_higher_timeframe)
                        quality=cycle.get("data_quality",{})
                        if all(quality.get(tf) is not None and quality[tf].acceptable and
                               len([c for c in histories.get(tf,()) if c.complete])>=50 for tf in required):
                            app.state.paper_engine.mark_market_event_processed(
                                symbol,entry_tf,candle_ms,_utc_ms())
                        signal_directions = tuple(signal.direction for signal in cycle.get("signals", ())
                                                   if signal.status == "conditions_met")
                        if signal_directions:
                            record_event("info","paper",
                                         f"Paper scan {symbol}: {', '.join(signal_directions)}")
                        if cycle.get("wait_reasons"):
                            app.state.paper_wait_reasons[symbol] = cycle["wait_reasons"]
                            app.state.paper_last_cycle = {"wait_reasons": cycle["wait_reasons"], "wait_summary": cycle.get("wait_summary", "Brak aktywnych sygnałów.")}
                            _persist_paper_wait_events(
                                app.state.paper_engine, symbol, cycle, entry_tf, candle_ms)
                            for reason in cycle["wait_reasons"][:8]:
                                record_event("info","paper_debug",f"Paper wait {symbol}: {reason}")
                        if cycle["issues"]:
                            paper_issues[symbol]=cycle["issues"]
                    except Exception as exc:
                        reason=f"paper cycle failed: {type(exc).__name__}"
                        paper_issues[symbol]=(reason,)
                        app.state.paper_last_cycle_by_symbol[symbol]={"signals":(),"entry_results":(),
                            "wait_reasons":(reason,),"wait_summary":reason,"issues":(reason,)}
                        record_event("error","paper",f"Paper cycle {symbol} failed: {type(exc).__name__}")
            previous=app.state.sync_status
            app.state.market_connection = "connected" if successful_requests else "error"
            if successful_requests:
                app.state.sync_status={"state":"degraded" if request_errors else "ok",
                    "last_success":datetime.now(timezone.utc).isoformat(),
                    "errors":previous["errors"]+request_errors,"paper_issues":paper_issues}
            else:
                app.state.sync_status={"state":"degraded","last_success":previous["last_success"],
                    "errors":previous["errors"]+max(1,request_errors),"paper_issues":paper_issues}
            return app.state.sync_status
        except Exception:
            app.state.market_connection = "error"
            raise
        finally:
            if provider is not None: provider.close()
            app.state.cycle_lock.release()

    async def sync_loop():
        interval=max(30,int(os.getenv("TRADINGAGENT_API_SYNC_SECONDS","60")))
        while (os.getenv("TRADINGAGENT_API_SYNC_ENABLED","true").lower() in {"1","true","yes","on"}
               or app.state.analysis_running or app.state.paper_running):
            try: await asyncio.to_thread(sync_once)
            except Exception:
                if app.state.paper_engine is not None:
                    app.state.paper_risk_manager.breaker.trip("market data synchronization failed")
                app.state.sync_status={"state":"degraded","last_success":app.state.sync_status["last_success"],
                                       "errors":app.state.sync_status["errors"]+1}
            if app.state.analysis_running and not app.state.paused:
                try: await asyncio.to_thread(analyze_once)
                except Exception as exc: record_event("error", "analysis", f"Analiza nie powiodła się: {type(exc).__name__}")
            if not (os.getenv("TRADINGAGENT_API_SYNC_ENABLED","true").lower() in {"1","true","yes","on"}
                    or app.state.analysis_running or app.state.paper_running):
                break
            await asyncio.sleep(interval)
        app.state.sync_task = None

    @app.on_event("startup")
    async def start_sync():
        enabled=os.getenv("TRADINGAGENT_API_SYNC_ENABLED","true").lower() in {"1","true","yes","on"}
        if enabled or app.state.analysis_running or app.state.paper_running:
            ensure_market_workers()
            app.state.sync_task=asyncio.create_task(sync_loop())

    @app.on_event("shutdown")
    async def stop_sync():
        tasks=[app.state.sync_task, app.state.market_stream_task,
               app.state.market_poll_task, app.state.market_finalize_task]
        for task in tasks:
            if task and not task.done(): task.cancel()
        for task in tasks:
            if task:
                try: await task
                except asyncio.CancelledError: pass
        app.state.backtest_executor.shutdown(wait=False, cancel_futures=True)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        token = os.getenv("TRADINGAGENT_API_TOKEN", "")
        if token:
            supplied = authorization.removeprefix("Bearer ") if authorization else ""
            if not hmac.compare_digest(token, supplied):
                raise HTTPException(status_code=401, detail="Bearer token required")

    def store() -> CandleStore:
        return CandleStore(app.state.settings.db_path)

    def record_event(level: str, category: str, message: str) -> None:
        event = {"timestamp_ms": _utc_ms(), "level": level, "category": category, "message": message}
        app.state.last_operation = event
        app.state.operation_events = ([event] + app.state.operation_events)[:100]
        try:
            with sqlite3.connect(app.state.settings.db_path, timeout=5) as db:
                db.execute("CREATE TABLE IF NOT EXISTS web_events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL, level TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL)")
                db.execute("INSERT INTO web_events(timestamp_ms,level,category,message) VALUES (?,?,?,?)",
                           (event["timestamp_ms"], level, category, message))
        except sqlite3.Error:
            pass

    def persist_stream_candle(symbol: str, timeframe: str, candle: Candle) -> None:
        store().upsert(app.state.settings.exchange, symbol, timeframe, [candle],
                       source="ccxt", complete=True)

    async def finalize_market_candles() -> None:
        while True:
            try:
                emitted = app.state.market_monitor.advance_time()
                for symbol, timeframe, candle in emitted:
                    await asyncio.to_thread(persist_stream_candle, symbol, timeframe, candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_event("error", "market_data", f"Finalizacja świecy: {type(exc).__name__}")
            await asyncio.sleep(0.5)

    async def market_stream_loop() -> None:
        provider = None
        attempt = 0
        while True:
            try:
                if provider is None:
                    provider = CcxtProMarketData(app.state.settings.exchange,
                                                 timeout_ms=app.state.settings.timeout_ms)
                if not provider.supports_trade_stream:
                    app.state.market_monitor.websocket_unavailable("Brak publicznego strumienia transakcji CCXT Pro")
                    await provider.close()
                    provider = None
                    return
                app.state.market_monitor.connection_started()
                # load_markets happens on the first subscription. The status moves to
                # websocket only after CCXT has returned the first real public batch.
                while True:
                    trades = await provider.watch_trades_for_symbols(app.state.settings.symbols)
                    app.state.market_monitor.connection_ready()
                    if attempt:
                        record_event("info", "market_data",
                            f"Strumień publiczny wznowiony po przerwie (próba {attempt}).")
                        attempt = 0
                    for trade in trades:
                        symbol = trade.get("symbol")
                        if symbol not in app.state.settings.symbols:
                            continue
                        try:
                            closed = app.state.market_monitor.apply_trade(symbol, trade)
                        except ValueError:
                            # A malformed event is rejected in isolation; it must not
                            # tear down a healthy subscription for the other markets.
                            continue
                        for timeframe, candle in closed:
                            await asyncio.to_thread(persist_stream_candle, symbol, timeframe, candle)
            except asyncio.CancelledError:
                if provider is not None:
                    try: await provider.close()
                    except Exception: pass
                raise
            except (RuntimeError, ValueError, MarketDataError) as exc:
                message = f"{type(exc).__name__}: {str(exc)[:160]}"
                app.state.market_monitor.websocket_unavailable(message)
                record_event("warning", "market_data",
                    f"Strumień WebSocket niedostępny; używam REST fallback: {message}")
                if provider is not None:
                    try: await provider.close()
                    except Exception: pass
                return
            except Exception as exc:
                attempt += 1
                message = f"{type(exc).__name__}: {str(exc)[:160]}"
                app.state.market_monitor.connection_failed(message)
                record_event("warning", "market_data", f"Strumień publiczny przerwany: {message}; ponowienie {attempt}")
                if provider is not None:
                    try: await provider.close()
                    except Exception: pass
                    provider = None
                await asyncio.sleep(min(2 ** min(attempt - 1, 5), 30))
    async def market_poll_loop() -> None:
        provider = None
        last_saved: dict[str, int] = {}
        try:
            while True:
                if provider is None:
                    try:
                        provider = CcxtMarketData(app.state.settings.exchange,
                            timeout_ms=app.state.settings.timeout_ms,
                            max_concurrency=app.state.settings.max_concurrency,
                            cache_ttl_seconds=app.state.settings.market_cache_seconds)
                    except Exception as exc:
                        for symbol in app.state.settings.symbols:
                            app.state.market_monitor.rest_failed(
                                symbol, f"{type(exc).__name__}: {str(exc)[:160]}")
                        record_event("warning", "market_data",
                            f"REST fallback niedostępny: {type(exc).__name__}: {str(exc)[:160]}")
                        await asyncio.sleep(min(app.state.settings.market_rest_poll_seconds * 2, 30))
                        continue
                now = _utc_ms()
                snapshots = {row["symbol"]: row for row in app.state.market_monitor.snapshot(now)}
                targets = [symbol for symbol, health in snapshots.items()
                           if health["connection_state"] != "websocket" or health["stale"]]
                for symbol in targets:
                    try:
                        ticker = await asyncio.to_thread(provider.fetch_ticker, symbol)
                        if app.state.market_monitor.apply_rest_ticker(ticker):
                            if ticker.received_timestamp_ms - last_saved.get(symbol, 0) >= 60_000:
                                await asyncio.to_thread(store().save_ticker, app.state.settings.exchange,
                                    symbol, ticker.received_timestamp_ms, "ccxt", asdict(ticker))
                                last_saved[symbol] = ticker.received_timestamp_ms
                        else:
                            app.state.market_monitor.rest_failed(symbol, "invalid_or_out_of_order_ticker")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        app.state.market_monitor.rest_failed(
                            symbol, f"{type(exc).__name__}: {str(exc)[:160]}")
                await asyncio.sleep(app.state.settings.market_rest_poll_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            if provider is not None:
                provider.close()

    def ensure_market_workers() -> None:
        if app.state.market_stream_task is None or app.state.market_stream_task.done():
            app.state.market_stream_task = asyncio.create_task(market_stream_loop())
        if app.state.market_poll_task is None or app.state.market_poll_task.done():
            app.state.market_poll_task = asyncio.create_task(market_poll_loop())
        if app.state.market_finalize_task is None or app.state.market_finalize_task.done():
            app.state.market_finalize_task = asyncio.create_task(finalize_market_candles())

    def ensure_worker() -> None:
        ensure_market_workers()
        task = app.state.sync_task
        if task is None or task.done():
            app.state.sync_task = asyncio.create_task(sync_loop())

    def ensure_paper_engine(require_amount_steps: bool = True) -> PaperTradingEngine:
        s = app.state.settings
        if require_amount_steps and set(app.state.paper_amount_steps) != set(s.symbols):
            raise HTTPException(status_code=409, detail="Ustaw krok ilości osobno dla każdego rynku w Ustawieniach przed uruchomieniem paper tradingu.")
        if app.state.paper_engine is None:
            app.state.paper_engine = PaperTradingEngine(s.db_path, s.paper_account_id,
                s.paper_initial_capital, s.paper_quote_currency, s.fee_bps,
                s.slippage_bps, s.paper_spread_bps,
                short_term_exit_minutes=s.paper_short_trade_window_minutes)
            app.state.paper_risk_manager = RiskManager.from_settings(s)
        return app.state.paper_engine

    def analyze_once() -> dict:
        result = signals(symbol=None, limit=500)
        saved = 0
        for item in result["items"]:
            stamp = item.get("timestamp")
            try:
                timestamp_ms = int(datetime.fromisoformat(stamp).timestamp() * 1000)
            except (TypeError, ValueError):
                timestamp_ms = _utc_ms()
            store().save_payload("signals", item["symbol"], app.state.settings.strategy_entry_timeframe,
                                 timestamp_ms, item.get("source", "ccxt"), item)
            saved += 1
        record_event("info", "analysis", f"Analiza zakończona; zapisano {saved} scenariuszy.")
        return {"saved_signals": saved, "generated_at": datetime.now(timezone.utc).isoformat()}

    def safe_symbol(symbol: str) -> str:
        if symbol not in app.state.settings.symbols:
            raise HTTPException(status_code=404, detail="symbol is not configured")
        return symbol

    @app.get("/health")
    def health():
        settings = app.state.settings
        db_ok = True
        try:
            with sqlite3.connect(settings.db_path, timeout=2) as db:
                db.execute("SELECT 1")
        except sqlite3.Error:
            db_ok = False
        sync=app.state.sync_status["state"]
        return {"status": "ok" if db_ok and sync in {"ok","starting"} else "degraded", "mode": settings.mode,
                "live_trading": False, "database": "ok" if db_ok else "unavailable",
                "market_data_sync":sync,"last_successful_update":app.state.sync_status["last_success"],
                "sync_errors":app.state.sync_status["errors"],
                "market_connection":app.state.market_connection,
                "analysis_running":app.state.analysis_running,"paper_running":app.state.paper_running,
                "paused":app.state.paused,
                "paper_auto_enabled":app.state.paper_auto_enabled,
                "paper_setup_warning":getattr(app.state,"paper_setup_warning",None),
                "llm_enabled": os.getenv("TRADINGAGENT_LLM_ENABLED", "false").lower() in {"1", "true", "yes", "on"},
                "generated_at": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status():
        s = app.state.settings
        db = store()
        assets = []
        now = int(datetime.now(timezone.utc).timestamp()*1000)
        for symbol in s.symbols:
            sources = db.sources(s.exchange, symbol, s.strategy_entry_timeframe)
            latest = db.latest_timestamp(s.exchange, symbol, s.strategy_entry_timeframe)
            age = now-latest-timeframe_to_ms(s.strategy_entry_timeframe) if latest is not None else None
            assets.append({"symbol":symbol,"exchange":s.exchange,"source":"ccxt/SQLite" if latest else None,
                           "latest_candle_ms":latest,"age_ms":age,"stale":age is None or age>s.max_market_data_age_ms,
                           "quality":"source_mismatch" if (len(sources)!=1 or sources[0] not in {"ccxt",s.exchange}) else ("available" if latest else "missing")})
        return {"mode":s.mode,"live_trading":False,"database":"ok","market_data_sync":app.state.sync_status,"assets":assets,
                "market_data_health":app.state.market_monitor.snapshot(now),
                "market_data_update":{"websocket":"public CCXT Pro trade events when supported",
                    "rest_fallback_poll_seconds":s.market_rest_poll_seconds,
                    "rest_fallback_resolution":"ticker snapshots; not every trade"},
                "last_successful_data_ms":max((a["latest_candle_ms"] or 0 for a in assets),default=0) or None,
                "market_connection":app.state.market_connection,
                "analysis_running":app.state.analysis_running,"paper_running":app.state.paper_running,
                "paused":app.state.paused,"last_operation":app.state.last_operation,
                "version":"0.3.0","live_trading":False}

    @app.get("/api/market", dependencies=[Depends(authorize)])
    def market(period: str = Query("24h", pattern="^(1h|24h|7d)$")):
        db=store(); result=[]; s=app.state.settings
        for symbol in app.state.settings.symbols:
            health=next((item for item in app.state.market_monitor.snapshot()
                         if item["symbol"]==symbol),None)
            rows=db.load(s.exchange,symbol,s.strategy_entry_timeframe,1000)
            providers=set(db.sources(s.exchange,symbol,s.strategy_entry_timeframe))
            provenance_ok=len(providers)==1 and providers.issubset({"ccxt",s.exchange})
            ticker=None
            try:
                with sqlite3.connect(s.db_path) as connection:
                    record=connection.execute("SELECT timestamp_ms,payload FROM ticker_snapshots WHERE exchange=? AND symbol=? ORDER BY timestamp_ms DESC,id DESC LIMIT 1",(s.exchange,symbol)).fetchone()
                if record:
                    payload=json.loads(record[1]); ticker={"timestamp_ms":record[0],"price":payload.get("last"),"bid":payload.get("bid"),"ask":payload.get("ask"),"source":"ccxt ticker"}
            except (sqlite3.Error,ValueError,TypeError):
                ticker=None
            if health and not health["stale"] and health["latest_price"] is not None:
                changes={"1h":None,"24h":None,"7d":None}
                if rows:
                    latest=rows[-1]
                    for label,period_ms in (("1h",3_600_000),("24h",86_400_000),("7d",604_800_000)):
                        target=latest.timestamp_ms-period_ms
                        prior=next((c for c in reversed(rows[:-1]) if c.timestamp_ms<=target),None)
                        changes[label]=((latest.close/prior.close)-1)*100 if prior and target-prior.timestamp_ms<=timeframe_to_ms(s.strategy_entry_timeframe) else None
                result.append({"symbol":symbol,"price":health["latest_price"],
                    "timestamp_ms":health["last_update_ms"],"source":health["source"],
                    "bid":health["bid"],"ask":health["ask"],
                    "last_closed_candle":rows[-1].close if rows else None,
                    "complete":rows[-1].complete if rows else None,"stale":False,
                    "change_pct":changes.get(period),"changes":changes})
            elif rows and provenance_ok:
                previous,latest=(rows[-2],rows[-1]) if len(rows)>1 else (None,rows[-1])
                changes={}
                for label,period_ms in (("1h",3_600_000),("24h",86_400_000),("7d",604_800_000)):
                    target=latest.timestamp_ms-period_ms
                    prior=next((c for c in reversed(rows[:-1]) if c.timestamp_ms<=target),None)
                    # Reject large gaps instead of implying a precise period change from old data.
                    changes[label]=((latest.close/prior.close)-1)*100 if prior and target-prior.timestamp_ms<=timeframe_to_ms(s.strategy_entry_timeframe) else None
                ticker_fresh=ticker and ticker["price"] and int(datetime.now(timezone.utc).timestamp()*1000)-ticker["timestamp_ms"]<=s.max_market_data_age_ms
                result.append({"symbol":symbol,"price":ticker["price"] if ticker_fresh else latest.close,
                    "timestamp_ms":ticker["timestamp_ms"] if ticker_fresh else latest.timestamp_ms,
                    "source":ticker["source"] if ticker_fresh else latest.source,
                    "last_closed_candle":latest.close,"complete":latest.complete,
                    "stale":not bool(ticker_fresh) and int(datetime.now(timezone.utc).timestamp()*1000)-latest.timestamp_ms-timeframe_to_ms(s.strategy_entry_timeframe)>s.max_market_data_age_ms,
                    "change_pct":changes.get(period),"changes":changes})
            elif not rows and ticker and ticker["price"] is not None:
                result.append({"symbol":symbol,"price":ticker["price"],"timestamp_ms":ticker["timestamp_ms"],
                    "source":ticker["source"],"bid":ticker["bid"],"ask":ticker["ask"],"change_pct":None,
                    "stale":int(datetime.now(timezone.utc).timestamp()*1000)-ticker["timestamp_ms"]>s.max_market_data_age_ms})
            else:
                result.append({"symbol":symbol,"price":None,"timestamp_ms":None,"source":None,"stale":True})
        return {"items":result,"simulated":False}

    @app.get("/api/assets/{symbol:path}", dependencies=[Depends(authorize)])
    def asset(symbol: str):
        symbol=safe_symbol(symbol); s=app.state.settings; db=store()
        output=[]
        for tf in s.timeframes:
            rows=db.load(s.exchange,symbol,tf,300)
            if not rows: continue
            good=[c for c in rows if c.complete]
            quality=validate_ohlcv([[c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume] for c in rows],
                s.exchange,symbol,tf,int(datetime.now(timezone.utc).timestamp()*1000))[1]
            providers=set(db.sources(s.exchange,symbol,tf))
            if len(providers)!=1 or not providers.issubset({"ccxt",s.exchange}):
                quality=replace(quality,source_mismatch=True)
            item={"timeframe":tf,"quality":asdict(quality),"candles":[],
                  "analysis_state":"insufficient_data"}
            if good:
                try:
                    ind=asdict(calculate_indicators(good)) if len(good)>=50 else None
                    analysis=analyze(symbol,tf,good,s.fee_bps,s.slippage_bps,s.max_spread_bps,quality=quality)
                    closes=[c.close for c in good]
                    def sma(values,period): return sum(values[-period:])/period if len(values)>=period else None
                    def ema(values,period):
                        if len(values)<period:return None
                        value=sum(values[:period])/period;alpha=2/(period+1)
                        for v in values[period:]:value=alpha*v+(1-alpha)*value
                        return value
                    def ema_series(values,period):
                        if len(values)<period:return []
                        alpha=2/(period+1);out=[sum(values[:period])/period]
                        for v in values[period:]:out.append(alpha*v+(1-alpha)*out[-1])
                        return out
                    macd_values=[]
                    if len(closes)>=34:
                        fast,slow=ema_series(closes,12),ema_series(closes,26)
                        macd_values=[a-b for a,b in zip(fast[-len(slow):],slow)]
                    macd=macd_values[-1] if macd_values else None
                    macd_signal_values=ema_series(macd_values,9)
                    macd_signal=macd_signal_values[-1] if macd_signal_values else None
                    state=("insufficient_data" if analysis.regime=="insufficient_data" else
                           "scenario" if analysis.scenario is not None else "no_signal")
                    item.update(analysis_state=state,
                        indicators={**(ind or {}),"sma20":sma(closes,20),"sma50":sma(closes,50),
                        "ema20":ema(closes,20),"ema50":ema(closes,50),"ema200":ema(closes,200),
                        "macd_line":macd,"macd_signal":macd_signal,"volume":good[-1].volume,
                        "support":min(c.low for c in good[-20:]),"resistance":max(c.high for c in good[-20:]),
                        "breakout_up":good[-1].close>max(c.high for c in good[-21:-1]) if len(good)>=21 else None,
                        "breakout_down":good[-1].close<min(c.low for c in good[-21:-1]) if len(good)>=21 else None},analysis=asdict(analysis))
                except (ValueError,ZeroDivisionError) as exc:
                    item.update(analysis_state="error",indicators=None,
                                analysis_error=f"{type(exc).__name__}: {str(exc)[:180]}")
            else:
                reasons=list(quality.reasons)
                item["analysis_reason"]=("Brak kompletnych świec dla tego interwału."
                    if not reasons else "; ".join(reasons))
            output.append(item)
        return {"symbol":symbol,"timeframes":output}

    @app.get("/api/candles/{symbol:path}", dependencies=[Depends(authorize)])
    def candles(symbol: str, timeframe: str=Query("15m",min_length=2,max_length=4),
                limit: int=Query(200,ge=1,le=1000), before_ms: int|None=Query(None,ge=0)):
        symbol=safe_symbol(symbol); s=app.state.settings
        if timeframe not in s.timeframes: raise HTTPException(status_code=422,detail="unsupported timeframe")
        db=store()
        rows=(db.load_before(s.exchange,symbol,timeframe,before_ms,limit+1) if before_ms is not None
              else db.load(s.exchange,symbol,timeframe,limit+1))
        more=len(rows)>limit; rows=rows[-limit:]
        sources=store().sources(s.exchange,symbol,timeframe)
        source_ok=len(sources)==1 and sources[0] in {"ccxt",s.exchange}
        return {"items":[asdict(c) for c in rows],"next_before_ms":rows[0].timestamp_ms if more and rows else None,
                "quality":"stored" if source_ok else "source_mismatch","sources":sources,
                "analysis_allowed":source_ok}

    @app.get("/api/signals", dependencies=[Depends(authorize)])
    def signals(symbol: str|None=None, limit: int=Query(100,ge=1,le=500)):
        s=app.state.settings; db=store(); items=[]
        targets=[safe_symbol(symbol)] if symbol else list(s.symbols)
        for asset_symbol in targets:
            tfs=(s.strategy_entry_timeframe,s.strategy_higher_timeframe)
            histories={}; analyses={}; qualities={}
            for tf in dict.fromkeys(tfs):
                rows=db.load(s.exchange,asset_symbol,tf,300)
                if not rows: continue
                normalized,q=validate_ohlcv([[c.timestamp_ms,c.open,c.high,c.low,c.close,c.volume] for c in rows],
                    s.exchange,asset_symbol,tf,int(datetime.now(timezone.utc).timestamp()*1000))
                providers=set(db.sources(s.exchange,asset_symbol,tf))
                if len(providers)!=1 or not providers.issubset({"ccxt",s.exchange}):
                    q=replace(q,source_mismatch=True)
                histories[tf]=[replace(c,source=s.exchange) for c in normalized]
                qualities[tf]=q
                if q.acceptable and len(histories[tf])>=50:
                    analyses[tf]=analyze(asset_symbol,tf,histories[tf],s.fee_bps,s.slippage_bps,s.max_spread_bps,quality=q)
            strategy_config=StrategyConfig(enabled=s.enabled_strategies,
                aggressive_mode=s.strategy_aggressive_mode,entry_timeframe=s.strategy_entry_timeframe,
                higher_timeframe=s.strategy_higher_timeframe,fee_bps_per_side=s.fee_bps,
                slippage_bps_per_side=s.slippage_bps,spread_bps=s.paper_spread_bps,
                min_net_risk_reward=s.strategy_min_net_risk_reward,min_score=s.strategy_min_score,
                weights=ScoreWeights(*s.score_weights))
            result=StrategyEngine(strategy_config).evaluate(asset_symbol,s.exchange,histories,analyses,qualities)
            reference=histories.get(s.strategy_entry_timeframe,[])
            reference_price=reference[-1].close if reference else None
            items.extend({**asdict(signal),"reference_price":reference_price,"simulated":False} for signal in result)
        items.sort(key=lambda x:x["timestamp"],reverse=True)
        watchlist=sorted((item for item in items if item["direction"]=="NO TRADE"),
                         key=lambda item:item.get("confidence",0),reverse=True)[:4]
        return {"items":items[:limit],"watchlist":watchlist,
                "watchlist_note":"Porządek według deterministycznego score warunków, nie prawdopodobieństwa.",
                "blocked_stale_or_bad_data":True}

    @app.get("/api/signals/history", dependencies=[Depends(authorize)])
    def signal_history(symbol: str|None=None,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
        allowed=app.state.settings.symbols
        if symbol and symbol not in allowed: raise HTTPException(status_code=404,detail="symbol is not configured")
        with sqlite3.connect(app.state.settings.db_path) as db:
            where="WHERE symbol=?" if symbol else ""
            params=(symbol,limit,offset) if symbol else (limit,offset)
            try: rows=db.execute(f"SELECT id,symbol,timeframe,timestamp_ms,source,payload FROM signals {where} ORDER BY timestamp_ms DESC,id DESC LIMIT ? OFFSET ?",params).fetchall()
            except sqlite3.Error: rows=[]
        return {"items":[{"id":r[0],"symbol":r[1],"timeframe":r[2],"timestamp_ms":r[3],"source":r[4],"signal":json.loads(r[5])} for r in rows],"limit":limit,"offset":offset}

    @app.get("/api/strategies", dependencies=[Depends(authorize)])
    def strategies():
        s=app.state.settings
        return {"enabled":list(s.enabled_strategies),"entry_timeframe":s.strategy_entry_timeframe,
                "higher_timeframe":s.strategy_higher_timeframe,"minimum_score":s.strategy_min_score,
                "minimum_net_rr":s.strategy_min_net_risk_reward,"read_only":True}

    @app.get("/api/settings",dependencies=[Depends(authorize)])
    def settings_view():
        s=app.state.settings
        return {"exchange":s.exchange,"source":"ccxt","symbols":s.symbols,"timeframes":s.timeframes,"mode":s.mode,
                "strategies":s.enabled_strategies,"strategy_aggressive_mode":s.strategy_aggressive_mode,
                "strategy_entry_timeframe":s.strategy_entry_timeframe,
                "strategy_higher_timeframe":s.strategy_higher_timeframe,
                "strategy_min_score":s.strategy_min_score,"strategy_min_net_risk_reward":s.strategy_min_net_risk_reward,
                "risk_limits":{"risk_per_trade":s.max_risk_per_trade,"total_exposure":s.max_total_exposure,
                "asset_exposure":s.max_asset_exposure,"correlated_exposure":s.max_correlated_exposure,
                "daily_loss":s.max_daily_loss,"drawdown":s.max_drawdown,"max_trades_per_day":s.max_trades_per_day},
                "paper":{"initial_capital":s.paper_initial_capital,"quote_currency":s.paper_quote_currency,
                "account_id":s.paper_account_id,"fee_bps":s.fee_bps,"slippage_bps":s.slippage_bps,
                "spread_bps":s.paper_spread_bps,"short_trade_window_minutes":s.paper_short_trade_window_minutes,
                "amount_steps":app.state.paper_amount_steps},
                "limit":s.limit,
                "automatic_analysis_seconds":max(30,int(os.getenv("TRADINGAGENT_API_SYNC_SECONDS","60"))),
                "supported_strategies":StrategyEngine.NAMES,"available_timeframes":
                ("1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d"),
                "write_access":True,"live_trading":False,"paper_auto_enabled":app.state.paper_auto_enabled,
                "paper_running":app.state.paper_running}

    @app.put("/api/settings", dependencies=[Depends(authorize)])
    def update_settings(payload: dict = Body(...)):
        s=app.state.settings
        reset_confirmation=payload.pop("reset_paper_account",None)
        reset_existing_account=False
        allowed={"symbols","timeframes","enabled_strategies","strategy_aggressive_mode",
            "strategy_entry_timeframe","strategy_higher_timeframe","strategy_min_score",
            "strategy_min_net_risk_reward","max_risk_per_trade","max_total_exposure",
            "max_asset_exposure","max_correlated_exposure","max_daily_loss","max_drawdown",
            "max_trades_per_day","fee_bps","slippage_bps","paper_spread_bps",
            "paper_short_trade_window_minutes","paper_initial_capital","paper_quote_currency",
            "paper_account_id","limit","amount_steps"}
        unknown=set(payload)-allowed
        if unknown: raise HTTPException(status_code=422,detail=f"Nieobsługiwane ustawienia: {', '.join(sorted(unknown))}")
        updates=dict(payload)
        amount_steps=updates.pop("amount_steps",app.state.paper_amount_steps)
        numeric_fields={"strategy_min_score","strategy_min_net_risk_reward","max_risk_per_trade",
            "max_total_exposure","max_asset_exposure","max_correlated_exposure","max_daily_loss",
            "max_drawdown","fee_bps","slippage_bps","paper_spread_bps","paper_initial_capital"}
        try:
            for key in numeric_fields & set(updates):
                if isinstance(updates[key],bool): raise ValueError(f"{key} musi być liczbą.")
                updates[key]=float(updates[key])
                if not math.isfinite(updates[key]): raise ValueError(f"{key} musi być liczbą skończoną.")
            for key in ("limit","max_trades_per_day","paper_short_trade_window_minutes"):
                if key in updates and (isinstance(updates[key],bool) or not isinstance(updates[key],int)):
                    raise ValueError(f"{key} musi być liczbą całkowitą.")
            if "strategy_aggressive_mode" in updates and not isinstance(updates["strategy_aggressive_mode"], bool):
                raise ValueError("strategy_aggressive_mode musi być wartością logiczną.")
        except (TypeError,ValueError) as exc:
            raise HTTPException(status_code=422,detail=str(exc)) from exc
        if "symbols" in updates:
            if not isinstance(updates["symbols"],list) or not updates["symbols"] or len(set(updates["symbols"]))!=len(updates["symbols"]):
                raise HTTPException(status_code=422,detail="Wybierz co najmniej jeden unikalny rynek.")
            if set(updates["symbols"])-{"BTC/USDT","ETH/USDT","XRP/USDT","SOL/USDT"}:
                raise HTTPException(status_code=422,detail="Dozwolone rynki: BTC, ETH, XRP i SOL w parze USDT.")
            updates["symbols"]=tuple(updates["symbols"])
        for key in ("timeframes","enabled_strategies"):
            if key in updates:
                if not isinstance(updates[key],list) or not updates[key] or any(not isinstance(x,str) for x in updates[key]):
                    raise HTTPException(status_code=422,detail=f"{key} musi być niepustą listą.")
                updates[key]=tuple(updates[key])
        try:
            candidate=replace(s,**updates); candidate.validate()
            if candidate.paper_quote_currency!="USDT": raise ValueError("Obsługiwane rynki są kwotowane wyłącznie w USDT.")
            for tf in candidate.timeframes: timeframe_to_ms(tf)
            if candidate.strategy_entry_timeframe not in candidate.timeframes or candidate.strategy_higher_timeframe not in candidate.timeframes:
                raise ValueError("Interwały strategii muszą znajdować się na liście pobieranych interwałów.")
            if not isinstance(amount_steps,dict): raise ValueError("Kroki ilości muszą być mapą symbol: krok.")
            cleaned_steps={}
            for symbol,value in amount_steps.items():
                number=float(value)
                if symbol not in candidate.symbols or not math.isfinite(number) or number<=0: raise ValueError("Każdy skonfigurowany rynek wymaga dodatniego kroku ilości.")
                cleaned_steps[symbol]=number
            sensitive={"paper_initial_capital","paper_quote_currency","paper_account_id","fee_bps","slippage_bps","paper_spread_bps"}
            if (app.state.paper_running and (set(updates)&sensitive or "amount_steps" in payload or
                    set(updates)&{"symbols","timeframes","enabled_strategies","strategy_entry_timeframe","strategy_higher_timeframe"})):
                raise ValueError("Zatrzymaj paper trading przed zmianą parametrów używanych przez symulację.")
            if app.state.analysis_running and (set(updates)-{"display_timezone"}):
                raise ValueError("Zatrzymaj analizę przed zmianą jej konfiguracji.")
            if candidate.paper_account_id==s.paper_account_id and (
                candidate.paper_initial_capital!=s.paper_initial_capital or
                candidate.paper_quote_currency!=s.paper_quote_currency):
                with sqlite3.connect(s.db_path,timeout=5) as db:
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_accounts'").fetchone()
                    account=(db.execute("SELECT initial_capital,quote_currency FROM paper_accounts WHERE account_id=?",
                        (s.paper_account_id,)).fetchone() if exists else None)
                if account and (abs(float(account[0])-candidate.paper_initial_capital)>1e-8 or account[1]!=candidate.paper_quote_currency):
                    if reset_confirmation!="RESET PAPER ACCOUNT":
                        raise ValueError("Zmiana salda/waluty wymaga jawnego resetu historii PAPER.")
                    if app.state.paper_running:
                        raise ValueError("Zatrzymaj paper trading przed resetem konta i zmianą salda.")
                    reset_existing_account=True
            if set(cleaned_steps) not in (set(),set(candidate.symbols)):
                raise ValueError("Ustaw krok ilości dla każdego skonfigurowanego rynku albo usuń wszystkie kroki.")
            with sqlite3.connect(candidate.db_path,timeout=5) as db:
                db.execute("CREATE TABLE IF NOT EXISTS web_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)")
                persisted={key:getattr(candidate,key) for key in allowed if key!="amount_steps" and hasattr(candidate,key)}
                persisted={key:list(value) if isinstance(value,tuple) else value for key,value in persisted.items()}
                persisted["amount_steps"]=cleaned_steps
                db.execute("INSERT OR REPLACE INTO web_settings(id,payload,updated_at_ms) VALUES (1,?,?)",
                    (json.dumps(persisted,separators=(",",":")),_utc_ms()))
        except (TypeError,ValueError,sqlite3.Error) as exc:
            raise HTTPException(status_code=422,detail=str(exc)) from exc
        if reset_existing_account:
            reset_engine=app.state.paper_engine or PaperTradingEngine(s.db_path,s.paper_account_id,
                s.paper_initial_capital,s.paper_quote_currency,s.fee_bps,s.slippage_bps,s.paper_spread_bps,
                short_term_exit_minutes=s.paper_short_trade_window_minutes)
            reset_engine.reset(candidate.paper_initial_capital,candidate.paper_quote_currency)
            record_event("warning","paper","Ustawienie nowego salda spowodowało potwierdzony reset historii PAPER.")
        app.state.settings=candidate
        app.state.paper_amount_steps=cleaned_steps
        app.state.paper_risk_manager=RiskManager.from_settings(candidate)
        if app.state.paper_engine is not None:
            app.state.paper_engine=PaperTradingEngine(candidate.db_path,candidate.paper_account_id,
                candidate.paper_initial_capital,candidate.paper_quote_currency,candidate.fee_bps,
                candidate.slippage_bps,candidate.paper_spread_bps,
                short_term_exit_minutes=candidate.paper_short_trade_window_minutes)
        record_event("info","settings","Ustawienia aplikacji zapisano trwale.")
        return settings_view()

    @app.post("/api/control/analysis", dependencies=[Depends(authorize)])
    async def control_analysis(payload: dict = Body(...)):
        action=payload.get("action")
        if action not in {"start","stop","pause","resume"}:
            raise HTTPException(status_code=422,detail="Akcja musi być start, stop, pause albo resume.")
        if action=="start":
            if app.state.analysis_running and not app.state.paused:
                raise HTTPException(status_code=409,detail="Analiza już działa.")
            app.state.analysis_running=True; app.state.paused=False
            try:
                result=await asyncio.to_thread(analyze_once)
                ensure_worker()
            except Exception as exc:
                app.state.analysis_running=False
                record_event("error","analysis",f"Analiza nie powiodła się: {type(exc).__name__}")
                raise HTTPException(status_code=503,detail="Analiza nie powiodła się; sprawdź diagnostykę i dostępność danych.") from exc
        elif action=="stop":
            app.state.analysis_running=False; result={"state":"stopping" if app.state.sync_task else "stopped"}
        elif action=="pause":
            app.state.paused=True; result={"state":"paused"}
        else:
            app.state.paused=False; ensure_worker(); result={"state":"running"}
        record_event("info","analysis",f"Polecenie analizy: {action}.")
        return {"action":action,"analysis_running":app.state.analysis_running,"paused":app.state.paused,**result}

    @app.post("/api/control/refresh", dependencies=[Depends(authorize)])
    async def refresh_market_data():
        if not app.state.cycle_lock.acquire(blocking=False):
            raise HTTPException(status_code=409,detail="Synchronizacja już trwa.")
        app.state.cycle_lock.release()
        try: result=await asyncio.to_thread(sync_once)
        except Exception as exc:
            record_event("error","market_data",f"Odświeżenie danych nie powiodło się: {type(exc).__name__}")
            raise HTTPException(status_code=503,detail="Nie udało się pobrać danych rynkowych; sprawdź połączenie i diagnostykę.") from exc
        if result.get("state")=="busy": raise HTTPException(status_code=409,detail="Synchronizacja już trwa.")
        record_event("info","market_data","Ręczne odświeżenie danych zakończone.")
        return result

    @app.post("/api/control/paper", dependencies=[Depends(authorize)])
    async def control_paper(payload: dict = Body(...)):
        action=payload.get("action")
        if action=="start":
            if app.state.paper_running:
                raise HTTPException(status_code=409,detail="Paper trading już działa.")
            try: ensure_paper_engine()
            except HTTPException: raise
            except (ValueError,sqlite3.Error) as exc: raise HTTPException(status_code=409,detail=str(exc)) from exc
            app.state.paper_running=True; app.state.paused=False; ensure_worker()
            record_event("info","paper","Uruchomiono symulację paper; bez prawdziwych zleceń.")
        elif action=="stop":
            app.state.paper_running=False
            record_event("info","paper","Zatrzymano nowe decyzje paper; historia i otwarte pozycje pozostały bez zmian.")
        elif action=="close":
            symbol=payload.get("symbol")
            if symbol not in app.state.settings.symbols: raise HTTPException(status_code=404,detail="Nieznany symbol.")
            if payload.get("confirm")!=f"CLOSE {symbol}": raise HTTPException(status_code=400,detail=f"Potwierdź zamknięcie wpisując CLOSE {symbol}.")
            engine=ensure_paper_engine(); position=engine.position(symbol)
            if position is None: raise HTTPException(status_code=404,detail="Brak otwartej pozycji dla tego rynku.")
            step=app.state.paper_amount_steps.get(symbol)
            rows=store().load(app.state.settings.exchange,symbol,app.state.settings.strategy_entry_timeframe,1)
            if not step or not rows: raise HTTPException(status_code=409,detail="Brak precyzji ilości albo aktualnej ceny do bezpiecznego zamknięcia.")
            candle=rows[-1]; now=_utc_ms()
            if now-candle.timestamp_ms-timeframe_to_ms(app.state.settings.strategy_entry_timeframe)>app.state.settings.max_market_data_age_ms:
                raise HTTPException(status_code=409,detail="Ostatnia cena jest nieaktualna; odśwież dane przed zamknięciem.")
            fill=engine.close_position(symbol,position.quantity,candle.close,max(now,position.opened_at_ms+1),"manual_close",step)
            record_event("info","paper",f"Ręcznie zamknięto symulowaną pozycję {symbol}.")
            return {"action":action,"fill":asdict(fill)}
        elif action=="reset":
            if app.state.paper_running:
                raise HTTPException(status_code=409,detail="Najpierw zatrzymaj paper trading.")
            if payload.get("confirm")!="RESET PAPER ACCOUNT":
                raise HTTPException(status_code=400,detail="Potwierdź dokładnie: RESET PAPER ACCOUNT.")
            engine=ensure_paper_engine(require_amount_steps=False); engine.reset()
            record_event("warning","paper","Zresetowano konto paper, pozycje, wypełnienia i historię zdarzeń.")
        else:
            raise HTTPException(status_code=422,detail="Akcja musi być start, stop, close albo reset.")
        return {"action":action,"paper_running":app.state.paper_running,"simulated_only":True}

    @app.post("/api/backtests/run", status_code=202, dependencies=[Depends(authorize)])
    async def start_backtest(payload: dict = Body(...)):
        if app.state.backtest.get("state") in {"queued","running"}:
            raise HTTPException(status_code=409,detail="Backtest już trwa.")
        required={"symbols","strategy","entry_timeframe","higher_timeframe","since","until","amount_steps"}
        if not required.issubset(payload): raise HTTPException(status_code=422,detail="Brakuje wymaganych parametrów backtestu.")
        s=app.state.settings
        try:
            symbols=payload["symbols"]
            if not isinstance(symbols,list) or not symbols or set(symbols)-set(s.symbols): raise ValueError("Wybierz skonfigurowane symbole.")
            strategy=payload["strategy"]
            if strategy not in StrategyEngine.NAMES: raise ValueError("Nieznana strategia.")
            entry_tf,higher_tf=payload["entry_timeframe"],payload["higher_timeframe"]
            if entry_tf==higher_tf or entry_tf not in s.timeframes or higher_tf not in s.timeframes: raise ValueError("Interwały muszą być różne i skonfigurowane.")
            start_ms,end_ms=_parse_utc(payload["since"]),_parse_utc(payload["until"])
            if end_ms<=start_ms: raise ValueError("Data końcowa musi być późniejsza od początkowej.")
            steps=payload["amount_steps"]
            if not isinstance(steps,dict) or any(symbol not in steps or not math.isfinite(float(steps[symbol])) or float(steps[symbol])<=0 for symbol in symbols): raise ValueError("Podaj dodatni krok ilości dla każdego symbolu.")
            initial=float(payload.get("initial_capital",s.paper_initial_capital)); risk=float(payload.get("risk_per_trade",s.max_risk_per_trade))
            fee=float(payload.get("fee_bps",s.fee_bps)); slip=float(payload.get("slippage_bps",s.slippage_bps))
            if not all(math.isfinite(v) for v in (initial,risk,fee,slip)) or initial<=0 or not 0<risk<=.02 or min(fee,slip)<0: raise ValueError("Nieprawidłowe saldo, ryzyko lub koszty.")
        except (TypeError,ValueError,KeyError) as exc: raise HTTPException(status_code=422,detail=str(exc)) from exc
        job_id=f"bt-{_utc_ms()}-{threading.get_ident()}"
        app.state.backtest={"state":"queued","job_id":job_id,"error":None,"result_ids":[],"started_at_ms":_utc_ms()}
        def execute():
            app.state.backtest={**app.state.backtest,"state":"running"}
            record_event("info","backtest",f"Backtest rozpoczęto: {', '.join(symbols)} · {strategy}.")
            ids=[]
            try:
                risk_limits=replace(RiskManager.from_settings(s).limits,max_position_risk_fraction=risk)
                strategy_config=StrategyConfig(enabled=(strategy,),entry_timeframe=entry_tf,higher_timeframe=higher_tf,
                    fee_bps_per_side=fee,slippage_bps_per_side=slip,spread_bps=s.paper_spread_bps,
                    min_net_risk_reward=s.strategy_min_net_risk_reward,min_score=s.strategy_min_score,
                    weights=ScoreWeights(*s.score_weights))
                engine=BacktestEngine(store())
                reports=[]
                for symbol in symbols:
                    histories={tf:store().load(s.exchange,symbol,tf,None,include_incomplete=True) for tf in (entry_tf,higher_tf)}
                    config=BacktestConfig(symbol,s.exchange,entry_tf,higher_tf,start_ms,end_ms,initial,
                        s.paper_quote_currency,fee,slip,s.paper_spread_bps,float(steps[symbol]),
                        strategy=strategy_config,risk_limits=risk_limits)
                    reports.append(engine.run(histories,config))
                for report in reports:
                    with sqlite3.connect(s.db_path) as db:
                        row=db.execute("SELECT id FROM backtest_results WHERE symbol=? AND timeframe=? AND timestamp_ms=? ORDER BY id DESC LIMIT 1",
                            (report.config["symbol"],entry_tf,end_ms)).fetchone()
                    if row: ids.append(row[0])
                app.state.backtest={"state":"completed","job_id":job_id,"error":None,"result_ids":ids,
                    "finished_at_ms":_utc_ms(),"summary":[{"run_id":r.run_id,"symbol":r.config["symbol"],"metrics":r.metrics} for r in reports]}
                record_event("info","backtest",f"Backtest zakończony poprawnie ({len(reports)} raportów).")
            except Exception as exc:
                app.state.backtest={"state":"failed","job_id":job_id,"error":f"{type(exc).__name__}: {exc}","result_ids":[],"finished_at_ms":_utc_ms()}
                record_event("error","backtest",f"Backtest nie powiódł się: {type(exc).__name__}.")
        app.state.backtest_executor.submit(execute)
        return {"job_id":job_id,"state":"queued"}

    @app.get("/api/backtests/status", dependencies=[Depends(authorize)])
    def backtest_status(): return app.state.backtest

    @app.get("/api/backtests/{result_id}", dependencies=[Depends(authorize)])
    def backtest_detail(result_id:int):
        with sqlite3.connect(app.state.settings.db_path) as db:
            row=db.execute("SELECT id,symbol,timeframe,timestamp_ms,source,payload FROM backtest_results WHERE id=?",
                           (result_id,)).fetchone()
        if row is None: raise HTTPException(status_code=404,detail="Nie znaleziono raportu backtestu.")
        return {"id":row[0],"symbol":row[1],"timeframe":row[2],"timestamp_ms":row[3],
                "source":row[4],"result":json.loads(row[5])}

    @app.get("/api/diagnostics", dependencies=[Depends(authorize)])
    def diagnostics(limit:int=Query(100,ge=1,le=500)):
        s=app.state.settings; events=[]; cutoff_ms=_utc_ms()-_LOG_WINDOW_MS
        try:
            with sqlite3.connect(s.db_path) as db:
                _register_event_timestamp_function(db)
                try:
                    events=[{"id":r[0],"timestamp_ms":r[1],"level":r[2],"category":r[3],"message":r[4]} for r in
                        reversed(db.execute("SELECT id,timestamp_ms,level,category,message FROM web_events "
                        "WHERE event_timestamp_ms(timestamp_ms)>=? "
                        "ORDER BY event_timestamp_ms(timestamp_ms) DESC,id DESC LIMIT ?",(cutoff_ms,limit)).fetchall())]
                    events=[{**event,"timestamp_ms":_event_timestamp_ms(event["timestamp_ms"])} for event in events]
                except sqlite3.Error: pass
                runs=[{"symbol":r[0],"timeframe":r[1],"timestamp_ms":r[2],"complete":bool(r[3]),"source":r[4]} for r in
                    db.execute("SELECT symbol,timeframe,fetched_at_ms,complete,source FROM market_data_runs ORDER BY fetched_at_ms DESC LIMIT ?",(min(limit,100),)).fetchall()]
        except sqlite3.Error: runs=[]
        dependencies={name:__import__("importlib.util",fromlist=["find_spec"]).find_spec(name) is not None for name in ("ccxt","fastapi","uvicorn","pytest")}
        return {"backend":"ok","database":"ok" if Path(s.db_path).exists() or s.db_path==":memory:" else "not_initialized",
            "market_connection":app.state.market_connection,"sync":app.state.sync_status,
            "analysis_running":app.state.analysis_running,"paper_running":app.state.paper_running,
            "paused":app.state.paused,"pid":os.getpid(),"version":"0.3.0","python":__import__("sys").version.split()[0],
            "dependencies":dependencies,"backtest":app.state.backtest,"events":events,"market_data_runs":runs,
            "market_data_health":app.state.market_monitor.snapshot(),
            "market_data_update":{"rest_fallback_poll_seconds":s.market_rest_poll_seconds,
                "rest_fallback_resolution":"ticker snapshots; not every trade"}}

    @app.get("/api/diagnostics/events", dependencies=[Depends(authorize)])
    def diagnostic_events(after_id:int|None=Query(None,ge=0),limit:int=Query(100,ge=1,le=500)):
        """Return recent events in event-time order while paging by stable insertion ID."""
        events=[]; latest_event_id=0; latest_event_timestamp_ms=None
        cutoff_ms=_utc_ms()-_LOG_WINDOW_MS
        try:
            with sqlite3.connect(app.state.settings.db_path) as db:
                db.execute("BEGIN")
                db.row_factory=sqlite3.Row
                _register_event_timestamp_function(db)
                latest=db.execute("SELECT id,timestamp_ms FROM web_events ORDER BY id DESC LIMIT 1").fetchone()
                if latest:
                    latest_event_id=int(latest[0])
                if after_id is None:
                    rows=db.execute("SELECT id,timestamp_ms,level,category,message FROM web_events "
                        "WHERE event_timestamp_ms(timestamp_ms)>=? "
                        "ORDER BY event_timestamp_ms(timestamp_ms) DESC,id DESC LIMIT ?",
                        (cutoff_ms,limit)).fetchall()
                    rows=list(reversed(rows))
                    next_after_id=latest_event_id
                else:
                    rows=db.execute("SELECT id,timestamp_ms,level,category,message FROM web_events "
                        "WHERE id>? ORDER BY id LIMIT ?",(after_id,limit)).fetchall()
                    next_after_id=int(rows[-1]["id"]) if rows else after_id
                events=[dict(row) for row in rows
                        if (_event_timestamp_ms(row["timestamp_ms"]) or 0)>=cutoff_ms]
                events.sort(key=lambda event:(_event_timestamp_ms(event["timestamp_ms"]) or 0,event["id"]))
                events=[{**event,"timestamp_ms":_event_timestamp_ms(event["timestamp_ms"])} for event in events]
                latest_visible=db.execute("SELECT MAX(event_timestamp_ms(timestamp_ms)) FROM web_events "
                                          "WHERE event_timestamp_ms(timestamp_ms)>=?",
                                          (cutoff_ms,)).fetchone()
                if latest_visible and latest_visible[0] is not None:
                    latest_event_timestamp_ms=int(latest_visible[0])
        except sqlite3.Error as exc:
            raise HTTPException(status_code=503,detail="Dziennik diagnostyczny jest chwilowo niedostępny.") from exc
        return {"events":events,"latest_event_id":latest_event_id,
                "latest_event_timestamp_ms":latest_event_timestamp_ms,
                "next_after_id":next_after_id if after_id is not None or latest_event_id else 0}

    @app.get("/api/market-data/health", dependencies=[Depends(authorize)])
    def market_data_health():
        s=app.state.settings
        return {"mode":"public_read_only","websocket":"CCXT Pro public trade stream when supported",
            "rest_fallback_poll_seconds":s.market_rest_poll_seconds,
            "rest_fallback_resolution":"ticker snapshots; does not deliver every trade",
            "ohlcv_reconciliation_seconds":max(30,int(os.getenv("TRADINGAGENT_API_SYNC_SECONDS","60"))),
            "markets":app.state.market_monitor.snapshot()}

    @app.get("/api/backtests", dependencies=[Depends(authorize)])
    def backtests(limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0)):
        with sqlite3.connect(app.state.settings.db_path) as db:
            try: rows=db.execute("SELECT id,symbol,timeframe,timestamp_ms,source,payload FROM backtest_results ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()
            except sqlite3.Error: rows=[]
        return {"items":[{"id":r[0],"symbol":r[1],"timeframe":r[2],"timestamp_ms":r[3],"source":r[4],"result":json.loads(r[5])} for r in rows],"limit":limit,"offset":offset}

    @app.get("/api/paper/events", dependencies=[Depends(authorize)])
    def paper_events(after_id:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=500)):
        s=app.state.settings
        engine=app.state.paper_engine or PaperTradingEngine(s.db_path,s.paper_account_id,
            s.paper_initial_capital,s.paper_quote_currency,s.fee_bps,s.slippage_bps,
            s.paper_spread_bps,short_term_exit_minutes=s.paper_short_trade_window_minutes)
        cutoff_ms=_utc_ms()-_LOG_WINDOW_MS
        events,next_after_id=engine.events_page_after(after_id,limit,cutoff_ms)
        latest=engine.latest_event_since(cutoff_ms)
        return {"events":events,"latest_event_id":engine.latest_event_id(),
                "latest_event_timestamp_ms":latest["timestamp_ms"] if latest else None,
                "next_after_id":next_after_id}

    @app.get("/api/paper", dependencies=[Depends(authorize)])
    def paper(limit:int=Query(1000,ge=1,le=5000),include_events:bool=Query(True)):
        s=app.state.settings
        engine = app.state.paper_engine or PaperTradingEngine(s.db_path,s.paper_account_id,
            s.paper_initial_capital,s.paper_quote_currency,
            s.fee_bps,s.slippage_bps,s.paper_spread_bps,
            short_term_exit_minutes=s.paper_short_trade_window_minutes)
        prices={}
        db=store()
        health_by_symbol={item["symbol"]:item for item in app.state.market_monitor.snapshot()}
        for symbol in s.symbols:
            health=health_by_symbol.get(symbol,{})
            if not health.get("stale") and health.get("latest_price") is not None:
                prices[symbol]=health["latest_price"]
            else:
                rows=db.load(s.exchange,symbol,s.strategy_entry_timeframe,1)
                if rows: prices[symbol]=rows[-1].close
        snapshot=engine.snapshot(prices)
        if include_events:
            events,event_cursor=engine.recent_events_page(_utc_ms()-_LOG_WINDOW_MS,limit)
        else:
            events=[]; event_cursor=engine.latest_event_id()
        fills=engine.fills()
        roundtrips=[]
        for original in engine.roundtrips():
            trade=dict(original)
            entries=[f for f in fills if f.symbol==trade["symbol"] and f.role=="ENTRY" and f.timestamp_ms==trade["opened_at_ms"]]
            exits=[f for f in fills if f.symbol==trade["symbol"] and f.role=="EXIT" and
                   trade["opened_at_ms"]<f.timestamp_ms<=trade["closed_at_ms"]]
            entry_qty=sum(f.quantity for f in entries); exit_qty=sum(f.quantity for f in exits)
            trade["entry_price"]=(sum(f.quantity*f.price for f in entries)/entry_qty if entry_qty else None)
            trade["exit_price"]=(sum(f.quantity*f.price for f in exits)/exit_qty if exit_qty else None)
            roundtrips.append(trade)
        strategy_stats={}
        for strategy in sorted({trade["strategy"] for trade in roundtrips}):
            rows=[trade for trade in roundtrips if trade["strategy"]==strategy]
            wins=[float(trade["net_pnl"]) for trade in rows if float(trade["net_pnl"])>0]
            pnl=[float(trade["net_pnl"]) for trade in rows]
            strategy_stats[strategy]={"trades":len(rows),"win_rate_pct":100*len(wins)/len(rows),
                "net_pnl":sum(pnl),"average_pnl":sum(pnl)/len(pnl)}
        wait_reasons=[]
        for symbol, reasons in app.state.paper_wait_reasons.items():
            if reasons:
                wait_reasons.append({"symbol": symbol, "issues": list(reasons)[:10]})
        directional_analysis=[]
        for symbol in s.symbols:
            cycle=app.state.paper_last_cycle_by_symbol.get(symbol,{})
            executions={(item.get("strategy"),item.get("candidate_direction")):item
                        for item in cycle.get("entry_results",())}
            for signal in cycle.get("signals",()):
                side=signal.candidate_direction or signal.position_side
                directional_analysis.append({
                    "symbol":symbol,"strategy":signal.strategy,"candidate_direction":side,
                    "direction":signal.direction,"status":signal.status,
                    "score":signal.confidence,"timestamp":signal.timestamp.isoformat(),
                    "reasons":list(signal.rejection_reasons or signal.missing_data),
                    "execution":executions.get((signal.strategy,side)),
                })
        positions=[]
        for position in snapshot.positions:
            item = asdict(position)
            item["mark_price"] = snapshot.prices.get(position.symbol, position.average_entry)
            positions.append(item)
        return {"simulated":True,"snapshot":asdict(snapshot),
                "positions":positions,"trades":roundtrips[-limit:],"strategy_stats":strategy_stats,
                "fills":[asdict(fill) for fill in fills[-limit:]],
                "events":events,"event_cursor":event_cursor,
                "equity_curve":[asdict(point) for point in engine.equity_curve()[-limit:]],
                "wait_reasons":wait_reasons,
                "wait_summary":app.state.paper_last_cycle.get("wait_summary") or "Brak aktywnych sygnałów.",
                "paper_debug":app.state.paper_last_cycle.get("wait_reasons", []),
                "directional_analysis":directional_analysis}

    @app.get("/",response_class=HTMLResponse,include_in_schema=False)
    def dashboard():
        page=Path(__file__).resolve().parents[1]/"dashboard"/"index.html"
        return page.read_text(encoding="utf-8") if page.is_file() else "Dashboard asset missing"

    return app

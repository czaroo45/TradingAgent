"""Market-data synchronization, diagnostics, and conditional market analysis CLI."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tradingagent.analysis import analyze
from tradingagent.config import Settings
from tradingagent.market_data import CcxtMarketData, MarketDataError, timeframe_to_ms, validate_ohlcv
from tradingagent.models import DataQuality, MarketTicker, OrderBookSnapshot
from tradingagent.storage import CandleStore
from tradingagent.strategies import StrategyConfig, StrategyEngine
from tradingagent.scoring import ScoreWeights
from tradingagent.backtesting import BacktestConfig, BacktestEngine
from tradingagent.risk import RiskManager
from tradingagent.notifications import TelegramNotifier


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        result = {key: _jsonable(val) for key, val in asdict(value).items()}
        if isinstance(value, DataQuality):
            result.update(acceptable=value.acceptable, reasons=value.reasons, warnings=value.warnings)
        elif isinstance(value, (MarketTicker, OrderBookSnapshot)):
            result["spread_bps"] = value.spread_bps
        return result
    if isinstance(value, dict):
        return {key: _jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _timestamp(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use ISO-8601, e.g. 2025-01-01T00:00:00Z") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("date must include timezone, preferably Z/UTC")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _merge_response_defects(final_quality, reports):
    """Keep defects detected before normalization/deduplication in the signal gate."""
    duplicates = set(final_quality.duplicate_timestamps)
    invalid_rows = list(final_quality.invalid_rows)
    non_monotonic = final_quality.non_monotonic
    truncated = final_quality.pagination_truncated
    for report in reports:
        duplicates.update(report.duplicate_timestamps)
        invalid_rows.extend(report.invalid_rows)
        non_monotonic = non_monotonic or report.non_monotonic
        truncated = truncated or report.pagination_truncated
    return replace(final_quality, duplicate_timestamps=tuple(sorted(duplicates)),
                   invalid_rows=tuple(invalid_rows), non_monotonic=non_monotonic,
                   pagination_truncated=truncated)


def _stored_source_is_supported(store: CandleStore, exchange: str, symbol: str, timeframe: str) -> bool:
    sources=store.sources(exchange,symbol,timeframe)
    return len(sources)==1 and sources[0] in {"ccxt",exchange}


def _client(settings: Settings) -> CcxtMarketData:
    return CcxtMarketData(settings.exchange, timeout_ms=settings.timeout_ms,
                          max_concurrency=settings.max_concurrency,
                          cache_ttl_seconds=settings.market_cache_seconds)


def _record_signals_and_notify(store: CandleStore, settings: Settings, signals) -> None:
    try:
        notifier = TelegramNotifier.from_env(settings.db_path)
    except (ValueError, KeyError, OSError):
        logging.getLogger(__name__).error("Telegram configuration unavailable")
        notifier = None
    for signal in signals:
        try:
            store.save_payload("signals", signal.symbol, settings.strategy_entry_timeframe,
                int(signal.timestamp.timestamp()*1000), signal.source, _jsonable(signal))
            if notifier:
                notifier.notify_signal(signal)
        except Exception:
            # External errors may contain credential-bearing URLs; log only a safe category.
            logging.getLogger(__name__).error("Signal persistence or notification failed for %s", signal.symbol)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading Agent public market-data tools")
    parser.add_argument("--offline", action="store_true", help="use only candles stored in SQLite")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--check-config", action="store_true", help="validate and print effective configuration")
    parser.add_argument("--check-connection", action="store_true", help="check public market/ticker access")
    parser.add_argument("--history", metavar="SYMBOL", help="download historical range, e.g. BTC/USDT")
    parser.add_argument("--timeframe", default="1h", help="timeframe for history/last-candles/quality")
    parser.add_argument("--since", type=_timestamp, help="history range start, ISO-8601 with timezone")
    parser.add_argument("--until", type=_timestamp, help="history range end, ISO-8601 with timezone")
    parser.add_argument("--last-candles", metavar="SYMBOL", help="show most recent stored/fetched bars")
    parser.add_argument("--count", type=int, default=10, help="number of recent candles to show")
    parser.add_argument("--quality", metavar="SYMBOL", help="validate stored candle quality")
    parser.add_argument("--backtest", metavar="SYMBOL", help="backtest stored history, e.g. BTC/USDT")
    parser.add_argument("--entry-timeframe", default="15m", help="backtest signal timeframe")
    parser.add_argument("--higher-timeframe", default="4h", help="higher timeframe confirmation")
    parser.add_argument("--amount-step", type=float, help="venue amount precision step; required for backtests")
    parser.add_argument("--compare-strategies", action="store_true", help="run enabled strategies independently on identical data")
    parser.add_argument("--report-dir", help="optional JSON, CSV, equity SVG, and drawdown SVG output directory")
    parser.add_argument("--serve", action="store_true", help="serve read-only API and dashboard (requires .[api])")
    parser.add_argument("--host", default="127.0.0.1", help="API bind address; non-local addresses require TRADINGAGENT_API_TOKEN")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    if args.serve:
        if not 1 <= args.port <= 65535:
            parser.error("--port must be 1..65535")
        if args.host not in {"127.0.0.1","localhost","::1"} and not __import__("os").getenv("TRADINGAGENT_API_TOKEN"):
            parser.error("non-local API binding requires TRADINGAGENT_API_TOKEN")
        try:
            import uvicorn
            from tradingagent.api.app import create_app
        except ImportError:
            parser.error("install dashboard dependencies with python -m pip install -e '.[api]'")
        uvicorn.run(create_app(settings),host=args.host,port=args.port,log_level="info")
        return 0
    if args.check_config:
        config=asdict(settings)
        for secret in ("telegram_bot_token",):
            if config.get(secret): config[secret]="[REDACTED]"
        print(json.dumps(config, indent=2))
        return 0
    store = CandleStore(settings.db_path)
    if args.offline and (args.check_connection or args.history):
        parser.error("--check-connection and --history require online access")
    if args.backtest and (args.since is None or args.until is None):
        parser.error("--backtest requires --since and --until")
    if args.backtest and args.amount_step is None:
        parser.error("--backtest requires --amount-step from exchange market precision")

    try:
        client = None if args.offline or args.backtest else _client(settings)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    try:
        if args.backtest:
            rows = {
                tf: store.load(settings.exchange, args.backtest, tf, None, include_incomplete=True)
                for tf in (args.entry_timeframe, args.higher_timeframe)
            }
            strategy_config = StrategyConfig(enabled=settings.enabled_strategies,
                entry_timeframe=args.entry_timeframe,higher_timeframe=args.higher_timeframe,
                fee_bps_per_side=settings.fee_bps,slippage_bps_per_side=settings.slippage_bps,
                min_net_risk_reward=settings.strategy_min_net_risk_reward,
                min_score=settings.strategy_min_score,weights=ScoreWeights(*settings.score_weights),
                spread_bps=settings.paper_spread_bps)
            backtest_config = BacktestConfig(args.backtest,settings.exchange,
                entry_timeframe=args.entry_timeframe,higher_timeframe=args.higher_timeframe,
                start_ms=args.since,end_ms=args.until,initial_capital=settings.paper_initial_capital,
                quote_currency=settings.paper_quote_currency,fee_bps_per_side=settings.fee_bps,
                slippage_bps=settings.slippage_bps,spread_bps=settings.paper_spread_bps,
                amount_step=args.amount_step,strategy=strategy_config,
                risk_limits=RiskManager.from_settings(settings).limits)
            engine=BacktestEngine(store)
            if args.compare_strategies:
                result=engine.compare(rows,backtest_config,settings.enabled_strategies)
            else:
                result=engine.run(rows,backtest_config)
            if args.report_dir:
                reports=result.values() if isinstance(result,dict) else (result,)
                for report in reports:
                    report.export_json(f"{args.report_dir}/{report.run_id}.json")
                    report.export_csv(f"{args.report_dir}/{report.run_id}-trades.csv")
                    report.export_fills_csv(f"{args.report_dir}/{report.run_id}-fills.csv")
                    report.export_charts(args.report_dir)
            print(json.dumps(_jsonable(result),indent=2))
            return 0

        if args.check_connection:
            assert client is not None
            client._check_market(settings.symbols[0], settings.timeframes[0])
            ticker = client.fetch_ticker(settings.symbols[0])
            store.save_ticker(settings.exchange, ticker.symbol, ticker.received_timestamp_ms,
                              "ccxt", _jsonable(ticker))
            book = client.fetch_order_book(settings.symbols[0])
            if book:
                store.save_order_book(settings.exchange, book.symbol, book.received_timestamp_ms,
                                      "ccxt", _jsonable(book))
            print(json.dumps({"status": "ok", "exchange": settings.exchange,
                              "ohlcv_supported_timeframes": [tf for tf in settings.timeframes
                                  if tf in (client.exchange.timeframes or {})],
                              "market": _jsonable(ticker), "order_book": _jsonable(book)}, indent=2))
            return 0

        if args.history:
            if args.since is None or args.until is None:
                parser.error("--history requires --since and --until")
            assert client is not None
            stored_latest = store.latest_timestamp(settings.exchange, args.history, args.timeframe)
            fetch_since = stored_latest if stored_latest is not None and args.since < stored_latest < args.until else args.since
            candles, quality = client.fetch_history(args.history, args.timeframe, fetch_since, args.until)
            saved = store.upsert(settings.exchange, args.history, args.timeframe, candles, source="ccxt")
            store.record_market_data_run(settings.exchange, args.history, args.timeframe,
                args.since, args.until, int(datetime.now(timezone.utc).timestamp() * 1000),
                "ccxt", all(c.complete for c in candles) and quality.acceptable, saved,
                {"provider": "ccxt", "page_limit": 500, "max_pages": 100,
                 "resume_since_ms": fetch_since}, _jsonable(quality))
            print(json.dumps({"saved": saved, "quality": _jsonable(quality)}, indent=2))
            return 0 if quality.acceptable else 2

        if args.last_candles:
            if args.count <= 0:
                parser.error("--count must be positive")
            if client:
                rows, quality = client.fetch_candles(args.last_candles, args.timeframe, args.count)
                store.upsert(settings.exchange, args.last_candles, args.timeframe, rows, source="ccxt")
                store.record_market_data_run(settings.exchange, args.last_candles, args.timeframe,
                    rows[0].timestamp_ms if rows else None, rows[-1].timestamp_ms if rows else None,
                    int(datetime.now(timezone.utc).timestamp() * 1000), "ccxt",
                    all(c.complete for c in rows) and quality.acceptable, len(rows),
                    {"provider": "ccxt", "purpose": "last_candles", "limit": args.count},
                    _jsonable(quality))
            else:
                rows = store.load(settings.exchange, args.last_candles, args.timeframe,
                                  args.count, include_incomplete=True)
                rows, quality = validate_ohlcv([[c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume]
                                                 for c in rows], settings.exchange, args.last_candles,
                                                args.timeframe, int(datetime.now(timezone.utc).timestamp() * 1000))
            if not _stored_source_is_supported(store,settings.exchange,args.last_candles,args.timeframe):
                quality = replace(quality, source_mismatch=True)
            output = {"candles": [_jsonable(c) for c in rows], "quality": _jsonable(quality)}
            print(json.dumps(output, indent=2))
            return 0 if quality.acceptable else 2

        if args.quality:
            rows = store.load(settings.exchange, args.quality, args.timeframe, None,
                              include_incomplete=True)
            candles, quality = validate_ohlcv(
                [[c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume] for c in rows],
                settings.exchange, args.quality, args.timeframe,
                int(datetime.now(timezone.utc).timestamp() * 1000))
            if not _stored_source_is_supported(store,settings.exchange,args.quality,args.timeframe):
                quality = replace(quality, source_mismatch=True)
            print(json.dumps({"quality": _jsonable(quality)}, indent=2))
            return 0 if quality.acceptable else 2

        if client:
            analyses, strategy_signals, failures, tickers, books, slippage_estimates = [], [], [], {}, {}, {}
            for symbol in settings.symbols:
                symbol_candles, symbol_analyses, symbol_qualities = {}, {}, {}
                try:
                    tickers[symbol] = client.fetch_ticker(symbol)
                    store.save_ticker(settings.exchange, symbol, tickers[symbol].received_timestamp_ms,
                                      "ccxt", _jsonable(tickers[symbol]))
                except (MarketDataError, ValueError, OSError) as exc:
                    tickers[symbol] = None
                    failures.append({"symbol": symbol, "error": f"ticker: {exc}"})
                try:
                    books[symbol] = client.fetch_order_book(symbol)
                    if books[symbol]:
                        store.save_order_book(settings.exchange, symbol, books[symbol].received_timestamp_ms,
                                              "ccxt", _jsonable(books[symbol]))
                        buy_slip = books[symbol].estimate_slippage_bps("buy", settings.cost_estimate_notional)
                        sell_slip = books[symbol].estimate_slippage_bps("sell", settings.cost_estimate_notional)
                        if buy_slip is not None and sell_slip is not None:
                            slippage_estimates[symbol] = (buy_slip + sell_slip) / 2
                except (MarketDataError, ValueError, OSError) as exc:
                    books[symbol] = None
                    failures.append({"symbol": symbol, "error": f"order book: {exc}"})
                for timeframe in settings.timeframes:
                    try:
                        latest = store.latest_timestamp(settings.exchange, symbol, timeframe)
                        if latest is None:
                            fetched, fetch_quality = client.fetch_candles(symbol, timeframe, settings.limit)
                            requested_since = fetched[0].timestamp_ms if fetched else None
                        else:
                            requested_since = latest
                            fetched, fetch_quality = client.fetch_history(symbol, timeframe,
                                latest, int(datetime.now(timezone.utc).timestamp() * 1000) + 1,
                                page_limit=min(settings.limit, 1000))
                        store.upsert(settings.exchange, symbol, timeframe, fetched, source="ccxt")
                        rows = store.load(settings.exchange, symbol, timeframe, settings.limit,
                                          include_incomplete=True)
                        normalized, quality = validate_ohlcv(
                            [[c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume] for c in rows],
                            settings.exchange, symbol, timeframe,
                            int(datetime.now(timezone.utc).timestamp() * 1000))
                        if not _stored_source_is_supported(store,settings.exchange,symbol,timeframe):
                            quality = replace(quality, source_mismatch=True)
                        response_reports = [fetch_quality]
                        if quality.gaps:
                            for left_ts, right_ts in quality.gaps:
                                repair_rows, repair_quality = client.fetch_history(
                                    symbol, timeframe, left_ts + timeframe_to_ms(timeframe), right_ts,
                                    page_limit=min(settings.limit, 1000))
                                response_reports.append(repair_quality)
                                store.upsert(settings.exchange, symbol, timeframe, repair_rows, source="ccxt")
                                store.record_market_data_run(settings.exchange, symbol, timeframe,
                                    left_ts + timeframe_to_ms(timeframe), right_ts,
                                    int(datetime.now(timezone.utc).timestamp() * 1000), "ccxt",
                                    all(c.complete for c in repair_rows) and repair_quality.acceptable, len(repair_rows),
                                    {"provider": "ccxt", "purpose": "gap_repair", "page_limit": min(settings.limit, 1000)},
                                    _jsonable(repair_quality))
                            rows = store.load(settings.exchange, symbol, timeframe, settings.limit,
                                              include_incomplete=True)
                            normalized, quality = validate_ohlcv(
                                [[c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume] for c in rows],
                                settings.exchange, symbol, timeframe,
                                int(datetime.now(timezone.utc).timestamp() * 1000))
                            if not _stored_source_is_supported(store,settings.exchange,symbol,timeframe):
                                quality = replace(quality, source_mismatch=True)
                        quality = _merge_response_defects(quality, response_reports)
                        store.record_market_data_run(settings.exchange, symbol, timeframe,
                            requested_since, int(datetime.now(timezone.utc).timestamp() * 1000),
                            int(datetime.now(timezone.utc).timestamp() * 1000), "ccxt",
                            all(c.complete for c in fetched) and quality.acceptable, len(fetched),
                            {"provider": "ccxt", "configured_limit": settings.limit,
                             "fetch_quality": _jsonable(fetch_quality)}, _jsonable(quality))
                        if not quality.acceptable:
                            failures.append({"symbol": symbol, "timeframe": timeframe,
                                             "quality": _jsonable(quality), "error": "analysis blocked by data quality"})
                            continue
                        if tickers[symbol] is None:
                            continue
                        symbol_candles[timeframe] = [c for c in normalized if c.complete]
                        symbol_qualities[timeframe] = quality
                        closed = [c for c in normalized if c.complete]
                        analysis = analyze(symbol, timeframe, closed, settings.fee_bps,
                                                settings.slippage_bps, settings.max_spread_bps,
                                                quality=quality, ticker=tickers[symbol],
                                                estimated_slippage_bps=slippage_estimates.get(symbol))
                        analyses.append(analysis)
                        symbol_analyses[timeframe] = analysis
                    except (MarketDataError, ValueError, OSError) as exc:
                        failures.append({"symbol": symbol, "timeframe": timeframe, "error": str(exc)})
                if symbol_candles:
                    strategy_signals.extend(StrategyEngine(StrategyConfig(
                        enabled=settings.enabled_strategies,
                        entry_timeframe=settings.strategy_entry_timeframe,
                        higher_timeframe=settings.strategy_higher_timeframe,
                        fee_bps_per_side=settings.fee_bps,
                        slippage_bps_per_side=slippage_estimates.get(symbol, settings.slippage_bps),
                        spread_bps=tickers[symbol].spread_bps if tickers[symbol] else None,
                        min_net_risk_reward=settings.strategy_min_net_risk_reward,
                        min_score=settings.strategy_min_score,
                        weights=ScoreWeights(*settings.score_weights))).evaluate(
                            symbol, settings.exchange, symbol_candles, symbol_analyses, symbol_qualities))
            _record_signals_and_notify(store, settings, strategy_signals)
            result = {"generated_at": datetime.now(ZoneInfo(settings.display_timezone)).isoformat(),
                      "exchange": settings.exchange, "mode": settings.mode,
                      "tickers": _jsonable(tickers), "order_books": _jsonable(books),
                      "slippage_estimate_bps": slippage_estimates,
                      "cost_estimate_notional": settings.cost_estimate_notional,
                      "analyses": _jsonable(analyses), "strategy_signals": _jsonable(strategy_signals),
                      "failures": failures}
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                for item in analyses:
                    print(f"{item.symbol:10} {item.timeframe:4} {item.regime:18} quality={item.signal_quality:2} scenario={item.scenario.direction if item.scenario else 'none'}")
                for signal in strategy_signals:
                    reasons=signal.rejection_reasons or signal.missing_data or signal.explanation
                    detail="; ".join(reasons[:2])
                    print(f"STRATEGY {signal.symbol:10} {signal.strategy:18} {signal.direction:8} score={signal.confidence:.1f} status={signal.status} reason={detail}")
                for failure in failures:
                    print(f"ERROR {failure.get('symbol')} {failure.get('timeframe', '')}: {failure.get('error')}")
            return 1 if failures else 0

        results, failures, strategy_signals = [], [], []
        for symbol in settings.symbols:
            symbol_candles, symbol_analyses, symbol_qualities = {}, {}, {}
            for timeframe in settings.timeframes:
                rows = store.load(settings.exchange, symbol, timeframe, settings.limit)
                candles, quality = validate_ohlcv(
                    [[c.timestamp_ms, c.open, c.high, c.low, c.close, c.volume] for c in rows],
                    settings.exchange, symbol, timeframe,
                    int(datetime.now(timezone.utc).timestamp() * 1000))
                if not _stored_source_is_supported(store,settings.exchange,symbol,timeframe):
                    quality = replace(quality, source_mismatch=True)
                if not quality.acceptable:
                    failures.append({"symbol": symbol, "timeframe": timeframe,
                                     "error": "analysis blocked by data quality", "quality": _jsonable(quality)})
                    continue
                results.append(analyze(symbol, timeframe, [c for c in candles if c.complete],
                                       settings.fee_bps, settings.slippage_bps, settings.max_spread_bps,
                                       quality=quality))
                symbol_candles[timeframe] = [c for c in candles if c.complete]
                symbol_qualities[timeframe] = quality
                symbol_analyses[timeframe] = results[-1]
            if symbol_candles:
                strategy_signals.extend(StrategyEngine(StrategyConfig(
                    enabled=settings.enabled_strategies,
                    entry_timeframe=settings.strategy_entry_timeframe,
                    higher_timeframe=settings.strategy_higher_timeframe,
                    fee_bps_per_side=settings.fee_bps,
                    slippage_bps_per_side=settings.slippage_bps,
                    min_net_risk_reward=settings.strategy_min_net_risk_reward,
                    min_score=settings.strategy_min_score,
                    weights=ScoreWeights(*settings.score_weights))).evaluate(
                        symbol, settings.exchange, symbol_candles, symbol_analyses, symbol_qualities))
        _record_signals_and_notify(store, settings, strategy_signals)
        payload = {"generated_at": datetime.now(ZoneInfo(settings.display_timezone)).isoformat(),
                   "exchange": settings.exchange, "mode": settings.mode,
                   "analyses": _jsonable(results), "strategy_signals": _jsonable(strategy_signals),
                   "failures": failures}
        print(json.dumps(payload, indent=2) if args.json else "\n".join(
            f"{x.symbol:10} {x.timeframe:4} {x.regime:18} quality={x.signal_quality:2}" for x in results))
        if not args.json:
            for signal in strategy_signals:
                reasons=signal.rejection_reasons or signal.missing_data or signal.explanation
                print(f"STRATEGY {signal.symbol:10} {signal.strategy:18} {signal.direction:8} score={signal.confidence:.1f} status={signal.status} reason={'; '.join(reasons[:2])}")
        return 1 if failures else 0
    except (MarketDataError, ValueError, OSError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "exchange": settings.exchange}, indent=2))
        else:
            logging.error("Market data command failed: %s", exc)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())

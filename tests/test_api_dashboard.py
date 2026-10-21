import json
import sqlite3
import shutil
import subprocess
import time
import re
from pathlib import Path
from datetime import datetime, timezone

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tradingagent.api.app import _paper_amount_steps, _persist_paper_wait_events, create_app
from tradingagent.config import Settings
from tradingagent.storage import CandleStore
from tradingagent.models import Analysis, Candle
from tradingagent.models import MarketTicker
from tradingagent.models import ScoreBreakdown, StrategySignal
from tradingagent.paper_trading import PaperTradingEngine, RiskManager, run_paper_cycle
from tradingagent.risk import InstrumentConstraints, PortfolioSnapshot, RiskRequest
from tradingagent.market_data import timeframe_to_ms


def test_paper_precision_requires_a_distinct_configured_step_per_market(monkeypatch):
    settings=Settings(symbols=("BTC/USDT","ETH/USDT"))
    monkeypatch.setenv("TRADINGAGENT_PAPER_AMOUNT_STEP","0.001")
    monkeypatch.delenv("TRADINGAGENT_PAPER_AMOUNT_STEPS",raising=False)
    assert _paper_amount_steps(settings)=={}
    monkeypatch.setenv("TRADINGAGENT_PAPER_AMOUNT_STEPS","BTC/USDT=0.001,ETH/USDT=0.01")
    assert _paper_amount_steps(settings)=={"BTC/USDT":0.001,"ETH/USDT":0.01}
    monkeypatch.setenv("TRADINGAGENT_PAPER_AMOUNT_STEPS","BTC/USDT=0.001")
    assert _paper_amount_steps(settings)=={}


def test_dashboard_api_serves_only_saved_market_data_and_paper_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    path=str(tmp_path/"market.db")
    settings=Settings(db_path=path,timeframes=("15m","4h"),
                      strategy_entry_timeframe="15m",strategy_higher_timeframe="4h")
    store=CandleStore(path)
    candles=[]
    for i in range(60):
        p=100+i*.1
        candles.append(Candle(i*900_000,p,p+1,p-1,p+.2,100,"fixture",True))
    store.upsert("binance","BTC/USDT","15m",candles,source="ccxt")
    # A second configured interval is deliberately absent and must remain unavailable.
    with TestClient(create_app(settings)) as client:
        health=client.get("/health").json()
        assert health["live_trading"] is False
        market_health=client.get("/api/market-data/health").json()
        assert market_health["rest_fallback_poll_seconds"] == settings.market_rest_poll_seconds
        assert {item["symbol"] for item in market_health["markets"]} == set(settings.symbols)
        assert market_health["rest_fallback_resolution"].startswith("ticker snapshots")
        assert "Trading Agent" in client.get("/").text
        market=client.get("/api/market").json()["items"]
        btc=next(x for x in market if x["symbol"]=="BTC/USDT")
        assert btc["price"] == candles[-1].close and btc["source"] == "ccxt"
        missing=next(x for x in market if x["symbol"]=="ETH/USDT")
        assert missing["price"] is None
        response=client.get("/api/candles/BTC%2FUSDT?timeframe=15m&limit=5")
        assert response.status_code==200 and len(response.json()["items"])==5
        paper=client.get("/api/paper").json()
        assert paper["simulated"] is True
        assert paper["directional_analysis"] == []
        assert paper["event_cursor"] == 0
        event_response=client.get("/api/paper/events?after_id=0&limit=10")
        assert event_response.status_code==200
        assert event_response.json()=={"events":[],"latest_event_id":0,
                                       "latest_event_timestamp_ms":None,"next_after_id":0}
        dashboard=client.get("/").text
        assert 'id="paperDirections"' in dashboard
        assert 'id="marketHealth"' in dashboard
        assert 'id="marketPollCadence"' in dashboard
        assert "/api/market-data/health" in dashboard
        assert "Ostatnia ocena LONG / SHORT" in dashboard
        assert 'id="paperLogStatus"' in dashboard
        assert "/api/paper/events?after_id=" in dashboard
        assert "setInterval(refreshPaperLogs,3000)" in dashboard
        assert client.get("/api/candles/BTC%2FUSDT?timeframe=unsupported").status_code==422


def test_paper_events_endpoint_reads_only_new_persisted_events(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    path=str(tmp_path/"paper-events.db")
    settings=Settings(db_path=path)
    app=create_app(settings)
    engine=PaperTradingEngine(path,settings.paper_account_id,
        settings.paper_initial_capital,settings.paper_quote_currency)
    app.state.paper_engine=engine
    cycle=run_paper_cycle(symbol="BTC/USDT",exchange=settings.exchange,histories={},
        settings=settings,engine=engine,risk_manager=RiskManager(),amount_step=0.001,
        now_ms=int(time.time()*1000))
    assert cycle["wait_reasons"]
    wait_ids=_persist_paper_wait_events(engine,"BTC/USDT",cycle,
        settings.strategy_entry_timeframe,900_000)
    assert len(wait_ids)==min(8,len(cycle["wait_reasons"]))
    route=next(route for route in app.routes if route.path=="/api/paper/events")
    before_first_wait=wait_ids[0]-1
    first=route.endpoint(after_id=before_first_wait,limit=1)
    assert first["events"][0]["id"]==wait_ids[0]
    assert first["events"][0]["kind"]=="wait"
    assert first["events"][0]["symbol"]=="BTC/USDT"
    assert first["events"][0]["payload"]["reason"]==cycle["wait_reasons"][0]
    assert first["events"][0]["payload"]["timeframe"]==settings.strategy_entry_timeframe
    assert first["latest_event_id"]==wait_ids[-1]
    received=[first["events"][0]["id"]]
    cursor=received[-1]
    while cursor<wait_ids[-1]:
        page=route.endpoint(after_id=cursor,limit=2)
        assert page["events"]
        received.extend(event["id"] for event in page["events"])
        cursor=received[-1]
    assert received==list(wait_ids)
    assert list(route.endpoint(after_id=wait_ids[-1],limit=1)["events"])==[]
    later_cycle={"analyzed_at_ms":int(time.time()*1000)+1,
                 "wait_reasons":["A new cycle is waiting for confirmation."]}
    later_ids=_persist_paper_wait_events(engine,"BTC/USDT",later_cycle,
        settings.strategy_entry_timeframe,1_800_000)
    after_empty=route.endpoint(after_id=wait_ids[-1],limit=1)
    assert [event["id"] for event in after_empty["events"]]==[later_ids[0]]
    assert after_empty["events"][0]["payload"]["reason"]==later_cycle["wait_reasons"][0]


def test_diagnostic_events_endpoint_cursor_empty_pages_and_new_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    now_ms=1_800_000_000_000
    monkeypatch.setattr("tradingagent.api.app._utc_ms",lambda:now_ms)
    path=tmp_path/"diagnostic-events.db"
    settings=Settings(db_path=str(path))
    app=create_app(settings)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE web_events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL, level TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL)")
        db.executemany("INSERT INTO web_events(timestamp_ms,level,category,message) VALUES (?,?,?,?)",
            [(now_ms-1000,"info","test","same-a"),
             (now_ms-3000,"info","test","late-old"),
             (now_ms-1000,"info","test","same-b"),
             (now_ms-2000,"info","test","middle"),
             (now_ms-500,"info","test","newest")])
    route=next(route for route in app.routes if route.path=="/api/diagnostics/events")

    first=route.endpoint(after_id=None,limit=2)
    first_ids=[event["id"] for event in first["events"]]
    assert [event["message"] for event in first["events"]]==["same-b","newest"]
    assert first_ids==[3,5] and first["latest_event_id"]==5
    assert first["next_after_id"]==5
    page=route.endpoint(after_id=first_ids[-1],limit=2)
    assert [event["message"] for event in page["events"]]==[]
    assert page["latest_event_id"]==5

    # New records after an empty response are visible; bounded pages neither skip nor repeat.
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO web_events(timestamp_ms,level,category,message) VALUES (?,?,?,?)",
            [(now_ms-2500,"warning","test","later-old"),
             (now_ms-500,"warning","test","later-tied"),
             (now_ms-1500,"warning","test","later-middle"),
             (now_ms-400,"warning","test","later-newer"),
             (now_ms-500,"warning","test","later-tied-2")])
    cursor=first_ids[-1]
    received=[]
    while True:
        result=route.endpoint(after_id=cursor,limit=2)
        batch=result["events"]
        if not batch:
            cursor=result["next_after_id"]
            break
        ids=[event["id"] for event in batch]
        assert all(event_id>cursor for event_id in ids)
        received.extend(ids)
        cursor=result["next_after_id"]
    assert sorted(received)==[6,7,8,9,10]
    assert route.endpoint(after_id=cursor,limit=2)["events"]==[]
    page_events=route.endpoint(after_id=5,limit=5)["events"]
    assert [(event["timestamp_ms"],event["id"]) for event in page_events]==sorted(
        (event["timestamp_ms"],event["id"]) for event in page_events)


def test_paper_event_page_filters_30_minutes_and_advances_across_expired_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    now_ms=1_800_000_000_000; cutoff=now_ms-30*60*1000
    monkeypatch.setattr("tradingagent.api.app._utc_ms",lambda:now_ms)
    settings=Settings(db_path=str(tmp_path/"paper-window.db"))
    app=create_app(settings)
    engine=PaperTradingEngine(settings.db_path,settings.paper_account_id,
        settings.paper_initial_capital,settings.paper_quote_currency)
    engine.record_wait_reason("BTC/USDT",cutoff-1,"expired","15m",cutoff-1000)
    engine.record_wait_reason("BTC/USDT",now_ms,"fresh","15m",now_ms)
    boundary_id=engine.record_wait_reason("BTC/USDT",cutoff,"boundary","15m",cutoff)
    boundary_tie_id=engine.record_wait_reason("BTC/USDT",cutoff,"boundary-tie","15m",cutoff)
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE paper_events SET timestamp_ms=? WHERE id=?",(cutoff//1000,boundary_id))
        db.execute("UPDATE paper_events SET timestamp_ms=? WHERE id=?",
            (datetime.fromtimestamp(cutoff/1000,timezone.utc).isoformat(),boundary_tie_id))
    app.state.paper_engine=engine
    snapshot_route=next(route for route in app.routes if route.path=="/api/paper")
    snapshot=snapshot_route.endpoint(limit=10,include_events=True)
    assert [event["payload"]["reason"] for event in snapshot["events"]]==["boundary","boundary-tie","fresh"]
    assert [event["id"] for event in snapshot["events"]]==[boundary_id,boundary_tie_id,2]
    assert snapshot["event_cursor"]==4
    assert len(engine.events())==4  # API filtering leaves historical rows untouched.
    route=next(route for route in app.routes if route.path=="/api/paper/events")
    first=route.endpoint(after_id=0,limit=2)
    assert [event["payload"]["reason"] for event in first["events"]]==["fresh"]
    assert first["next_after_id"]==2
    second=route.endpoint(after_id=first["next_after_id"],limit=2)
    assert [event["payload"]["reason"] for event in second["events"]]==["boundary","boundary-tie"]
    assert [event["id"] for event in second["events"]]==[boundary_id,boundary_tie_id]
    cursor=second["next_after_id"]
    engine.record_wait_reason("BTC/USDT",cutoff-2,"late-but-expired","15m",cutoff-2)
    engine.record_wait_reason("BTC/USDT",now_ms+1,"late-fresh","15m",now_ms+1)
    third=route.endpoint(after_id=cursor,limit=2)
    assert [event["payload"]["reason"] for event in third["events"]]==["late-fresh"]
    assert third["next_after_id"]>cursor


def test_diagnostic_log_endpoints_filter_boundary_and_late_expired_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    now_ms=1_800_000_000_000; cutoff=now_ms-30*60*1000
    monkeypatch.setattr("tradingagent.api.app._utc_ms",lambda:now_ms)
    path=tmp_path/"diagnostic-window.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE web_events (id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL, level TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL)")
        db.executemany("INSERT INTO web_events(timestamp_ms,level,category,message) VALUES (?,?,?,?)",
            [(cutoff-1,"info","test","expired"),(cutoff,"info","test","boundary"),
             (now_ms,"info","test","fresh"),(cutoff//1000,"info","test","boundary-seconds"),
             (datetime.fromtimestamp(cutoff/1000,timezone.utc).isoformat(),"info","test","boundary-iso")])
    app=create_app(Settings(db_path=str(path)))
    incremental=next(route for route in app.routes if route.path=="/api/diagnostics/events")
    snapshot=next(route for route in app.routes if route.path=="/api/diagnostics")
    initial=incremental.endpoint(after_id=None,limit=10)
    assert [row["message"] for row in initial["events"]]==["boundary","boundary-seconds","boundary-iso","fresh"]
    assert [row["timestamp_ms"] for row in initial["events"][:3]]==[cutoff,cutoff,cutoff]
    assert initial["next_after_id"]==5
    assert [row["message"] for row in snapshot.endpoint(limit=10)["events"]]==["boundary","boundary-seconds","boundary-iso","fresh"]
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO web_events(timestamp_ms,level,category,message) VALUES (?,?,?,?)",
            [(cutoff-2,"warning","test","late-but-expired"),
             (now_ms+1,"warning","test","late-fresh")])
    page=incremental.endpoint(after_id=initial["next_after_id"],limit=10)
    assert [row["message"] for row in page["events"]]==["late-fresh"]
    assert page["next_after_id"]==7
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM web_events").fetchone()[0]==7


def test_event_timestamp_normalization_handles_precision_timezone_and_boundary():
    from tradingagent.api.app import _event_timestamp_ms
    expected=1_800_000_000_000
    assert _event_timestamp_ms(expected)==expected
    assert _event_timestamp_ms(expected/1000)==expected
    assert _event_timestamp_ms("2027-01-15T08:00:00.000+02:00")==_event_timestamp_ms("2027-01-15T06:00:00Z")
    assert _event_timestamp_ms("not-a-timestamp") is None


def test_dashboard_chronology_and_30_minute_window_in_javascript():
    node=shutil.which("node")
    if not node:
        pytest.skip("Node.js niedostępny; składnia JS jest sprawdzana osobno")
    dashboard=(Path(__file__).parents[1]/"src/tradingagent/dashboard/index.html").read_text()
    lines=dashboard.splitlines()
    declarations=[line for line in lines if line.startswith("const LOG_WINDOW_MS=") or
        line.startswith("function eventTimestampMs(") or line.startswith("function compareLogEvents(") or
        line.startswith("function isRecentLogEvent(") or line.startswith("function mergeRecentLogEvents(")]
    assert len(declarations)==5
    source="\n".join(declarations)+"\n"+'''\
const assert=require('node:assert/strict');
const now=1800000000000;
const items=[
 {id:10,timestamp_ms:now-500},
 {id:8,timestamp_ms:now-1000},
 {id:9,timestamp_ms:(now-1000)/1000},
 {id:20,timestamp_ms:now-2500},
 {id:4,timestamp_ms:now-2000},
 {id:30,timestamp_ms:new Date(now-1500).toISOString()},
 {id:31,timestamp_ms:new Date(now-1500).toISOString().replace('Z','+00:00')}
];
assert.deepEqual(items.sort(compareLogEvents).map(x=>x.id),[20,4,30,31,8,9,10]);
assert.equal(isRecentLogEvent({timestamp_ms:now-LOG_WINDOW_MS},now),true);
assert.equal(isRecentLogEvent({timestamp_ms:now-LOG_WINDOW_MS-1},now),false);
assert.equal(eventTimestampMs('2026-10-18T14:00:00+02:00'),eventTimestampMs('2026-10-18T12:00:00Z'));
const unique=new Map();
assert.equal(mergeRecentLogEvents(unique,[items[0]],0,now).length,1);
assert.equal(mergeRecentLogEvents(unique,[items[0]],0,now).length,0);
assert.equal(unique.size,1);
const windowNow=now+1000;
assert.equal(isRecentLogEvent({timestamp_ms:now-LOG_WINDOW_MS},windowNow),false);
assert.equal(isRecentLogEvent({timestamp_ms:windowNow-LOG_WINDOW_MS},windowNow),true);
'''
    completed=subprocess.run([node,"-e",source],capture_output=True,text=True,timeout=10)
    assert completed.returncode==0,completed.stderr


def test_diagnostic_events_endpoint_reports_storage_failure(tmp_path, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    app=create_app(Settings(db_path=str(tmp_path/"missing-events-table.db")))
    route=next(route for route in app.routes if route.path=="/api/diagnostics/events")
    with pytest.raises(HTTPException) as error:
        route.endpoint(after_id=0,limit=10)
    assert error.value.status_code==503


def test_dashboard_log_polling_is_incremental_and_recovers_after_errors():
    dashboard=(Path(__file__).parents[1]/"src/tradingagent/dashboard/index.html").read_text()
    poll=dashboard.split("async function refreshDiagnosticLogs()",1)[1].split("function syncDiagnosticLogPolling()",1)[0]
    assert "/api/diagnostics/events?limit=100" in poll
    assert "after_id=${diagnosticLogCursor}" in poll
    assert "finally{clearTimeout(deadline);diagnosticLogInFlight=false}" in poll
    assert "setInterval(refreshDiagnosticLogs,3000)" in dashboard
    assert "mergeRecentLogEvents(diagnosticLogEntries,data.events,diagnosticLogCursor,now)" in poll
    assert "[...diagnosticLogEntries.values()].sort(compareLogEvents)" in dashboard
    assert "paperLogEntries.values()].sort(compareLogEvents)" in dashboard
    assert "document.addEventListener('visibilitychange',()=>{syncPaperLogPolling();syncDiagnosticLogPolling()})" in dashboard
    view=next(line for line in dashboard.splitlines() if line.startswith("function view(name)"))
    assert "syncDiagnosticLogPolling()" in view and "syncPaperLogPolling()" in view
    assert "diagnosticLogCursor=0" not in view and "paperLogCursor=0" not in view
    assert "async function loadHomeHealth(){try{const d=await api('/api/diagnostics?limit=1');$('homeHealth').innerHTML=" in dashboard


def test_asset_analysis_is_scoped_to_each_symbol_and_timeframe(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    path=str(tmp_path/"asset-analysis.db")
    timeframes=("15m","4h")
    symbols=("BTC/USDT","ETH/USDT","XRP/USDT")
    settings=Settings(db_path=path,symbols=symbols,timeframes=timeframes,
        strategy_entry_timeframe="15m",strategy_higher_timeframe="4h")
    store=CandleStore(path)
    now_ms=int(time.time()*1000)
    latest_prices={}
    for symbol_index,symbol in enumerate(symbols[:2]):
        for timeframe_index,timeframe in enumerate(timeframes):
            period=timeframe_to_ms(timeframe)
            latest_open=(now_ms//period)*period-period
            base=100+symbol_index*1000+timeframe_index*100
            candles=[]
            for i in range(60):
                price=base+i*.25
                candles.append(Candle(latest_open-(59-i)*period,price,price+1,
                    price-1,price+.1,100,"binance",True))
            latest_prices[(symbol,timeframe)]=candles[-1].close
            store.upsert(settings.exchange,symbol,timeframe,candles,source="ccxt")

    app=create_app(settings)
    route=next(route for route in app.routes if route.path=="/api/assets/{symbol:path}")
    results={symbol:route.endpoint(symbol=symbol) for symbol in symbols}
    seen_prices={}
    for symbol in symbols[:2]:
        result=results[symbol]
        assert result["symbol"]==symbol
        assert {item["timeframe"] for item in result["timeframes"]}==set(timeframes)
        for item in result["timeframes"]:
            assert item["analysis"]["symbol"]==symbol
            assert item["analysis"]["timeframe"]==item["timeframe"]
            assert item["analysis"]["candle_time"] is not None
            assert item["analysis_state"] in {"scenario","no_signal","insufficient_data","error"}
            price=item["indicators"]["latest_close"]
            assert price==latest_prices[(symbol,item["timeframe"])]
            seen_prices[(symbol,item["timeframe"])]=price
    assert len(set(seen_prices.values()))==len(seen_prices)
    assert results["XRP/USDT"]["timeframes"]==[]


def test_asset_analysis_distinguishes_no_signal_insufficient_data_and_error(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    path=str(tmp_path/"asset-analysis-state.db")
    settings=Settings(db_path=path,symbols=("BTC/USDT",),timeframes=("15m",),
        strategy_entry_timeframe="15m",strategy_higher_timeframe="15m")
    now_ms=int(time.time()*1000); period=timeframe_to_ms("15m")
    latest_open=(now_ms//period)*period-period
    candles=[]
    for i in range(60):
        price=100+i*.25
        candles.append(Candle(latest_open-(59-i)*period,price,price+1,price-1,
            price+.1,100,"binance",True))
    CandleStore(path).upsert(settings.exchange,"BTC/USDT","15m",candles,source="ccxt")
    app=create_app(settings)
    route=next(route for route in app.routes if route.path=="/api/assets/{symbol:path}")

    def analysis(regime):
        return Analysis("BTC/USDT","15m",regime,0,("Test analysis explanation.",),
                        None,None,candles[-1].timestamp)

    monkeypatch.setattr("tradingagent.api.app.analyze",lambda *args,**kwargs:analysis("sideways"))
    no_signal=route.endpoint(symbol="BTC/USDT")["timeframes"][0]
    assert no_signal["analysis_state"]=="no_signal"
    assert no_signal["analysis"]["scenario"] is None

    monkeypatch.setattr("tradingagent.api.app.analyze",
                        lambda *args,**kwargs:analysis("insufficient_data"))
    insufficient=route.endpoint(symbol="BTC/USDT")["timeframes"][0]
    assert insufficient["analysis_state"]=="insufficient_data"

    def fail_indicators(_candles):
        raise ValueError("deterministic indicator failure")
    monkeypatch.setattr("tradingagent.api.app.calculate_indicators",fail_indicators)
    failed=route.endpoint(symbol="BTC/USDT")["timeframes"][0]
    assert failed["analysis_state"]=="error"
    assert "ValueError" in failed["analysis_error"]


def test_dashboard_has_persistent_responsive_paper_log_panel():
    dashboard=(Path(__file__).parents[1]/"src/tradingagent/dashboard/index.html").read_text()
    assert ".shell{grid-template-columns:220px minmax(0,1fr) 310px" in dashboard
    assert 'id="paperLogPanel"' in dashboard
    assert 'id="logPanelToggle"' in dashboard and 'id="paperLogClose"' in dashboard
    assert "@media(max-width:1199px)" in dashboard
    assert "window.matchMedia('(min-width: 1200px)').matches" in dashboard
    assert "setInterval(refreshPaperLogs,3000)" in dashboard
    assert "const item=asset.timeframes.find(x=>x.timeframe===timeframe)" in dashboard
    assert "const requestId=++detailRequestId" in dashboard
    assert "if(requestId!==detailRequestId)return" in dashboard
    assert "Brak danych dla ${esc(symbol)} · ${esc(timeframe)}" in dashboard
    assert "Analiza w toku · ${esc(symbol)} · ${esc(timeframe)}" in dashboard
    assert "item.analysis_state==='error'" in dashboard
    assert "item.analysis_state==='insufficient_data'" in dashboard
    assert "Brak scenariusza wejścia dla ${esc(symbol)} · ${esc(timeframe)}" in dashboard
    assert "Brak wystarczających danych do analizy ${esc(symbol)} · ${esc(timeframe)}" in dashboard
    assert "Błąd analizy ${esc(symbol)} · ${esc(timeframe)}" in dashboard
    assert "s.symbol===x.symbol&&s.direction!=='NO TRADE'&&(s.timeframes||[]).includes(timeframe)" in dashboard
    assert "if(paperLogInitialized&&cursor!=null)return" in dashboard
    refresh=dashboard.split("async function refreshPaperLogs()",1)[1].split("function syncPaperLogPolling()",1)[0]
    assert "!$('view-paper').classList.contains('active')" not in refresh
    assert "finally{clearTimeout(deadline);paperLogInFlight=false}" in refresh
    assert "function compareLogEvents(a,b){return eventTimestampMs(a)-eventTimestampMs(b)||Number(a.id)-Number(b.id)}" in dashboard
    assert "function isRecentLogEvent(event,now=Date.now()){const stamp=eventTimestampMs(event);return Number.isFinite(stamp)&&stamp>=now-LOG_WINDOW_MS}" in dashboard
    assert "function mergeRecentLogEvents(entries,events,cursor,now=Date.now())" in dashboard
    assert "data.next_after_id" in dashboard
    paper_view=dashboard.split('id="view-paper"',1)[1].split('</section>',1)[0]
    assert 'id="paperEvents"' not in paper_view


def test_api_bearer_token_protects_portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED","false")
    monkeypatch.setenv("TRADINGAGENT_API_TOKEN","test-secret")
    settings=Settings(db_path=str(tmp_path/"market.db"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/paper").status_code==401
        assert client.get("/api/paper",headers={"Authorization":"Bearer test-secret"}).status_code==200


def test_market_endpoint_uses_fresh_monitor_price_over_cached_candle(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED","false")
    settings=Settings(db_path=str(tmp_path/"market-live.db"))
    app=create_app(settings)
    now=int(time.time()*1000)
    ticker=MarketTicker("binance","BTC/USDT",101.5,101.4,101.6,20.0,
        1.0,101.5,now,now,False)
    assert app.state.market_monitor.apply_rest_ticker(ticker)
    engine=PaperTradingEngine(settings.db_path,settings.paper_account_id,
        settings.paper_initial_capital,"USDT",fee_bps=0,slippage_bps=0,spread_bps=0)
    score=ScoreBreakdown(1,1,1,1,1,1,1,1,100)
    signal=StrategySignal("BTC/USDT","binance",datetime.fromtimestamp(0,timezone.utc),
        "fixture","LONG","conditions_met",(999,1001),"calculated",990,
        "calculated",990,(1030,1040),2,0,0,("4h","15m"),(),("fixture",),(),
        100,score)
    request=RiskRequest(signal,PortfolioSnapshot(10_000),
        InstrumentConstraints("USDT",amount_step=.001),0,True,
        fee_bps_round_trip=0,slippage_bps_entry=0,slippage_bps_stop=0)
    engine.execute_entry(signal,RiskManager.from_settings(settings),request,
        Candle(900_000,1000,1001,999,1000,100_000,"binance",True),.001)
    app.state.paper_engine=engine
    with TestClient(app) as client:
        items=client.get("/api/market").json()["items"]
        btc=next(row for row in items if row["symbol"]=="BTC/USDT")
        assert btc["price"]==101.5
        assert btc["source"]=="REST ticker snapshots"
        paper=client.get("/api/paper").json()
        assert paper["simulated"] is True
        assert paper["positions"][0]["mark_price"] == 101.5

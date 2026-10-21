import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tradingagent.api.app import create_app
from tradingagent.config import Settings
from tradingagent.market_data import MarketDataError


class OfflineProvider:
    def __init__(self, *args, **kwargs): pass
    def fetch_ticker(self, symbol): raise MarketDataError("offline")
    def fetch_candles(self, symbol, timeframe, limit): raise MarketDataError("offline")
    def fetch_history(self, symbol, timeframe, since, until, **kwargs): raise MarketDataError("offline")
    def close(self): pass


class OfflineWebsocket:
    def __init__(self, *args, **kwargs): self.supports_trade_stream=False
    async def close(self): pass


def test_analysis_controls_and_settings_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    monkeypatch.setattr("tradingagent.api.app.CcxtMarketData", OfflineProvider)
    monkeypatch.setattr("tradingagent.api.app.CcxtProMarketData", OfflineWebsocket)
    path = str(tmp_path / "web.sqlite")
    settings = Settings(db_path=path)
    with TestClient(create_app(settings)) as client:
        started = client.post("/api/control/analysis", json={"action": "start"})
        assert started.status_code == 200
        assert started.json()["analysis_running"] is True
        assert client.post("/api/control/analysis", json={"action": "pause"}).json()["paused"] is True
        assert client.post("/api/control/analysis", json={"action": "resume"}).json()["paused"] is False
        assert client.post("/api/control/analysis", json={"action": "stop"}).json()["analysis_running"] is False
        saved = client.put("/api/settings", json={"strategy_min_score": 72})
        assert saved.status_code == 200
        assert saved.json()["strategy_min_score"] == 72
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/settings").json()["strategy_min_score"] == 72
        assert client.get("/health").json()["live_trading"] is False


def test_paper_reset_requires_confirmation_and_preserves_market_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    monkeypatch.setattr("tradingagent.api.app.CcxtMarketData", OfflineProvider)
    monkeypatch.setattr("tradingagent.api.app.CcxtProMarketData", OfflineWebsocket)
    monkeypatch.setenv("TRADINGAGENT_PAPER_AMOUNT_STEPS",
                       "BTC/USDT=0.00001,ETH/USDT=0.0001,XRP/USDT=0.1,SOL/USDT=0.001")
    settings = Settings(db_path=str(tmp_path / "paper.sqlite"))
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/control/paper", json={"action": "start"}).status_code == 200
        assert client.post("/api/control/paper", json={"action": "reset"}).status_code == 409
        assert client.post("/api/control/paper", json={"action": "stop"}).status_code == 200
        assert client.post("/api/control/paper", json={"action": "reset", "confirm": "wrong"}).status_code == 400
        assert client.post("/api/control/paper", json={
            "action": "reset", "confirm": "RESET PAPER ACCOUNT"}).status_code == 200
        assert client.get("/api/paper").json()["snapshot"]["cash"] == settings.paper_initial_capital
        assert client.put("/api/settings", json={"paper_initial_capital": 12000}).status_code == 422
        changed = client.put("/api/settings", json={"paper_initial_capital": 12000,
            "reset_paper_account": "RESET PAPER ACCOUNT"})
        assert changed.status_code == 200
        assert client.get("/api/paper").json()["snapshot"]["cash"] == 12000


def test_auto_paper_starts_workers_even_when_general_sync_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    monkeypatch.setenv("TRADINGAGENT_PAPER_AUTO_ENABLED", "true")
    monkeypatch.setenv("TRADINGAGENT_PAPER_AMOUNT_STEPS",
        "BTC/USDT=0.00001,ETH/USDT=0.0001,XRP/USDT=0.1,SOL/USDT=0.001")
    monkeypatch.setattr("tradingagent.api.app.CcxtMarketData", OfflineProvider)
    monkeypatch.setattr("tradingagent.api.app.CcxtProMarketData", OfflineWebsocket)
    settings=Settings(mode="paper",db_path=str(tmp_path/"auto-paper.sqlite"))
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["paper_running"] is True
        assert client.app.state.sync_task is not None
        assert client.app.state.market_poll_task is not None
        assert client.app.state.market_stream_task is not None


def test_backtest_rejects_incomplete_request_without_fabricating_results(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_API_SYNC_ENABLED", "false")
    settings = Settings(db_path=str(tmp_path / "backtest.sqlite"))
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/backtests/run", json={"symbols": ["BTC/USDT"]})
        assert response.status_code == 422
        assert client.get("/api/backtests").json()["items"] == []
        paths = {route.path for route in client.app.routes}
        assert not any("order" in path.lower() for path in paths)

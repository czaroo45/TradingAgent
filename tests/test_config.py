import pytest

from tradingagent.config import Settings


def test_dotenv_configuration_and_process_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TRADINGAGENT_EXCHANGE=kraken\nTRADINGAGENT_MODE=paper\n")
    monkeypatch.delenv("TRADINGAGENT_EXCHANGE", raising=False)
    monkeypatch.setenv("TRADINGAGENT_MODE", "analysis")
    settings = Settings.from_env(str(env_file))
    assert settings.exchange == "kraken"
    assert settings.mode == "analysis"


def test_live_mode_is_disabled(monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_MODE", "live")
    with pytest.raises(ValueError, match="live mode is disabled"):
        Settings.from_env("/nonexistent/.env")


def test_risk_limit_is_validated(monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_MAX_RISK_PER_TRADE", "0.05")
    with pytest.raises(ValueError, match="risk per trade"):
        Settings.from_env("/nonexistent/.env")


def test_aggressive_mode_and_short_term_config_are_loaded(monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_STRATEGY_AGGRESSIVE_MODE", "true")
    monkeypatch.setenv("TRADINGAGENT_PAPER_SHORT_TRADE_WINDOW_MINUTES", "8")
    settings = Settings.from_env("/nonexistent/.env")
    assert settings.strategy_aggressive_mode is True
    assert settings.paper_short_trade_window_minutes == 8


def test_market_rest_poll_interval_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("TRADINGAGENT_MARKET_REST_POLL_SECONDS", "3")
    assert Settings.from_env("/nonexistent/.env").market_rest_poll_seconds == 3
    monkeypatch.setenv("TRADINGAGENT_MARKET_REST_POLL_SECONDS", "0.5")
    with pytest.raises(ValueError, match="REST poll"):
        Settings.from_env("/nonexistent/.env")

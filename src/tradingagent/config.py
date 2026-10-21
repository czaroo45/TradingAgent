"""Validated environment-backed application settings."""
from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _load_dotenv(path: str = ".env") -> None:
    """Load simple KEY=VALUE dotenv entries without overriding the process environment."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True, slots=True)
class Settings:
    exchange: str = "binance"
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "XRP/USDT", "SOL/USDT")
    timeframes: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
    limit: int = 300
    db_path: str = "data/market.sqlite3"
    data_source: str = "ccxt"
    timeout_ms: int = 15000
    max_concurrency: int = 1
    market_cache_seconds: float = 5.0
    market_rest_poll_seconds: float = 5.0
    display_timezone: str = "UTC"
    mode: str = "analysis"
    paper_initial_capital: float = 10_000.0
    paper_quote_currency: str = "USDT"
    paper_account_id: str = "default"
    paper_spread_bps: float = 2.0
    paper_short_trade_window_minutes: int = 0
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    cost_estimate_notional: float = 1000.0
    max_spread_bps: float = 20.0
    max_risk_per_trade: float = 0.01
    max_total_exposure: float = 0.50
    max_asset_exposure: float = 0.20
    max_correlated_exposure: float = 0.30
    correlated_asset_groups: tuple[tuple[str, ...], ...] = (("BTC", "ETH", "XRP", "SOL"),)
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.15
    max_trades_per_day: int = 10
    min_risk_reward: float = 1.5
    max_transaction_cost_bps: float = 50.0
    max_market_data_age_ms: int = 120_000
    strategy: str = "baseline"
    enabled_strategies: tuple[str, ...] = ("trend_following", "pullback", "breakout_retest", "momentum")
    strategy_aggressive_mode: bool = False
    strategy_entry_timeframe: str = "15m"
    strategy_higher_timeframe: str = "4h"
    strategy_min_net_risk_reward: float = 1.5
    strategy_min_score: float = 60.0
    score_weights: tuple[float, ...] = (1.0, 1.0, 1.0, 0.75, 1.0, 1.0, 1.0, 1.0)
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls, dotenv_path: str = ".env") -> "Settings":
        _load_dotenv(dotenv_path)
        s = cls(
            exchange=os.getenv("TRADINGAGENT_EXCHANGE", "binance").strip().lower(),
            symbols=_csv("TRADINGAGENT_SYMBOLS", "BTC/USDT,ETH/USDT,XRP/USDT,SOL/USDT"),
            timeframes=_csv("TRADINGAGENT_TIMEFRAMES", "5m,15m,1h,4h,1d"),
            limit=int(os.getenv("TRADINGAGENT_LIMIT", "300")),
            db_path=os.getenv("TRADINGAGENT_DB_PATH", "data/market.sqlite3"),
            data_source=os.getenv("TRADINGAGENT_DATA_SOURCE", "ccxt").strip().lower(),
            timeout_ms=int(os.getenv("TRADINGAGENT_TIMEOUT_MS", "15000")),
            max_concurrency=int(os.getenv("TRADINGAGENT_MAX_CONCURRENCY", "1")),
            market_cache_seconds=float(os.getenv("TRADINGAGENT_MARKET_CACHE_SECONDS", "5")),
            market_rest_poll_seconds=float(os.getenv("TRADINGAGENT_MARKET_REST_POLL_SECONDS", "5")),
            display_timezone=os.getenv("TRADINGAGENT_DISPLAY_TIMEZONE", "UTC"),
            mode=os.getenv("TRADINGAGENT_MODE", "analysis").strip().lower(),
            paper_initial_capital=float(os.getenv("TRADINGAGENT_PAPER_INITIAL_CAPITAL", "10000")),
            paper_quote_currency=os.getenv("TRADINGAGENT_PAPER_QUOTE_CURRENCY", "USDT").strip().upper(),
            paper_account_id=os.getenv("TRADINGAGENT_PAPER_ACCOUNT_ID", "default").strip(),
            paper_spread_bps=float(os.getenv("TRADINGAGENT_PAPER_SPREAD_BPS", "2")),
            paper_short_trade_window_minutes=int(os.getenv("TRADINGAGENT_PAPER_SHORT_TRADE_WINDOW_MINUTES", "0")),
            fee_bps=float(os.getenv("TRADINGAGENT_FEE_BPS", "10")),
            slippage_bps=float(os.getenv("TRADINGAGENT_SLIPPAGE_BPS", "5")),
            cost_estimate_notional=float(os.getenv("TRADINGAGENT_COST_ESTIMATE_NOTIONAL", "1000")),
            max_spread_bps=float(os.getenv("TRADINGAGENT_MAX_SPREAD_BPS", "20")),
            max_risk_per_trade=float(os.getenv("TRADINGAGENT_MAX_RISK_PER_TRADE", "0.01")),
            max_total_exposure=float(os.getenv("TRADINGAGENT_MAX_TOTAL_EXPOSURE", "0.50")),
            max_asset_exposure=float(os.getenv("TRADINGAGENT_MAX_ASSET_EXPOSURE", "0.20")),
            max_correlated_exposure=float(os.getenv("TRADINGAGENT_MAX_CORRELATED_EXPOSURE", "0.30")),
            correlated_asset_groups=_groups("TRADINGAGENT_CORRELATED_ASSET_GROUPS", "BTC+ETH+XRP+SOL"),
            max_daily_loss=float(os.getenv("TRADINGAGENT_MAX_DAILY_LOSS", "0.03")),
            max_drawdown=float(os.getenv("TRADINGAGENT_MAX_DRAWDOWN", "0.15")),
            max_trades_per_day=int(os.getenv("TRADINGAGENT_MAX_TRADES_PER_DAY", "10")),
            min_risk_reward=float(os.getenv("TRADINGAGENT_MIN_RISK_REWARD", "1.5")),
            max_transaction_cost_bps=float(os.getenv("TRADINGAGENT_MAX_TRANSACTION_COST_BPS", "50")),
            max_market_data_age_ms=int(os.getenv("TRADINGAGENT_MAX_MARKET_DATA_AGE_MS", "120000")),
            strategy=os.getenv("TRADINGAGENT_STRATEGY", "baseline"),
            enabled_strategies=_csv("TRADINGAGENT_ENABLED_STRATEGIES", "trend_following,pullback,breakout_retest,momentum"),
            strategy_aggressive_mode=_bool(
                "TRADINGAGENT_STRATEGY_AGGRESSIVE_MODE",
                _bool("TRADINGAGENT_STRATEGY_AGGRESSIVE_ENTRY", False),
            ),
            strategy_entry_timeframe=os.getenv("TRADINGAGENT_STRATEGY_ENTRY_TIMEFRAME", "15m"),
            strategy_higher_timeframe=os.getenv("TRADINGAGENT_STRATEGY_HIGHER_TIMEFRAME", "4h"),
            strategy_min_net_risk_reward=float(os.getenv("TRADINGAGENT_STRATEGY_MIN_NET_RR", "1.5")),
            strategy_min_score=float(os.getenv("TRADINGAGENT_STRATEGY_MIN_SCORE", "60")),
            score_weights=tuple(float(x) for x in _csv("TRADINGAGENT_SCORE_WEIGHTS", "1,1,1,0.75,1,1,1,1")),
            telegram_enabled=_bool("TRADINGAGENT_TELEGRAM_ENABLED", False),
            telegram_bot_token=os.getenv("TRADINGAGENT_TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TRADINGAGENT_TELEGRAM_CHAT_ID", ""),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if not self.exchange or self.data_source != "ccxt":
            raise ValueError("data source must currently be 'ccxt' and exchange cannot be empty")
        if not 50 <= self.limit <= 1000:
            raise ValueError("limit must be 50..1000")
        if self.timeout_ms <= 0:
            raise ValueError("timeout must be positive")
        try:
            ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown display timezone: {self.display_timezone}") from exc
        if (self.max_concurrency <= 0 or self.market_cache_seconds < 0 or
                not math.isfinite(self.market_rest_poll_seconds) or
                not 1 <= self.market_rest_poll_seconds <= 300):
            raise ValueError("max concurrency must be positive, cache seconds nonnegative, and REST poll seconds 1..300")
        if self.mode not in {"analysis", "paper", "live"}:
            raise ValueError("mode must be analysis, paper, or live")
        if self.mode == "live":
            raise ValueError("live mode is disabled until a separate safe execution module is implemented")
        if (not math.isfinite(self.paper_initial_capital) or self.paper_initial_capital <= 0 or
                not self.paper_quote_currency or not self.paper_account_id or
                not math.isfinite(self.paper_spread_bps) or self.paper_spread_bps < 0 or
                self.paper_short_trade_window_minutes < 0):
            raise ValueError("paper capital, quote currency, and account id must be valid")
        if not self.symbols or not self.timeframes:
            raise ValueError("symbols and timeframes cannot be empty")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if not 0 < self.max_risk_per_trade <= 0.02:
            raise ValueError("max risk per trade must be > 0 and <= 2%")
        risk_values = (self.max_total_exposure, self.max_asset_exposure,
                       self.max_correlated_exposure, self.max_daily_loss, self.max_drawdown)
        if any(not math.isfinite(x) or not 0 < x <= 1 for x in risk_values):
            raise ValueError("exposure, daily loss, and drawdown limits must be finite and in (0, 1]")
        from tradingagent.risk import RiskLimits
        try:
            RiskLimits(max_position_risk_fraction=self.max_risk_per_trade,
                max_total_exposure_fraction=self.max_total_exposure,
                max_asset_exposure_fraction=self.max_asset_exposure,
                max_correlated_exposure_fraction=self.max_correlated_exposure,
                correlated_groups=self.correlated_asset_groups,
                max_daily_loss_fraction=self.max_daily_loss,
                max_drawdown_fraction=self.max_drawdown,
                max_trades_per_day=self.max_trades_per_day,
                min_risk_reward=self.min_risk_reward,
                max_transaction_cost_bps=self.max_transaction_cost_bps,
                max_data_age_ms=self.max_market_data_age_ms)
        except ValueError as exc:
            raise ValueError(f"invalid risk limit: {exc}") from exc
        if min(self.fee_bps, self.slippage_bps, self.max_spread_bps) < 0:
            raise ValueError("cost settings cannot be negative")
        if self.cost_estimate_notional <= 0:
            raise ValueError("cost estimate notional must be positive")
        supported_strategies = {"trend_following", "pullback", "breakout_retest", "momentum", "mean_reversion"}
        if set(self.enabled_strategies) - supported_strategies:
            raise ValueError("unknown enabled strategy")
        if not 0 < self.strategy_min_net_risk_reward or not 0 <= self.strategy_min_score <= 100:
            raise ValueError("strategy thresholds are invalid")
        if len(self.score_weights) != 8 or any(not math.isfinite(x) or x < 0 for x in self.score_weights) or sum(self.score_weights) <= 0:
            raise ValueError("score weights require eight nonnegative values with a positive sum")
        if self.telegram_enabled and not (self.telegram_bot_token and self.telegram_chat_id):
            raise ValueError("Telegram requires bot token and chat id when enabled")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


def _groups(name: str, default: str) -> tuple[tuple[str, ...], ...]:
    raw = os.getenv(name, default)
    return tuple(tuple(asset.strip().upper() for asset in group.split("+") if asset.strip())
                 for group in raw.split(",") if group.strip())

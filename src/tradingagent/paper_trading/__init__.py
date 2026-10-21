"""Persistent simulated trading; risk approval is mandatory, no exchange execution."""

from tradingagent.paper_trading.engine import (
    EquityPoint, PaperFill, PaperPosition, PaperSnapshot, PaperTradeRejected,
    PaperTradingEngine,
)
from tradingagent.risk import RiskDecision, RiskManager, RiskRequest
from tradingagent.paper_trading.runner import run_paper_cycle


def authorize_paper_entry(manager: RiskManager, request: RiskRequest) -> RiskDecision:
    """Mandatory preflight for future paper-entry consumers; never submits an order."""
    decision = manager.evaluate(request)
    if not decision.accepted:
        raise PaperTradeRejected("paper entry rejected by risk gate: " + "; ".join(decision.reasons))
    return decision


__all__ = ["EquityPoint", "PaperFill", "PaperPosition", "PaperSnapshot",
           "PaperTradeRejected", "PaperTradingEngine", "authorize_paper_entry", "run_paper_cycle"]

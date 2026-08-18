"""Deterministic risk engine (M4): coherence checks, ladder, sizing, rails.

Rule zero (specs/RISK_ENGINE.md): no LLM in this module. Decimal money math,
100% branch coverage, tests written first.
"""

from sentinel.risk.accounting import Exit, Fill, realized_r
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import (
    AccountState,
    EntryRung,
    GateDecision,
    GateStatus,
    MarketContext,
    PauseReason,
    PauseState,
    PortfolioState,
    RejectionReason,
    TradePlan,
)
from sentinel.risk.rails import check_portfolio_rails, evaluate_daily_loss

__all__ = [
    "AccountState",
    "EntryRung",
    "Exit",
    "Fill",
    "GateDecision",
    "GateStatus",
    "MarketContext",
    "PauseReason",
    "PauseState",
    "PortfolioState",
    "RejectionReason",
    "RiskEngine",
    "TradePlan",
    "check_portfolio_rails",
    "evaluate_daily_loss",
    "realized_r",
]

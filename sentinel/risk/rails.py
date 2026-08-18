"""Portfolio rails and pause state (§2 rule 7, §7).

Enforced in code, never in a prompt (PRD F11). The engine reads state it is
given; M7's tracker loads it from Postgres before every cycle and every tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.core.config import RiskConfig
from sentinel.risk.models import PauseReason, PauseState, PortfolioState, RejectionReason

#: §7 — a loss-limit pause lasts 24h; /resume can end it early, with confirmation.
LOSS_PAUSE_HOURS = 24


def check_portfolio_rails(
    *,
    portfolio: PortfolioState,
    symbol: str,
    risk_per_trade_pct: Decimal,
    config: RiskConfig,
    now: datetime,
) -> RejectionReason | None:
    """Rule 7, in the order that makes a rejection most explainable."""
    if portfolio.pause.is_active(now):
        return RejectionReason.PAUSED
    if portfolio.open_risk_pct + risk_per_trade_pct > config.max_open_risk_pct:
        return RejectionReason.MAX_OPEN_RISK
    if portfolio.open_positions >= config.max_positions:
        return RejectionReason.MAX_POSITIONS

    cooldown = portfolio.cooldown_until.get(symbol)
    if cooldown is not None and now < cooldown:
        return RejectionReason.SYMBOL_COOLDOWN
    return None


def evaluate_daily_loss(
    *,
    realized_loss_pct: Decimal,
    limit_pct: Decimal,
    now: datetime,
    pause_hours: int = LOSS_PAUSE_HOURS,
    current: PauseState | None = None,
) -> PauseState:
    """§7 — realized daily loss at or beyond the limit auto-pauses for 24h.

    ``realized_loss_pct`` is a positive magnitude (3.1 means "down 3.1%"). A quiet
    day never clears an existing pause: only ``/resume`` does that.
    """
    if realized_loss_pct >= limit_pct:
        return PauseState(
            paused=True,
            reason=PauseReason.DAILY_LOSS_LIMIT,
            until=now + timedelta(hours=pause_hours),
        )
    return current if current is not None else PauseState()

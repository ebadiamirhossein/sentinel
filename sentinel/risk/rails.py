"""Portfolio rails and pause state (§2 rule 7, §7).

Enforced in code, never in a prompt (PRD F11). The engine reads state it is
given; M7's tracker loads it from Postgres before every cycle and every tick.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.core.config import RiskConfig
from sentinel.risk.models import PauseReason, PauseState, PortfolioState, RejectionReason

HUNDRED = Decimal("100")

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
    if portfolio.signals_today >= config.max_signals_per_day:
        return RejectionReason.DAILY_SIGNAL_CAP

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


# --------------------------------------------------------------------------- #
# Building the state above from what the database holds (M7)
# --------------------------------------------------------------------------- #
#
# M4 left ``PortfolioState`` a plain input and noted that M7 would fill it. These
# three conversions are that filling. They live here rather than in the
# orchestrator because they decide whether a signal is allowed, which makes them
# risk math — pure, ``Decimal``, and covered to 100% branches like everything
# else in this module. The *selection* (which signals count as open, which
# resolutions arm a cooldown) stays outside: that is a query, not arithmetic.


def open_risk_pct(risk_pcts: Iterable[Decimal]) -> Decimal:
    """Total open risk, as a percentage of capital.

    Each value is the risk percentage the plan was **issued** with, not today's
    setting: §7 keeps open signals on their original sizing, so a signal issued at
    0.75% still occupies 0.75% of the budget after ``/risk 1.0``.
    """
    return sum(risk_pcts, Decimal(0))


def cooldown_until(
    resolutions: Iterable[tuple[str, datetime]], *, hours: int
) -> dict[str, datetime]:
    """Per-symbol cooldown expiry from ``(symbol, resolved_at)`` pairs.

    ARCHITECTURE §3 step 6 arms this after a rejected or expired signal; M7's
    owner ruling adds a stop-out, because re-entering the same failing idea on the
    next 15-minute cycle is exactly what the rail is for.

    The **latest** resolution per symbol wins, and the caller is not required to
    sort: two stop-outs in an afternoon must not let the older one shorten the
    cooldown the newer one deserves.

    ``hours <= 0`` disables the rail rather than pinning every symbol to "expires
    now" — an expiry equal to the current instant is a race whose outcome depends
    on which side of a microsecond the tick lands on.
    """
    if hours <= 0:
        return {}

    latest: dict[str, datetime] = {}
    for symbol, resolved_at in resolutions:
        known = latest.get(symbol)
        if known is None or resolved_at > known:
            latest[symbol] = resolved_at
    return {symbol: at + timedelta(hours=hours) for symbol, at in latest.items()}


def realized_loss_pct(realized_eur: Iterable[Decimal], *, capital_eur: Decimal | None) -> Decimal:
    """Today's realized loss as the **positive magnitude** §7's limit compares to.

    Inputs are signed realized P&L in EUR, so wins and losses net off within the
    day: §7 limits the realized daily loss, and an afternoon that gives most of a
    bad morning back is not a 3% day. A net profit is reported as ``0`` rather
    than as a negative loss — a negative would compare as "safely under the
    limit" while quietly meaning the rail had been handed a profit as a drawdown.

    Without capital there is no denominator, and inventing one is the fabrication
    CLAUDE.md forbids. The gate already rejects every signal with ``NO_CAPITAL`` in
    that state, so there is nothing a pause would additionally protect.
    """
    if capital_eur is None or capital_eur <= 0:
        return Decimal(0)

    net = sum(realized_eur, Decimal(0))
    if net >= 0:
        return Decimal(0)
    return -net / capital_eur * HUNDRED


__all__ = [
    "LOSS_PAUSE_HOURS",
    "check_portfolio_rails",
    "cooldown_until",
    "evaluate_daily_loss",
    "open_risk_pct",
    "realized_loss_pct",
]

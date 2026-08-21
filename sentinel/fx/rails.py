"""Forex portfolio rails (FOREX.md §9, §16.5 row 6).

Four checks, in the order a rejection is most useful to read.

**§9's cap of one open position is the one with no crypto analogue.** EURUSD, GBPUSD
and USDJPY all cross the dollar, so a long EURUSD and a short USDJPY is one large
short-dollar bet wearing two hats, and crypto's ``max_positions`` rail would allow both.
Crude, safe, and it makes the first measurement interpretable — relax it to a
correlation-adjusted rail once there is DRY_RUN data to calibrate against.

**The pause arrives as a composed verdict, not as four rails.** ``core/pauses.py``
already composes the global, per-market, per-user and per-(user, market) pauses into one
answer for the crypto engine. This module takes that answer, so there is exactly one
implementation of "is this paused" and forex cannot drift into a fifth reading of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sentinel.core.config import ForexConfig
from sentinel.fx.models import ForexRejection


@dataclass(frozen=True)
class ForexPortfolioState:
    """What the rest of this market's book looks like right now.

    Deliberately plain values rather than an import of
    :class:`sentinel.risk.models.PortfolioState`: this package depends on nothing in the
    frozen one, and a ``PauseState`` here would be that dependency for a boolean and a
    timestamp. The caller composes the pause and hands over the answer.
    """

    #: Open forex positions across every user this book belongs to.
    open_positions: int = 0
    #: Forex signals published today, against ``max_signals_per_day``.
    signals_today: int = 0
    #: Symbol -> the instant its cooldown lapses.
    cooldown_until: dict[str, datetime] = field(default_factory=dict)
    #: The composed pause verdict from ``core/pauses.py``.
    paused: bool = False
    #: Why, in words, so the rejection message can say which pause it was.
    pause_detail: str = ""


def check_portfolio_rails(
    *,
    portfolio: ForexPortfolioState,
    symbol: str,
    config: ForexConfig,
    now: datetime,
) -> ForexRejection | None:
    """The first rail that fails, or ``None``.

    Pause first, because a paused system should say so rather than reporting whichever
    other rail happens to also be true — that is the answer the person who typed
    ``/pause`` is looking for.
    """
    if portfolio.paused:
        return ForexRejection.PAUSED
    if portfolio.open_positions >= config.max_concurrent_positions:
        return ForexRejection.MAX_CONCURRENT_POSITIONS
    if portfolio.signals_today >= config.max_signals_per_day:
        return ForexRejection.DAILY_SIGNAL_CAP
    cooldown = portfolio.cooldown_until.get(symbol)
    if cooldown is not None and now < cooldown:
        return ForexRejection.SYMBOL_COOLDOWN
    return None


__all__ = ["ForexPortfolioState", "check_portfolio_rails"]

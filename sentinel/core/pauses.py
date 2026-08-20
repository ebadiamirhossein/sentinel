"""Which pause is holding somebody, out of the four that can (M10a Step 5).

Before this milestone there were two — the operator's system-wide ``/pause`` and a
user's own daily-loss pause — and ``bot/handlers/admin.py`` composed them inline in
four lines. A second market makes four:

======================  ==========================================================
``global``              the operator's ``/pause`` with no argument. Every market,
                        every user. The row is ``risk_state``, unchanged since M4.
``market``              the operator's ``/pause forex``. One market, every user.
``user``                one user's daily-loss pause across **all** markets — the
                        day was bad everywhere. The columns on ``users``, which
                        keep exactly the meaning M8.1 gave them.
``user_market``         one user's daily-loss pause in **one** market. A forex
                        loss stops forex and leaves a good crypto day alone.
======================  ==========================================================

**Widest wins**, in that order, and only an *active* pause counts. Reporting the
narrower of two active pauses would understate what is stopped, which on a status
card is the difference between "your forex is paused" and "nothing is running".

**Why this is not in** ``sentinel/risk/rails.py``, where it plainly belongs. M10a
freezes ``sentinel/risk/`` — not one line — because the package carries 100% branch
coverage and a live measurement window depends on its behaviour being provably
unchanged. So the composition lives here, pure and fully covered by its own truth
table, and journal/M10a_REPORT.md records the move for M10b to undo.

Pure: no clock, no database, no config. The caller supplies ``now``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sentinel.core.markets import Market
from sentinel.risk.models import PauseState


class PauseScope(StrEnum):
    """How wide the pause that is actually holding somebody is.

    The values of :attr:`GLOBAL` and :attr:`USER` are the exact strings ``/status``
    has printed since M8.1 ("system", "you"). That is not nostalgia: with one market
    enabled and no market-level pause ever set, the card has to render byte for byte
    what it renders today, and the scope label is the only part of it this milestone
    touches.
    """

    NONE = ""
    GLOBAL = "system"
    MARKET = "market"
    USER = "you"
    USER_MARKET = "user_market"


@dataclass(frozen=True)
class EffectivePause:
    """The pause in force, and which of the four it came from."""

    state: PauseState
    scope: PauseScope
    #: Set only when the scope is market-shaped, so a card can name it.
    market: Market | None = None

    @property
    def active(self) -> bool:
        return self.scope is not PauseScope.NONE

    def label(self, *, multi_market: bool) -> str:
        """What ``/status`` prints in brackets after "PAUSED".

        With one market enabled a market-shaped pause still names the market — the
        owner asked for that scope explicitly by typing it, so echoing it back is
        the honest answer. What ``multi_market`` decides is whether the *other* two
        scopes gain a market qualifier they never had.
        """
        if self.scope is PauseScope.NONE:
            return ""
        if self.market is None:
            return self.scope.value
        if self.scope is PauseScope.MARKET:
            return self.market.value
        return f"{PauseScope.USER.value} · {self.market.value}"


def effective_pause(
    *,
    global_pause: PauseState,
    market_pause: PauseState,
    user_pause: PauseState,
    user_market_pause: PauseState,
    market: Market,
    now: datetime,
) -> EffectivePause:
    """The widest active pause, or an inactive one when nothing is holding.

    Every argument is required and keyword-only. A default would let a caller omit
    a rail and get "not paused" back, which is the failure that delivers a signal to
    somebody the system had stopped — and it would be silent.
    """
    if global_pause.is_active(now):
        return EffectivePause(global_pause, PauseScope.GLOBAL)
    if market_pause.is_active(now):
        return EffectivePause(market_pause, PauseScope.MARKET, market)
    if user_pause.is_active(now):
        return EffectivePause(user_pause, PauseScope.USER)
    if user_market_pause.is_active(now):
        return EffectivePause(user_market_pause, PauseScope.USER_MARKET, market)
    return EffectivePause(PauseState(), PauseScope.NONE)


__all__ = ["EffectivePause", "PauseScope", "effective_pause"]

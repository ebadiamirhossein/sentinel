"""The four pause sources, enumerated (M10a Step 5).

Written as a **full truth table** rather than as scenarios, in the manner of
``tests/features/test_regime.py``'s classifier table. Four independent sources with
a widest-wins ordering is the most error-prone thing in this milestone: sixteen
combinations, and the ones that matter are precisely the ones nobody thinks to
write a scenario for — two rails active at once, where reporting the narrower would
tell a reader that less is stopped than really is.

Sixteen rows is small enough to read in one screen and large enough that an
ordering mistake cannot hide in it.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from sentinel.core.markets import Market
from sentinel.core.pauses import EffectivePause, PauseScope, effective_pause
from sentinel.risk.models import PauseReason, PauseState

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: An active pause of each kind, distinguishable by reason so a test can prove
#: *which* one survived rather than only that something did.
GLOBAL = PauseState(paused=True, reason=PauseReason.MANUAL, until=None)
MARKET = PauseState(paused=True, reason=PauseReason.MANUAL, until=NOW + timedelta(hours=1))
USER = PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW + timedelta(hours=6))
USER_MARKET = PauseState(
    paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW + timedelta(hours=12)
)
OFF = PauseState()


def _compose(g: bool, m: bool, u: bool, um: bool) -> EffectivePause:
    return effective_pause(
        global_pause=GLOBAL if g else OFF,
        market_pause=MARKET if m else OFF,
        user_pause=USER if u else OFF,
        user_market_pause=USER_MARKET if um else OFF,
        market=Market.CRYPTO,
        now=NOW,
    )


def _expected_scope(g: bool, m: bool, u: bool, um: bool) -> PauseScope:
    """Widest wins. Stated independently of the implementation, on purpose."""
    if g:
        return PauseScope.GLOBAL
    if m:
        return PauseScope.MARKET
    if u:
        return PauseScope.USER
    if um:
        return PauseScope.USER_MARKET
    return PauseScope.NONE


#: All 2^4 combinations of (global, market, user, user_market).
TABLE = list(itertools.product([False, True], repeat=4))

EXPECTED_STATE = {
    PauseScope.GLOBAL: GLOBAL,
    PauseScope.MARKET: MARKET,
    PauseScope.USER: USER,
    PauseScope.USER_MARKET: USER_MARKET,
    PauseScope.NONE: OFF,
}


@pytest.mark.parametrize(
    "g,m,u,um",
    TABLE,
    ids=[
        "".join(letter for letter, on in zip("GMUX", row, strict=True) if on) or "none"
        for row in TABLE
    ],
)
def test_the_widest_active_pause_wins(g: bool, m: bool, u: bool, um: bool) -> None:
    """Every row: the scope, the surviving reason, and the surviving expiry."""
    result = _compose(g, m, u, um)
    expected = _expected_scope(g, m, u, um)

    assert result.scope is expected
    assert result.active is (expected is not PauseScope.NONE)
    assert result.state == EXPECTED_STATE[expected]
    # The expiry travels with the state, and it is the figure /status prints. A
    # composition that returned the right *reason* from the wrong source would
    # otherwise pass — and would tell somebody their pause lifts at the wrong hour.
    assert result.state.until == EXPECTED_STATE[expected].until


def test_the_table_covers_every_combination() -> None:
    """A meta-test: sixteen rows, no duplicates, and every scope reached.

    Without it, a future edit could drop a row from ``TABLE`` and the suite would go
    quietly greener — which is the shape of failure this project keeps meeting.
    """
    assert len(TABLE) == 16
    assert len(set(TABLE)) == 16
    reached = {_expected_scope(*row) for row in TABLE}
    assert reached == set(PauseScope)


# --------------------------------------------------------------------------- #
# Beyond the table: expiry, and what the label says
# --------------------------------------------------------------------------- #


def test_an_expired_pause_does_not_hold() -> None:
    """``until`` in the past means not paused — the rail lapses on its own.

    §7's loss pause lasts 24h and is *not* cleared by a write; it simply stops being
    active. A composition that read ``paused`` alone would hold somebody for ever.
    """
    lapsed = PauseState(
        paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=NOW - timedelta(seconds=1)
    )
    result = effective_pause(
        global_pause=OFF,
        market_pause=OFF,
        user_pause=lapsed,
        user_market_pause=OFF,
        market=Market.CRYPTO,
        now=NOW,
    )
    assert result.scope is PauseScope.NONE
    assert not result.active


def test_a_lapsed_wider_pause_yields_to_a_live_narrower_one() -> None:
    """Widest wins among the *active* ones — not among the configured ones.

    The case a naive ordering gets wrong: yesterday's expired global pause must not
    mask today's live per-market one, or ``/status`` would report "not paused" while
    the gate rejects everything with PAUSED.
    """
    result = effective_pause(
        global_pause=PauseState(
            paused=True, reason=PauseReason.MANUAL, until=NOW - timedelta(days=1)
        ),
        market_pause=MARKET,
        user_pause=OFF,
        user_market_pause=OFF,
        market=Market.FOREX,
        now=NOW,
    )
    assert result.scope is PauseScope.MARKET
    assert result.market is Market.FOREX


@pytest.mark.parametrize(
    "scope,multi,expected",
    [
        (PauseScope.NONE, False, ""),
        (PauseScope.NONE, True, ""),
        # The two labels /status has printed since M8.1, unchanged — with one market
        # and no market-scoped pause, the card renders exactly what it does today.
        (PauseScope.GLOBAL, False, "system"),
        (PauseScope.GLOBAL, True, "system"),
        (PauseScope.USER, False, "you"),
        (PauseScope.USER, True, "you"),
        (PauseScope.MARKET, False, "crypto"),
        (PauseScope.MARKET, True, "crypto"),
        (PauseScope.USER_MARKET, False, "you · crypto"),
        (PauseScope.USER_MARKET, True, "you · crypto"),
    ],
)
def test_the_status_label(scope: PauseScope, multi: bool, expected: str) -> None:
    """What ``/status`` prints in brackets, for every scope.

    A market-shaped pause names its market whether or not a second market is
    enabled: the owner reached that scope by typing it, so echoing it back is the
    honest answer rather than a leak of a feature that is switched off.
    """
    market = (
        None if scope in {PauseScope.NONE, PauseScope.GLOBAL, PauseScope.USER} else Market.CRYPTO
    )
    pause = EffectivePause(EXPECTED_STATE[scope], scope, market)

    assert pause.label(multi_market=multi) == expected

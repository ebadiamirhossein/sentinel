"""The transition truth table (docs/MILESTONES.md M7: "state-machine transition
table tests").

Every cell of ``SignalStatus x EventKind`` is stated below as an explicit
expectation, and two meta-tests keep the table honest:

* ``test_the_table_covers_every_state_and_every_event`` fails if a status or an
  event kind is ever added without a row here — the failure mode a hand-written
  list of "interesting cases" cannot catch, because nothing tells you what you
  forgot.
* ``test_the_table_would_catch_a_wrong_transition`` proves the checker can fail.
  M6 §3's rule: a guard that cannot fail is not a guard.

The cells that *refuse* matter as much as the ones that act. A fill arriving for
a stopped signal, a target taken before any entry, an invalidation after the
position is real — each is a plausible reading of the spec, and each would
silently corrupt the R accounting if it were allowed.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import product

import pytest

from sentinel.bot.models import SignalStatus
from sentinel.core.config import AppConfig
from sentinel.risk.models import TradePlan
from sentinel.tracker.machine import advance
from sentinel.tracker.models import (
    EXIT_MANUAL,
    EXIT_STOP,
    EventKind,
    LegExit,
    MarketEvent,
    SignalTracking,
    Transition,
)

from .conftest import LATER, fill, tracking

# --------------------------------------------------------------------------- #
# The states, each built the shortest way that makes it real
# --------------------------------------------------------------------------- #


def state(plan: TradePlan, status: SignalStatus) -> SignalTracking:
    """A representative signal in ``status``, with a history that fits it."""
    if status is SignalStatus.PENDING_ENTRY:
        return tracking(plan)
    if status is SignalStatus.PARTIALLY_FILLED:
        return tracking(plan, status=status, fills=(fill(plan, 0),))
    if status is SignalStatus.FILLED:
        return tracking(plan, status=status, fills=tuple(fill(plan, i) for i in range(3)))
    if status is SignalStatus.STOPPED:
        return tracking(
            plan,
            status=status,
            fills=(fill(plan, 0),),
            exits=(
                LegExit(kind=EXIT_STOP, price=Decimal("81.20"), qty=plan.entries[0].qty, at=LATER),
            ),
        )
    if status is SignalStatus.CLOSED:
        return tracking(
            plan,
            status=status,
            fills=(fill(plan, 0),),
            exits=(
                LegExit(
                    kind=EXIT_MANUAL, price=Decimal("84.00"), qty=plan.entries[0].qty, at=LATER
                ),
            ),
        )
    # INVALIDATED and EXPIRED both resolve before any fill.
    return tracking(plan, status=status)


def event_for(kind: EventKind, plan: TradePlan) -> MarketEvent:
    """An event of ``kind`` that would be *valid* if the status allowed it.

    Chosen so a refusal is always about the state machine's rules and never about
    a malformed event — otherwise a cell could pass for the wrong reason.
    """
    if kind is EventKind.ENTRY_FILLED:
        # Rung 3 is unfilled in every state above that has any fills at all,
        # except FILLED — where refusing it is exactly the expectation.
        index = 2
        return MarketEvent(kind=kind, at=LATER, index=index, price=plan.entries[index].price)
    if kind is EventKind.TP_HIT:
        return MarketEvent(kind=kind, at=LATER, index=0, price=plan.targets[0])
    if kind is EventKind.STOPPED:
        return MarketEvent(kind=kind, at=LATER, price=plan.stop)
    if kind is EventKind.CLOSED_MANUALLY:
        return MarketEvent(kind=kind, at=LATER, price=Decimal("84.00"))
    if kind is EventKind.INVALIDATED:
        return MarketEvent(
            kind=kind, at=LATER, price=Decimal("81.05"), reference_price=Decimal("81.05")
        )
    return MarketEvent(kind=kind, at=LATER, price=Decimal("83.00"))


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

APPLIED = "applied"

#: (status, event kind) -> the status afterwards, or None meaning "refused".
#: Read a row as: "when a signal is X and the market does Y, it becomes Z".
TABLE: dict[tuple[SignalStatus, EventKind], SignalStatus | None] = {
    # Nothing has filled. Only a fill, an invalidation or the clock can act.
    (SignalStatus.PENDING_ENTRY, EventKind.ENTRY_FILLED): SignalStatus.PARTIALLY_FILLED,
    (SignalStatus.PENDING_ENTRY, EventKind.TP_HIT): None,
    (SignalStatus.PENDING_ENTRY, EventKind.STOPPED): None,
    (SignalStatus.PENDING_ENTRY, EventKind.INVALIDATED): SignalStatus.INVALIDATED,
    (SignalStatus.PENDING_ENTRY, EventKind.EXPIRED): SignalStatus.EXPIRED,
    (SignalStatus.PENDING_ENTRY, EventKind.CLOSED_MANUALLY): None,
    (SignalStatus.PENDING_ENTRY, EventKind.LADDER_COMPLETE): None,
    (SignalStatus.PENDING_ENTRY, EventKind.NOTE): None,
    # One rung in. The position is real, so the stop governs and the ladder's
    # clock no longer does.
    (SignalStatus.PARTIALLY_FILLED, EventKind.ENTRY_FILLED): SignalStatus.PARTIALLY_FILLED,
    (SignalStatus.PARTIALLY_FILLED, EventKind.TP_HIT): SignalStatus.PARTIALLY_FILLED,
    (SignalStatus.PARTIALLY_FILLED, EventKind.STOPPED): SignalStatus.STOPPED,
    (SignalStatus.PARTIALLY_FILLED, EventKind.INVALIDATED): None,
    (SignalStatus.PARTIALLY_FILLED, EventKind.EXPIRED): None,
    (SignalStatus.PARTIALLY_FILLED, EventKind.CLOSED_MANUALLY): SignalStatus.CLOSED,
    (SignalStatus.PARTIALLY_FILLED, EventKind.LADDER_COMPLETE): None,
    (SignalStatus.PARTIALLY_FILLED, EventKind.NOTE): None,
    # The whole ladder is in.
    (SignalStatus.FILLED, EventKind.ENTRY_FILLED): None,
    (SignalStatus.FILLED, EventKind.TP_HIT): SignalStatus.FILLED,
    (SignalStatus.FILLED, EventKind.STOPPED): SignalStatus.STOPPED,
    (SignalStatus.FILLED, EventKind.INVALIDATED): None,
    (SignalStatus.FILLED, EventKind.EXPIRED): None,
    (SignalStatus.FILLED, EventKind.CLOSED_MANUALLY): SignalStatus.CLOSED,
    (SignalStatus.FILLED, EventKind.LADDER_COMPLETE): None,
    (SignalStatus.FILLED, EventKind.NOTE): None,
}

#: Terminal statuses absorb everything. Spelled as a loop rather than 32 more
#: literal rows, because the rule is one rule and repeating it 32 times would
#: invite someone to "fix" a single row.
for _status in (
    SignalStatus.STOPPED,
    SignalStatus.INVALIDATED,
    SignalStatus.EXPIRED,
    SignalStatus.CLOSED,
):
    for _kind in EventKind:
        TABLE[(_status, _kind)] = None


def check(
    plan: TradePlan,
    config: AppConfig,
    status: SignalStatus,
    kind: EventKind,
    expected: SignalStatus | None,
) -> Transition:
    before = state(plan, status)
    result = advance(before, event_for(kind, plan), management=config.management)
    if expected is None:
        assert not result.applied, (
            f"{status.value} + {kind.value} must change nothing, got {result.status.value}"
        )
        assert result.status is status, "a refused event must leave the status alone"
        assert result.reason, "a refusal must say why — it is read in logs and in tests"
        assert result.event is None, "a refused event must not be journalled or posted"
    else:
        assert result.applied, f"{status.value} + {kind.value} must apply: {result.reason}"
        assert result.status is expected, (
            f"{status.value} + {kind.value} -> expected {expected.value}, got {result.status.value}"
        )
        assert result.event is not None, "an applied event must be journalled"
    return result


CELLS = sorted(TABLE, key=lambda cell: (cell[0].value, cell[1].value))


@pytest.mark.parametrize(("status", "kind"), CELLS)
def test_the_transition_table(
    plan: TradePlan, config: AppConfig, status: SignalStatus, kind: EventKind
) -> None:
    check(plan, config, status, kind, TABLE[(status, kind)])


# --------------------------------------------------------------------------- #
# Meta-tests — the table's own guarantees
# --------------------------------------------------------------------------- #


def test_the_table_covers_every_state_and_every_event() -> None:
    """A new status or a new event kind must not be able to arrive untested.

    This is the assertion that makes the table a table. Without it, adding
    ``SignalStatus.SOMETHING`` would leave eight silent, untested cells.
    """
    assert set(TABLE) == set(product(SignalStatus, EventKind))


def test_the_table_would_catch_a_wrong_transition(plan: TradePlan, config: AppConfig) -> None:
    """Proof of teeth, both ways: a cell that should apply asserted as refused,
    and a cell that should refuse asserted as applying."""
    with pytest.raises(AssertionError):
        check(plan, config, SignalStatus.PENDING_ENTRY, EventKind.ENTRY_FILLED, None)
    with pytest.raises(AssertionError):
        check(
            plan,
            config,
            SignalStatus.STOPPED,
            EventKind.TP_HIT,
            SignalStatus.CLOSED,
        )


# --------------------------------------------------------------------------- #
# The transitions the table can only summarise
# --------------------------------------------------------------------------- #


def test_the_last_rung_completes_the_ladder_and_says_so(plan: TradePlan, config: AppConfig) -> None:
    """§4 reports "Ladder complete, avg 82.68" as its own line, so the machine
    emits a second event rather than folding it into the fill."""
    before = tracking(
        plan,
        status=SignalStatus.PARTIALLY_FILLED,
        fills=(fill(plan, 0), fill(plan, 1)),
    )
    result = advance(
        before,
        MarketEvent(kind=EventKind.ENTRY_FILLED, at=LATER, index=2, price=plan.entries[2].price),
        management=config.management,
    )
    assert result.status is SignalStatus.FILLED
    assert result.event is not None and result.event.kind is EventKind.ENTRY_FILLED
    assert [extra.kind for extra in result.extra_events] == [EventKind.LADDER_COMPLETE]
    # The average the owner actually holds, not §3's risk-weighted gate basis.
    assert result.extra_events[0].price == plan.avg_fill_price == Decimal("82.55")


def test_the_final_target_closes_the_position(plan: TradePlan, config: AppConfig) -> None:
    """TP3 "closes remainder", so it is the one target that ends the signal."""
    before = tracking(
        plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3))
    )
    first = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=2, price=plan.targets[2]),
        management=config.management,
    )
    assert first.status is SignalStatus.CLOSED
    assert first.exit is not None and first.exit.qty == before.open_qty


def test_tp1_moves_the_stop_to_the_price_the_owner_actually_averaged(
    plan: TradePlan, config: AppConfig
) -> None:
    """§5: "TP1: close 40%, move stop to breakeven". Breakeven is
    ``avg_fill_price`` — the quantity-weighted price actually paid — not §3's
    ``avg_entry``, which is a conservative gate basis nobody was filled at."""
    before = tracking(
        plan, status=SignalStatus.FILLED, fills=tuple(fill(plan, i) for i in range(3))
    )
    result = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=0, price=plan.targets[0]),
        management=config.management,
    )
    assert result.stop_price == plan.avg_fill_price == Decimal("82.55")
    assert plan.avg_entry == Decimal("82.675"), "the two averages must stay distinct"


def test_tp2_does_not_move_the_stop_again(plan: TradePlan, config: AppConfig) -> None:
    before = tracking(
        plan,
        status=SignalStatus.FILLED,
        fills=tuple(fill(plan, i) for i in range(3)),
        exits=(LegExit(kind="TP1", price=plan.targets[0], qty=Decimal("25.67"), at=LATER),),
        stop_price=plan.avg_fill_price,
    )
    result = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=1, price=plan.targets[1]),
        management=config.management,
    )
    assert result.stop_price is None, "only TP1 moves the stop (§5)"


def test_a_target_already_taken_is_refused(plan: TradePlan, config: AppConfig) -> None:
    """A tick re-run after a crash replays the same detection. Taking TP1 twice
    would double-count its realized R."""
    before = tracking(
        plan,
        status=SignalStatus.FILLED,
        fills=tuple(fill(plan, i) for i in range(3)),
        exits=(LegExit(kind="TP1", price=plan.targets[0], qty=Decimal("25.67"), at=LATER),),
    )
    result = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=0, price=plan.targets[0]),
        management=config.management,
    )
    assert not result.applied
    assert "already taken" in result.reason


def test_a_rung_already_filled_is_refused(plan: TradePlan, config: AppConfig) -> None:
    before = tracking(plan, status=SignalStatus.PARTIALLY_FILLED, fills=(fill(plan, 0),))
    result = advance(
        before,
        MarketEvent(kind=EventKind.ENTRY_FILLED, at=LATER, index=0, price=plan.entries[0].price),
        management=config.management,
    )
    assert not result.applied
    assert "already filled" in result.reason


def test_a_rung_that_is_not_on_the_plan_is_refused(plan: TradePlan, config: AppConfig) -> None:
    result = advance(
        tracking(plan),
        MarketEvent(kind=EventKind.ENTRY_FILLED, at=LATER, index=7, price=Decimal("83.10")),
        management=config.management,
    )
    assert not result.applied


def test_a_target_that_is_not_on_the_plan_is_refused(plan: TradePlan, config: AppConfig) -> None:
    before = tracking(plan, status=SignalStatus.FILLED, fills=(fill(plan, 0),))
    result = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=9, price=Decimal("99")),
        management=config.management,
    )
    assert not result.applied


def test_a_fully_closed_position_refuses_another_close(plan: TradePlan, config: AppConfig) -> None:
    """Everything closed at TP3 but the status not yet written — the state a crash
    between the exit row and the status update leaves behind."""
    before = tracking(
        plan,
        status=SignalStatus.FILLED,
        fills=(fill(plan, 0),),
        exits=(LegExit(kind="TP3", price=plan.targets[2], qty=plan.entries[0].qty, at=LATER),),
    )
    for kind in (EventKind.STOPPED, EventKind.CLOSED_MANUALLY):
        result = advance(
            before,
            MarketEvent(kind=kind, at=LATER, price=Decimal("84.00")),
            management=config.management,
        )
        assert not result.applied, f"{kind.value} must not re-close a closed position"

    taken = advance(
        before,
        MarketEvent(kind=EventKind.TP_HIT, at=LATER, index=1, price=plan.targets[1]),
        management=config.management,
    )
    assert not taken.applied and "nothing left open" in taken.reason


def test_the_short_mirror_behaves_identically(short_plan: TradePlan, config: AppConfig) -> None:
    """Nothing in the machine may depend on the direction — that belongs to
    detection (which way price has to move) and to the R math (the sign)."""
    before = tracking(short_plan)
    filled = advance(
        before,
        MarketEvent(
            kind=EventKind.ENTRY_FILLED, at=LATER, index=0, price=short_plan.entries[0].price
        ),
        management=config.management,
    )
    assert filled.status is SignalStatus.PARTIALLY_FILLED
    assert filled.fill is not None and filled.fill.qty == short_plan.entries[0].qty

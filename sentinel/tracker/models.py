"""What the tracker observes, and what it decides (ARCHITECTURE.md §3).

The split here is the point of the module. ``MarketEvent`` is a fact about price:
"rung 2's limit would have filled at 12:34". ``Transition`` is a decision about a
signal: "that fill moves it from PENDING_ENTRY to PARTIALLY_FILLED, and here is
the row to write and the reply to post". Detection produces the first, the state
machine turns it into the second, and neither touches a database or a clock.

Keeping them apart is what makes the state machine a *table* — one function of
(status, event) — rather than a loop with the market woven through it. That table
is what specs/MILESTONES.md M7 asks to be tested exhaustively.

No LLM here (CLAUDE.md's deterministic modules). No floats. All money ``Decimal``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from sentinel.bot.models import SignalStatus
from sentinel.bot.plans import AnyPlan
from sentinel.risk.accounting import Exit, Fill


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventKind(StrEnum):
    """The vocabulary of specs/TELEGRAM_UX.md §4, one member per bullet.

    ``LADDER_COMPLETE`` is not a market event of its own — it is the fill of the
    last rung, reported separately because §4 does ("📥 Ladder complete, avg
    82.68"). It is the one kind detection never emits and the machine always does.
    """

    ENTRY_FILLED = "ENTRY_FILLED"
    LADDER_COMPLETE = "LADDER_COMPLETE"
    TP_HIT = "TP_HIT"
    STOPPED = "STOPPED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CLOSED_MANUALLY = "CLOSED_MANUALLY"
    NOTE = "NOTE"


#: Exit kinds as stored in ``signal_exits.kind``.
EXIT_STOP = "STOP"
EXIT_INVALIDATION = "INVALIDATION"
EXIT_EXPIRY = "EXPIRY"
EXIT_MANUAL = "MANUAL"


class MarketEvent(Frozen):
    """One thing the market did, at a known instant.

    ``price`` is the price the leg is accounted at, already chosen conservatively
    by detection — see ``tracker/detect.py`` for which way each leg rounds and why.
    """

    kind: EventKind
    at: datetime
    #: Rung index for a fill, target index for a TP hit, 0 otherwise. Zero-based;
    #: the card adds one, because "Entry 1" is what the owner reads.
    index: int = 0
    price: Decimal = Decimal("0")
    #: The 1h close that broke the invalidation level, when that is what happened.
    reference_price: Decimal | None = None
    #: Why this happened, in words, when the kind alone does not say (M10c). An
    #: expiry has one meaning in a 24/7 market and three in forex: the TTL ran out,
    #: the Friday close arrived first (§5.4), or a currency-matched high-impact event
    #: cancelled the pending ladder (§8). "Expired unfilled" is true of all three and
    #: useful for none of them — a reader who cannot tell a time stop from a central
    #: bank decision cannot learn anything from a run of them.
    cause: str = ""


class RungFill(Frozen):
    """A fill, with the rung it belongs to."""

    rung_index: int
    price: Decimal
    qty: Decimal
    at: datetime

    def as_fill(self) -> Fill:
        return Fill(price=self.price, qty=self.qty)


class LegExit(Frozen):
    """A close, with what closed it."""

    kind: str
    price: Decimal
    qty: Decimal
    at: datetime

    def as_exit(self) -> Exit:
        return Exit(price=self.price, qty=self.qty)


class SignalTracking(Frozen):
    """A signal and everything that has already happened to it.

    Rebuilt from the database on every tick — the tracker keeps no state between
    them, which is the whole of ARCHITECTURE §6's "tracker rebuilds state from DB"
    and the reason a crash needs no recovery routine.
    """

    #: ``TradePlan`` or ``ForexPlan`` — the union ``SignalRecord.plan`` carries.
    #: Everything the tracker reads off a plan is a name the two share on purpose
    #: (FOREX.md §16.2): ``entries[].price``/``.qty``, ``stop``, ``targets``,
    #: ``expires_at``, ``avg_fill_price``, ``planned_risk_eur``, ``direction`` and
    #: ``report.invalidation_price``. That shared vocabulary is exactly what lets
    #: one detection path and one state machine serve both markets.
    plan: AnyPlan
    status: SignalStatus
    fills: tuple[RungFill, ...] = ()
    exits: tuple[LegExit, ...] = ()
    #: Live stop: the plan's, or breakeven once §5's TP1 rule has moved it.
    stop_price: Decimal | None = None

    @property
    def rung_count(self) -> int:
        return len(self.plan.entries)

    @property
    def filled_rungs(self) -> frozenset[int]:
        return frozenset(fill.rung_index for fill in self.fills)

    @property
    def hit_targets(self) -> frozenset[int]:
        prefix = "TP"
        return frozenset(
            int(exit_.kind[len(prefix) :]) - 1
            for exit_ in self.exits
            if exit_.kind.startswith(prefix)
        )

    @property
    def filled_qty(self) -> Decimal:
        return sum((fill.qty for fill in self.fills), Decimal(0))

    @property
    def closed_qty(self) -> Decimal:
        return sum((exit_.qty for exit_ in self.exits), Decimal(0))

    @property
    def open_qty(self) -> Decimal:
        return self.filled_qty - self.closed_qty

    @property
    def live_stop(self) -> Decimal:
        """The stop the tracker is watching now — moved to breakeven after TP1."""
        return self.stop_price if self.stop_price is not None else self.plan.stop


class TrackerEvent(Frozen):
    """A journal entry: what happened, what it did, and what to say about it.

    ``event_key`` is the idempotency spine. It is unique per signal in
    ``signal_events`` and it is the fourth column of ``telegram_messages``' unique
    key, so an event is recorded once and posted once even if a tick is
    interrupted between the two.
    """

    event_key: str
    kind: EventKind
    at: datetime
    from_status: SignalStatus | None = None
    to_status: SignalStatus | None = None
    price: Decimal | None = None
    realized_r: Decimal | None = None
    realized_eur: Decimal | None = None
    detail: str = ""
    #: Pre-rendered fragments for the reply. Never numbers for a card to combine
    #: — specs/TELEGRAM_UX.md §1: the bot renders, it never computes.
    payload: dict[str, str] = {}


class Transition(Frozen):
    """What one event does to one signal. ``applied=False`` means "nothing"."""

    status: SignalStatus
    applied: bool = False
    fill: RungFill | None = None
    exit: LegExit | None = None
    event: TrackerEvent | None = None
    #: Extra events the same market event implies — today only LADDER_COMPLETE.
    extra_events: tuple[TrackerEvent, ...] = ()
    #: Set when §5's TP1 rule moves the stop to breakeven.
    stop_price: Decimal | None = None
    #: Why nothing was applied. Read by the tests, and by anyone reading a log.
    reason: str = ""


__all__ = [
    "EXIT_EXPIRY",
    "EXIT_INVALIDATION",
    "EXIT_MANUAL",
    "EXIT_STOP",
    "EventKind",
    "LegExit",
    "MarketEvent",
    "RungFill",
    "SignalTracking",
    "TrackerEvent",
    "Transition",
]

"""``/journal`` — one user's whole book, as rows rather than as an aggregate (M8.6).

``/stats`` answers "how am I doing" and ``/positions`` answers "what is open right
now". Neither answers "show me what happened, one line per signal" — which is the
thing a person actually reviews, sorts and keeps. PRD F10 says a signal is fully
reconstructable from the database, and it is: by somebody with an SSH session.

This module is the arithmetic half of closing that gap. It is pure, ``Decimal``
throughout and does no I/O, in the manner of ``stats/compute.py``; ``bot/export.py``
turns what comes out of here into an XLSX and computes nothing of its own.

---

**Three definitions are settled here, and each one is a place the journal could
quietly disagree with a surface that already exists.**

1. **1R is ``planned_risk_eur``**, because that is the basis every R figure in the
   system is already on (``tracker/loop.py``: ``realized_eur = r *
   plan.planned_risk_eur``). The ``Risk EUR`` column is therefore that number and
   not ``plan.risk_eur``, the step-floored actual — the two differ by cents, and a
   spreadsheet where ``PnL R x Risk EUR != PnL EUR`` is a spreadsheet nobody trusts
   again.

2. **Net subtracts costs from the numerator only** — the money is
   ``realized_eur - realized_costs_eur`` and the R figure is that divided by 1R.
   Deliberately *not* ``risk/costs.net_rr_multiples``' convention, which also grows
   the denominator: that function prices a **prospective** trade, where the cost is
   money additionally at risk. Here the trade is over, R is a fixed unit of account
   (EUR 75 stays EUR 75) and a running balance has to add up. Whoever reads both
   will think one of them is a bug, so it is written down in both.

3. **A win is *gross* realized R > 0** — ``stats/compute.py``'s definition,
   unchanged — while the **running balance is *net* R**. The two bases differ on
   purpose: the win rate at the bottom of the Real sheet has to equal what
   ``/stats`` reports for the same window, or the two surfaces appear to contradict
   each other; and a balance is money, which costs really do come out of. A trade
   that won gross and lost after fees therefore raises the rate and lowers the
   balance. That is true of ``/stats`` too, and the Legend sheet says so.

**Open signals are rows with blank running columns** (owner ruling, 2026-08-20).
They sit at the bottom of their sheet, and they contribute to neither the balance
nor the rate until they resolve. Had they participated, every earlier row's running
balance would silently renumber on the day an open trade closed and landed somewhere
else in history — a record that reorders itself is not a record. Blank says "not yet
part of this"; a zero would claim the trade contributed nothing, which is a claim
about something that has not finished.

**No user id reaches this module's output.** :class:`JournalRow` has no field that
could carry one — the same type-level boundary as ``bot/views.UserView`` (M8.1 §6)
and ``PulseGateView`` (M8.4 §3). The scoping itself happens one layer down, in
``SignalRepository.journal_since``, whose ``user_id`` is keyword-only with no
default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import ValidationError

from sentinel.bot.models import SignalDecision
from sentinel.bot.plans import AnyPlan, leverage_of, market_of, plan_of
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.risk.accounting import Exit, Fill, avg_exit_price, avg_fill_price
from sentinel.risk.rounding import money, percent, ratio
from sentinel.stats.models import Frozen, Population
from sentinel.storage.models import SignalExitRow, SignalFillRow, SignalRow

log = get_logger(__name__)

HUNDRED = Decimal("100")

#: What the ``Outcome`` column says for a signal that has not resolved yet.
OPEN = "open"

#: What the ``Decided`` column says when no button was ever pressed.
#:
#: A visible mark rather than an empty cell, found by rendering against the live
#: database: openpyxl stores ``""`` as an empty cell, which reads as missing data —
#: and "we do not know" is the opposite of what this cell means. Nobody answered,
#: and that is a fact worth stating.
NOT_DECIDED = "—"


class JournalPopulation(StrEnum):
    """Which sheet a signal lands on.

    ``stats.models.Population``'s three, with ``HYPOTHETICAL`` split in two. That
    split exists only here and only for this file: a card nobody ever answered and a
    card somebody deliberately marked ❌ Skip are the same thing *statistically* —
    the pipeline produced a setup and no money was placed on it — and they are
    completely different things in a personal record. ``/stats`` should not learn
    the difference; a journal should show it.

    :func:`journal_population_of` is proven against ``population_of`` by a meta-test,
    so the split can never drift into a *disagreement*.
    """

    REAL = "REAL"
    HYPOTHETICAL = "HYPOTHETICAL"
    UNDECIDED = "UNDECIDED"
    DRY_RUN = "DRY_RUN"


#: Sheet title and the sentence under it. The note is the whole reason the four are
#: never merged: each answers a different question and only one of them is money.
POPULATION_LABELS: dict[JournalPopulation, tuple[str, str]] = {
    JournalPopulation.REAL: (
        "Real (Taken)",
        "Signals you marked ✅ Taken. Your record — the only sheet with euro P&L.",
    ),
    JournalPopulation.HYPOTHETICAL: (
        "Hypothetical",
        "👀 Watching and ❌ Skipped. Tracked to the end, in R only: no money was placed, "
        "so no euro figure is claimed.",
    ),
    JournalPopulation.UNDECIDED: (
        "Undecided",
        "Cards no button was ever pressed on. Still resolved in the background, and "
        "kept apart because 'not answered' is not a decision.",
    ),
    JournalPopulation.DRY_RUN: (
        "Dry run",
        "Produced by a rehearsal cycle and never delivered to anybody. Never a trade.",
    ),
}

#: The order the sheets appear in, and the order the Legend lists them.
SHEET_ORDER: tuple[JournalPopulation, ...] = (
    JournalPopulation.REAL,
    JournalPopulation.HYPOTHETICAL,
    JournalPopulation.UNDECIDED,
    JournalPopulation.DRY_RUN,
)

#: Sheets written even when they hold no rows, so an empty one reads as "you have
#: none of these" rather than as a sheet somebody forgot. The other two appear only
#: when they have rows, and the Legend carries a count for all four regardless.
ALWAYS_WRITTEN: frozenset[JournalPopulation] = frozenset(
    {JournalPopulation.REAL, JournalPopulation.HYPOTHETICAL}
)


def journal_population_of(decision: SignalDecision | None, *, dry_run: bool) -> JournalPopulation:
    """Which sheet, with ``dry_run`` winning over the decision exactly as it does in
    ``stats.models.population_of``. A rehearsal is a rehearsal however it was
    answered — and it cannot have been answered, because it was never sent."""
    if dry_run:
        return JournalPopulation.DRY_RUN
    if decision is None:
        return JournalPopulation.UNDECIDED
    if decision is SignalDecision.TAKEN:
        return JournalPopulation.REAL
    return JournalPopulation.HYPOTHETICAL


#: How each journal sheet maps back onto the population ``/stats`` counts. Written
#: down rather than inferred so the meta-test has something to compare against.
STATS_POPULATION: dict[JournalPopulation, Population] = {
    JournalPopulation.REAL: Population.REAL,
    JournalPopulation.HYPOTHETICAL: Population.HYPOTHETICAL,
    JournalPopulation.UNDECIDED: Population.HYPOTHETICAL,
    JournalPopulation.DRY_RUN: Population.DRY_RUN,
}


class JournalRow(Frozen):
    """One signal, flattened into the columns of one spreadsheet row.

    **Carries no user id and no chat id**, and that is the boundary rather than a
    rule the writer remembers: this file leaves Telegram as a document and a
    document is forwardable, so a column that could name somebody must not be
    reachable from here at all.

    Every plan-derived field is optional. A row whose stored ``plan`` will not
    validate against the current :class:`TradePlan` still has its own columns —
    symbol, decision, outcome, realized figures — and loses only the sizing block.
    Same posture as ``bot/pulse.symbol_pulse_view`` takes one table over: a later
    schema change costs some cells, never the row.
    """

    number: int
    symbol: str
    direction: str
    setup_type: str
    confidence: int
    #: What was actually pressed: TAKEN | WATCHING | SKIPPED, or "" for nothing.
    #: Kept beside :attr:`population` rather than folded into it because the two
    #: disagree in exactly two useful places — Watching and Skipped share a
    #: population, and a dry-run signal has a population and no decision at all.
    decided: str
    population: str
    time_in: datetime | None
    time_out: datetime | None
    avg_entry: Decimal | None
    avg_exit: Decimal | None
    size_eur: Decimal | None
    leverage: int | None
    sl_pct: Decimal | None
    #: 1R in euros — ``planned_risk_eur``. See the module docstring, definition 1.
    risk_eur: Decimal | None
    pnl_r_gross: Decimal | None
    costs_eur: Decimal | None
    pnl_r_net: Decimal | None
    #: Euro figures are carried only where money was actually placed; the builder
    #: blanks them off the Real sheet.
    pnl_eur_gross: Decimal | None
    pnl_eur_net: Decimal | None
    outcome: str
    #: Blank while the signal is open — never zero. See the module docstring.
    running_r: Decimal | None = None
    win_rate_pct: Decimal | None = None

    @property
    def resolved(self) -> bool:
        return self.time_out is not None


class JournalBook(Frozen):
    """One population's sheet: its title, the sentence under it, and its rows.

    From M10a a sheet is one population **in one market**. The title carries the
    market only when it is not the historical default, so a crypto-only export has
    exactly the four sheet names M8.6 shipped — the names a reader's saved files and
    their spreadsheet formulas already refer to.
    """

    population: JournalPopulation
    market: Market = LEGACY_MARKET
    title: str
    note: str
    rows: tuple[JournalRow, ...] = ()

    @property
    def carries_money(self) -> bool:
        """Only the Real sheet does. Elsewhere no money was placed, so no euro
        figure is asserted (owner's column ruling)."""
        return self.population is JournalPopulation.REAL


def _plan_of(row: SignalRow) -> AnyPlan | None:
    """The stored plan, dispatched on the row's market (§16.7).

    The ``except`` below is a **version** guard, not a model guard: it exists so a plan
    stored under an older schema blanks its sizing columns rather than failing an export
    the owner asked for. Before M10c it was also, accidentally, the thing that would
    have blanked every forex row in the workbook — silently, on a path whose whole
    purpose is a record of what happened.
    """
    try:
        return plan_of(row.plan, market_of(row))
    except ValidationError:
        log.warning(
            "stats.journal_plan_unreadable",
            number=row.number,
            symbol=row.symbol,
            detail="stored plan does not match the current model; sizing columns blank",
        )
        return None


def journal_row_of(
    row: SignalRow,
    fills: Sequence[SignalFillRow],
    exits: Sequence[SignalExitRow],
) -> JournalRow:
    """One ``signals`` row plus its legs, as a spreadsheet row. Running columns unset.

    They are filled in by :func:`build_journal`, because a running balance is a
    property of a *sequence* and nothing here can see one.
    """
    plan = _plan_of(row)
    decision = None if row.decision is None else SignalDecision(row.decision)
    population = journal_population_of(decision, dry_run=row.dry_run)

    closed = tuple(Exit(price=exit_.price, qty=exit_.qty) for exit_ in exits if exit_.qty > 0)
    filled = tuple(Fill(price=fill.price, qty=fill.qty) for fill in fills)

    risk_eur = None if plan is None else plan.planned_risk_eur
    gross_eur = row.realized_eur
    costs_eur = row.realized_costs_eur
    net_eur: Decimal | None = None
    net_r: Decimal | None = None
    if gross_eur is not None:
        net_eur = money(gross_eur - (costs_eur or Decimal(0)))
        if risk_eur is not None and risk_eur > 0:
            net_r = ratio(net_eur / risk_eur)

    carries_money = population is JournalPopulation.REAL
    return JournalRow(
        number=row.number,
        symbol=row.symbol,
        direction=row.direction,
        setup_type=row.setup_type,
        confidence=row.confidence,
        decided=NOT_DECIDED if decision is None else decision.value,
        population=population.value,
        time_in=row.first_fill_at,
        time_out=row.closed_at,
        # The tracker's rolled-up ``avg_fill_price`` is the same figure; recomputing
        # it from the legs keeps entry and exit on one basis, and covers a row whose
        # roll-up predates a leg the recovery path replayed.
        avg_entry=avg_fill_price(filled) if filled else None,
        avg_exit=avg_exit_price(closed) if closed else None,
        size_eur=None if plan is None else plan.notional_eur,
        # Crypto's is derived, forex's is a configured cap (§7.6) — the accessor is
        # where that difference is stated, so the column reads honestly for both.
        leverage=None if plan is None else leverage_of(plan),
        sl_pct=None if plan is None else plan.stop_distance_pct,
        risk_eur=risk_eur,
        pnl_r_gross=row.realized_r,
        costs_eur=costs_eur,
        pnl_r_net=net_r,
        pnl_eur_gross=gross_eur if carries_money else None,
        pnl_eur_net=net_eur if carries_money else None,
        outcome=row.outcome or (OPEN if row.closed_at is None else ""),
    )


def _ordered(rows: Sequence[JournalRow]) -> tuple[JournalRow, ...]:
    """Resolved rows chronologically by resolution, then the open ones (owner ruling).

    ``time_out`` orders the resolved half for the same reason
    ``compute.max_drawdown_r`` sorts by ``closed_at``: a cumulative series is a
    statement about a sequence, and a set sorted any other way produces a different
    and meaningless answer. Open rows are ordered by signal number among themselves,
    which is issue order — the only order they have.
    """
    dated: list[tuple[datetime, int, JournalRow]] = [
        (row.time_out, row.number, row) for row in rows if row.time_out is not None
    ]
    resolved = [row for _, _, row in sorted(dated, key=lambda item: (item[0], item[1]))]
    still_open = sorted((row for row in rows if row.time_out is None), key=lambda row: row.number)
    return (*resolved, *still_open)


def _with_running(rows: Sequence[JournalRow]) -> tuple[JournalRow, ...]:
    """Walk one population once, filling in the running balance and win rate.

    A row that never filled was not a trade (``compute.py``'s rule): it appears, and
    it moves neither figure — the carried-forward values are re-stated on it, which
    is what "the balance as of this row" means. An open row gets neither, and stops
    the walk contributing anything further, because it has no result to contribute.
    """
    balance = Decimal(0)
    traded = 0
    wins = 0
    out: list[JournalRow] = []
    for row in rows:
        if not row.resolved:
            out.append(row)
            continue
        if row.time_in is not None and row.pnl_r_net is not None:
            balance += row.pnl_r_net
            traded += 1
            if (row.pnl_r_gross or Decimal(0)) > 0:
                wins += 1
        out.append(
            row.model_copy(
                update={
                    "running_r": ratio(balance),
                    "win_rate_pct": (
                        percent(Decimal(wins) / Decimal(traded) * HUNDRED) if traded else None
                    ),
                }
            )
        )
    return tuple(out)


def build_journal(
    signals: Sequence[SignalRow],
    *,
    fills: Mapping[UUID, Sequence[SignalFillRow]],
    exits: Mapping[UUID, Sequence[SignalExitRow]],
    market: Market = LEGACY_MARKET,
) -> tuple[JournalBook, ...]:
    """One user's signals, split into sheets and walked for the running columns.

    Every population in :data:`SHEET_ORDER` comes back, empty ones included: the
    caller decides which get a sheet and the Legend reports a count for all four, so
    "you have none of these" is never rendered as an absence somebody has to notice.
    """
    grouped: dict[JournalPopulation, list[JournalRow]] = {group: [] for group in SHEET_ORDER}
    for row in signals:
        built = journal_row_of(row, fills.get(row.id, ()), exits.get(row.id, ()))
        grouped[JournalPopulation(built.population)].append(built)

    books: list[JournalBook] = []
    for population in SHEET_ORDER:
        title, note = POPULATION_LABELS[population]
        books.append(
            JournalBook(
                population=population,
                market=market,
                # A running balance walks within a sheet, so a sheet has to be one
                # market: a balance that stepped from a EUR/USD trade into a BTC one
                # would be the same objection M8.6 already makes about stepping from
                # a trade you took into one you skipped.
                title=title if market is LEGACY_MARKET else f"{title} · {market.value}",
                note=note,
                rows=_with_running(_ordered(grouped[population])),
            )
        )
    return tuple(books)


__all__ = [
    "ALWAYS_WRITTEN",
    "NOT_DECIDED",
    "OPEN",
    "POPULATION_LABELS",
    "SHEET_ORDER",
    "STATS_POPULATION",
    "JournalBook",
    "JournalPopulation",
    "JournalRow",
    "build_journal",
    "journal_population_of",
    "journal_row_of",
]

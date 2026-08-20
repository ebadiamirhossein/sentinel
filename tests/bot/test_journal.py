"""``/journal`` — one user's book as a file, and the boundary that makes it safe (M8.6).

Four kinds of test live here, and the first is the one the milestone exists to pass.

* **The scoping proof, three ways.** User A's file must not contain user B. It is
  asserted at the view level, by reading the produced workbook back through openpyxl
  cell by cell, and by scanning the raw zip's XML. The third is not redundant with
  the second: a value can reach the artefact without reaching a cell openpyxl
  reports back — a shared string left over, a defined name, a comment — and only the
  zip scan looks at what actually leaves the process.
* **The three definitions** ``stats/journal.py`` settles: the 1R basis, net vs
  gross, and a win being counted on gross R so the last row of the Real sheet agrees
  with ``/stats``. Each is a place this file could quietly disagree with a surface
  that already exists.
* **The owner's ruling on open signals** — bottom of the sheet, running columns
  blank, and no effect on any earlier row. The last clause is the point: a record
  that renumbers itself when an open trade resolves is not a record.
* **Closed classifications**, in the manner of ``auth.TABLE`` and M8.4's
  ``SKIP_WORDING``: the four journal populations map onto ``/stats``' three, and a
  fifth added without a label would fail here rather than render as a blank tab.

Rows are the **real** ORM classes, as in ``test_pulse.py``: a renamed column fails
here instead of on somebody's phone.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from sentinel.bot.auth import Actor
from sentinel.bot.cards import journal_caption, journal_empty_card
from sentinel.bot.context import BotContext
from sentinel.bot.export import columns, journal_filename, journal_workbook
from sentinel.bot.handlers import commands
from sentinel.bot.models import SignalDecision
from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig, Secrets, Settings, load_config
from sentinel.stats.journal import (
    ALWAYS_WRITTEN,
    NOT_DECIDED,
    OPEN,
    POPULATION_LABELS,
    SHEET_ORDER,
    STATS_POPULATION,
    JournalPopulation,
    JournalRow,
    build_journal,
    journal_population_of,
)
from sentinel.stats.models import population_of
from sentinel.storage.models import SignalExitRow, SignalFillRow, SignalRow
from tests.bot.telegram_html import assert_sendable
from tests.bot_double import (
    FakeBot,
    FakeDatabase,
    FakeStore,
    _SignalRow,
    fake_repositories,
    member_account,
    owner_account,
)
from tests.risk_double import PLAN_NOW, approved_plan

#: The numeric payload of one XLSX cell. Compared as ``Decimal`` rather than as
#: text: openpyxl serialises through ``"%.16g"``, which writes ``83.10`` as
#: ``83.1`` — a different string and the identical number, and it is the number
#: this codebase promises.
NUMERIC_CELL = re.compile(r"<v>(-?\d+(?:\.\d+)?)</v>")

A = 111
B = 222
TZ = ZoneInfo("Europe/Vilnius")


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def signal(
    config: AppConfig,
    *,
    number: int,
    user_id: int = A,
    symbol: str = "SOLUSDT",
    decision: SignalDecision | None = SignalDecision.TAKEN,
    dry_run: bool = False,
    filled: bool = True,
    closed_after_hours: int | None = 2,
    realized_r: str | None = "1.71",
    costs: str | None = "3.34",
    outcome: str | None = "TP1",
    plan: dict[str, Any] | None = None,
) -> SignalRow:
    """A ``signals`` row as the tracker leaves it.

    ``realized_eur`` is derived from ``realized_r`` the way ``tracker/loop.py``
    derives it — ``r * planned_risk_eur`` — rather than typed in, so the fixtures
    obey the same 1R basis the assertions are about.
    """
    built = approved_plan(config)
    risk = built.planned_risk_eur
    closed = None if closed_after_hours is None else PLAN_NOW + timedelta(hours=closed_after_hours)
    r = None if realized_r is None else Decimal(realized_r)
    return SignalRow(
        id=uuid4(),
        plan_id=uuid4(),
        user_id=user_id,
        number=number,
        symbol=symbol,
        direction=built.direction.value,
        setup_type=built.setup_type.value,
        confidence=built.confidence,
        created_at=PLAN_NOW,
        expires_at=built.expires_at,
        status="CLOSED" if closed else "PENDING_ENTRY",
        decision=None if decision is None else decision.value,
        dry_run=dry_run,
        plan=plan if plan is not None else built.model_dump(mode="json"),
        filled_qty=Decimal("18.30") if filled else Decimal("0"),
        avg_fill_price=Decimal("83.10") if filled else None,
        tp_hits=1 if outcome == "TP1" else 0,
        realized_r=r,
        realized_eur=None if r is None else (r * risk).quantize(Decimal("0.01")),
        realized_costs_eur=None if costs is None else Decimal(costs),
        outcome=outcome,
        first_fill_at=PLAN_NOW if filled else None,
        closed_at=closed,
    )


def legs(row: SignalRow) -> tuple[dict[Any, Any], dict[Any, Any]]:
    """One filled rung and one exit, keyed as the repositories return them."""
    fill = SignalFillRow(
        signal_id=row.id,
        rung_index=0,
        price=Decimal("83.10"),
        qty=Decimal("18.30"),
        filled_at=PLAN_NOW,
        detected_at=PLAN_NOW,
    )
    exit_ = SignalExitRow(
        signal_id=row.id,
        kind="TP1",
        price=Decimal("85.20"),
        qty=Decimal("18.30"),
        exited_at=PLAN_NOW,
        detected_at=PLAN_NOW,
    )
    return {row.id: [fill]}, {row.id: [exit_]}


def books_of(config: AppConfig, rows: list[SignalRow]) -> Any:
    fills: dict[Any, Any] = {}
    exits: dict[Any, Any] = {}
    for row in rows:
        if row.filled_qty > 0:
            fill, exit_ = legs(row)
            fills.update(fill)
            if row.closed_at is not None:
                exits.update(exit_)
    return build_journal(rows, fills=fills, exits=exits)


def book(books: Any, population: JournalPopulation) -> Any:
    return next(item for item in books if item.population is population)


# --------------------------------------------------------------------------- #
# The closed classifications
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("decision", [None, *SignalDecision])
def test_the_journal_split_never_disagrees_with_the_stats_split(
    decision: SignalDecision | None, dry_run: bool
) -> None:
    """The whole cartesian product, mapped back onto ``/stats``' three populations.

    ``/journal`` splits ``HYPOTHETICAL`` into answered-and-declined versus never
    answered, which ``/stats`` should not learn. A *split* is fine; a
    *disagreement* — a signal counted REAL here and HYPOTHETICAL there — would mean
    two surfaces telling one person two different stories about the same trade.
    """
    mine = journal_population_of(decision, dry_run=dry_run)
    assert STATS_POPULATION[mine] == population_of(decision, dry_run=dry_run)


def test_every_population_has_a_label_and_a_place_in_the_order() -> None:
    """A fifth population added without a label would render as a blank tab, and a
    blank tab is the failure this whole file is written to make loud."""
    assert set(POPULATION_LABELS) == set(JournalPopulation)
    assert set(SHEET_ORDER) == set(JournalPopulation)
    assert set(JournalPopulation) >= ALWAYS_WRITTEN


def test_a_rehearsal_is_a_rehearsal_however_it_was_answered() -> None:
    """``dry_run`` wins over the decision, exactly as it does in ``population_of``.

    A dry-run signal cannot carry a decision — it was never delivered, so no button
    existed to press — but if one ever did it would still not be a trade.
    """
    assert journal_population_of(SignalDecision.TAKEN, dry_run=True) is JournalPopulation.DRY_RUN


def test_the_journal_row_has_no_field_that_could_name_a_user() -> None:
    """The boundary as a type, in the manner of ``UserView`` (M8.1 §6).

    This file leaves Telegram as a document and a document is forwardable. A future
    column naming somebody would need a field here first — which is a visible change
    with this test against it.
    """
    assert set(JournalRow.model_fields) == {
        "number",
        "symbol",
        "direction",
        "setup_type",
        "confidence",
        "decided",
        "population",
        "time_in",
        "time_out",
        "avg_entry",
        "avg_exit",
        "size_eur",
        "leverage",
        "sl_pct",
        "risk_eur",
        "pnl_r_gross",
        "costs_eur",
        "pnl_r_net",
        "pnl_eur_gross",
        "pnl_eur_net",
        "outcome",
        "running_r",
        "win_rate_pct",
    }, (
        "a field was added to JournalRow — check it cannot name a person before "
        "widening this list. Every column of /journal is reachable from here."
    )


# --------------------------------------------------------------------------- #
# The three definitions
# --------------------------------------------------------------------------- #


def test_net_is_gross_minus_costs_and_the_sheet_can_be_checked_by_hand(
    bot_config: AppConfig,
) -> None:
    """The reason ``Costs EUR`` and ``PnL EUR (gross)`` are columns at all.

    The database stores realized R **gross** with costs beside it, deliberately
    ("costs are reported beside it rather than baked in, so a card reconciles by
    hand"). Net is this milestone's derivation, and a derived figure nobody can
    check is a figure nobody should trust.
    """
    rows = [signal(bot_config, number=1)]
    row = book(books_of(bot_config, rows), JournalPopulation.REAL).rows[0]

    assert row.pnl_eur_gross is not None and row.costs_eur is not None
    assert row.pnl_eur_net == row.pnl_eur_gross - row.costs_eur


def test_one_r_is_the_column_beside_it_so_the_two_units_reconcile(
    bot_config: AppConfig,
) -> None:
    """``PnL R (net) x Risk EUR (1R)`` reproduces ``PnL EUR (net)`` — within R's own
    rounding, which is the best any 2dp R column can do.

    This is why ``Risk EUR`` is ``planned_risk_eur`` and not ``plan.risk_eur``, the
    step-floored actual: against the wrong basis the gap would be a *bias* growing
    with the figure, rather than the half-a-hundredth-of-R it is here. R has been
    quantized to two decimals everywhere since M4 and the signal card prints it that
    way, so widening it for this one surface would make the journal disagree with the
    card instead. The euro columns are the exact ones, and the Legend says so.
    """
    row = book(books_of(bot_config, [signal(bot_config, number=1)]), JournalPopulation.REAL).rows[0]

    assert row.pnl_r_net is not None and row.risk_eur is not None and row.pnl_eur_net is not None
    reconstructed = (row.pnl_r_net * row.risk_eur).quantize(Decimal("0.01"))
    # Half of one hundredth of R, in euros — the exact width of ``ratio()``'s rounding.
    assert abs(reconstructed - row.pnl_eur_net) <= row.risk_eur * Decimal("0.005")


def test_the_win_rate_is_counted_on_gross_so_it_matches_stats(bot_config: AppConfig) -> None:
    """A trade that won gross and lost after costs still counts as a win.

    Deliberate, and the alternative is worse: ``/stats`` defines a win as realized R
    > 0 on the gross figure, and a journal that used net would report a different win
    rate for the same window — two surfaces contradicting each other about one
    person's record. The running balance is net, because a balance is money. The
    Legend sheet says both.
    """
    # +0.02R gross on a EUR 75 budget is about EUR 1.50, less than the EUR 3.34 round trip.
    rows = [signal(bot_config, number=1, realized_r="0.02")]
    row = book(books_of(bot_config, rows), JournalPopulation.REAL).rows[0]

    assert row.pnl_r_gross is not None and row.pnl_r_net is not None
    assert row.pnl_r_gross > 0 and row.pnl_r_net < 0, "the fixture no longer makes the point"
    assert row.win_rate_pct == Decimal("100"), "a gross win must count as a win"
    assert row.running_r is not None and row.running_r < 0, "the balance must be net"


def test_euro_figures_appear_only_where_money_was_actually_placed(
    bot_config: AppConfig,
) -> None:
    """👀 Watching and ❌ Skipped are tracked in R and never in euros: nobody placed
    the trade, so a euro figure would assert money that was never at risk. The R
    columns stay, which is the entire point of resolving a hypothetical."""
    rows = [
        signal(bot_config, number=1, decision=SignalDecision.TAKEN),
        signal(bot_config, number=2, decision=SignalDecision.SKIPPED),
    ]
    books = books_of(bot_config, rows)

    real = book(books, JournalPopulation.REAL).rows[0]
    hypothetical = book(books, JournalPopulation.HYPOTHETICAL).rows[0]
    assert real.pnl_eur_net is not None and real.pnl_eur_gross is not None
    assert hypothetical.pnl_eur_net is None and hypothetical.pnl_eur_gross is None
    assert hypothetical.pnl_r_net is not None, "the R figure is the point of tracking it"


def test_decided_and_population_are_separate_columns_because_they_differ(
    bot_config: AppConfig,
) -> None:
    """The owner's addition, and the two places it earns its width: Watching and
    Skipped share one population, and a rehearsal has a population and no decision
    at all."""
    rows = [
        signal(bot_config, number=1, decision=SignalDecision.WATCHING),
        signal(bot_config, number=2, decision=SignalDecision.SKIPPED),
        signal(bot_config, number=3, decision=None, dry_run=True),
    ]
    books = books_of(bot_config, rows)

    hypothetical = book(books, JournalPopulation.HYPOTHETICAL).rows
    assert {row.decided for row in hypothetical} == {"WATCHING", "SKIPPED"}
    assert {row.population for row in hypothetical} == {"HYPOTHETICAL"}

    rehearsal = book(books, JournalPopulation.DRY_RUN).rows[0]
    assert rehearsal.decided == NOT_DECIDED, "nobody pressed anything — it was never delivered"
    assert rehearsal.decided != "", (
        "an empty cell reads as missing data; this cell means the opposite — that "
        "nobody answered, which is itself a fact"
    )
    assert rehearsal.population == "DRY_RUN"


# --------------------------------------------------------------------------- #
# Ordering, the running columns, and the owner's ruling on open signals
# --------------------------------------------------------------------------- #


def test_resolved_rows_are_ordered_by_when_they_resolved(bot_config: AppConfig) -> None:
    """Not by signal number: a cumulative series is a statement about a sequence,
    and the sequence a balance follows is the order results landed in. Signal 2 was
    issued second and closed first."""
    rows = [
        signal(bot_config, number=1, closed_after_hours=9),
        signal(bot_config, number=2, closed_after_hours=3),
    ]
    ordered = book(books_of(bot_config, rows), JournalPopulation.REAL).rows
    assert [row.number for row in ordered] == [2, 1]


def test_an_open_signal_sits_at_the_bottom_with_its_running_columns_blank(
    bot_config: AppConfig,
) -> None:
    """The owner's ruling. Blank, **not zero**: zero would claim the trade
    contributed nothing, which is a claim about something that has not finished."""
    rows = [
        signal(bot_config, number=1),
        signal(
            bot_config, number=2, closed_after_hours=None, realized_r=None, costs=None, outcome=None
        ),
    ]
    ordered = book(books_of(bot_config, rows), JournalPopulation.REAL).rows

    assert [row.number for row in ordered] == [1, 2]
    still_open = ordered[-1]
    assert still_open.outcome == OPEN
    assert still_open.running_r is None and still_open.win_rate_pct is None


def test_an_open_signal_changes_no_earlier_running_balance(bot_config: AppConfig) -> None:
    """The reasoning behind the ruling, asserted rather than trusted.

    Had an open row participated, every earlier row's balance would silently
    renumber on the day that trade resolved and landed somewhere else in history. A
    record that reorders itself is not a record.
    """
    closed = [signal(bot_config, number=1), signal(bot_config, number=2)]
    without = [
        row.running_r for row in book(books_of(bot_config, closed), JournalPopulation.REAL).rows
    ]

    with_open = [
        *closed,
        signal(
            bot_config, number=3, closed_after_hours=None, realized_r=None, costs=None, outcome=None
        ),
    ]
    after = book(books_of(bot_config, with_open), JournalPopulation.REAL).rows
    assert [row.running_r for row in after[:2]] == without


def test_a_signal_that_never_filled_is_a_row_and_moves_neither_figure(
    bot_config: AppConfig,
) -> None:
    """``compute.py``'s rule, unchanged: the price never reached a rung, so it was
    not a trade. It is still a row — it happened, and a journal that hid it would be
    hiding the expiries."""
    rows = [
        signal(bot_config, number=1),
        signal(bot_config, number=2, filled=False, realized_r=None, costs=None, outcome="EXPIRY"),
    ]
    ordered = book(books_of(bot_config, rows), JournalPopulation.REAL).rows

    assert [row.number for row in ordered] == [1, 2]
    assert ordered[1].outcome == "EXPIRY"
    assert ordered[1].running_r == ordered[0].running_r
    assert ordered[1].win_rate_pct == ordered[0].win_rate_pct


def test_the_running_balance_accumulates_within_a_population_and_never_across_one(
    bot_config: AppConfig,
) -> None:
    """The reason there is a sheet per population rather than a column on one sheet.

    A balance that walked from a trade somebody placed into one they skipped would be
    a number nothing in this system endorses — ``stats/models.py``'s three
    populations are "reported separately and never merged".
    """
    rows = [
        signal(bot_config, number=1, decision=SignalDecision.TAKEN),
        signal(bot_config, number=2, decision=SignalDecision.SKIPPED, closed_after_hours=3),
        signal(bot_config, number=3, decision=SignalDecision.TAKEN, closed_after_hours=4),
    ]
    books = books_of(bot_config, rows)

    real = book(books, JournalPopulation.REAL).rows
    hypothetical = book(books, JournalPopulation.HYPOTHETICAL).rows
    assert [row.number for row in real] == [1, 3]
    assert real[1].running_r == real[0].running_r + (real[1].pnl_r_net or Decimal(0))
    # The hypothetical sheet starts its own series from zero rather than continuing.
    assert hypothetical[0].running_r == hypothetical[0].pnl_r_net


def test_a_plan_that_will_not_validate_costs_the_sizing_and_not_the_row(
    bot_config: AppConfig,
) -> None:
    """M8.5 decision 4's posture, one table over. The verdict columns live on the row
    itself and survive; a later ``TradePlan`` change must not make old signals
    vanish from somebody's own history."""
    rows = [signal(bot_config, number=1, plan={"schema_version": 99})]
    row = book(books_of(bot_config, rows), JournalPopulation.REAL).rows[0]

    assert row.symbol == "SOLUSDT" and row.outcome == "TP1"
    assert row.size_eur is None and row.leverage is None and row.risk_eur is None
    assert row.pnl_r_gross is not None, "the gross figure is a column, not a plan field"


# --------------------------------------------------------------------------- #
# The workbook
# --------------------------------------------------------------------------- #


def workbook_of(books: Any) -> bytes:
    return journal_workbook(books, window_label="all", generated_at=PLAN_NOW, tz=TZ)


def every_cell(data: bytes) -> list[str]:
    """Every populated cell of every sheet, as text."""
    workbook = load_workbook(io.BytesIO(data))
    return [
        str(cell.value)
        for sheet in workbook.worksheets
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    ]


def raw_xml(data: bytes) -> str:
    """Every XML part of the file, concatenated.

    Not the same assertion as :func:`every_cell`, and deliberately kept beside it: a
    value can reach the artefact without reaching a cell openpyxl reports back — a
    stale shared string, a defined name, a comment — and this is what actually leaves
    the process.
    """
    archive = zipfile.ZipFile(io.BytesIO(data))
    return "".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())


def test_the_real_and_hypothetical_sheets_are_written_even_when_empty(
    bot_config: AppConfig,
) -> None:
    """ "You have none of these" has to read as an answer rather than as a tab
    somebody forgot to add. Undecided and Dry run appear only when they hold rows,
    and the Legend counts all four either way."""
    data = workbook_of(books_of(bot_config, [signal(bot_config, number=1)]))
    names = load_workbook(io.BytesIO(data)).sheetnames

    assert names == ["Real (Taken)", "Hypothetical", "Legend"]


def test_a_population_with_rows_always_gets_its_own_sheet(bot_config: AppConfig) -> None:
    rows = [
        signal(bot_config, number=1),
        signal(bot_config, number=2, decision=None),
        signal(bot_config, number=3, decision=None, dry_run=True),
    ]
    names = load_workbook(io.BytesIO(workbook_of(books_of(bot_config, rows)))).sheetnames

    assert names == ["Real (Taken)", "Hypothetical", "Undecided", "Dry run", "Legend"]


def test_the_legend_counts_every_population_including_the_empty_ones(
    bot_config: AppConfig,
) -> None:
    """journal/M8_2_REPORT.md's no-silent-caps rule. A reader must never have to
    guess whether an absent sheet means "none" or "not exported"."""
    data = workbook_of(books_of(bot_config, [signal(bot_config, number=1)]))
    legend = load_workbook(io.BytesIO(data))["Legend"]
    pairs = {str(row[0].value): str(row[1].value) for row in legend.iter_rows(max_col=2)}

    for population in JournalPopulation:
        title, note = POPULATION_LABELS[population]
        assert title in pairs, f"the Legend never mentions the {title} sheet"
        assert pairs[title].startswith("0 row(s)") or "row(s)" in pairs[title]
        assert note in pairs[title], "the count and what it counts belong on one row"


def test_the_hypothetical_sheet_has_no_euro_pnl_columns_at_all(bot_config: AppConfig) -> None:
    """Dropped rather than written empty. On a sheet of signals nobody placed, a
    euro figure would assert something untrue — and a column of blanks reads as a
    bug rather than as a boundary."""
    headers = {
        name: [column.header for column in columns(zone="UTC", money_columns=money)]
        for name, money in (("real", True), ("hypothetical", False))
    }

    assert "PnL EUR (net)" in headers["real"] and "PnL EUR (gross)" in headers["real"]
    assert not [header for header in headers["hypothetical"] if header.startswith("PnL EUR")]
    assert "PnL R (net)" in headers["hypothetical"], "the R figure is why it is tracked"
    assert "Risk EUR (1R)" in headers["hypothetical"], (
        "the 1R basis stays: it is the unit the R columns are measured in, not money anybody placed"
    )


def test_every_money_and_price_figure_round_trips_through_the_file(
    bot_config: AppConfig,
) -> None:
    """Where ``Decimal`` ends is the file format, and nothing is altered on the way.

    XLSX holds numbers as IEEE doubles — that is the format, not a library choice —
    and openpyxl writes them at sixteen significant digits, which is why the raw XML
    for 83.10 reads ``83.09999999999999``. That text parses back to the *same*
    double, and every money column carries a 2dp number format, so what a reader
    sees and computes with is the figure this system produced. The assertion is
    therefore on the round trip rather than on the bytes: the bytes are allowed to
    be ugly, the value is not allowed to move.

    This is the test that fails on the day a figure does move, which is the day this
    file would start lying about money.
    """
    books = books_of(bot_config, [signal(bot_config, number=1)])
    row = book(books, JournalPopulation.REAL).rows[0]
    sheet = load_workbook(io.BytesIO(workbook_of(books)))["Real (Taken)"]
    headers = [cell.value for cell in sheet[1]]

    def cell(header: str) -> Decimal:
        return Decimal(str(sheet.cell(row=2, column=headers.index(header) + 1).value))

    assert cell("PnL EUR (net)") == row.pnl_eur_net
    assert cell("PnL EUR (gross)") == row.pnl_eur_gross
    assert cell("Costs EUR") == row.costs_eur
    assert cell("Risk EUR (1R)") == row.risk_eur
    assert cell("Size EUR") == row.size_eur
    assert cell("Avg entry") == row.avg_entry
    assert cell("PnL R (net)") == row.pnl_r_net
    assert cell("Running R (net)") == row.running_r


def test_the_money_columns_carry_a_two_decimal_format(bot_config: AppConfig) -> None:
    """The other half of the guarantee above. A double's shortest form is not
    necessarily two decimals, and a euro column that rendered as ``124.9099999`` in
    somebody's Excel would look wrong even though the value is right."""
    books = books_of(bot_config, [signal(bot_config, number=1)])
    sheet = load_workbook(io.BytesIO(workbook_of(books)))["Real (Taken)"]
    headers = [cell.value for cell in sheet[1]]

    for header in ("PnL EUR (net)", "Costs EUR", "Risk EUR (1R)", "Size EUR"):
        assert sheet.cell(row=2, column=headers.index(header) + 1).number_format == "#,##0.00"


def test_timestamps_are_written_naive_in_the_owner_timezone(bot_config: AppConfig) -> None:
    """Excel cannot hold a timezone — openpyxl raises on a tz-aware datetime — so
    the instant is converted and the **zone name** goes in the header. The name and
    not the abbreviation, because a window spanning a DST change would have half its
    rows mislabelled by an abbreviation read off one instant."""
    data = workbook_of(books_of(bot_config, [signal(bot_config, number=1)]))
    sheet = load_workbook(io.BytesIO(data))["Real (Taken)"]
    headers = [cell.value for cell in sheet[1]]
    written = sheet.cell(row=2, column=headers.index("Time in — first fill (Europe/Vilnius)") + 1)

    assert written.value is not None
    assert written.value.tzinfo is None
    assert written.value == PLAN_NOW.astimezone(TZ).replace(tzinfo=None)


def test_the_filename_carries_no_user_id_and_no_name() -> None:
    """A document is forwardable and its filename is the part that travels furthest:
    it survives being saved, re-sent and screenshot long after the caption is gone."""
    name = journal_filename(PLAN_NOW, TZ)

    assert name == "sentinel-journal-2026-08-18.xlsx"
    assert str(A) not in name and str(B) not in name


# --------------------------------------------------------------------------- #
# The handler, and the scoping proof this milestone exists to pass
# --------------------------------------------------------------------------- #


class FakeMessage:
    """The two things the handler touches on a Message: the sender, and ``answer``."""

    def __init__(self, user_id: int = A) -> None:
        self.from_user = _User(user_id)
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeCommand:
    def __init__(self, args: str | None = None) -> None:
        self.args = args


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.users[A] = owner_account(A)
    store.users[B] = member_account(B)
    return store


@pytest.fixture
def ctx(store: FakeStore) -> BotContext:
    return BotContext(
        settings=Settings(secrets=Secrets(_env_file=None), config=load_config()),
        database=FakeDatabase(store),  # type: ignore[arg-type]
        clock=FrozenClock(PLAN_NOW),
        tz=TZ,
        repositories=fake_repositories(),
    )


def stored(
    store: FakeStore, *, number: int, user_id: int, symbol: str, bot_config: AppConfig
) -> None:
    built = approved_plan(bot_config)
    row = _SignalRow(
        signal_id=uuid4(),
        plan_id=uuid4(),
        number=number,
        user_id=user_id,
        symbol=symbol,
        decision=SignalDecision.TAKEN.value,
        plan=built.model_dump(mode="json"),
        created_at=PLAN_NOW,
        closed_at=PLAN_NOW + timedelta(hours=2),
        filled_qty=Decimal("18.30"),
        avg_fill_price=Decimal("83.10"),
        first_fill_at=PLAN_NOW,
        realized_r=Decimal("1.71"),
        realized_eur=Decimal("128.25"),
        realized_costs_eur=Decimal("3.34"),
        outcome="TP1",
        tp_hits=1,
    )
    store.signals[row.signal_id] = row


async def run(ctx: BotContext, *, user_id: int = A, args: str | None = None) -> tuple[Any, FakeBot]:
    message = FakeMessage(user_id)
    bot = FakeBot()
    actor = Actor(user_id=user_id, account=owner_account(user_id))
    await commands.journal(message, FakeCommand(args), ctx, actor, bot)
    return message, bot


async def test_an_empty_journal_is_a_sentence_and_not_a_file(ctx: BotContext) -> None:
    """An empty spreadsheet is indistinguishable from a broken export, and the
    reader would have to open it to find out which it was."""
    message, bot = await run(ctx)

    assert bot.of("send_document") == [], "an empty book must not produce a file"
    assert "nothing to export" in message.replies[-1]


async def test_a_journal_with_rows_arrives_as_a_document(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig
) -> None:
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    message, bot = await run(ctx)

    sent = bot.of("send_document")
    assert len(sent) == 1
    assert message.replies == [], "the file is the answer; a second message is noise"
    assert sent[0].kwargs["document"].filename == "sentinel-journal-2026-08-18.xlsx"
    assert "yours alone" in sent[0].kwargs["caption"]


async def test_the_file_is_addressed_to_the_caller_and_not_to_the_chat(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig
) -> None:
    """Somebody's whole trading record should leave the process pointed at exactly
    one place, which is where every other outbound in this codebase is addressed."""
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    _, bot = await run(ctx)

    assert bot.of("send_document")[0].kwargs["chat_id"] == A


@pytest.mark.parametrize("args", ["7d", "week", "everything", "30"])
async def test_an_unrecognised_window_gets_usage_rather_than_the_whole_history(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig, args: str
) -> None:
    """M8.4's ruling for ``/pulse 7d``. Somebody who asked for a week and silently
    received two years would read the file as a week's worth — a wrong answer that
    looks like a right one."""
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    message, bot = await run(ctx, args=args)

    assert bot.of("send_document") == []
    assert "Usage" in message.replies[-1]


async def test_user_a_cannot_receive_a_row_belonging_to_user_b(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig
) -> None:
    """**The test this milestone exists to pass**, asserted three ways.

    The view level says the builder never grouped B's row. Reading the workbook back
    through openpyxl says no cell holds it. The raw zip scan says it is not in the
    artefact at all — which is not the same assertion, because a value can reach the
    file without reaching a cell openpyxl reports back, and the artefact is what
    actually leaves the process.
    """
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    stored(store, number=987654, user_id=B, symbol="LINKUSDT", bot_config=bot_config)

    _, bot = await run(ctx, user_id=A)
    data: bytes = bot.of("send_document")[0].kwargs["document"].data

    cells = every_cell(data)
    assert "SOLUSDT" in cells, "the caller's own row has to be there, or this proves nothing"
    assert "LINKUSDT" not in cells
    assert "987654" not in cells

    xml = raw_xml(data)
    assert "LINKUSDT" not in xml
    assert "987654" not in xml


async def test_the_scoping_proof_would_notice_a_leak(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig
) -> None:
    """Proof of teeth. A guard that cannot fail is not a guard, so the same
    assertions are run against a file built from *both* users' rows and must fail."""
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    stored(store, number=987654, user_id=B, symbol="LINKUSDT", bot_config=bot_config)

    both: list[Any] = list(store.signals.values())
    leaked = workbook_of(build_journal(both, fills={}, exits={}))
    assert "LINKUSDT" in every_cell(leaked)
    assert "LINKUSDT" in raw_xml(leaked)


async def test_the_window_narrows_what_is_exported(
    ctx: BotContext, store: FakeStore, bot_config: AppConfig
) -> None:
    """The window is on ``created_at``, not ``closed_at`` — an export is windowed by
    when a signal was issued, or one sent inside the window and still running would
    fall out of its own journal."""
    stored(store, number=1, user_id=A, symbol="SOLUSDT", bot_config=bot_config)
    old = store.signals[next(iter(store.signals))]
    old.created_at = PLAN_NOW - timedelta(days=200)

    message, bot = await run(ctx, args="30d")
    assert bot.of("send_document") == []
    assert "nothing to export" in message.replies[-1]

    _, bot = await run(ctx, args="all")
    assert len(bot.of("send_document")) == 1


def test_the_empty_card_names_the_window_and_a_way_forward() -> None:
    """Two different nothings — a fresh account and a narrow window — and only one
    of them is fixed by asking for more."""
    card = journal_empty_card("30d")

    assert "30d" in card
    assert "/journal all" in card and "/pulse" in card and "/capital" in card


def test_the_caption_counts_the_sheets_it_shipped(bot_config: AppConfig) -> None:
    books = books_of(bot_config, [signal(bot_config, number=1)])
    caption = journal_caption(books, window_label="all")

    assert "Real (Taken) 1" in caption and "Hypothetical 0" in caption
    assert "never mixed" in caption


def test_the_workbook_opens_on_the_first_sheet_that_has_rows(bot_config: AppConfig) -> None:
    """A new member's Real sheet is empty until they press ✅ Taken, and a file that
    opens on a blank grid reads as a broken export.

    The sheet *order* is unchanged — Real stays first, because it is the book that
    matters — and only the active tab moves. Both halves are asserted, since "fix it
    by reordering the sheets" is the obvious wrong way to do this.
    """
    rows = [signal(bot_config, number=1, decision=SignalDecision.SKIPPED)]
    workbook = load_workbook(io.BytesIO(workbook_of(books_of(bot_config, rows))))

    assert workbook.sheetnames[0] == "Real (Taken)", "the order must not change"
    assert workbook.active.title == "Hypothetical"


def test_it_opens_on_real_whenever_real_has_anything(bot_config: AppConfig) -> None:
    rows = [
        signal(bot_config, number=1, decision=SignalDecision.TAKEN),
        signal(bot_config, number=2, decision=SignalDecision.SKIPPED),
    ]
    workbook = load_workbook(io.BytesIO(workbook_of(books_of(bot_config, rows))))

    assert workbook.active.title == "Real (Taken)"


def test_a_journal_of_nothing_but_rehearsals_opens_on_the_rehearsals(
    bot_config: AppConfig,
) -> None:
    """The state the live database is actually in for a brand-new member, and the
    one the first draft would have opened on an empty Real sheet for."""
    rows = [signal(bot_config, number=1, decision=None, dry_run=True)]
    workbook = load_workbook(io.BytesIO(workbook_of(books_of(bot_config, rows))))

    assert workbook.active.title == "Dry run"


def test_the_caption_and_the_empty_card_are_something_telegram_will_send(
    bot_config: AppConfig,
) -> None:
    """The same sweep ``test_snapshot.py`` runs, over the two things ``/journal``
    says in words. The symbol and the window label both reach these unescaped-by-
    default, and a caption Telegram refuses means the **document** does not arrive."""
    books = books_of(bot_config, [signal(bot_config, number=1)])

    assert_sendable(journal_caption(books, window_label="all"), what="the journal caption")
    assert_sendable(journal_empty_card("30d"), what="the empty-journal card")

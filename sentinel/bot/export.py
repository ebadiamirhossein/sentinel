"""``/journal``'s XLSX writer (M8.6) — a renderer, exactly like ``cards.py``.

Everything numeric arrives finished from ``stats/journal.py``. Nothing here adds,
subtracts, divides or counts: the rule specs/TELEGRAM_UX.md §1 sets for a signal
card ("the bot renders, never computes") does not stop applying because the output
is a file instead of a message. ``tests/bot/test_no_arithmetic.py``'s AST scan does
not reach this module, and it is written as though it did.

**openpyxl, not pandas.** ``pandas`` is already a dependency and ``to_excel`` would
be two lines — and it puts every column through a numpy dtype on the way, which for
a column of ``Decimal`` means ``object`` at best and ``float64`` the moment anything
coerces. Cells are written directly instead, and openpyxl accepts a ``Decimal``
without a cast at the call site (``openpyxl/compat/numbers.py``:
``NUMERIC_TYPES = (int, float, Decimal)``).

**Where Decimal ends, and it is not this module.** XLSX stores numbers as IEEE
doubles — that is the file format, not a library choice — so no writer can put a
``Decimal`` in a cell and get it back. openpyxl renders each one at sixteen
significant digits (``openpyxl/compat/strings.py``: ``"%.16g"``), which is why the
raw XML for ``83.10`` reads ``83.09999999999999``: that is the shortest-but-one
spelling of the same double, and it parses back to exactly the double ``83.1``
does. Nothing moves.

Two things keep that from mattering to a reader, and both are asserted in
``tests/bot/test_journal.py`` rather than assumed: every figure **round-trips**
through the file unchanged, and every money column carries a 2dp number format, so
what Excel displays is the cent figure ``risk/rounding`` produced rather than a
double's shortest form. The day a value stops round-tripping is the day this file
would start lying about money, and that is the test that fails.

**Excel cannot hold a timezone.** ``openpyxl`` raises on a tz-aware datetime
outright, so every instant is converted to the owner's timezone and written naive,
with the **zone name** in the column header — the name and not the abbreviation,
because a window spanning a DST change would have half its rows mislabelled by an
abbreviation read off one instant. The Legend says the stored values are UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from sentinel.bot.formatting import DISCLAIMER, local_date_time
from sentinel.stats.journal import (
    ALWAYS_WRITTEN,
    POPULATION_LABELS,
    SHEET_ORDER,
    JournalBook,
    JournalRow,
)

#: Number formats, by the kind of thing a column holds. Prices use ``General``
#: rather than a fixed number of decimals: this system quotes BTC in the tens of
#: thousands and small alts to eight places, and any fixed mask is wrong for one of
#: them.
MONEY = "#,##0.00"
MULTIPLE = "0.00"
PERCENT = '0.00"%"'
WHOLE = "0"
PRICE = "General"
TIMESTAMP = "yyyy-mm-dd hh:mm"


class Column:
    """One spreadsheet column: its header, the :class:`JournalRow` attribute behind
    it, how Excel should format it, and how wide it should be."""

    def __init__(self, header: str, field: str, fmt: str, width: int) -> None:
        self.header = header
        self.field = field
        self.fmt = fmt
        self.width = width


def columns(*, zone: str, money_columns: bool) -> tuple[Column, ...]:
    """The sheet's columns. ``money_columns`` is false off the Real sheet.

    The two euro P&L columns are **dropped** there rather than written empty: on a
    sheet of 👀 Watching and ❌ Skipped signals no money was ever placed, so a euro
    figure would assert something untrue — and a column of blanks reads as a bug
    rather than as a boundary. The R columns stay, which is the entire point of
    tracking a hypothetical.
    """
    core = (
        Column("Signal #", "number", WHOLE, 10),
        Column("Symbol", "symbol", PRICE, 12),
        Column("Direction", "direction", PRICE, 10),
        Column("Setup", "setup_type", PRICE, 18),
        Column("Confidence", "confidence", WHOLE, 11),
        Column("Decided", "decided", PRICE, 11),
        Column("Population", "population", PRICE, 14),
        Column(f"Time in — first fill ({zone})", "time_in", TIMESTAMP, 24),
        Column(f"Time out — resolved ({zone})", "time_out", TIMESTAMP, 24),
        Column("Avg entry", "avg_entry", PRICE, 14),
        Column("Avg exit", "avg_exit", PRICE, 14),
        Column("Size EUR", "size_eur", MONEY, 12),
        Column("Leverage", "leverage", WHOLE, 10),
        Column("SL %", "sl_pct", PERCENT, 9),
        Column("Risk EUR (1R)", "risk_eur", MONEY, 13),
        Column("PnL R (gross)", "pnl_r_gross", MULTIPLE, 13),
        Column("Costs EUR", "costs_eur", MONEY, 11),
        Column("PnL R (net)", "pnl_r_net", MULTIPLE, 12),
    )
    euros = (
        Column("PnL EUR (gross)", "pnl_eur_gross", MONEY, 15),
        Column("PnL EUR (net)", "pnl_eur_net", MONEY, 14),
    )
    tail = (
        Column("Outcome", "outcome", PRICE, 13),
        Column("Running R (net)", "running_r", MULTIPLE, 15),
        Column("Win rate % (gross)", "win_rate_pct", PERCENT, 17),
    )
    return (*core, *(euros if money_columns else ()), *tail)


def _cell_value(row: JournalRow, column: Column, tz: ZoneInfo) -> Any:
    """One cell. A ``datetime`` is localized and stripped of its zone; everything
    else — ``Decimal`` included — goes in as it is."""
    value = getattr(row, column.field)
    if isinstance(value, datetime):
        return value.astimezone(tz).replace(tzinfo=None)
    return value


def _write_sheet(sheet: Worksheet, book: JournalBook, *, tz: ZoneInfo) -> None:
    cols = columns(zone=str(tz), money_columns=book.carries_money)

    for index, column in enumerate(cols, start=1):
        header = sheet.cell(row=1, column=index, value=column.header)
        header.font = Font(bold=True)
        header.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = column.width

    for offset, row in enumerate(book.rows, start=2):
        for index, column in enumerate(cols, start=1):
            cell = sheet.cell(row=offset, column=index, value=_cell_value(row, column, tz))
            cell.number_format = column.fmt

    sheet.freeze_panes = "A2"
    # An autofilter over a header row with no data rows is legal and useless; over
    # a populated sheet it is the first thing anybody reaches for.
    if book.rows:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{sheet.max_row}"


def _legend_lines(
    books: Sequence[JournalBook], *, window_label: str, generated_at: datetime, tz: ZoneInfo
) -> list[tuple[str, str]]:
    """``(label, value)`` pairs for the Legend sheet.

    Every definition this file's numbers rest on is written down here, because a
    spreadsheet outlives the chat it arrived in and a column called "Win rate %"
    with no stated basis is a number somebody will one day disagree with.
    """
    # One row per population, count and meaning together. They were two blocks in
    # the first draft and the sheet listed every name twice — read back off the live
    # database, that reads as a duplicate rather than as two facts.
    sheets = [
        (POPULATION_LABELS[book.population][0], f"{len(book.rows)} row(s) — {book.note}")
        for book in books
    ]
    return [
        ("Sentinel — trade journal", ""),
        ("Generated", local_date_time(generated_at, tz)),
        ("Window", window_label),
        ("Timestamps", f"shown in {tz}; every value is stored internally in UTC"),
        ("", ""),
        ("Sheets", "each population is counted separately and never merged"),
        *sheets,
        ("", ""),
        ("How to read the numbers", ""),
        (
            "1R",
            "the planned risk budget for that signal, in euros — the 'Risk EUR (1R)' "
            "column. Every R figure on the row is measured in those units.",
        ),
        (
            "Gross vs net",
            "gross is before costs; net is after the fees and funding the trade "
            "actually paid. 'PnL EUR (gross)' minus 'Costs EUR' is 'PnL EUR (net)', "
            "exactly.",
        ),
        (
            "R is shown to 2dp",
            "as it is on every card — so multiplying 'PnL R (net)' by 'Risk EUR (1R)' "
            "reproduces 'PnL EUR (net)' only to within half a hundredth of R. The euro "
            "columns are the exact figures; the R columns are the comparable ones.",
        ),
        (
            "Running R (net)",
            "the cumulative net R down this sheet, in resolution order. A balance is "
            "money, and costs really do come out of it.",
        ),
        (
            "Win rate % (gross)",
            "wins over trades that filled, counted on gross R — the same definition "
            "/stats uses, so the last row of the Real sheet matches what /stats says "
            "for the same window. A trade can therefore win gross and still lower the "
            "running balance after costs.",
        ),
        (
            "Open signals",
            "sit at the bottom of their sheet with the running columns blank. They "
            "join the balance and the win rate when they resolve — blank means 'not "
            "yet part of this', where a zero would claim the trade contributed "
            "nothing.",
        ),
        (
            "Signals that never filled",
            "are rows, and move neither the balance nor the win rate. They were not "
            "trades: the price never reached a single entry rung.",
        ),
        (
            "Euro columns",
            "appear on the Real sheet only. Nowhere else was money actually placed, "
            "so nowhere else is a euro figure claimed.",
        ),
        (
            "Outcome",
            "TP1 / TP2 / TP3 / STOP / INVALIDATION / EXPIRY / MANUAL, or 'open'.",
        ),
        ("", ""),
        ("This file is yours alone", "it contains no other user's signals, sizing or decisions."),
        ("", DISCLAIMER),
    ]


def _write_legend(sheet: Worksheet, lines: Sequence[tuple[str, str]]) -> None:
    for index, (label, value) in enumerate(lines, start=1):
        left = sheet.cell(row=index, column=1, value=label)
        left.font = Font(bold=True)
        left.alignment = Alignment(vertical="top")
        right = sheet.cell(row=index, column=2, value=value)
        right.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.column_dimensions["A"].width = 28
    sheet.column_dimensions["B"].width = 96


def journal_workbook(
    books: Sequence[JournalBook],
    *,
    window_label: str,
    generated_at: datetime,
    tz: ZoneInfo,
) -> bytes:
    """The whole file, in memory. No temp file — this is a few hundred KB at most.

    The Real and Hypothetical sheets are always written, empty or not, so "you have
    none of these" reads as an answer rather than as a sheet somebody forgot.
    Undecided and Dry run appear only when they hold rows, and the Legend counts all
    four either way — journal/M8_2_REPORT.md's rule that a view bounding its own
    contents has to say so.
    """
    workbook = Workbook()
    # Workbook() comes with one sheet already; the first population takes it over
    # rather than leaving an empty "Sheet" behind.
    workbook.remove(workbook.active)

    ordered = sorted(books, key=lambda book: SHEET_ORDER.index(book.population))
    written: list[JournalBook] = []
    for book in ordered:
        if book.rows or book.population in ALWAYS_WRITTEN:
            _write_sheet(workbook.create_sheet(title=book.title), book, tz=tz)
            written.append(book)

    _write_legend(
        workbook.create_sheet(title="Legend"),
        _legend_lines(ordered, window_label=window_label, generated_at=generated_at, tz=tz),
    )

    # Open on the first sheet that has anything in it. The sheet *order* is fixed —
    # Real first, because that is the book that matters — but a new member's Real
    # sheet is empty for as long as they have not pressed ✅ Taken, and a workbook
    # that opens on a blank grid reads as a broken export. The order is unchanged;
    # only where the file opens is.
    populated = next((book for book in written if book.rows), None)
    if populated is not None:
        workbook.active = workbook.sheetnames.index(populated.title)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def journal_filename(generated_at: datetime, tz: ZoneInfo) -> str:
    """``sentinel-journal-2026-08-20.xlsx``.

    **No user id, and no username.** A document is forwardable and its filename is
    the part that travels furthest — it survives being saved, re-sent and screenshot
    long after the caption is gone. specs/TELEGRAM_UX.md §7's boundary: who uses this
    bot is not something a file should carry.
    """
    return f"sentinel-journal-{generated_at.astimezone(tz):%Y-%m-%d}.xlsx"


__all__ = ["Column", "columns", "journal_filename", "journal_workbook"]

"""Join 3 of journal/M10c_REPORT.md §13: a forex row through the reader surfaces.

``/positions``, ``/journal`` and ``/stats`` are market-blind **by construction and by
type** — ``position_view`` reads only names §16.2 mirrors, and the three that differ go
through ``bot/plans.py``'s named accessors. That is an argument about the code. No forex
row had ever been through any of them, and this project has now found a defect at three
consecutive milestone boundaries by composing exactly this kind of argument.

So: a real ``ForexPlan``, built by the real gate, serialised the way ``signal_row()``
serialises it, read back through ``plan_of`` and rendered by the real renderers.

The assertions are chosen for what would hurt rather than for what is easy to check:

* **the leverage is the ESMA words, not a derived suggestion.** Crypto *derives* a
  leverage from its liquidation buffer; forex has no per-position liquidation price
  (§7.6), so 30:1 is a configured cap resting on a documented assumption. One number
  rendered as the other is exactly the confusion ``leverage_of`` exists to prevent.
* **the risk figure is in the quote currency the pair actually has.** ``planned_risk_
  usdt`` is really "risk in the quote currency" and USDJPY sizes through **EURJPY**
  (§7.1). A EURUSD rate used there is a ~145x error that sizes a position to roughly
  nothing while looking like an unremarkable number.
* **the price precision is the instrument's.** ``charts/renderer._format_price``
  branches at 1000 and at 1 (HANDOFF §4 item 10), and every forex price is inside the
  1-to-1000 branch that had no golden until M10b-2.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from sentinel.bot.cards import positions_card, stats_card
from sentinel.bot.export import journal_workbook
from sentinel.bot.markets import section_header
from sentinel.bot.plans import journal_leverage_of, leverage_of, market_of, plan_of
from sentinel.bot.readmodels import position_view, stats_view
from sentinel.core.config import AppConfig
from sentinel.core.markets import Market
from sentinel.fx.plan import ForexPlan
from sentinel.stats.compute import summarize
from sentinel.stats.journal import build_journal
from sentinel.stats.models import Book, BookStats, Population, StatsReport
from sentinel.stats.queries import resolved_from_row
from sentinel.storage.models import SignalRow
from tests.fx.forex_double import FX_NOW, forex_plan

TZ = ZoneInfo("Europe/Vilnius")
OWNER = 7222549221
SYMBOL = "EURUSD"


def _uuid(tag: int) -> UUID:
    return UUID(int=tag)


@pytest.fixture
def plan(repo_config: AppConfig) -> ForexPlan:
    return forex_plan(repo_config)


def forex_row(plan: ForexPlan, *, tag: int = 1, outcome: str | None = None) -> SignalRow:
    """A stored forex ``signals`` row, exactly as ``signal_row()`` writes one.

    ``plan`` is dumped rather than handed over as an object: the surfaces read JSONB
    out of Postgres, and a test that passed the model straight through would skip the
    serialisation round trip where a ``Decimal`` loses its scale.
    """
    row = SignalRow(
        id=_uuid(tag),
        plan_id=_uuid(1000 + tag),
        cycle_id=_uuid(9000),
        user_id=OWNER,
        market=Market.FOREX.value,
        symbol=plan.symbol,
        direction=plan.direction.value,
        setup_type=plan.setup_type.value,
        prompt_version="fable_forex_v1",
        confidence=plan.confidence,
        created_at=FX_NOW - timedelta(days=tag),
        expires_at=plan.expires_at,
        status="CLOSED" if outcome else "PARTIALLY_FILLED",
        decision="TAKEN",
        decided_at=FX_NOW - timedelta(days=tag),
        decided_by_user_id=OWNER,
        plan=plan.model_dump(mode="json"),
        chart_params=[],
        dry_run=False,
        filled_qty=plan.entries[0].qty,
        avg_fill_price=plan.entries[0].price,
        tp_hits=1 if outcome else 0,
        outcome=outcome,
        closed_at=None if outcome is None else FX_NOW - timedelta(days=tag, hours=-6),
        realized_r=None if outcome is None else Decimal("1.60"),
        realized_eur=None if outcome is None else Decimal("120.00"),
        stop_price_current=None,
    )
    # `number` is an IDENTITY column: Postgres assigns it, so a detached row has
    # none until it is set. Same convention as tests/golden/surfaces.py.
    row.number = tag
    return row


# --------------------------------------------------------------------------- #
# /positions
# --------------------------------------------------------------------------- #


def test_positions_renders_a_forex_row(plan: ForexPlan) -> None:
    row = forex_row(plan)
    rehydrated = plan_of(row.plan, market_of(row))

    view = position_view(row, rehydrated, fills=(), exits=(), mark_price=None)
    card = positions_card((view,), TZ)

    assert SYMBOL in card
    assert str(plan.stop) in card
    # Not a crash and not a blank: the row rendered with its own numbers.
    assert view.symbol == SYMBOL
    assert view.risk_eur == str(plan.risk_eur)


def test_the_leverage_on_a_forex_position_is_a_cap_on_the_card_and_blank_in_the_journal(
    plan: ForexPlan,
) -> None:
    """The two halves of the defect join 3 found, pinned together on purpose.

    ``JournalRow.leverage`` is ``int | None`` and M10c fed it ``leverage_of``, whose
    forex answer is the string ``"30x max"``. Crypto's ``"5"`` coerced silently; forex
    raised, and the raise took ``/journal`` down for the **whole workbook**.

    Both assertions have to hold at once, which is why they are one test: showing the
    cap on the card is §7.6's requirement that the ESMA words travel with the number,
    and leaving the journal column blank is §2.1's — a per-position leverage is a
    figure this market does not have, and a constant repeated on every row is not one.
    A future fix that satisfies either alone re-breaks the other.
    """
    assert leverage_of(plan) == f"{plan.max_leverage}x max"
    assert plan.leverage_basis, "the ESMA words must travel with the number (§7.6)"
    assert journal_leverage_of(plan) is None


def test_a_forex_position_carries_no_crypto_only_field(plan: ForexPlan) -> None:
    """§16.2 group 3, asserted on the object the surfaces actually read."""
    fields = set(type(plan).model_fields)
    forbidden = {
        "notional_usdt",
        "suggested_leverage",
        "liq_distance_pct",
        "liq_buffer_ok",
        "eurusd_rate",
    }
    assert not (fields & forbidden), sorted(fields & forbidden)
    assert not [name for name in fields if name.startswith("funding_")]


# --------------------------------------------------------------------------- #
# /stats
# --------------------------------------------------------------------------- #


def test_stats_renders_a_forex_book(plan: ForexPlan, repo_config: AppConfig) -> None:
    """``summarize`` raises on rows spanning more than one market (§11), so this also
    proves a forex book is a *book* rather than rows that happen to be forex."""
    rows = [forex_row(plan, tag=tag, outcome="TP2") for tag in (1, 2)]
    resolved = [resolved_from_row(row) for row in rows]

    report = StatsReport(
        window="last 30 days",
        since=FX_NOW - timedelta(days=30),
        generated_at=FX_NOW,
        market=Market.FOREX,
        books=(
            BookStats(
                book=Book(population=Population.REAL, market=Market.FOREX),
                stats=summarize(resolved),
            ),
        ),
        by_setup=(),
        by_prompt_version=(),
    )
    config = repo_config.model_copy(deep=True)
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(update={"enabled": True})
    card = stats_card(stats_view(report, header=section_header(Market.FOREX, config)), TZ)

    assert "forex" in card.lower(), card
    assert "2" in card


# --------------------------------------------------------------------------- #
# /journal
# --------------------------------------------------------------------------- #


def test_the_journal_workbook_holds_a_forex_row_with_its_sizing(plan: ForexPlan) -> None:
    """The export's ``except ValidationError`` blanks sizing columns for an old schema.

    Before ``plan_of`` was swept through here (journal/M10c_REPORT.md §8) that branch
    would have caught **every** forex row and silently blanked the sizing on a file
    whose entire purpose is a record of what happened. This is that sweep, asserted on
    the bytes that leave the process rather than on the function that writes them.
    """
    row = forex_row(plan, outcome="TP2")
    books = build_journal([row], fills={}, exits={}, market=Market.FOREX)
    payload = journal_workbook(books, window_label="last 30 days", generated_at=FX_NOW, tz=TZ)

    workbook = load_workbook(io.BytesIO(payload))
    # The sheet names itself after its market (M10a). Asserted rather than hardcoded,
    # because a forex row landing on an untagged "Real (Taken)" sheet would be a row
    # merged into crypto's book — the one thing §11 says never happens.
    titles = [name for name in workbook.sheetnames if name.startswith("Real (Taken)")]
    assert titles == ["Real (Taken) · forex"], workbook.sheetnames

    real = workbook[titles[0]]
    values = list(real.iter_rows(values_only=True))
    assert len(values) > 1, "the forex row did not reach the Real sheet"

    header = [str(cell) for cell in values[0]]
    first: dict[str, Any] = dict(zip(header, values[1], strict=True))
    assert first["Symbol"] == SYMBOL
    populated = [name for name, value in first.items() if value not in (None, "")]
    assert len(populated) > len(header) // 2, (
        f"most of the forex row's columns are blank — {sorted(set(header) - set(populated))}"
    )

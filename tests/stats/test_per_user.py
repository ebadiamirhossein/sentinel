"""``/stats`` is strictly per user — specs/TELEGRAM_UX.md §3 (M8.1).

PRD G2 exists to make one number trustworthy: "the system's real success rate is
*known*, not guessed". The REAL population is defined by ``decision == TAKEN``, and
under one shared analysis several people answer the same setup differently — so
without a user filter one person's Taken lands in another person's record, and the
number G2 is about stops being about anybody in particular.

The filter is on the **query**, not on the report: ``SignalRepository`` requires
``user_id`` as a keyword on every book-reading method, so forgetting it is a
``mypy --strict`` error rather than a privacy leak that passes its tests.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sentinel.bot.models import SignalDecision
from sentinel.core.markets import Market
from sentinel.stats.models import Population
from sentinel.stats.queries import StatsRepository, build_report
from sentinel.storage.repositories import SignalRepository
from tests.bot_double import FakeStore, _SignalRow

OWNER = 111
MEMBER = 222
NOW = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)


def resolved(
    store: FakeStore,
    user_id: int,
    *,
    decision: SignalDecision,
    realized_r: str,
    seed: int,
) -> None:
    """A closed signal in one user's book."""
    from uuid import UUID

    signal_id = UUID(int=seed)
    row = _SignalRow(signal_id, UUID(int=seed + 500), number=seed, user_id=user_id)
    row.decision = decision.value
    row.status = "CLOSED"
    row.closed_at = NOW
    row.created_at = NOW
    row.filled_qty = Decimal("1")
    row.realized_r = Decimal(realized_r)
    row.realized_eur = Decimal(realized_r)
    row.realized_costs_eur = Decimal("0")
    row.tp_hits = 1 if Decimal(realized_r) > 0 else 0
    row.outcome = "TP1" if Decimal(realized_r) > 0 else "STOP"
    row.setup_type = "trend_pullback"
    store.signals[signal_id] = row


class Session:
    """A session whose only job is to carry the store to the fake repository."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


async def report_for(store: FakeStore, user_id: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.bot_double import FakeSignalRepository

    monkeypatch.setattr(
        "sentinel.stats.queries.SignalRepository",
        # ``market`` is keyword-only on the real repository from M10a, and the fake
        # takes it too — so the double is substituted with the same signature rather
        # than one that silently ignores the scoping.
        lambda session, *, market=Market.CRYPTO: FakeSignalRepository(session, market=market),
    )
    return await build_report(
        Session(store),  # type: ignore[arg-type]
        window="all",
        now=NOW,
        user_id=user_id,
    )


async def test_the_real_population_never_contains_another_users_decision(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one thing per-user statistics have to guarantee.

    The owner watched a setup and the member took it. The owner's REAL book must
    contain nothing, and the member's must contain exactly their own trade.
    """
    resolved(store, OWNER, decision=SignalDecision.WATCHING, realized_r="2.00", seed=1)
    resolved(store, MEMBER, decision=SignalDecision.TAKEN, realized_r="-1.00", seed=2)

    owner_report = await report_for(store, OWNER, monkeypatch)
    member_report = await report_for(store, MEMBER, monkeypatch)

    assert owner_report.book(Population.REAL).count == 0, (
        "somebody else's Taken is not the owner's record"
    )
    assert owner_report.book(Population.HYPOTHETICAL).count == 1
    assert member_report.book(Population.REAL).count == 1
    assert member_report.book(Population.REAL).total_r == Decimal("-1.00")
    assert member_report.book(Population.HYPOTHETICAL).count == 0


async def test_two_users_answering_the_same_analysis_keep_separate_books(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shared report, two rows, two win rates — and neither is an average."""
    resolved(store, OWNER, decision=SignalDecision.TAKEN, realized_r="3.00", seed=1)
    resolved(store, MEMBER, decision=SignalDecision.TAKEN, realized_r="-1.00", seed=2)

    owner_report = await report_for(store, OWNER, monkeypatch)
    member_report = await report_for(store, MEMBER, monkeypatch)

    assert owner_report.book(Population.REAL).win_rate_pct == Decimal("100")
    assert member_report.book(Population.REAL).win_rate_pct == Decimal("0")


async def test_the_breakdowns_are_scoped_too(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By-setup and by-prompt-version run over the same rows, so a leak there would
    be the same leak wearing a different label."""
    resolved(store, MEMBER, decision=SignalDecision.TAKEN, realized_r="1.00", seed=1)
    resolved(store, MEMBER, decision=SignalDecision.TAKEN, realized_r="1.00", seed=2)

    owner_report = await report_for(store, OWNER, monkeypatch)

    assert owner_report.by_setup == ()
    assert owner_report.book(Population.REAL).count == 0


async def test_an_empty_book_reports_nothing_measured_rather_than_zero(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new member's first ``/stats``. A 0% win rate would read as "this never
    works"; "nothing measured yet" is the truth."""
    report = await report_for(store, MEMBER, monkeypatch)

    assert report.book(Population.REAL).count == 0
    assert report.book(Population.REAL).measured is False


def test_every_book_reading_query_requires_a_user() -> None:
    """The structural guarantee behind all of the above.

    If any of these took ``user_id`` as an *optional* keyword meaning "everybody",
    the promise would be something each caller has to remember. Required keywords
    make a forgotten filter a type error.
    """
    scoped = (
        "with_decision",
        "recent",
        "undecided_count",
        "open_symbols",
        "open_taken",
        "published_since",
        "resolutions_since",
        "resolved_since",
    )
    for name in scoped:
        signature = inspect.signature(getattr(SignalRepository, name))
        parameter = signature.parameters["user_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name}: user_id must be keyword"
        assert parameter.default is inspect.Parameter.empty, (
            f"{name}: user_id must be required — a default would mean 'everybody'"
        )


def test_the_stats_repository_takes_one_too() -> None:
    parameter = inspect.signature(StatsRepository.resolved).parameters["user_id"]
    assert parameter.default is inspect.Parameter.empty


def test_the_populations_are_still_the_three_m7_defined() -> None:
    """M8.1 partitions each book by user; it does not add a fourth population or
    merge two of the three."""
    assert {p.value for p in Population} == {"REAL", "HYPOTHETICAL", "DRY_RUN"}

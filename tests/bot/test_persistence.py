"""M6 round-trips against a real Postgres (opt-in).

The hermetic suite models the two unique constraints the idempotency guarantee
rests on. This file proves the *database* actually enforces them — a fake that
agrees with a wrong assumption is worse than no fake at all.

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:...@localhost:5432/sentinel \\
        .venv/bin/pytest tests/bot/test_persistence.py
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.bot.models import MessageKind, MessageStatus, SignalDecision, SignalRecord
from sentinel.risk.models import TradePlan
from sentinel.storage.models import (
    ConfigChangeRow,
    RuntimeSettingRow,
    SignalRow,
    TelegramMessageRow,
)
from sentinel.storage.repositories import (
    RuntimeSettingsRepository,
    SignalRepository,
    TelegramMessageRepository,
)
from tests.risk_double import PLAN_NOW, approved_plan

TEST_DB_URL = os.getenv("SENTINEL_TEST_DATABASE_URL")


def requires_db(func: Any) -> Any:
    func = pytest.mark.allow_socket(func)
    return pytest.mark.skipif(
        TEST_DB_URL is None,
        reason="set SENTINEL_TEST_DATABASE_URL to run DB round-trip tests",
    )(func)


async def _clean(session: AsyncSession) -> None:
    """Before *and* after: M5.1 §5 — these tests assert counts, and a dirty
    database is exactly what a developer running them is most likely to have."""
    for table in (TelegramMessageRow, SignalRow, ConfigChangeRow, RuntimeSettingRow):
        await session.execute(delete(table))
    await session.commit()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _clean(session)
        yield session
        await session.rollback()
        await _clean(session)
    await engine.dispose()


@pytest.fixture
def plan() -> TradePlan:
    return approved_plan()


@requires_db
async def test_a_signal_round_trips_with_its_whole_plan(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    claimed = await repo.claim(SignalRecord(plan=plan))
    assert claimed is not None
    await session.commit()

    row = await repo.get_by_plan_id(plan.plan_id)
    assert row is not None
    assert row.symbol == "SOLUSDT"
    assert row.number >= 1, "the IDENTITY column must assign a human-facing number"
    # Decimals land as strings inside JSONB, as everywhere else in this codebase.
    assert row.plan["risk_eur"] == "74.98"
    assert row.plan["schema_version"] == 3
    assert row.plan["target_distances_pct"] == ["3.0541", "4.7475", "7.5295"]
    assert row.plan["entries"][0]["distance_pct"] == "-0.3597"


@requires_db
async def test_the_plan_id_constraint_is_what_stops_a_double_post(
    session: AsyncSession, plan: TradePlan
) -> None:
    """specs/TELEGRAM_UX.md §6, enforced by Postgres rather than by a code path."""
    repo = SignalRepository(session)
    assert await repo.claim(SignalRecord(plan=plan)) is not None
    await session.commit()

    # A *different* SignalRecord for the same plan — a retry, or a restart.
    assert await repo.claim(SignalRecord(plan=plan)) is None
    await session.commit()

    assert len(await repo.recent()) == 1


@requires_db
async def test_signal_numbers_are_stable_and_increasing(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    first = await repo.claim(SignalRecord(plan=plan))
    second = await repo.claim(SignalRecord(plan=plan.model_copy(update={"plan_id": uuid4()})))
    await session.commit()
    assert first is not None and second is not None
    assert second.number > first.number


@requires_db
async def test_a_message_can_be_claimed_once_and_confirmed(
    session: AsyncSession, plan: TradePlan
) -> None:
    record = await SignalRepository(session).claim(SignalRecord(plan=plan))
    assert record is not None
    await session.commit()

    repo = TelegramMessageRepository(session)
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is True
    await session.commit()

    # The second claim is the restart case: it must fail, not raise.
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is False
    await session.commit()

    await repo.confirm(record.signal_id, MessageKind.CARD, 42, message_id=777, at=PLAN_NOW)
    await session.commit()

    posted = await repo.get(record.signal_id, MessageKind.CARD, 42)
    assert posted is not None
    assert posted.message_id == 777
    assert posted.status is MessageStatus.SENT
    assert await repo.stuck() == []


@requires_db
async def test_the_album_and_the_card_are_separate_claims(
    session: AsyncSession, plan: TradePlan
) -> None:
    """Same signal, same chat, different kind — both must be claimable."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan))
    assert record is not None
    await session.commit()

    repo = TelegramMessageRepository(session)
    assert await repo.claim(record.signal_id, MessageKind.CHARTS, 42, at=PLAN_NOW) is True
    assert await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW) is True
    await session.commit()


@requires_db
async def test_an_unconfirmed_claim_shows_up_as_stuck(
    session: AsyncSession, plan: TradePlan
) -> None:
    """The crash window is visible in /status rather than silent."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan))
    assert record is not None
    repo = TelegramMessageRepository(session)
    await repo.claim(record.signal_id, MessageKind.CARD, 42, at=PLAN_NOW)
    await session.commit()

    stuck = await repo.stuck()
    assert [row.status for row in stuck] == [MessageStatus.PENDING.value]


@requires_db
async def test_a_decision_is_persisted_and_idempotent(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    record = await repo.claim(SignalRecord(plan=plan))
    assert record is not None
    await session.commit()

    first = await repo.record_decision(
        record.signal_id, SignalDecision.TAKEN, at=PLAN_NOW, user_id=111
    )
    await session.commit()
    assert first is not None and first[1] is True

    again = await repo.record_decision(
        record.signal_id, SignalDecision.TAKEN, at=PLAN_NOW, user_id=111
    )
    assert again is not None and again[1] is False

    taken = await repo.with_decision(SignalDecision.TAKEN)
    assert [row.signal_id if hasattr(row, "signal_id") else row.id for row in taken] == [
        record.signal_id
    ]
    assert await repo.undecided_count() == 0


@requires_db
async def test_runtime_settings_upsert_and_leave_an_audit_trail(session: AsyncSession) -> None:
    """PRD F10 — config *changes*, not merely current values."""
    repo = RuntimeSettingsRepository(session)
    await repo.set("capital_eur", "10000", at=PLAN_NOW, user_id=111)
    await session.commit()
    await repo.set("capital_eur", "12000", at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC), user_id=111)
    await session.commit()

    assert await repo.get("capital_eur") == "12000"
    assert await repo.all() == {"capital_eur": "12000"}

    changes = await repo.changes()
    assert [(row.old_value, row.new_value) for row in changes] == [
        ("10000", "12000"),
        (None, "10000"),
    ]


@requires_db
async def test_a_watchlist_survives_the_round_trip_as_a_list(session: AsyncSession) -> None:
    repo = RuntimeSettingsRepository(session)
    await repo.set("watchlist", ["BTCUSDT", "SOLUSDT"], at=PLAN_NOW, user_id=111)
    await session.commit()
    assert await repo.get("watchlist") == ["BTCUSDT", "SOLUSDT"]


@requires_db
async def test_no_float_reaches_the_stored_plan(session: AsyncSession, plan: TradePlan) -> None:
    """CLAUDE.md — money math is Decimal, and JSONB is where floats sneak in."""
    record = await SignalRepository(session).claim(SignalRecord(plan=plan))
    assert record is not None
    await session.commit()
    row = await SignalRepository(session).get_by_plan_id(plan.plan_id)
    assert row is not None

    def walk(payload: object) -> None:
        if isinstance(payload, dict):
            for value in payload.values():
                walk(value)
        elif isinstance(payload, list):
            for value in payload:
                walk(value)
        else:
            assert not isinstance(payload, float), f"float in the stored plan: {payload!r}"

    walk(row.plan)
    assert Decimal(row.plan["notional_eur"]) == plan.notional_eur

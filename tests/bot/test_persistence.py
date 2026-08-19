"""M6 round-trips against a real Postgres (opt-in).

The hermetic suite models the two unique constraints the idempotency guarantee
rests on. This file proves the *database* actually enforces them — a fake that
agrees with a wrong assumption is worse than no fake at all.

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:...@localhost:5432/sentinel \\
        .venv/bin/pytest tests/bot/test_persistence.py
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.bot.models import (
    MessageKind,
    MessageStatus,
    SignalDecision,
    SignalRecord,
    UserRole,
    UserStatus,
)
from sentinel.risk.models import TradePlan
from sentinel.storage.models import (
    ConfigChangeRow,
    RuntimeSettingRow,
    SignalRow,
    TelegramMessageRow,
    UserRow,
)
from sentinel.storage.repositories import (
    RuntimeSettingsRepository,
    SignalRepository,
    TelegramMessageRepository,
    UserRepository,
)
from tests.bot_double import OWNER_ID
from tests.db_guard import TEST_DB_URL, requires_db
from tests.risk_double import PLAN_NOW, approved_plan


async def _clean(session: AsyncSession) -> None:
    """Before *and* after: M5.1 §5 — these tests assert counts, and a dirty
    database is exactly what a developer running them is most likely to have."""
    for table in (TelegramMessageRow, SignalRow, ConfigChangeRow, RuntimeSettingRow, UserRow):
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
    claimed = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    assert claimed is not None
    await session.commit()

    row = await repo.get_by_plan_id(plan.plan_id)
    assert row is not None
    assert row.symbol == "SOLUSDT"
    assert row.number >= 1, "the IDENTITY column must assign a human-facing number"
    # Decimals land as strings inside JSONB, as everywhere else in this codebase.
    assert row.plan["risk_eur"] == "74.98"
    assert row.plan["schema_version"] == 3
    assert row.plan["target_distances_pct"] == ["3.05", "4.75", "7.53"]
    assert row.plan["entries"][0]["distance_pct"] == "-0.36"


@requires_db
async def test_the_plan_id_constraint_is_what_stops_a_double_post(
    session: AsyncSession, plan: TradePlan
) -> None:
    """specs/TELEGRAM_UX.md §6, enforced by Postgres rather than by a code path."""
    repo = SignalRepository(session)
    assert await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID)) is not None
    await session.commit()

    # A *different* SignalRecord for the same plan — a retry, or a restart.
    assert await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID)) is None
    await session.commit()

    assert len(await repo.recent(user_id=OWNER_ID)) == 1


@requires_db
async def test_signal_numbers_are_stable_and_increasing(
    session: AsyncSession, plan: TradePlan
) -> None:
    repo = SignalRepository(session)
    first = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
    second = await repo.claim(
        SignalRecord(plan=plan.model_copy(update={"plan_id": uuid4()}), user_id=OWNER_ID)
    )
    await session.commit()
    assert first is not None and second is not None
    assert second.number > first.number


@requires_db
async def test_a_message_can_be_claimed_once_and_confirmed(
    session: AsyncSession, plan: TradePlan
) -> None:
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
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
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
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
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
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
    record = await repo.claim(SignalRecord(plan=plan, user_id=OWNER_ID))
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

    taken = await repo.with_decision(SignalDecision.TAKEN, user_id=OWNER_ID)
    assert [row.signal_id if hasattr(row, "signal_id") else row.id for row in taken] == [
        record.signal_id
    ]
    assert await repo.undecided_count(user_id=OWNER_ID) == 0


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
    record = await SignalRepository(session).claim(SignalRecord(plan=plan, user_id=OWNER_ID))
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


# --------------------------------------------------------------------------- #
# users — M8.1's table, against the real constraints
# --------------------------------------------------------------------------- #


@requires_db
async def test_a_second_start_from_the_same_id_writes_nothing(session: AsyncSession) -> None:
    """The anti-spam guarantee, against the real primary key rather than a fake one.

    ``request`` is ``ON CONFLICT DO NOTHING``: a stranger tapping /start twenty times
    produces one row and one owner notification, and a restart cannot lose the count
    because there is no count.
    """
    repo = UserRepository(session)
    first = await repo.request(4242, username="newcomer", display_name="New", at=PLAN_NOW)
    await session.commit()
    later = datetime(2026, 8, 18, 14, 0, tzinfo=UTC)
    second = await repo.request(4242, username="renamed", display_name="New", at=later)
    await session.commit()

    assert first is not None and first.status is UserStatus.PENDING
    assert second is None, "a re-request is a no-op, not an update"
    stored = await repo.get(4242)
    assert stored is not None
    assert stored.username == "newcomer", "the original row is untouched"
    assert stored.requested_at == PLAN_NOW


@requires_db
async def test_the_database_refuses_a_second_owner(session: AsyncSession) -> None:
    """``uq_users_single_owner`` — two owners would mean two people can admit users
    to somebody else's trading system, and the failure would be silent."""
    from sqlalchemy.exc import IntegrityError

    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await session.commit()

    session.add(
        UserRow(
            telegram_user_id=222,
            status=UserStatus.APPROVED.value,
            role=UserRole.OWNER.value,
            requested_at=PLAN_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@requires_db
async def test_seeding_an_owner_twice_never_overwrites_the_first(
    session: AsyncSession,
) -> None:
    """``ensure_owner`` runs on every boot. If it were an upsert it would re-approve
    an owner who suspended themselves and reset a capital they had changed."""
    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await repo.set_capital(111, Decimal("8000"), at=PLAN_NOW)
    await session.commit()

    again = await repo.ensure_owner(
        111, at=datetime(2026, 9, 1, tzinfo=UTC), acknowledged_version="v1"
    )
    await session.commit()

    assert again.capital_eur == Decimal("8000")
    assert again.requested_at == PLAN_NOW


@requires_db
async def test_the_sizing_inputs_leave_an_audit_trail(session: AsyncSession) -> None:
    """PRD F10 followed the value when it moved out of ``runtime_settings``.

    "Why was this signal sized against €8,000" must still have a timestamped answer,
    and the key is namespaced per user because ``config_changes`` is shared and a
    bare ``capital_eur`` would now be ambiguous.
    """
    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    await repo.set_capital(111, Decimal("8000"), at=PLAN_NOW)
    await session.commit()
    await repo.set_capital(111, Decimal("9000"), at=datetime(2026, 8, 18, 13, 0, tzinfo=UTC))
    await session.commit()

    changes = await RuntimeSettingsRepository(session).changes()
    assert [(row.key, row.new_value) for row in changes] == [
        ("user.111.capital_eur", "9000"),
        ("user.111.capital_eur", "8000"),
    ]
    # The previous value is read back from a Numeric(38,18) column, so it carries the
    # column's scale rather than the string that was typed. That is the honest
    # record: it is what the database held, not what a renderer would have shown.
    assert changes[0].old_value == "8000.000000000000000000"
    assert changes[1].old_value is None, "the first write has nothing to supersede"


@requires_db
async def test_a_users_pause_round_trips_as_a_pause_state(session: AsyncSession) -> None:
    """The per-user daily-loss pause reuses ``PauseState`` field for field, so one
    helper serves both it and ``risk_state``."""
    from sentinel.risk.models import PauseReason, PauseState

    repo = UserRepository(session)
    await repo.ensure_owner(111, at=PLAN_NOW, acknowledged_version="v1")
    until = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    await repo.set_pause(
        111,
        PauseState(paused=True, reason=PauseReason.DAILY_LOSS_LIMIT, until=until),
        at=PLAN_NOW,
    )
    await session.commit()

    stored = await repo.get(111)
    assert stored is not None
    assert stored.pause.paused is True
    assert stored.pause.reason is PauseReason.DAILY_LOSS_LIMIT
    assert stored.pause.until == until


@requires_db
async def test_two_users_signals_never_appear_in_each_others_books(
    session: AsyncSession, plan: TradePlan
) -> None:
    """The privacy guarantee, asserted against real SQL rather than a fake filter."""
    repo = SignalRepository(session)
    mine = await repo.claim(SignalRecord(plan=plan, user_id=111))
    theirs = await repo.claim(
        SignalRecord(plan=plan.model_copy(update={"plan_id": uuid4()}), user_id=222)
    )
    await session.commit()
    assert mine is not None and theirs is not None

    assert [row.id for row in await repo.recent(user_id=111)] == [mine.signal_id]
    assert [row.id for row in await repo.recent(user_id=222)] == [theirs.signal_id]
    assert await repo.undecided_count(user_id=111) == 1
    assert await repo.open_symbols(user_id=111) == {plan.symbol}

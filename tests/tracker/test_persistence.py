"""The tracker's tables against real Postgres.

The hermetic suite proves the *behaviour* against fakes that model the two unique
constraints. This proves the constraints themselves exist and behave that way,
which is the half a fake cannot assert about itself.

**Destructive** — see ``tests/db_guard.py``. It truncates what it counts, and
refuses to run against the app's own database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.bot.models import MessageKind, SignalRecord, SignalStatus
from sentinel.storage.repositories import (
    SignalEventRepository,
    SignalExitRepository,
    SignalFillRepository,
    SignalRepository,
    TelegramMessageRepository,
)
from tests.bot_double import OWNER_ID
from tests.db_guard import TEST_DB_URL, requires_db
from tests.risk_double import approved_plan

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

TABLES = ("signal_events", "signal_exits", "signal_fills", "telegram_messages", "signals")


async def _clean(session: AsyncSession) -> None:
    for table in TABLES:
        await session.execute(text(f"DELETE FROM {table}"))
    await session.commit()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _clean(session)
        try:
            yield session
        finally:
            await _clean(session)
    await engine.dispose()


async def _signal(session: AsyncSession, **overrides: object) -> SignalRecord:
    record = SignalRecord(plan=approved_plan(), user_id=OWNER_ID, **overrides)
    claimed = await SignalRepository(session).claim(record)
    assert claimed is not None
    await session.commit()
    return claimed


@requires_db
async def test_a_rung_can_only_fill_once(session: AsyncSession) -> None:
    """``UNIQUE (signal_id, rung_index)`` — the constraint a replayed tick relies on."""
    record = await _signal(session)
    fills = SignalFillRepository(session)

    first = await fills.record(
        record.signal_id,
        rung_index=0,
        price=Decimal("83.10"),
        qty=Decimal("18.30"),
        filled_at=NOW,
        detected_at=NOW,
    )
    second = await fills.record(
        record.signal_id,
        rung_index=0,
        price=Decimal("83.10"),
        qty=Decimal("18.30"),
        filled_at=NOW,
        detected_at=NOW,
    )
    await session.commit()

    assert (first, second) == (True, False)
    assert len(await fills.for_signal(record.signal_id)) == 1


@requires_db
async def test_an_exit_kind_can_only_happen_once(session: AsyncSession) -> None:
    record = await _signal(session)
    exits = SignalExitRepository(session)

    for _ in range(2):
        await exits.record(
            record.signal_id,
            kind="TP1",
            price=Decimal("85.20"),
            qty=Decimal("25.67"),
            exited_at=NOW,
            detected_at=NOW,
        )
    await session.commit()
    assert len(await exits.for_signal(record.signal_id)) == 1


@requires_db
async def test_an_event_is_journalled_once_and_found_unposted(
    session: AsyncSession,
) -> None:
    """The pair that makes a mid-tick crash safe: recorded once, posted once."""
    record = await _signal(session)
    events = SignalEventRepository(session)

    assert await events.record(
        record.signal_id, event_key="stop", kind="STOPPED", at=NOW, price=Decimal("81.20")
    )
    assert not await events.record(
        record.signal_id, event_key="stop", kind="STOPPED", at=NOW, price=Decimal("81.20")
    )
    await session.commit()

    pending = await events.unposted(chat_id=OWNER_ID, user_id=OWNER_ID)
    assert [row.event_key for row in pending] == ["stop"]

    messages = TelegramMessageRepository(session)
    await messages.claim(record.signal_id, MessageKind.UPDATE, OWNER_ID, at=NOW, event_key="stop")
    await messages.confirm(
        record.signal_id,
        MessageKind.UPDATE,
        OWNER_ID,
        message_id=1,
        at=NOW,
        event_key="stop",
    )
    await session.commit()

    assert await events.unposted(chat_id=OWNER_ID, user_id=OWNER_ID) == []


@requires_db
async def test_the_widened_message_key_lets_a_thread_carry_many_updates(
    session: AsyncSession,
) -> None:
    """The constraint M7 changed. With ``kind`` alone, a stop-out after a fill
    would have been silently un-postable."""
    record = await _signal(session)
    messages = TelegramMessageRepository(session)

    for event_key in ("", "fill:0", "fill:1", "stop"):
        claimed = await messages.claim(
            record.signal_id,
            MessageKind.CARD if event_key == "" else MessageKind.UPDATE,
            4242,
            at=NOW,
            event_key=event_key,
        )
        assert claimed, f"{event_key or 'the card'} must be claimable"
    await session.commit()

    # And the card's own guarantee is untouched: its key is still unique.
    assert not await messages.claim(record.signal_id, MessageKind.CARD, 4242, at=NOW)


@requires_db
async def test_the_tracker_columns_round_trip(session: AsyncSession) -> None:
    record = await _signal(session)
    signals = SignalRepository(session)

    await signals.advance(
        record.signal_id,
        status=SignalStatus.STOPPED.value,
        filled_qty=Decimal("18.30"),
        avg_fill_price=Decimal("83.10"),
        realized_r=Decimal("-0.40"),
        realized_eur=Decimal("-29.99"),
        realized_costs_eur=Decimal("0.90"),
        outcome="STOP",
        closed_at=NOW,
        last_checked_at=NOW,
    )
    await session.commit()

    row = await signals.get(record.signal_id)
    assert row is not None
    assert row.status == SignalStatus.STOPPED.value
    assert row.realized_r == Decimal("-0.40")
    assert row.outcome == "STOP"
    assert row.dry_run is False


@requires_db
async def test_open_signals_excludes_the_resolved_ones(session: AsyncSession) -> None:
    """The tracker's own query, and the whole of its crash recovery."""
    live = await _signal(session)
    done = await _signal(session)
    signals = SignalRepository(session)
    await signals.advance(done.signal_id, status=SignalStatus.STOPPED.value, closed_at=NOW)
    await session.commit()

    open_rows = await signals.open_signals()
    assert [row.id for row in open_rows] == [live.signal_id]


@requires_db
async def test_a_stop_out_arms_a_cooldown_and_a_target_does_not(
    session: AsyncSession,
) -> None:
    """The owner ruling behind ``resolutions_since``: re-entering the same failing
    idea on the next cycle is what the rail is for; a winner is not a reason to
    stay away from the symbol."""
    stopped = await _signal(session)
    won = await _signal(session)
    signals = SignalRepository(session)
    await signals.advance(stopped.signal_id, status=SignalStatus.STOPPED.value, closed_at=NOW)
    await signals.advance(
        won.signal_id, status=SignalStatus.CLOSED.value, outcome="TP3", closed_at=NOW
    )
    await session.commit()

    resolutions = await signals.resolutions_since(NOW - timedelta(hours=4), user_id=OWNER_ID)
    assert [symbol for symbol, _ in resolutions] == ["SOLUSDT"]
    assert len(resolutions) == 1, "only the stop-out arms a cooldown"

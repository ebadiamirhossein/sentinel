"""The Persian summary cache through the **real** repository into a **real** table.

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://…/sentinel_test \\
        .venv/bin/pytest tests/storage/test_persian_summary_persistence.py

Opt-in like every DB test here. Three of the four claims below cannot be made against a
double at all, which is the whole reason this file exists rather than trusting
``FakePersianSummaryRepository``:

1. **``ON CONFLICT DO NOTHING`` really returns the winner's text.** The fake models the
   unique index with a ``dict.setdefault``; only Postgres runs the actual index, and the
   ``RETURNING``-is-empty-on-conflict path is the one branch a dict cannot exercise.
2. **The cost round-trips through ``Numeric(18, 8)`` at its stored scale.** HANDOFF §4
   item 12: an in-memory fixture is structurally blind to a scale change, because it
   cannot produce the value that shows one.
3. **The daily-cap queries actually filter.** ``generations_today`` and
   ``spend_today_usd`` are the two rails that stand between a bored thumb and forex's
   $3 of slack under the global ceiling, and both are SQL.
4. **The table is genuinely detached.** Deleting every row here leaves ``signals``
   untouched — no foreign key, no cascade, nothing to notice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.analyst.persian.models import PersianSourceKind, PersianSummary
from sentinel.core.markets import Market
from sentinel.storage.models import PersianSummaryRow, SignalRow
from sentinel.storage.repositories import PersianSummaryRepository
from tests.db_guard import TEST_DB_URL, requires_db

OWNER = 7222549221
MEMBER = 1958877587
DAY_START = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
NOW = DAY_START + timedelta(hours=12)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await _clean(db)
        yield db
        await db.rollback()
        await _clean(db)
    await engine.dispose()


async def _clean(session: AsyncSession) -> None:
    await session.execute(delete(PersianSummaryRow))
    await session.commit()


def summary(**overrides: object) -> PersianSummary:
    payload: dict[str, object] = {
        "input_sha256": "a" * 64,
        "source_kind": PersianSourceKind.SIGNAL,
        "signal_id": uuid4(),
        "market": Market.CRYPTO,
        "symbol": "SOLUSDT",
        "summary_text": "❌ الان نخر — فقط تماشا کن",
        "input_text": "🛑 <b>Stop: 81.20</b>",
        "prompt_version": "persian_summary_v1",
        "model": "claude-sonnet-4-6",
        "tokens_in": 900,
        "tokens_out": 210,
        # Eight decimal places, which is exactly the column's scale: the point is to
        # write a value that would show a scale change if one happened.
        "cost_usd_estimate": Decimal("0.00585000"),
        "llm_call_id": uuid4(),
        "created_at": NOW,
        "created_by_user_id": OWNER,
    }
    payload |= overrides
    return PersianSummary.model_validate(payload)


@requires_db
async def test_a_summary_round_trips(session: AsyncSession) -> None:
    repo = PersianSummaryRepository(session)
    stored = await repo.store(summary())
    await session.commit()

    found = await repo.find("a" * 64)
    assert found is not None
    assert found.summary_text == stored.summary_text
    assert found.source_kind is PersianSourceKind.SIGNAL
    assert found.market is Market.CRYPTO
    assert found.analyst_report_id is None


@requires_db
async def test_the_cost_keeps_the_scale_the_column_stores(session: AsyncSession) -> None:
    """HANDOFF §4 item 12, at the one seam this milestone adds.

    ``str`` and not ``==``: ``Decimal("0.00585") == Decimal("0.00585000")`` is true, and
    that equality is precisely what could not see the ``capital €200.000000000000000000``
    defect for 54 cycles.
    """
    repo = PersianSummaryRepository(session)
    await repo.store(summary())
    await session.commit()

    found = await repo.find("a" * 64)
    assert found is not None
    assert str(found.cost_usd_estimate) == "0.00585000"


@requires_db
async def test_a_second_store_returns_the_first_winners_text(session: AsyncSession) -> None:
    """The race the in-flight coalescer cannot see: two processes, one card.

    Only Postgres runs the unique index, so only Postgres exercises the branch where
    ``RETURNING`` comes back empty and the row has to be re-read. Both readers must end
    up with the SAME Persian words — a card that read differently to two people would
    defeat the entire reason the text is stored at all.
    """
    repo = PersianSummaryRepository(session)
    first = await repo.store(summary(summary_text="اولی"))
    await session.commit()
    second = await repo.store(
        summary(summary_text="دومی", created_by_user_id=MEMBER, signal_id=uuid4())
    )
    await session.commit()

    assert second.summary_text == first.summary_text == "اولی"
    assert second.created_by_user_id == OWNER, "the winner's row is what everybody reads"
    count = (
        await session.execute(select(func.count()).select_from(PersianSummaryRow))
    ).scalar_one()
    assert count == 1


@requires_db
async def test_the_per_user_cap_counts_only_that_user_and_only_today(
    session: AsyncSession,
) -> None:
    repo = PersianSummaryRepository(session)
    await repo.store(summary(input_sha256="b" * 64))
    await repo.store(summary(input_sha256="c" * 64, created_by_user_id=MEMBER))
    await repo.store(summary(input_sha256="d" * 64, created_at=DAY_START - timedelta(hours=1)))
    await session.commit()

    assert await repo.generations_today(user_id=OWNER, day_start=DAY_START) == 1
    assert await repo.generations_today(user_id=MEMBER, day_start=DAY_START) == 1


@requires_db
async def test_the_daily_usd_cap_sums_every_user(session: AsyncSession) -> None:
    """Deployment-wide, deliberately: the ceiling it protects is deployment-wide too."""
    repo = PersianSummaryRepository(session)
    await repo.store(summary(input_sha256="e" * 64))
    await repo.store(summary(input_sha256="f" * 64, created_by_user_id=MEMBER))
    await repo.store(summary(input_sha256="0" * 64, created_at=DAY_START - timedelta(days=1)))
    await session.commit()

    assert await repo.spend_today_usd(day_start=DAY_START) == Decimal("0.01170000")


@requires_db
async def test_deleting_every_row_leaves_the_signals_table_alone(session: AsyncSession) -> None:
    """§F's actual requirement, asserted rather than argued.

    ``signal_id`` is an indexed, **unconstrained** UUID: no foreign key, so no
    constraint trigger on ``signals`` and no direction in which this table can make a
    signal undeletable. Emptying it is a no-op for everything else in the schema.
    """
    repo = PersianSummaryRepository(session)
    before = (await session.execute(select(func.count()).select_from(SignalRow))).scalar_one()
    await repo.store(summary())
    await session.commit()

    await session.execute(delete(PersianSummaryRow))
    await session.commit()

    after = (await session.execute(select(func.count()).select_from(SignalRow))).scalar_one()
    assert after == before
    assert await repo.find("a" * 64) is None

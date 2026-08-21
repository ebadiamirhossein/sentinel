"""Postgres round-trip for M10b's two new tables (opt-in, like every DB test here).

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://…/sentinel_test \\
        .venv/bin/pytest tests/ingestion/test_forex_persistence.py

The forex candle test is the one worth reading. Migration 0011 makes
``ohlcv_candles.volume`` nullable so that forex — which has no volume of any kind —
can say so instead of writing a zero, and the danger of that permission is that it
also lets a *crypto* candle arrive with no volume and silently disable relative
volume. The invariant is enforced in code in both directions; this proves the column
actually accepts the null it was widened for.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.core.markets import Market
from sentinel.fx.instruments import ForexInstrument
from sentinel.fx.models import SaxoTokenBundle
from sentinel.ingestion.models import Candle, MarketSnapshot, OHLCVSeries
from sentinel.storage.models import ForexInstrumentRow, OhlcvCandleRow, SaxoTokenRow
from sentinel.storage.repositories import (
    ForexInstrumentRepository,
    SaxoTokenRepository,
    SnapshotRepository,
)
from tests.db_guard import TEST_DB_URL, requires_db

NOW = datetime(2026, 8, 21, 7, 0, tzinfo=UTC)


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
    for table in (SaxoTokenRow, ForexInstrumentRow):
        await session.execute(delete(table))
    await session.execute(delete(OhlcvCandleRow).where(OhlcvCandleRow.market == Market.FOREX.value))
    await session.commit()


def eurusd() -> ForexInstrument:
    return ForexInstrument(
        symbol="EURUSD",
        uic=21,
        decimals=4,
        pip=Decimal("0.0001"),
        tick_size=Decimal("0.00001"),
        min_trade_size=Decimal("1000"),
        amount_decimals=2,
        base_currency="EUR",
        quote_currency="USD",
        resolved_at=NOW,
    )


@requires_db
async def test_a_resolved_instrument_round_trips_with_its_pip_intact(
    session: AsyncSession,
) -> None:
    repo = ForexInstrumentRepository(session)
    await repo.upsert(eurusd())
    await session.commit()

    stored = await repo.get("EURUSD")
    assert stored is not None
    assert (stored.uic, stored.decimals, stored.pip) == (21, 4, Decimal("0.0001"))
    # Re-validated on the way out: failure mode A does not stop being dangerous once
    # the value is in a database.
    assert stored.pip == stored.tick_size * 10


@requires_db
async def test_re_resolving_an_instrument_updates_rather_than_duplicates(
    session: AsyncSession,
) -> None:
    repo = ForexInstrumentRepository(session)
    await repo.upsert(eurusd())
    await repo.upsert(eurusd().model_copy(update={"resolved_at": NOW + timedelta(days=1)}))
    await session.commit()

    rows = (await session.execute(select(ForexInstrumentRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].resolved_at == NOW + timedelta(days=1)


@requires_db
async def test_the_token_bundle_round_trips_including_an_unknown_expiry(
    session: AsyncSession,
) -> None:
    """A bootstrap token has no lifetime anybody told us. ``NULL`` is that fact, and
    it has to survive the database rather than becoming an invented hour."""
    repo = SaxoTokenRepository(session)
    bundle = SaxoTokenBundle(refresh_token=SecretStr("seed-token"), obtained_at=NOW)
    await repo.save(bundle)
    await session.commit()

    stored = await repo.load()
    assert stored is not None
    assert stored.refresh_token_value == "seed-token"
    assert stored.refresh_expires_at is None
    assert stored.access_token is None
    assert stored.refresh_count == 0


@requires_db
async def test_a_refresh_overwrites_the_single_row_rather_than_adding_one(
    session: AsyncSession,
) -> None:
    """The refresh token is single-use, so a second row is not a stale answer — it
    is a spent one, and there would be no way to tell which is which."""
    repo = SaxoTokenRepository(session)
    await repo.save(SaxoTokenBundle(refresh_token=SecretStr("first"), obtained_at=NOW))
    await repo.save(
        SaxoTokenBundle(
            refresh_token=SecretStr("second"),
            access_token=SecretStr("access"),
            obtained_at=NOW + timedelta(minutes=5),
            access_expires_at=NOW + timedelta(minutes=25),
            refresh_expires_at=NOW + timedelta(minutes=65),
            refresh_count=1,
        )
    )
    await session.commit()

    rows = (await session.execute(select(SaxoTokenRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == 1

    stored = await repo.load()
    assert stored is not None
    assert stored.refresh_token_value == "second"
    assert stored.refresh_count == 1


@requires_db
async def test_a_forex_candle_persists_with_a_null_volume(session: AsyncSession) -> None:
    """What migration 0011 exists for. Absent, not zero (specs/FOREX.md §2.1)."""
    snapshot = MarketSnapshot(
        symbol="EURUSD",
        captured_at=NOW,
        last_price=Decimal("1.1692"),
        ohlcv={
            "1h": OHLCVSeries(
                source="saxo_fxspot",
                fetched_at=NOW,
                symbol="EURUSD",
                timeframe="1h",
                market=Market.FOREX,
                candles=tuple(
                    Candle(
                        open_time=NOW - timedelta(hours=index + 1),
                        open=Decimal("1.1690"),
                        high=Decimal("1.1695"),
                        low=Decimal("1.1685"),
                        close=Decimal("1.1692"),
                        volume=None,
                    )
                    for index in range(3)
                ),
            )
        },
    )
    await SnapshotRepository(session, market=Market.FOREX).save(snapshot)
    await session.commit()

    rows = (
        (
            await session.execute(
                select(OhlcvCandleRow).where(OhlcvCandleRow.market == Market.FOREX.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert all(row.volume is None for row in rows)

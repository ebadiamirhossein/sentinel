"""Snapshot serialization (always) and Postgres round-trip (opt-in).

The round-trip tests need a real Postgres because the schema uses JSONB and
``ON CONFLICT``. They are skipped unless ``SENTINEL_TEST_DATABASE_URL`` is set,
so ``make test`` stays hermetic:

    SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:change-me@localhost:5432/sentinel \\
        .venv/bin/pytest tests/ingestion/test_persistence.py
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sentinel.ingestion.models import (
    BookSnapshot,
    Candle,
    DataQuality,
    DerivContext,
    FxRate,
    InstrumentMeta,
    MarketSnapshot,
    OHLCVSeries,
    SentimentContext,
)
from sentinel.storage.models import FxRateRow, MarketSnapshotRow, OhlcvCandleRow
from sentinel.storage.repositories import (
    FxRateRepository,
    InstrumentMetaRepository,
    SnapshotRepository,
    candle_rows,
    snapshot_context,
    snapshot_sources,
)
from tests.db_guard import TEST_DB_URL, requires_db

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def candle(minute: int) -> Candle:
    return Candle(
        open_time=datetime(2026, 8, 18, 11, minute, tzinfo=UTC),
        open=Decimal("100.5"),
        high=Decimal("110.25"),
        low=Decimal("99.75"),
        close=Decimal("105.125"),
        volume=Decimal("1234.5"),
    )


def make_snapshot(symbol: str = "BTCUSDT", *, degraded: bool = False) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        captured_at=NOW,
        last_price=Decimal("105.125"),
        ohlcv={
            "15m": OHLCVSeries(
                source="binance_usdm",
                fetched_at=NOW,
                symbol=symbol,
                timeframe="15m",
                candles=(candle(0), candle(15), candle(30)),
            ),
            "1h": OHLCVSeries(
                source="binance_usdm",
                fetched_at=NOW,
                symbol=symbol,
                timeframe="1h",
                candles=(candle(0),),
            ),
        },
        instrument=InstrumentMeta(
            source="binance_usdm",
            fetched_at=NOW,
            symbol=symbol,
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_notional=Decimal("50"),
        ),
        derivatives=DerivContext(
            source="binance_usdm",
            fetched_at=NOW,
            funding_rate=Decimal("0.00005084"),
            open_interest_base=Decimal("106182.123"),
            long_short_ratio=Decimal("1.6392"),
        ),
        orderbook=BookSnapshot(
            source="binance_usdm",
            fetched_at=NOW,
            depth_levels=50,
            best_bid=Decimal("105"),
            best_ask=Decimal("105.2"),
            spread_pct=Decimal("0.19"),
            bid_notional=Decimal("500000"),
            ask_notional=Decimal("400000"),
            imbalance=Decimal("0.111111"),
        ),
        sentiment=SentimentContext(
            source="alternative.me", fetched_at=NOW, value=41, classification="Fear"
        ),
        fx=FxRate(source="frankfurter", fetched_at=NOW, rate=Decimal("1.1593")),
        data_quality=DataQuality.DEGRADED if degraded else DataQuality.OK,
        degraded_fields=("news",) if degraded else (),
    )


# ── pure serialization (no database) ─────────────────────────────────────────


def test_context_holds_every_non_candle_part() -> None:
    context = snapshot_context(make_snapshot())

    assert context["derivatives"]["funding_rate"] == "0.00005084"
    assert context["orderbook"]["imbalance"] == "0.111111"
    assert context["instrument"]["min_notional"] == "50"
    assert context["sentiment"]["value"] == 41
    assert context["fx"]["rate"] == "1.1593"
    assert context["features"] is None
    assert "ohlcv" not in context  # candles live in their own table


def test_sources_record_provenance_for_every_part() -> None:
    sources = snapshot_sources(make_snapshot())

    assert sources["ohlcv_15m"] == {
        "source": "binance_usdm",
        "fetched_at": NOW.isoformat(),
        "candles": 3,
    }
    assert sources["derivatives"]["source"] == "binance_usdm"
    assert sources["sentiment"]["source"] == "alternative.me"
    assert sources["fx"]["source"] == "frankfurter"


def test_candle_rows_flatten_every_timeframe() -> None:
    rows = candle_rows(make_snapshot())

    assert len(rows) == 4  # 3 x 15m + 1 x 1h
    assert {row["timeframe"] for row in rows} == {"15m", "1h"}
    assert all(row["symbol"] == "BTCUSDT" for row in rows)
    assert all(isinstance(row["close"], Decimal) for row in rows)


def test_context_is_json_serializable() -> None:
    import json

    payload = snapshot_context(make_snapshot(degraded=True))
    assert json.loads(json.dumps(payload))["derivatives"]["funding_rate"] == "0.00005084"


# ── Postgres round-trip (opt-in) ─────────────────────────────────────────────


async def _clean(session: AsyncSession) -> None:
    """Empty the tables these tests count rows in.

    Child-first, so the candle rows go before the snapshots they reference.
    """
    for table in (OhlcvCandleRow, MarketSnapshotRow, FxRateRow):
        await session.execute(delete(table))
    await session.commit()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A session against a database these tests have emptied **first**.

    Cleaning only on the way out was not enough (M4_REPORT §8): these tests assert
    absolute row counts (``len(candles) == 4``), so anything already in the
    database fails them — and the M1/M3 demos leave ~1,068 candles behind on the
    very database the developer running these tests is most likely to have. A
    fixture that only tidies up after itself is correct exactly once, on a database
    nobody has used. Cleaning at setup as well makes the tests idempotent and
    re-runnable, which is what a row-count assertion needs to mean anything.
    """
    assert TEST_DB_URL is not None
    engine = create_async_engine(TEST_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _clean(session)
        yield session
        await session.rollback()
        await _clean(session)
    await engine.dispose()


@requires_db
async def test_snapshot_round_trip(session: AsyncSession) -> None:
    snapshot = make_snapshot(degraded=True)

    snapshot_id = await SnapshotRepository(session).save(snapshot)
    await session.commit()

    row = await session.get(MarketSnapshotRow, snapshot_id)
    assert row is not None
    assert row.symbol == "BTCUSDT"
    assert row.data_quality == "DEGRADED"
    assert row.degraded_fields == ["news"]
    assert row.last_price == Decimal("105.125")
    assert row.context["derivatives"]["funding_rate"] == "0.00005084"
    assert row.sources["ohlcv_15m"]["candles"] == 3

    candles = (await session.execute(select(OhlcvCandleRow))).scalars().all()
    assert len(candles) == 4
    assert candles[0].close == Decimal("105.125")


@requires_db
async def test_candle_upsert_is_idempotent(session: AsyncSession) -> None:
    """Re-running a cycle must not duplicate or lose candles."""
    repo = SnapshotRepository(session)

    await repo.save(make_snapshot())
    await session.commit()
    await repo.save(make_snapshot())
    await session.commit()

    candles = (await session.execute(select(OhlcvCandleRow))).scalars().all()
    assert len(candles) == 4

    snapshots = (await session.execute(select(MarketSnapshotRow))).scalars().all()
    assert len(snapshots) == 2  # each cycle keeps its own audit row


@requires_db
async def test_fx_last_known_good_survives_a_restart(session: AsyncSession) -> None:
    repo = FxRateRepository(session)
    await repo.upsert(FxRate(source="frankfurter", fetched_at=NOW, rate=Decimal("1.1593")))
    await session.commit()

    stored = await repo.get("EURUSD")

    assert stored is not None
    assert stored.rate == Decimal("1.1593")
    assert stored.is_last_known_good is True


@requires_db
async def test_instrument_meta_upsert_refreshes_the_cache(session: AsyncSession) -> None:
    repo = InstrumentMetaRepository(session)
    meta = InstrumentMeta(
        source="binance_usdm",
        fetched_at=NOW,
        symbol="BTCUSDT",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_notional=Decimal("50"),
    )
    await repo.upsert(meta)
    await repo.upsert(meta.model_copy(update={"min_notional": Decimal("100")}))
    await session.commit()

    stored = await repo.get("BTCUSDT")

    assert stored is not None
    assert stored.min_notional == Decimal("100")

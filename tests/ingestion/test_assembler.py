"""Assembler: per-symbol isolation, cycle-wide context reuse, explicit degradation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import AppConfig
from sentinel.ingestion.assembler import SnapshotAssembler
from sentinel.ingestion.errors import CoreDataMissing, SourceUnavailable
from sentinel.ingestion.models import (
    BookSnapshot,
    Candle,
    DataQuality,
    DerivContext,
    FxRate,
    GlobalContext,
    InstrumentMeta,
    MacroContext,
    MarketHours,
    NewsContext,
    OHLCVSeries,
    SentimentContext,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def config() -> AppConfig:
    """Four timeframes, short per-symbol timeout to keep the tests fast."""
    return AppConfig.model_validate(
        {
            "schedule": {"symbol_timeout_seconds": 1},
            "market_data": {
                "timeframes": [
                    {"timeframe": "15m", "candles": 200},
                    {"timeframe": "1h", "candles": 200},
                    {"timeframe": "4h", "candles": 200},
                    {"timeframe": "1d", "candles": 100},
                ]
            },
        }
    )


class FakeAdapter:
    """Adapter double with per-symbol behaviour knobs."""

    def __init__(
        self,
        *,
        ohlcv_error: dict[str, Exception] | None = None,
        deriv_error: dict[str, Exception] | None = None,
        book_error: dict[str, Exception] | None = None,
        delay: dict[str, float] | None = None,
    ) -> None:
        self.ohlcv_error = ohlcv_error or {}
        self.deriv_error = deriv_error or {}
        self.book_error = book_error or {}
        self.delay = delay or {}
        self.ohlcv_calls: list[tuple[str, str, int]] = []
        self.closed = False

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        self.ohlcv_calls.append((symbol, timeframe, limit))
        if symbol in self.delay:
            await asyncio.sleep(self.delay[symbol])
        if symbol in self.ohlcv_error:
            raise self.ohlcv_error[symbol]
        return OHLCVSeries(
            source="fake",
            fetched_at=NOW,
            symbol=symbol,
            timeframe=timeframe,
            candles=(
                Candle(
                    open_time=NOW,
                    open=Decimal("100"),
                    high=Decimal("110"),
                    low=Decimal("95"),
                    close=Decimal("105"),
                    volume=Decimal("1000"),
                ),
            ),
        )

    async def derivatives_context(self, symbol: str) -> DerivContext | None:
        if symbol in self.deriv_error:
            raise self.deriv_error[symbol]
        return DerivContext(
            source="fake",
            fetched_at=NOW,
            funding_rate=Decimal("0.0001"),
            open_interest_base=Decimal("1000"),
            long_short_ratio=Decimal("1.4"),
        )

    async def orderbook_snapshot(self, symbol: str) -> BookSnapshot | None:
        if symbol in self.book_error:
            raise self.book_error[symbol]
        return BookSnapshot(
            source="fake",
            fetched_at=NOW,
            depth_levels=50,
            best_bid=Decimal("104"),
            best_ask=Decimal("106"),
            spread_pct=Decimal("1.9"),
            bid_notional=Decimal("600"),
            ask_notional=Decimal("400"),
            imbalance=Decimal("0.2"),
        )

    async def instrument_meta(self, symbol: str) -> InstrumentMeta:
        return InstrumentMeta(
            source="fake",
            fetched_at=NOW,
            symbol=symbol,
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_notional=Decimal("50"),
        )

    def market_hours(self) -> MarketHours:
        return MarketHours()

    async def close(self) -> None:
        self.closed = True


class CountingClient:
    """Global-context client that records how often it was called."""

    def __init__(self, value: Any, *, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.calls = 0

    async def fetch(self, *args: Any) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def global_clients() -> dict[str, CountingClient]:
    return {
        "news": CountingClient(
            NewsContext(source="cryptopanic", fetched_at=NOW, provider="cryptopanic")
        ),
        "sentiment": CountingClient(
            SentimentContext(
                source="alternative.me", fetched_at=NOW, value=41, classification="Fear"
            )
        ),
        "macro": CountingClient(
            MacroContext(
                source="coingecko",
                fetched_at=NOW,
                btc_dominance_pct=Decimal("56"),
                total_mcap_change_24h_pct=Decimal("0.6"),
            )
        ),
        "fx": CountingClient(FxRate(source="frankfurter", fetched_at=NOW, rate=Decimal("1.15"))),
    }


def make_assembler(
    config: AppConfig, adapter: FakeAdapter, clients: dict[str, CountingClient] | None = None
) -> tuple[SnapshotAssembler, dict[str, CountingClient]]:
    clients = clients if clients is not None else global_clients()
    assembler = SnapshotAssembler(
        adapter,  # structurally satisfies MarketDataAdapter
        config,
        news=clients["news"],  # type: ignore[arg-type]
        sentiment=clients["sentiment"],  # type: ignore[arg-type]
        macro=clients["macro"],  # type: ignore[arg-type]
        fx=clients["fx"],  # type: ignore[arg-type]
        clock=FrozenClock(NOW),
    )
    return assembler, clients


# ── happy path ───────────────────────────────────────────────────────────────


async def test_assemble_builds_a_complete_snapshot(config: AppConfig) -> None:
    assembler, _ = make_assembler(config, FakeAdapter())

    snapshot = await assembler.assemble("BTCUSDT")

    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.captured_at == NOW
    assert set(snapshot.ohlcv) == {"15m", "1h", "4h", "1d"}
    assert snapshot.last_price == Decimal("105")
    assert snapshot.data_quality is DataQuality.OK
    assert snapshot.degraded_fields == ()
    assert snapshot.derivatives is not None
    assert snapshot.instrument is not None
    assert snapshot.features is None  # the feature engine lands in M2


async def test_configured_tail_lengths_are_requested(config: AppConfig) -> None:
    adapter = FakeAdapter()
    assembler, _ = make_assembler(config, adapter)

    await assembler.assemble("BTCUSDT")

    assert sorted(adapter.ohlcv_calls) == sorted(
        [
            ("BTCUSDT", "15m", 200),
            ("BTCUSDT", "1h", 200),
            ("BTCUSDT", "4h", 200),
            ("BTCUSDT", "1d", 100),
        ]
    )


async def test_cycle_id_is_carried_onto_the_snapshot(config: AppConfig) -> None:
    assembler, _ = make_assembler(config, FakeAdapter())
    cycle_id = uuid4()

    snapshot = await assembler.assemble("BTCUSDT", cycle_id=cycle_id)

    assert snapshot.cycle_id == cycle_id


# ── degradation ──────────────────────────────────────────────────────────────


async def test_failed_secondary_source_degrades_the_snapshot(config: AppConfig) -> None:
    adapter = FakeAdapter(book_error={"BTCUSDT": SourceUnavailable("binance_usdm", "boom")})
    assembler, _ = make_assembler(config, adapter)

    snapshot = await assembler.assemble("BTCUSDT")

    assert snapshot.data_quality is DataQuality.DEGRADED
    assert snapshot.degraded_fields == ("orderbook",)
    assert snapshot.orderbook is None  # named as missing, never fabricated


async def test_failed_global_source_degrades_every_symbol(config: AppConfig) -> None:
    clients = global_clients()
    clients["sentiment"] = CountingClient(None, error=SourceUnavailable("alternative.me", "503"))
    assembler, _ = make_assembler(config, FakeAdapter(), clients)

    snapshots = await assembler.assemble_many(["BTCUSDT", "ETHUSDT"])

    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot.data_quality is DataQuality.DEGRADED
        assert "fear_greed" in snapshot.degraded_fields


async def test_missing_core_ohlcv_raises_core_data_missing(config: AppConfig) -> None:
    adapter = FakeAdapter(ohlcv_error={"BTCUSDT": SourceUnavailable("binance_usdm", "down")})
    assembler, _ = make_assembler(config, adapter)

    with pytest.raises(CoreDataMissing):
        await assembler.assemble("BTCUSDT")


async def test_stale_core_ohlcv_skips_the_symbol(config: AppConfig) -> None:
    """A snapshot is never emitted from stale core data (§4)."""

    class StaleAdapter(FakeAdapter):
        async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
            series = await super().ohlcv(symbol, timeframe, limit)
            return series.model_copy(update={"fetched_at": NOW - timedelta(hours=48)})

    assembler, _ = make_assembler(config, StaleAdapter())

    with pytest.raises(CoreDataMissing, match="stale OHLCV"):
        await assembler.assemble("BTCUSDT")


# ── per-symbol isolation (the requirement) ───────────────────────────────────


async def test_one_slow_symbol_does_not_block_the_others(config: AppConfig) -> None:
    adapter = FakeAdapter(delay={"SLOWUSDT": 5.0})  # timeout is 1s
    assembler, _ = make_assembler(config, adapter)
    skips: list[tuple[str, str]] = []

    snapshots = await asyncio.wait_for(
        assembler.assemble_many(
            ["BTCUSDT", "SLOWUSDT", "ETHUSDT"], on_skip=lambda s, r: skips.append((s, r))
        ),
        timeout=4.0,  # would blow up if the slow symbol serialised the cycle
    )

    assert {s.symbol for s in snapshots} == {"BTCUSDT", "ETHUSDT"}
    assert skips == [("SLOWUSDT", "timeout after 1s")]


async def test_one_failing_symbol_does_not_affect_the_others(config: AppConfig) -> None:
    adapter = FakeAdapter(
        ohlcv_error={
            "BADUSDT": SourceUnavailable("binance_usdm", "delisted"),
            "BOOMUSDT": RuntimeError("unexpected"),
        }
    )
    assembler, _ = make_assembler(config, adapter)
    skips: list[tuple[str, str]] = []

    snapshots = await assembler.assemble_many(
        ["BTCUSDT", "BADUSDT", "ETHUSDT", "BOOMUSDT", "SOLUSDT"],
        on_skip=lambda s, r: skips.append((s, r)),
    )

    assert {s.symbol for s in snapshots} == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert {symbol for symbol, _ in skips} == {"BADUSDT", "BOOMUSDT"}
    assert all(reason for _, reason in skips)


async def test_every_symbol_failing_yields_an_empty_cycle_not_an_exception(
    config: AppConfig,
) -> None:
    adapter = FakeAdapter(
        ohlcv_error={
            "BTCUSDT": SourceUnavailable("binance_usdm", "down"),
            "ETHUSDT": SourceUnavailable("binance_usdm", "down"),
        }
    )
    assembler, _ = make_assembler(config, adapter)

    assert await assembler.assemble_many(["BTCUSDT", "ETHUSDT"]) == []


# ── cycle-wide context is fetched once ───────────────────────────────────────


async def test_global_context_is_fetched_once_per_cycle(config: AppConfig) -> None:
    """§3: responses cached per cycle — 10 symbols must not mean 10 F&G calls."""
    assembler, clients = make_assembler(config, FakeAdapter())

    await assembler.assemble_many(["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"])

    for client in clients.values():
        assert client.calls == 1


async def test_supplied_context_is_reused(config: AppConfig) -> None:
    assembler, clients = make_assembler(config, FakeAdapter())
    context = GlobalContext()

    await assembler.assemble("BTCUSDT", context=context)

    for client in clients.values():
        assert client.calls == 0


async def test_news_currencies_are_derived_from_the_watchlist(config: AppConfig) -> None:
    class RecordingNews(CountingClient):
        def __init__(self) -> None:
            super().__init__(
                NewsContext(source="cryptopanic", fetched_at=NOW, provider="cryptopanic")
            )
            self.currencies: tuple[str, ...] = ()

        async def fetch(self, *args: Any) -> Any:
            self.currencies = args[0] if args else ()
            return await super().fetch()

    clients = global_clients()
    news = RecordingNews()
    clients["news"] = news
    assembler, _ = make_assembler(config, FakeAdapter(), clients)

    await assembler.assemble_many(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    assert news.currencies == ("BTC", "ETH", "SOL")

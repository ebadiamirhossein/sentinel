"""Binance adapter — parsing recorded ccxt output, and the no-credentials rule."""

from __future__ import annotations

import ast
import inspect
import textwrap
from decimal import Decimal

import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import IngestionConfig
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter, to_ccxt_symbol
from sentinel.ingestion.adapters.protocol import MarketDataAdapter
from sentinel.ingestion.errors import SourceUnavailable
from tests.ingestion.conftest import FakeExchange


@pytest.fixture
def adapter(ingestion_config: IngestionConfig, clock: FrozenClock) -> BinanceCryptoAdapter:
    return BinanceCryptoAdapter(ingestion_config, exchange=FakeExchange(), clock=clock)


def test_adapter_satisfies_the_protocol(adapter: BinanceCryptoAdapter) -> None:
    assert isinstance(adapter, MarketDataAdapter)


@pytest.mark.parametrize(
    ("plain", "expected"),
    [
        ("BTCUSDT", "BTC/USDT:USDT"),
        ("SOLUSDT", "SOL/USDT:USDT"),
        ("BTC/USDT:USDT", "BTC/USDT:USDT"),
    ],
)
def test_symbol_mapping(plain: str, expected: str) -> None:
    assert to_ccxt_symbol(plain) == expected


def test_symbol_mapping_rejects_unknown_quote() -> None:
    with pytest.raises(ValueError, match="cannot map"):
        to_ccxt_symbol("BTCGBP")


# ── the hard constraint ──────────────────────────────────────────────────────


def test_no_credentials_are_ever_passed_to_ccxt() -> None:
    """CLAUDE.md/PRD §3: a keyless client physically cannot place an order.

    Checks the parsed literals, not the raw text, so the explanatory comment
    about the *absence* of credentials does not trip the assertion.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(BinanceCryptoAdapter._build_exchange)))
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)} | {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }

    for forbidden in ("apiKey", "secret", "password", "privateKey", "walletAddress", "uid"):
        assert forbidden not in literals

    exchange = FakeExchange()
    BinanceCryptoAdapter(IngestionConfig(), exchange=exchange)
    assert exchange.apiKey is None
    assert exchange.secret is None


def test_adapter_module_exposes_no_order_functions() -> None:
    import sentinel.ingestion.adapters.crypto_binance as module

    names = [name.lower() for name in dir(module)]
    for forbidden in ("create_order", "place_order", "cancel_order", "withdraw"):
        assert not any(forbidden in name for name in names)


# ── OHLCV ────────────────────────────────────────────────────────────────────


async def test_ohlcv_parses_recorded_candles(adapter: BinanceCryptoAdapter) -> None:
    series = await adapter.ohlcv("BTCUSDT", "1h", 60)

    assert series.symbol == "BTCUSDT"
    assert series.timeframe == "1h"
    assert series.source == "binance_usdm"
    assert len(series.candles) == 60

    candle = series.candles[-1]
    assert candle.open_time.tzinfo is not None
    assert isinstance(candle.close, Decimal)
    assert candle.high >= candle.low
    assert series.last_close == candle.close


async def test_ohlcv_requests_the_configured_tail_length(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    exchange = FakeExchange()
    adapter = BinanceCryptoAdapter(ingestion_config, exchange=exchange, clock=clock)

    await adapter.ohlcv("BTCUSDT", "4h", 200)

    name, args, kwargs = exchange.calls[-1]
    assert name == "fetch_ohlcv"
    assert args == ("BTC/USDT:USDT", "4h")
    assert kwargs["limit"] == 200


async def test_ohlcv_to_frame_is_indicator_ready(adapter: BinanceCryptoAdapter) -> None:
    frame = (await adapter.ohlcv("BTCUSDT", "1h", 60)).to_frame()

    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "open_time"
    assert len(frame) == 60
    assert frame["close"].dtype.kind == "f"


async def test_empty_ohlcv_is_a_source_failure(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    class EmptyExchange(FakeExchange):
        async def fetch_ohlcv(
            self, symbol: str, timeframe: str, limit: int | None = None
        ) -> list[list[float]]:
            return []

    adapter = BinanceCryptoAdapter(ingestion_config, exchange=EmptyExchange(), clock=clock)

    with pytest.raises(SourceUnavailable, match="empty OHLCV"):
        await adapter.ohlcv("BTCUSDT", "1h", 60)


# ── derivatives ──────────────────────────────────────────────────────────────


async def test_derivatives_context_parses_funding_oi_and_positioning(
    adapter: BinanceCryptoAdapter,
) -> None:
    deriv = await adapter.derivatives_context("BTCUSDT")

    assert deriv is not None
    assert isinstance(deriv.funding_rate, Decimal)
    assert deriv.next_funding_time is not None and deriv.next_funding_time.tzinfo is not None
    assert deriv.open_interest_base > 0
    assert len(deriv.open_interest_24h) == 24
    assert deriv.open_interest_24h[0].at.tzinfo is not None
    assert deriv.long_short_ratio is not None
    assert deriv.long_account_pct is not None and deriv.short_account_pct is not None


async def test_open_interest_history_uses_configured_period_and_limit(
    clock: FrozenClock,
) -> None:
    config = IngestionConfig(open_interest_history_period="1h", open_interest_history_limit=24)
    exchange = FakeExchange()
    adapter = BinanceCryptoAdapter(config, exchange=exchange, clock=clock)

    await adapter.derivatives_context("BTCUSDT")

    history_call = next(c for c in exchange.calls if c[0] == "fetch_open_interest_history")
    assert history_call[1] == ("BTC/USDT:USDT", "1h")
    assert history_call[2]["limit"] == 24

    ratio_call = next(c for c in exchange.calls if c[0].startswith("fapiData"))
    assert ratio_call[1][0] == {"symbol": "BTCUSDT", "period": "1h", "limit": 1}


async def test_missing_long_short_rows_degrade_rather_than_raise(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    class NoRatio(FakeExchange):
        async def fapiDataGetTopLongShortAccountRatio(
            self, params: dict[str, object]
        ) -> list[dict[str, object]]:
            return []

    adapter = BinanceCryptoAdapter(ingestion_config, exchange=NoRatio(), clock=clock)
    deriv = await adapter.derivatives_context("BTCUSDT")

    assert deriv is not None
    assert deriv.long_short_ratio is None  # named as missing, never invented


# ── order book ───────────────────────────────────────────────────────────────


async def test_orderbook_imbalance_is_bounded_and_signed(
    adapter: BinanceCryptoAdapter,
) -> None:
    book = await adapter.orderbook_snapshot("BTCUSDT")

    assert book is not None
    assert Decimal("-1") <= book.imbalance <= Decimal("1")
    assert book.best_ask > book.best_bid
    assert book.spread_pct > 0
    assert book.bid_notional > 0 and book.ask_notional > 0

    expected = (book.bid_notional - book.ask_notional) / (book.bid_notional + book.ask_notional)
    assert book.imbalance == expected


async def test_orderbook_requests_configured_depth(clock: FrozenClock) -> None:
    exchange = FakeExchange()
    adapter = BinanceCryptoAdapter(
        IngestionConfig(orderbook_depth=50), exchange=exchange, clock=clock
    )

    await adapter.orderbook_snapshot("BTCUSDT")

    name, args, kwargs = exchange.calls[-1]
    assert name == "fetch_order_book"
    assert args == ("BTC/USDT:USDT",)
    assert kwargs["limit"] == 50


async def test_empty_book_is_a_source_failure(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    class EmptyBook(FakeExchange):
        async def fetch_order_book(
            self, symbol: str, limit: int | None = None
        ) -> dict[str, object]:
            return {"bids": [], "asks": []}

    adapter = BinanceCryptoAdapter(ingestion_config, exchange=EmptyBook(), clock=clock)

    with pytest.raises(SourceUnavailable, match="empty order book"):
        await adapter.orderbook_snapshot("BTCUSDT")


# ── instrument meta ──────────────────────────────────────────────────────────


async def test_instrument_meta_maps_exchange_rules(adapter: BinanceCryptoAdapter) -> None:
    meta = await adapter.instrument_meta("BTCUSDT")

    assert meta.symbol == "BTCUSDT"
    assert meta.tick_size == Decimal("0.1")
    assert meta.qty_step == Decimal("0.001")
    assert meta.min_notional == Decimal("50")
    assert meta.contract_size == Decimal("1")


async def test_instrument_meta_is_cached_in_process(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    exchange = FakeExchange()
    adapter = BinanceCryptoAdapter(ingestion_config, exchange=exchange, clock=clock)

    await adapter.instrument_meta("BTCUSDT")
    await adapter.instrument_meta("BTCUSDT")

    assert sum(1 for call in exchange.calls if call[0] == "market") == 1


def test_market_hours_is_always_open(adapter: BinanceCryptoAdapter) -> None:
    hours = adapter.market_hours()
    assert hours.always_open is True
    assert hours.venue == "binance_usdm"


async def test_close_closes_the_exchange(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    exchange = FakeExchange()
    adapter = BinanceCryptoAdapter(ingestion_config, exchange=exchange, clock=clock)

    await adapter.close()

    assert exchange.closed is True

"""Binance USDT-M futures adapter (specs/DATA_SOURCES.md §2.1).

**Public endpoints only.** No API key is ever constructed, read from the
environment, or passed to ccxt — v1 has no execution path, so the process holds
no credential that could place an order (CLAUDE.md hard constraint, PRD §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import IngestionConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.models import (
    BookSnapshot,
    Candle,
    DerivContext,
    InstrumentMeta,
    MarketHours,
    OHLCVSeries,
    OpenInterestPoint,
)

log = get_logger(__name__)

SOURCE = "binance_usdm"
_QUOTES = ("USDT", "USDC", "BUSD")


def to_ccxt_symbol(symbol: str) -> str:
    """``BTCUSDT`` → ``BTC/USDT:USDT`` (ccxt's linear-perpetual notation)."""
    if "/" in symbol:
        return symbol
    for quote in _QUOTES:
        if symbol.endswith(quote):
            return f"{symbol[: -len(quote)]}/{quote}:{quote}"
    raise ValueError(f"cannot map {symbol!r} to a ccxt perpetual symbol")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _require(value: Any, field: str) -> Decimal:
    converted = _dec(value)
    if converted is None:
        raise SourceUnavailable(SOURCE, f"missing {field}")
    return converted


def _ms_to_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


class BinanceCryptoAdapter:
    """:class:`~sentinel.ingestion.adapters.protocol.MarketDataAdapter` for Binance."""

    def __init__(
        self,
        config: IngestionConfig,
        *,
        exchange: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or SystemClock()
        self._exchange = exchange if exchange is not None else self._build_exchange(config)
        self._meta_cache: dict[str, InstrumentMeta] = {}
        self._markets_loaded = exchange is not None

    @staticmethod
    def _build_exchange(config: IngestionConfig) -> Any:
        import ccxt.async_support as ccxt

        # Note the absence of apiKey/secret. This is deliberate and load-bearing:
        # a keyless client physically cannot place an order.
        return ccxt.binanceusdm(
            {
                "enableRateLimit": True,  # §2.1 — stay inside public limits
                "timeout": int(config.request_timeout_seconds * 1000),
                "options": {"defaultType": "swap"},
            }
        )

    def _now(self) -> datetime:
        return self._clock.now()

    async def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            await self._exchange.load_markets()
            self._markets_loaded = True

    # ── MarketDataAdapter ────────────────────────────────────────────────────

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        raw = await self._exchange.fetch_ohlcv(to_ccxt_symbol(symbol), timeframe, limit=limit)
        if not raw:
            raise SourceUnavailable(SOURCE, f"empty OHLCV for {symbol} {timeframe}")

        candles = tuple(
            Candle(
                open_time=_ms_to_utc(row[0]) or self._now(),
                open=_require(row[1], "open"),
                high=_require(row[2], "high"),
                low=_require(row[3], "low"),
                close=_require(row[4], "close"),
                volume=_require(row[5], "volume"),
            )
            for row in raw
        )
        return OHLCVSeries(
            source=SOURCE,
            fetched_at=self._now(),
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
        )

    async def derivatives_context(self, symbol: str) -> DerivContext | None:
        ccxt_symbol = to_ccxt_symbol(symbol)

        funding = await self._exchange.fetch_funding_rate(ccxt_symbol)
        open_interest = await self._exchange.fetch_open_interest(ccxt_symbol)
        history = await self._exchange.fetch_open_interest_history(
            ccxt_symbol,
            self._config.open_interest_history_period,
            limit=self._config.open_interest_history_limit,
        )
        long_short = await self._fetch_long_short(symbol)

        return DerivContext(
            source=SOURCE,
            fetched_at=self._now(),
            funding_rate=_require(funding.get("fundingRate"), "fundingRate"),
            next_funding_time=_ms_to_utc(funding.get("fundingTimestamp")),
            next_funding_rate=_dec(funding.get("nextFundingRate")),
            mark_price=_dec(funding.get("markPrice")),
            open_interest_base=_require(
                open_interest.get("openInterestAmount"), "openInterestAmount"
            ),
            open_interest_value=_dec(open_interest.get("openInterestValue")),
            open_interest_24h=tuple(
                OpenInterestPoint(
                    at=_ms_to_utc(point.get("timestamp")) or self._now(),
                    open_interest_base=_require(point.get("openInterestAmount"), "oi"),
                    open_interest_value=_dec(point.get("openInterestValue")),
                )
                for point in history
            ),
            long_short_ratio=_dec(long_short.get("longShortRatio")),
            long_account_pct=_dec(long_short.get("longAccount")),
            short_account_pct=_dec(long_short.get("shortAccount")),
        )

    async def _fetch_long_short(self, symbol: str) -> dict[str, Any]:
        """Top-trader account ratio — a raw fapi data endpoint, via ccxt's implicit API."""
        rows = await self._exchange.fapiDataGetTopLongShortAccountRatio(
            {"symbol": symbol, "period": self._config.long_short_period, "limit": 1}
        )
        if not rows:
            return {}
        latest: dict[str, Any] = rows[-1]
        return latest

    async def orderbook_snapshot(self, symbol: str) -> BookSnapshot | None:
        depth = self._config.orderbook_depth
        book = await self._exchange.fetch_order_book(to_ccxt_symbol(symbol), depth)

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise SourceUnavailable(SOURCE, f"empty order book for {symbol}")

        bid_notional = sum(
            (_require(price, "bid") * _require(size, "bid_size") for price, size, *_ in bids),
            Decimal("0"),
        )
        ask_notional = sum(
            (_require(price, "ask") * _require(size, "ask_size") for price, size, *_ in asks),
            Decimal("0"),
        )
        total = bid_notional + ask_notional

        best_bid = _require(bids[0][0], "best_bid")
        best_ask = _require(asks[0][0], "best_ask")
        mid = (best_bid + best_ask) / 2

        return BookSnapshot(
            source=SOURCE,
            fetched_at=self._now(),
            depth_levels=min(depth, len(bids), len(asks)),
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=(best_ask - best_bid) / mid * 100 if mid else Decimal("0"),
            bid_notional=bid_notional,
            ask_notional=ask_notional,
            imbalance=(bid_notional - ask_notional) / total if total else Decimal("0"),
        )

    async def instrument_meta(self, symbol: str) -> InstrumentMeta:
        """Tick/step/min-notional. Cached in-process; the repository caches for 24h."""
        if symbol in self._meta_cache:
            return self._meta_cache[symbol]

        await self._ensure_markets()
        market = self._exchange.market(to_ccxt_symbol(symbol))
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        cost = limits.get("cost") or {}

        meta = InstrumentMeta(
            source=SOURCE,
            fetched_at=self._now(),
            symbol=symbol,
            tick_size=_require(precision.get("price"), "tick_size"),
            qty_step=_require(precision.get("amount"), "qty_step"),
            min_notional=_dec(cost.get("min")) or Decimal("0"),
            contract_size=_dec(market.get("contractSize")) or Decimal("1"),
        )
        self._meta_cache[symbol] = meta
        return meta

    def market_hours(self) -> MarketHours:
        return MarketHours(always_open=True, venue=SOURCE)

    async def close(self) -> None:
        close = getattr(self._exchange, "close", None)
        if close is not None:
            await close()

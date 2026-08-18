"""Cassette-backed doubles for the ingestion tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import IngestionConfig
from sentinel.ingestion.http import HttpFetcher
from tests.conftest import CASSETTE_NOW, cassette


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CASSETTE_NOW)


@pytest.fixture
def ingestion_config() -> IngestionConfig:
    return IngestionConfig()


def make_fetcher(
    handler: httpx.MockTransport,
    *,
    max_retries: int = 2,
    backoff_seconds: float = 0.0,
) -> tuple[HttpFetcher, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler, follow_redirects=True)
    return (
        HttpFetcher(
            client, timeout_seconds=1.0, max_retries=max_retries, backoff_seconds=backoff_seconds
        ),
        client,
    )


def json_transport(payload: Any, status_code: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


class FakeExchange:
    """Replays recorded ccxt output; records the calls the adapter made."""

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = ("BTCUSDT", "SOLUSDT"),
        fail: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.closed = False
        self._fail = fail or {}
        self._plain = {f"{s[:-4]}/USDT:USDT": s for s in symbols}
        self.options: dict[str, Any] = {}
        #: Deliberately empty — a keyless client cannot trade.
        self.apiKey: str | None = None
        self.secret: str | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self._fail:
            raise self._fail[name]

    def _symbol(self, ccxt_symbol: str) -> str:
        try:
            return self._plain[ccxt_symbol]
        except KeyError as exc:
            raise ValueError(f"unknown symbol {ccxt_symbol}") from exc

    async def load_markets(self) -> dict[str, Any]:
        self._record("load_markets")
        return {}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int | None = None
    ) -> list[list[float]]:
        self._record("fetch_ohlcv", symbol, timeframe, limit=limit)
        rows: list[list[float]] = cassette(f"binance_ohlcv_{self._symbol(symbol)}_{timeframe}.json")
        return rows if limit is None else rows[-limit:]

    async def fetch_order_book(self, symbol: str, limit: int | None = None) -> dict[str, Any]:
        self._record("fetch_order_book", symbol, limit=limit)
        book: dict[str, Any] = cassette(f"binance_orderbook_{self._symbol(symbol)}.json")
        return book

    async def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        self._record("fetch_funding_rate", symbol)
        funding: dict[str, Any] = cassette(f"binance_funding_{self._symbol(symbol)}.json")
        return funding

    async def fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        self._record("fetch_open_interest", symbol)
        oi: dict[str, Any] = cassette(f"binance_oi_{self._symbol(symbol)}.json")
        return oi

    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self._record("fetch_open_interest_history", symbol, timeframe, limit=limit)
        history: list[dict[str, Any]] = cassette(f"binance_oi_history_{self._symbol(symbol)}.json")
        return history

    async def fapiDataGetTopLongShortAccountRatio(
        self, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self._record("fapiDataGetTopLongShortAccountRatio", params)
        rows: list[dict[str, Any]] = cassette(f"binance_long_short_{params['symbol']}.json")
        return rows

    def market(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("market", (symbol,), {}))
        market: dict[str, Any] = cassette(f"binance_market_{self._symbol(symbol)}.json")
        return market

    async def close(self) -> None:
        self.closed = True


def freeze_to_last_candle(exchange_symbol: str = "BTCUSDT") -> datetime:
    """A `now` that keeps the recorded 15m candles fresh (2x timeframe budget)."""
    rows: list[list[float]] = cassette(f"binance_ohlcv_{exchange_symbol}_15m.json")
    return datetime.fromtimestamp(rows[-1][0] / 1000, tz=UTC)

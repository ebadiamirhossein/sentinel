"""The market adapter seam (specs/DATA_SOURCES.md §3).

``crypto_binance.py`` is the only v1 implementation. A future ``forex_oanda.py``
implements the same protocol and the pipeline does not change.

Deviation from the literal signature in §3: ``ohlcv`` returns an
:class:`~sentinel.ingestion.models.OHLCVSeries` rather than a bare ``DataFrame``.
A DataFrame cannot carry the ``{source, fetched_at}`` stamp that the same spec
requires of every record (§3 preamble), and it is not a Pydantic contract. The
spec's DataFrame is one call away: ``series.to_frame()``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinel.ingestion.models import (
    BookSnapshot,
    DerivContext,
    InstrumentMeta,
    MarketHours,
    OHLCVSeries,
)


@runtime_checkable
class MarketDataAdapter(Protocol):
    """Everything the snapshot assembler needs from one venue."""

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries: ...

    #: None for spot/forex venues that have no derivatives context.
    async def derivatives_context(self, symbol: str) -> DerivContext | None: ...

    async def orderbook_snapshot(self, symbol: str) -> BookSnapshot | None: ...

    #: Tick size, quantity step and min notional — consumed by the risk engine.
    async def instrument_meta(self, symbol: str) -> InstrumentMeta: ...

    def market_hours(self) -> MarketHours: ...

    async def close(self) -> None: ...

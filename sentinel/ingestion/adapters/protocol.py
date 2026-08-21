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
    #:
    #: ``None`` from M10b, for a venue whose trading rules genuinely do not have this
    #: shape. Forex has a pip and a minimum *trade size* in base units, not a tick, a
    #: quantity step and a minimum notional; forcing those into these four fields
    #: would put a units figure in ``min_notional`` and read as a money one. The
    #: forex adapter answers ``None`` here and exposes a ``ForexInstrument`` instead.
    #: ``MarketSnapshot.instrument`` has always been optional, and the assembler
    #: already treats a missing one as a degraded secondary rather than a failure.
    async def instrument_meta(self, symbol: str) -> InstrumentMeta | None: ...

    def market_hours(self) -> MarketHours: ...

    async def close(self) -> None: ...

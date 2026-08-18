"""Snapshot assembler — global context once per cycle, then isolated per-symbol work.

ARCHITECTURE.md §3: "Assemble MarketSnapshot for each watchlist symbol (parallel,
per-symbol timeout 20s; failures → symbol skipped this cycle, logged)."

Isolation is the load-bearing property here: every symbol runs under its own
``asyncio.wait_for`` and its own exception boundary, so a hung exchange call for
one symbol can never delay or fail another.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.adapters.protocol import MarketDataAdapter
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.errors import CoreDataMissing, IngestionError
from sentinel.ingestion.models import (
    DataQuality,
    FxRate,
    GlobalContext,
    MacroContext,
    MarketSnapshot,
    NewsContext,
    SentimentContext,
)
from sentinel.ingestion.staleness import SnapshotParts, evaluate

log = get_logger(__name__)


async def _optional[T](name: str, coro: Awaitable[T], failures: dict[str, str]) -> T | None:
    """Run a secondary fetch; record the failure instead of propagating it."""
    try:
        return await coro
    except (IngestionError, TimeoutError, ValueError, KeyError) as exc:
        failures[name] = str(exc)
        log.warning("ingestion.source_failed", source=name, error=str(exc))
        return None


class SnapshotAssembler:
    """Builds :class:`MarketSnapshot` objects for a watchlist."""

    def __init__(
        self,
        adapter: MarketDataAdapter,
        config: AppConfig,
        *,
        news: NewsClient | None = None,
        sentiment: SentimentClient | None = None,
        macro: MacroClient | None = None,
        fx: FxClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._news = news
        self._sentiment = sentiment
        self._macro = macro
        self._fx = fx
        self._clock = clock or SystemClock()

    # ── cycle-wide context ───────────────────────────────────────────────────

    async def global_context(self, symbols: Sequence[str] = ()) -> GlobalContext:
        """Fetched once per cycle and shared by every symbol (§3: cached per cycle)."""
        failures: dict[str, str] = {}
        currencies = tuple(dict.fromkeys(s.removesuffix("USDT") for s in symbols))

        async def news() -> NewsContext | None:
            if self._news is None:
                return None
            return await _optional("news", self._news.fetch(currencies), failures)

        async def sentiment() -> SentimentContext | None:
            if self._sentiment is None:
                return None
            return await _optional("fear_greed", self._sentiment.fetch(), failures)

        async def macro() -> MacroContext | None:
            if self._macro is None:
                return None
            return await _optional("btc_dominance", self._macro.fetch(), failures)

        async def fx() -> FxRate | None:
            if self._fx is None:
                return None
            return await _optional("eurusd", self._fx.fetch(), failures)

        news_ctx, sentiment_ctx, macro_ctx, fx_rate = await asyncio.gather(
            news(), sentiment(), macro(), fx()
        )

        if failures:
            log.warning("ingestion.global_context_degraded", failures=sorted(failures))

        return GlobalContext(news=news_ctx, sentiment=sentiment_ctx, macro=macro_ctx, fx=fx_rate)

    # ── per symbol ───────────────────────────────────────────────────────────

    async def assemble(
        self,
        symbol: str,
        *,
        context: GlobalContext | None = None,
        cycle_id: UUID | None = None,
    ) -> MarketSnapshot:
        """Assemble one symbol. Raises :class:`CoreDataMissing` when it must be skipped."""
        context = context if context is not None else await self.global_context((symbol,))
        parts = SnapshotParts(
            news=context.news,
            sentiment=context.sentiment,
            macro=context.macro,
            fx=context.fx,
        )

        # Core market data: all timeframes must arrive or the symbol is skipped.
        try:
            series_list = await asyncio.gather(
                *(
                    self._adapter.ohlcv(symbol, spec.timeframe, spec.candles)
                    for spec in self._config.market_data.timeframes
                )
            )
        except Exception as exc:
            raise CoreDataMissing(symbol, f"OHLCV fetch failed: {exc}") from exc

        parts.ohlcv = {series.timeframe: series for series in series_list}

        parts.derivatives = await _optional(
            "derivatives", self._adapter.derivatives_context(symbol), parts.failures
        )
        parts.orderbook = await _optional(
            "orderbook", self._adapter.orderbook_snapshot(symbol), parts.failures
        )
        instrument = await _optional(
            "instrument_meta", self._adapter.instrument_meta(symbol), parts.failures
        )

        now = self._clock.now()
        report = evaluate(
            parts,
            now=now,
            required_timeframes=tuple(
                spec.timeframe for spec in self._config.market_data.timeframes
            ),
            max_age=self._config.data_quality.max_age_seconds,
        )
        if report.skip_reason is not None:
            raise CoreDataMissing(symbol, report.skip_reason)

        primary = self._config.market_data.timeframes[0].timeframe
        snapshot = MarketSnapshot(
            cycle_id=cycle_id,
            symbol=symbol,
            captured_at=now,
            last_price=parts.ohlcv[primary].last_close,
            ohlcv=parts.ohlcv,
            instrument=instrument,
            derivatives=parts.derivatives,
            orderbook=parts.orderbook,
            news=parts.news,
            sentiment=parts.sentiment,
            macro=parts.macro,
            fx=parts.fx,
            data_quality=DataQuality.DEGRADED if report.is_degraded else DataQuality.OK,
            degraded_fields=report.degraded_fields,
        )

        log.info(
            "ingestion.snapshot_ready",
            symbol=symbol,
            data_quality=snapshot.data_quality.value,
            degraded_fields=list(snapshot.degraded_fields),
            last_price=str(snapshot.last_price),
        )
        return snapshot

    async def assemble_many(
        self,
        symbols: Sequence[str],
        *,
        cycle_id: UUID | None = None,
        on_skip: Callable[[str, str], None] | None = None,
    ) -> list[MarketSnapshot]:
        """Assemble every symbol concurrently, each under its own timeout.

        One symbol timing out, raising, or lacking core data never affects another:
        results come back per symbol and failures are logged and dropped.
        """
        context = await self.global_context(symbols)
        timeout = self._config.schedule.symbol_timeout_seconds

        async def one(symbol: str) -> MarketSnapshot | None:
            try:
                return await asyncio.wait_for(
                    self.assemble(symbol, context=context, cycle_id=cycle_id),
                    timeout=timeout,
                )
            except TimeoutError:
                reason = f"timeout after {timeout}s"
            except CoreDataMissing as exc:
                reason = exc.reason
            except Exception as exc:  # never let one symbol take down the cycle
                reason = f"{type(exc).__name__}: {exc}"

            log.warning("ingestion.symbol_skipped", symbol=symbol, reason=reason)
            if on_skip is not None:
                on_skip(symbol, reason)
            return None

        results = await asyncio.gather(*(one(symbol) for symbol in symbols))
        snapshots = [snapshot for snapshot in results if snapshot is not None]

        log.info(
            "ingestion.cycle_assembled",
            requested=len(symbols),
            assembled=len(snapshots),
            skipped=len(symbols) - len(snapshots),
        )
        return snapshots

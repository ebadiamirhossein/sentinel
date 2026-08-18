"""Assembling the deterministic half of the pipeline: ingestion + features.

``core/`` is where stages get wired together (ARCHITECTURE.md §3). This began as
private setup inside ``tools/snapshot.py``; M5's ``tools/analyze.py`` needs the
same twelve lines, and M7's cycle orchestrator will need them a third time, so it
lives here once instead of being copied twice.

Nothing here is LLM-aware: it stops at a snapshot with features attached, which
is exactly the input the screener and analyst take.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx

from sentinel.core.config import FeaturesConfig, Settings
from sentinel.core.logging import get_logger
from sentinel.features import compute as compute_features
from sentinel.features.engine import attach as attach_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter
from sentinel.ingestion.assembler import SnapshotAssembler
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import FxRate, MarketSnapshot

log = get_logger(__name__)

LastKnownGoodFx = Callable[[], Coroutine[Any, Any, FxRate | None]]


@asynccontextmanager
async def market_adapter(settings: Settings) -> AsyncIterator[BinanceCryptoAdapter]:
    """Just the exchange adapter, closed after use.

    M7's tracker needs candles and nothing else — no news, no sentiment, no FX. It
    gets its own entry point rather than reaching into ``snapshot_assembler``'s
    internals, which would also spin up four HTTP clients a price check never
    touches.
    """
    adapter = BinanceCryptoAdapter(settings.config.ingestion)
    try:
        yield adapter
    finally:
        await adapter.close()


@asynccontextmanager
async def snapshot_assembler(
    settings: Settings,
    *,
    last_known_good_fx: LastKnownGoodFx | None = None,
) -> AsyncIterator[SnapshotAssembler]:
    """Build the assembler and its clients; close the exchange adapter after.

    ``last_known_good_fx`` is optional so a caller without a database still gets
    a working assembler — the FX client then simply has no fallback and the
    snapshot degrades explicitly, which is specs/DATA_SOURCES.md §2.4's behaviour.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        fetcher = HttpFetcher(
            client,
            timeout_seconds=settings.config.ingestion.request_timeout_seconds,
            max_retries=settings.config.ingestion.max_retries,
            backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
        )
        adapter = BinanceCryptoAdapter(settings.config.ingestion)
        cryptopanic_key = (
            settings.secrets.cryptopanic_api_key.get_secret_value()
            if settings.secrets.cryptopanic_api_key
            else None
        )
        try:
            yield SnapshotAssembler(
                adapter,
                settings.config,
                news=NewsClient(fetcher, settings.config.ingestion, api_key=cryptopanic_key),
                sentiment=SentimentClient(fetcher, settings.config.ingestion),
                macro=MacroClient(fetcher, settings.config.ingestion),
                fx=FxClient(
                    fetcher,
                    settings.config.ingestion,
                    last_known_good=last_known_good_fx,
                ),
            )
        finally:
            await adapter.close()


async def assemble_with_features(
    assembler: SnapshotAssembler,
    symbols: list[str],
    features_config: FeaturesConfig,
    *,
    cycle_id: UUID | None = None,
) -> tuple[list[MarketSnapshot], dict[str, SymbolFeatures]]:
    """Assemble snapshots and compute M2 features for each.

    Returns both the feature-carrying snapshots and the typed ``SymbolFeatures``
    objects: the snapshot holds features as JSON (the storage contract), while
    the screener and chart renderer want the model.
    """
    raw = await assembler.assemble_many(symbols, cycle_id=cycle_id)
    snapshots: list[MarketSnapshot] = []
    features: dict[str, SymbolFeatures] = {}
    for snapshot in raw:
        computed = compute_features(snapshot, features_config)
        features[snapshot.symbol] = computed
        snapshots.append(attach_features(snapshot, computed))
    return snapshots, features

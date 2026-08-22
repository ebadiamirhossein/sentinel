"""Assembling the deterministic half of the pipeline: ingestion + features.

``core/`` is where stages get wired together (ARCHITECTURE.md §3). This began as
private setup inside ``tools/snapshot.py``; M5's ``tools/analyze.py`` needs the
same twelve lines, and M7's cycle orchestrator will need them a third time, so it
lives here once instead of being copied twice.

Nothing here is LLM-aware: it stops at a snapshot with features attached, which
is exactly the input the screener and analyst take.

**M10b-2 adds the adapter registry.** ``MarketConfig.adapter`` has named an adapter
by string since M10a and nothing resolved it: this module hardcoded
:class:`BinanceCryptoAdapter` in two places and even annotated its return type as
that concrete class. So ``adapter: forex_saxo`` in the shipped config named real,
tested code and no behaviour at all — which was the correct state for M10a and is
not one to leave standing now that a forex cycle exists.

The property M10a was protecting by leaving the key unregistered — *a market naming
an adapter that does not exist must fail loudly rather than do nothing* — is kept:
:func:`build_adapter` raises on an unknown name. What changes is that
``forex_saxo`` is no longer one of the unknown names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx

from sentinel.core.config import (
    CRYPTO_ADAPTER,
    FOREX_ADAPTER,
    FeaturesConfig,
    Settings,
    TimeframeSpec,
)
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.features import compute as compute_features
from sentinel.features.engine import attach as attach_features
from sentinel.features.models import SymbolFeatures
from sentinel.fx.errors import ReauthenticationRequired
from sentinel.fx.models import SaxoTokenBundle
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter
from sentinel.ingestion.adapters.forex_saxo import AccessTokenProvider, SaxoForexAdapter
from sentinel.ingestion.adapters.protocol import MarketDataAdapter
from sentinel.ingestion.assembler import SnapshotAssembler
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.clients.saxo_auth import SaxoAuth
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import FxRate, MarketSnapshot
from sentinel.storage.db import Database
from sentinel.storage.repositories import SaxoTokenRepository

log = get_logger(__name__)

LastKnownGoodFx = Callable[[], Coroutine[Any, Any, FxRate | None]]


class UnknownAdapter(LookupError):
    """A market names an adapter this deployment cannot build.

    A dedicated type because the remedy is specific and the alternative is worse:
    falling back to the crypto adapter would ingest Binance candles for EURUSD and
    label them forex, which is a corrupted population rather than an outage.
    """


def _binance(settings: Settings, **_: Any) -> MarketDataAdapter:
    return BinanceCryptoAdapter(settings.config.ingestion)


def _saxo(
    settings: Settings,
    *,
    fetcher: HttpFetcher | None = None,
    tokens: AccessTokenProvider | None = None,
    **_: Any,
) -> MarketDataAdapter:
    """The Saxo adapter needs two things the Binance one owns for itself.

    It does not create its own HTTP client (the caller's is reused, and closing it is
    the caller's job) and it cannot mint its own access token — the OAuth chain is
    stateful, single-use and lives in Postgres (§3). Both arrive as keyword arguments
    rather than being built here, so a caller that has neither gets a clear error
    instead of a half-built adapter that fails on its first request.
    """
    if fetcher is None or tokens is None:
        raise UnknownAdapter(
            f"{FOREX_ADAPTER} needs an HTTP fetcher and an access-token provider; "
            f"this call site supplied {'no fetcher' if fetcher is None else 'no tokens'}"
        )
    return SaxoForexAdapter(settings.config.forex, fetcher=fetcher, tokens=tokens)


#: Adapter name -> factory. ``MarketConfig.adapter`` is resolved through here and
#: nowhere else, so there is exactly one place a market's data source is decided.
ADAPTERS: dict[str, Callable[..., MarketDataAdapter]] = {
    CRYPTO_ADAPTER: _binance,
    FOREX_ADAPTER: _saxo,
}


def build_adapter(
    settings: Settings,
    market: Market = LEGACY_MARKET,
    *,
    fetcher: HttpFetcher | None = None,
    tokens: AccessTokenProvider | None = None,
) -> MarketDataAdapter:
    """Build the adapter this market's config names.

    Raises :class:`UnknownAdapter` on a name nothing implements. That is the property
    M10a was protecting by deliberately leaving ``forex_saxo`` unregistered, and it
    still holds for a typo — which is the case that actually happens.
    """
    name = settings.config.market(market).adapter
    factory = ADAPTERS.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTERS))
        raise UnknownAdapter(
            f"market {market.value!r} names adapter {name!r}, which nothing implements "
            f"(known: {known})"
        )
    return factory(settings, fetcher=fetcher, tokens=tokens)


@asynccontextmanager
async def market_adapter(
    settings: Settings,
    market: Market = LEGACY_MARKET,
    *,
    fetcher: HttpFetcher | None = None,
    tokens: AccessTokenProvider | None = None,
) -> AsyncIterator[MarketDataAdapter]:
    """Just the exchange adapter, closed after use.

    M7's tracker needs candles and nothing else — no news, no sentiment, no FX. It
    gets its own entry point rather than reaching into ``snapshot_assembler``'s
    internals, which would also spin up four HTTP clients a price check never
    touches.

    The return type is the protocol, not a concrete class: it was
    ``BinanceCryptoAdapter`` while that was the only thing this could build, and
    leaving it would make the registry a lie ``mypy`` believed.
    """
    adapter = build_adapter(settings, market, fetcher=fetcher, tokens=tokens)
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

    **Crypto only, deliberately.** Forex does not come through here: its staleness rule
    is a different rule (spec defect #14 — ``ingestion/staleness`` measures from the
    *fetch*, which is always recent, and forex needs recency measured from the
    *candle*, skipped while the market is shut), and its bid and ask tails have to be
    one read rather than two (D-d: the series is not stable across queries). See
    :mod:`sentinel.core.forex_cycle`.
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        fetcher = HttpFetcher(
            client,
            timeout_seconds=settings.config.ingestion.request_timeout_seconds,
            max_retries=settings.config.ingestion.max_retries,
            backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
        )
        adapter = build_adapter(settings, Market.CRYPTO)
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


@asynccontextmanager
async def forex_fx_client(settings: Settings) -> AsyncIterator[FxClient]:
    """A Frankfurter client for the forex path's EUR->quote rates (§7.1).

    Its own entry point rather than a field on the snapshot assembler, for the reason
    :func:`forex_adapter` is: forex does not go through the assembler at all (defect
    #14), and reaching into one to borrow a client would put a forex dependency inside
    the object a crypto cycle builds.

    **No last-known-good loader.** The crypto client has one because a display rate that
    is an hour stale is better than no card. A *sizing* rate is different: §12 says an
    unavailable rate means no sizing and therefore no signal, and a fallback here would
    quietly size a position at a rate from before whatever moved the market.
    """
    async with httpx.AsyncClient(
        timeout=settings.config.ingestion.request_timeout_seconds
    ) as client:
        yield FxClient(
            HttpFetcher(
                client,
                timeout_seconds=settings.config.ingestion.request_timeout_seconds,
                max_retries=settings.config.ingestion.max_retries,
                backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
            ),
            settings.config.ingestion,
        )


class DatabaseTokenStore:
    """The Saxo credential, in Postgres, one session per operation (§3 requirement 6).

    A session per call rather than one held open for the adapter's lifetime: the
    refresh runs on its own five-minute cadence, and a connection pinned for hours so
    that two short writes can share it is a connection the rest of the app cannot use.

    ``save`` and ``load`` are both real round trips, which is the point — §3
    requirement 2 says a 2xx from the token endpoint is not proof the token was
    stored, and the read-back check is only worth anything if it actually reads.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def load(self) -> SaxoTokenBundle | None:
        async with self._database.session() as session:
            return await SaxoTokenRepository(session).load()

    async def save(self, bundle: SaxoTokenBundle) -> None:
        async with self._database.session() as session:
            await SaxoTokenRepository(session).save(bundle)
            await session.commit()


@asynccontextmanager
async def forex_auth(settings: Settings, database: Database) -> AsyncIterator[SaxoAuth]:
    """The OAuth chain and the HTTP client it talks over, closed after use.

    Split out of :func:`forex_adapter` at M10d, because the credential now has a
    **second** caller that wants no adapter at all: the five-minute refresh job (§3
    requirement 5). That job exists to keep the refresh token's one-hour life rolling
    over — including all weekend, when the market is shut and nothing is reading a
    chart — so building a chart adapter to do it would be building the wrong thing.

    Raises when the app keys are absent. A forex cycle without credentials cannot
    degrade into a quieter forex cycle — it has no data at all — and the failure has
    to name the missing thing rather than surface later as an HTTP 401.
    """
    key, secret = settings.secrets.saxo_app_key, settings.secrets.saxo_app_secret
    if key is None or secret is None:
        raise ReauthenticationRequired(
            "SAXO_APP_KEY and SAXO_APP_SECRET are required to ingest forex; "
            "forex stays paused until they are set (specs/FOREX.md §3)"
        )
    async with httpx.AsyncClient(follow_redirects=True) as client:
        yield SaxoAuth(
            settings.config.forex,
            fetcher=HttpFetcher(
                client,
                timeout_seconds=settings.config.ingestion.request_timeout_seconds,
                max_retries=settings.config.ingestion.max_retries,
                backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
            ),
            store=DatabaseTokenStore(database),
            app_key=key,
            app_secret=secret,
        )


@asynccontextmanager
async def forex_adapter(settings: Settings, database: Database) -> AsyncIterator[SaxoForexAdapter]:
    """The Saxo adapter over the OAuth chain, closed after use.

    Its own entry point rather than a branch inside :func:`snapshot_assembler`,
    because almost nothing is shared: forex has no CryptoPanic, no fear-and-greed and
    no BTC dominance, and it needs a stateful credential none of those want.
    """
    async with forex_auth(settings, database) as auth:
        adapter = build_adapter(settings, Market.FOREX, fetcher=auth.fetcher, tokens=auth)
        assert isinstance(adapter, SaxoForexAdapter)
        try:
            yield adapter
        finally:
            await adapter.close()


def _timeframes_for(settings: Settings, market: Market) -> tuple[TimeframeSpec, ...]:
    """Which tail lengths this market reads.

    ``SnapshotAssembler`` read ``config.market_data.timeframes`` directly until now —
    the crypto global — so a forex assembler would have asked Saxo for crypto's tails.
    Injected rather than branched inside the assembler, and the crypto value is the
    same expression it always was.
    """
    if market is Market.FOREX:
        return settings.config.forex.timeframes
    return settings.config.market_data.timeframes


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

"""The adapter registry — FOREX.md §4, build-order step 8.

``MarketConfig.adapter`` has named an adapter by string since M10a and nothing
resolved it: ``sentinel/core/wiring.py`` hardcoded the Binance adapter in two places.
So ``adapter: forex_saxo`` in the shipped config named real, tested code and no
behaviour at all.

M10a left ``forex_saxo`` deliberately unregistered so that a market claiming it would
fail loudly rather than silently do nothing. **That property is what these tests are
mostly about**: it has to survive the key becoming resolvable, because the case that
actually happens is a typo, not a deliberate reference to something unbuilt.
"""

from __future__ import annotations

import pytest

from sentinel.core.config import CRYPTO_ADAPTER, FOREX_ADAPTER, Settings
from sentinel.core.markets import Market
from sentinel.core.wiring import ADAPTERS, UnknownAdapter, build_adapter
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter
from sentinel.ingestion.adapters.forex_saxo import SaxoForexAdapter
from sentinel.ingestion.adapters.protocol import MarketDataAdapter


class _StubTokens:
    async def access_token(self) -> str:
        return "stub"


def test_the_crypto_adapter_is_built_exactly_as_it_was_before_the_registry(
    settings: Settings,
) -> None:
    """The registry must be a lookup, not a change. Crypto is mid-measurement."""
    adapter = build_adapter(settings, Market.CRYPTO)
    assert isinstance(adapter, BinanceCryptoAdapter)
    assert isinstance(adapter, MarketDataAdapter)
    # And the default market is still crypto, so every pre-M10b-2 call site is
    # untouched by not passing one.
    assert isinstance(build_adapter(settings), BinanceCryptoAdapter)


def test_forex_saxo_now_resolves_to_the_adapter_m10b_1_built(settings: Settings) -> None:
    """The key M10a left unregistered. It resolves to real code from this milestone."""
    adapter = build_adapter(settings, Market.FOREX, fetcher=object(), tokens=_StubTokens())  # type: ignore[arg-type]
    assert isinstance(adapter, SaxoForexAdapter)
    assert isinstance(adapter, MarketDataAdapter)


def test_a_typo_in_a_market_adapter_name_fails_loudly_at_wiring_time(
    settings: Settings,
) -> None:
    """The property M10a's unregistered key was protecting, kept.

    Falling back to the crypto adapter would ingest Binance candles for EURUSD and
    store them as forex — a corrupted population rather than an outage, and the kind
    that is only discovered by the numbers being wrong months later.
    """
    typo = settings.config.model_copy(deep=True)
    typo.markets[Market.FOREX] = typo.markets[Market.FOREX].model_copy(
        update={"adapter": "forex_sax0"}
    )
    broken = settings.model_copy(update={"config": typo})
    with pytest.raises(UnknownAdapter, match="forex_sax0"):
        build_adapter(broken, Market.FOREX)


def test_the_error_names_what_this_deployment_can_actually_build(settings: Settings) -> None:
    typo = settings.config.model_copy(deep=True)
    typo.markets[Market.CRYPTO] = typo.markets[Market.CRYPTO].model_copy(
        update={"adapter": "nonesuch"}
    )
    broken = settings.model_copy(update={"config": typo})
    with pytest.raises(UnknownAdapter, match=f"{CRYPTO_ADAPTER}, {FOREX_ADAPTER}"):
        build_adapter(broken, Market.CRYPTO)


def test_the_saxo_adapter_refuses_to_be_half_built(settings: Settings) -> None:
    """It cannot make its own HTTP client or mint its own token — the OAuth chain is
    stateful, single-use and lives in Postgres (§3). A caller with neither must get an
    error here rather than an adapter that fails on its first request, at 3am."""
    with pytest.raises(UnknownAdapter, match="no fetcher"):
        build_adapter(settings, Market.FOREX)
    with pytest.raises(UnknownAdapter, match="no tokens"):
        build_adapter(settings, Market.FOREX, fetcher=object())  # type: ignore[arg-type]


def test_every_configured_market_names_an_adapter_the_registry_knows(
    settings: Settings,
) -> None:
    """The shipped config, checked as a whole — including the disabled market.

    An unbuildable adapter name behind ``enabled: false`` is a trap set for the day it
    is switched on, and this is the cheapest possible moment to hear about it.
    """
    for market, cfg in settings.config.markets.items():
        assert cfg.adapter in ADAPTERS, f"{market.value} names {cfg.adapter!r}"


def test_forex_reads_its_own_tail_lengths_not_cryptos(settings: Settings) -> None:
    """``SnapshotAssembler`` read ``config.market_data.timeframes`` — the crypto
    global — until this milestone, so a forex assembler would have asked Saxo for
    crypto's tails. The two lists differ on purpose: the forex 1h tail is 1200 bars so
    the spread profile gets ~35 samples per hour-of-day rather than ~10 (§7.3)."""
    from sentinel.core.wiring import _timeframes_for

    crypto = {s.timeframe: s.candles for s in _timeframes_for(settings, Market.CRYPTO)}
    forex = {s.timeframe: s.candles for s in _timeframes_for(settings, Market.FOREX)}
    assert crypto["1h"] == 321
    assert forex["1h"] == 1200
    assert crypto == {s.timeframe: s.candles for s in settings.config.market_data.timeframes}

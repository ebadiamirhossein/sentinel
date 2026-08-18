"""REST clients replayed from cassettes: sentiment, macro, FX, news, and retries."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import IngestionConfig, NewsConfig
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.models import FxRate
from tests.conftest import cassette
from tests.ingestion.conftest import json_transport, make_fetcher

# ── HTTP policy (specs/DATA_SOURCES.md §3, CLAUDE.md) ────────────────────────


async def test_retries_twice_then_fails() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "busy"})

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        with pytest.raises(SourceUnavailable, match="HTTP 503"):
            await fetcher.get_json("https://example.test/x", source="test")

    assert attempts == 3  # initial attempt + max 2 retries


async def test_client_errors_are_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": "nope"})

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        with pytest.raises(SourceUnavailable, match="HTTP 404"):
            await fetcher.get_json("https://example.test/x", source="test")

    assert attempts == 1  # a 4xx will not fix itself


async def test_transient_failure_then_success() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("boom")
        return httpx.Response(200, json={"ok": True})

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        assert await fetcher.get_json("https://example.test/x", source="test") == {"ok": True}

    assert attempts == 2


# ── Fear & Greed ─────────────────────────────────────────────────────────────


async def test_sentiment_parses_value_classification_and_delta(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    fetcher, client = make_fetcher(json_transport(cassette("alternative_fng.json")))
    async with client:
        sentiment = await SentimentClient(fetcher, ingestion_config, clock=clock).fetch()

    assert sentiment.value == 41
    assert sentiment.classification == "Fear"
    assert sentiment.previous_value == 31
    assert sentiment.delta == 10
    assert sentiment.source == "alternative.me"
    assert sentiment.fetched_at.tzinfo is not None


async def test_sentiment_rejects_empty_payload(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    fetcher, client = make_fetcher(json_transport({"data": []}))
    async with client:
        with pytest.raises(SourceUnavailable):
            await SentimentClient(fetcher, ingestion_config, clock=clock).fetch()


# ── CoinGecko ────────────────────────────────────────────────────────────────


async def test_macro_parses_dominance_and_mcap_change(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    fetcher, client = make_fetcher(json_transport(cassette("coingecko_global.json")))
    async with client:
        macro = await MacroClient(fetcher, ingestion_config, clock=clock).fetch()

    assert Decimal("50") < macro.btc_dominance_pct < Decimal("70")
    assert isinstance(macro.total_mcap_change_24h_pct, Decimal)
    assert macro.source == "coingecko"


async def test_macro_rejects_missing_fields(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    fetcher, client = make_fetcher(json_transport({"data": {"market_cap_percentage": {}}}))
    async with client:
        with pytest.raises(SourceUnavailable, match="missing dominance"):
            await MacroClient(fetcher, ingestion_config, clock=clock).fetch()


# ── Frankfurter FX ───────────────────────────────────────────────────────────


async def test_fx_parses_eurusd(ingestion_config: IngestionConfig, clock: FrozenClock) -> None:
    fetcher, client = make_fetcher(json_transport(cassette("frankfurter_latest.json")))
    async with client:
        rate = await FxClient(fetcher, ingestion_config, clock=clock).fetch()

    assert rate.pair == "EURUSD"
    assert rate.rate == Decimal("1.1593")
    assert rate.is_last_known_good is False


async def test_fx_caches_within_the_ttl(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=cassette("frankfurter_latest.json"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        fx = FxClient(fetcher, ingestion_config, clock=clock, cache_ttl_seconds=3600)
        await fx.fetch()
        await fx.fetch()

    assert calls == 1  # §2.4: fetched hourly, cached


async def test_fx_falls_back_to_persisted_last_known_good(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    stored = FxRate(
        source="frankfurter",
        fetched_at=clock.now(),
        pair="EURUSD",
        rate=Decimal("1.0850"),
        is_last_known_good=False,
    )

    async def loader() -> FxRate:
        return stored

    fetcher, client = make_fetcher(json_transport({"error": "down"}, status_code=500))
    async with client:
        rate = await FxClient(
            fetcher, ingestion_config, clock=clock, last_known_good=loader
        ).fetch()

    assert rate.rate == Decimal("1.0850")
    assert rate.is_last_known_good is True  # flagged, so staleness can degrade the snapshot


async def test_fx_raises_when_there_is_no_fallback(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    """No rate is better than an invented rate (CLAUDE.md)."""
    fetcher, client = make_fetcher(json_transport({"error": "down"}, status_code=500))
    async with client:
        with pytest.raises(SourceUnavailable):
            await FxClient(fetcher, ingestion_config, clock=clock).fetch()


# ── News ─────────────────────────────────────────────────────────────────────


async def test_news_parses_cryptopanic(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=cassette("cryptopanic_posts.json"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        news = await NewsClient(fetcher, ingestion_config, api_key="test-key", clock=clock).fetch(
            ("BTC", "SOL")
        )

    assert news.provider == "cryptopanic"
    assert len(news.items) == 3

    first = news.items[0]
    assert first.source_domain == "coindesk.com"
    assert first.currencies == ("BTC",)
    assert first.votes == 4
    assert first.age_minutes == 45  # cassette anchor 08:15Z vs published 07:30Z

    assert captured["currencies"] == "BTC,SOL"
    assert captured["filter"] == "important"


async def test_news_falls_back_to_rss_when_cryptopanic_fails(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cryptopanic" in request.url.host:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, text=cassette("coindesk_rss.xml"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        news = await NewsClient(fetcher, ingestion_config, api_key="test-key", clock=clock).fetch(
            ("BTC",)
        )

    assert news.provider == "rss"
    assert news.items
    assert all(item.title for item in news.items)
    assert news.items == tuple(sorted(news.items, key=lambda i: i.published_at, reverse=True))


async def test_news_uses_rss_when_no_key_is_configured(
    ingestion_config: IngestionConfig, clock: FrozenClock
) -> None:
    """An absent CryptoPanic key is a documented fallback, not an error."""
    cryptopanic_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cryptopanic_calls
        if "cryptopanic" in request.url.host:
            cryptopanic_calls += 1
        return httpx.Response(200, text=cassette("coindesk_rss.xml"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    async with client:
        news = await NewsClient(fetcher, ingestion_config, api_key=None, clock=clock).fetch()

    assert cryptopanic_calls == 0
    assert news.provider == "rss"


async def test_news_raises_when_every_source_is_down(clock: FrozenClock) -> None:
    config = IngestionConfig(news=NewsConfig(rss_feeds=("https://feed.test/rss",)))
    fetcher, client = make_fetcher(json_transport({"error": "down"}, status_code=500))
    async with client:
        with pytest.raises(SourceUnavailable):
            await NewsClient(fetcher, config, api_key=None, clock=clock).fetch()


async def test_news_respects_the_configured_limit(clock: FrozenClock) -> None:
    config = IngestionConfig(news=NewsConfig(limit=2))
    fetcher, client = make_fetcher(json_transport(cassette("cryptopanic_posts.json")))
    async with client:
        news = await NewsClient(fetcher, config, api_key="test-key", clock=clock).fetch()

    assert len(news.items) == 2

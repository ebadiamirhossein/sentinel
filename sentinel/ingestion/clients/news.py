"""News headlines: CryptoPanic with an RSS fallback (specs/DATA_SOURCES.md §2.2).

Kept per item: title, source domain, published_at, currencies, votes. Body text is
deliberately not fetched in v1 — the analyst judges relevance from headline,
source and freshness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import IngestionConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import NewsContext, NewsItem

log = get_logger(__name__)

CRYPTOPANIC = "cryptopanic"
RSS = "rss"


def _age_minutes(published_at: datetime, now: datetime) -> int:
    return max(0, int((now - published_at).total_seconds() // 60))


def _domain(url: str | None) -> str:
    if not url:
        return "unknown"
    return urlparse(url).netloc.removeprefix("www.") or "unknown"


class NewsClient:
    """Tries CryptoPanic first (when keyed), then falls back to RSS."""

    def __init__(
        self,
        fetcher: HttpFetcher,
        config: IngestionConfig,
        *,
        api_key: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._config = config
        self._api_key = api_key
        self._clock = clock or SystemClock()

    async def fetch(self, currencies: tuple[str, ...] = ()) -> NewsContext:
        if self._api_key:
            try:
                return await self._fetch_cryptopanic(currencies)
            except SourceUnavailable as exc:
                log.warning("news.cryptopanic_failed", reason=exc.reason)
        else:
            log.info("news.no_cryptopanic_key", fallback=RSS)

        return await self._fetch_rss()

    # ── CryptoPanic ──────────────────────────────────────────────────────────

    async def _fetch_cryptopanic(self, currencies: tuple[str, ...]) -> NewsContext:
        params: dict[str, Any] = {"auth_token": self._api_key, "public": "true"}
        if currencies:
            params["currencies"] = ",".join(currencies)
        if self._config.news.important_only:
            params["filter"] = "important"

        payload: Any = await self._fetcher.get_json(
            self._config.news.cryptopanic_base_url, source=CRYPTOPANIC, params=params
        )
        results = (payload or {}).get("results")
        if results is None:
            raise SourceUnavailable(CRYPTOPANIC, "no results field in payload")

        now = self._clock.now()
        items: list[NewsItem] = []
        for entry in results[: self._config.news.limit]:
            published = self._parse_iso(entry.get("published_at"))
            if published is None:
                continue
            votes = entry.get("votes") or {}
            items.append(
                NewsItem(
                    title=str(entry.get("title", "")).strip(),
                    source_domain=(entry.get("source") or {}).get("domain")
                    or _domain(entry.get("url")),
                    published_at=published,
                    age_minutes=_age_minutes(published, now),
                    currencies=tuple(
                        str(c.get("code")) for c in (entry.get("currencies") or []) if c.get("code")
                    ),
                    votes=votes.get("important") if isinstance(votes, dict) else None,
                )
            )

        return NewsContext(
            source=CRYPTOPANIC, fetched_at=now, provider=CRYPTOPANIC, items=tuple(items)
        )

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    # ── RSS fallback ─────────────────────────────────────────────────────────

    async def _fetch_rss(self) -> NewsContext:
        import feedparser

        now = self._clock.now()
        items: list[NewsItem] = []
        failures: list[str] = []

        for url in self._config.news.rss_feeds:
            try:
                body = await self._fetcher.get_text(url, source=RSS)
            except SourceUnavailable as exc:
                failures.append(f"{_domain(url)}: {exc.reason}")
                continue

            feed = feedparser.parse(body)
            for entry in feed.entries[: self._config.news.limit]:
                published = self._parse_struct_time(entry)
                if published is None:
                    continue
                items.append(
                    NewsItem(
                        title=str(getattr(entry, "title", "")).strip(),
                        source_domain=_domain(url),
                        published_at=published,
                        age_minutes=_age_minutes(published, now),
                    )
                )

        if not items:
            raise SourceUnavailable(RSS, "; ".join(failures) or "no parsable RSS entries")

        items.sort(key=lambda item: item.published_at, reverse=True)
        return NewsContext(
            source=RSS,
            fetched_at=now,
            provider=RSS,
            items=tuple(items[: self._config.news.limit]),
        )

    @staticmethod
    def _parse_struct_time(entry: Any) -> datetime | None:
        from calendar import timegm

        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if parsed is None:
            return None
        return datetime.fromtimestamp(timegm(parsed), tz=UTC)

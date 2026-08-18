"""alternative.me Fear & Greed index (specs/DATA_SOURCES.md §2.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import IngestionConfig
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import SentimentContext

SOURCE = "alternative.me"


class SentimentClient:
    def __init__(
        self, fetcher: HttpFetcher, config: IngestionConfig, *, clock: Clock | None = None
    ) -> None:
        self._fetcher = fetcher
        self._config = config
        self._clock = clock or SystemClock()

    async def fetch(self) -> SentimentContext:
        """Current value + classification, plus yesterday's value for the delta."""
        payload: Any = await self._fetcher.get_json(
            self._config.fear_greed_url, source=SOURCE, params={"limit": 2}
        )
        entries = (payload or {}).get("data") or []
        if not entries:
            raise SourceUnavailable(SOURCE, "no fear & greed entries returned")

        current = entries[0]
        previous = entries[1] if len(entries) > 1 else None

        try:
            value = int(current["value"])
            classification = str(current["value_classification"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceUnavailable(SOURCE, f"unexpected payload: {exc}") from exc

        published = current.get("timestamp")
        fetched_at = (
            datetime.fromtimestamp(int(published), tz=UTC)
            if published is not None
            else self._clock.now()
        )

        return SentimentContext(
            source=SOURCE,
            fetched_at=fetched_at,
            value=value,
            classification=classification,
            previous_value=int(previous["value"]) if previous and "value" in previous else None,
        )

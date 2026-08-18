"""CoinGecko global market data: BTC dominance, total mcap change (§2.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import IngestionConfig
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import MacroContext

SOURCE = "coingecko"


class MacroClient:
    def __init__(
        self, fetcher: HttpFetcher, config: IngestionConfig, *, clock: Clock | None = None
    ) -> None:
        self._fetcher = fetcher
        self._config = config
        self._clock = clock or SystemClock()

    async def fetch(self) -> MacroContext:
        payload: Any = await self._fetcher.get_json(
            self._config.coingecko_global_url, source=SOURCE
        )
        data = (payload or {}).get("data")
        if not data:
            raise SourceUnavailable(SOURCE, "no global data returned")

        dominance = (data.get("market_cap_percentage") or {}).get("btc")
        mcap_change = data.get("market_cap_change_percentage_24h_usd")
        if dominance is None or mcap_change is None:
            raise SourceUnavailable(SOURCE, "missing dominance or market-cap change")

        updated = data.get("updated_at")
        fetched_at = (
            datetime.fromtimestamp(int(updated), tz=UTC)
            if updated is not None
            else self._clock.now()
        )

        return MacroContext(
            source=SOURCE,
            fetched_at=fetched_at,
            btc_dominance_pct=Decimal(str(dominance)),
            total_mcap_change_24h_pct=Decimal(str(mcap_change)),
        )

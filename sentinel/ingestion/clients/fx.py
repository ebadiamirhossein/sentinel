"""EUR rates via Frankfurter (ECB rates, no key) — display/sizing conversion only.

specs/DATA_SOURCES.md §2.4: fetched hourly, cached, with a last-known-good
fallback. The fallback is persisted, so a restart during a Frankfurter outage
still has a rate instead of silently having none.

**M10c adds a second method and does not touch the first.** Forex sizes in EUR
against the *quote* currency (FOREX.md §7.1), which is USD for EURUSD and GBPUSD and
**JPY** for USDJPY — a different number, and reaching for EURUSD there is a ~145x
error that sizes a position to roughly nothing and looks like an unremarkable
rejection. :meth:`FxClient.fetch_quotes` asks for those currencies in one request.

``_fetch_live`` keeps its ``symbols=USD`` parameter **exactly**: it is the request a
live crypto cycle makes every hour, and widening it to serve a market that is
switched off would change a running system's HTTP traffic for nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import IngestionConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import FxRate

log = get_logger(__name__)

SOURCE = "frankfurter"
PAIR = "EURUSD"

#: Loads the last persisted rate when the API is unreachable (set by the assembler).
LastKnownGoodLoader = Callable[[], Awaitable[FxRate | None]]


class FxClient:
    def __init__(
        self,
        fetcher: HttpFetcher,
        config: IngestionConfig,
        *,
        clock: Clock | None = None,
        cache_ttl_seconds: int = 3600,
        last_known_good: LastKnownGoodLoader | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._config = config
        self._clock = clock or SystemClock()
        self._ttl = timedelta(seconds=cache_ttl_seconds)
        self._last_known_good = last_known_good
        self._cached: FxRate | None = None

    async def fetch(self) -> FxRate:
        """Cached rate while fresh, else refetch, else the last known good rate."""
        now = self._clock.now()
        if self._cached is not None and now - self._cached.fetched_at < self._ttl:
            return self._cached

        try:
            rate = await self._fetch_live()
        except SourceUnavailable as exc:
            fallback = await self._fallback()
            if fallback is None:
                raise
            log.warning("fx.last_known_good", reason=exc.reason, rate=str(fallback.rate))
            return fallback

        self._cached = rate
        return rate

    async def fetch_quotes(self, currencies: tuple[str, ...]) -> dict[str, FxRate]:
        """EUR -> each currency, in **one** request, keyed by pair (``EURJPY``).

        No cache and no last-known-good of its own: the forex cycle calls this once per
        cycle, and a stale sizing rate is the one thing §12 says must produce **no
        signal** rather than a plausible one. The caller persists what it gets, so the
        stored rate stays available to whatever wants a fallback — but nothing here
        will hand back an old number as though it were current.

        A currency the payload does not carry is **absent from the result**, not zero
        and not defaulted. The gate then rejects that instrument with
        ``FX_RATE_UNAVAILABLE``, which is §12's "no sizing, so no signal".
        """
        if not currencies:
            return {}
        payload: Any = await self._fetcher.get_json(
            self._config.frankfurter_url,
            source=SOURCE,
            params={"base": "EUR", "symbols": ",".join(sorted(set(currencies)))},
        )
        rates = (payload or {}).get("rates") or {}
        fetched_at = self._clock.now()
        out: dict[str, FxRate] = {}
        for currency in sorted(set(currencies)):
            value = rates.get(currency)
            if value is None:
                log.warning("fx.quote_missing", currency=currency, source=SOURCE)
                continue
            out[f"EUR{currency}"] = FxRate(
                source=SOURCE,
                fetched_at=fetched_at,
                pair=f"EUR{currency}",
                rate=Decimal(str(value)),
                is_last_known_good=False,
            )
        return out

    async def _fetch_live(self) -> FxRate:
        payload: Any = await self._fetcher.get_json(
            self._config.frankfurter_url,
            source=SOURCE,
            params={"base": "EUR", "symbols": "USD"},
        )
        rate = ((payload or {}).get("rates") or {}).get("USD")
        if rate is None:
            raise SourceUnavailable(SOURCE, "no EURUSD rate in payload")

        return FxRate(
            source=SOURCE,
            fetched_at=self._clock.now(),
            pair=PAIR,
            rate=Decimal(str(rate)),
            is_last_known_good=False,
        )

    async def _fallback(self) -> FxRate | None:
        if self._cached is not None:
            return self._cached.model_copy(update={"is_last_known_good": True})
        if self._last_known_good is None:
            return None
        stored = await self._last_known_good()
        if stored is None:
            return None
        return stored.model_copy(update={"is_last_known_good": True})

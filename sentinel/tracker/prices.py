"""Getting the candles a tick needs, and choosing how far back to look.

Two timeframes, for two different questions:

* **1m high/low** — did a limit fill, did the stop go? The in-progress candle is
  included: its high and low are facts about where price has already been, and
  waiting a minute to admit them would reintroduce exactly the blind spot the
  candle source exists to remove.
* **closed 1h** — did the invalidation level break on a *close*? The in-progress
  candle is dropped here, because a close is not a close until it closes. This is
  the same rule M2's feature engine applies for the same reason (no repainting).

**How far back.** ``fill_lookback_candles`` is a floor, not the answer. A tick that
runs 60s after the last one needs a couple of minutes of history; a tick that runs
after the process was down for three hours needs three hours, or every fill in
that window is invisible forever. So the window is derived from
``last_checked_at`` and the floor only applies when there is nothing to derive it
from — a signal's first tick. That is what makes ARCHITECTURE §6's "tracker
rebuilds state from DB" recover a *gap* and not merely a process.
"""

from __future__ import annotations

from datetime import datetime
from math import ceil
from typing import Protocol

from sentinel.core.config import TrackerConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.models import Candle, OHLCVSeries

log = get_logger(__name__)

#: Binance's per-request kline ceiling. A gap longer than this is reported rather
#: than silently half-covered.
MAX_CANDLES = 1000

#: Saxo's is 1200 (``ForexConfig.max_count``), and over-requesting **clamps silently**
#: to it rather than erroring — spike defect D-e. So the ceiling is a constructor
#: argument from M10c rather than a module constant: a forex feed that kept Binance's
#: 1000 would under-request by 200 bars, which is not wrong so much as arbitrary, and a
#: forex feed that guessed higher would get a short read that looks like a quiet market.
DEFAULT_MAX_CANDLES = MAX_CANDLES

MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


class CandleSource(Protocol):
    """The one method the tracker needs from a market adapter."""

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries: ...


def lookback(
    *,
    since: datetime | None,
    now: datetime,
    minimum: int,
    timeframe_minutes: int = 1,
    maximum: int = MAX_CANDLES,
) -> int:
    """How many candles this tick must request, to cover the window since ``since``.

    ``since`` is the last tick, or — on a signal's **first** tick — when the signal
    was created. That distinction was a real bug, found by the first live run: a
    signal published two hours before the tracker existed had no ``last_checked_at``,
    so the window fell back to the five-candle floor, the tracker saw only the
    aftermath, and it recorded an invalidation for a ladder that had in fact filled
    and stopped out hours earlier. A tracker that starts watching a signal must
    look at the whole of the signal's life, not at the last five minutes of it.

    ``+1`` because the candle containing ``since`` is only partly covered by
    whatever looked last — dropping it would lose a fill in the seconds after.
    """
    if since is None:
        return minimum
    elapsed = (now - since).total_seconds() / 60
    needed = ceil(elapsed / timeframe_minutes) + 1
    return max(minimum, min(needed, maximum))


class PriceFeed:
    """Candles for one tick, per symbol.

    **Requests are memoised within a tick (M8.1).** Since one shared analysis now
    produces one signal per approved user, several open signals routinely differ only
    in whose they are: same symbol, same timeframe, same window. Fetched naively that
    is N identical calls to a rate-limited public endpoint every 60 seconds, growing
    with the number of members. The cache is keyed on
    ``(symbol, timeframe, limit)`` — the whole of what determines the answer — and
    :meth:`reset` clears it at the top of each tick, because candles go stale in
    exactly one minute and a feed that remembered them across ticks would be the
    polled-mark-price bug M7 removed, reintroduced through a cache.
    """

    def __init__(
        self,
        source: CandleSource,
        config: TrackerConfig,
        *,
        max_candles: int = DEFAULT_MAX_CANDLES,
    ) -> None:
        self._source = source
        self._config = config
        #: This venue's per-request ceiling — Binance 1000, Saxo 1200.
        self._max_candles = max_candles
        self._cache: dict[tuple[str, str, int], tuple[Candle, ...]] = {}

    def reset(self) -> None:
        """Forget this tick's candles. Called at the start of every tick."""
        self._cache.clear()

    async def _fetch(self, symbol: str, timeframe: str, limit: int) -> tuple[Candle, ...]:
        key = (symbol, timeframe, limit)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        series = await self._source.ohlcv(symbol, timeframe, limit)
        self._cache[key] = series.candles
        return series.candles

    async def fill_candles(
        self, symbol: str, *, since: datetime | None, now: datetime
    ) -> tuple[Candle, ...]:
        minutes = MINUTES.get(self._config.fill_timeframe, 1)
        limit = lookback(
            since=since,
            now=now,
            minimum=self._config.fill_lookback_candles,
            timeframe_minutes=minutes,
            maximum=self._max_candles,
        )
        if since is not None and limit >= self._max_candles:
            log.warning(
                "tracker.lookback_capped",
                symbol=symbol,
                since=since.isoformat(),
                limit=limit,
                detail="the gap since the last tick exceeds one request; "
                "fills older than the window cannot be detected",
            )
        return await self._fetch(symbol, self._config.fill_timeframe, limit)

    async def invalidation_candles(self, symbol: str, *, limit: int = 3) -> tuple[Candle, ...]:
        """Closed candles on the invalidation timeframe, oldest first.

        The last row from the exchange is the in-progress candle and is dropped:
        specs/TELEGRAM_UX.md §4 measures invalidation on a *close*.
        """
        candles = await self._fetch(symbol, self._config.invalidation_timeframe, limit + 1)
        return candles[:-1]


__all__ = ["DEFAULT_MAX_CANDLES", "MAX_CANDLES", "CandleSource", "PriceFeed", "lookback"]

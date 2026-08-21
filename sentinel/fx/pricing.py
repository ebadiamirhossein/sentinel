"""The tracker's view of Saxo — the one place forex's candle reads differ (M10d).

``PriceFeed`` asks a :class:`~sentinel.tracker.prices.CandleSource` two very different
questions with the same method, and Binance happens to answer both correctly by
returning the in-progress candle last:

* **1m for fills and stop-outs** — it wants the in-progress bar *included*. Its high
  and low are prices that have already traded, and M7 reads them precisely so a
  60-second poll cannot miss the wick that filled a rung or hit the stop.
* **1h for invalidation** — it wants closed bars only, and drops the last row itself
  because on Binance that row is the forming one. specs/TELEGRAM_UX.md §4 words
  invalidation as a *close*, and specs/PROMPTS.md §2 rule e tells the analyst that a
  wick-only breach is noise.

Saxo answers neither correctly, and the reason is the same for both: it sends **no
closed flag**, so :meth:`SaxoForexAdapter.fetch_tail` applies §4.1's clock rule and
drops the forming bar itself. Composed naively (journal/M10d_REPORT.md, join 2) that
gave:

* a **1m read two minutes stale on every tick** — ``now >= T + H + grace`` with a
  1-minute horizon and 30 s of grace leaves the newest closed bar 120 s old, so the
  tracker never saw the minute it was running in;
* an **invalidation read an hour behind** — the forming bar dropped by the adapter and
  then the last row dropped again by ``PriceFeed``, two drops for one bar, discarding
  the newest closed hour, which is the only hour an invalidation is ever measured on.

Neither produced an error. Both would have been the quiet, permanent behaviour of the
forex tracker.

**So this class answers each question the way it was actually meant.** It is a
``CandleSource``, it lives in ``sentinel/fx/``, and ``sentinel/tracker/`` and the
crypto path are untouched — the same precedent ``fx/rounding.py`` set, and for the same
reason: new-market arithmetic does not get to reshape the module the measured market
depends on.

**Why the invalidation read appends the forming bar rather than asking for one more.**
``PriceFeed.invalidation_candles`` drops ``candles[-1]`` by contract. The bar handed
back to be dropped is Saxo's own still-forming row, straight out of the response — not
a fabricated one, and not a closed bar sacrificed to satisfy a slice. §2.1's rule
holds: nothing here invents a candle, and if the venue returns no forming bar the
series is passed through as it came.
"""

from __future__ import annotations

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import SAXO_HORIZONS, ForexConfig, TrackerConfig
from sentinel.core.logging import get_logger
from sentinel.ingestion.adapters.forex_saxo import SaxoForexAdapter, is_closed
from sentinel.ingestion.models import Candle, OHLCVSeries

log = get_logger(__name__)


class ForexCandleSource:
    """A ``CandleSource`` over ``SaxoForexAdapter``, with §4.1 applied per question."""

    def __init__(
        self,
        adapter: SaxoForexAdapter,
        config: ForexConfig,
        tracker: TrackerConfig,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._clock = clock or SystemClock()
        #: Which timeframe is which question. Read from config rather than hardcoded
        #: as ``"1m"``/``"1h"``, so changing ``tracker.fill_timeframe`` cannot silently
        #: send the fill read down the invalidation branch.
        self._fill_timeframe = tracker.fill_timeframe
        self._invalidation_timeframe = tracker.invalidation_timeframe

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        """The **bid** series (§7.5), shaped for whichever read is asking."""
        if timeframe == self._fill_timeframe:
            tail = await self._adapter.fetch_tail(symbol, timeframe, limit, include_forming=True)
            return tail.bid
        if timeframe == self._invalidation_timeframe:
            return await self._invalidation(symbol, timeframe, limit)
        # Any other timeframe is not one of PriceFeed's two questions, so the plain
        # closed-only read is the right answer and the safe default.
        return await self._adapter.ohlcv(symbol, timeframe, limit)

    async def _invalidation(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        """Closed bars with **one** of the venue's forming bars last, for the caller to drop.

        One request, not two. The bars come back time-ordered and closedness is
        monotonic in time, so the closed ones are a prefix — and within the 30-second
        grace after an hour boundary there can be *two* unclosed bars, which is why
        this counts them rather than assuming the last one.
        """
        series = (
            await self._adapter.fetch_tail(symbol, timeframe, limit, include_forming=True)
        ).bid
        horizon = SAXO_HORIZONS[timeframe]
        now = self._clock.now()
        closed: list[Candle] = []
        forming: list[Candle] = []
        for candle in series.candles:
            done = is_closed(candle.open_time, horizon=horizon, now=now, config=self._config)
            (closed if done else forming).append(candle)
        if not forming:
            # Every bar the venue returned has closed — the market shut inside the
            # window. There is no forming row to sacrifice, so the caller's slice eats
            # a real close and this read is one short. Logged rather than papered over
            # with an invented bar (§2.1), and it degrades to the old behaviour.
            log.debug(
                "forex.invalidation_no_forming_bar",
                symbol=symbol,
                timeframe=timeframe,
                closed=len(closed),
            )
            return series
        return _replace_candles(series, (*closed, forming[-1]))


def _replace_candles(series: OHLCVSeries, candles: tuple[Candle, ...]) -> OHLCVSeries:
    return series.model_copy(update={"candles": candles})


__all__ = ["ForexCandleSource"]

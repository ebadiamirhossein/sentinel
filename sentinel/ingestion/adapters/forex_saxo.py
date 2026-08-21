"""Saxo Bank OpenAPI adapter for FxSpot (docs/specs/FOREX.md §4).

Implements the same :class:`~sentinel.ingestion.adapters.protocol.MarketDataAdapter`
the Binance adapter does, so the pipeline shape does not change. What differs is
everything the venue does not have: no derivatives context, no order book, no
volume of any kind, and no ``InstrumentMeta`` — forex precision is a pip and a
minimum *trade size*, which is a different object (:class:`ForexInstrument`).

**Read-only, and structurally so.** The only Saxo service groups reached here are
``/ref`` and ``/chart``. No order endpoint is imported, named or reachable from this
module, and the app registration itself was created with the trading checkbox
unchecked (CLAUDE.md's hard constraint, PRD §3).

Three things this module is careful about, each because the live spike caught a
version of it:

**Paging is forbidden (§4.4, D-d/D-e).** Every tail we need fits in one request —
the largest is 1200 against a ceiling of 1200. That is not merely simpler, it is
safer: the chart series is *not stable across queries*, so the same hour can be
present or absent depending on the request's anchor and ``Count``, and a page
boundary is exactly where that would produce a tail with a hole. So: one request,
**no anchor specified at all** (no ``Mode``, no ``Time`` — just the newest ``Count``
bars), and an assertion that the count returned is the count asked for, because an
over-request clamps silently rather than erroring.

**There is no closed flag (§4.1).** A bar stamped ``T`` on horizon ``H`` is closed
iff ``now >= T + H + grace``. Feeding a forming bar to RSI, ATR or the EMA stack
makes every indicator wrong on the newest candle — plausibly wrong, with no error.

**Precision comes from reference data only (§4.2, D-h).** Chart v3 returns empty
``ChartInfo`` and ``DisplayAndFormat`` objects, so there is nothing to read there
even by accident, and the pip is cross-checked against ``TickSize x 10`` before it
can exist. See :mod:`sentinel.fx.instruments`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from sentinel.core.clock import Clock, SystemClock
from sentinel.core.config import SAXO_HORIZONS, ForexConfig
from sentinel.core.logging import get_logger
from sentinel.core.markets import Market
from sentinel.fx.errors import DegradedRead, InstrumentUnresolved
from sentinel.fx.instruments import ForexInstrument, instrument_from_details, resolve_uic
from sentinel.ingestion.errors import SourceUnavailable
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import (
    BookSnapshot,
    Candle,
    DerivContext,
    InstrumentMeta,
    MarketHours,
    OHLCVSeries,
)

log = get_logger(__name__)

SOURCE = "saxo_fxspot"
ASSET_TYPE = "FxSpot"

#: The four price fields, each in a bid and an ask flavour. Present on every row of
#: every chart response, on LIVE and SIM alike. Named as a constant because a
#: missing one is a contract change and must raise rather than default.
_PRICE_FIELDS = ("Open", "High", "Low", "Close")


class AccessTokenProvider(Protocol):
    """Whatever can hand this adapter a currently-valid bearer token.

    A seam rather than a concrete dependency: the real implementation
    (``ingestion/clients/saxo_auth.py``) refreshes on a five-minute cadence against a
    twenty-minute token and persists a single-use rotating credential, none of which
    a chart read should know about.
    """

    async def access_token(self) -> str: ...


class ForexTail(BaseModel):
    """One instrument, one timeframe: the closed bid and ask series, and how they came.

    Both sides travel together because they arrive together — a chart row carries
    ``OpenBid`` and ``OpenAsk`` alike — and because separating them would mean either
    a second request or a spread measured against a differently-anchored series.

    Features are computed from **bid** (owner decision, §7.5). The ask is what makes
    the spread measurable, and the spread is the one thing forex has that crypto's
    order book was standing in for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timeframe: str
    horizon_minutes: int
    #: What was asked for, before the forming bar was dropped. The assertion that
    #: this many rows came back is the guard against D-e's silent clamp.
    requested: int
    bid: OHLCVSeries
    ask: OHLCVSeries
    #: True when the newest row was still forming and was dropped (§4.1).
    forming_dropped: bool
    #: The distinct UTC hours the bars in this tail start on. **Observed, never
    #: assumed**: 4h and 1d are anchored to 17:00 America/New_York and shift with US
    #: daylight saving (D-k), so this is 01/05/09/13/17/21 in August and
    #: 02/06/10/14/18/22 in January. Recorded so a chart or a comparison can read the
    #: real grid instead of guessing one, and so nothing may assume forex and crypto
    #: share a 4h boundary. They never do.
    alignment_hours_utc: tuple[int, ...]


class SaxoForexAdapter:
    """FxSpot market data from Saxo. Chart and reference endpoints only."""

    def __init__(
        self,
        config: ForexConfig,
        *,
        fetcher: HttpFetcher,
        tokens: AccessTokenProvider,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._tokens = tokens
        self._clock = clock or SystemClock()
        self._instruments: dict[str, ForexInstrument] = {}

    # ── instrument resolution ────────────────────────────────────────────────

    async def resolve(self, symbol: str) -> ForexInstrument:
        """Resolve one symbol to a Uic and a **cross-checked** pip (§4.2).

        Cached in process for the life of the adapter. No Uic is hardcoded anywhere
        in this package: the number comes from the search response, whatever it is,
        and an unresolvable symbol raises with the reason named rather than falling
        back to something plausible.
        """
        cached = self._instruments.get(symbol)
        if cached is not None:
            return cached

        search = await self._get(
            "/ref/v1/instruments",
            params={"Keywords": symbol, "AssetTypes": ASSET_TYPE},
        )
        uic = resolve_uic(search, symbol=symbol)

        details = await self._get(
            "/ref/v1/instruments/details",
            params={"Uics": str(uic), "AssetTypes": ASSET_TYPE},
        )
        entry = _details_for(details, symbol=symbol, uic=uic)
        instrument = instrument_from_details(
            entry, symbol=symbol, uic=uic, resolved_at=self._clock.now()
        )
        self._instruments[symbol] = instrument
        log.info(
            "forex.instrument_resolved",
            symbol=symbol,
            uic=instrument.uic,
            decimals=instrument.decimals,
            pip=str(instrument.pip),
            tick_size=str(instrument.tick_size),
            min_trade_size=str(instrument.min_trade_size),
        )
        return instrument

    async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, ForexInstrument]:
        """Resolve a watchlist, skipping what cannot be resolved **with a reason**."""
        resolved: dict[str, ForexInstrument] = {}
        for symbol in symbols:
            try:
                resolved[symbol] = await self.resolve(symbol)
            except (InstrumentUnresolved, SourceUnavailable) as exc:
                log.warning("forex.instrument_skipped", symbol=symbol, reason=str(exc))
        return resolved

    # ── candles ──────────────────────────────────────────────────────────────

    async def fetch_tail(self, symbol: str, timeframe: str, limit: int) -> ForexTail:
        """The newest ``limit`` bars, bid and ask, with the forming one dropped.

        The primitive every other candle read is built on. One request, no anchor:
        ``Mode`` and ``Time`` are deliberately never sent, so the series cannot vary
        with an anchor the way D-d showed it can.
        """
        horizon = _horizon_for(timeframe)
        if limit > self._config.max_count:
            raise DegradedRead(
                f"{symbol} {timeframe}: {limit} candles requested against a "
                f"{self._config.max_count} ceiling — Saxo would clamp this silently "
                f"rather than erroring, and paging is forbidden (§4.4)"
            )
        instrument = await self.resolve(symbol)
        payload = await self._get(
            "/chart/v3/charts",
            params={
                "AssetType": ASSET_TYPE,
                "Uic": str(instrument.uic),
                "Horizon": str(horizon),
                "Count": str(limit),
            },
        )
        rows = _rows(payload, symbol=symbol, timeframe=timeframe)

        # D-e. An over-requested Count clamps to the ceiling, and a range with fewer
        # bars available returns what it has. Neither is a shorter tail we may
        # analyse: both are a degraded read.
        if len(rows) != limit:
            raise DegradedRead(
                f"{symbol} {timeframe}: asked for {limit} candles and got {len(rows)}. "
                f"This is a degraded read, not a shorter tail — a silent clamp and a "
                f"short window are indistinguishable from here (§4.4, D-e)"
            )

        now = self._clock.now()
        closed = [row for row in rows if _is_closed(row, horizon, now, self._config)]
        forming_dropped = len(closed) != len(rows)
        if not closed:
            raise DegradedRead(
                f"{symbol} {timeframe}: every returned bar is still forming at {now.isoformat()}"
            )

        fetched_at = now
        return ForexTail(
            symbol=symbol,
            timeframe=timeframe,
            horizon_minutes=horizon,
            requested=limit,
            bid=_series(closed, symbol=symbol, timeframe=timeframe, side="Bid", at=fetched_at),
            ask=_series(closed, symbol=symbol, timeframe=timeframe, side="Ask", at=fetched_at),
            forming_dropped=forming_dropped,
            alignment_hours_utc=tuple(sorted({row["at"].hour for row in closed})),
        )

    # ── MarketDataAdapter ────────────────────────────────────────────────────

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        """The **bid** series (§7.5). Ask travels with it — see :meth:`fetch_tail`."""
        return (await self.fetch_tail(symbol, timeframe, limit)).bid

    async def derivatives_context(self, symbol: str) -> DerivContext | None:
        """None. Forex has no funding, no open interest and no long/short ratio.

        Saxo's order and position book endpoints were discontinued, so this is not a
        gap to fill later — the data does not exist to be fetched (§2). Returning
        ``None`` is how the snapshot says so; a zero-filled context would say
        something false.
        """
        return None

    async def orderbook_snapshot(self, symbol: str) -> BookSnapshot | None:
        """None. There is no exchange depth for OTC spot FX.

        Its replacement is the **measured spread**, which the bid/ask tails carry.
        """
        return None

    async def instrument_meta(self, symbol: str) -> InstrumentMeta | None:
        """None — forex precision does not fit this shape, and is not forced into it.

        :class:`~sentinel.ingestion.models.InstrumentMeta` is a tick size, a quantity
        step, a minimum **notional** and a contract size. Forex has a pip, no lot
        rounding at all (``LotSize: null``, ``LotSizeType: NotUsed``) and a minimum
        **trade size** in base units — a different quantity that would be wrong in
        the ``min_notional`` field rather than merely unfamiliar.

        So the answer is that this market has no ``InstrumentMeta``, not a plausible
        one assembled from near-misses. :meth:`resolve` returns the object that does
        fit.
        """
        return None

    def market_hours(self) -> MarketHours:
        """Forex is not always open. Sessions arrive with :mod:`sentinel.fx.hours`."""
        return MarketHours(always_open=False, venue=SOURCE)

    async def close(self) -> None:
        """Nothing to close: the HTTP client is owned by the caller that built it."""
        return None

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _get(self, path: str, *, params: dict[str, str]) -> Any:
        token = await self._tokens.access_token()
        return await self._fetcher.get_json(
            f"{self._config.base_url}{path}",
            source=SOURCE,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )


def _horizon_for(timeframe: str) -> int:
    try:
        return SAXO_HORIZONS[timeframe]
    except KeyError as exc:
        raise ValueError(
            f"no Saxo Horizon for timeframe {timeframe!r}; known: "
            f"{', '.join(sorted(SAXO_HORIZONS))}"
        ) from exc


def _details_for(payload: Any, *, symbol: str, uic: int) -> Any:
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise InstrumentUnresolved(f"{symbol}: instrument details returned no Data array")
    for entry in data:
        if isinstance(entry, dict) and entry.get("Uic") == uic:
            return entry
    raise InstrumentUnresolved(f"{symbol}: no details entry for Uic {uic}")


def _rows(payload: Any, *, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    """Parse a chart response into ``{at, OpenBid, ..., CloseAsk}`` dicts.

    Every one of the eight price fields is required. A missing field is a contract
    change at the venue and must be loud: silently defaulting one would produce a
    spread, a range or a body that is quietly wrong.
    """
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SourceUnavailable(SOURCE, f"{symbol} {timeframe}: chart response has no Data array")

    rows: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            raise SourceUnavailable(SOURCE, f"{symbol} {timeframe}: chart row is not an object")
        row: dict[str, Any] = {"at": _parse_time(raw.get("Time"), symbol=symbol)}
        for field in _PRICE_FIELDS:
            for side in ("Bid", "Ask"):
                key = f"{field}{side}"
                value = raw.get(key)
                if value is None:
                    raise SourceUnavailable(
                        SOURCE, f"{symbol} {timeframe}: chart row at {row['at']} has no {key}"
                    )
                row[key] = Decimal(str(value))
        rows.append(row)

    rows.sort(key=lambda row: row["at"])
    return rows


def _parse_time(value: Any, *, symbol: str) -> datetime:
    if not isinstance(value, str):
        raise SourceUnavailable(SOURCE, f"{symbol}: chart row has no Time")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SourceUnavailable(SOURCE, f"{symbol}: unparseable Time {value!r}") from exc
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_closed(row: dict[str, Any], horizon: int, now: datetime, config: ForexConfig) -> bool:
    """§4.1's rule, and the whole of it: ``now >= T + H + grace``."""
    opened_at: datetime = row["at"]
    closes_at = opened_at + timedelta(minutes=horizon, seconds=config.candle_grace_seconds)
    return now >= closes_at


def _series(
    rows: list[dict[str, Any]], *, symbol: str, timeframe: str, side: str, at: datetime
) -> OHLCVSeries:
    return OHLCVSeries(
        source=SOURCE,
        fetched_at=at,
        symbol=symbol,
        timeframe=timeframe,
        market=Market.FOREX,
        candles=tuple(
            Candle(
                open_time=row["at"],
                open=row[f"Open{side}"],
                high=row[f"High{side}"],
                low=row[f"Low{side}"],
                close=row[f"Close{side}"],
                # Absent, not zero. Saxo publishes no volume field of any kind — not
                # even tick counts — and OHLCVSeries refuses a forex candle that
                # carries one (specs/FOREX.md §2.1).
                volume=None,
            )
            for row in rows
        ),
    )


__all__ = ["SOURCE", "AccessTokenProvider", "ForexTail", "SaxoForexAdapter"]

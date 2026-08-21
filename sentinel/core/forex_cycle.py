"""Assembling a forex cycle: tails in, snapshots and features out (FOREX.md §14 step 8).

Forex does **not** go through :class:`~sentinel.ingestion.assembler.SnapshotAssembler`,
and the two reasons are not stylistic.

1. **The staleness rule is a different rule.** Spec defect #14: crypto's
   ``ingestion/staleness`` measures from ``OHLCVSeries.fetched_at``, which is always
   ~now for a fresh fetch. Applied to forex it would call a weekend snapshot whose
   newest bar is fifty hours old perfectly fresh. Forex needs recency measured from
   the *candle* — :func:`sentinel.fx.hours.candles_are_stale` — and that check must be
   **skipped while the market is shut**, because closed is a normal state and not a
   fault.

2. **Bid and ask have to be one read.** The spread is measured as ``CloseAsk -
   CloseBid`` per bar, so the two sides must come from the same request. D-d found the
   same hour present or absent depending on the request's anchor, which makes two
   reads of "the same" window a genuine hazard rather than a theoretical one. The
   adapter's :meth:`~sentinel.ingestion.adapters.forex_saxo.SaxoForexAdapter.ohlcv`
   returns the bid alone, so this module uses ``fetch_tail`` and derives everything —
   snapshot, features and spread — from that single read.

**Where this stops.** At an analyst report. ``sentinel/risk/``'s gate and ``TradePlan``
are crypto-shaped (spec defect #12), and the card, publishing and tracking are M10c.
So a forex cycle assembles, computes, renders, asks the analyst and records the answer.
It does not size, gate, publish or track, and it says so in a log line rather than
falling through to a crypto path that would misread every number it touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sentinel.charts.models import AnnotationKind, ChartAnnotation
from sentinel.core.config import AppConfig
from sentinel.core.logging import get_logger
from sentinel.features import compute as compute_features
from sentinel.features.engine import attach as attach_features
from sentinel.features.models import SymbolFeatures
from sentinel.fx.errors import DegradedRead, ForexError
from sentinel.fx.features import ForexFeatures, SymbolTails, compute_cycle_features
from sentinel.fx.hours import MarketState, candles_are_stale, state_at
from sentinel.fx.sessions import Session, session_bands
from sentinel.ingestion.adapters.forex_saxo import ForexTail, SaxoForexAdapter
from sentinel.ingestion.models import DataQuality, MarketSnapshot

log = get_logger(__name__)

#: The exact reason string a closed market produces, so the orchestrator can map it
#: to :attr:`SkipReason.MARKET_CLOSED` without matching on prose.
MARKET_CLOSED_REASON = "market closed"

#: Which forex feature maps onto which mark on the chart (§6). Session shading is
#: added separately because it is an interval rather than a price.
_LINE_SOURCES: tuple[tuple[AnnotationKind, str, str, str], ...] = (
    (AnnotationKind.PRIOR_DAY_HIGH, "prior_day", "high", "PDH"),
    (AnnotationKind.PRIOR_DAY_LOW, "prior_day", "low", "PDL"),
    (AnnotationKind.PRIOR_WEEK_HIGH, "prior_week", "high", "PWH"),
    (AnnotationKind.PRIOR_WEEK_LOW, "prior_week", "low", "PWL"),
)


@dataclass
class ForexAssembly:
    """One cycle's worth of forex, per symbol."""

    snapshots: list[MarketSnapshot] = field(default_factory=list)
    features: dict[str, SymbolFeatures] = field(default_factory=dict)
    forex_features: dict[str, ForexFeatures] = field(default_factory=dict)
    annotations: dict[str, tuple[ChartAnnotation, ...]] = field(default_factory=dict)
    #: Symbol -> why it was skipped. Never silent (§4.2, §12).
    skipped: dict[str, str] = field(default_factory=dict)


async def fetch_tails(
    adapter: SaxoForexAdapter, symbol: str, *, config: AppConfig
) -> dict[str, ForexTail]:
    """Every configured timeframe for one symbol, in one request each.

    Paging is forbidden (§4.4) and the adapter refuses an over-request before making
    it, because Saxo clamps ``Count`` to 1200 silently.
    """
    specs = config.forex.timeframes
    tails = await asyncio.gather(
        *(adapter.fetch_tail(symbol, spec.timeframe, spec.candles) for spec in specs)
    )
    return {tail.timeframe: tail for tail in tails}


def chart_annotations(
    features: ForexFeatures, *, window_start: datetime, window_end: datetime
) -> tuple[ChartAnnotation, ...]:
    """The marks §6 asks for: prior-period levels, the period opens, session shading.

    Built from the computed features rather than recomputed, so the line on the chart
    and the number in the payload cannot disagree — the same discipline the EMA
    overlays already follow. A level the features do not have produces **no
    annotation**, rather than one at zero (§2.1).
    """
    marks: list[ChartAnnotation] = []
    for kind, section, attribute, label in _LINE_SOURCES:
        levels = getattr(features, section)
        if levels is None:
            continue
        marks.append(ChartAnnotation(kind=kind, label=label, price=getattr(levels, attribute)))
    for kind, value, label in (
        (AnnotationKind.DAILY_OPEN, features.daily_open, "DO"),
        (AnnotationKind.WEEKLY_OPEN, features.weekly_open, "WO"),
    ):
        if value is not None:
            marks.append(ChartAnnotation(kind=kind, label=label, price=value))

    marks.extend(
        ChartAnnotation(
            kind=AnnotationKind.SESSION_BAND,
            label=Session.LONDON_NY_OVERLAP.value,
            from_at=start,
            to_at=end,
        )
        for start, end in session_bands(window_start, window_end)
    )
    return tuple(marks)


async def assemble_forex(
    adapter: SaxoForexAdapter,
    symbols: list[str],
    *,
    config: AppConfig,
    now: datetime,
    cycle_id: UUID | None = None,
) -> ForexAssembly:
    """Fetch, compute and assemble one cycle. Per-symbol failures skip that symbol.

    Isolation is the load-bearing property, exactly as it is in the crypto assembler:
    one instrument's failed read must never cost the other two their cycle.
    """
    assembly = ForexAssembly()
    if state_at(now, config.forex) is MarketState.CLOSED:
        # Not a fault and not a degradation. §5.1: closed is a normal state with its
        # own reason code, and the candle-recency check below must not run inside it.
        log.info("forex.market_closed", detail="cycle skipped", symbols=len(symbols))
        assembly.skipped = dict.fromkeys(symbols, MARKET_CLOSED_REASON)
        return assembly

    instruments = await adapter.resolve_many(tuple(symbols))
    tails_by_symbol: dict[str, SymbolTails] = {}

    for symbol in symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            assembly.skipped[symbol] = "instrument unresolved"
            continue
        try:
            tails = await fetch_tails(adapter, symbol, config=config)
        except DegradedRead as exc:
            assembly.skipped[symbol] = f"degraded read: {exc}"
            log.warning("forex.symbol_skipped", symbol=symbol, reason=str(exc))
            continue
        except (ForexError, TimeoutError, ValueError, KeyError) as exc:
            assembly.skipped[symbol] = f"{type(exc).__name__}: {exc}"
            log.warning("forex.symbol_skipped", symbol=symbol, reason=str(exc))
            continue

        stale = _stale_timeframes(tails, now=now, config=config)
        if stale:
            # Measured from the candle, not the fetch — spec defect #14 — and only
            # ever asked while the market is open, which the guard above ensures.
            assembly.skipped[symbol] = f"stale candles: {', '.join(stale)}"
            log.warning("forex.symbol_skipped", symbol=symbol, reason="stale candles", stale=stale)
            continue

        tails_by_symbol[symbol] = SymbolTails(
            symbol=symbol,
            pip=instrument.pip,
            bid={tf: tail.bid for tf, tail in tails.items()},
            ask={tf: tail.ask for tf, tail in tails.items()},
            alignment_hours_utc=_alignment_of(tails),
        )

    if not tails_by_symbol:
        return assembly

    assembly.forex_features = compute_cycle_features(
        tails_by_symbol,
        now=now,
        feature_candles_1h=config.forex.feature_candles_1h,
        spread_lookback=config.forex.spread_lookback_candles,
    )

    for symbol, symbol_tails in tails_by_symbol.items():
        snapshot = _snapshot_for(symbol, symbol_tails, config=config, now=now, cycle_id=cycle_id)
        computed = compute_features(snapshot, config.features)
        assembly.features[symbol] = computed
        assembly.snapshots.append(
            _attach_forex(snapshot, computed, assembly.forex_features[symbol])
        )
        # Bands are generated across every charted timeframe's span, not the 1h one:
        # a 120-bar 4h chart reaches twenty days back where a 120-bar 1h chart reaches
        # five, and each render clips to its own window anyway. Taken from the
        # snapshot rather than the raw tail, so the 1200-bar spread tail does not
        # produce fifty bands for a chart that can show four.
        starts = [series.candles[0].open_time for series in snapshot.ohlcv.values()]
        ends = [series.candles[-1].open_time for series in snapshot.ohlcv.values()]
        assembly.annotations[symbol] = chart_annotations(
            assembly.forex_features[symbol],
            window_start=min(starts),
            window_end=max(ends),
        )
    return assembly


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _stale_timeframes(
    tails: dict[str, ForexTail], *, now: datetime, config: AppConfig
) -> list[str]:
    return [
        timeframe
        for timeframe, tail in tails.items()
        if tail.bid.candles
        and candles_are_stale(
            tail.bid.candles[-1].open_time,
            timeframe=timeframe,
            now=now,
            config=config.forex,
        )
    ]


def _alignment_of(tails: dict[str, ForexTail]) -> tuple[int, ...]:
    """The observed 4h grid (D-k). Read from the data, never assumed, and empty rather
    than guessed when this cycle has no 4h tail to read it from."""
    tail = tails.get("4h")
    return () if tail is None else tail.alignment_hours_utc


def _snapshot_for(
    symbol: str,
    tails: SymbolTails,
    *,
    config: AppConfig,
    now: datetime,
    cycle_id: UUID | None,
) -> MarketSnapshot:
    """A forex snapshot, from bid candles, with the 1h tail trimmed for features.

    ``derivatives``, ``orderbook`` and ``instrument`` stay ``None``: this venue has no
    funding, no open interest and no order book, and the crypto ``InstrumentMeta`` has
    nowhere to put a Uic or a pip anyway (the same defect class as #12). They are
    absent, which is what §2.1 asks for — and they are absent for a market, not
    because a fetch failed, so ``data_quality`` stays OK.
    """
    from sentinel.fx.features import trim_to_feature_window

    ohlcv = dict(tails.bid)
    hourly = ohlcv.get("1h")
    if hourly is not None:
        ohlcv["1h"] = trim_to_feature_window(hourly, candles=config.forex.feature_candles_1h)

    primary = config.forex.timeframes[0].timeframe
    return MarketSnapshot(
        cycle_id=cycle_id,
        symbol=symbol,
        captured_at=now,
        last_price=ohlcv[primary].last_close,
        ohlcv=ohlcv,
        data_quality=DataQuality.OK,
    )


def _attach_forex(
    snapshot: MarketSnapshot, features: SymbolFeatures, forex: ForexFeatures
) -> MarketSnapshot:
    """Merge the forex block into the snapshot's feature dict.

    Inside ``features`` rather than as a new snapshot section, and deliberately: a new
    top-level field would have to join ``analyst/serialization.PASSTHROUGH``, which
    dumps with ``include=``, so crypto's payload would gain ``"forex": null`` and the
    crypto prompt golden would move. ``MarketSnapshot.features`` is already a free
    dict, so this costs crypto nothing.
    """
    attached = attach_features(snapshot, features)
    block = dict(attached.features or {})
    block["forex"] = forex.model_dump(mode="json")
    return attached.model_copy(update={"features": block})


__all__ = [
    "MARKET_CLOSED_REASON",
    "ForexAssembly",
    "assemble_forex",
    "chart_annotations",
    "fetch_tails",
]

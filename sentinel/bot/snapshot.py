"""``/snapshot`` — what the code computed, before any model was called (M8.6).

``/pulse`` and ``/pulse SOLUSDT`` (M8.4/M8.5) made the pipeline's *reasoning*
visible, and every word of it is model output: thesis, evidence, counter-thesis,
verdict. This is the other half, and it is the half with no AI in it — the regimes,
EMAs, RSI, ATR, relative volume, funding, open interest, support and resistance with
their touch counts, orderbook imbalance, Fear & Greed, BTC dominance and data-quality
flags that ``features/`` and ``ingestion/`` compute and store on every
``market_snapshots`` row, and that the analyst is then handed.

It has been in the database since M1 and readable only over SSH. The point of
surfacing it is that a reader can now check the analysis against its own inputs:
when a card claims "1h pullback into EMA50", the EMA50 the model was actually given
is six characters away.

**Shared market data.** Same access as ``/pulse``, every approved user — and unlike
``/pulse`` there is not even a spend line to differ on, so the owner and a member get
byte-identical text. The view type carries no user id, no capital, no sizing and no
decision, and there is nothing on a ``market_snapshots`` row that could supply one.

**Read-only, and no exchange round trip.** This reports what the pipeline measured
and when, not what is true this second — and it reads the newest stored snapshot
whatever its age, for M8.5 decision 3's reason: a window would turn a stale answer
into *no* answer, which reads as "never ingested" and is a different fact. The
capture time is on the card, so age is visible rather than hidden.

**Arithmetic lives here, not in ``cards.py``**, exactly as it does in ``bot/pulse.py``
— ``tests/bot/test_no_arithmetic.py`` scans the renderer and would fail on a single
subtraction. There is one derivation in this module (open interest's 24h change) and
two unit conversions (a funding rate into a percentage, and one instant into the
owner's timezone at render time).
"""

from __future__ import annotations

from decimal import Context, Decimal

from pydantic import BaseModel, ValidationError

from sentinel.bot.views import (
    SnapshotBookView,
    SnapshotDerivativesView,
    SnapshotLevelView,
    SnapshotMacroView,
    SnapshotSentimentView,
    SnapshotTimeframeView,
    SnapshotView,
)
from sentinel.core.logging import get_logger
from sentinel.features.models import LevelKind, SymbolFeatures
from sentinel.ingestion.models import BookSnapshot, DerivContext, MacroContext, SentimentContext
from sentinel.risk.rounding import percent
from sentinel.storage.models import MarketSnapshotRow

log = get_logger(__name__)

#: Shown wherever a figure was not computed. A word, never a blank and never a zero:
#: "the ATR could not be computed" and "the ATR is 0" are different facts, and this
#: card exists partly to show which of the two happened (CLAUDE.md: degrade
#: explicitly, never fabricate).
NA = "n/a"

#: The timeframes the card leads with, in the order a chart is read. Any other
#: timeframe present on the snapshot is appended rather than dropped — a config
#: change must not silently remove a row from a card whose whole job is completeness.
TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

#: How many support/resistance zones are listed, nearest first. The feature engine
#: clusters more than fit a phone; the card says how many it left out (M8.4 decision
#: 6 — a bounded list that does not say it is bounded reads as "that was everything").
MAX_LEVELS = 8

#: Significant digits for a price-like figure. Fixed decimal places cannot serve a
#: system that quotes BTC in the tens of thousands and small alts to eight places;
#: significant digits do.
#:
#: **Six, chosen against the live database rather than in the abstract.** Eight put
#: ``EMA20 6.3501787`` beside ``last 6.327`` on a real AVAXUSDT card — four digits
#: past that symbol's tick, which reads as noise rather than as precision. Six keeps
#: every timeframe's EMA at or past the tick for both ends of the range this system
#: trades (``63820.6`` for BTC, ``6.35018`` for AVAX) and is still far short of the
#: magnitude where Decimal would switch to scientific notation.
SIGNIFICANT = Context(prec=6)


def _sig(value: Decimal | None) -> str:
    """A price, EMA or ATR at :data:`SIGNIFICANT` digits. ``None`` → :data:`NA`.

    Two different inputs land here and both need trimming, in opposite directions.
    The feature engine converts through ``Decimal(str(float))`` and hands over
    ``82.54714285714286``; ``market_snapshots.last_price`` is ``Numeric(38, 18)``
    and comes back out of Postgres as ``64100.000000000000000000``. Sixteen digits
    of an EMA is noise on a phone and eighteen trailing zeros on a price reads as a
    bug — this is display only, and the stored value is untouched either way.

    ``normalize()`` strips the trailing zeros and would turn ``64100.000`` into
    ``6.41E+4``, which is the same number and renders as scientific notation on a
    card; an integral result is therefore re-quantized to a zero exponent. Exactly
    the dance ``risk/rounding.percent`` documents, for exactly the same reason.
    """
    if value is None:
        return NA
    reduced = SIGNIFICANT.plus(value).normalize()
    if reduced == reduced.to_integral_value():
        return str(reduced.quantize(Decimal(1)))
    return str(reduced)


def _fixed(value: Decimal | None, places: int) -> str:
    return NA if value is None else f"{value:.{places}f}"


def _signed(value: Decimal | None, places: int = 2) -> str:
    """A change, with its sign always shown — ``+1.94%`` reads differently to ``1.94%``
    beside a column of negatives."""
    return NA if value is None else f"{value:+.{places}f}%"


def _grouped(value: Decimal | None) -> str:
    """Open interest, which runs to nine figures in USD and is unreadable ungrouped."""
    return NA if value is None else f"{value:,.0f}"


def _part[T: BaseModel](model: type[T], payload: object, *, symbol: str, field: str) -> T | None:
    """One block of the stored context, back through the contract that wrote it.

    Validating rather than reading keys out of the dict means this card is reading
    the same model ``ingestion/`` produced, not a shape it assumes. A block that will
    not validate degrades to ``None`` and is **named as absent on the card** — the
    same posture ``pulse.symbol_pulse_view`` takes: a later schema change costs one
    section, never the whole card.
    """
    if payload is None:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        log.warning(
            "bot.snapshot_part_unreadable",
            symbol=symbol,
            field=field,
            detail="stored block does not match the current model; shown as unavailable",
        )
        return None


def _timeframe_view(features: SymbolFeatures, timeframe: str) -> SnapshotTimeframeView | None:
    tf = features.timeframes.get(timeframe)
    if tf is None:
        return None
    return SnapshotTimeframeView(
        timeframe=tf.timeframe,
        regime=tf.trend_regime.value,
        regime_basis=tf.regime_basis.value,
        volatility=tf.volatility_regime.value,
        ema20=_sig(tf.ema20),
        ema50=_sig(tf.ema50),
        ema200=_sig(tf.ema200),
        rsi14=_fixed(tf.rsi14, 1),
        atr14=_sig(tf.atr14),
        atr_pct=_fixed(tf.atr_pct, 2),
        relative_volume=_fixed(tf.relative_volume, 2),
        ema_stack=tf.ema_stack or NA,
        candles_used=tf.candles_used,
        partial_candle_dropped=tf.partial_candle_dropped,
    )


def _timeframes(features: SymbolFeatures) -> tuple[SnapshotTimeframeView, ...]:
    """:data:`TIMEFRAMES` in order, then anything else the snapshot happens to hold."""
    extra = sorted(name for name in features.timeframes if name not in TIMEFRAMES)
    views = (_timeframe_view(features, name) for name in (*TIMEFRAMES, *extra))
    return tuple(view for view in views if view is not None)


def _levels(features: SymbolFeatures) -> tuple[tuple[SnapshotLevelView, ...], int]:
    """The nearest :data:`MAX_LEVELS` zones, and how many were left out.

    Nearest first rather than highest first: a level 30% away is true and useless,
    and the ones that decide a stop or a target are the ones the price is near.
    """
    ordered = sorted(features.levels, key=lambda level: abs(level.distance_pct))
    shown = ordered[:MAX_LEVELS]
    views = tuple(
        SnapshotLevelView(
            kind="R" if level.kind is LevelKind.RESISTANCE else "S",
            price=_sig(level.price),
            timeframe=level.timeframe,
            touches=level.touches,
            distance_pct=_signed(level.distance_pct),
            strength=_fixed(level.strength, 2),
        )
        for level in shown
    )
    return views, len(ordered) - len(shown)


def _oi_change_pct(deriv: DerivContext) -> str:
    """Open interest now against open interest a day ago, as a percentage.

    The only genuine derivation in this module. ``DerivContext`` stores the 24h
    series and no delta — the feature engine never computed one, because nothing
    upstream needed it — so it is computed here rather than added to what the
    pipeline *writes*: this milestone is read-only, and nothing about a transparency
    command should be able to change a cycle.

    Two points are the minimum; one point is a reading, not a change.
    """
    points = deriv.open_interest_24h
    if len(points) < 2:
        return NA
    first = points[0].open_interest_base
    last = points[-1].open_interest_base
    if first <= 0:
        return NA
    return _signed(percent((last - first) / first * Decimal(100)))


def _derivatives(deriv: DerivContext | None) -> SnapshotDerivativesView | None:
    if deriv is None:
        return None
    return SnapshotDerivativesView(
        # Stored as a fraction, read as a percentage — 0.0000193 is 0.00193%. Four
        # decimal places because two would round every normal funding rate to 0.00%.
        funding_pct=f"{deriv.funding_rate * Decimal(100):+.4f}%",
        next_funding_at=deriv.next_funding_time,
        open_interest_base=_grouped(deriv.open_interest_base),
        open_interest_value=_grouped(deriv.open_interest_value),
        change_24h_pct=_oi_change_pct(deriv),
        points=len(deriv.open_interest_24h),
        long_short_ratio=_fixed(deriv.long_short_ratio, 2),
    )


def _book(book: BookSnapshot | None) -> SnapshotBookView | None:
    if book is None:
        return None
    return SnapshotBookView(
        imbalance=_fixed(book.imbalance, 4),
        spread_pct=_fixed(book.spread_pct, 4),
        best_bid=_sig(book.best_bid),
        best_ask=_sig(book.best_ask),
        depth_levels=book.depth_levels,
    )


def _sentiment(sentiment: SentimentContext | None) -> SnapshotSentimentView | None:
    if sentiment is None:
        return None
    delta = sentiment.delta
    return SnapshotSentimentView(
        value=sentiment.value,
        classification=sentiment.classification,
        delta=NA if delta is None else f"{delta:+d}",
    )


def _macro(macro: MacroContext | None) -> SnapshotMacroView | None:
    if macro is None:
        return None
    return SnapshotMacroView(
        btc_dominance_pct=_fixed(macro.btc_dominance_pct, 2),
        mcap_change_24h_pct=_signed(macro.total_mcap_change_24h_pct),
    )


def snapshot_view(
    row: MarketSnapshotRow | None, *, symbol: str, on_watchlist: bool
) -> SnapshotView:
    """The newest stored snapshot for one symbol, as the card's inputs.

    ``features`` lives in the same JSONB as the rest of the context and is validated
    back through :class:`SymbolFeatures`. When it will not validate the indicators
    are gone and the row's own columns — price, capture time, data quality — are
    not, so the card reports that rather than rendering an empty table.
    """
    if row is None:
        return SnapshotView(symbol=symbol, at=None, on_watchlist=on_watchlist)

    context = row.context if isinstance(row.context, dict) else {}
    features = _part(SymbolFeatures, context.get("features"), symbol=symbol, field="features")
    levels, dropped = _levels(features) if features is not None else ((), 0)

    return SnapshotView(
        symbol=row.symbol,
        at=row.captured_at,
        on_watchlist=on_watchlist,
        last_price=_sig(row.last_price),
        quality=row.data_quality,
        degraded_fields=tuple(row.degraded_fields or ()),
        timeframes=() if features is None else _timeframes(features),
        htf_regime="" if features is None else features.htf_regime.value,
        regime_aligned=None if features is None else features.regime_aligned,
        change_1h=NA if features is None else _signed(features.pct_change_1h),
        change_4h=NA if features is None else _signed(features.pct_change_4h),
        change_24h=NA if features is None else _signed(features.pct_change_24h),
        levels=levels,
        levels_dropped=dropped,
        nearest_support=NA if features is None else _sig(features.nearest_support),
        nearest_resistance=NA if features is None else _sig(features.nearest_resistance),
        derivatives=_derivatives(
            _part(DerivContext, context.get("derivatives"), symbol=symbol, field="derivatives")
        ),
        book=_book(_part(BookSnapshot, context.get("orderbook"), symbol=symbol, field="orderbook")),
        sentiment=_sentiment(
            _part(SentimentContext, context.get("sentiment"), symbol=symbol, field="sentiment")
        ),
        macro=_macro(_part(MacroContext, context.get("macro"), symbol=symbol, field="macro")),
        features_unreadable=features is None and context.get("features") is not None,
    )


__all__ = ["MAX_LEVELS", "NA", "TIMEFRAMES", "snapshot_view"]

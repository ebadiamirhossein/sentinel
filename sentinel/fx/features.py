"""What forex has instead of what it lost (FOREX.md §2, §6).

§2's ledger is honest about the trade: funding, open interest, long/short ratio and
volume all go, and only two of the four have a replacement. What forex gains is
structure — a day with a beginning, a week with a beginning, desks that open and shut,
three instruments that all cross the same dollar — plus the one genuinely good
replacement, the **measured** spread.

Everything here is computed by us and deterministic. None of it is a prompt hint: the
analyst is told the numbers, never told what to conclude from them.

**Two spec defects were found writing this and are recorded dated in FOREX.md.**

* **#18** — §6 asks for "a synthetic USD strength index from the three pairs" and
  defines no formula, and for "rolling cross-pair correlation" with no window, no
  timeframe and no return basis. Owner ruling 2026-08-21: geometric mean of the three
  USD legs rebased to 100, and Pearson correlation on log returns over 120 hourly
  bars. Both are stated in code below rather than left to the reader.
* **#17** — the session labels, which live in :mod:`sentinel.fx.sessions`.

**§2.1 is the rule this module is built around.** Where an input does not exist the
field is **absent** — ``None``, never ``0``, and for the four inputs this market does
not have at all, no field exists to be null. A zero meaning "no data" reads to a model
as "no activity" and yields a confident answer built on nothing.

**Nothing here is imported from an adapter.** :class:`SymbolTails` is the shape this
module needs; ``core/`` builds one from a ``ForexTail``. That keeps the dependency
pointing the way ARCHITECTURE.md §3 requires and keeps this module testable from plain
series.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from pydantic import BaseModel, ConfigDict

from sentinel.core.logging import get_logger
from sentinel.fx.sessions import Session, session_label
from sentinel.fx.spread import SpreadProfile, build_profile, spread_samples
from sentinel.ingestion.models import OHLCVSeries

log = get_logger(__name__)

#: The four things §2's ledger says forex simply does not have. Named here so the
#: feature payload can *state* the absence rather than merely contain it — §2.1's
#: "missing data must look missing", made explicit rather than left to inference.
UNAVAILABLE_INPUTS: tuple[str, ...] = (
    "volume",
    "tick_volume",
    "open_interest",
    "long_short_ratio",
    "funding_rate",
)

#: Index and correlation places. Four is well inside the noise of either statistic and
#: keeps the payload readable.
INDEX_PLACES = Decimal("0.0001")
#: §18's ruling: Pearson on log returns over this many hourly bars.
CORRELATION_WINDOW = 120
#: Rebase point for the strength index. 100 is a convention with no information in it,
#: which is the point — only the *change* means anything.
INDEX_BASE = Decimal(100)

#: How each pair expresses the dollar. EURUSD and GBPUSD quote the dollar, so the USD
#: leg is their reciprocal — the dollar strengthens as they fall. USDJPY bases it, so
#: the leg is the price itself.
USD_LEG_INVERTED: Mapping[str, bool] = {"EURUSD": True, "GBPUSD": True, "USDJPY": False}

_MIN_GAP_HOURS = 12


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class SymbolTails:
    """One instrument's bid/ask tails, keyed by timeframe, and the grid they sit on.

    ``alignment_hours_utc`` is the **observed** 4h grid (D-k), carried so the daily
    anchor can be cross-checked against reality rather than assumed — the same
    discipline the pip's ``TickSize x 10`` assertion applies to precision.
    """

    symbol: str
    pip: Decimal
    bid: Mapping[str, OHLCVSeries]
    ask: Mapping[str, OHLCVSeries]
    alignment_hours_utc: tuple[int, ...]


class PeriodLevels(Frozen):
    """A completed day or week, as the platform the owner executes on would show it."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    period_start: datetime
    period_end: datetime


class SpreadView(Frozen):
    """The measured spread, surfaced for the analyst (§7.3).

    Both baselines travel, because they answer different questions and §5.3's defect
    #15 was precisely the two being confused: ``median_pips`` is the gate's global
    baseline, ``median_this_hour_pips`` is the cost model's per-hour expectation.
    """

    current_pips: Decimal
    median_pips: Decimal
    median_this_hour_pips: Decimal
    min_pips: Decimal
    max_pips: Decimal
    samples: int


class UsdStrength(Frozen):
    """Synthetic, equal-weight, geometric — and deliberately not DXY (§2).

    DXY is ICE-licensed and redistributing it is a problem we have no need to acquire.
    This is built from the three pairs we already hold, which also means it moves for
    reasons we can point at.
    """

    value: Decimal
    change_pct: Decimal
    change_24h_pct: Decimal | None
    window_bars: int
    pairs: tuple[str, ...]


class PairCorrelation(Frozen):
    """Pearson on log returns. §9's rail exists because these are rarely far from 1."""

    pair: tuple[str, str]
    coefficient: Decimal
    window_bars: int


class ForexFeatures(Frozen):
    """The forex half of a symbol's feature block.

    Rides inside ``MarketSnapshot.features`` alongside the market-blind
    :class:`~sentinel.features.models.SymbolFeatures` rather than extending it: that
    model is dumped whole into the crypto golden, so a field added there would move
    crypto's bytes for a market it knows nothing about.

    There is no volume field, no funding field and no open-interest field here — not
    nulled, absent. ``unavailable_inputs`` names them instead, so the absence is a
    statement rather than a silence (§2.1).
    """

    schema_version: int = 1
    symbol: str
    computed_at: datetime

    prior_day: PeriodLevels | None = None
    prior_week: PeriodLevels | None = None
    daily_open: Decimal | None = None
    weekly_open: Decimal | None = None

    session_now: Session
    session_of_last_bar: Session

    spread: SpreadView | None = None
    usd_strength: UsdStrength | None = None
    correlations: tuple[PairCorrelation, ...] = ()

    #: The observed 4h grid. Never 00/04/08/12/16/20 — a forex 4h bar does not share a
    #: boundary with a crypto one, in either season (D-k).
    bar_alignment_hours_utc: tuple[int, ...] = ()
    unavailable_inputs: tuple[str, ...] = UNAVAILABLE_INPUTS


# --------------------------------------------------------------------------- #
# Tail trimming — the first reader ForexConfig.feature_candles_1h has ever had
# --------------------------------------------------------------------------- #


def trim_to_feature_window(series: OHLCVSeries, *, candles: int) -> OHLCVSeries:
    """The most recent ``candles`` bars, for the feature engine.

    The 1h tail is 1200 bars so the spread profile gets ~35 samples per hour-of-day
    instead of ~10 (§7.3, owner correction). Features do not want 1200 — and the owner
    verified live on 2026-08-21 that the last 321 rows of the long tail produce
    ``TimeframeFeatures`` **identical** to a direct 321-bar request, so this trim is
    free rather than merely cheap.
    """
    if candles < 1:
        raise ValueError(f"feature window must be at least one candle, got {candles}")
    if len(series.candles) <= candles:
        return series
    return series.model_copy(update={"candles": series.candles[-candles:]})


# --------------------------------------------------------------------------- #
# The day and the week — read from the data, cross-checked against the clock
# --------------------------------------------------------------------------- #


def daily_anchor(on: datetime) -> datetime:
    """The 17:00 America/New_York instant that opens the forex day containing ``on``.

    Derived from the zone, never from a UTC hour: it is 21:00Z in August and 22:00Z in
    January (D-k), and Saxo's own 1d and 4h bars move with it.
    """
    from sentinel.fx.sessions import Session as _S
    from sentinel.fx.sessions import utc_bounds

    probe = on.astimezone(UTC).date()
    for offset in (0, -1, -2, -3):
        candidate_date = probe + timedelta(days=offset)
        bounds = utc_bounds(_S.NEW_YORK, candidate_date)
        if bounds is not None and bounds[1] <= on:
            return bounds[1]
    raise ValueError(f"no daily anchor found on or before {on.isoformat()}")


def assert_alignment(anchor: datetime, observed_hours: Sequence[int], *, symbol: str) -> None:
    """The anchor derived from the clock must appear in the grid the venue sent.

    Same shape as the pip's ``TickSize x 10`` cross-check, and for the same reason: two
    independent derivations that must agree, so a wrong one fails loudly instead of
    producing plausible levels an hour out. An empty grid is not checked — that is a
    tail too short to have a 4h bar in it, which is a different problem with its own
    reason code.
    """
    if not observed_hours:
        return
    if anchor.hour not in observed_hours:
        raise ValueError(
            f"{symbol}: the daily anchor derived from America/New_York is "
            f"{anchor.hour:02d}:00Z, which is not in the 4h grid the venue sent "
            f"({', '.join(f'{h:02d}' for h in observed_hours)}). One of the two is "
            f"wrong and levels an hour out would still look plausible (D-k)."
        )


def week_starts(
    open_times: Sequence[datetime], *, min_gap_hours: int = _MIN_GAP_HOURS
) -> tuple[datetime, ...]:
    """Every bar that follows a weekend-sized hole in the series (§5.2).

    Closed hours are cleanly **absent** from a Saxo chart response — measured, not
    assumed (journal/M10b_SPIKE.md §6) — so the weekend is a hole and the first bar
    after it is the open. This generalises :func:`sentinel.fx.hours.derive_week_open`,
    which reports only the most recent one, because the prior week needs two boundaries
    rather than one.
    """
    ordered = sorted(open_times)
    threshold = timedelta(hours=min_gap_hours)
    return tuple(nxt for prev, nxt in pairwise(ordered) if nxt - prev >= threshold)


def _levels_over(series: OHLCVSeries, start: datetime, end: datetime) -> PeriodLevels | None:
    """High/low/open/close across ``[start, end)``. ``None`` when the window is empty."""
    inside = [c for c in series.candles if start <= c.open_time < end]
    if not inside:
        return None
    return PeriodLevels(
        open=inside[0].open,
        high=max(c.high for c in inside),
        low=min(c.low for c in inside),
        close=inside[-1].close,
        period_start=inside[0].open_time,
        period_end=inside[-1].open_time,
    )


def prior_day_levels(daily: OHLCVSeries) -> PeriodLevels | None:
    """Yesterday, from Saxo's **native** 1d bar (§6.1).

    The adapter has already dropped the forming bar, so the newest closed daily bar is
    the completed day. Its ``open_time`` is a **date label**, not the bar's start (D-k)
    — which is why ``period_start`` reports the label rather than pretending to know
    the instant.
    """
    if not daily.candles:
        return None
    bar = daily.candles[-1]
    return PeriodLevels(
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        period_start=bar.open_time,
        period_end=bar.open_time,
    )


def prior_week_levels(hourly: OHLCVSeries) -> PeriodLevels | None:
    """The last **complete** week, bounded by two observed week opens (§5.2).

    Not the last five days: a week is what the market says it is, and the boundary
    moves by an hour twice a year because the US and EU change daylight saving on
    different dates. Reading it from the holes in the series sidesteps the question.
    """
    starts = week_starts([c.open_time for c in hourly.candles])
    if len(starts) < 2:
        return None
    return _levels_over(hourly, starts[-2], starts[-1])


def _open_at_or_after(hourly: OHLCVSeries, at: datetime) -> Decimal | None:
    for candle in hourly.candles:
        if candle.open_time >= at:
            return candle.open
    return None


def daily_open(
    hourly: OHLCVSeries, *, now: datetime, symbol: str, alignment: Sequence[int]
) -> Decimal | None:
    """The open of the day in progress, from the first hourly bar after the anchor."""
    anchor = daily_anchor(now)
    assert_alignment(anchor, alignment, symbol=symbol)
    return _open_at_or_after(hourly, anchor)


def weekly_open(hourly: OHLCVSeries) -> Decimal | None:
    """The open of the week in progress, from the most recent observed week open."""
    starts = week_starts([c.open_time for c in hourly.candles])
    if not starts:
        return None
    return _open_at_or_after(hourly, starts[-1])


# --------------------------------------------------------------------------- #
# Cross-pair: the two features one symbol cannot produce alone
# --------------------------------------------------------------------------- #


def _quantize(value: float, places: Decimal = INDEX_PLACES) -> Decimal:
    return Decimal(repr(value)).quantize(places, rounding=ROUND_HALF_UP)


def _usd_leg(symbol: str, price: Decimal) -> float:
    if price <= 0:
        raise ValueError(f"{symbol}: a price of {price} cannot enter a strength index")
    return 1.0 / float(price) if USD_LEG_INVERTED[symbol] else float(price)


def usd_strength_index(
    closes: Mapping[str, Sequence[Decimal]], *, window: int | None = None
) -> UsdStrength | None:
    """Spec defect #18's ruling, in one function.

    ``index_t = 100 x geomean_over_pairs(leg_t / leg_0)``. The geometric mean is what a
    currency index wants: it is scale-free, so a pair quoted in the hundreds does not
    outvote one quoted near parity, and it is symmetric — a leg halving and a leg
    doubling cancel exactly, which an arithmetic mean of percentage changes does not.

    ``None``, never a partial index, when any of the three pairs is missing or too
    short. Two thirds of a dollar index is not a dollar index, and a number computed
    from two pairs while claiming to be from three is worse than no number.
    """
    if set(closes) != set(USD_LEG_INVERTED):
        return None
    length = min(len(series) for series in closes.values())
    if window is not None:
        length = min(length, window)
    if length < 2:
        return None

    trimmed = {symbol: list(series)[-length:] for symbol, series in closes.items()}
    ratios_now = [
        _usd_leg(symbol, series[-1]) / _usd_leg(symbol, series[0])
        for symbol, series in trimmed.items()
    ]
    value = INDEX_BASE * Decimal(repr(math.exp(sum(map(math.log, ratios_now)) / len(ratios_now))))

    change_24h: Decimal | None = None
    if length > 24:
        ratios_24 = [
            _usd_leg(symbol, series[-1]) / _usd_leg(symbol, series[-25])
            for symbol, series in trimmed.items()
        ]
        geo_24 = math.exp(sum(map(math.log, ratios_24)) / len(ratios_24))
        change_24h = _quantize((geo_24 - 1.0) * 100.0)

    return UsdStrength(
        value=value.quantize(INDEX_PLACES, rounding=ROUND_HALF_UP),
        change_pct=_quantize(float(value / INDEX_BASE - 1) * 100.0),
        change_24h_pct=change_24h,
        window_bars=length,
        pairs=tuple(sorted(trimmed)),
    )


def _log_returns(series: Sequence[Decimal]) -> list[float]:
    return [math.log(float(nxt) / float(prev)) for prev, nxt in pairwise(series)]


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    count = len(left)
    if count < 2:
        return None
    mean_l, mean_r = sum(left) / count, sum(right) / count
    dev_l = [value - mean_l for value in left]
    dev_r = [value - mean_r for value in right]
    denominator = math.sqrt(sum(d * d for d in dev_l)) * math.sqrt(sum(d * d for d in dev_r))
    if denominator == 0:
        # A flat series has no correlation to report. Zero would read as "unrelated",
        # which is a claim; None is the absence of one (§2.1).
        return None
    return sum(a * b for a, b in zip(dev_l, dev_r, strict=True)) / denominator


def cross_pair_correlation(
    closes: Mapping[str, Sequence[Decimal]], *, window: int = CORRELATION_WINDOW
) -> tuple[PairCorrelation, ...]:
    """Pearson on log returns, pairwise (§9, defect #18's ruling).

    Log returns rather than prices, because two trending price series correlate near 1
    whatever they are doing — which would make §9's rail look satisfied by arithmetic.
    """
    symbols = sorted(closes)
    returns: dict[str, list[float]] = {}
    for symbol in symbols:
        series = list(closes[symbol])[-(window + 1) :]
        if len(series) >= 2:
            returns[symbol] = _log_returns(series)

    out: list[PairCorrelation] = []
    for index, left in enumerate(symbols):
        for right in symbols[index + 1 :]:
            if left not in returns or right not in returns:
                continue
            size = min(len(returns[left]), len(returns[right]))
            coefficient = _pearson(returns[left][-size:], returns[right][-size:])
            if coefficient is None:
                continue
            out.append(
                PairCorrelation(
                    pair=(left, right),
                    coefficient=_quantize(coefficient),
                    window_bars=size,
                )
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def spread_view(profile: SpreadProfile | None, *, now: datetime) -> SpreadView | None:
    """Both baselines, or nothing. Never a zeroed profile (§5.3, defect #15)."""
    if profile is None:
        return None
    return SpreadView(
        current_pips=profile.median_by_hour_pips.get(now.hour, profile.global_median_pips),
        median_pips=profile.global_median_pips,
        median_this_hour_pips=profile.expected_at(now.hour),
        min_pips=profile.min_pips,
        max_pips=profile.max_pips,
        samples=profile.samples,
    )


def compute_cycle_features(
    tails: Mapping[str, SymbolTails],
    *,
    now: datetime,
    feature_candles_1h: int,
    spread_lookback: int | None = None,
) -> dict[str, ForexFeatures]:
    """Every symbol's forex features, including the two that need all three pairs.

    Cross-pair work happens **once per cycle**, here, rather than inside
    :func:`sentinel.features.engine.compute`, which is per-symbol and market-blind and
    stays that way — it is the module the crypto golden is computed from.
    """
    hourly_closes: dict[str, list[Decimal]] = {}
    for symbol, tail in tails.items():
        hourly = tail.bid.get("1h")
        if hourly is not None and symbol in USD_LEG_INVERTED:
            hourly_closes[symbol] = [c.close for c in hourly.candles]

    strength = usd_strength_index(hourly_closes, window=feature_candles_1h)
    correlations = cross_pair_correlation(hourly_closes)

    out: dict[str, ForexFeatures] = {}
    for symbol, tail in tails.items():
        out[symbol] = _one_symbol(
            tail,
            now=now,
            feature_candles_1h=feature_candles_1h,
            spread_lookback=spread_lookback,
            strength=strength,
            correlations=correlations,
        )
    return out


def _one_symbol(
    tail: SymbolTails,
    *,
    now: datetime,
    feature_candles_1h: int,
    spread_lookback: int | None,
    strength: UsdStrength | None,
    correlations: tuple[PairCorrelation, ...],
) -> ForexFeatures:
    hourly = tail.bid.get("1h")
    daily = tail.bid.get("1d")

    profile: SpreadProfile | None = None
    hourly_ask = tail.ask.get("1h")
    if hourly is not None and hourly_ask is not None:
        profile = build_profile(
            spread_samples(hourly, hourly_ask, pip=tail.pip),
            symbol=tail.symbol,
            lookback=spread_lookback,
        )

    features_hourly = (
        trim_to_feature_window(hourly, candles=feature_candles_1h) if hourly is not None else None
    )

    last_bar_at = (
        features_hourly.candles[-1].open_time
        if features_hourly is not None and features_hourly.candles
        else now
    )

    return ForexFeatures(
        symbol=tail.symbol,
        computed_at=now,
        prior_day=prior_day_levels(daily) if daily is not None else None,
        prior_week=prior_week_levels(hourly) if hourly is not None else None,
        daily_open=(
            daily_open(hourly, now=now, symbol=tail.symbol, alignment=tail.alignment_hours_utc)
            if hourly is not None
            else None
        ),
        weekly_open=weekly_open(hourly) if hourly is not None else None,
        session_now=session_label(now),
        session_of_last_bar=session_label(last_bar_at),
        spread=spread_view(profile, now=now),
        usd_strength=strength,
        correlations=correlations,
        bar_alignment_hours_utc=tail.alignment_hours_utc,
    )


__all__ = [
    "CORRELATION_WINDOW",
    "UNAVAILABLE_INPUTS",
    "ForexFeatures",
    "PairCorrelation",
    "PeriodLevels",
    "SpreadView",
    "SymbolTails",
    "UsdStrength",
    "assert_alignment",
    "compute_cycle_features",
    "cross_pair_correlation",
    "daily_anchor",
    "daily_open",
    "prior_day_levels",
    "prior_week_levels",
    "spread_view",
    "trim_to_feature_window",
    "usd_strength_index",
    "week_starts",
    "weekly_open",
]

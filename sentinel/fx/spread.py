"""The measured spread, and the two different baselines it needs (FOREX.md §7.3, §5.3).

Forex loses the order book and gains this. It is the one replacement in §2's ledger
that is genuinely good: journal/M10b_SPIKE.md measured 46-95 **distinct** spread
values per pair over ~50 days of hourly bars, and confirmed them against a firm
dealable ``infoprices`` quote (1.2 pips against a 1.1-pip chart median,
``DelayedByMinutes: 0``). So the spread is not configured and not assumed — it is
read off the same bid/ask tails the adapter already fetched.

**Two baselines, and they are deliberately different (owner correction C1, spec
defect #15, 2026-08-21).**

§5.3 as written gated on "a configured multiple of that instrument's median **for
that hour-of-day**". That cannot work, and the reason is worth stating because it is
almost invisible: at 21:00 UTC, GBPUSD's hour-of-day median *is* 12.0 pips. So a
12-pip spread at 21:00 is "normal for that hour" and sails through the gate — while
costing 0.667R of an 18-pip stop. The per-hour baseline normalises away precisely the
widening the gate exists to catch.

So:

* the **cost model** uses the per-hour-of-day median, which is the right expectation
  of what trading in this hour costs, and it feeds net RR;
* the **gate** uses the instrument's **global** median across the whole sample, so
  rollover hours fail it naturally — which is what §5.3 was reaching for — and a
  genuine anomaly fails it at any hour of the day.

**Why the 1h tail is 1200 bars.** 321 bars is ~13 days, which after weekends leaves
about ten samples in each hour-of-day bucket. A median over ten noisy samples is not
a baseline, least of all in the tail hours where it decides whether a signal is
emitted. 1200 bars is ~50 days and ~35 samples per bucket — what the spike actually
measured — and it is still **one** request, sitting exactly at the ceiling rather
than over it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sentinel.core.config import ForexConfig
from sentinel.fx.hours import MarketState, state_at
from sentinel.fx.models import ForexRejection
from sentinel.ingestion.models import OHLCVSeries

#: Spreads are reported to a tenth of a pip. That is not a display choice: ``TickSize``
#: is one tenth of a pip on all three pairs, so a tenth is the venue's own granularity
#: and quantizing here loses nothing.
TENTH_PIP = Decimal("0.1")


@dataclass(frozen=True)
class SpreadSample:
    at: datetime
    pips: Decimal


@dataclass(frozen=True)
class SpreadProfile:
    """What this instrument's spread has been doing, from the tail we already hold."""

    symbol: str
    samples: int
    #: The gate's baseline. One number for the whole sample, so a wide hour is wide.
    global_median_pips: Decimal
    #: The cost model's baseline, per UTC hour-of-day.
    median_by_hour_pips: dict[int, Decimal]
    min_pips: Decimal
    max_pips: Decimal

    def expected_at(self, hour: int) -> Decimal:
        """What this hour of the day usually costs, falling back to the global median.

        The fallback matters on a young deployment: a bucket with no samples yet must
        not read as "free", which a zero would.
        """
        return self.median_by_hour_pips.get(hour, self.global_median_pips)


def spread_samples(bid: OHLCVSeries, ask: OHLCVSeries, *, pip: Decimal) -> tuple[SpreadSample, ...]:
    """``CloseAsk - CloseBid`` per bar, in pips — the spike's own measurement.

    The two series must be the same read of the same window. They always are: they
    come from one chart response, where every row carries both sides.
    """
    if pip <= 0:
        raise ValueError(f"pip must be positive, got {pip}")
    if len(bid.candles) != len(ask.candles):
        raise ValueError(
            f"{bid.symbol}: bid and ask tails differ in length "
            f"({len(bid.candles)} vs {len(ask.candles)}) — they must be one read"
        )
    samples: list[SpreadSample] = []
    for bid_candle, ask_candle in zip(bid.candles, ask.candles, strict=True):
        if bid_candle.open_time != ask_candle.open_time:
            raise ValueError(
                f"{bid.symbol}: bid and ask bars are not aligned at "
                f"{bid_candle.open_time.isoformat()}"
            )
        samples.append(
            SpreadSample(
                at=bid_candle.open_time,
                pips=quantize_pips((ask_candle.close - bid_candle.close) / pip),
            )
        )
    return tuple(samples)


def build_profile(
    samples: Sequence[SpreadSample], *, symbol: str, lookback: int | None = None
) -> SpreadProfile | None:
    """Summarise a spread series. ``None`` when there is nothing to summarise.

    ``None`` rather than an empty profile with zeros in it: a zero baseline would make
    every spread look enormous relative to it, or — worse, depending which way the
    comparison went — make every spread acceptable.
    """
    window = list(samples)[-lookback:] if lookback else list(samples)
    if not window:
        return None
    pips = [sample.pips for sample in window]
    by_hour: dict[int, list[Decimal]] = {}
    for sample in window:
        by_hour.setdefault(sample.at.hour, []).append(sample.pips)
    return SpreadProfile(
        symbol=symbol,
        samples=len(window),
        global_median_pips=median(pips),
        median_by_hour_pips={hour: median(values) for hour, values in sorted(by_hour.items())},
        min_pips=min(pips),
        max_pips=max(pips),
    )


def median(values: Sequence[Decimal]) -> Decimal:
    """Exact median, quantized to a tenth of a pip. Even samples average the middle two."""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise ValueError("median of an empty series")
    middle = count // 2
    if count % 2:
        return quantize_pips(ordered[middle])
    return quantize_pips((ordered[middle - 1] + ordered[middle]) / 2)


def quantize_pips(value: Decimal) -> Decimal:
    return value.quantize(TENTH_PIP, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SpreadVerdict:
    """Whether the current spread permits a new signal, and on what evidence."""

    rejection: ForexRejection | None
    #: ``"measured"`` when the spread series decided it, ``"clock"`` when the series
    #: was unavailable and the 19:00-21:00 UTC backstop did.
    basis: str
    current_pips: Decimal | None = None
    #: The **global** median (see the module docstring), never the hour's.
    baseline_pips: Decimal | None = None
    threshold_pips: Decimal | None = None

    @property
    def allowed(self) -> bool:
        return self.rejection is None


def spread_gate(
    *,
    current_pips: Decimal | None,
    profile: SpreadProfile | None,
    now: datetime,
    config: ForexConfig,
) -> SpreadVerdict:
    """§5.3's rollover rail, spread-triggered with a clock backstop.

    Spread-triggered is better than clock-triggered for two reasons: it self-calibrates
    as an instrument's normal spread changes, and it catches unscheduled widening that
    no clock would. The clock stays as the backstop for when the series is missing,
    because "we cannot measure it" must not read as "it is fine".
    """
    usable = (
        profile is not None
        and profile.samples >= config.spread_min_samples
        and current_pips is not None
        and profile.global_median_pips > 0
    )
    if not usable:
        if state_at(now, config) is MarketState.ROLLOVER:
            return SpreadVerdict(rejection=ForexRejection.ROLLOVER_WINDOW, basis="clock")
        return SpreadVerdict(rejection=None, basis="clock")

    assert profile is not None and current_pips is not None  # narrowed by `usable`
    threshold = quantize_pips(profile.global_median_pips * config.spread_max_multiple)
    if current_pips > threshold:
        return SpreadVerdict(
            rejection=ForexRejection.SPREAD_TOO_WIDE,
            basis="measured",
            current_pips=current_pips,
            baseline_pips=profile.global_median_pips,
            threshold_pips=threshold,
        )
    return SpreadVerdict(
        rejection=None,
        basis="measured",
        current_pips=current_pips,
        baseline_pips=profile.global_median_pips,
        threshold_pips=threshold,
    )


__all__ = [
    "TENTH_PIP",
    "SpreadProfile",
    "SpreadSample",
    "SpreadVerdict",
    "build_profile",
    "median",
    "quantize_pips",
    "spread_gate",
    "spread_samples",
]

"""The measured spread and its two baselines — FOREX.md §7.3, §5.3, spec defect #15.

The test this file exists for is
``test_the_per_hour_baseline_would_never_fire_at_the_hour_it_is_for``. §5.3 as
written gates on a multiple of the hour-of-day median, and that is self-defeating in
a way that is almost invisible: at 21:00 UTC GBPUSD's hour-of-day median **is** 12.0
pips, so a 12-pip spread at 21:00 is "normal for that hour" and passes — while
costing 0.667R of an 18-pip stop, which the corrected net-RR model turns into a gross
1.5 netting 0.500.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from sentinel.core.clock import FrozenClock
from sentinel.core.config import ForexConfig
from sentinel.core.markets import Market
from sentinel.fx.models import ForexRejection
from sentinel.fx.spread import (
    SpreadSample,
    build_profile,
    median,
    spread_gate,
    spread_samples,
)
from sentinel.ingestion.adapters.forex_saxo import ForexTail, SaxoForexAdapter
from sentinel.ingestion.models import Candle, OHLCVSeries
from tests.conftest import cassette
from tests.ingestion.conftest import make_fetcher

CONFIG = ForexConfig()
START = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


async def eurusd_tail() -> ForexTail:
    """One real adapter read of the reconstructed EURUSD chart fixture."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/instruments/details"):
            return httpx.Response(200, json=cassette("saxo_ref_details.json"))
        if path.endswith("/ref/v1/instruments"):
            return httpx.Response(200, json=cassette("saxo_ref_instruments_EURUSD.json"))
        return httpx.Response(200, json=cassette("saxo_chart_EURUSD_60.json"))

    fetcher, client = make_fetcher(httpx.MockTransport(handler))
    adapter = SaxoForexAdapter(
        CONFIG,
        fetcher=fetcher,
        tokens=_StubTokens(),
        clock=FrozenClock(datetime(2026, 8, 21, 8, 30, tzinfo=UTC)),
    )
    async with client:
        return await adapter.fetch_tail("EURUSD", "1h", 10)


class _StubTokens:
    async def access_token(self) -> str:
        return "stub"


def samples(*, days: int, normal: str, hour_21: str) -> list[SpreadSample]:
    """An hourly series shaped like the spike's GBPUSD: flat all day, wide at 21:00."""
    out: list[SpreadSample] = []
    for day in range(days):
        for hour in range(24):
            at = START + timedelta(days=day, hours=hour)
            out.append(SpreadSample(at=at, pips=Decimal(hour_21 if hour == 21 else normal)))
    return out


# ── reading the series off the tails ────────────────────────────────────────


async def test_the_spread_series_comes_from_the_bid_and_ask_tails() -> None:
    """``CloseAsk - CloseBid`` per bar, the spike's own measurement, through the real
    adapter path. The fixture is 1.1 pips everywhere but the bar stamped 01:00Z,
    which carries the 2.7-pip rollover median from the findings' §3 table."""
    tail = await eurusd_tail()
    series = spread_samples(tail.bid, tail.ask, pip=Decimal("0.0001"))
    assert len(series) == 10
    assert [s.pips for s in series] == [Decimal("1.1")] * 3 + [Decimal("2.7")] + [
        Decimal("1.1")
    ] * 6
    assert series[3].at.hour == 1


def test_bid_and_ask_must_be_one_read_of_one_window() -> None:
    """They always are — one chart row carries both sides — and if they ever were not,
    the spread would be measured across two differently-anchored series (D-d)."""

    def series(count: int, offset_hours: int = 0) -> OHLCVSeries:
        return OHLCVSeries(
            source="saxo_fxspot",
            fetched_at=START,
            symbol="EURUSD",
            timeframe="1h",
            market=Market.FOREX,
            candles=tuple(
                Candle(
                    open_time=START + timedelta(hours=index + offset_hours),
                    open=Decimal("1.1690"),
                    high=Decimal("1.1695"),
                    low=Decimal("1.1685"),
                    close=Decimal("1.1692"),
                    volume=None,
                )
                for index in range(count)
            ),
        )

    with pytest.raises(ValueError, match="differ in length"):
        spread_samples(series(5), series(4), pip=Decimal("0.0001"))
    with pytest.raises(ValueError, match="not aligned"):
        spread_samples(series(5), series(5, offset_hours=1), pip=Decimal("0.0001"))


def test_the_median_is_exact_and_quantized_to_a_tenth_of_a_pip() -> None:
    """A tenth is the venue's own granularity — ``TickSize`` is a tenth of a pip on
    all three pairs — so this loses nothing."""
    assert median([Decimal("1.0"), Decimal("1.1"), Decimal("1.9")]) == Decimal("1.1")
    assert median([Decimal("1.0"), Decimal("1.1")]) == Decimal("1.1")  # 1.05 -> 1.1
    assert median([Decimal("1.0"), Decimal("1.2")]) == Decimal("1.1")
    with pytest.raises(ValueError, match="empty"):
        median([])


def test_an_empty_series_yields_no_profile_rather_than_a_profile_of_zeros() -> None:
    """A zero baseline would make every spread look enormous relative to it — or,
    depending which way the comparison ran, make every spread acceptable."""
    assert build_profile([], symbol="EURUSD") is None


def test_an_unseen_hour_falls_back_to_the_global_median_not_to_zero() -> None:
    profile = build_profile(samples(days=3, normal="1.1", hour_21="1.1"), symbol="EURUSD")
    assert profile is not None
    profile.median_by_hour_pips.pop(13)
    assert profile.expected_at(13) == profile.global_median_pips


# ── spec defect #15: the baseline the gate uses ─────────────────────────────


def test_the_per_hour_baseline_would_never_fire_at_the_hour_it_is_for() -> None:
    """§5.3 as written, shown failing on the case it was written for.

    GBPUSD's measured hour-of-day median at 21:00 is 12.0 pips. Against *that*
    baseline a 12-pip spread is entirely normal and a x3 gate passes it — while the
    corrected net-RR model turns a gross 1.5 into a net 0.500 at that spread. The
    per-hour baseline normalises away exactly the widening the gate exists to catch.
    """
    profile = build_profile(samples(days=50, normal="1.8", hour_21="12.0"), symbol="GBPUSD")
    assert profile is not None
    assert profile.global_median_pips == Decimal("1.8")
    assert profile.median_by_hour_pips[21] == Decimal("12.0")

    # What §5.3 said to do, computed here rather than implemented anywhere:
    per_hour_threshold = profile.median_by_hour_pips[21] * CONFIG.spread_max_multiple
    assert Decimal("12.0") <= per_hour_threshold, "the defect, demonstrated"

    # What ships instead.
    verdict = spread_gate(
        current_pips=Decimal("12.0"),
        profile=profile,
        now=datetime(2026, 8, 17, 21, 30, tzinfo=UTC),
        config=CONFIG,
    )
    assert verdict.rejection is ForexRejection.SPREAD_TOO_WIDE
    assert verdict.baseline_pips == Decimal("1.8")
    assert verdict.threshold_pips == Decimal("5.4")
    assert verdict.basis == "measured"


def test_the_hour_of_day_median_is_still_what_the_cost_model_uses() -> None:
    """Both baselines exist and they are for different questions: what this hour
    usually costs, and whether right now is abnormal."""
    profile = build_profile(samples(days=50, normal="1.8", hour_21="12.0"), symbol="GBPUSD")
    assert profile is not None
    assert profile.expected_at(21) == Decimal("12.0")
    assert profile.expected_at(9) == Decimal("1.8")


@pytest.mark.parametrize(
    ("median_pips", "threshold", "passes", "fails"),
    [("1.1", "3.3", "3.3", "3.4"), ("1.8", "5.4", "5.4", "5.5")],
    ids=["EURUSD-1.1", "GBPUSD-1.8"],
)
def test_the_threshold_matches_the_owners_sanity_check(
    median_pips: str, threshold: str, passes: str, fails: str
) -> None:
    """x3.0 against the global median: EURUSD 1.1 -> 3.3, GBPUSD 1.8 -> 5.4. Not
    reached in normal London/New York hours, exceeded at rollover. 3.0 is a starting
    guess to be calibrated from DRY_RUN data, and the report says so."""
    profile = build_profile(
        samples(days=50, normal=median_pips, hour_21=median_pips), symbol="EURUSD"
    )
    assert profile is not None
    assert profile.global_median_pips == Decimal(median_pips)

    now = datetime(2026, 8, 17, 9, 30, tzinfo=UTC)
    ok = spread_gate(current_pips=Decimal(passes), profile=profile, now=now, config=CONFIG)
    assert ok.allowed
    assert ok.threshold_pips == Decimal(threshold)

    wide = spread_gate(current_pips=Decimal(fails), profile=profile, now=now, config=CONFIG)
    assert wide.rejection is ForexRejection.SPREAD_TOO_WIDE


# ── the clock backstop ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (9, None),
        (19, ForexRejection.ROLLOVER_WINDOW),
        (21, ForexRejection.ROLLOVER_WINDOW),
        (22, None),
    ],
)
def test_without_a_series_the_clock_backstops_the_rollover_window(
    hour: int, expected: ForexRejection | None
) -> None:
    """§5.3's floor. "We cannot measure it" must not read as "it is fine"."""
    verdict = spread_gate(
        current_pips=None,
        profile=None,
        now=datetime(2026, 8, 17, hour, 30, tzinfo=UTC),
        config=CONFIG,
    )
    assert verdict.rejection is expected
    assert verdict.basis == "clock"


def test_too_few_samples_falls_back_to_the_clock_rather_than_trusting_them() -> None:
    thin = build_profile(samples(days=1, normal="1.8", hour_21="12.0"), symbol="GBPUSD")
    assert thin is not None
    assert thin.samples < CONFIG.spread_min_samples

    verdict = spread_gate(
        current_pips=Decimal("40.0"),
        profile=thin,
        now=datetime(2026, 8, 17, 21, 30, tzinfo=UTC),
        config=CONFIG,
    )
    assert verdict.rejection is ForexRejection.ROLLOVER_WINDOW
    assert verdict.basis == "clock"


def test_a_normal_spread_in_normal_hours_is_simply_allowed() -> None:
    profile = build_profile(samples(days=50, normal="1.1", hour_21="2.7"), symbol="EURUSD")
    assert profile is not None
    verdict = spread_gate(
        current_pips=Decimal("1.1"),
        profile=profile,
        now=datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
        config=CONFIG,
    )
    assert verdict.allowed
    assert verdict.basis == "measured"
    assert verdict.current_pips == Decimal("1.1")


# --------------------------------------------------------------------------- #
# Spec defect #30 — the clock backstop is unreachable in production (M10d)
# --------------------------------------------------------------------------- #


def test_the_clock_backstop_never_fires_once_the_series_is_usable() -> None:
    """**A rail that cannot fire**, pinned as the defect it is rather than as a feature.

    §5.3 answered D-g — elevated spreads from 19:00 to 21:00 UTC — with a
    spread-triggered rail and "a time-based floor of 19:00-21:00 UTC as a backstop for
    when the spread series is unavailable". In production the series is *never*
    unavailable: the 1h tail is 1200 bars every cycle, comfortably past
    ``spread_min_samples``. So ``ROLLOVER_WINDOW`` is unreachable and the measured rail
    decides the rollover hours alone — which is FOREX.md defect #21's own sin one level
    out, *a rail that cannot fire is worse than an absent one, because it reads on a
    checklist as a rail.*

    This is asserted, not fixed. The fix is a new threshold and therefore a new
    uncalibrated guess; ``forex.scan_hours_utc`` ending at 19 sidesteps the window for
    the observation period instead. When somebody does fix it, this test is the one
    that has to flip, and it says so.
    """
    profile = build_profile(samples(days=50, normal="1.8", hour_21="12.0"), symbol="GBPUSD")
    assert profile is not None
    assert profile.samples >= CONFIG.spread_min_samples

    for hour in range(CONFIG.rollover_window_start_hour_utc, CONFIG.rollover_window_end_hour_utc):
        verdict = spread_gate(
            # This hour's OWN typical spread, which is what a real cycle presents.
            current_pips=profile.expected_at(hour),
            profile=profile,
            now=datetime(2026, 8, 17, hour, 30, tzinfo=UTC),
            config=CONFIG,
        )
        assert verdict.basis == "measured", hour
        assert verdict.rejection is not ForexRejection.ROLLOVER_WINDOW, (
            f"the clock backstop fired at {hour}:00 with a usable series — defect #30 "
            f"has been fixed and this test should be inverted, not deleted"
        )


def test_the_measured_rail_admits_a_typical_twenty_hundred_spread() -> None:
    """The half of #30 that costs money, stated as the number it turns on.

    GBPUSD's hour-of-day median at 20:00 is 4.6 pips against a global median of 1.8, so
    the 3.0x threshold is 5.4 and a typical 20:00 bar sails through — while the cost
    model charges that same 4.6 at row 9, which is 0.256R of an 18-pip stop. The cycle
    is paid for in full and most of what it buys is then rejected on net RR.
    """
    profile = build_profile(samples(days=50, normal="1.8", hour_21="12.0"), symbol="GBPUSD")
    assert profile is not None
    threshold = profile.global_median_pips * CONFIG.spread_max_multiple

    typical_at_20 = Decimal("4.6")
    assert typical_at_20 < threshold, "the premise of #30 no longer holds; recheck the entry"

    verdict = spread_gate(
        current_pips=typical_at_20,
        profile=profile,
        now=datetime(2026, 8, 17, 20, 30, tzinfo=UTC),
        config=CONFIG,
    )
    assert verdict.allowed


def test_the_scan_window_ends_before_the_hours_the_rail_cannot_cover() -> None:
    """The relationship D2 rests on, so widening the window re-opens the question.

    ``scan_hours_utc`` ending at or before ``rollover_window_start_hour_utc`` is what
    makes defect #30 harmless for this observation window. Extend the window past 19
    without fixing the rail and this fails — which is the point: it should not be
    possible to re-acquire the exposure quietly.

    19 is also already ``friday_signal_cutoff_hour_utc``, chosen for the same reason, so
    the daily window ends where the Friday one does and neither number is invented.
    """
    from sentinel.core.config import load_config

    forex = load_config().forex
    _, end = forex.scan_hours_utc

    assert end <= forex.rollover_window_start_hour_utc
    assert end == forex.friday_signal_cutoff_hour_utc

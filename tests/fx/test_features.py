"""Forex features — FOREX.md §2, §6, and spec defects #17 and #18.

Three families of test here, and they exist for different reasons.

* The **day and week** tests pin that boundaries are read from the data and
  cross-checked against the clock, because a level an hour out still looks plausible
  (D-k, and the same failure shape as the pip).
* The **cross-pair** tests pin defect #18's rulings — geometric mean, log returns —
  by checking the properties that distinguish them from the alternatives that were
  rejected, not by checking a magic number.
* The **§2.1** tests pin that missing data looks missing: no zero stands in for a
  null, and the four inputs this market does not have have no field at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.core.markets import Market
from sentinel.fx.features import (
    UNAVAILABLE_INPUTS,
    ForexFeatures,
    SymbolTails,
    assert_alignment,
    compute_cycle_features,
    cross_pair_correlation,
    daily_anchor,
    prior_day_levels,
    prior_week_levels,
    trim_to_feature_window,
    usd_strength_index,
    week_starts,
)
from sentinel.fx.sessions import Session
from sentinel.ingestion.models import Candle, OHLCVSeries

PIP = Decimal("0.0001")
#: A Sunday 21:00Z week open, which is what the venue shows in August (EDT).
WEEK_OPEN = datetime(2026, 7, 19, 21, tzinfo=UTC)
#: The August 4h grid: 17:00 America/New_York is 21:00Z (D-k).
AUGUST_GRID = (1, 5, 9, 13, 17, 21)


def hourly_open_times(*, weeks: int, start: datetime = WEEK_OPEN) -> list[datetime]:
    """Sunday 21:00Z to Friday 21:00Z, with the weekend simply **absent**.

    Measured behaviour, not a convenience: journal/M10b_SPIKE.md §6 confirmed closed
    hours are cleanly missing from a chart response — not zero-filled and not a
    repeated Friday bar — which is what makes the hole readable as a boundary.
    """
    out: list[datetime] = []
    for week in range(weeks):
        opened = start + timedelta(days=7 * week)
        out.extend(opened + timedelta(hours=step) for step in range(120))  # Sun 21Z -> Fri 21Z
    return out


def series(
    open_times: list[datetime],
    *,
    symbol: str = "EURUSD",
    timeframe: str = "1h",
    prices: list[Decimal] | None = None,
) -> OHLCVSeries:
    values = prices if prices is not None else [Decimal("1.1690")] * len(open_times)
    return OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=open_times[-1],
        symbol=symbol,
        timeframe=timeframe,
        market=Market.FOREX,
        candles=tuple(
            Candle(
                open_time=at,
                open=price,
                high=price + Decimal("0.0010"),
                low=price - Decimal("0.0010"),
                close=price,
                volume=None,
            )
            for at, price in zip(open_times, values, strict=True)
        ),
    )


def tails(symbol: str, prices: list[Decimal], open_times: list[datetime]) -> SymbolTails:
    bid = series(open_times, symbol=symbol, prices=prices)
    ask = series(open_times, symbol=symbol, prices=[p + PIP for p in prices])
    daily = series(
        [WEEK_OPEN.replace(hour=0) + timedelta(days=n) for n in range(5)],
        symbol=symbol,
        timeframe="1d",
        prices=prices[:5],
    )
    return SymbolTails(
        symbol=symbol,
        pip=PIP,
        bid={"1h": bid, "1d": daily},
        ask={"1h": ask},
        alignment_hours_utc=AUGUST_GRID,
    )


# ── the day, the week, and the anchor ───────────────────────────────────────


@pytest.mark.parametrize(
    ("on", "expected_hour"),
    [(datetime(2026, 8, 13, 12, tzinfo=UTC), 21), (datetime(2026, 1, 13, 12, tzinfo=UTC), 22)],
    ids=["august-edt", "january-est"],
)
def test_the_daily_anchor_is_seventeen_hundred_new_york_in_both_seasons(
    on: datetime, expected_hour: int
) -> None:
    """D-k. The forex day does not begin at midnight UTC and does not begin at a fixed
    hour at all — it begins at 17:00 New York, which is 21:00Z in August and 22:00Z in
    January, and Saxo's own 1d and 4h bars move with it."""
    anchor = daily_anchor(on)
    assert anchor.hour == expected_hour
    assert anchor <= on


def test_a_daily_anchor_that_is_not_in_the_venues_own_grid_fails_loudly() -> None:
    """The load-bearing half, and the same shape as the pip's ``TickSize x 10`` check.

    Two independent derivations of the same boundary — one from the zone, one from the
    grid the venue actually sent — must agree. A silent disagreement puts every prior-
    day level an hour out while leaving it entirely plausible.
    """
    august_anchor = daily_anchor(datetime(2026, 8, 13, 12, tzinfo=UTC))  # 21:00Z
    assert_alignment(august_anchor, AUGUST_GRID, symbol="EURUSD")  # agrees, no raise
    with pytest.raises(ValueError, match="not in the 4h grid"):
        assert_alignment(august_anchor, (2, 6, 10, 14, 18, 22), symbol="EURUSD")  # winter grid


def test_an_empty_grid_is_not_checked_rather_than_failed() -> None:
    """A tail too short to contain a 4h bar is a different problem with its own reason
    code; inventing an alignment failure for it would point at the wrong thing."""
    assert_alignment(daily_anchor(datetime(2026, 8, 13, 12, tzinfo=UTC)), (), symbol="EURUSD")


def test_week_starts_reads_the_boundary_from_the_hole_not_from_config() -> None:
    """§5.2. The week is what the market says it is, and the boundary moves by an hour
    twice a year because the US and EU change daylight saving on different dates."""
    starts = week_starts(hourly_open_times(weeks=3))
    assert starts == (WEEK_OPEN + timedelta(days=7), WEEK_OPEN + timedelta(days=14))


def test_prior_week_is_bounded_by_two_observed_week_opens() -> None:
    """Not "the last five days" — the last **complete** week, between two holes."""
    times = hourly_open_times(weeks=3)
    prices = [Decimal("1.1000") + Decimal("0.0001") * n for n in range(len(times))]
    levels = prior_week_levels(series(times, prices=prices))
    assert levels is not None
    assert levels.period_start == WEEK_OPEN + timedelta(days=7)
    assert levels.open == prices[120]
    assert levels.high == max(prices[120:240]) + Decimal("0.0010")


def test_prior_week_is_absent_rather_than_partial_when_only_one_week_is_held() -> None:
    assert prior_week_levels(series(hourly_open_times(weeks=1))) is None


def test_prior_day_comes_from_saxos_native_daily_bar() -> None:
    """§6.1's reversed recommendation. The levels must match what the owner sees in
    SaxoTraderGO, so they come from the venue's own 1d bar rather than a UTC day we
    aggregate ourselves — and its ``open_time`` is a date **label**, not a start."""
    times = [WEEK_OPEN.replace(hour=0) + timedelta(days=n) for n in range(5)]
    prices = [Decimal("1.10"), Decimal("1.11"), Decimal("1.12"), Decimal("1.13"), Decimal("1.14")]
    levels = prior_day_levels(series(times, timeframe="1d", prices=prices))
    assert levels is not None
    assert levels.open == Decimal("1.14")  # the newest CLOSED daily bar
    assert levels.period_start == times[-1]


def test_prior_day_is_absent_rather_than_zero_when_there_is_no_daily_tail() -> None:
    assert (
        prior_day_levels(series([WEEK_OPEN], timeframe="1d").model_copy(update={"candles": ()}))
        is None
    )


def test_the_daily_open_is_the_first_hourly_bar_after_the_anchor() -> None:
    """Mid-session, the day in progress has an open, and it is the bar that starts at
    17:00 New York — 21:00Z in August. At the anchor instant itself there is not yet a
    bar after it, and the field is absent rather than reaching backwards for one."""
    from sentinel.fx.features import daily_open as daily_open_of

    times = hourly_open_times(weeks=2)
    prices = [Decimal("1.1000") + Decimal("0.0001") * n for n in range(len(times))]
    tail = series(times, prices=prices)

    mid_session = WEEK_OPEN + timedelta(days=8, hours=6)  # Monday 03:00Z of week two
    anchor = daily_anchor(mid_session)
    assert anchor.hour == 21
    opened = daily_open_of(tail, now=mid_session, symbol="EURUSD", alignment=AUGUST_GRID)
    expected = next(c.open for c in tail.candles if c.open_time >= anchor)
    assert opened == expected


# ── the tail trim: ForexConfig.feature_candles_1h finally has a reader ──────


def test_trim_keeps_the_most_recent_candles_and_leaves_a_short_series_alone() -> None:
    """§7.3. The 1h tail is 1200 bars so the spread profile gets ~35 samples per
    hour-of-day instead of ~10; features want the most recent 321 of them, and the
    owner verified live that those 321 give identical values to a direct request."""
    times = hourly_open_times(weeks=3)
    trimmed = trim_to_feature_window(series(times), candles=100)
    assert len(trimmed.candles) == 100
    assert trimmed.candles[-1].open_time == times[-1]
    assert len(trim_to_feature_window(series(times[:50]), candles=100).candles) == 50


def test_a_zero_length_feature_window_is_an_error_not_an_empty_series() -> None:
    with pytest.raises(ValueError, match="at least one candle"):
        trim_to_feature_window(series(hourly_open_times(weeks=1)), candles=0)


# ── defect #18: the USD index ───────────────────────────────────────────────


def test_the_index_is_geometric_so_a_halving_and_a_doubling_cancel() -> None:
    """The property that decides between the two candidate formulas.

    EURUSD halves and GBPUSD doubles: the dollar has strengthened enormously against
    one and weakened equally against the other, so an equal-weight index should not
    move. A geometric mean gives exactly 100. An arithmetic mean of percentage changes
    would give +25%, which is an artefact of the arithmetic and not a fact about the
    dollar.
    """
    index = usd_strength_index(
        {
            "EURUSD": [Decimal("1.2"), Decimal("0.6")],
            "GBPUSD": [Decimal("1.3"), Decimal("2.6")],
            "USDJPY": [Decimal("150"), Decimal("150")],
        }
    )
    assert index is not None
    assert index.value == Decimal("100.0000")
    assert index.change_pct == Decimal("0.0000")


def test_a_dollar_that_strengthens_against_all_three_moves_the_index_up() -> None:
    """Direction, which is the only thing an index is for. EURUSD and GBPUSD quote the
    dollar, so the dollar strengthening means they fall; USDJPY bases it, so it rises."""
    index = usd_strength_index(
        {
            "EURUSD": [Decimal("1.20"), Decimal("1.10")],
            "GBPUSD": [Decimal("1.30"), Decimal("1.20")],
            "USDJPY": [Decimal("150"), Decimal("160")],
        }
    )
    assert index is not None
    assert index.value > Decimal(100)
    assert index.change_pct > 0


def test_the_index_is_absent_rather_than_computed_from_two_of_three_pairs() -> None:
    """§2.1. Two thirds of a dollar index is not a dollar index, and a number computed
    from two pairs while claiming three is worse than no number at all."""
    assert (
        usd_strength_index({"EURUSD": [Decimal("1.2")] * 5, "GBPUSD": [Decimal("1.3")] * 5}) is None
    )


def test_the_index_reports_which_pairs_it_was_built_from() -> None:
    index = usd_strength_index(
        {
            "EURUSD": [Decimal("1.2")] * 30,
            "GBPUSD": [Decimal("1.3")] * 30,
            "USDJPY": [Decimal("150")] * 30,
        }
    )
    assert index is not None
    assert index.pairs == ("EURUSD", "GBPUSD", "USDJPY")
    assert index.change_24h_pct is not None


# ── defect #18: the correlation ────────────────────────────────────────────


def test_correlation_is_on_log_returns_not_prices() -> None:
    """The property that decides the basis, and it matters for §9's rail.

    Two series that both drift upward correlate near 1 on **prices** whatever their
    day-to-day behaviour, so a price-based correlation would report the rail satisfied
    by arithmetic. These two have deliberately opposed returns riding on a shared
    uptrend: on prices they look identical, on returns they are strongly negative.
    """
    up = [Decimal("1.0") + Decimal("0.01") * n for n in range(60)]
    left = [p + (Decimal("0.005") if n % 2 else Decimal("0")) for n, p in enumerate(up)]
    right = [p - (Decimal("0.005") if n % 2 else Decimal("0")) for n, p in enumerate(up)]
    (result,) = cross_pair_correlation({"A": left, "B": right})
    assert result.pair == ("A", "B")
    assert result.coefficient < Decimal("-0.5")


def test_a_flat_series_yields_no_correlation_rather_than_a_zero() -> None:
    """§2.1 again. Zero would read as "unrelated", which is a claim about the data;
    a flat series supports no claim at all."""
    flat = [Decimal("1.1690")] * 60
    moving = [Decimal("1.1") + Decimal("0.001") * n for n in range(60)]
    assert cross_pair_correlation({"A": flat, "B": moving}) == ()


def test_every_pair_is_reported_once_in_a_stable_order() -> None:
    closes = {
        "EURUSD": [Decimal("1.1") + Decimal("0.001") * n for n in range(60)],
        "GBPUSD": [Decimal("1.3") + Decimal("0.002") * n for n in range(60)],
        "USDJPY": [Decimal("150") - Decimal("0.1") * n for n in range(60)],
    }
    result = cross_pair_correlation(closes)
    assert [r.pair for r in result] == [
        ("EURUSD", "GBPUSD"),
        ("EURUSD", "USDJPY"),
        ("GBPUSD", "USDJPY"),
    ]


# ── §2.1, the whole point ──────────────────────────────────────────────────


def built() -> dict[str, ForexFeatures]:
    times = hourly_open_times(weeks=3)
    return compute_cycle_features(
        {
            "EURUSD": tails(
                "EURUSD",
                [Decimal("1.16") + Decimal("0.0001") * n for n in range(len(times))],
                times,
            ),
            "GBPUSD": tails(
                "GBPUSD",
                [Decimal("1.30") + Decimal("0.0002") * n for n in range(len(times))],
                times,
            ),
            "USDJPY": tails(
                "USDJPY", [Decimal("150") - Decimal("0.001") * n for n in range(len(times))], times
            ),
        },
        now=times[-1] + timedelta(hours=1),
        feature_candles_1h=321,
    )


def test_forex_features_carry_no_volume_funding_or_open_interest_field_at_all() -> None:
    """The stronger form of §2.1, the same rule ``ForexSizing`` is held to by defect
    #12's ruling: if a concept does not exist in this market, there is **no field** to
    put it in — not a null, and certainly not a zero."""
    forbidden = ("volume", "funding", "open_interest", "long_short", "liq")
    for model in (ForexFeatures,):
        names = set(model.model_fields)
        assert not [n for n in names if any(bad in n for bad in forbidden)]


def test_the_absence_is_stated_rather_than_merely_present() -> None:
    """§2.1: not quietly dropped. The payload names what this market does not have, so
    a reader — human or model — sees a statement instead of an unexplained silence."""
    features = built()["EURUSD"]
    assert features.unavailable_inputs == UNAVAILABLE_INPUTS
    assert "volume" in features.unavailable_inputs
    assert "funding_rate" in features.unavailable_inputs


def numeric_zeros(payload: Any, path: str = "forex") -> list[str]:
    """Every leaf in a dumped payload that is a numeric zero, by path.

    Shared by the two tests below so the second can prove the first is not vacuous.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            found += numeric_zeros(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found += numeric_zeros(value, f"{path}[{index}]")
    elif isinstance(payload, str) and payload.strip("-").replace(".", "").isdigit():
        if Decimal(payload) == 0:
            found.append(path)
    elif isinstance(payload, int) and not isinstance(payload, bool) and payload == 0:
        found.append(path)
    return found


def unavailable() -> ForexFeatures:
    """One symbol whose tails did not arrive at all — every input unavailable."""
    empty = SymbolTails(symbol="EURUSD", pip=PIP, bid={}, ask={}, alignment_hours_utc=())
    return compute_cycle_features(
        {"EURUSD": empty}, now=WEEK_OPEN + timedelta(hours=30), feature_candles_1h=321
    )["EURUSD"]


def test_no_forex_feature_holds_a_zero_standing_in_for_a_null() -> None:
    """§2.1, tested where it actually bites: when nothing arrived.

    This is the case a naive implementation gets wrong — an empty tail yielding a
    0.0 high, a 0-pip spread and a 0.0 correlation, every one of which reads to a
    model as a measurement rather than as an absence. Every one of them must be
    ``None``, and the collections must be empty rather than full of zeros.
    """
    features = unavailable()
    assert features.prior_day is None
    assert features.prior_week is None
    assert features.daily_open is None
    assert features.weekly_open is None
    assert features.spread is None
    assert features.usd_strength is None
    assert features.correlations == ()
    assert features.bar_alignment_hours_utc == ()
    # And nothing anywhere in the dumped payload is a numeric zero.
    assert numeric_zeros(features.model_dump(mode="json")) == []


def test_the_zero_walk_would_catch_a_zero_if_one_were_there() -> None:
    """The non-vacuity sibling. A check never seen to fail is indistinguishable from
    one that cannot fail, which is this project's own named failure mode."""
    planted = unavailable().model_dump(mode="json")
    planted["daily_open"] = "0.0000"
    planted["spread"] = {"median_pips": "0.0", "samples": 0}
    assert sorted(numeric_zeros(planted)) == [
        "forex.daily_open",
        "forex.spread.median_pips",
        "forex.spread.samples",
    ]


def test_every_optional_field_is_populated_when_the_data_is_there() -> None:
    """The other half: the ``None``s above must be reachable, not permanent."""
    features = built()["EURUSD"]
    assert features.prior_day is not None
    assert features.prior_week is not None
    assert features.weekly_open is not None
    assert features.spread is not None
    assert features.usd_strength is not None
    assert features.correlations != ()


def test_the_recorded_grid_never_shares_a_boundary_with_the_crypto_one() -> None:
    """D-k, carried into the feature payload. Nothing downstream may assume the two
    markets sit on one 4h grid, because in neither season do they."""
    features = built()["EURUSD"]
    assert features.bar_alignment_hours_utc == AUGUST_GRID
    assert set(features.bar_alignment_hours_utc).isdisjoint({0, 4, 8, 12, 16, 20})


def test_the_two_spread_baselines_both_travel_and_stay_distinct() -> None:
    """Defect #15. The gate's global median and the cost model's per-hour median are
    different numbers answering different questions, and conflating them is what made
    §5.3-as-written never fire at the one hour it was written for."""
    spread = built()["EURUSD"].spread
    assert spread is not None
    assert spread.samples > 0
    assert spread.median_pips == Decimal("1.0")
    assert spread.median_this_hour_pips == Decimal("1.0")


def test_cross_pair_features_are_computed_once_and_shared_by_every_symbol() -> None:
    """They cannot be computed per symbol — that is why they do not live in the
    market-blind feature engine, which is the module the crypto golden comes from."""
    features = built()
    assert features["EURUSD"].usd_strength == features["USDJPY"].usd_strength
    assert features["EURUSD"].correlations == features["GBPUSD"].correlations
    assert features["EURUSD"].usd_strength is not None


def test_the_session_label_travels_for_both_now_and_the_last_bar() -> None:
    features = built()["EURUSD"]
    assert isinstance(features.session_now, Session)
    assert isinstance(features.session_of_last_bar, Session)

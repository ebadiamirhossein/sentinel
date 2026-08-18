"""specs/DATA_SOURCES.md §4 truth table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.config import MaxAgeConfig
from sentinel.ingestion.models import (
    BookSnapshot,
    Candle,
    DerivContext,
    FxRate,
    MacroContext,
    NewsContext,
    OHLCVSeries,
    SentimentContext,
)
from sentinel.ingestion.staleness import (
    QualityReport,
    SnapshotParts,
    evaluate,
    is_stale,
    ohlcv_max_age,
    timeframe_to_timedelta,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
TIMEFRAMES = ("15m", "1h", "4h", "1d")
MAX_AGE = MaxAgeConfig()


def series(timeframe: str, *, age: timedelta = timedelta(0)) -> OHLCVSeries:
    return OHLCVSeries(
        source="binance_usdm",
        fetched_at=NOW - age,
        symbol="BTCUSDT",
        timeframe=timeframe,
        candles=(
            Candle(
                open_time=NOW - age,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("1"),
                close=Decimal("2"),
                volume=Decimal("10"),
            ),
        ),
    )


def full_parts(*, age: timedelta = timedelta(0)) -> SnapshotParts:
    """Everything present and fresh."""
    return SnapshotParts(
        ohlcv={tf: series(tf) for tf in TIMEFRAMES},
        derivatives=DerivContext(
            source="binance_usdm",
            fetched_at=NOW - age,
            funding_rate=Decimal("0.0001"),
            open_interest_base=Decimal("1000"),
            long_short_ratio=Decimal("1.5"),
        ),
        orderbook=BookSnapshot(
            source="binance_usdm",
            fetched_at=NOW - age,
            depth_levels=50,
            best_bid=Decimal("100"),
            best_ask=Decimal("101"),
            spread_pct=Decimal("1"),
            bid_notional=Decimal("500"),
            ask_notional=Decimal("400"),
            imbalance=Decimal("0.11"),
        ),
        news=NewsContext(source="cryptopanic", fetched_at=NOW - age, provider="cryptopanic"),
        sentiment=SentimentContext(
            source="alternative.me", fetched_at=NOW - age, value=41, classification="Fear"
        ),
        macro=MacroContext(
            source="coingecko",
            fetched_at=NOW - age,
            btc_dominance_pct=Decimal("56"),
            total_mcap_change_24h_pct=Decimal("0.6"),
        ),
        fx=FxRate(source="frankfurter", fetched_at=NOW - age, rate=Decimal("1.15")),
    )


def run(parts: SnapshotParts) -> QualityReport:
    return evaluate(parts, now=NOW, required_timeframes=TIMEFRAMES, max_age=MAX_AGE)


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("timeframe", "minutes"), [("15m", 15), ("1h", 60), ("4h", 240), ("1d", 1440)]
)
def test_timeframe_conversion(timeframe: str, minutes: int) -> None:
    assert timeframe_to_timedelta(timeframe) == timedelta(minutes=minutes)


def test_unknown_timeframe_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported timeframe"):
        timeframe_to_timedelta("7s")


def test_ohlcv_budget_is_two_times_the_timeframe() -> None:
    """§4: OHLCV tolerates 2x its own timeframe."""
    assert ohlcv_max_age("1h", 2) == timedelta(hours=2)
    assert ohlcv_max_age("15m", 2) == timedelta(minutes=30)


def test_is_stale_boundary_is_inclusive() -> None:
    assert is_stale(NOW - timedelta(minutes=30), NOW, timedelta(minutes=30)) is False
    assert is_stale(NOW - timedelta(minutes=31), NOW, timedelta(minutes=30)) is True


# ── core data → skip the symbol ──────────────────────────────────────────────


def test_all_fresh_is_ok() -> None:
    report = run(full_parts())
    assert report.skip_reason is None
    assert report.degraded_fields == ()
    assert report.is_degraded is False


def test_missing_timeframe_skips_the_symbol() -> None:
    parts = full_parts()
    del parts.ohlcv["1h"]

    report = run(parts)

    assert report.skip_reason == "missing OHLCV 1h"


def test_empty_candles_skip_the_symbol() -> None:
    parts = full_parts()
    parts.ohlcv["4h"] = OHLCVSeries(
        source="binance_usdm", fetched_at=NOW, symbol="BTCUSDT", timeframe="4h", candles=()
    )

    assert run(parts).skip_reason == "empty OHLCV 4h"


def test_stale_ohlcv_skips_the_symbol() -> None:
    parts = full_parts()
    parts.ohlcv["15m"] = series("15m", age=timedelta(minutes=31))  # budget is 30m

    assert run(parts).skip_reason == "stale OHLCV 15m"


def test_ohlcv_within_budget_does_not_skip() -> None:
    parts = full_parts()
    parts.ohlcv["15m"] = series("15m", age=timedelta(minutes=29))

    assert run(parts).skip_reason is None


# ── secondary data → DEGRADED, never substituted ─────────────────────────────


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("orderbook", ("orderbook",)),
        ("sentiment", ("fear_greed",)),
        ("macro", ("btc_dominance",)),
        ("news", ("news",)),
        ("fx", ("eurusd",)),
    ],
)
def test_missing_secondary_field_degrades(field: str, expected: tuple[str, ...]) -> None:
    parts = full_parts()
    setattr(parts, field, None)

    report = run(parts)

    assert report.skip_reason is None
    assert report.degraded_fields == expected
    assert report.is_degraded is True


def test_missing_derivatives_names_every_dependent_field() -> None:
    parts = full_parts()
    parts.derivatives = None

    assert run(parts).degraded_fields == ("funding", "open_interest", "long_short_ratio")


def test_stale_funding_degrades() -> None:
    """Funding budget is 15m."""
    parts = full_parts()
    parts.derivatives = DerivContext(
        source="binance_usdm",
        fetched_at=NOW - timedelta(minutes=16),
        funding_rate=Decimal("0.0001"),
        open_interest_base=Decimal("1000"),
        long_short_ratio=Decimal("1.5"),
    )

    assert run(parts).degraded_fields == ("funding",)


def test_stale_open_interest_degrades_funding_and_oi() -> None:
    """OI budget is 30m; funding's 15m budget is breached first."""
    parts = full_parts()
    parts.derivatives = DerivContext(
        source="binance_usdm",
        fetched_at=NOW - timedelta(minutes=31),
        funding_rate=Decimal("0.0001"),
        open_interest_base=Decimal("1000"),
        long_short_ratio=Decimal("1.5"),
    )

    assert run(parts).degraded_fields == ("funding", "open_interest", "long_short_ratio")


def test_absent_long_short_ratio_degrades_alone() -> None:
    parts = full_parts()
    parts.derivatives = DerivContext(
        source="binance_usdm",
        fetched_at=NOW,
        funding_rate=Decimal("0.0001"),
        open_interest_base=Decimal("1000"),
        long_short_ratio=None,
    )

    assert run(parts).degraded_fields == ("long_short_ratio",)


def test_last_known_good_fx_counts_as_degraded() -> None:
    """A cached rate is honest but not fresh — the analyst is told."""
    parts = full_parts()
    parts.fx = FxRate(
        source="frankfurter",
        fetched_at=NOW,
        rate=Decimal("1.15"),
        is_last_known_good=True,
    )

    assert run(parts).degraded_fields == ("eurusd",)


def test_stale_fear_greed_degrades_after_24h() -> None:
    parts = full_parts()
    parts.sentiment = SentimentContext(
        source="alternative.me",
        fetched_at=NOW - timedelta(hours=25),
        value=41,
        classification="Fear",
    )

    assert run(parts).degraded_fields == ("fear_greed",)


def test_multiple_degradations_are_reported_together_without_duplicates() -> None:
    parts = full_parts()
    parts.derivatives = None
    parts.fx = FxRate(
        source="frankfurter", fetched_at=NOW, rate=Decimal("1.15"), is_last_known_good=True
    )
    parts.news = None

    report = run(parts)

    assert report.degraded_fields == (
        "funding",
        "open_interest",
        "long_short_ratio",
        "news",
        "eurusd",
    )
    assert len(set(report.degraded_fields)) == len(report.degraded_fields)

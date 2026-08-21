"""The tail-consistency check (owner correction C3, spike defect D-d).

**What these tests prove and what they cannot.** They prove the comparison detects a
disagreement of either shape — a changed price or a bar that exists in one read and
not the other. They do **not** prove that Saxo's 1200-bar and 321-bar reads actually
agree: that is a live fact, it can only be established against the API, and
``tools/saxo_record_fixtures.py --check-tail-consistency`` is what establishes it.
Until that has been run, "the 1h tail was widened to 1200 safely" is an expectation,
not a finding, and journal/M10b_REPORT.md says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel.core.markets import Market
from sentinel.fx.tails import compare_tail_overlap
from sentinel.ingestion.models import Candle, OHLCVSeries

START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def tail(
    count: int, *, skip: frozenset[int] = frozenset(), bump: dict[int, str] | None = None
) -> OHLCVSeries:
    bump = bump or {}
    candles = []
    for index in range(count):
        if index in skip:
            continue
        base = Decimal("1.1600") + Decimal(index) / 10000
        close = Decimal(bump[index]) if index in bump else base + Decimal("0.0001")
        candles.append(
            Candle(
                open_time=START + timedelta(hours=index),
                open=base,
                high=base + Decimal("0.0004"),
                low=base - Decimal("0.0004"),
                close=close,
                volume=None,
            )
        )
    return OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=START + timedelta(hours=count),
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=tuple(candles),
    )


def test_two_agreeing_reads_report_nothing() -> None:
    long_tail = tail(120)
    short_tail = OHLCVSeries(
        source=long_tail.source,
        fetched_at=long_tail.fetched_at,
        symbol=long_tail.symbol,
        timeframe=long_tail.timeframe,
        market=Market.FOREX,
        candles=long_tail.candles[-30:],
    )
    assert compare_tail_overlap(long_tail, short_tail) == ()


def test_a_changed_price_in_the_shared_window_is_caught() -> None:
    long_tail = tail(120, bump={100: "9.9999"})
    short_tail = OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=long_tail.fetched_at,
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=tail(120).candles[-30:],
    )
    found = compare_tail_overlap(long_tail, short_tail)
    assert [d.field for d in found] == ["close"]
    assert found[0].long_value == Decimal("9.9999")


def test_an_hour_present_in_only_one_read_is_caught_in_both_directions() -> None:
    """This is D-d's actual shape: the same hour appears or vanishes by framing."""
    full = tail(120)
    missing_from_long = tail(120, skip=frozenset({110}))
    short_tail = OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=full.fetched_at,
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=full.candles[-30:],
    )
    found = compare_tail_overlap(missing_from_long, short_tail)
    assert [(d.field, d.long_value) for d in found] == [("present", None)]

    short_missing = OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=full.fetched_at,
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=tail(120, skip=frozenset({110})).candles[-29:],
    )
    found = compare_tail_overlap(full, short_missing)
    assert [(d.field, d.short_value) for d in found] == [("present", None)]


def test_history_older_than_the_short_read_is_not_a_disagreement() -> None:
    """The long tail exists precisely to carry more history; that is not a difference."""
    long_tail = tail(1200)
    short_tail = OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=long_tail.fetched_at,
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=long_tail.candles[-321:],
    )
    assert compare_tail_overlap(long_tail, short_tail) == ()

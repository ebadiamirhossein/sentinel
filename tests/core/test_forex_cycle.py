"""The forex cycle path — FOREX.md §14 step 8, and the M10b boundary defect.

**Why this file's first test is the one that matters.** M10b-1 shipped a Saxo adapter
whose output could not be drawn: ``OHLCVSeries.to_frame`` puts NaN in the volume column
for a market with no volume, the renderer passed ``volume=True`` unconditionally, and
mplfinance raised ``ValueError('Axis limits cannot be NaN or Inf')`` on the first forex
render. All 1777 tests passed, because the adapter and the renderer were composed for
the first time in the session **after** the one that shipped them.

A milestone boundary is a place where nothing is tested by construction. So
``test_the_whole_forex_path_composes_from_adapter_to_rendered_chart`` runs the real
adapter over a synthetic venue, through the real feature engine, into the real
renderer, and looks at the bytes that come out.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.charts.models import AnnotationKind, ChartSpec
from sentinel.charts.renderer import render_album
from sentinel.core.config import AppConfig, load_config
from sentinel.core.forex_cycle import MARKET_CLOSED_REASON, ForexAssembly, assemble_forex
from sentinel.core.markets import Market
from tests.core.saxo_double import SyntheticSaxo, build

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY"]


async def run(transport: SyntheticSaxo, *, config: AppConfig | None = None) -> ForexAssembly:
    adapter, client = build(transport)
    async with client:
        return await assemble_forex(
            adapter, SYMBOLS, config=config or load_config(), now=transport.now
        )


# ── the composition that spanned the milestone boundary ────────────────────


async def test_the_whole_forex_path_composes_from_adapter_to_rendered_chart() -> None:
    """Adapter -> features -> renderer, with nothing mocked below the HTTP layer.

    Every step of this was tested in M10b-1 and none of the joins were. The single
    most valuable assertion here is that ``render_album`` returns bytes at all.
    """
    assembly = await run(SyntheticSaxo())
    assert [s.symbol for s in assembly.snapshots] == SYMBOLS
    assert assembly.skipped == {}

    snapshot = assembly.snapshots[0]
    charts = render_album(
        snapshot,
        assembly.features[snapshot.symbol],
        tuple(ChartSpec(symbol=snapshot.symbol, timeframe=tf) for tf in ("15m", "1h", "4h")),
        annotations=assembly.annotations[snapshot.symbol],
    )
    assert len(charts) == 3
    assert all(chart.png.startswith(b"\x89PNG") for chart in charts)
    assert all(chart.params.annotations for chart in charts)


async def test_every_forex_candle_carries_no_volume_and_the_charts_show_none() -> None:
    """§2.1 end to end: absent at the adapter, absent in the snapshot, and no panel."""
    assembly = await run(SyntheticSaxo())
    for snapshot in assembly.snapshots:
        for series in snapshot.ohlcv.values():
            assert series.market is Market.FOREX
            assert all(candle.volume is None for candle in series.candles)
    chart = render_album(
        assembly.snapshots[0],
        assembly.features["EURUSD"],
        (ChartSpec(symbol="EURUSD", timeframe="1h"),),
        annotations=assembly.annotations["EURUSD"],
    )[0]
    assert chart.params.spec.volume_panel_ratio == 0.22  # the spec is untouched...
    assert chart.png  # ...and no panel was drawn from it


# ── one read, no anchor ────────────────────────────────────────────────────


async def test_bid_and_ask_come_from_one_read_per_timeframe() -> None:
    """D-d: the same hour can be present or absent depending on the request's anchor,
    so a spread measured across two reads of "the same" window is a real hazard.
    Four timeframes, three symbols, twelve chart requests — not twenty-four."""
    transport = SyntheticSaxo()
    await run(transport)
    assert len(transport.chart_requests) == 12


async def test_no_anchor_is_ever_sent() -> None:
    """§4.4 hazard C. ``Mode`` and ``Time`` are never sent, so the series cannot vary
    with an anchor the way D-d showed it can."""
    transport = SyntheticSaxo()
    await run(transport)
    for params in transport.chart_requests:
        assert set(params) == {"AssetType", "Uic", "Horizon", "Count"}


async def test_the_1h_tail_is_1200_for_the_spread_and_321_for_the_features() -> None:
    """§7.3, owner correction. 321 bars is ~13 days and leaves ~10 samples per
    hour-of-day bucket; a median over ten noisy samples is not a baseline. 1200 is ~50
    days and still one request. Features see the most recent 321 of it — verified live
    on 2026-08-21 to be identical to a direct 321-bar read."""
    transport = SyntheticSaxo()
    assembly = await run(transport)
    asked = {(p["Horizon"], p["Count"]) for p in transport.chart_requests}
    assert ("60", "1200") in asked
    assert len(assembly.snapshots[0].ohlcv["1h"].candles) == 321
    spread = assembly.forex_features["EURUSD"].spread
    assert spread is not None
    assert spread.samples > 1000


# ── failures degrade forex only, and never silently ────────────────────────


async def test_a_closed_market_skips_the_cycle_without_calling_it_a_fault() -> None:
    """§5.1. Closed is a normal state with its own reason code, and the candle-recency
    check must not run inside it — a weekend tail is not stale, it is a weekend."""
    saturday = datetime(2026, 8, 15, 12, tzinfo=UTC)
    assembly = await run(SyntheticSaxo(now=saturday))
    assert assembly.snapshots == []
    assert set(assembly.skipped) == set(SYMBOLS)
    assert set(assembly.skipped.values()) == {MARKET_CLOSED_REASON}


async def test_a_short_read_skips_the_symbol_and_is_named_a_degraded_read() -> None:
    """D-e: an over-requested ``Count`` clamps to 1200 silently and a short window
    returns what it has. Neither is a shorter tail we may analyse."""
    assembly = await run(SyntheticSaxo(short_by=5))
    assert assembly.snapshots == []
    assert all("degraded read" in reason for reason in assembly.skipped.values())


async def test_an_unresolvable_instrument_skips_only_that_symbol() -> None:
    """§4.2: skipped with a named reason, never silently — and never at the cost of
    the other two, which is the isolation property the crypto assembler also has."""
    assembly = await run(SyntheticSaxo(unresolvable=frozenset({"GBPUSD"})))
    assert [s.symbol for s in assembly.snapshots] == ["EURUSD", "USDJPY"]
    assert assembly.skipped == {"GBPUSD": "instrument unresolved"}


async def test_stale_candles_skip_the_symbol_while_the_market_is_open() -> None:
    """Spec defect #14. ``ingestion/staleness`` measures from ``fetched_at``, which is
    always ~now, so it would call a fifty-hour-old weekend tail perfectly fresh. This
    measures from the candle — and only ever while the market is open, which the
    closed-market test above is the other half of."""
    assembly = await run(SyntheticSaxo(stale_hours=10))
    assert assembly.snapshots == []
    assert all("stale candles" in reason for reason in assembly.skipped.values())


async def test_the_same_stale_tail_over_a_weekend_is_closed_not_stale() -> None:
    """The two checks in the right order. The identical data reads as a closed market
    rather than a fault, which is the false-failure §5.1 was trying to prevent."""
    saturday = datetime(2026, 8, 15, 12, tzinfo=UTC)
    assembly = await run(SyntheticSaxo(now=saturday, stale_hours=10))
    assert set(assembly.skipped.values()) == {MARKET_CLOSED_REASON}


# ── what the snapshot carries, and what it deliberately does not ───────────


async def test_the_forex_snapshot_has_no_derivatives_orderbook_or_instrument() -> None:
    """§2.1. This venue has no funding, no open interest and no order book, and the
    crypto ``InstrumentMeta`` has nowhere to put a Uic or a pip anyway — the same
    defect class as #12. Absent, and not a degradation: nothing failed to arrive."""
    assembly = await run(SyntheticSaxo())
    snapshot = assembly.snapshots[0]
    assert snapshot.derivatives is None
    assert snapshot.orderbook is None
    assert snapshot.instrument is None
    assert snapshot.data_quality.value == "OK"
    assert snapshot.degraded_fields == ()


async def test_the_forex_block_rides_inside_the_feature_dict() -> None:
    """Not as a new snapshot section: that would have to join
    ``analyst/serialization.PASSTHROUGH``, which dumps with ``include=``, so crypto's
    payload would gain ``"forex": null`` and the crypto prompt golden would move."""
    assembly = await run(SyntheticSaxo())
    features = assembly.snapshots[0].features
    assert features is not None
    assert "forex" in features
    assert features["forex"]["symbol"] == "EURUSD"
    # The market-blind block is still there and unchanged in shape.
    assert features["schema_version"] == 1
    assert "timeframes" in features


async def test_relative_volume_is_absent_from_the_forex_feature_block() -> None:
    """The market-blind engine's own §2.1 behaviour, confirmed on a real forex tail
    rather than on a hand-built series."""
    assembly = await run(SyntheticSaxo())
    features = assembly.snapshots[0].features
    assert features is not None
    for timeframe in features["timeframes"].values():
        assert timeframe["relative_volume"] is None


async def test_the_four_hour_grid_never_shares_a_boundary_with_crypto() -> None:
    """D-k, read from the data and carried into the features. In August the forex 4h
    grid is 01/05/09/13/17/21; crypto's is 00/04/08/12/16/20. In neither season do
    they meet."""
    assembly = await run(SyntheticSaxo())
    grid = assembly.forex_features["EURUSD"].bar_alignment_hours_utc
    assert grid == (1, 5, 9, 13, 17, 21)
    assert set(grid).isdisjoint({0, 4, 8, 12, 16, 20})


# ── annotations ────────────────────────────────────────────────────────────


async def test_annotations_come_from_the_features_so_they_cannot_disagree() -> None:
    """The line on the chart and the number in the payload are the same number — the
    discipline the EMA overlays already follow."""
    assembly = await run(SyntheticSaxo())
    features = assembly.forex_features["EURUSD"]
    marks = {a.kind: a.price for a in assembly.annotations["EURUSD"] if a.price is not None}
    assert features.prior_day is not None and features.prior_week is not None
    assert marks[AnnotationKind.PRIOR_DAY_HIGH] == features.prior_day.high
    assert marks[AnnotationKind.PRIOR_WEEK_LOW] == features.prior_week.low
    assert marks[AnnotationKind.DAILY_OPEN] == features.daily_open
    assert marks[AnnotationKind.WEEKLY_OPEN] == features.weekly_open


async def test_a_level_the_features_do_not_have_produces_no_annotation() -> None:
    """§2.1: no mark at zero. A one-week tail has no prior week, so there is no
    prior-week line — rather than one drawn at the bottom of the chart."""
    config = load_config()
    forex = config.forex.model_copy(
        update={
            "timeframes": tuple(
                spec.model_copy(update={"candles": 60}) if spec.timeframe == "1h" else spec
                for spec in config.forex.timeframes
            )
        }
    )
    assembly = await run(SyntheticSaxo(), config=config.model_copy(update={"forex": forex}))
    kinds = {a.kind for a in assembly.annotations["EURUSD"]}
    assert AnnotationKind.PRIOR_WEEK_HIGH not in kinds
    assert assembly.forex_features["EURUSD"].prior_week is None
    # ...and the ones it does have are still there.
    assert AnnotationKind.PRIOR_DAY_HIGH in kinds


async def test_session_bands_span_every_charted_timeframe() -> None:
    """A 120-bar 4h chart reaches twenty days back where a 120-bar 1h chart reaches
    five, and each render clips to its own window."""
    assembly = await run(SyntheticSaxo())
    bands = [a for a in assembly.annotations["EURUSD"] if a.kind is AnnotationKind.SESSION_BAND]
    assert bands
    snapshot = assembly.snapshots[0]
    earliest = min(s.candles[0].open_time for s in snapshot.ohlcv.values())
    assert min(b.from_at for b in bands if b.from_at is not None) >= earliest - timedelta(days=1)


@pytest.mark.parametrize("symbol", SYMBOLS)
async def test_every_pair_gets_the_same_cross_pair_view(symbol: str) -> None:
    """The USD index and the correlations are computed once per cycle: they need all
    three pairs, and the per-symbol feature engine cannot produce them."""
    assembly = await run(SyntheticSaxo())
    assert assembly.forex_features[symbol].usd_strength == (
        assembly.forex_features["EURUSD"].usd_strength
    )
    assert assembly.forex_features[symbol].usd_strength is not None

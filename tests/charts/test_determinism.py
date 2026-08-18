"""Byte-identical output — the property that makes a stored signal reproducible.

Byte-identity is asserted for a given matplotlib/mplfinance build; both are
pinned exactly in pyproject.toml precisely so a future rebuild cannot change
rasterisation silently.
"""

from __future__ import annotations

import hashlib

from sentinel.charts.models import ChartSpec
from sentinel.charts.renderer import render
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import OHLCVSeries
from tests.charts.conftest import series_from_cassette


def test_same_ohlcv_yields_identical_bytes(
    series: OHLCVSeries, spec: ChartSpec, features: SymbolFeatures
) -> None:
    first = render(series, spec, features=features)
    second = render(series, spec, features=features)

    assert first.png == second.png
    assert first.sha256 == second.sha256


def test_determinism_survives_fresh_objects(features: SymbolFeatures) -> None:
    """Catches state leaking through module-level matplotlib globals."""
    first = render(
        series_from_cassette(),
        ChartSpec(symbol="BTCUSDT", timeframe="1h"),
        features=features,
    )
    # Render something else in between — a shared figure or rc mutation would
    # show up as a difference on the third render.
    render(series_from_cassette("SOLUSDT", "4h"), ChartSpec(symbol="SOLUSDT", timeframe="4h"))
    third = render(
        series_from_cassette(),
        ChartSpec(symbol="BTCUSDT", timeframe="1h"),
        features=features,
    )

    assert first.png == third.png


def test_png_carries_no_timestamp_metadata(series: OHLCVSeries, spec: ChartSpec) -> None:
    """matplotlib stamps Software/Creation Time by default — that alone would
    make two identical renders differ."""
    png = render(series, spec).png

    header = png[:2048]
    assert b"Creation Time" not in header
    assert b"Software" not in header
    assert b"matplotlib" not in header.lower()


def test_recorded_hash_matches_the_bytes(series: OHLCVSeries, spec: ChartSpec) -> None:
    image = render(series, spec)

    assert image.params.image_sha256 == hashlib.sha256(image.png).hexdigest()


def test_stored_candles_reproduce_the_same_image(
    series: OHLCVSeries, spec: ChartSpec, features: SymbolFeatures
) -> None:
    """PRD F4: a chart must be reproducible from stored OHLCV, not just live data."""
    live = render(series, spec, features=features)

    # Rebuild the series the way the DB path does — same candles, new objects.
    from_storage = OHLCVSeries(
        source=series.source,
        fetched_at=series.fetched_at,
        symbol=series.symbol,
        timeframe=series.timeframe,
        candles=tuple(candle.model_copy() for candle in series.candles),
    )
    replayed = render(from_storage, spec, features=features)

    assert replayed.png == live.png


def test_different_data_yields_different_bytes(spec: ChartSpec) -> None:
    """Sanity check on the determinism assertions above — they'd be vacuous if
    the renderer produced the same image regardless of input."""
    btc = render(series_from_cassette("BTCUSDT", "1h"), spec)
    sol = render(series_from_cassette("SOLUSDT", "1h"), ChartSpec(symbol="SOLUSDT", timeframe="1h"))

    assert btc.png != sol.png

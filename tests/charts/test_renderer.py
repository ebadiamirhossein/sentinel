"""What is actually on the canvas — asserted from pixels, not from intent.

The renderer can happily report that it drew an EMA while drawing nothing (a
single valid point has no line to draw). These tests sample the rendered pixels
so that class of bug fails here instead of reaching the analyst.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from sentinel.charts import theme
from sentinel.charts.models import ChartSpec
from sentinel.charts.renderer import _format_price, render, render_album
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import DataQuality, MarketSnapshot, OHLCVSeries
from tests.charts.conftest import (
    count_colour,
    dominant_colour,
    image_size,
    series_from_cassette,
    snapshot_from_cassettes,
)

HEADER_BAND_PX = 100  # below this, pixels belong to the watermark/EMA key


# ── image basics ─────────────────────────────────────────────────────────────


def test_output_is_a_png_at_the_configured_size(series: OHLCVSeries, spec: ChartSpec) -> None:
    image = render(series, spec)

    assert image.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert image_size(image.png) == (spec.width_px, spec.height_px)
    assert len(image.png) > 20_000  # a blank canvas compresses far smaller


def test_custom_dimensions_are_honoured(series: OHLCVSeries) -> None:
    image = render(series, ChartSpec(symbol="BTCUSDT", timeframe="1h", width_px=800, height_px=600))

    assert image_size(image.png) == (800, 600)


def test_background_is_dark(series: OHLCVSeries, spec: ChartSpec) -> None:
    """Dark theme per M3 — and the analyst sees the same contrast we designed."""
    red, green, blue = dominant_colour(render(series, spec).png)

    assert red + green + blue < 120


# ── EMAs ─────────────────────────────────────────────────────────────────────


def test_emas_are_drawn_as_lines_not_just_announced(
    series: OHLCVSeries, spec: ChartSpec, features: SymbolFeatures
) -> None:
    """Regression: EMA200 was listed in the key while contributing no line.

    A cassette series is 60 candles, so only EMA20 qualifies — the assertion is
    that whatever `emas_drawn` claims has real pixels inside the plot area.
    """
    image = render(series, spec, features=features)

    assert image.params.emas_drawn
    for period in image.params.emas_drawn:
        drawn = count_colour(image.png, theme.EMA_COLOURS[period], min_y=HEADER_BAND_PX)
        assert drawn > 100, f"EMA{period} announced but only {drawn}px in the plot area"


def test_ema_needing_more_history_than_available_is_omitted(
    series: OHLCVSeries, spec: ChartSpec
) -> None:
    """An EMA with a single valid point cannot form a line, so it is not claimed."""
    image = render(series, spec)

    assert 200 not in image.params.emas_drawn  # 60-candle cassette
    assert count_colour(image.png, theme.EMA_COLOURS[200], min_y=HEADER_BAND_PX) < 50


def test_all_three_emas_draw_when_history_allows(spec: ChartSpec) -> None:
    """With window + period - 1 candles, EMA200 spans the whole window."""
    base = series_from_cassette()
    candles = base.candles
    # Stretch the cassette to 320 candles by repeating it — enough history for
    # EMA200 to be valid across a 120-candle window.
    stretched = base.model_copy(update={"candles": tuple((candles * 6)[:320])})

    image = render(stretched, spec)

    assert image.params.emas_drawn == (20, 50, 200)
    for period in (20, 50, 200):
        assert count_colour(image.png, theme.EMA_COLOURS[period], min_y=HEADER_BAND_PX) > 300


# ── levels ───────────────────────────────────────────────────────────────────


def test_levels_are_drawn_and_recorded(
    series: OHLCVSeries, spec: ChartSpec, features: SymbolFeatures
) -> None:
    image = render(series, spec, features=features)

    assert image.params.levels_drawn
    for level in image.params.levels_drawn:
        assert level.timeframe == "1h"
        assert level.kind in {"SUPPORT", "RESISTANCE"}
        assert level.touches >= 1


def test_only_this_timeframes_levels_are_drawn(
    features: SymbolFeatures, snapshot: MarketSnapshot
) -> None:
    """A cross-timeframe overlay would be unreadable; each chart shows its own."""
    four_hour = render(
        snapshot.ohlcv["4h"],
        ChartSpec(symbol="BTCUSDT", timeframe="4h"),
        features=features,
    )

    assert {level.timeframe for level in four_hour.params.levels_drawn} == {"4h"}


def test_level_count_is_capped(features: SymbolFeatures, snapshot: MarketSnapshot) -> None:
    image = render(
        snapshot.ohlcv["1h"],
        ChartSpec(symbol="BTCUSDT", timeframe="1h", max_levels=2),
        features=features,
    )

    assert len(image.params.levels_drawn) <= 2


def test_renders_cleanly_with_no_levels(series: OHLCVSeries, spec: ChartSpec) -> None:
    image = render(series, spec, levels=())

    assert image.params.levels_drawn == ()
    assert len(image.png) > 20_000


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (64578.1, "64,578"),
        (1000.0, "1,000"),
        (75.97, "75.97"),
        (1.5, "1.50"),
        (0.00012345, "0.00012345"),
    ],
)
def test_price_formatting_is_readable_at_every_scale(price: float, expected: str) -> None:
    """`{:,.4g}` renders 64578 as `6.458e+04` — unreadable on a chart."""
    assert _format_price(price) == expected


# ── closed candles and the watermark ─────────────────────────────────────────


def test_in_progress_candle_is_excluded(series: OHLCVSeries, spec: ChartSpec) -> None:
    """Consistent with M2: an unclosed bar would make the image repaint."""
    mid_candle = series.candles[-1].open_time + timedelta(minutes=30)

    image = render(series, spec, now=mid_candle)

    assert image.params.last_candle_at == series.candles[-2].open_time
    assert image.params.last_close == series.candles[-2].close


def test_closed_candle_is_included(series: OHLCVSeries, spec: ChartSpec) -> None:
    after_close = series.candles[-1].open_time + timedelta(hours=2)

    image = render(series, spec, now=after_close)

    assert image.params.last_candle_at == series.candles[-1].open_time


def test_window_limits_the_candles_drawn(series: OHLCVSeries) -> None:
    # `now` past the final candle's close, so windowing is the only thing
    # trimming the series (otherwise the partial-candle drop shifts it by one).
    after_close = series.candles[-1].open_time + timedelta(hours=2)

    image = render(
        series, ChartSpec(symbol="BTCUSDT", timeframe="1h", candle_window=30), now=after_close
    )

    assert image.params.candles_drawn == 30
    assert image.params.first_candle_at == series.candles[-30].open_time
    assert image.params.last_candle_at == series.candles[-1].open_time


def test_short_series_renders_what_exists(series: OHLCVSeries, spec: ChartSpec) -> None:
    short = series.model_copy(update={"candles": series.candles[:15]})

    image = render(short, spec)

    assert image.params.candles_drawn == 15


def test_empty_after_dropping_partial_raises(series: OHLCVSeries, spec: ChartSpec) -> None:
    single = series.model_copy(update={"candles": series.candles[:1]})

    with pytest.raises(ValueError, match="no closed candles"):
        render(single, spec, now=single.candles[0].open_time + timedelta(minutes=1))


# ── degradation ──────────────────────────────────────────────────────────────


def test_degraded_data_is_visible_in_the_image(spec: ChartSpec) -> None:
    """The analyst is told to be stricter on degraded data, so it has to be able
    to see the degradation in the picture, not only in the JSON beside it."""
    degraded = snapshot_from_cassettes(degraded=True)

    image = render(
        degraded.ohlcv["1h"],
        spec,
        data_quality=degraded.data_quality,
        degraded_fields=degraded.degraded_fields,
        now=degraded.captured_at,
    )

    assert image.params.data_quality == "DEGRADED"
    assert image.params.degraded_fields == ("funding", "news")
    banner = count_colour(image.png, theme.DEGRADED)
    assert banner > 400, f"degraded banner barely visible ({banner}px)"


def test_healthy_data_draws_no_banner(series: OHLCVSeries, spec: ChartSpec) -> None:
    image = render(series, spec, data_quality=DataQuality.OK)

    assert count_colour(image.png, theme.DEGRADED) < 100


# ── params and album ─────────────────────────────────────────────────────────


def test_params_capture_everything_needed_to_re_render(
    series: OHLCVSeries, spec: ChartSpec, features: SymbolFeatures
) -> None:
    params = render(series, spec, features=features).params

    assert params.spec == spec
    assert params.source == "binance_usdm"
    assert params.fetched_at == series.fetched_at
    assert params.renderer_version >= 1
    assert isinstance(params.last_close, Decimal)
    assert params.candles_drawn > 0
    assert json.loads(json.dumps(params.to_json_dict()))  # JSON-safe for the signal record


def test_album_covers_the_configured_timeframes(
    snapshot: MarketSnapshot, features: SymbolFeatures
) -> None:
    specs = tuple(ChartSpec(symbol="BTCUSDT", timeframe=tf) for tf in ("15m", "1h", "4h"))

    images = render_album(snapshot, features, specs)

    assert [image.params.spec.timeframe for image in images] == ["15m", "1h", "4h"]
    assert len({image.sha256 for image in images}) == 3  # genuinely different pictures


def test_album_skips_a_missing_timeframe(
    snapshot: MarketSnapshot, features: SymbolFeatures
) -> None:
    specs = (
        ChartSpec(symbol="BTCUSDT", timeframe="1h"),
        ChartSpec(symbol="BTCUSDT", timeframe="30m"),  # not in the snapshot
    )

    images = render_album(snapshot, features, specs)

    assert [image.params.spec.timeframe for image in images] == ["1h"]

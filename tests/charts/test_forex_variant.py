"""The forex chart variant — FOREX.md §6, and the defect that spanned a milestone.

**M10b-1 shipped an adapter whose output could not be drawn, and 1777 tests passed.**
``OHLCVSeries.to_frame`` puts NaN in the volume column for a market with no volume,
and ``_draw`` passed ``volume=True`` unconditionally, so mplfinance raised
``ValueError('Axis limits cannot be NaN or Inf')`` on the first forex render. Nothing
caught it because the adapter and the renderer were composed for the first time in
*this* session — a milestone boundary is a place where nothing is tested by
construction.

``test_the_defect_that_spanned_the_milestone_boundary`` is that composition, pinned.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from PIL import Image

from sentinel.charts import theme
from sentinel.charts.models import AnnotationKind, ChartAnnotation, ChartSpec
from sentinel.charts.renderer import render
from sentinel.core.markets import Market
from sentinel.ingestion.models import Candle, OHLCVSeries
from tests.charts.conftest import count_colour

START = datetime(2026, 7, 20, 0, tzinfo=UTC)
NOW = START + timedelta(hours=140)
SPEC = ChartSpec(symbol="EURUSD", timeframe="1h")


def forex_series(*, bars: int = 140) -> OHLCVSeries:
    times = [START + timedelta(hours=i) for i in range(bars)]
    prices = [Decimal("1.1690") + Decimal("0.0004") * (i % 20) for i in range(bars)]
    step = Decimal("0.0006")
    return OHLCVSeries(
        source="saxo_fxspot",
        fetched_at=NOW,
        symbol="EURUSD",
        timeframe="1h",
        market=Market.FOREX,
        candles=tuple(
            Candle(
                open_time=at,
                open=price,
                high=price + Decimal("0.0012"),
                low=price - Decimal("0.0012"),
                close=price + (step if index % 2 else -step),
                volume=None,
            )
            for index, (at, price) in enumerate(zip(times, prices, strict=True))
        ),
    )


def crypto_series(*, bars: int = 140) -> OHLCVSeries:
    times = [START + timedelta(hours=i) for i in range(bars)]
    prices = [Decimal("64000") + Decimal("50") * (i % 20) for i in range(bars)]
    step = Decimal("60")
    return OHLCVSeries(
        source="binance_usdm",
        fetched_at=NOW,
        symbol="BTCUSDT",
        timeframe="1h",
        market=Market.CRYPTO,
        candles=tuple(
            Candle(
                open_time=at,
                open=price,
                high=price + Decimal("120"),
                low=price - Decimal("120"),
                close=price + (step if index % 2 else -step),
                volume=Decimal("1500"),
            )
            for index, (at, price) in enumerate(zip(times, prices, strict=True))
        ),
    )


def bottom_row_is_plot(png: bytes) -> bool:
    """Does the panel reach the bottom of the plot area rather than stopping short?"""
    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    return bool(pixels.shape[0] > 0)


# ── the composition defect ──────────────────────────────────────────────────


def test_the_defect_that_spanned_the_milestone_boundary() -> None:
    """A forex series renders at all.

    This one assertion is the whole finding: before M10b-2 it raised
    ``ValueError('Axis limits cannot be NaN or Inf')``, because a market with no
    volume met a renderer that always drew a volume panel. Both halves were correct
    and tested; the seam between them was neither.
    """
    image = render(forex_series(), SPEC, now=NOW)
    assert image.png.startswith(b"\x89PNG")
    assert image.sha256


def test_a_forex_series_gets_no_volume_panel_at_all_rather_than_an_empty_one() -> None:
    """§6. An empty panel would be a picture of missing data presented as data.

    Proved on pixels: the volume bar colours appear nowhere on a forex chart, and the
    price panel claims the canvas the volume panel would have used — so the bottom of
    the plot is price, not blank space where a panel used to be.
    """
    forex = render(forex_series(), SPEC, now=NOW)
    crypto = render(crypto_series(), ChartSpec(symbol="BTCUSDT", timeframe="1h"), now=NOW)

    # Tolerance 4, not the default 28: the volume hues sit close enough to the panel
    # background that a loose match counts grid pixels and would pass on anything.
    assert count_colour(forex.png, theme.VOLUME_UP, tolerance=4, min_y=110) == 0
    assert count_colour(forex.png, theme.VOLUME_DOWN, tolerance=4, min_y=110) == 0
    # The non-vacuity half: the crypto branch is still taken, and still draws them.
    assert count_colour(crypto.png, theme.VOLUME_UP, tolerance=4, min_y=110) > 1000
    assert count_colour(crypto.png, theme.VOLUME_DOWN, tolerance=4, min_y=110) > 1000


def test_the_price_panel_claims_the_canvas_the_volume_panel_would_have_used() -> None:
    """``_claim_canvas``'s no-volume branch. Candle pixels must reach further down the
    figure than they do on a chart that reserves 22% of it for volume."""
    forex = render(forex_series(), SPEC, now=NOW)
    crypto = render(crypto_series(), ChartSpec(symbol="BTCUSDT", timeframe="1h"), now=NOW)

    def lowest_candle_row(png: bytes) -> int:
        pixels = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.int16)
        target = np.array(
            [int(theme.UP[1:3], 16), int(theme.UP[3:5], 16), int(theme.UP[5:7], 16)],
            dtype=np.int16,
        )
        rows = np.where((np.abs(pixels - target).max(axis=2) <= 28).any(axis=1))[0]
        return int(rows.max())

    assert lowest_candle_row(forex.png) > lowest_candle_row(crypto.png)


def test_the_volume_decision_is_read_from_the_data_not_from_the_spec() -> None:
    """journal/M10b_REPORT.md §12's constraint on this milestone.

    ``ChartSpec`` is dumped inside every params record, crypto's included, so a
    ``draw_volume`` field there would move the crypto chart golden for a market that
    does not have the concept. The same spec draws a volume panel or not depending
    entirely on whether the candles carry volume.
    """
    assert "volume_panel_ratio" in ChartSpec.model_fields
    assert not [f for f in ChartSpec.model_fields if "draw" in f or "market" in f]

    spec = ChartSpec(symbol="X", timeframe="1h")
    with_volume = render(crypto_series(), spec, now=NOW)
    without = render(forex_series(), spec, now=NOW)
    assert with_volume.params.spec == without.params.spec  # identical spec, different picture
    assert with_volume.sha256 != without.sha256


# ── annotations: drawn, and recorded (owner ruling G1) ─────────────────────


def marks() -> tuple[ChartAnnotation, ...]:
    return (
        ChartAnnotation(kind=AnnotationKind.PRIOR_DAY_HIGH, label="PDH", price=Decimal("1.1760")),
        ChartAnnotation(kind=AnnotationKind.PRIOR_DAY_LOW, label="PDL", price=Decimal("1.1690")),
        ChartAnnotation(kind=AnnotationKind.DAILY_OPEN, label="DO", price=Decimal("1.1725")),
        ChartAnnotation(
            kind=AnnotationKind.SESSION_BAND,
            label="LDN-NY",
            from_at=START + timedelta(hours=100),
            to_at=START + timedelta(hours=104),
        ),
    )


def test_prior_day_and_prior_week_levels_are_drawn() -> None:
    plain = render(forex_series(), SPEC, now=NOW)
    marked = render(forex_series(), SPEC, annotations=marks(), now=NOW)
    assert count_colour(marked.png, theme.REFERENCE, min_y=110) > count_colour(
        plain.png, theme.REFERENCE, min_y=110
    )
    assert count_colour(marked.png, theme.REFERENCE_OPEN, min_y=110) > 0


def test_session_shading_is_drawn_behind_the_candles() -> None:
    band_only = (marks()[3],)
    plain = render(forex_series(), SPEC, now=NOW)
    shaded = render(forex_series(), SPEC, annotations=band_only, now=NOW)
    assert shaded.sha256 != plain.sha256
    assert len(shaded.params.annotations) == 1


def test_the_record_carries_every_line_and_band_actually_drawn() -> None:
    """Owner ruling G1: the reconstruction record has to be complete.

    A chart that draws a line nothing in the database explains breaks the property M3
    exists for, and "forex reaches no signal row yet" is true only until the day it
    does.
    """
    image = render(forex_series(), SPEC, annotations=marks(), now=NOW)
    recorded = image.params.to_json_dict()["annotations"]
    assert len(recorded) == 4
    assert {a["kind"] for a in recorded} == {
        "prior_day_high",
        "prior_day_low",
        "daily_open",
        "session_band",
    }
    assert all(a["price"] is not None for a in recorded if a["kind"] != "session_band")


def test_the_record_reports_what_was_drawn_not_what_was_asked_for() -> None:
    """An off-chart line is skipped — the same rule S/R levels follow — so recording
    the *request* would describe an image that does not exist."""
    off_chart = ChartAnnotation(
        kind=AnnotationKind.PRIOR_WEEK_HIGH, label="PWH", price=Decimal("9.99")
    )
    image = render(forex_series(), SPEC, annotations=(*marks(), off_chart), now=NOW)
    assert len(image.params.annotations) == 4
    assert AnnotationKind.PRIOR_WEEK_HIGH not in {a.kind for a in image.params.annotations}


def test_a_crypto_chart_record_has_no_annotations_key_at_all() -> None:
    """Not ``[]``, not ``null`` — absent. That is what keeps the crypto chart golden
    byte-identical while the forex record stays complete (owner ruling G1)."""
    crypto = render(crypto_series(), ChartSpec(symbol="BTCUSDT", timeframe="1h"), now=NOW)
    assert "annotations" not in crypto.params.to_json_dict()
    assert "annotations" not in crypto.params.model_dump(mode="json")


def test_the_omission_is_conditional_and_not_permanent() -> None:
    """The non-vacuity sibling: a field that never serialises would pass the test above
    just as well, and would record nothing for forex either."""
    forex = render(forex_series(), SPEC, annotations=marks(), now=NOW)
    assert "annotations" in forex.params.to_json_dict()


def test_a_forex_chart_carries_no_funding_annotation() -> None:
    """§6: "no funding annotation". Forex has swap/rollover, which is a different thing
    charged at a different time, and it is never called funding (defect #12's rule)."""
    kinds = {kind.value for kind in AnnotationKind}
    assert not [k for k in kinds if "funding" in k]


# ── the annotation contract itself ─────────────────────────────────────────


def test_a_band_may_not_carry_a_price_and_a_line_may_not_carry_an_interval() -> None:
    """§2.1 made unrepresentable rather than merely discouraged. A zero price on a
    session band is exactly the fabricated number the section forbids."""
    with pytest.raises(ValueError, match="has no price"):
        ChartAnnotation(
            kind=AnnotationKind.SESSION_BAND,
            label="x",
            price=Decimal("0"),
            from_at=START,
            to_at=NOW,
        )
    with pytest.raises(ValueError, match="has no interval"):
        ChartAnnotation(
            kind=AnnotationKind.DAILY_OPEN, label="x", price=Decimal("1.17"), from_at=START
        )
    with pytest.raises(ValueError, match="needs a price"):
        ChartAnnotation(kind=AnnotationKind.DAILY_OPEN, label="x")
    with pytest.raises(ValueError, match="needs both ends"):
        ChartAnnotation(kind=AnnotationKind.SESSION_BAND, label="x", from_at=START)


def test_a_band_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="ends at or before it starts"):
        ChartAnnotation(kind=AnnotationKind.SESSION_BAND, label="x", from_at=NOW, to_at=START)


def test_a_forex_chart_is_byte_identical_on_a_second_render() -> None:
    """M3's promise, extended to the new drawing path: same input, same bytes."""
    first = render(forex_series(), SPEC, annotations=marks(), now=NOW)
    second = render(forex_series(), SPEC, annotations=marks(), now=NOW)
    assert first.png == second.png
    assert first.params.to_json_dict() == second.params.to_json_dict()

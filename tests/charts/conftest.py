"""Chart fixtures. Cassette-backed; nothing here touches the network."""

from __future__ import annotations

import io

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image

from sentinel.charts.models import ChartSpec
from sentinel.core.config import FeaturesConfig
from sentinel.features import compute as compute_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot, OHLCVSeries
from tests.market_double import series_from_cassette, snapshot_from_cassettes


@pytest.fixture
def series() -> OHLCVSeries:
    return series_from_cassette()


@pytest.fixture
def snapshot() -> MarketSnapshot:
    return snapshot_from_cassettes()


@pytest.fixture
def features(snapshot: MarketSnapshot) -> SymbolFeatures:
    return compute_features(snapshot, FeaturesConfig())


@pytest.fixture
def spec() -> ChartSpec:
    return ChartSpec(symbol="BTCUSDT", timeframe="1h")


# ── pixel helpers: assert what is actually on the canvas ─────────────────────


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _pixels(png: bytes) -> npt.NDArray[np.int16]:
    """(height, width, 3) RGB array — signed so colour differences don't wrap."""
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.int16)


def count_colour(png: bytes, hex_colour: str, *, tolerance: int = 28, min_y: int = 0) -> int:
    """Pixels close to a colour. ``min_y`` skips the header band."""
    pixels = _pixels(png)[min_y:]
    target = np.array(_rgb(hex_colour), dtype=np.int16)
    return int((np.abs(pixels - target).max(axis=2) <= tolerance).sum())


def dominant_colour(png: bytes) -> tuple[int, int, int]:
    pixels = _pixels(png).reshape(-1, 3)
    colours, counts = np.unique(pixels, axis=0, return_counts=True)
    red, green, blue = colours[int(counts.argmax())]
    return (int(red), int(green), int(blue))


def image_size(png: bytes) -> tuple[int, int]:
    height, width, _ = _pixels(png).shape
    return (width, height)

"""Chart fixtures. Cassette-backed; nothing here touches the network."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from PIL import Image

from sentinel.charts.models import ChartSpec
from sentinel.core.config import FeaturesConfig
from sentinel.features import compute as compute_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import Candle, DataQuality, MarketSnapshot, OHLCVSeries

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"


def series_from_cassette(symbol: str = "BTCUSDT", timeframe: str = "1h") -> OHLCVSeries:
    rows = json.loads((CASSETTES / f"binance_ohlcv_{symbol}_{timeframe}.json").read_text())
    candles = tuple(
        Candle(
            open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
        )
        for row in rows
    )
    return OHLCVSeries(
        source="binance_usdm",
        fetched_at=candles[-1].open_time,
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
    )


def snapshot_from_cassettes(symbol: str = "BTCUSDT", *, degraded: bool = False) -> MarketSnapshot:
    ohlcv = {tf: series_from_cassette(symbol, tf) for tf in ("15m", "1h", "4h", "1d")}
    last = ohlcv["1h"].candles[-1]
    return MarketSnapshot(
        symbol=symbol,
        captured_at=last.open_time + timedelta(hours=2),  # last candle is closed
        last_price=last.close,
        ohlcv=ohlcv,
        data_quality=DataQuality.DEGRADED if degraded else DataQuality.OK,
        degraded_fields=("funding", "news") if degraded else (),
    )


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

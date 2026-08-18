"""Market-data doubles shared by every test package.

The cassette-backed snapshot builders started life in ``tests/charts/conftest.py``
because M3 was the first thing that needed them. M5's screener and analyst need
the same snapshots, and importing one package's conftest from another is a
cross-wiring that breaks the moment either moves -- so they live here, and the
fixtures that wrap them live in the root conftest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sentinel.charts.models import ChartImage, ChartRenderParams, ChartSpec
from sentinel.ingestion.models import (
    Candle,
    DataQuality,
    MarketSnapshot,
    NewsContext,
    NewsItem,
    OHLCVSeries,
)

#: This module now lives at tests/ root, one level up from where these
#: builders started, so the cassette directory is a sibling, not an uncle.
CASSETTES = Path(__file__).resolve().parent / "cassettes"


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


# --------------------------------------------------------------------------- #
# Chart stand-ins (M5). M3 already asserts, pixel by pixel, what lands on a real
# canvas; rendering three PNGs per prompt-assembly test would add seconds of
# matplotlib to every assertion about text. The analyst only base64-encodes the
# bytes and reads the params, so a stand-in walks the same code path.
# --------------------------------------------------------------------------- #

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-chart-bytes"


def fake_chart(symbol: str = "BTCUSDT", timeframe: str = "1h") -> ChartImage:
    params = ChartRenderParams(
        spec=ChartSpec(symbol=symbol, timeframe=timeframe),
        source="binance_usdm",
        fetched_at=datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
        candles_drawn=120,
        first_candle_at=datetime(2026, 8, 13, 11, 0, tzinfo=UTC),
        last_candle_at=datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        last_close=Decimal("64213.6"),
        emas_drawn=(20, 50, 200),
        image_sha256=hashlib.sha256(FAKE_PNG + timeframe.encode()).hexdigest(),
    )
    return ChartImage(png=FAKE_PNG, params=params)


def chart_album(symbol: str = "BTCUSDT") -> list[ChartImage]:
    """Rendered order is ascending (M3 config); the analyst reorders to 4h,1h,15m."""
    return [fake_chart(symbol, tf) for tf in ("15m", "1h", "4h")]


def with_news(snapshot: MarketSnapshot, titles: list[str]) -> MarketSnapshot:
    """Attach headlines -- the one attacker-influenceable input in the pipeline."""
    items = tuple(
        NewsItem(
            title=title,
            source_domain="feed.example",
            published_at=datetime(2026, 8, 18, 11, 30, tzinfo=UTC),
            age_minutes=30 + index,
        )
        for index, title in enumerate(titles)
    )
    return snapshot.model_copy(
        update={
            "news": NewsContext(
                source="rss",
                fetched_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
                provider="rss",
                items=items,
            )
        }
    )


def valid_report_json(symbol: str = "BTCUSDT", **overrides: Any) -> str:
    """A schema-valid analyst report, shaped as the model returns one."""
    payload: dict[str, Any] = {
        "symbol": symbol,
        "candidate_status": "CANDIDATE",
        "setup_type": "trend_pullback",
        "direction": "long",
        "timeframe_label": "intraday",
        "thesis": "4h regime up, 1h pullback into demand, 15m reclaim with volume.",
        "evidence": [{"claim": "1h RSI reset to 48", "source_field": "features.1h.rsi14"}],
        "counter_thesis": "Funding is mildly crowded long.",
        "entry_zone": {"low": 63900.0, "high": 64200.0},
        "stop": 63400.0,
        "targets": [65100.0, 65900.0],
        "invalidation_price": 63400.0,
        "invalidation_text": "1h close below 63400 invalidates the demand shelf.",
        "confidence": 74,
        "data_quality_note": None,
    }
    payload.update(overrides)
    return json.dumps(payload)

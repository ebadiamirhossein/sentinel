"""Feature engine end to end: closed-candle discipline, contracts, attachment."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.core.config import FeaturesConfig
from sentinel.features import engine
from sentinel.features.models import RegimeBasis, SymbolFeatures, TrendRegime, VolatilityRegime
from sentinel.ingestion.models import Candle, DataQuality, MarketSnapshot, OHLCVSeries

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"
CONFIG = FeaturesConfig()


def series_from_cassette(symbol: str, timeframe: str) -> OHLCVSeries:
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


def snapshot_from_cassettes(
    symbol: str = "BTCUSDT", timeframes: tuple[str, ...] = ("15m", "1h", "4h", "1d")
) -> MarketSnapshot:
    ohlcv = {tf: series_from_cassette(symbol, tf) for tf in timeframes}
    last = ohlcv["1h"].candles[-1]
    return MarketSnapshot(
        symbol=symbol,
        # After the last candle opened but before it closes → it is partial.
        captured_at=last.open_time + timedelta(minutes=30),
        last_price=last.close,
        ohlcv=ohlcv,
        data_quality=DataQuality.OK,
    )


# ── closed candles only ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("timeframe", "elapsed", "expected"),
    [
        ("1h", timedelta(minutes=30), True),
        ("1h", timedelta(minutes=59, seconds=59), True),
        ("1h", timedelta(hours=1), False),
        ("1h", timedelta(hours=2), False),
        ("15m", timedelta(minutes=14), True),
        ("15m", timedelta(minutes=15), False),
        ("4h", timedelta(hours=3), True),
        ("1d", timedelta(hours=23), True),
    ],
)
def test_partial_candle_detection(timeframe: str, elapsed: timedelta, expected: bool) -> None:
    opened = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)

    assert engine.is_partial(opened, timeframe, opened + elapsed) is expected


def test_in_progress_candle_is_dropped() -> None:
    """Otherwise every indicator repaints mid-candle and relative volume reads low."""
    series = series_from_cassette("BTCUSDT", "1h")
    now = series.candles[-1].open_time + timedelta(minutes=30)

    features = engine.compute_timeframe(series, CONFIG, now=now)

    assert features is not None
    assert features.partial_candle_dropped is True
    assert features.candles_used == len(series.candles) - 1
    assert features.last_closed_at == series.candles[-2].open_time
    assert features.last_close == series.candles[-2].close


def test_closed_candle_is_kept() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    now = series.candles[-1].open_time + timedelta(hours=1, minutes=1)

    features = engine.compute_timeframe(series, CONFIG, now=now)

    assert features is not None
    assert features.partial_candle_dropped is False
    assert features.candles_used == len(series.candles)


def test_dropping_can_be_disabled() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    now = series.candles[-1].open_time + timedelta(minutes=30)

    features = engine.compute_timeframe(series, FeaturesConfig(drop_partial_candle=False), now=now)

    assert features is not None
    assert features.partial_candle_dropped is False
    assert features.candles_used == len(series.candles)


# ── timeframe features ───────────────────────────────────────────────────────


def test_timeframe_features_are_populated() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    now = series.candles[-1].open_time + timedelta(hours=2)

    features = engine.compute_timeframe(series, CONFIG, now=now)

    assert features is not None
    assert features.ema20 is not None
    assert features.ema50 is not None
    assert features.rsi14 is not None
    assert features.atr14 is not None and features.atr14 > 0
    assert features.atr_pct is not None and 0 < features.atr_pct < 100
    assert features.relative_volume is not None
    assert features.ema_stack is not None
    assert features.trend_regime is not TrendRegime.UNKNOWN
    assert isinstance(features.volatility_regime, VolatilityRegime)


def test_ema200_is_absent_on_a_60_bar_series_and_the_basis_says_so() -> None:
    """No fabrication: the cassette tails are 60 bars, so EMA200 cannot exist."""
    series = series_from_cassette("BTCUSDT", "1d")
    now = series.candles[-1].open_time + timedelta(days=2)

    features = engine.compute_timeframe(series, CONFIG, now=now)

    assert features is not None
    assert features.ema200 is None
    assert features.price_above_ema200 is None
    assert features.regime_basis is RegimeBasis.REDUCED
    assert "200" not in (features.ema_stack or "")


def test_price_flags_track_the_emas() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    now = series.candles[-1].open_time + timedelta(hours=2)

    features = engine.compute_timeframe(series, CONFIG, now=now)

    assert features is not None
    assert features.ema20 is not None and features.price_above_ema20 is not None
    assert features.price_above_ema20 == (features.last_close > features.ema20)


def test_short_series_yields_no_features() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    single = series.model_copy(update={"candles": series.candles[:1]})
    now = single.candles[-1].open_time + timedelta(minutes=30)

    assert engine.compute_timeframe(single, CONFIG, now=now) is None


def test_missing_indicators_stay_none_rather_than_zero() -> None:
    series = series_from_cassette("BTCUSDT", "1h")
    short = series.model_copy(update={"candles": series.candles[:10]})
    now = short.candles[-1].open_time + timedelta(hours=2)

    features = engine.compute_timeframe(short, CONFIG, now=now)

    assert features is not None
    assert features.ema20 is None
    assert features.ema50 is None
    assert features.rsi14 is None  # 10 bars < 14 + 1


# ── whole snapshot ───────────────────────────────────────────────────────────


def test_compute_covers_every_timeframe() -> None:
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    assert set(features.timeframes) == {"15m", "1h", "4h", "1d"}
    assert features.symbol == "BTCUSDT"
    assert features.reference_price > 0


def test_levels_come_only_from_the_configured_timeframes() -> None:
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    assert features.levels
    assert {level.timeframe for level in features.levels} <= {"1h", "4h"}


def test_nearest_levels_bracket_the_reference_price() -> None:
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    if features.nearest_support is not None:
        assert features.nearest_support < features.reference_price
        assert features.distance_to_support_pct is not None
        assert features.distance_to_support_pct < 0
    if features.nearest_resistance is not None:
        assert features.nearest_resistance > features.reference_price
        assert features.distance_to_resistance_pct is not None
        assert features.distance_to_resistance_pct > 0


def test_htf_regime_comes_from_the_4h_timeframe() -> None:
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    assert features.htf_regime is features.timeframes["4h"].trend_regime


def test_percentage_changes_are_computed() -> None:
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    assert features.pct_change_1h is not None
    assert features.pct_change_4h is not None
    assert features.pct_change_24h is not None


def test_compute_is_deterministic() -> None:
    snapshot = snapshot_from_cassettes()

    assert engine.compute(snapshot, CONFIG) == engine.compute(snapshot, CONFIG)


def test_missing_timeframe_is_tolerated() -> None:
    snapshot = snapshot_from_cassettes(timeframes=("1h",))

    features = engine.compute(snapshot, CONFIG)

    assert set(features.timeframes) == {"1h"}
    assert features.htf_regime is TrendRegime.UNKNOWN
    assert features.regime_aligned is None


# ── contracts ────────────────────────────────────────────────────────────────


def test_screener_view_matches_the_prompt_spec() -> None:
    """specs/PROMPTS.md §1 lists exactly what the cheap screener receives."""
    features = engine.compute(snapshot_from_cassettes(), CONFIG)

    view = features.screener_view()

    for key in (
        "symbol",
        "last_price",
        "pct_change_1h",
        "pct_change_4h",
        "pct_change_24h",
        "rsi_1h",
        "rsi_4h",
        "ema_stack_1h",
        "relative_volume_1h",
        "atr_pct_1h",
        "distance_to_support_pct",
        "distance_to_resistance_pct",
    ):
        assert key in view, key
    assert json.loads(json.dumps(view))  # JSON-serialisable for the prompt


def test_attach_serializes_features_onto_the_snapshot() -> None:
    snapshot = snapshot_from_cassettes()
    features = engine.compute(snapshot, CONFIG)

    updated = engine.attach(snapshot, features)

    assert updated.features is not None
    assert updated.features["symbol"] == "BTCUSDT"
    assert json.loads(json.dumps(updated.features))  # JSONB-ready
    assert snapshot.features is None  # original untouched (frozen model)


def test_attached_features_round_trip_back_into_the_model() -> None:
    snapshot = snapshot_from_cassettes()
    features = engine.compute(snapshot, CONFIG)

    restored = SymbolFeatures.model_validate(engine.attach(snapshot, features).features)

    assert restored == features


def test_features_are_decimal_not_float() -> None:
    """The risk engine consumes ATR directly — it must not receive a float."""
    features = engine.compute(snapshot_from_cassettes(), CONFIG)
    one_hour = features.timeframes["1h"]

    assert isinstance(one_hour.atr14, Decimal)
    assert isinstance(one_hour.ema20, Decimal)
    assert isinstance(features.reference_price, Decimal)

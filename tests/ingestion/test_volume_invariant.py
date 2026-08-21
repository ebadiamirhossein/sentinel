"""Volume is required for crypto and forbidden for forex (M10b, owner requirement R-b).

``Candle.volume`` became optional so that forex — which has no volume of any kind,
not even tick counts — can say so instead of writing a zero (specs/FOREX.md §2.1).
The risk that creates is that "nullable" quietly becomes "sometimes missing" for
**crypto** as well, which would disable relative volume with nothing to notice it by.

So the permission is policed in both directions, and both directions are tested.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sentinel.core.config import FeaturesConfig
from sentinel.core.markets import Market
from sentinel.features.engine import compute_timeframe
from sentinel.ingestion.models import Candle, MarketSnapshot, OHLCVSeries
from sentinel.storage.repositories import candle_rows

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def bars(count: int, *, volume: Decimal | None) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            open_time=NOW - timedelta(hours=count - index),
            open=Decimal("1.1690"),
            high=Decimal("1.1695"),
            low=Decimal("1.1685"),
            close=Decimal("1.1692"),
            volume=volume,
        )
        for index in range(count)
    )


def series(market: Market, *, volume: Decimal | None, timeframe: str = "1h") -> OHLCVSeries:
    return OHLCVSeries(
        source="test",
        fetched_at=NOW,
        symbol="EURUSD" if market is Market.FOREX else "BTCUSDT",
        timeframe=timeframe,
        candles=bars(30, volume=volume),
        market=market,
    )


def test_a_crypto_candle_without_volume_is_rejected() -> None:
    with pytest.raises(ValueError, match="has no volume"):
        series(Market.CRYPTO, volume=None)


def test_a_forex_candle_with_volume_is_rejected() -> None:
    with pytest.raises(ValueError, match="no volume of any kind"):
        series(Market.FOREX, volume=Decimal("1234"))


def test_a_forex_candle_with_a_zero_volume_is_rejected_too() -> None:
    """Zero is the substitution §2.1 forbids, and it is the one somebody would reach for."""
    with pytest.raises(ValueError, match="fabricated"):
        series(Market.FOREX, volume=Decimal("0"))


def test_the_two_valid_combinations_are_accepted() -> None:
    assert series(Market.CRYPTO, volume=Decimal("12.5")).candles[0].volume == Decimal("12.5")
    assert series(Market.FOREX, volume=None).candles[0].volume is None


def test_the_default_market_is_crypto_so_existing_construction_still_requires_volume() -> None:
    """Every pre-M10b call site omits ``market``; none of them may become lenient."""
    with pytest.raises(ValueError, match="has no volume"):
        OHLCVSeries(
            source="test",
            fetched_at=NOW,
            symbol="BTCUSDT",
            timeframe="1h",
            candles=bars(3, volume=None),
        )


def test_a_volumeless_series_frames_as_nan_never_zero() -> None:
    """A 0.0 in the frame is indistinguishable from a genuinely silent bar."""
    frame = series(Market.FOREX, volume=None).to_frame()
    assert frame["volume"].isna().all()
    assert not (frame["volume"] == 0.0).any()


def test_relative_volume_is_absent_rather_than_computed_from_nothing() -> None:
    features = compute_timeframe(
        series(Market.FOREX, volume=None), FeaturesConfig(), now=NOW + timedelta(hours=1)
    )
    assert features is not None
    assert features.relative_volume is None
    # And the crypto path still computes it, so the guard above is narrow.
    crypto = compute_timeframe(
        series(Market.CRYPTO, volume=Decimal("12.5")),
        FeaturesConfig(),
        now=NOW + timedelta(hours=1),
    )
    assert crypto is not None
    assert crypto.relative_volume is not None
    assert not math.isnan(float(crypto.relative_volume))


def test_candle_rows_refuses_a_series_whose_market_disagrees_with_the_repository() -> None:
    """The seam where a forex series could land as crypto rows with null volumes."""
    snapshot = MarketSnapshot(
        symbol="EURUSD",
        captured_at=NOW,
        last_price=Decimal("1.1692"),
        ohlcv={"1h": series(Market.FOREX, volume=None)},
    )
    with pytest.raises(ValueError, match="scoped to crypto"):
        candle_rows(snapshot, market=Market.CRYPTO)

    rows = candle_rows(snapshot, market=Market.FOREX)
    assert rows and all(row["volume"] is None for row in rows)
    assert all(row["market"] == "forex" for row in rows)

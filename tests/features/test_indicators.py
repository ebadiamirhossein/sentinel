"""Indicators: golden files, an independent recurrence, closed forms, and a
third-party oracle.

Goldens alone would only prove the code agrees with itself, so every golden value
is *also* checked against a from-scratch NumPy implementation written here, and
against `pandas-ta` as an outside opinion. Tolerance is 1e-8 as specified.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest

from sentinel.features import indicators

TOLERANCE = 1e-8
GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
CASSETTES = Path(__file__).resolve().parents[1] / "cassettes"
CASES = [
    (symbol, timeframe)
    for symbol in ("BTCUSDT", "SOLUSDT")
    for timeframe in ("15m", "1h", "4h", "1d")
]


def load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    import json

    rows = json.loads((CASSETTES / f"binance_ohlcv_{symbol}_{timeframe}.json").read_text())
    frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame.set_index("open_time")


def load_golden(symbol: str, timeframe: str) -> pd.DataFrame:
    return pd.read_csv(GOLDEN_DIR / f"{symbol}_{timeframe}.csv", index_col=0, parse_dates=True)


def assert_close(actual: pd.Series, expected: pd.Series, name: str) -> None:
    both = pd.concat([actual, expected], axis=1).dropna()
    assert not both.empty, f"{name}: nothing to compare"
    diff = (both.iloc[:, 0] - both.iloc[:, 1]).abs().max()
    assert diff <= TOLERANCE, f"{name}: max diff {diff:.3e} exceeds {TOLERANCE:.0e}"
    # NaN warm-ups must line up exactly too, or the goldens hide an off-by-one.
    assert actual.isna().tolist() == expected.isna().tolist(), f"{name}: warm-up differs"


# ── independent reference implementations (deliberately not the production code) ──


def ref_ema(values: npt.NDArray[np.float64], length: int) -> npt.NDArray[np.float64]:
    out = np.full(values.shape, np.nan)
    if values.size < length:
        return out
    out[length - 1] = values[:length].mean()
    alpha = 2.0 / (length + 1.0)
    for i in range(length, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def ref_rsi(values: npt.NDArray[np.float64], length: int = 14) -> npt.NDArray[np.float64]:
    delta = np.diff(values)
    gain = np.clip(delta, 0.0, None)
    loss = np.clip(-delta, 0.0, None)
    n = values.size
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    avg_gain[length] = gain[:length].mean()
    avg_loss[length] = loss[:length].mean()
    for i in range(length + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (length - 1) + gain[i - 1]) / length
        avg_loss[i] = (avg_loss[i - 1] * (length - 1) + loss[i - 1]) / length
    out = np.full(n, np.nan)
    for i in range(length, n):
        if avg_loss[i] == 0.0:
            out[i] = 50.0 if avg_gain[i] == 0.0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_gain[i] / avg_loss[i])
    return out


def ref_atr(
    high: npt.NDArray[np.float64],
    low: npt.NDArray[np.float64],
    close: npt.NDArray[np.float64],
    length: int = 14,
) -> npt.NDArray[np.float64]:
    n = close.size
    tr = np.full(n, np.nan)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    out = np.full(n, np.nan)
    out[length] = tr[1 : length + 1].mean()
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


# ── golden files ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("symbol", "timeframe"), CASES)
def test_golden_values_match(symbol: str, timeframe: str) -> None:
    frame = load_ohlcv(symbol, timeframe)
    golden = load_golden(symbol, timeframe)
    close, high, low, volume = (
        frame["close"],
        frame["high"],
        frame["low"],
        frame["volume"],
    )

    assert_close(indicators.ema(close, 20), golden["ema20"], "ema20")
    assert_close(indicators.ema(close, 50), golden["ema50"], "ema50")
    assert_close(indicators.rsi(close, 14), golden["rsi14"], "rsi14")
    assert_close(indicators.atr(high, low, close, 14), golden["atr14"], "atr14")
    assert_close(indicators.relative_volume(volume, 20), golden["relative_volume20"], "rel_volume")


@pytest.mark.parametrize(("symbol", "timeframe"), CASES)
def test_matches_independent_implementation(symbol: str, timeframe: str) -> None:
    """The goldens are only worth something if a second implementation agrees."""
    frame = load_ohlcv(symbol, timeframe)
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)

    for length in (20, 50):
        produced = indicators.ema(frame["close"], length).to_numpy()
        expected = ref_ema(close, length)
        np.testing.assert_allclose(produced, expected, rtol=0, atol=TOLERANCE)

    np.testing.assert_allclose(
        indicators.rsi(frame["close"], 14).to_numpy(), ref_rsi(close), rtol=0, atol=TOLERANCE
    )
    np.testing.assert_allclose(
        indicators.atr(frame["high"], frame["low"], frame["close"], 14).to_numpy(),
        ref_atr(high, low, close),
        rtol=0,
        atol=TOLERANCE,
    )


def test_pandas_ta_agrees_once_its_seeding_has_decayed() -> None:
    """Third-party oracle.

    pandas-ta seeds its RSI/ATR differently (unseeded EWM, and it emits values
    during warm-up), so it disagrees early and converges later. Over a long
    series the two must agree closely — that is what makes it a useful outside
    check on our Wilder implementation. See journal/M2_REPORT.md §4.
    """
    pandas_ta = pytest.importorskip("pandas_ta")

    rng = np.random.default_rng(7)
    n = 600
    close = pd.Series(60_000 + np.cumsum(rng.normal(0, 80, n)))
    high = close + np.abs(rng.normal(0, 40, n))
    low = close - np.abs(rng.normal(0, 40, n))

    ours_rsi = indicators.rsi(close, 14).to_numpy()
    theirs_rsi = pandas_ta.rsi(close, length=14).to_numpy()
    assert np.abs(ours_rsi[-1] - theirs_rsi[-1]) < 1e-6

    ours_atr = indicators.atr(high, low, close, 14).to_numpy()
    theirs_atr = pandas_ta.atr(high, low, close, length=14).to_numpy()
    assert np.abs(ours_atr[-1] - theirs_atr[-1]) < 1e-6

    ours_ema = indicators.ema(close, 20).to_numpy()
    theirs_ema = pandas_ta.ema(close, length=20).to_numpy()
    np.testing.assert_allclose(ours_ema[19:], theirs_ema[19:], rtol=0, atol=1e-6)


# ── closed-form sanity ───────────────────────────────────────────────────────


def test_ema_of_a_constant_series_is_that_constant() -> None:
    values = pd.Series([100.0] * 50)
    result = indicators.ema(values, 20).dropna()
    assert (result == 100.0).all()


def test_ema_seed_is_the_sma() -> None:
    values = pd.Series([float(i) for i in range(1, 41)])
    result = indicators.ema(values, 20)
    assert result.iloc[19] == pytest.approx(sum(range(1, 21)) / 20, abs=TOLERANCE)
    assert result.iloc[:19].isna().all()


def test_rsi_of_a_monotonic_rise_is_100() -> None:
    values = pd.Series([float(i) for i in range(1, 60)])
    assert indicators.rsi(values, 14).dropna().eq(100.0).all()


def test_rsi_of_a_monotonic_fall_is_0() -> None:
    values = pd.Series([float(i) for i in range(60, 1, -1)])
    assert indicators.rsi(values, 14).dropna().eq(0.0).all()


def test_rsi_of_a_flat_series_is_50() -> None:
    """No gains and no losses: 0/0 is not 100 — a flat tape is neutral."""
    values = pd.Series([100.0] * 40)
    assert indicators.rsi(values, 14).dropna().eq(50.0).all()


def test_atr_of_constant_range_candles_is_that_range() -> None:
    close = pd.Series([100.0] * 40)
    high = pd.Series([102.0] * 40)
    low = pd.Series([98.0] * 40)
    assert indicators.atr(high, low, close, 14).dropna().eq(4.0).all()


def test_true_range_uses_the_previous_close_when_it_gaps() -> None:
    high = pd.Series([10.0, 20.0])
    low = pd.Series([9.0, 19.0])
    close = pd.Series([9.5, 19.5])

    result = indicators.true_range(high, low, close)

    assert result.iloc[0] == pytest.approx(1.0)  # first bar: high - low
    assert result.iloc[1] == pytest.approx(20.0 - 9.5)  # gap up vs previous close


def test_relative_volume_excludes_the_current_bar() -> None:
    volume = pd.Series([100.0] * 20 + [300.0])
    assert indicators.relative_volume(volume, 20).iloc[-1] == pytest.approx(3.0)


def test_relative_volume_of_a_steady_tape_is_one() -> None:
    volume = pd.Series([50.0] * 30)
    assert indicators.relative_volume(volume, 20).dropna().eq(1.0).all()


# ── warm-up and guards ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("factory", "length", "first_valid"),
    [
        (lambda s: indicators.ema(s, 20), 20, 19),
        (lambda s: indicators.rsi(s, 14), 14, 14),
        (lambda s: indicators.relative_volume(s, 20), 20, 20),
    ],
)
def test_warm_up_is_masked(factory, length: int, first_valid: int) -> None:  # type: ignore[no-untyped-def]
    """No value before the indicator is defined — pandas-ta's failing here is
    exactly why these are implemented directly."""
    values = pd.Series([float(100 + i % 7) for i in range(60)])
    result = factory(values)
    assert result.iloc[:first_valid].isna().all()
    assert not np.isnan(result.iloc[first_valid])


def test_atr_warm_up_is_masked() -> None:
    frame = load_ohlcv("BTCUSDT", "1h")
    result = indicators.atr(frame["high"], frame["low"], frame["close"], 14)
    assert result.iloc[:14].isna().all()
    assert not np.isnan(result.iloc[14])


def test_too_short_a_series_yields_all_nan() -> None:
    values = pd.Series([1.0, 2.0, 3.0])
    assert indicators.ema(values, 20).isna().all()


def test_nan_input_is_rejected() -> None:
    values = pd.Series([1.0, np.nan, 3.0])
    with pytest.raises(ValueError, match="must not contain NaN"):
        indicators.ema(values, 2)


def test_pct_change_and_percentile_rank() -> None:
    closes = pd.Series([100.0, 110.0, 121.0])
    assert indicators.pct_change(closes, 1) == pytest.approx(10.0)
    assert indicators.pct_change(closes, 2) == pytest.approx(21.0)
    assert indicators.pct_change(closes, 5) is None

    values = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert indicators.percentile_rank(values, 2.0) == pytest.approx(50.0)
    assert indicators.percentile_rank(values, 4.0) == pytest.approx(100.0)

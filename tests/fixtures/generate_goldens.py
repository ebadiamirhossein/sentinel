"""Regenerate the golden indicator CSVs from the M1 cassettes.

    python tests/fixtures/generate_goldens.py

The goldens are produced by our own implementation, so on their own they would
only prove the code agrees with itself. What makes them meaningful is
`tests/features/test_indicators.py`, which independently checks the same values
against (a) a from-scratch NumPy recurrence, (b) closed-form cases, and (c)
`pandas-ta` as a third-party oracle. Regenerate only when an intended change to
an indicator has been justified in the milestone report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from sentinel.features import indicators

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "cassettes"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

SYMBOLS = ("BTCUSDT", "SOLUSDT")
TIMEFRAMES = ("15m", "1h", "4h", "1d")


def load_frame(symbol: str, timeframe: str) -> pd.DataFrame:
    rows = json.loads((CASSETTES / f"binance_ohlcv_{symbol}_{timeframe}.json").read_text())
    frame = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame.set_index("open_time")


def build(frame: pd.DataFrame) -> pd.DataFrame:
    close, high, low, volume = frame["close"], frame["high"], frame["low"], frame["volume"]
    return pd.DataFrame(
        {
            "close": close,
            "ema20": indicators.ema(close, 20),
            "ema50": indicators.ema(close, 50),
            "rsi14": indicators.rsi(close, 14),
            "atr14": indicators.atr(high, low, close, 14),
            "relative_volume20": indicators.relative_volume(volume, 20),
        }
    )


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            golden = build(load_frame(symbol, timeframe))
            path = GOLDEN_DIR / f"{symbol}_{timeframe}.csv"
            golden.to_csv(path, float_format="%.12f")
            print(f"wrote {path.relative_to(ROOT)} ({len(golden)} rows)")


if __name__ == "__main__":
    main()

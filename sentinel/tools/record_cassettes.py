"""Record fixture payloads from the live public APIs — run manually, never in tests.

    python -m sentinel.tools.record_cassettes                    # everything
    python -m sentinel.tools.record_cassettes --symbols DOGEUSDT # one symbol only
    python -m sentinel.tools.record_cassettes --skip-http        # Binance only

Writes ``tests/cassettes/*.json``. Every call here is public, read-only and
keyless (CryptoPanic is the one exception and is skipped unless a key is present).
The test suite replays these files and never opens a socket.

Candle series are trimmed to keep the fixtures small; the adapter's request
parameters are asserted separately, so fixture length carries no meaning.

**``--symbols`` exists because a re-record is destructive** (M10b-2). Every golden
in this repo is computed from these files, so running the whole recorder to add one
symbol would refresh the other two with fresh market data and move every golden
fixture at once — a regeneration disguised as an addition. Narrowing the run is how
a symbol gets *added*. ``--skip-http`` does the same for the shared non-Binance
fixtures, which no per-symbol golden needs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import httpx

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes"
#: Every symbol this repo has recorded, and the ccxt name it is fetched under.
#: Three price magnitudes on purpose — see tests/golden/pipeline.py and
#: journal/M10b_2_REPORT.md §4a: the chart golden pins one formatter branch per
#: symbol, and BTCUSDT alone left two of the three unpinned.
SYMBOLS = {
    "BTCUSDT": "BTC/USDT:USDT",
    "SOLUSDT": "SOL/USDT:USDT",
    "DOGEUSDT": "DOGE/USDT:USDT",
}
TIMEFRAMES = ("15m", "1h", "4h", "1d")
CANDLE_LIMIT = 60  # trimmed; production limits live in config.yaml

FNG_URL = "https://api.alternative.me/fng/?limit=2"
COINGECKO_URL = "https://api.coingecko.com/api/v3/global"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD"
RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"


def _write(name: str, payload: Any) -> None:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    path = CASSETTE_DIR / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"  wrote {path.relative_to(CASSETTE_DIR.parents[1])} ({path.stat().st_size:,} bytes)")


async def record_binance(symbols: Mapping[str, str] = SYMBOLS) -> None:
    """Record ccxt's *parsed* output — that is what the adapter actually consumes."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    try:
        await exchange.load_markets()
        for plain, ccxt_symbol in symbols.items():
            print(f"binance {plain}")
            for timeframe in TIMEFRAMES:
                candles = await exchange.fetch_ohlcv(ccxt_symbol, timeframe, limit=CANDLE_LIMIT)
                _write(f"binance_ohlcv_{plain}_{timeframe}.json", candles)

            _write(
                f"binance_orderbook_{plain}.json", await exchange.fetch_order_book(ccxt_symbol, 50)
            )
            _write(f"binance_funding_{plain}.json", await exchange.fetch_funding_rate(ccxt_symbol))
            _write(f"binance_oi_{plain}.json", await exchange.fetch_open_interest(ccxt_symbol))
            _write(
                f"binance_oi_history_{plain}.json",
                await exchange.fetch_open_interest_history(ccxt_symbol, "1h", limit=24),
            )
            _write(
                f"binance_long_short_{plain}.json",
                await exchange.fapiDataGetTopLongShortAccountRatio(
                    {"symbol": plain, "period": "1h", "limit": 1}
                ),
            )
            _write(f"binance_market_{plain}.json", exchange.market(ccxt_symbol))
    finally:
        await exchange.close()


async def record_http() -> None:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for name, url in (
            ("alternative_fng.json", FNG_URL),
            ("coingecko_global.json", COINGECKO_URL),
            ("frankfurter_latest.json", FRANKFURTER_URL),
        ):
            print(f"http {url}")
            response = await client.get(url)
            response.raise_for_status()
            _write(name, response.json())

        print(f"rss {RSS_URL}")
        try:
            feed = await client.get(RSS_URL)
            feed.raise_for_status()
            _write("coindesk_rss.xml", feed.text[:60_000])
        except httpx.HTTPError as exc:  # the fallback feed is best-effort
            print(f"  RSS unavailable ({exc}); keep the existing fixture")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        nargs="+",
        choices=sorted(SYMBOLS),
        help="record only these symbols. Omit to record all of them — which "
        "REFRESHES the existing ones and will move every golden.",
    )
    parser.add_argument(
        "--skip-http",
        action="store_true",
        help="skip the shared non-Binance fixtures (sentiment, macro, FX, RSS).",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    selected = {name: SYMBOLS[name] for name in (args.symbols or SYMBOLS)}
    if args.symbols:
        print(f"recording {', '.join(sorted(selected))} only — other fixtures untouched\n")
    else:
        print(
            "recording EVERY symbol and every shared fixture. This refreshes files the\n"
            "goldens are computed from and will move them. Use --symbols to add one.\n"
        )
    await record_binance(selected)
    if not args.skip_http:
        await record_http()
        print(
            "\nCryptoPanic is not recorded automatically (needs a key); "
            "see tests/cassettes/README.md"
        )


if __name__ == "__main__":
    asyncio.run(main())

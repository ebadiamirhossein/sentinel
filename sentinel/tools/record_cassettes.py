"""Record fixture payloads from the live public APIs — run manually, never in tests.

    python -m sentinel.tools.record_cassettes

Writes ``tests/cassettes/*.json``. Every call here is public, read-only and
keyless (CryptoPanic is the one exception and is skipped unless a key is present).
The test suite replays these files and never opens a socket.

Candle series are trimmed to keep the fixtures small; the adapter's request
parameters are asserted separately, so fixture length carries no meaning.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import httpx

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "tests" / "cassettes"
SYMBOLS = {"BTCUSDT": "BTC/USDT:USDT", "SOLUSDT": "SOL/USDT:USDT"}
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


async def record_binance() -> None:
    """Record ccxt's *parsed* output — that is what the adapter actually consumes."""
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    try:
        await exchange.load_markets()
        for plain, ccxt_symbol in SYMBOLS.items():
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


async def main() -> None:
    await record_binance()
    await record_http()
    print(
        "\nCryptoPanic is not recorded automatically (needs a key); see tests/cassettes/README.md"
    )


if __name__ == "__main__":
    asyncio.run(main())

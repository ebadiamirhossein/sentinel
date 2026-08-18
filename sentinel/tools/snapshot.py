"""M1 demo: assemble and print a validated MarketSnapshot.

    python -m sentinel.tools.snapshot BTCUSDT
    python -m sentinel.tools.snapshot BTCUSDT SOLUSDT --json
    python -m sentinel.tools.snapshot BTCUSDT --save      # persist to Postgres

This is the one place that talks to live APIs, and only when you run it. Public
read-only endpoints only; no credential is constructed anywhere in the path.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

import httpx

from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter
from sentinel.ingestion.assembler import SnapshotAssembler
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import MarketSnapshot
from sentinel.storage.db import Database
from sentinel.storage.repositories import (
    FxRateRepository,
    InstrumentMetaRepository,
    SnapshotRepository,
)

log = get_logger(__name__)


def _summary(snapshot: MarketSnapshot) -> str:
    lines = [
        f"── {snapshot.symbol} ─────────────────────────────────────────",
        f"captured_at   {snapshot.captured_at.isoformat()}",
        f"last_price    {snapshot.last_price}",
        f"data_quality  {snapshot.data_quality.value}"
        + (
            f"  degraded: {', '.join(snapshot.degraded_fields)}" if snapshot.degraded_fields else ""
        ),
        "ohlcv         "
        + ", ".join(f"{tf}:{len(series.candles)}" for tf, series in sorted(snapshot.ohlcv.items())),
    ]

    if snapshot.instrument:
        meta = snapshot.instrument
        lines.append(
            f"instrument    tick={meta.tick_size} step={meta.qty_step} "
            f"min_notional={meta.min_notional}"
        )
    if snapshot.derivatives:
        deriv = snapshot.derivatives
        lines.append(
            f"derivatives   funding={deriv.funding_rate} oi={deriv.open_interest_base} "
            f"l/s={deriv.long_short_ratio} oi_points={len(deriv.open_interest_24h)}"
        )
    if snapshot.orderbook:
        book = snapshot.orderbook
        lines.append(
            f"orderbook     imbalance={book.imbalance:.4f} spread={book.spread_pct:.4f}% "
            f"levels={book.depth_levels}"
        )
    if snapshot.sentiment:
        sentiment = snapshot.sentiment
        lines.append(
            f"fear_greed    {sentiment.value} ({sentiment.classification}) delta={sentiment.delta}"
        )
    if snapshot.macro:
        lines.append(
            f"macro         btc_dominance={snapshot.macro.btc_dominance_pct:.2f}% "
            f"mcap_24h={snapshot.macro.total_mcap_change_24h_pct:.2f}%"
        )
    if snapshot.fx:
        suffix = " (last known good)" if snapshot.fx.is_last_known_good else ""
        lines.append(f"eurusd        {snapshot.fx.rate}{suffix}")
    if snapshot.news:
        lines.append(f"news          {snapshot.news.provider}: {len(snapshot.news.items)} items")
        for item in snapshot.news.items[:3]:
            lines.append(f"              · [{item.age_minutes}m] {item.title[:70]}")

    return "\n".join(lines)


async def run(symbols: list[str], *, as_json: bool, save: bool, settings: Settings) -> int:
    cycle_id = uuid4()
    database = Database(settings.secrets.database_url) if save else None

    async with httpx.AsyncClient(follow_redirects=True) as client:
        fetcher = HttpFetcher(
            client,
            timeout_seconds=settings.config.ingestion.request_timeout_seconds,
            max_retries=settings.config.ingestion.max_retries,
            backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
        )

        async def last_known_good_fx() -> object | None:
            if database is None:
                return None
            async with database.session() as session:
                return await FxRateRepository(session).get()

        adapter = BinanceCryptoAdapter(settings.config.ingestion)
        assembler = SnapshotAssembler(
            adapter,
            settings.config,
            news=NewsClient(
                fetcher,
                settings.config.ingestion,
                api_key=(
                    settings.secrets.cryptopanic_api_key.get_secret_value()
                    if settings.secrets.cryptopanic_api_key
                    else None
                ),
            ),
            sentiment=SentimentClient(fetcher, settings.config.ingestion),
            macro=MacroClient(fetcher, settings.config.ingestion),
            fx=FxClient(
                fetcher,
                settings.config.ingestion,
                last_known_good=last_known_good_fx,  # type: ignore[arg-type]
            ),
        )

        try:
            snapshots = await assembler.assemble_many(symbols, cycle_id=cycle_id)
        finally:
            await adapter.close()

        for snapshot in snapshots:
            print(snapshot.model_dump_json(indent=2) if as_json else _summary(snapshot))
            print()

        if save and database is not None:
            async with database.session() as session:
                for snapshot in snapshots:
                    await SnapshotRepository(session).save(snapshot)
                    if snapshot.instrument:
                        await InstrumentMetaRepository(session).upsert(snapshot.instrument)
                    if snapshot.fx and not snapshot.fx.is_last_known_good:
                        await FxRateRepository(session).upsert(snapshot.fx)
                await session.commit()
            print(f"saved {len(snapshots)} snapshot(s) · cycle_id={cycle_id}")

    if database is not None:
        await database.dispose()

    missing = len(symbols) - len(snapshots)
    if missing:
        print(f"{missing} symbol(s) skipped — see the logs above")
    return 0 if snapshots else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble a MarketSnapshot from live data.")
    parser.add_argument("symbols", nargs="+", help="e.g. BTCUSDT SOLUSDT")
    parser.add_argument("--json", action="store_true", help="print the full validated snapshot")
    parser.add_argument("--save", action="store_true", help="persist to Postgres")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)

    raise SystemExit(
        asyncio.run(
            run(
                [s.upper() for s in args.symbols],
                as_json=args.json,
                save=args.save,
                settings=settings,
            )
        )
    )


if __name__ == "__main__":
    main()

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
from decimal import Decimal
from uuid import uuid4

import httpx

from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.features import compute as compute_features
from sentinel.features.engine import attach as attach_features
from sentinel.features.models import LevelKind, SymbolFeatures
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


def _features_summary(features: SymbolFeatures) -> str:
    lines = ["", "features"]
    for timeframe in sorted(features.timeframes, key=_timeframe_order):
        tf = features.timeframes[timeframe]
        lines.append(
            f"  {tf.timeframe:<4} close={tf.last_close} "
            f"ema20={_fmt(tf.ema20)} ema50={_fmt(tf.ema50)} ema200={_fmt(tf.ema200)}"
        )
        lines.append(
            f"       rsi14={_fmt(tf.rsi14, 2)} atr14={_fmt(tf.atr14, 4)} "
            f"atr%={_fmt(tf.atr_pct, 3)} rel_vol={_fmt(tf.relative_volume, 2)} "
            f"stack={tf.ema_stack or 'n/a'}"
        )
        lines.append(
            f"       regime={tf.trend_regime.value} ({tf.regime_basis.value.lower()}) "
            f"vol={tf.volatility_regime.value} bars={tf.candles_used}"
            + ("  [partial candle dropped]" if tf.partial_candle_dropped else "")
        )

    lines.append(f"  htf   regime_4h={features.htf_regime.value} aligned={features.regime_aligned}")
    lines.append(
        f"  chg   1h={_fmt(features.pct_change_1h, 2)}% 4h={_fmt(features.pct_change_4h, 2)}% "
        f"24h={_fmt(features.pct_change_24h, 2)}%"
    )

    if features.levels:
        lines.append("  levels (price · touches · strength · distance)")
        for level in sorted(features.levels, key=lambda lv: -lv.price):
            marker = "R" if level.kind is LevelKind.RESISTANCE else "S"
            lines.append(
                f"    {marker} {level.timeframe:<3} {level.price:>12} · "
                f"{level.touches}x · {level.strength:.2f} · {level.distance_pct:+.2f}%"
            )
    else:
        lines.append("  levels  none detected")

    return "\n".join(lines)


def _timeframe_order(timeframe: str) -> int:
    order = {"15m": 0, "1h": 1, "4h": 2, "1d": 3}
    return order.get(timeframe, 99)


def _fmt(value: Decimal | None, places: int | None = None) -> str:
    if value is None:
        return "n/a"
    if places is None:
        return str(value)
    return f"{value:.{places}f}"


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
            raw_snapshots = await assembler.assemble_many(symbols, cycle_id=cycle_id)
        finally:
            await adapter.close()

        # M2: deterministic features are computed here and travel with the snapshot.
        snapshots = []
        for snapshot in raw_snapshots:
            features = compute_features(snapshot, settings.config.features)
            snapshots.append(attach_features(snapshot, features))

            if as_json:
                print(snapshots[-1].model_dump_json(indent=2))
            else:
                print(_summary(snapshot))
                print(_features_summary(features))
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

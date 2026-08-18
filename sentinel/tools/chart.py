"""M3 demo: render candlestick PNGs for the analyst.

    python -m sentinel.tools.chart SOLUSDT 1h
    python -m sentinel.tools.chart BTCUSDT --all          # 15m + 1h + 4h album
    python -m sentinel.tools.chart BTCUSDT 1h --from-db   # reproduce from stored candles

Each render writes ``<symbol>_<tf>.png`` plus a ``.json`` sidecar holding the
``ChartRenderParams`` — everything needed to redraw the same image.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from sqlalchemy import select

from sentinel.charts.models import ChartImage, ChartSpec
from sentinel.charts.renderer import render
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.features import compute as compute_features
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.adapters.crypto_binance import BinanceCryptoAdapter
from sentinel.ingestion.assembler import SnapshotAssembler
from sentinel.ingestion.clients import FxClient, MacroClient, NewsClient, SentimentClient
from sentinel.ingestion.http import HttpFetcher
from sentinel.ingestion.models import Candle, DataQuality, OHLCVSeries
from sentinel.storage.db import Database
from sentinel.storage.models import MarketSnapshotRow, OhlcvCandleRow

log = get_logger(__name__)


def _spec(symbol: str, timeframe: str, settings: Settings) -> ChartSpec:
    charts = settings.config.charts
    return ChartSpec(
        symbol=symbol,
        timeframe=timeframe,
        candle_window=charts.candle_window,
        width_px=charts.width_px,
        height_px=charts.height_px,
        dpi=charts.dpi,
        volume_panel_ratio=charts.volume_panel_ratio,
        ema_periods=charts.ema_periods,
        max_levels=charts.max_levels,
    )


def _write(image: ChartImage, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{image.params.spec.symbol}_{image.params.spec.timeframe}"
    png_path = out_dir / f"{stem}.png"
    png_path.write_bytes(image.png)
    (out_dir / f"{stem}.json").write_text(
        json.dumps(image.params.to_json_dict(), indent=2), encoding="utf-8"
    )

    params = image.params
    print(f"  {png_path}  ({len(image.png):,} bytes)")
    print(
        f"    {params.candles_drawn} candles · "
        f"EMAs {list(params.emas_drawn)} · {len(params.levels_drawn)} levels · "
        f"{params.spec.width_px}x{params.spec.height_px}@{params.spec.dpi}dpi"
    )
    print(
        f"    last closed {params.last_candle_at:%Y-%m-%d %H:%M} UTC"
        f" · sha256 {params.image_sha256[:16]}"
    )
    if params.data_quality != "OK":
        print(f"    DEGRADED: {', '.join(params.degraded_fields)}")
    return png_path


async def _from_live(symbols: list[str], timeframes: list[str], settings: Settings) -> None:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        fetcher = HttpFetcher(
            client,
            timeout_seconds=settings.config.ingestion.request_timeout_seconds,
            max_retries=settings.config.ingestion.max_retries,
            backoff_seconds=settings.config.ingestion.retry_backoff_seconds,
        )
        adapter = BinanceCryptoAdapter(settings.config.ingestion)
        assembler = SnapshotAssembler(
            adapter,
            settings.config,
            news=NewsClient(fetcher, settings.config.ingestion),
            sentiment=SentimentClient(fetcher, settings.config.ingestion),
            macro=MacroClient(fetcher, settings.config.ingestion),
            fx=FxClient(fetcher, settings.config.ingestion),
        )
        try:
            snapshots = await assembler.assemble_many(symbols)
        finally:
            await adapter.close()

    out_dir = Path(settings.config.charts.output_dir)
    for snapshot in snapshots:
        features = compute_features(snapshot, settings.config.features)
        print(f"\n{snapshot.symbol}  ({snapshot.data_quality.value})")
        for timeframe in timeframes:
            series = snapshot.ohlcv.get(timeframe)
            if series is None:
                print(f"  {timeframe}: no data")
                continue
            image = render(
                series,
                _spec(snapshot.symbol, timeframe, settings),
                features=features,
                data_quality=snapshot.data_quality,
                degraded_fields=snapshot.degraded_fields,
                now=snapshot.captured_at,
            )
            _write(image, out_dir)


async def _from_db(symbol: str, timeframe: str, settings: Settings) -> int:
    """Reproduce a chart from stored candles — PRD F4's real requirement."""
    database = Database(settings.secrets.database_url)
    try:
        async with database.session() as session:
            rows = (
                (
                    await session.execute(
                        select(OhlcvCandleRow)
                        .where(
                            OhlcvCandleRow.symbol == symbol,
                            OhlcvCandleRow.timeframe == timeframe,
                        )
                        .order_by(OhlcvCandleRow.open_time)
                    )
                )
                .scalars()
                .all()
            )
            # The stored feature block carries the S/R levels as they were when
            # the snapshot was taken. Recomputing them here would draw *today's*
            # structure on a historical chart.
            snapshot_row = (
                (
                    await session.execute(
                        select(MarketSnapshotRow)
                        .where(MarketSnapshotRow.symbol == symbol)
                        .order_by(MarketSnapshotRow.captured_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
    finally:
        await database.dispose()

    stored_features: SymbolFeatures | None = None
    if snapshot_row is not None and snapshot_row.context.get("features"):
        stored_features = SymbolFeatures.model_validate(snapshot_row.context["features"])

    if not rows:
        print(f"no stored candles for {symbol} {timeframe} — run the snapshot tool with --save")
        return 1

    series = OHLCVSeries(
        source=rows[-1].source,
        # The most recent fetch governs whether the final bar was still open.
        fetched_at=rows[-1].fetched_at,
        symbol=symbol,
        timeframe=timeframe,
        candles=tuple(
            Candle(
                open_time=row.open_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
            )
            for row in rows
        ),
    )
    print(
        f"\n{symbol} {timeframe} — reproduced from {len(rows)} stored candles"
        + (" (with stored features)" if stored_features else " (no stored features)")
    )
    # Stored candles are NOT all closed: M1 persists the in-progress bar too, so
    # the same partial-candle rule has to run here. Each candle's own
    # `fetched_at` reproduces the decision the live render made.
    image = render(
        series,
        _spec(symbol, timeframe, settings),
        features=stored_features,
        data_quality=DataQuality.OK,
    )
    _write(image, Path(settings.config.charts.output_dir))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Render analyst charts from OHLCV.")
    parser.add_argument("symbol")
    parser.add_argument("timeframe", nargs="?", help="e.g. 1h; omit with --all")
    parser.add_argument("--all", action="store_true", help="render every configured timeframe")
    parser.add_argument("--from-db", action="store_true", help="reproduce from stored candles")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)

    symbol = args.symbol.upper()
    if args.from_db:
        if not args.timeframe:
            parser.error("--from-db needs an explicit timeframe")
        raise SystemExit(asyncio.run(_from_db(symbol, args.timeframe, settings)))

    timeframes = list(settings.config.charts.timeframes) if args.all else [args.timeframe or "1h"]
    asyncio.run(_from_live([symbol], timeframes, settings))


if __name__ == "__main__":
    main()

"""M7 demo: run one tracker tick by hand.

    python -m sentinel.tools.track --once                # against live prices
    python -m sentinel.tools.track --once --simulate 83.05
    python -m sentinel.tools.track --once --no-notify

``--simulate`` replaces the exchange with a single synthetic 1m candle at the
given price. It exists so the demo can *show* a fill without waiting for the
market to produce one — and it is honest about what it is: the candle is
constructed here, in a tool, never in the pipeline (CLAUDE.md forbids fabricated
market values outside fixtures, and this is the fixture).

The tracker makes no LLM calls at all, so this costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.bot.app import build_bot
from sentinel.bot.notifier import TrackerNotifier
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.core.wiring import market_adapter
from sentinel.ingestion.models import Candle, OHLCVSeries
from sentinel.storage.db import Database
from sentinel.tracker.loop import TickResult, TrackerLoop
from sentinel.tracker.prices import PriceFeed

log = get_logger(__name__)


class SimulatedSource:
    """One 1m candle at a chosen price, and nothing else.

    Deliberately not a market: it is a way to drive the state machine through a
    fill or a stop during a demo. Everything it returns is stamped ``simulated``.
    """

    def __init__(self, price: Decimal, *, at: datetime) -> None:
        self._price = price
        self._at = at

    async def ohlcv(self, symbol: str, timeframe: str, limit: int) -> OHLCVSeries:
        candle = Candle(
            open_time=self._at,
            open=self._price,
            high=self._price,
            low=self._price,
            close=self._price,
            volume=Decimal("0"),
        )
        # The invalidation feed drops its last row as in-progress, so hand it two.
        candles = (candle,) if timeframe == "1m" else (candle, candle)
        return OHLCVSeries(
            source="simulated",
            fetched_at=self._at,
            symbol=symbol,
            timeframe=timeframe,
            candles=candles,
        )


def render(result: TickResult, sent: int) -> str:
    lines = [
        "── tracker tick ──",
        f"checked       {result.checked} open signal(s)",
        f"events        {result.events}",
        f"resolved      {result.resolved}",
        f"skipped       {result.skipped}",
        f"telegram      {sent} reply(ies)",
    ]
    if result.paused:
        lines.append("paused        daily loss limit reached (specs/RISK_ENGINE.md §7)")
    for failure in result.failures:
        lines.append(f"  failed      {failure}")
    return "\n".join(lines)


async def run(settings: Settings, *, simulate: Decimal | None, notify: bool) -> int:
    database = Database(settings.secrets.database_url)
    bot = None
    try:
        if simulate is not None:
            from sentinel.core.clock import SystemClock

            source: object = SimulatedSource(
                simulate, at=SystemClock().now() - timedelta(minutes=1)
            )
            print(f"simulating a 1m candle at {simulate} — no exchange call is made.\n")
            result = await TrackerLoop(
                database,
                PriceFeed(source, settings.config.tracker),  # type: ignore[arg-type]
                settings,
            ).tick()
        else:
            async with market_adapter(settings) as adapter:
                result = await TrackerLoop(
                    database, PriceFeed(adapter, settings.config.tracker), settings
                ).tick()

        sent = 0
        if notify and settings.secrets.telegram_bot_token is not None:
            bot = build_bot(settings)
            sent = await TrackerNotifier(
                database,
                bot,
                chat_ids=settings.secrets.allowed_user_ids,
                telegram=settings.config.telegram,
            ).deliver()
    finally:
        if bot is not None:
            await bot.session.close()
        await database.dispose()

    print(render(result, sent))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Sentinel tracker tick.")
    parser.add_argument("--once", action="store_true", help="run a single tick (the default)")
    parser.add_argument(
        "--simulate",
        type=Decimal,
        default=None,
        help="drive the tick with one synthetic 1m candle at this price",
    )
    parser.add_argument(
        "--no-notify", action="store_true", help="record events without posting them"
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    raise SystemExit(asyncio.run(run(settings, simulate=args.simulate, notify=not args.no_notify)))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()

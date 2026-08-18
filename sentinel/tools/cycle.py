"""M7 demo: run the scan cycle by hand.

    python -m sentinel.tools.cycle --once            # one full cycle
    python -m sentinel.tools.cycle --once --dry-run  # ...publishing nothing

This is the same ``CycleOrchestrator`` the scheduler runs, not a parallel path —
running it here and running it every fifteen minutes exercise identical code.

It costs real money: a screener pass over the watchlist is ~$0.02 and each deep
analysis ~$0.32, and the run prints what it spent so the number is never a
mystery. ``--dry-run`` costs exactly the same, because that is the point: it
rehearses the spend as well as the pipeline.

No trade execution, no exchange credentials, no order placement (PRD §3).
"""

from __future__ import annotations

import argparse
import asyncio

from sentinel.bot.app import build_bot
from sentinel.bot.formatting import zone_info
from sentinel.bot.publisher import SignalPublisher
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.core.orchestrator import CycleOrchestrator, CycleResult
from sentinel.storage.db import Database

log = get_logger(__name__)


def render(result: CycleResult) -> str:
    lines = [
        f"── cycle {result.cycle_id} {'(DRY RUN)' if result.dry_run else ''} ──",
        f"symbols       {result.symbols_scanned} scanned of {result.symbols_requested} "
        f"({result.symbols_skipped} skipped at ingestion)",
        f"screener      {result.candidates} candidate(s)",
        f"analyst       {result.analyzed} analysed",
        f"gate          {result.approved} approved",
        f"telegram      {result.published} "
        f"{'stored (nothing sent)' if result.dry_run else 'published'}",
        f"spend         ~${result.spend_usd_estimate} (estimate)",
    ]
    if result.analysis_suspended:
        lines.append(f"suspended     {result.suspended_reason}")
    for symbol, reason in sorted(result.skipped.items()):
        lines.append(f"  skipped     {symbol}: {reason}")
    if result.error:
        lines.append(f"error         {result.error}")
    return "\n".join(lines)


async def run(settings: Settings, *, dry_run: bool) -> int:
    if dry_run and not settings.config.dry_run:
        settings = Settings(
            secrets=settings.secrets,
            config=settings.config.model_copy(update={"dry_run": True}),
        )

    database = Database(settings.secrets.database_url)
    publisher = None
    bot = None
    if settings.secrets.telegram_bot_token is not None and not settings.config.dry_run:
        bot = build_bot(settings)
        publisher = SignalPublisher(
            database,
            bot,
            chat_ids=settings.secrets.allowed_user_ids,
            telegram=settings.config.telegram,
            tz=zone_info(settings.config.telegram.owner_timezone),
        )
    elif settings.config.dry_run:
        print("dry run: the cycle will run in full and publish nothing.\n")
    else:
        print("no TELEGRAM_BOT_TOKEN — an approved plan will be stored, not sent.\n")

    try:
        result = await CycleOrchestrator(settings, database, publisher=publisher).run()
    finally:
        if bot is not None:
            await bot.session.close()
        await database.dispose()

    print(render(result))
    return 0 if result.error is None else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Sentinel scan cycle.")
    parser.add_argument("--once", action="store_true", help="run a single cycle (the default)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry run for this cycle, whatever config.yaml says",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    raise SystemExit(asyncio.run(run(settings, dry_run=args.dry_run)))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()

"""M8 demo: what an admin alert looks like, and whether it reaches your phone.

    python -m sentinel.tools.alert                # render the real state, send nothing
    python -m sentinel.tools.alert --simulate 3   # render a 3-failure alert, send nothing
    python -m sentinel.tools.alert --send         # actually deliver the simulated alert

docs/DEPLOY.md §8 uses ``--send`` once, on purpose, as the only way to prove the
whole chain — bot token, allowlist, network egress from the container's host — is
wired before an outage tries to use it at 3am. The alert path is otherwise
exercised only by an actual outage, which is a bad time to discover a wrong chat id.

Without ``--send`` nothing is delivered: the card is printed here. ``--simulate``
fabricates the *streak*, never the cycle rows — nothing is written to the database
by any mode of this tool.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from sentinel.bot.app import build_bot
from sentinel.bot.cards import alert_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.readmodels import alert_view
from sentinel.core.alerts import Alert, AlertKind, CycleOutcome, cycle_alert
from sentinel.core.clock import SystemClock
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging, get_logger
from sentinel.storage.db import Database
from sentinel.storage.repositories import CycleRepository

log = get_logger(__name__)


def simulated(failures: int, settings: Settings) -> Alert:
    """A failure streak that did not happen, for a card that is otherwise real."""
    now = SystemClock().now()
    return Alert(
        kind=AlertKind.CYCLE_FAILURES,
        failures=failures,
        last_error="simulated by python -m sentinel.tools.alert — nothing is wrong",
        since=now - timedelta(minutes=settings.config.schedule.scan_interval_minutes * failures),
    )


async def run(settings: Settings, *, simulate: int | None, send: bool) -> int:
    config = settings.config
    alert: Alert | None
    if simulate is not None:
        alert = simulated(simulate, settings)
    else:
        database = Database(settings.secrets.database_url)
        try:
            async with database.session() as session:
                rows = await CycleRepository(session).recent(
                    limit=config.alerts.consecutive_cycle_failures * 4
                )
        finally:
            await database.dispose()
        alert = cycle_alert(
            [
                CycleOutcome(
                    status=row.status,
                    started_at=row.started_at,
                    finished_at=row.finished_at,
                    error=row.error,
                )
                for row in rows
            ],
            now=SystemClock().now(),
            stale_after=timedelta(
                minutes=config.schedule.scan_interval_minutes * config.alerts.stale_cycle_multiplier
            ),
            threshold=config.alerts.consecutive_cycle_failures,
        )
        if alert is None:
            print(
                "── admin alerts ──\n"
                f"nothing to send: the last {len(rows)} cycle(s) do not justify an alert "
                f"(threshold {config.alerts.consecutive_cycle_failures} consecutive failures).\n"
                "Use --simulate 3 to see the card, --simulate 3 --send to deliver one."
            )
            return 0

    tz = zone_info(config.telegram.owner_timezone)
    text = alert_card(alert_view(alert), tz)
    print("── the alert ──")
    print(text)

    if not send:
        print("\n(not sent — pass --send to deliver it)")
        return 0

    chat_ids = settings.secrets.allowed_user_ids
    if not chat_ids:
        print("\nTELEGRAM_ALLOWED_USER_IDS is empty — there is nobody to send to.")
        return 1

    bot = build_bot(settings)
    try:
        for chat_id in chat_ids:
            await bot.send_message(chat_id=chat_id, text=text)
            print(f"\nsent to {chat_id}")
    finally:
        await bot.session.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Render or send a Sentinel admin alert.")
    parser.add_argument(
        "--simulate",
        type=int,
        metavar="N",
        help="render an alert for N consecutive cycle failures instead of the real state",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help=(
            "deliver it to every allowlisted chat. Implies --simulate 3 unless a count is "
            "given: this sends a TEST alert, never a real one the scheduler has already sent."
        ),
    )
    args = parser.parse_args()
    if args.send and args.simulate is None:
        args.simulate = 3

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    raise SystemExit(asyncio.run(run(settings, simulate=args.simulate, send=args.send)))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()

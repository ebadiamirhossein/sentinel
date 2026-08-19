"""M7 demo: the ``/stats`` figures, on a terminal.

    python -m sentinel.tools.stats            # 30d
    python -m sentinel.tools.stats 90d
    python -m sentinel.tools.stats all

Reads the same ``build_report`` the Telegram command does, so the two can never
disagree about what a win rate is.
"""

from __future__ import annotations

import argparse
import asyncio

from sentinel.bot.readmodels import stats_view
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.stats.models import StatsReport
from sentinel.stats.queries import build_report, parse_window
from sentinel.storage.db import Database


def render(report: StatsReport) -> str:
    view = stats_view(report)
    since = "all time" if view.since is None else view.since.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"── stats ({view.window} · since {since}) ──"]

    for group in view.groups:
        lines.append("")
        lines.append(f"{group.label}  {group.note}")
        if not group.measured:
            lines.append(
                f"  nothing measured yet — {group.count} signal(s), {group.unfilled} never filled"
            )
            continue
        lines.append(
            f"  trades      {group.filled}  ({group.wins}W / {group.losses}L"
            f"{f' / {group.scratches} scratch' if group.scratches else ''})"
        )
        lines.append(f"  win rate    {group.win_rate_pct}%")
        lines.append(f"  avg R       {group.avg_r}")
        lines.append(f"  total       {group.total_r}R  (€{group.total_eur})")
        lines.append(f"  profit f.   {group.profit_factor}")
        lines.append(f"  max DD      {group.max_drawdown_r}R")
        lines.append(f"  reached TP1 {group.reached_tp1}/{group.filled} ({group.reached_tp1_pct}%)")
        lines.append(f"  costs paid  €{group.costs_eur}")
        if group.unfilled:
            lines.append(f"  unfilled    {group.unfilled} (excluded from the figures above)")

    for title, rows in (
        ("by setup type", view.by_setup),
        ("by prompt version", view.by_prompt_version),
    ):
        if not rows:
            continue
        lines.append("")
        lines.append(f"{title}")
        for row in rows:
            lines.append(
                f"  {row.key:<24} {row.count:>3} signals · {row.win_rate_pct}% · {row.avg_r}R"
            )
    return "\n".join(lines)


async def run(settings: Settings, window: str, user_id: int) -> int:
    from sentinel.core.clock import SystemClock

    database = Database(settings.secrets.database_url)
    try:
        async with database.session() as session:
            report = await build_report(
                session, window=window, now=SystemClock().now(), user_id=user_id
            )
    finally:
        await database.dispose()

    print(render(report))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel performance statistics.")
    parser.add_argument("window", nargs="?", default="30d", help="30d | 90d | all")
    parser.add_argument(
        "--user",
        type=int,
        default=None,
        help="Telegram user id whose book to report. Defaults to the owner "
        "(TELEGRAM_OWNER_USER_ID) — statistics are per user from M8.1.",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    user_id = args.user if args.user is not None else settings.secrets.owner_user_id
    if user_id is None:
        raise SystemExit(
            "No user to report on. Set TELEGRAM_OWNER_USER_ID in .env, or pass --user <id>. "
            "Statistics are per user from M8.1 and there is no combined book."
        )
    raise SystemExit(asyncio.run(run(settings, parse_window(args.window), user_id)))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()

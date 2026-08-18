"""M7 demo: what the pipeline has spent, and whether the guard has tripped.

    python -m sentinel.tools.spend

The same accumulators the orchestrator gates on, so the number here is the number
that suspends deep analysis. It is an **estimate** derived from token counts and
``config.llm.pricing``, not a bill — and if any call used a model with no price
configured, this says the figure is a floor rather than quietly treating those
calls as free.
"""

from __future__ import annotations

import asyncio

from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.llm.spend import SpendState, SpendTotals, evaluate_spend, spend_window
from sentinel.risk.rounding import money
from sentinel.storage.db import Database
from sentinel.storage.repositories import LLMCallRepository


def render(totals: SpendTotals, settings: Settings) -> str:
    llm = settings.config.llm
    state = evaluate_spend(totals, llm)
    floor = "at least " if totals.is_floor else ""
    lines = [
        "── llm spend (estimate, not a bill) ──",
        f"today         {floor}${money(totals.day_usd)} of ${llm.daily_spend_limit_usd}",
        f"month to date {floor}${money(totals.month_usd)}",
        f"calls today   {totals.calls} ({totals.priced_calls} priced)",
        f"state         {state.value}",
    ]
    if totals.is_floor:
        lines.append(
            f"⚠️  {totals.unpriced_calls} call(s) used a model absent from config.llm.pricing. "
            "Their cost is missing from the figures above — not zero."
        )
    if state is SpendState.LIMIT_REACHED:
        lines.append(
            "⛔ new deep analysis is suspended until 00:00 UTC. "
            "The screener and the tracker keep running."
        )
    elif state is SpendState.WARN:
        lines.append(f"⚠️  past the ${llm.daily_spend_warn_usd} warn level.")
    return "\n".join(lines)


async def run(settings: Settings) -> int:
    from sentinel.core.clock import SystemClock

    day_start, month_start = spend_window(SystemClock().now())
    database = Database(settings.secrets.database_url)
    try:
        async with database.session() as session:
            totals = await LLMCallRepository(session).spend_totals(
                day_start=day_start,
                month_start=month_start,
                priced_models=tuple(settings.config.llm.pricing),
            )
    finally:
        await database.dispose()

    print(render(totals, settings))
    return 0


def main() -> None:
    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)
    raise SystemExit(asyncio.run(run(settings)))


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    main()

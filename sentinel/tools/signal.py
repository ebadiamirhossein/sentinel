"""M6 demo: a fixture through the risk gate, rendered as a Telegram signal card.

    python -m sentinel.tools.signal --fixture examples/sol_long.json
    python -m sentinel.tools.signal --fixture examples/sol_long_thin.json   # rejected
    python -m sentinel.tools.signal --fixture examples/sol_long.json --doc   # spec block
    python -m sentinel.tools.signal --fixture examples/sol_long.json --post  # to Telegram

Without ``--post`` this touches nothing: no network, no database, no LLM. The
clock is frozen so the same fixture always renders the same card — which is what
makes ``--doc`` output safe to paste into specs/TELEGRAM_UX.md §1 and re-verify
later.

``--post`` sends a real card with working buttons and writes the signal, so it
needs Postgres and a bot token. Run it twice: the second run posts nothing. That
is specs/TELEGRAM_UX.md §6 demonstrated rather than asserted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from sentinel.bot.app import build_bot
from sentinel.bot.cards import rejection_card, signal_card
from sentinel.bot.formatting import zone_info
from sentinel.bot.models import SignalRecord
from sentinel.bot.publisher import SignalPublisher
from sentinel.core.clock import FrozenClock
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.risk.engine import RiskEngine
from sentinel.risk.models import GateDecision, GateStatus
from sentinel.storage.db import Database
from sentinel.tools.size import build_inputs, load_fixture

#: The instant the documented example is frozen at. Any fixed, past instant would
#: do; this one matches the M4/M5.1 worked examples so the reports line up.
DOC_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

TAGS = re.compile(r"</?(b|i|code|pre|a)\b[^>]*>")


def evaluate(fixture: Path, settings: Settings, at: datetime) -> GateDecision:
    report, market, account, portfolio = build_inputs(load_fixture(fixture), capital_override=None)
    decision = RiskEngine(settings.config, clock=FrozenClock(at)).evaluate(
        report=report, market=market, account=account, portfolio=portfolio
    )
    if decision.plan is None:
        return decision
    return decision.model_copy(
        update={"plan": decision.plan.model_copy(update={"plan_id": demo_plan_id(fixture)})}
    )


def demo_plan_id(fixture: Path) -> UUID:
    """A stable ``plan_id`` for one fixture, so ``--post`` twice is a real test.

    ``TradePlan.plan_id`` is a fresh uuid4 per evaluation, which is right in
    production — every cycle's plan is genuinely a new plan. But it makes the
    demo's second run post a *second* card rather than colliding, so running
    ``--post`` twice would look like idempotency working while proving nothing.
    Deriving the id from the fixture's bytes makes the second run hit the real
    ``plan_id`` unique constraint, which is the guarantee specs/TELEGRAM_UX.md §6
    actually makes. Demo-only: nothing in the pipeline calls this.
    """
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    return uuid5(NAMESPACE_URL, f"sentinel-demo-signal/{digest}")


def as_plain_text(card: str) -> str:
    """Undo the HTML layer, for a terminal or a markdown doc.

    The document has to show what the owner *sees* in Telegram: ``<b>`` markers
    would be noise in the spec, and ``Fear &amp; Greed`` is an escape artefact,
    not what appears on the phone. Tags are stripped first so an entity inside one
    cannot survive.
    """
    return html.unescape(TAGS.sub("", card))


def render(decision: GateDecision, settings: Settings, at: datetime, number: int = 1) -> str:
    tz = zone_info(settings.config.telegram.owner_timezone)
    if decision.plan is None:
        return as_plain_text(rejection_card(decision, tz, at))
    record = SignalRecord(plan=decision.plan, number=number)
    return as_plain_text(signal_card(record, tz))


def doc_block(decision: GateDecision, settings: Settings) -> str:
    """The §1 example card, buttons included, ready to paste into the spec."""
    body = render(decision, settings, DOC_NOW)
    return "\n".join([body, "", "[✅ Taken]  [👀 Watching]  [❌ Skip]"])


async def post(decision: GateDecision, settings: Settings) -> int:
    if decision.plan is None:
        print("nothing to post — the gate did not approve this fixture")
        return 1
    chat_ids = settings.secrets.allowed_user_ids
    if not chat_ids:
        print("TELEGRAM_ALLOWED_USER_IDS is empty — nobody to post to. See .env.example.")
        return 2

    database = Database(settings.secrets.database_url)
    bot = build_bot(settings)
    try:
        publisher = SignalPublisher(
            database,
            bot,
            chat_ids=chat_ids,
            telegram=settings.config.telegram,
            tz=zone_info(settings.config.telegram.owner_timezone),
        )
        result = await publisher.publish(decision.plan)
    finally:
        await bot.session.close()
        await database.dispose()

    if result.record is None:
        # The plan_id collided: this plan already has a signal row, so nothing was
        # sent. This is the §6 guarantee, and the only case that may claim to be.
        print(f"nothing sent — {result.reason} (specs/TELEGRAM_UX.md §6 working)")
        return 0
    if not result.published:
        # The row was claimed but Telegram refused the send. Emphatically *not*
        # idempotency working — the card is missing and the owner needs to know.
        print(
            f"signal #{result.record.number} was recorded but NOT delivered — "
            "see the bot.send_failed log line above and telegram_messages.error"
        )
        return 1
    print(f"posted signal #{result.record.number} to {len(chat_ids)} chat(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a signal card from a fixture")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--doc", action="store_true", help="emit the specs/TELEGRAM_UX.md §1 block")
    parser.add_argument("--post", action="store_true", help="send it to Telegram for real")
    parser.add_argument("--number", type=int, default=1, help="signal number to render")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings.secrets.log_level, json_logs=settings.secrets.json_logs)

    at = DOC_NOW
    decision = evaluate(args.fixture, settings, at)

    if args.doc:
        print(doc_block(decision, settings))
        return 0

    print(render(decision, settings, at, args.number))

    if args.post:
        return asyncio.run(post(decision, settings))
    return 0 if decision.status is GateStatus.APPROVED_FOR_HUMAN else 1


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    raise SystemExit(main())


__all__ = ["as_plain_text", "demo_plan_id", "doc_block", "evaluate", "main", "render"]

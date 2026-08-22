"""The 🇮🇷 فارسی button (M11p).

Press a card, get a short Persian explanation of **that card**, as a reply sitting
underneath it. The original card is never edited and the button never disappears.

Four properties this handler is responsible for, in the order they bite:

1. **The model is shown a card and nothing else.** The card is *re-rendered* here from
   the id in the callback, through the same renderer that produced the message, in its
   ``shared_only`` form. Re-rendering rather than reading ``message.text`` is what lets
   a ``/pulse SYMBOL`` card spread over two messages be summarised as one card, and it
   is what keeps the per-user sizing out of a text two users may share.
2. **Nothing is sent that failed the numbers check.** ``SummaryOutcome`` carries the
   reason; every non-OK outcome sends Persian words, never silence and never an English
   traceback.
3. **A second press costs nothing.** The stored row is keyed on a hash of the exact
   input text, so the cache hit is exact by construction rather than by assumption.
4. **It cannot eat the analysis budget.** Two caps, checked before the call: a
   deployment-wide daily USD ceiling for this feature, and a per-user daily count of
   *generations*. See :class:`~sentinel.core.config.PersianSummaryConfig` for why
   crypto's reserved floor does not already cover this.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sentinel.analyst.persian.models import PersianSourceKind, PersianSummary
from sentinel.analyst.persian.summariser import SummaryOutcome
from sentinel.bot.auth import Actor
from sentinel.bot.cards import signal_card, symbol_pulse_card
from sentinel.bot.context import BotContext
from sentinel.bot.forex_cards import forex_signal_card
from sentinel.bot.keyboards import PersianCallback
from sentinel.bot.models import SignalRecord
from sentinel.bot.persian_cards import (
    CARD_NOT_FOUND,
    COULD_NOT_PRODUCE,
    DAILY_CAP_REACHED,
    NOT_YOUR_CARD,
    persian_message,
)
from sentinel.bot.plans import market_of, plan_of
from sentinel.bot.pulse import symbol_pulse_view
from sentinel.bot.runtime import effective_config
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.llm.spend import spend_window

log = get_logger(__name__)

persian_router = Router(name="persian")

#: Presses already being served, keyed by the hash of the card text.
#:
#: This bot has no other in-memory guard, on purpose -- every guard here is
#: database-backed. This one is not a rate limiter and is not a substitute for the
#: unique index: it is an **in-flight coalescer**. Two taps in the same second both
#: miss the cache, and without this both would buy a call and one would lose the
#: ``ON CONFLICT``. The loser's money is already spent by then, so the race has to be
#: closed before the call rather than after it. It holds only for the length of one
#: generation and is process-local, which is correct for a single-process bot and
#: honest about what it covers.
_IN_FLIGHT: dict[str, asyncio.Future[str]] = {}

#: When each user's last press finished, for the double-tap window. Bounded by the
#: number of approved users, so it needs no eviction.
_LAST_PRESS: dict[int, float] = {}


def card_hash(text: str) -> str:
    """The cache key: SHA-256 of the exact text the model will be shown."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@persian_router.callback_query(PersianCallback.filter())
async def persian_summary(
    query: CallbackQuery, callback_data: PersianCallback, ctx: BotContext, actor: Actor
) -> None:
    """Send a Persian summary of the pressed card, as a reply to it."""
    try:
        await _handle(query, callback_data, ctx, actor)
    except Exception:
        # ``answers_on_failure`` covers message handlers only, and a callback that
        # raises leaves the reader watching a spinner with no idea why. Every other
        # exit from this handler says something in Persian; so does this one.
        log.exception(
            "persian.failed",
            source_kind=callback_data.source_kind.value,
            source_id=str(callback_data.source_id),
            user_id=actor.user_id,
        )
        await _say(query, COULD_NOT_PRODUCE)


async def _handle(
    query: CallbackQuery, callback_data: PersianCallback, ctx: BotContext, actor: Actor
) -> None:
    settings = ctx.settings.config.persian_summary
    if not settings.enabled or ctx.summariser is None:
        log.warning("persian.unavailable_here", detail="no summariser is configured")
        await query.answer()
        await _say(query, COULD_NOT_PRODUCE)
        return

    now = ctx.clock.now()
    if _double_tapped(actor.user_id, now, window=settings.double_tap_seconds):
        # Answering the query and doing nothing else: the reply from the first press
        # is already on its way, and a second identical message under one card reads
        # as a bug rather than as a fast thumb.
        await query.answer()
        return

    rendered = await _render_card(callback_data, ctx, actor)
    if rendered is None:
        await query.answer()
        await _say(query, CARD_NOT_FOUND)
        return
    if rendered is _NOT_YOURS:
        log.warning(
            "persian.rejected",
            source_id=str(callback_data.source_id),
            user_id=actor.user_id,
            detail="the signal belongs to another user",
        )
        await query.answer(NOT_YOUR_CARD, show_alert=True)
        return

    card, market, symbol, candidate_status = rendered
    digest = card_hash(card)

    stored = await _cached(ctx, digest)
    if stored is not None:
        log.info("persian.cache_hit", input_sha256=digest, symbol=symbol, user_id=actor.user_id)
        await query.answer()
        await _say(query, stored.summary_text)
        return

    await query.answer()
    inflight = _IN_FLIGHT.get(digest)
    if inflight is not None:
        # Someone else's press is already paying for this exact card. Wait for their
        # text rather than buying a second copy of it.
        log.info("persian.coalesced", input_sha256=digest, user_id=actor.user_id)
        await _say(query, await asyncio.shield(inflight))
        return

    capped = await _cap_reached(ctx, actor.user_id, now)
    if capped is not None:
        log.warning("persian.capped", user_id=actor.user_id, detail=capped)
        await _say(query, DAILY_CAP_REACHED)
        return

    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    _IN_FLIGHT[digest] = future
    try:
        text = await _generate(
            ctx, actor, callback_data, card, digest, market, symbol, candidate_status, now
        )
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        _IN_FLIGHT.pop(digest, None)
        _LAST_PRESS[actor.user_id] = _monotonic()

    if not future.done():
        future.set_result(text)
    await _say(query, text)


async def _generate(
    ctx: BotContext,
    actor: Actor,
    callback_data: PersianCallback,
    card: str,
    digest: str,
    market: Market,
    symbol: str,
    candidate_status: str,
    now: datetime,
) -> str:
    """One paid call, checked, recorded and stored. Returns the text to send.

    ``candidate_status`` is read from the stored report, never from the card text and
    never from the model's answer: it is what the verdict rail is checked *against*.
    """
    assert ctx.summariser is not None
    result = await ctx.summariser.summarise(card, symbol=symbol, candidate_status=candidate_status)

    # The audit row is written on EVERY outcome, including the two failures: the money
    # was spent either way, and a failure that left no trace would be invisible to the
    # spend line and to whoever reviews this feature.
    async with ctx.database.session() as session:
        await ctx.repositories.llm_calls(session, market=market).record(result.call)
        await session.commit()

    if result.outcome is not SummaryOutcome.OK:
        log.warning(
            "persian.not_sent",
            outcome=result.outcome.value,
            symbol=symbol,
            user_id=actor.user_id,
            numbers=None if result.check is None else result.check.detail,
            verdict=None if result.verdict is None else result.verdict.detail,
        )
        return COULD_NOT_PRODUCE

    summary = PersianSummary(
        input_sha256=digest,
        source_kind=callback_data.source_kind,
        signal_id=(
            callback_data.source_id
            if callback_data.source_kind is PersianSourceKind.SIGNAL
            else None
        ),
        analyst_report_id=(
            callback_data.source_id
            if callback_data.source_kind is PersianSourceKind.PULSE_VERDICT
            else None
        ),
        market=market,
        symbol=symbol,
        summary_text=result.text,
        input_text=card,
        prompt_version=result.call.prompt_version,
        model=result.call.model,
        tokens_in=result.call.usage.input_tokens,
        tokens_out=result.call.usage.output_tokens,
        cost_usd_estimate=result.call.cost_usd_estimate,
        llm_call_id=result.call.call_id,
        created_at=now,
        created_by_user_id=actor.user_id,
    )
    async with ctx.database.session() as session:
        winner = await ctx.repositories.persian_summaries(session).store(summary)
        await session.commit()

    log.info(
        "persian.generated",
        input_sha256=digest,
        symbol=symbol,
        user_id=actor.user_id,
        chars=len(winner.summary_text),
        tokens_in=result.call.usage.input_tokens,
        tokens_out=result.call.usage.output_tokens,
        cost_usd_estimate=str(result.call.cost_usd_estimate),
        duration_ms=result.call.duration_ms,
    )
    return winner.summary_text


# --------------------------------------------------------------------------- #
# Rendering the input
# --------------------------------------------------------------------------- #

#: Sentinel for "this card is somebody else's". Distinct from ``None`` (no such card)
#: because the two say different things to the presser.
_NOT_YOURS: Any = object()


async def _render_card(
    callback_data: PersianCallback, ctx: BotContext, actor: Actor
) -> tuple[str, Market, str, str] | Any | None:
    """``(card text, market, symbol, candidate_status)``, ``None``, or ``_NOT_YOURS``."""
    if callback_data.source_kind is PersianSourceKind.SIGNAL:
        return await _render_signal(callback_data, ctx, actor)
    return await _render_pulse(callback_data, ctx, actor)


async def _render_signal(
    callback_data: PersianCallback, ctx: BotContext, actor: Actor
) -> tuple[str, Market, str, str] | Any | None:
    async with ctx.database.session() as session:
        row = await ctx.repositories.signals(session).get(callback_data.source_id)
        if row is None:
            return None
        if row.user_id != actor.user_id:
            return _NOT_YOURS
        stored = await ctx.repositories.settings(session).all()

    config = effective_config(ctx.settings, stored)
    market = market_of(row)
    # Rehydration dispatches on ``market``, never on trying one plan shape and falling
    # back to the other (FOREX.md §16.7) -- ``plan_of`` is the one place that decides.
    plan = plan_of(row.plan, market)
    # Only the fields the renderer reads. ``cycle_id`` and the tracker's columns are
    # not on the card, and a reconstruction that carried them would invite the next
    # reader to believe this is the delivered record rather than a view of it.
    record = SignalRecord(
        signal_id=callback_data.source_id,
        market=market,
        number=row.number,
        user_id=row.user_id,
        plan=plan,
    )
    render = signal_card if market is Market.CRYPTO else forex_signal_card
    card = render(record, ctx.tz, show_market=config.multi_market, shared_only=True)
    # Read from the stored report rather than assumed. A signal card exists only for a
    # gate-approved plan -- ``_check_preconditions`` returns ``NOT_A_CANDIDATE`` before a
    # plan is ever built -- so this is CANDIDATE today, and hard-coding it would be a
    # true statement that stops being true the day the gate learns a second way to
    # approve something.
    return card, market, plan.symbol, plan.report.candidate_status.value


async def _render_pulse(
    callback_data: PersianCallback, ctx: BotContext, actor: Actor
) -> tuple[str, Market, str, str] | Any | None:
    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
        # ``get`` is by primary key and carries no market filter, so the market is read
        # off the row rather than guessed before the read. See its docstring.
        row = await ctx.repositories.reports(session, market=LEGACY_MARKET).get(
            callback_data.source_id
        )
        if row is None:
            return None
        market = market_of(row)
        decisions = (
            []
            if row.cycle_id is None
            else await ctx.repositories.gate_decisions(session, market=market).for_cycles(
                [row.cycle_id]
            )
        )

    config = effective_config(ctx.settings, stored)
    watchlist = config.market(market).watchlist
    view = symbol_pulse_view(
        row, decisions, symbol=row.symbol, on_watchlist=row.symbol in watchlist
    )
    # ``/pulse SYMBOL`` is byte-identical for the owner and for a member -- it carries
    # no cost figure and nothing else that varies by role -- so no ``shared_only``
    # equivalent is needed here. The card is several messages on Telegram and one card
    # to a reader, so the pages are rejoined before the model sees them.
    # The surface the verdict rail exists for: this card renders WATCHLIST and NO_SETUP
    # as well as CANDIDATE, and it carries almost no numbers for the other rail to
    # constrain.
    return (
        "\n".join(symbol_pulse_card(view, ctx.tz)),
        market,
        row.symbol,
        row.candidate_status,
    )


# --------------------------------------------------------------------------- #
# Rails
# --------------------------------------------------------------------------- #


async def _cached(ctx: BotContext, digest: str) -> PersianSummary | None:
    async with ctx.database.session() as session:
        return await ctx.repositories.persian_summaries(session).find(digest)


async def _cap_reached(ctx: BotContext, user_id: int, now: datetime) -> str | None:
    """``None`` when a new generation is allowed, else why it is not."""
    settings = ctx.settings.config.persian_summary
    day_start, _ = spend_window(now)
    async with ctx.database.session() as session:
        repo = ctx.repositories.persian_summaries(session)
        spent = await repo.spend_today_usd(day_start=day_start)
        if spent >= settings.daily_usd_cap:
            return f"persian spend ${spent} today of ${settings.daily_usd_cap}"
        mine = await repo.generations_today(user_id=user_id, day_start=day_start)
        if mine >= settings.daily_generations_per_user:
            return f"{mine} generations today of {settings.daily_generations_per_user}"
    return None


def _double_tapped(user_id: int, now: datetime, *, window: float) -> bool:
    last = _LAST_PRESS.get(user_id)
    return last is not None and (_monotonic() - last) < window


def _monotonic() -> float:
    """Wall-clock-independent, so a clock change cannot open or close the window."""
    return asyncio.get_running_loop().time()


async def _say(query: CallbackQuery, body: str) -> None:
    """Reply to the pressed card, so the two sit together in the chat.

    A reply rather than an edit: the English card is the reference, and rewriting it in
    place would remove the thing the Persian text points back at.
    """
    try:
        await query.message.reply(persian_message(body))  # type: ignore[union-attr]
    except (TelegramBadRequest, AttributeError):
        log.warning("persian.reply_failed", detail="the card could not be replied to")


__all__ = ["card_hash", "persian_router", "persian_summary"]

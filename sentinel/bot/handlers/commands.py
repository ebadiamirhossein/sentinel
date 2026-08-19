"""Member commands — specs/TELEGRAM_UX.md §3.

``/capital /risk /positions /stats``. Everything here is **scoped to the caller**:
their own capital, their own risk %, their own signals, their own statistics. Nothing
in this module can read another user's book, because every query it makes takes a
required ``user_id``.

``/start /help /leave`` live in ``membership.py`` (they must work before full
standing does), and ``/status /settings /watchlist /pause /resume /users /approve
/reject /suspend`` live in ``admin.py`` behind the owner filter. ``/pulse`` and
``/analyze`` are P1 and stay unregistered, because an unregistered command is silent
rather than answered with a promise.

Every handler writes through a repository and commits its own unit of work, then
confirms back in words — §3 requires ``/capital`` and ``/risk`` to "confirm", and a
setting that changes sizing should never change quietly.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sentinel.bot.auth import Actor
from sentinel.bot.cards import (
    positions_card,
    stats_card,
    watchlist_request_ack_card,
    watchlist_request_card,
)
from sentinel.bot.context import BotContext
from sentinel.bot.formatting import escape
from sentinel.bot.keyboards import watchlist_request_keyboard
from sentinel.bot.models import SignalDecision
from sentinel.bot.outbound import SupportsBot
from sentinel.bot.readmodels import position_view, stats_view
from sentinel.bot.runtime import (
    Invalid,
    effective_config,
    parse_capital,
    parse_risk_pct,
    parse_symbol,
    risk_pct_of,
    verify_symbol,
)
from sentinel.core.logging import get_logger
from sentinel.risk.models import TradePlan
from sentinel.stats.queries import build_report, parse_window

log = get_logger(__name__)

commands_router = Router(name="commands")


@commands_router.message(Command("capital"))
async def capital(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/capital 10000`` — validated > 0, confirmed, applies to new signals only.

    Written to the caller's own ``users`` row from M8.1. It was one global setting
    through M8, which is exactly the value that must not be shared.
    """
    account = actor.known()
    if not command.args:
        await message.answer(
            f"Your capital: €{account.capital_eur}"
            if account.capital_eur is not None
            else "Your capital is not set, so no signal can be sized for you and none "
            "will be sent.\nSet it with: /capital 10000"
        )
        return

    parsed = parse_capital(command.args)
    if isinstance(parsed, Invalid):
        await message.answer(f"❌ {escape(parsed.message)}")
        return

    async with ctx.database.session() as session:
        await ctx.repositories.users(session).set_capital(actor.user_id, parsed, at=ctx.clock.now())
        await session.commit()

    log.info("bot.capital_set", capital_eur=str(parsed), user_id=actor.user_id)
    await message.answer(
        f"✅ Your capital is set to <b>€{parsed}</b>.\n"
        "Applies to new signals only — open ones keep the sizing they were issued "
        "with (specs/RISK_ENGINE.md §7)."
    )


@commands_router.message(Command("risk"))
async def risk(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/risk 0.75`` — bounds come from config, never from a literal here."""
    account = actor.known()
    risk_config = ctx.settings.config.risk
    if not command.args:
        chosen = "" if account.risk_per_trade_pct is not None else " (the default)"
        await message.answer(
            f"Your risk per trade: <b>{risk_pct_of(account, ctx.settings.config)}%</b>"
            f"{chosen} (allowed {risk_config.risk_per_trade_min_pct} to "
            f"{risk_config.risk_per_trade_max_pct}%)"
        )
        return

    parsed = parse_risk_pct(command.args, risk_config)
    if isinstance(parsed, Invalid):
        await message.answer(f"❌ {escape(parsed.message)}")
        return

    async with ctx.database.session() as session:
        await ctx.repositories.users(session).set_risk_pct(
            actor.user_id, parsed, at=ctx.clock.now()
        )
        await session.commit()

    log.info("bot.risk_set", risk_pct=str(parsed), user_id=actor.user_id)
    await message.answer(
        f"✅ Your risk per trade is set to <b>{parsed}%</b>.\nApplies to new signals only."
    )


@commands_router.message(Command("positions"))
async def positions(message: Message, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/positions`` — the caller's Taken signals with live uPnL in R and EUR.

    The mark is the tracker's last observed price for the symbol, and the R figure
    comes from ``risk/accounting.py``. The snapshot feed is shared — it is market
    data — but the signals, the sizing and therefore every euro figure are the
    caller's alone.
    """
    async with ctx.database.session() as session:
        rows = await ctx.repositories.signals(session).with_decision(
            SignalDecision.TAKEN, user_id=actor.user_id
        )
        fills = await ctx.repositories.fills(session).for_signals([row.id for row in rows])
        exits = await ctx.repositories.exits(session).for_signals([row.id for row in rows])
        marks = {
            row.symbol: snapshot.last_price
            for snapshot in await ctx.repositories.snapshots(session).latest_per_symbol()
            for row in rows
            if row.symbol == snapshot.symbol
        }

    views = [
        position_view(
            row,
            TradePlan.model_validate(row.plan),
            fills.get(row.id, []),
            exits.get(row.id, []),
            mark_price=marks.get(row.symbol),
        )
        for row in rows
    ]
    await message.answer(positions_card(views, ctx.tz))


@commands_router.message(Command("stats"))
async def stats(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/stats [30d|90d|all]`` — **the caller's numbers, and nobody else's.**

    Three populations, reported separately: real (✅ Taken), hypothetical
    (👀 Watching + ❌ Skipped) and, when any exist, dry run. Merging them would be the
    one thing PRD G2 cannot afford — "the system's real success rate is *known*, not
    guessed".

    M8.1 adds a second thing it cannot afford. The REAL population is defined by
    ``decision == TAKEN``, and under one shared analysis several people answer the
    same setup differently: without the user filter, one person's Taken would land in
    another person's record. It is not a filter on the report — it is a filter on the
    query, and ``SignalRepository`` requires it.
    """
    window = parse_window(command.args)
    async with ctx.database.session() as session:
        report = await build_report(
            session, window=window, now=ctx.clock.now(), user_id=actor.user_id
        )
    await message.answer(stats_card(stats_view(report), ctx.tz))


__all__ = ["commands_router"]


@commands_router.message(Command("request"))
async def request(
    message: Message,
    command: CommandObject,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
) -> None:
    """§3 ``/request SOLUSDT`` — ask the owner to watch a symbol (M8.3).

    **Members ask; they do not add.** The watchlist decides what the shared deep
    analyst is pointed at, every symbol on it is screened every cycle, and each one
    can buy a ~$0.28 analyst call on the owner's key. A member editing it directly
    would be spending somebody else's budget, so ``/watchlist add|remove`` stays
    owner-only and this is the door.

    Validation is the *same* path ``/watchlist add`` uses — ``parse_symbol``, then
    the cached ``instrument_meta`` row, then one keyless public call for a symbol
    nobody has ingested yet. Checking here rather than at approval time means a typo
    is answered in a second by the person who made it, instead of arriving as a card
    the owner cannot evaluate.

    The refusals are four distinct messages on purpose. "Not a symbol", "already
    watched", "already asked for" and "the list is full" are four different things to
    do next, and a member should never have to guess which one happened.
    """
    if actor.account is not None and actor.account.is_owner:
        # The owner has /watchlist add. Offering both would be two ways to do one
        # thing, and the request path would make them approve their own card.
        await message.answer(
            "You own the watchlist — use <code>/watchlist add SOLUSDT</code> directly."
        )
        return

    if not command.args:
        await message.answer("❌ Usage: <code>/request SOLUSDT</code>")
        return

    parsed = parse_symbol(command.args.strip().split()[0])
    if isinstance(parsed, Invalid):
        await message.answer(f"❌ {escape(parsed.message)}")
        return

    now = ctx.clock.now()
    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
        config = effective_config(ctx.settings, stored)
        current: tuple[str, ...] = tuple(config.watchlist)

        if parsed in current:
            await message.answer(f"{escape(parsed)} is already on the watchlist.")
            return
        if len(current) >= config.watchlist_max_symbols:
            await message.answer(
                f"❌ The watchlist is full ({len(current)} of "
                f"{config.watchlist_max_symbols}). Ask the owner to make room first."
            )
            return

        known = await ctx.repositories.instruments(session).get(parsed)
        unknown = await verify_symbol(parsed, known, ctx.symbol_checker)
        if isinstance(unknown, Invalid):
            await message.answer(f"❌ {escape(unknown.message)}")
            return

        requests = ctx.repositories.watchlist_requests(session)
        created = await requests.request(parsed, user_id=actor.user_id, at=now)
        owner = await ctx.repositories.users(session).owner()
        await session.commit()

    if created is None:
        # Somebody already asked. Deliberately the same wording whoever they were:
        # who else uses this bot is not a member's business (M8.1 §6's boundary).
        await message.answer(f"{escape(parsed)} has already been requested and is waiting.")
        return

    log.info("bot.watchlist_requested", symbol=parsed, user_id=actor.user_id)
    await message.answer(watchlist_request_ack_card(parsed))

    if owner is None:  # pragma: no cover — a database with users always has an owner
        log.warning("bot.no_owner", symbol=parsed, detail="watchlist request stored, nobody to ask")
        return
    await bot.send_message(
        chat_id=owner.telegram_user_id,
        text=watchlist_request_card(
            created,
            actor.account,
            ctx.tz,
            size=len(current),
            cap=config.watchlist_max_symbols,
        ),
        parse_mode=ctx.settings.config.telegram.parse_mode,
        reply_markup=watchlist_request_keyboard(parsed),
        reply_to_message_id=None,
    )

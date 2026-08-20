"""Owner-only commands — specs/TELEGRAM_UX.md §3 and §7 (M8.1).

``/users /approve /reject /suspend`` are new here. ``/status /settings /watchlist
/pause /resume`` moved here from ``commands.py`` unchanged in behaviour, because the
split this milestone needs is exactly operator-versus-member: what shapes the whole
pipeline (the watchlist, the pause), what reports on it (status, settings, spend),
and who may be in it.

**There is no refusal path in this module**, and that is deliberate. Every handler
sits behind :class:`~sentinel.bot.auth.OwnerOnly`; a filter that does not match means
no handler runs, which is silence. A member typing ``/approve`` therefore learns
nothing — not that the command exists, not that they lack the role, not that anybody
has it.

``/users`` carries this milestone's other owner ruling, and it is worth reading the
docstring on :func:`~sentinel.bot.cards.users_card` for it: the card shows standing,
join date, whether a capital has been set at all, and whether a loss pause is
holding. It shows no amount, no P&L, no win rate and no decision. Operating a system
for friends does not require watching them trade.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from sentinel.bot.auth import Actor, OwnerOnly
from sentinel.bot.cards import (
    acknowledgement_card,
    settings_card,
    standing_card,
    status_card,
    users_card,
    watchlist_card,
    watchlist_full_card,
    watchlist_request_decided_card,
    welcome_card,
)
from sentinel.bot.context import BotContext
from sentinel.bot.formatting import escape
from sentinel.bot.keyboards import (
    AdminAction,
    AdminCallback,
    WatchlistCallback,
    acknowledge_keyboard,
    resume_keyboard,
)
from sentinel.bot.markets import parse_market, resolve_markets, section_header
from sentinel.bot.menu import clear_for, publish_for
from sentinel.bot.models import (
    SignalDecision,
    UserAccount,
    UserStatus,
    WatchlistRequest,
    WatchlistRequestStatus,
)
from sentinel.bot.outbound import SupportsBot
from sentinel.bot.readmodels import spend_view, user_view
from sentinel.bot.runtime import (
    Invalid,
    effective_config,
    parse_symbol,
    risk_pct_of,
    verify_symbol,
    watchlist_key,
    watchlist_source,
)
from sentinel.bot.views import DataSourceView, SettingsView, StatusView
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET
from sentinel.core.pauses import effective_pause
from sentinel.risk.models import PauseReason, PauseState, TradePlan
from sentinel.risk.rails import open_risk_pct

log = get_logger(__name__)

admin_router = Router(name="admin")
admin_router.message.filter(OwnerOnly())
admin_router.callback_query.filter(OwnerOnly())

#: What ``/approve`` and friends say when the id is not a number or not known.
USAGE = "Usage: /approve 123456789 · /reject 123456789 · /suspend 123456789\nSee /users for ids."


def _parse_user_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    candidate = raw.strip()
    return int(candidate) if candidate.lstrip("-").isdigit() else None


# --------------------------------------------------------------------------- #
# §7 — who has access
# --------------------------------------------------------------------------- #


@admin_router.message(Command("users"))
async def users(message: Message, ctx: BotContext) -> None:
    """§7 ``/users`` — standing, join date, set-up-or-not, loss-paused-or-not."""
    now = ctx.clock.now()
    async with ctx.database.session() as session:
        accounts = await ctx.repositories.users(session).all()
    await message.answer(users_card([user_view(a, now=now) for a in accounts], ctx.tz))


@admin_router.message(Command("approve"))
async def approve(
    message: Message, command: CommandObject, ctx: BotContext, actor: Actor, bot: SupportsBot
) -> None:
    """§7 ``/approve <id>`` — and the first-run note goes out immediately."""
    user_id = _parse_user_id(command.args)
    if user_id is None:
        await message.answer(USAGE)
        return
    updated = await _decide(ctx, user_id, UserStatus.APPROVED, by=actor.user_id)
    if updated is None:
        await message.answer(f"No user with id <code>{user_id}</code>. {escape(USAGE)}")
        return

    await publish_for(bot, user_id, owner=updated.is_owner)
    await _greet(bot, ctx, updated)
    log.info("bot.user_approved", user_id=user_id, by_user_id=actor.user_id)
    await message.answer(
        f"✅ Approved <code>{user_id}</code>. They have been sent the note they must "
        "accept before anything is delivered, and asked to set their capital."
    )


@admin_router.message(Command("reject"))
async def reject(
    message: Message, command: CommandObject, ctx: BotContext, actor: Actor, bot: SupportsBot
) -> None:
    """§7 ``/reject <id>``. The person is told plainly; they are not left guessing."""
    await _refuse(
        message,
        command,
        ctx,
        actor,
        bot,
        status=UserStatus.REJECTED,
        confirmation="🚫 Rejected <code>{user_id}</code>. They have been told, once.",
    )


@admin_router.message(Command("suspend"))
async def suspend(
    message: Message, command: CommandObject, ctx: BotContext, actor: Actor, bot: SupportsBot
) -> None:
    """§7 ``/suspend <id>`` — stop delivering without deleting anything."""
    await _refuse(
        message,
        command,
        ctx,
        actor,
        bot,
        status=UserStatus.SUSPENDED,
        confirmation=(
            "⏸️ Suspended <code>{user_id}</code>. No further signals or updates. "
            "Their history is untouched, and /approve puts them back."
        ),
    )


@admin_router.callback_query(AdminCallback.filter())
async def approval_button(
    query: CallbackQuery,
    callback_data: AdminCallback,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
) -> None:
    """The Approve/Reject buttons on a request card.

    The role was already re-checked against the database by :class:`OwnerOnly` on
    this router — a callback payload is client-supplied, and a request card forwarded
    to somebody else carries its buttons along with it.
    """
    approved = callback_data.action is AdminAction.APPROVE
    status = UserStatus.APPROVED if approved else UserStatus.REJECTED
    updated = await _decide(ctx, callback_data.user_id, status, by=actor.user_id)
    if updated is None:  # pragma: no cover — the row existed when the card was sent
        await query.answer("That user is no longer in the database.", show_alert=True)
        return

    if approved:
        await publish_for(bot, updated.telegram_user_id, owner=updated.is_owner)
        await _greet(bot, ctx, updated)
    else:
        await clear_for(bot, updated.telegram_user_id)
        await _tell(bot, ctx, updated)

    log.info(
        "bot.user_decided",
        user_id=updated.telegram_user_id,
        status=status.value,
        by_user_id=actor.user_id,
    )
    await query.answer("Approved." if approved else "Rejected.")
    if query.message is None:  # pragma: no cover — a card too old to still be there
        return
    # The buttons come off the request card once it has been answered: a second tap
    # on a stale card would re-decide a standing the owner may since have changed by
    # hand.
    try:
        await query.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except (TelegramBadRequest, AttributeError):  # pragma: no cover — cosmetic only
        log.info("bot.request_markup_edit_skipped", user_id=updated.telegram_user_id)
    await query.message.answer(
        f"{'✅ Approved' if approved else '🚫 Rejected'} <code>{updated.telegram_user_id}</code>."
    )


async def _refuse(
    message: Message,
    command: CommandObject,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
    *,
    status: UserStatus,
    confirmation: str,
) -> None:
    """``/reject`` and ``/suspend`` — the same three steps with a different word."""
    user_id = _parse_user_id(command.args)
    if user_id is None:
        await message.answer(USAGE)
        return
    if user_id == actor.user_id:
        await message.answer(
            "That is you. Suspending the owner would leave the system with no one "
            "able to operate it."
        )
        return
    updated = await _decide(ctx, user_id, status, by=actor.user_id)
    if updated is None:
        await message.answer(f"No user with id <code>{user_id}</code>. {escape(USAGE)}")
        return
    await clear_for(bot, user_id)
    await _tell(bot, ctx, updated)
    log.info("bot.user_decided", user_id=user_id, status=status.value, by_user_id=actor.user_id)
    await message.answer(confirmation.format(user_id=user_id))


async def _decide(
    ctx: BotContext, user_id: int, status: UserStatus, *, by: int
) -> UserAccount | None:
    async with ctx.database.session() as session:
        updated = await ctx.repositories.users(session).set_status(
            user_id, status, at=ctx.clock.now(), by_user_id=by
        )
        await session.commit()
    return updated


async def _greet(bot: SupportsBot, ctx: BotContext, account: UserAccount) -> None:
    """Welcome, then the note that gates everything until it is acknowledged."""
    parse_mode = ctx.settings.config.telegram.parse_mode
    await bot.send_message(
        chat_id=account.telegram_user_id,
        text=welcome_card(account),
        parse_mode=parse_mode,
        reply_markup=None,  # type: ignore[arg-type]
        reply_to_message_id=None,
    )
    await bot.send_message(
        chat_id=account.telegram_user_id,
        text=acknowledgement_card(),
        parse_mode=parse_mode,
        reply_markup=acknowledge_keyboard(),
        reply_to_message_id=None,
    )


async def _tell(bot: SupportsBot, ctx: BotContext, account: UserAccount) -> None:
    """Tell somebody their standing changed. Rejection in silence is worse."""
    try:
        await bot.send_message(
            chat_id=account.telegram_user_id,
            text=standing_card(account),
            parse_mode=ctx.settings.config.telegram.parse_mode,
            reply_markup=None,  # type: ignore[arg-type]
            reply_to_message_id=None,
        )
    except Exception:
        # A user who blocked the bot cannot be told, and that must not fail the
        # owner's command: the standing change is already committed.
        log.warning("bot.notice_undeliverable", user_id=account.telegram_user_id, exc_info=True)


# --------------------------------------------------------------------------- #
# §3 — the operator's view of the pipeline
# --------------------------------------------------------------------------- #


@admin_router.message(Command("status"))
async def status(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/status`` — pipeline health, plus **the caller's own** rails.

    The signal counts and the risk figures are the owner's book, not a sum over
    everybody's: they are the numbers the owner's next signal is gated against, and
    a total across users would be a number no rail ever compares anything to.

    **One market at a time** (M10a). Every figure on this card — the watchlist size,
    the cycle counts, the open risk, the spend — is a per-market number, and a card
    that summed two markets would produce totals no rail compares anything to, which
    is the same objection as summing users. ``/status forex`` names one; with no
    argument it is the first enabled market, which with forex disabled is the only
    one there is and the card is unchanged.
    """
    account = actor.known()
    now = ctx.clock.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    config_for_market = effective_config(ctx.settings, {})
    resolved = resolve_markets(command.args, config_for_market)
    if isinstance(resolved, Invalid):
        await message.answer(f"❌ {resolved.message}")
        return
    market = resolved[0] if resolved else LEGACY_MARKET

    async with ctx.database.session() as session:
        global_pause = await ctx.repositories.risk_state(session).load()
        market_pause = await ctx.repositories.market_pause(session, market=market).load()
        user_market_pause = await ctx.repositories.user_market_pause(session, market=market).load(
            actor.user_id
        )
        stored = await ctx.repositories.settings(session).all()
        signals_repo = ctx.repositories.signals(session, market=market)
        recent = await signals_repo.recent(user_id=actor.user_id, limit=200)
        undecided = await signals_repo.undecided_count(user_id=actor.user_id)
        taken = await signals_repo.with_decision(
            SignalDecision.TAKEN, user_id=actor.user_id, limit=200
        )
        snapshots = await ctx.repositories.snapshots(session, market=market).latest_per_symbol()
        stuck = await ctx.repositories.messages(session).stuck()
        open_taken = await signals_repo.open_taken(user_id=actor.user_id)
        open_symbols = await signals_repo.open_symbols(user_id=actor.user_id)
        signals_today = await signals_repo.published_since(day_start, user_id=actor.user_id)
        cycles_repo = ctx.repositories.cycles(session, market=market)
        last_cycle = await cycles_repo.latest()
        completed, started = await cycles_repo.completion_since(now - timedelta(days=30))
        totals = await ctx.repositories.llm_calls(session, market=market).spend_totals(
            day_start=day_start,
            month_start=day_start.replace(day=1),
            priced_models=tuple(ctx.settings.config.llm.pricing),
        )

    config = effective_config(ctx.settings, stored)
    pause = effective_pause(
        global_pause=global_pause,
        market_pause=market_pause,
        user_pause=account.pause,
        user_market_pause=user_market_pause,
        market=market,
        now=now,
    )
    view = StatusView(
        paused=pause.active,
        pause_reason=None if pause.state.reason is None else pause.state.reason.value,
        paused_until=pause.state.until,
        pause_scope=pause.label(multi_market=config.multi_market),
        market=section_header(market, config),
        capital_eur=account.capital_eur,
        risk_per_trade_pct=risk_pct_of(account, config),
        watchlist_size=len(config.market(market).watchlist),
        signals_total=len(recent),
        signals_undecided=undecided,
        signals_taken=len(taken),
        stuck_messages=len(stuck),
        data_sources=tuple(
            DataSourceView(
                symbol=row.symbol,
                quality=row.data_quality,
                captured_at=row.captured_at,
                degraded_fields=tuple(row.degraded_fields),
            )
            for row in snapshots
        ),
        dry_run=config.market(market).dry_run,
        last_cycle_at=None
        if last_cycle is None
        else (last_cycle.finished_at or last_cycle.started_at),
        last_cycle_status=None if last_cycle is None else last_cycle.status,
        cycles_completed=completed,
        cycles_started=started,
        open_risk_pct=open_risk_pct(
            [TradePlan.model_validate(row.plan).risk_per_trade_pct for row in open_taken]
        ),
        max_open_risk_pct=config.risk.max_open_risk_pct,
        open_positions=len(open_taken),
        max_positions=config.risk.max_positions,
        signals_today=signals_today,
        max_signals_per_day=config.risk.max_signals_per_day,
        signals_open=len(open_symbols),
        spend=spend_view(totals, config.llm, market=config.market(market)),
    )
    await message.answer(status_card(view, ctx.tz))


@admin_router.message(Command("pause"))
async def pause(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/pause`` — manual, no expiry, survives a restart (RISK_ENGINE §7).

    Owner-only. Members cannot pause themselves (they have no ``/pause``), so a
    per-user pause here would leave the operator with no stop button for the system
    they run.

    **``/pause`` with no argument is still system-wide** (M10a), and that is not
    inertia: it is the command somebody reaches for when something is wrong, often
    on a phone, often in a hurry, and it must not quietly have become narrower than
    it was. ``/pause forex`` narrows deliberately, by typing a word.
    """
    named = parse_market(command.args) if command.args else None
    if command.args and named is None:
        await message.answer(
            "❌ Usage: /pause · /pause crypto · /pause forex (no argument pauses every market)"
        )
        return

    async with ctx.database.session() as session:
        state = PauseState(paused=True, reason=PauseReason.MANUAL, until=None)
        if named is None:
            await ctx.repositories.risk_state(session).save(state)
        else:
            await ctx.repositories.market_pause(session, market=named).save(state)
        await session.commit()
    log.info(
        "bot.paused",
        reason=PauseReason.MANUAL.value,
        scope="global" if named is None else named.value,
        user_id=actor.user_id,
    )
    if named is None:
        await message.answer(
            "⏸️ <b>Paused.</b> No new signals will be gated through for anyone until "
            "/resume.\nThe tracker keeps following everything already open."
        )
        return
    await message.answer(
        f"⏸️ <b>Paused {named.value}.</b> No new {named.value} signals will be gated "
        f"through for anyone until /resume {named.value}. Other markets are "
        "unaffected.\nThe tracker keeps following everything already open."
    )


@admin_router.message(Command("resume"))
async def resume(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/resume`` — a loss-limit pause needs an explicit confirmation button.

    The asymmetry is the point, and M8.1 keeps it across the split: a manual pause
    was a deliberate act and lifting it is another one, but a daily-loss pause exists
    precisely because the day has gone badly, and that is when a reflexive tap does
    the most damage. The loss pause lives on the caller's own ``users`` row; the
    manual one is the ``risk_state`` row, plus M10a's per-market rows.

    ``/resume`` with no argument lifts the **global** pause, mirroring ``/pause``.
    ``/resume forex`` lifts that market's. A loss-limit pause still intercepts both,
    because it is the caller's own book that is being overridden either way.
    """
    account = actor.known()
    now = ctx.clock.now()
    named = parse_market(command.args) if command.args else None
    if command.args and named is None:
        await message.answer("❌ Usage: /resume · /resume crypto · /resume forex")
        return

    async with ctx.database.session() as session:
        global_pause = await ctx.repositories.risk_state(session).load()
        market_pause = (
            PauseState()
            if named is None
            else await ctx.repositories.market_pause(session, market=named).load()
        )

        if account.pause.is_active(now):
            await message.answer(
                "⚠️ This is a <b>daily loss-limit</b> pause on your own book, not a "
                "manual one.\nResuming now overrides a rail that exists to stop a "
                "cascade day. Confirm if that is what you want.",
                reply_markup=resume_keyboard(),
            )
            return
        target = global_pause if named is None else market_pause
        if not target.is_active(now):
            await message.answer("▶️ Not paused — nothing to resume.")
            return
        if named is None:
            await ctx.repositories.risk_state(session).save(PauseState())
        else:
            await ctx.repositories.market_pause(session, market=named).save(PauseState())
        await session.commit()
    log.info("bot.resumed", scope="global" if named is None else named.value, user_id=actor.user_id)
    if named is None:
        await message.answer("▶️ <b>Resumed.</b> New signals will be gated normally again.")
        return
    await message.answer(
        f"▶️ <b>Resumed {named.value}.</b> New {named.value} signals will be gated normally again."
    )


@admin_router.message(Command("watchlist"))
async def watchlist(
    message: Message, command: CommandObject, ctx: BotContext, actor: Actor
) -> None:
    """§3 ``/watchlist [add|remove SYMBOL]`` — owner-only, because it spends money.

    The watchlist decides what the shared deep analyst is pointed at, and an analyst
    call is ~$0.32 on the owner's key. A member adding twelve symbols would be
    spending somebody else's budget, so members do not have this command at all.

    A symbol is checked against the exchange's own instrument list before it is
    stored. It is one keyless public call, and the alternative is a typo that
    silently produces a skipped symbol every cycle for as long as nobody notices.

    **Per market from M10a.** ``/watchlist`` with no argument lists every enabled
    market's list; ``/watchlist crypto`` narrows. An edit names its market
    (``/watchlist crypto add SOLUSDT``) **only when more than one is enabled** — with
    one market there is nothing to disambiguate and the old two-word form is what the
    owner's fingers know.
    """
    async with ctx.database.session() as session:
        settings_repo = ctx.repositories.settings(session)
        stored = await settings_repo.all()
        config = effective_config(ctx.settings, stored)

        parts = (command.args or "").split()
        named = parse_market(parts[0]) if parts else None
        if named is not None:
            parts = parts[1:]
        market = named or LEGACY_MARKET

        if named is not None and named not in config.enabled_markets:
            await message.answer(f"❌ <b>{named.value}</b> is not enabled on this deployment.")
            return

        if not parts:
            markets = config.enabled_markets if named is None else (market,)
            await message.answer(
                "\n\n".join(
                    watchlist_card(
                        tuple(config.market(each).watchlist),
                        watchlist_source(stored, each),
                        header=section_header(each, config),
                    )
                    for each in markets
                )
            )
            return

        current: tuple[str, ...] = tuple(config.market(market).watchlist)
        market_config = config.market(market)

        if len(parts) != 2 or parts[0].lower() not in {"add", "remove"}:
            await message.answer(
                "❌ Usage: /watchlist · /watchlist add SOLUSDT · /watchlist remove SOLUSDT"
            )
            return

        action, raw_symbol = parts[0].lower(), parts[1]
        parsed = parse_symbol(raw_symbol)
        if isinstance(parsed, Invalid):
            await message.answer(f"❌ {escape(parsed.message)}")
            return

        if action == "add":
            if parsed in current:
                await message.answer(f"{escape(parsed)} is already on the watchlist.")
                return
            # M8.3: the cap binds the owner too (owner ruling 2026-08-19). It is a
            # spend control — every symbol is screened every cycle and may buy a
            # ~$0.28 analyst call — and a cap that applied only to members would not
            # be a cap. It is the owner's own config value to raise.
            if len(current) >= market_config.watchlist_max_symbols:
                await message.answer(
                    f"❌ The watchlist is full ({len(current)} of "
                    f"{market_config.watchlist_max_symbols}). Remove a symbol first, or raise "
                    "<code>watchlist_max_symbols</code> in config.yaml."
                )
                return
            known = await ctx.repositories.instruments(session).get(parsed)
            unknown = await verify_symbol(parsed, known, ctx.symbol_checker)
            if isinstance(unknown, Invalid):
                await message.answer(f"❌ {escape(unknown.message)}")
                return
            updated = (*current, parsed)
        else:
            if parsed not in current:
                await message.answer(f"{escape(parsed)} is not on the watchlist.")
                return
            updated = tuple(symbol for symbol in current if symbol != parsed)
            if not updated:
                await message.answer(
                    "❌ That would empty the watchlist, and an empty watchlist means "
                    "the scan cycle has nothing to do. Add another symbol first."
                )
                return

        await settings_repo.set(
            watchlist_key(market), list(updated), at=ctx.clock.now(), user_id=actor.user_id
        )
        await session.commit()

    log.info(
        "bot.watchlist_changed",
        market=market.value,
        action=action,
        symbol=parsed,
        size=len(updated),
    )
    await message.answer(watchlist_card(updated, "db", header=section_header(market, config)))


@admin_router.message(Command("settings"))
async def settings(message: Message, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/settings`` — every runtime value that shapes a signal, with its source.

    ``capital_eur`` and ``risk_per_trade_pct`` are tagged ``you`` rather than ``db``
    from M8.1: they are no longer one global setting, and a card that still said
    ``db`` would imply the value applies to everybody.
    """
    account = actor.known()
    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
    config = effective_config(ctx.settings, stored)
    risk_config, costs, telegram = config.risk, config.costs, config.telegram

    view = SettingsView(
        groups=(
            (
                "Sizing — yours (specs/RISK_ENGINE.md §1)",
                (
                    (
                        "capital_eur",
                        "not set" if account.capital_eur is None else f"€{account.capital_eur}",
                        "you" if account.capital_eur is not None else "unset",
                    ),
                    (
                        "risk_per_trade_pct",
                        f"{risk_pct_of(account, config)}%",
                        "you" if account.risk_per_trade_pct is not None else "yaml",
                    ),
                    ("max_open_risk_pct", f"{risk_config.max_open_risk_pct}%", "yaml"),
                    ("max_positions", str(risk_config.max_positions), "yaml"),
                    ("max_leverage", f"{risk_config.max_leverage}x", "yaml"),
                    ("margin_budget_pct", f"{risk_config.margin_budget_pct}%", "yaml"),
                ),
            ),
            (
                "Gate thresholds",
                (
                    ("min_rr_tp1", f"{risk_config.min_rr_tp1}R (net of costs)", "yaml"),
                    ("min_confidence", str(risk_config.min_confidence), "yaml"),
                    ("max_entry_distance_pct", f"{risk_config.max_entry_distance_pct}%", "yaml"),
                    (
                        "stop_atr_multiple",
                        f"{risk_config.stop_atr_min_multiple} to "
                        f"{risk_config.stop_atr_max_multiple}",
                        "yaml",
                    ),
                    ("liq_buffer_multiple", f"{risk_config.liq_buffer_multiple}x", "yaml"),
                    ("daily_loss_limit_pct", f"{risk_config.daily_loss_limit_pct}%", "yaml"),
                    ("signal_cooldown_hours", f"{risk_config.signal_cooldown_hours}h", "yaml"),
                ),
            ),
            (
                "Costs (specs/RISK_ENGINE.md §4.2)",
                (
                    ("maker_fee_pct", f"{costs.maker_fee_pct}%", "yaml"),
                    ("taker_fee_pct", f"{costs.taker_fee_pct}%", "yaml"),
                    ("funding_interval_hours", f"{costs.funding_interval_hours}h", "yaml"),
                    (
                        "credit_favourable_funding",
                        str(costs.credit_favourable_funding).lower(),
                        "yaml",
                    ),
                ),
            ),
            (
                "Pipeline",
                (
                    *(
                        (
                            "watchlist"
                            if not config.multi_market
                            else f"watchlist ({market.value})",
                            f"{len(config.market(market).watchlist)} symbols",
                            watchlist_source(stored, market),
                        )
                        for market in config.enabled_markets
                    ),
                    *(
                        (
                            "scan_interval_minutes"
                            if not config.multi_market
                            else f"scan_interval_minutes ({market.value})",
                            str(config.market(market).scan_interval_minutes),
                            "yaml",
                        )
                        for market in config.enabled_markets
                    ),
                    ("screener_model", config.llm.screener_model, "yaml"),
                    ("analyst_model", config.llm.analyst_model, "yaml"),
                    ("owner_timezone", telegram.owner_timezone, "yaml"),
                    ("card_charts", ", ".join(telegram.card_chart_timeframes), "yaml"),
                ),
            ),
        )
    )
    await message.answer(settings_card(view))


__all__ = ["USAGE", "admin_router"]


@admin_router.callback_query(WatchlistCallback.filter())
async def watchlist_request_button(
    query: CallbackQuery,
    callback_data: WatchlistCallback,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
) -> None:
    """Approve/Decline on a member's ``/request`` (M8.3).

    Behind ``OwnerOnly`` on this router, like every other admin button: the payload
    is client-supplied and a forwarded card carries its buttons with it.

    **The cap is re-checked here, not only at request time**, and that is the point
    of doing the work twice. Three requests can be pending against two free slots,
    and each was legal when it was made. Only the moment of approval knows what the
    watchlist actually holds.

    A refusal here leaves the request **PENDING** rather than rejecting it (owner
    ruling 2026-08-19). Nobody decided against the symbol — the list ran out of room
    while it waited — so the owner can make space and approve the same card, instead
    of the member having to ask again for something they were about to be given.
    Both people are told which of the two happened.
    """
    symbol = callback_data.symbol
    approved = callback_data.action is AdminAction.APPROVE
    now = ctx.clock.now()

    async with ctx.database.session() as session:
        requests = ctx.repositories.watchlist_requests(session)
        pending = await requests.pending_for(symbol)
        if pending is None:
            await query.answer("That request has already been answered.", show_alert=True)
            return

        settings_repo = ctx.repositories.settings(session)
        stored = await settings_repo.all()
        config = effective_config(ctx.settings, stored)
        # A request is filed against a market (the row carries one), and M10a's
        # requests repository is bound to it. Only crypto can be requested today —
        # there is no forex adapter and no forex screener — so this reads the market
        # the repository is scoped to rather than inventing a second source of truth.
        market = requests.market
        current: tuple[str, ...] = tuple(config.market(market).watchlist)
        cap = config.market(market).watchlist_max_symbols

        if approved and symbol not in current and len(current) >= cap:
            await _watchlist_full(bot, ctx, pending, cap=cap)
            await query.answer(f"Watchlist is full ({cap}). Still pending.", show_alert=True)
            return

        decided = await requests.decide(
            symbol,
            WatchlistRequestStatus.APPROVED if approved else WatchlistRequestStatus.REJECTED,
            by=actor.user_id,
            at=now,
        )
        if approved and symbol not in current:
            # The same write ``/watchlist add`` makes, so the ``config_changes`` row
            # is identical whether a symbol arrived by hand or by button.
            await settings_repo.set(
                watchlist_key(market), [*current, symbol], at=now, user_id=actor.user_id
            )
        await session.commit()

    if decided is None:  # pragma: no cover — it was pending a moment ago
        return

    log.info(
        "bot.watchlist_request_decided",
        symbol=symbol,
        approved=approved,
        by_user_id=actor.user_id,
        requested_by=decided.requested_by_user_id,
    )
    await _tell_requester(
        bot,
        ctx,
        decided.requested_by_user_id,
        watchlist_request_decided_card(symbol, approved=approved),
    )
    await query.answer("Added." if approved else "Declined.")
    if query.message is None:  # pragma: no cover — a card too old to still be there
        return
    try:
        await query.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except Exception:  # pragma: no cover — an un-editable card is not an error
        log.info("bot.card_not_edited", symbol=symbol)


async def _watchlist_full(
    bot: SupportsBot, ctx: BotContext, request: WatchlistRequest, *, cap: int
) -> None:
    """Tell both sides the list filled up, and that the request survives."""
    await _tell_requester(
        bot,
        ctx,
        request.requested_by_user_id,
        watchlist_full_card(request.symbol, cap=cap, requester=True),
    )
    owner_id = ctx.settings.secrets.owner_user_id
    async with ctx.database.session() as session:
        owner = await ctx.repositories.users(session).owner()
    if owner is not None:
        owner_id = owner.telegram_user_id
    if owner_id is None:  # pragma: no cover — an owner pressed the button to get here
        return
    await _tell_requester(
        bot, ctx, owner_id, watchlist_full_card(request.symbol, cap=cap, requester=False)
    )


async def _tell_requester(bot: SupportsBot, ctx: BotContext, user_id: int, text: str) -> None:
    """Deliver one notice, and never let a blocked chat fail the owner's button."""
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ctx.settings.config.telegram.parse_mode,
            reply_markup=None,  # type: ignore[arg-type]
            reply_to_message_id=None,
        )
    except Exception:
        log.warning("bot.notice_undeliverable", user_id=user_id, exc_info=True)

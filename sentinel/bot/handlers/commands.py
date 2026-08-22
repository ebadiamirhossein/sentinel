"""Member commands — specs/TELEGRAM_UX.md §3.

``/capital /risk /positions /stats``. Everything here is **scoped to the caller**:
their own capital, their own risk %, their own signals, their own statistics. Nothing
in this module can read another user's book, because every query it makes takes a
required ``user_id``.

``/start /help /leave`` live in ``membership.py`` (they must work before full
standing does), and ``/status /settings /watchlist /pause /resume /users /approve
/reject /suspend`` live in ``admin.py`` behind the owner filter. ``/analyze`` is P1
and stays unregistered, because an unregistered command is silent rather than
answered with a promise.

**Every handler here answers even when it fails** (M8.6). ``answers_on_failure``
wraps each one, because on a bot where silence is the designed response to anyone
without standing, a crash and a command that does not exist are indistinguishable —
which is journal/M8_2_REPORT.md §1's lesson, and how a ``TelegramBadRequest`` in
``/snapshot`` reached production looking like a dead handler.

``/pulse`` (M8.4) is the one command here that is **not** scoped to the caller, and
deliberately so: it reports the pipeline's own reasoning, which is bought once and
shared, so it is identical for every approved user. It reads no book and prints no
capital, sizing, decision or statistic — the only thing that varies is the spend
line, which the owner sees and a member does not, and that variation is carried by
the view object rather than by a check in the renderer.

Every handler writes through a repository and commits its own unit of work, then
confirms back in words — §3 requires ``/capital`` and ``/risk`` to "confirm", and a
setting that changes sizing should never change quietly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from sentinel.analyst.persian.models import PersianSourceKind
from sentinel.bot.auth import Actor
from sentinel.bot.cards import (
    journal_caption,
    journal_empty_card,
    positions_card,
    pulse_card,
    pulse_day_card,
    snapshot_card,
    stats_card,
    symbol_pulse_card,
    watchlist_request_ack_card,
    watchlist_request_card,
)
from sentinel.bot.context import BotContext
from sentinel.bot.export import journal_filename, journal_workbook
from sentinel.bot.formatting import escape, money_eur
from sentinel.bot.handlers.guard import answers_on_failure
from sentinel.bot.keyboards import persian_keyboard, watchlist_request_keyboard
from sentinel.bot.markets import (
    market_of_symbol,
    parse_market,
    resolve_markets,
    section_header,
)
from sentinel.bot.models import SignalDecision
from sentinel.bot.outbound import SupportsBot
from sentinel.bot.plans import plan_of
from sentinel.bot.pulse import pulse_day_view, pulse_view, symbol_pulse_view
from sentinel.bot.readmodels import position_view, spend_view, stats_view
from sentinel.bot.runtime import (
    Invalid,
    effective_config,
    parse_capital,
    parse_risk_pct,
    parse_symbol,
    risk_pct_of,
    verify_symbol,
)
from sentinel.bot.snapshot import snapshot_view
from sentinel.bot.views import SpendView
from sentinel.core.logging import get_logger
from sentinel.core.markets import LEGACY_MARKET, Market
from sentinel.stats.journal import JournalBook, build_journal
from sentinel.stats.queries import WINDOWS, build_report, parse_window, window_start

log = get_logger(__name__)

commands_router = Router(name="commands")


@commands_router.message(Command("capital"))
@answers_on_failure
async def capital(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/capital 10000`` — validated > 0, confirmed, applies to new signals only.

    Written to the caller's own ``users`` row from M8.1. It was one global setting
    through M8, which is exactly the value that must not be shared.
    """
    account = actor.known()
    if not command.args:
        await message.answer(
            f"Your capital: €{money_eur(account.capital_eur)}"
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
@answers_on_failure
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
@answers_on_failure
async def positions(message: Message, ctx: BotContext, actor: Actor) -> None:
    """§3 ``/positions`` — the caller's Taken signals with live uPnL in R and EUR.

    The mark is the tracker's last observed price for the symbol, and the R figure
    comes from ``risk/accounting.py``. The snapshot feed is shared — it is market
    data — but the signals, the sizing and therefore every euro figure are the
    caller's alone.

    **Per market from M10a**, in config order, each block headed by its market when
    more than one is enabled. Positions are never summed across markets: the mark,
    the R and the euro figures all come from one market's own price feed.
    """
    config = effective_config(ctx.settings, {})
    blocks: list[str] = []
    async with ctx.database.session() as session:
        for market in config.enabled_markets:
            rows = await ctx.repositories.signals(session, market=market).with_decision(
                SignalDecision.TAKEN, user_id=actor.user_id
            )
            fills = await ctx.repositories.fills(session).for_signals([row.id for row in rows])
            exits = await ctx.repositories.exits(session).for_signals([row.id for row in rows])
            marks = {
                row.symbol: snapshot.last_price
                for snapshot in await ctx.repositories.snapshots(
                    session, market=market
                ).latest_per_symbol()
                for row in rows
                if row.symbol == snapshot.symbol
            }
            views = [
                position_view(
                    row,
                    # On the market being iterated, never on the payload (§16.7).
                    plan_of(row.plan, market),
                    fills.get(row.id, []),
                    exits.get(row.id, []),
                    mark_price=marks.get(row.symbol),
                )
                for row in rows
            ]
            blocks.append(positions_card(views, ctx.tz, header=section_header(market, config)))
    await message.answer("\n\n".join(blocks))


@commands_router.message(Command("stats"))
@answers_on_failure
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

    **And a fourth dimension at M10a: the market.** One block per enabled market,
    never a sum — a win rate that averaged a forex book into a crypto one would move
    when a market was switched on, which is not a fact about anything. The argument
    takes a market as well as a window (``/stats forex``, ``/stats 90d``); with one
    market enabled the card is exactly the one M8.1 shipped.
    """
    config = effective_config(ctx.settings, {})
    markets = resolve_markets(command.args, config)
    if isinstance(markets, Invalid):
        await message.answer(f"❌ {markets.message}")
        return
    # A market name is not a window. Without this, ``/stats forex`` would fall
    # through ``parse_window``'s "anything unrecognised is 30d" branch — the right
    # answer by accident, and the wrong one the day a market is named "all".
    window = parse_window(None if parse_market(command.args or "") else command.args)

    async with ctx.database.session() as session:
        reports = [
            await build_report(
                session,
                window=window,
                now=ctx.clock.now(),
                user_id=actor.user_id,
                market=market,
            )
            for market in markets
        ]
    await message.answer(
        "\n\n".join(
            stats_card(stats_view(report, header=section_header(report.market, config)), ctx.tz)
            for report in reports
        )
    )


#: What ``/pulse`` accepts after the command. Anything else is answered with usage
#: rather than silently treated as the default: a member typing ``/pulse 7d`` and
#: getting the last cycle would read it as a day's worth of nothing.
PULSE_DAY_ARGS = frozenset({"24h", "day", "1d", "today"})

PULSE_USAGE = (
    "❌ Usage: <code>/pulse</code> for the last cycle, <code>/pulse 24h</code> for the "
    "day, or <code>/pulse SOLUSDT</code> for one symbol's full verdict."
)


@commands_router.message(Command("pulse"))
@answers_on_failure
async def pulse(message: Message, command: CommandObject, ctx: BotContext, actor: Actor) -> None:
    """§3b ``/pulse [24h]`` — what the pipeline did, for everybody (M8.4).

    Read-only, and every table it touches is one the orchestrator writes: the
    screener's verdicts out of the ``llm_calls`` audit row, the skips out of
    ``cycles.skipped``, the verdicts out of ``analyst_reports``, the outcomes out of
    ``gate_decisions``. Nothing here writes and nothing here computes — ``bot/pulse``
    folds the rows, this handler fetches them, ``cards`` renders them.

    **The owner-only half is one argument, not a branch.** ``spend`` is passed as
    ``None`` for a member, so the view has no figure to print. A role check inside
    the renderer would be a boundary that survives only as long as everybody
    remembers it is there (M8.1 §6).
    """
    raw = (command.args or "").strip().split()[0].lower() if command.args else ""

    # The day words are checked first, and the two vocabularies cannot collide:
    # `parse_symbol` needs five alphanumeric characters and every day word is
    # shorter. Stated as an ordering anyway — a future `/pulse week` would be a
    # four-letter word that is also not a symbol, and the order is what keeps that
    # decision in one place.
    config = effective_config(ctx.settings, {})

    if raw in PULSE_DAY_ARGS:
        now = ctx.clock.now()
        blocks = [
            await _pulse_day(
                ctx,
                now=now,
                market=market,
                header=section_header(market, config),
                spend=await _pulse_spend(ctx, now=now, market=market, owner=actor.is_owner),
            )
            for market in config.enabled_markets
        ]
        await message.answer("\n\n".join(blocks))
        return

    named = parse_market(raw) if raw else None
    if named is not None:
        if named not in config.enabled_markets:
            await message.answer(f"❌ <b>{named.value}</b> is not enabled on this deployment.")
            return
        now = ctx.clock.now()
        await message.answer(
            await _pulse_cycle(
                ctx,
                market=named,
                header=section_header(named, config),
                spend=await _pulse_spend(ctx, now=now, market=named, owner=actor.is_owner),
            )
        )
        return

    if raw:
        parsed = parse_symbol(raw)
        if isinstance(parsed, Invalid):
            await message.answer(PULSE_USAGE)
            return
        pages, report_id = await _pulse_symbol(ctx, parsed)
        for index, page in enumerate(pages, start=1):
            # The 🇮🇷 فارسی button goes on the LAST page only, and the summary covers
            # every page: the card splits for Telegram's 4096-character limit, but it
            # is one verdict to a reader, and one button under the end of it is where a
            # reader looks. A card with no stored verdict has no id and no button.
            last = index == len(pages)
            await message.answer(
                page,
                reply_markup=(
                    persian_keyboard(PersianSourceKind.PULSE_VERDICT, report_id)
                    if last and report_id is not None
                    else None
                ),
            )
        return

    now = ctx.clock.now()
    blocks = [
        await _pulse_cycle(
            ctx,
            market=market,
            header=section_header(market, config),
            spend=await _pulse_spend(ctx, now=now, market=market, owner=actor.is_owner),
        )
        for market in config.enabled_markets
    ]
    await message.answer("\n\n".join(blocks))


async def _pulse_symbol(ctx: BotContext, symbol: str) -> tuple[tuple[str, ...], UUID | None]:
    """§3b ``/pulse SOLUSDT`` — one symbol's last verdict, in full (M8.5).

    **No spend argument.** This card carries no cost figure for anybody, so unlike the
    other two forms there is nothing here that varies by role — the owner and a member
    get byte-identical text. The symbol is not verified against the exchange either:
    the question is "what did we last say about this", and for a symbol nobody has
    analysed the answer is the same whether or not the exchange lists it.

    Returns pages, because the card splits rather than truncates when the analyst was
    verbose. Usually one.

    Also returns the report's id (M11p), so the caller can hang a 🇮🇷 فارسی button off
    the last page. ``None`` when no verdict exists — there is nothing to explain, and a
    button that produced "there is no analysis of this symbol" in Persian would be a
    worse answer than no button.
    """
    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
        config = effective_config(ctx.settings, stored)
        # The symbol names its own market — symbols are disjoint across markets — so
        # a reader never types one. See ``bot/markets.market_of_symbol``.
        market = market_of_symbol(symbol, config)
        reports = ctx.repositories.reports(session, market=market)
        row = await reports.latest_for_symbol(symbol)
        decisions = (
            []
            if row is None or row.cycle_id is None
            else await ctx.repositories.gate_decisions(session, market=market).for_cycles(
                [row.cycle_id]
            )
        )

    watchlist = config.market(market).watchlist
    view = symbol_pulse_view(row, decisions, symbol=symbol, on_watchlist=symbol in watchlist)
    return symbol_pulse_card(view, ctx.tz), None if row is None else row.id


async def _pulse_spend(
    ctx: BotContext, *, now: datetime, market: Market, owner: bool
) -> SpendView | None:
    """Today's LLM spend — **the owner's, and nobody else's to see**.

    specs/TELEGRAM_UX.md §7: the spend guard is an owner-only channel because a
    member has no lever to pull in response to it. It is also simply the owner's
    bill. Returning ``None`` rather than filtering later means the figure is never
    fetched for a member at all.
    """
    if not owner:
        return None
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with ctx.database.session() as session:
        totals = await ctx.repositories.llm_calls(session, market=market).spend_totals(
            day_start=day_start,
            month_start=day_start.replace(day=1),
            priced_models=tuple(ctx.settings.config.llm.pricing),
        )
    return spend_view(totals, ctx.settings.config.llm, market=ctx.settings.config.market(market))


async def _pulse_cycle(
    ctx: BotContext, *, market: Market, header: str, spend: SpendView | None
) -> str:
    """This market's last cycle that actually finished — not the one running.

    Per market from M10a: the scheduler runs one job per enabled market, so "the
    last cycle" is a question with one answer per market and no answer at all across
    them. The header is empty unless a second market is enabled.
    """
    async with ctx.database.session() as session:
        cycle = await ctx.repositories.cycles(session, market=market).latest_completed()
        if cycle is None:
            return pulse_card(
                pulse_view(None, screener=(), reports=(), decisions=(), spend=spend),
                ctx.tz,
                header=header,
            )
        cycle_ids = [cycle.cycle_id]
        calls = ctx.repositories.llm_calls(session, market=market)
        screener = await calls.screener_verdicts(cycle_ids)
        reports = await ctx.repositories.reports(session, market=market).for_cycles(cycle_ids)
        decisions = await ctx.repositories.gate_decisions(session, market=market).for_cycles(
            cycle_ids
        )

    view = pulse_view(
        cycle,
        screener=screener.get(cycle.cycle_id, ()),
        reports=reports,
        decisions=decisions,
        spend=spend,
    )
    return pulse_card(view, ctx.tz, header=header)


async def _pulse_day(
    ctx: BotContext, *, now: datetime, market: Market, header: str, spend: SpendView | None
) -> str:
    """The same four sections over a day, counted rather than listed — per market."""
    since = now - timedelta(hours=24)
    async with ctx.database.session() as session:
        cycles_repo = ctx.repositories.cycles(session, market=market)
        cycles = await cycles_repo.completed_since(since)
        _, started = await cycles_repo.completion_since(since)
        cycle_ids = [cycle.cycle_id for cycle in cycles]
        calls = ctx.repositories.llm_calls(session, market=market)
        screener = await calls.screener_verdicts(cycle_ids)
        reports = await ctx.repositories.reports(session, market=market).for_cycles(cycle_ids)
        decisions = await ctx.repositories.gate_decisions(session, market=market).for_cycles(
            cycle_ids
        )

    view = pulse_day_view(
        cycles,
        since=since,
        started=started,
        screener=screener,
        reports=reports,
        decisions=decisions,
        spend=spend,
    )
    return pulse_day_card(view, ctx.tz, header=header)


#: ``/journal`` accepts the same windows ``/stats`` does, plus its own default.
JOURNAL_WINDOWS = frozenset({*WINDOWS, "all"})

#: **"all", where ``/stats`` defaults to 30d.** A statistic is a report about a
#: recent period; an export is an archive, and the commonest reason to ask for one
#: is to have the whole thing.
DEFAULT_JOURNAL_WINDOW = "all"

JOURNAL_USAGE = (
    "❌ Usage: <code>/journal</code> for your whole history, or "
    "<code>/journal 30d</code> · <code>/journal 90d</code> · <code>/journal all</code>."
)

SNAPSHOT_USAGE = (
    "❌ Usage: <code>/snapshot SOLUSDT</code> — the numbers the code measured for one "
    "symbol, before any AI analysis."
)


@commands_router.message(Command("journal"))
@answers_on_failure
async def journal(
    message: Message,
    command: CommandObject,
    ctx: BotContext,
    actor: Actor,
    bot: SupportsBot,
) -> None:
    """§3c ``/journal [30d|90d|all]`` — **the caller's own book, as a file** (M8.6).

    The most per-user surface in this bot, and the only one whose output leaves the
    chat as a document. Three things carry the scoping, none of them a filter
    somebody has to remember:

    * ``SignalRepository.journal_since`` takes a keyword-only ``user_id`` with no
      default, so a caller cannot fail to supply one;
    * ``stats.journal.JournalRow`` has no field that could hold a user id, so
      nothing another user owns can reach a cell;
    * the file is sent to ``actor.user_id`` — the caller's own private chat — rather
      than to ``message.chat.id``, which is where every other outbound in this
      codebase is addressed too. A document is forwardable and this one is somebody's
      whole trading record; it should leave the process pointed at exactly one place.

    An unrecognised window gets the usage line rather than silently falling back to
    the default, on M8.4's ruling for ``/pulse 7d``: a person who typed ``/journal
    7d`` and received their whole history would not notice they had been answered a
    different question.

    **One sheet per (population, market) pair from M10a**, never merged. A running
    balance walks *within* a sheet, and one that stepped from a EUR/USD trade into a
    BTC one would mean nothing — the same objection M8.6 makes to stepping from a
    trade you took into one you skipped. With one market the sheet names are exactly
    M8.6's, because a reader's saved files and formulas already refer to them.
    """
    raw = (command.args or "").strip().split()[0].lower() if command.args else ""
    window = raw or DEFAULT_JOURNAL_WINDOW
    if window not in JOURNAL_WINDOWS:
        await message.answer(JOURNAL_USAGE)
        return

    now = ctx.clock.now()
    config = effective_config(ctx.settings, {})
    books: list[JournalBook] = []
    exported = 0
    async with ctx.database.session() as session:
        for market in config.enabled_markets:
            rows = await ctx.repositories.signals(session, market=market).journal_since(
                window_start(window, now=now), user_id=actor.user_id
            )
            signal_ids = [row.id for row in rows]
            fills = await ctx.repositories.fills(session).for_signals(signal_ids)
            exits = await ctx.repositories.exits(session).for_signals(signal_ids)
            exported += len(rows)
            # A market with nothing in the window gets no sheets at all. The
            # always-written pair (Real, Hypothetical) exists so an *empty* sheet
            # reads as "you have none of these" — repeating that for a market the
            # reader may not even have enabled would be noise, not honesty.
            if rows or market is LEGACY_MARKET:
                books.extend(build_journal(rows, fills=fills, exits=exits, market=market))

    if not exported:
        # A file with nothing but headers is indistinguishable from a broken export,
        # and the reader would open it to find out which it was.
        await message.answer(journal_empty_card(window))
        return

    log.info("bot.journal_exported", user_id=actor.user_id, window=window, signals=exported)
    await bot.send_document(
        chat_id=actor.user_id,
        document=BufferedInputFile(
            journal_workbook(books, window_label=window, generated_at=now, tz=ctx.tz),
            filename=journal_filename(now, ctx.tz),
        ),
        caption=journal_caption(books, window_label=window),
        parse_mode=ctx.settings.config.telegram.parse_mode,
    )


@commands_router.message(Command("snapshot"))
@answers_on_failure
async def snapshot(message: Message, command: CommandObject, ctx: BotContext) -> None:
    """§3d ``/snapshot SOLUSDT`` — the deterministic view, before the AI (M8.6).

    Shared market data, so this handler takes no ``actor`` at all: there is nothing
    on the card that could vary by caller, and a parameter it does not receive is a
    boundary it cannot cross. The counterpart to ``/pulse SOLUSDT``, which is
    entirely model output; this one contains none.

    Read-only, one table, and no exchange round trip — the question is "what did the
    pipeline measure", and for a symbol nobody has ingested the answer is the same
    whether or not the exchange lists it (M8.5 decision 2).
    """
    if not command.args:
        await message.answer(SNAPSHOT_USAGE)
        return

    parsed = parse_symbol(command.args.strip().split()[0])
    if isinstance(parsed, Invalid):
        await message.answer(SNAPSHOT_USAGE)
        return

    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
        config = effective_config(ctx.settings, stored)
        market = market_of_symbol(parsed, config)
        row = await ctx.repositories.snapshots(session, market=market).latest_for_symbol(parsed)

    watchlist = config.market(market).watchlist
    view = snapshot_view(row, symbol=parsed, on_watchlist=parsed in watchlist)
    await message.answer(snapshot_card(view, ctx.tz))


__all__ = ["commands_router"]


@commands_router.message(Command("request"))
@answers_on_failure
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
        # A request joins one market's watchlist. The symbol usually names it; when
        # it is on nobody's list yet — which is the normal case for a request — this
        # is the first enabled market, i.e. crypto on every deployment that exists.
        market = market_of_symbol(parsed, config)
        market_config = config.market(market)
        current: tuple[str, ...] = tuple(market_config.watchlist)

        if parsed in current:
            await message.answer(f"{escape(parsed)} is already on the watchlist.")
            return
        if len(current) >= market_config.watchlist_max_symbols:
            await message.answer(
                f"❌ The watchlist is full ({len(current)} of "
                f"{market_config.watchlist_max_symbols}). Ask the owner to make room first."
            )
            return

        known = await ctx.repositories.instruments(session).get(parsed)
        unknown = await verify_symbol(parsed, known, ctx.symbol_checker)
        if isinstance(unknown, Invalid):
            await message.answer(f"❌ {escape(unknown.message)}")
            return

        requests = ctx.repositories.watchlist_requests(session, market=market)
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
            cap=market_config.watchlist_max_symbols,
        ),
        parse_mode=ctx.settings.config.telegram.parse_mode,
        reply_markup=watchlist_request_keyboard(parsed),
        reply_to_message_id=None,
    )

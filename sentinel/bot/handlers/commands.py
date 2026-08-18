"""Commands — specs/TELEGRAM_UX.md §3.

Shipped here: ``/capital /risk /status /positions /pause /resume /watchlist
/settings`` (the M6 scope in docs/MILESTONES.md). ``/stats``, ``/pulse`` and
``/analyze`` are not registered: the first needs outcomes the tracker measures
from M7, and the other two are P1. An unregistered command is silent rather than
answered with a promise.

Every handler writes through a repository and commits its own unit of work, then
confirms back in words — §3 requires ``/capital`` and ``/risk`` to "confirm", and
a setting that changes sizing should never change quietly.
"""

from __future__ import annotations

from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sentinel.bot.cards import positions_card, settings_card, status_card, watchlist_card
from sentinel.bot.context import BotContext
from sentinel.bot.formatting import escape
from sentinel.bot.keyboards import resume_keyboard
from sentinel.bot.models import SignalDecision
from sentinel.bot.runtime import (
    CAPITAL_EUR,
    RISK_PER_TRADE_PCT,
    WATCHLIST,
    Invalid,
    effective_config,
    parse_capital,
    parse_risk_pct,
    parse_symbol,
    source_of,
    verify_symbol,
)
from sentinel.bot.views import DataSourceView, SettingsView, StatusView
from sentinel.core.logging import get_logger
from sentinel.risk.models import PauseReason, PauseState

log = get_logger(__name__)

commands_router = Router(name="commands")


def _user_id(message: Message) -> int:
    """The allowlist middleware guarantees a sender before a handler runs."""
    assert message.from_user is not None
    return message.from_user.id


@commands_router.message(Command("capital"))
async def capital(message: Message, command: CommandObject, ctx: BotContext) -> None:
    """§3 ``/capital 10000`` — validated > 0, confirmed, applies to new signals only."""
    async with ctx.database.session() as session:
        settings_repo = ctx.repositories.settings(session)
        if not command.args:
            current = await settings_repo.get(CAPITAL_EUR)
            await message.answer(
                f"Capital: €{current}"
                if current is not None
                else "Capital is not set. The risk gate rejects every signal with "
                "NO_CAPITAL until it is.\nSet it with: /capital 10000"
            )
            return

        parsed = parse_capital(command.args)
        if isinstance(parsed, Invalid):
            await message.answer(f"❌ {escape(parsed.message)}")
            return

        await settings_repo.set(
            CAPITAL_EUR, str(parsed), at=ctx.clock.now(), user_id=_user_id(message)
        )
        await session.commit()

    log.info("bot.capital_set", capital_eur=str(parsed), user_id=_user_id(message))
    await message.answer(
        f"✅ Capital set to <b>€{parsed}</b>.\n"
        "Applies to new signals only — open ones keep the sizing they were issued "
        "with (specs/RISK_ENGINE.md §7)."
    )


@commands_router.message(Command("risk"))
async def risk(message: Message, command: CommandObject, ctx: BotContext) -> None:
    """§3 ``/risk 0.75`` — bounds come from config, never from a literal here."""
    risk_config = ctx.settings.config.risk
    async with ctx.database.session() as session:
        settings_repo = ctx.repositories.settings(session)
        if not command.args:
            stored = await settings_repo.get(RISK_PER_TRADE_PCT)
            value = risk_config.risk_per_trade_pct if stored is None else Decimal(str(stored))
            await message.answer(
                f"Risk per trade: <b>{value}%</b> "
                f"(allowed {risk_config.risk_per_trade_min_pct} to "
                f"{risk_config.risk_per_trade_max_pct}%)"
            )
            return

        parsed = parse_risk_pct(command.args, risk_config)
        if isinstance(parsed, Invalid):
            await message.answer(f"❌ {escape(parsed.message)}")
            return

        await settings_repo.set(
            RISK_PER_TRADE_PCT, str(parsed), at=ctx.clock.now(), user_id=_user_id(message)
        )
        await session.commit()

    log.info("bot.risk_set", risk_pct=str(parsed), user_id=_user_id(message))
    await message.answer(
        f"✅ Risk per trade set to <b>{parsed}%</b>.\nApplies to new signals only."
    )


@commands_router.message(Command("status"))
async def status(message: Message, ctx: BotContext) -> None:
    """§3 ``/status`` — assembled from state that exists, with the gaps named."""
    async with ctx.database.session() as session:
        pause = await ctx.repositories.risk_state(session).load()
        stored = await ctx.repositories.settings(session).all()
        signals_repo = ctx.repositories.signals(session)
        recent = await signals_repo.recent(limit=200)
        undecided = await signals_repo.undecided_count()
        taken = await signals_repo.with_decision(SignalDecision.TAKEN, limit=200)
        snapshots = await ctx.repositories.snapshots(session).latest_per_symbol()
        stuck = await ctx.repositories.messages(session).stuck()

    config = effective_config(ctx.settings, stored)
    capital = stored.get(CAPITAL_EUR)
    risk_pct = stored.get(RISK_PER_TRADE_PCT)
    view = StatusView(
        paused=pause.is_active(ctx.clock.now()),
        pause_reason=None if pause.reason is None else pause.reason.value,
        paused_until=pause.until,
        capital_eur=None if capital is None else Decimal(str(capital)),
        risk_per_trade_pct=(
            config.risk.risk_per_trade_pct if risk_pct is None else Decimal(str(risk_pct))
        ),
        watchlist_size=len(config.watchlist),
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
    )
    await message.answer(status_card(view, ctx.tz))


@commands_router.message(Command("positions"))
async def positions(message: Message, ctx: BotContext) -> None:
    """§3 ``/positions`` — signals marked Taken (see ``positions_card`` on uPnL)."""
    async with ctx.database.session() as session:
        rows = await ctx.repositories.signals(session).with_decision(SignalDecision.TAKEN)
    await message.answer(positions_card(rows, ctx.tz))


@commands_router.message(Command("pause"))
async def pause(message: Message, ctx: BotContext) -> None:
    """§3 ``/pause`` — manual, no expiry, survives a restart (RISK_ENGINE §7)."""
    async with ctx.database.session() as session:
        await ctx.repositories.risk_state(session).save(
            PauseState(paused=True, reason=PauseReason.MANUAL, until=None)
        )
        await session.commit()
    log.info("bot.paused", reason=PauseReason.MANUAL.value, user_id=_user_id(message))
    await message.answer(
        "⏸️ <b>Paused.</b> No new signals will be gated through until /resume.\n"
        "The tracker keeps following anything already open."
    )


@commands_router.message(Command("resume"))
async def resume(message: Message, ctx: BotContext) -> None:
    """§3 ``/resume`` — a loss-limit pause needs an explicit confirmation button.

    The asymmetry is the point: a manual pause was a deliberate act and lifting it
    is another one, but a loss-limit pause exists precisely because the day has
    gone badly, and that is the moment a reflexive tap does the most damage.
    """
    async with ctx.database.session() as session:
        state = await ctx.repositories.risk_state(session).load()
        if not state.is_active(ctx.clock.now()):
            await message.answer("▶️ Not paused — nothing to resume.")
            return
        if state.reason is PauseReason.DAILY_LOSS_LIMIT:
            await message.answer(
                "⚠️ This is a <b>daily loss-limit</b> pause, not a manual one.\n"
                "Resuming now overrides a rail that exists to stop a cascade day. "
                "Confirm if that is what you want.",
                reply_markup=resume_keyboard(),
            )
            return
        await ctx.repositories.risk_state(session).save(PauseState())
        await session.commit()
    log.info("bot.resumed", user_id=_user_id(message))
    await message.answer("▶️ <b>Resumed.</b> New signals will be gated normally again.")


@commands_router.message(Command("watchlist"))
async def watchlist(message: Message, command: CommandObject, ctx: BotContext) -> None:
    """§3 ``/watchlist [add|remove SYMBOL]``.

    A symbol is checked against the exchange's own instrument list before it is
    stored. It is one keyless public call, and the alternative is a typo that
    silently produces a skipped symbol every cycle for as long as nobody notices.
    """
    async with ctx.database.session() as session:
        settings_repo = ctx.repositories.settings(session)
        stored = await settings_repo.all()
        config = effective_config(ctx.settings, stored)
        current: tuple[str, ...] = tuple(config.watchlist)

        if not command.args:
            await message.answer(watchlist_card(current, source_of(WATCHLIST, stored)))
            return

        parts = command.args.split()
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
            WATCHLIST, list(updated), at=ctx.clock.now(), user_id=_user_id(message)
        )
        await session.commit()

    log.info("bot.watchlist_changed", action=action, symbol=parsed, size=len(updated))
    await message.answer(watchlist_card(updated, "db"))


@commands_router.message(Command("settings"))
async def settings(message: Message, ctx: BotContext) -> None:
    """§3 ``/settings`` — every runtime value that shapes a signal, with its source."""
    async with ctx.database.session() as session:
        stored = await ctx.repositories.settings(session).all()
    config = effective_config(ctx.settings, stored)
    risk_config, costs, telegram = config.risk, config.costs, config.telegram
    capital = stored.get(CAPITAL_EUR)
    risk_pct = stored.get(RISK_PER_TRADE_PCT)

    view = SettingsView(
        groups=(
            (
                "Sizing (specs/RISK_ENGINE.md §1)",
                (
                    (
                        "capital_eur",
                        "not set" if capital is None else f"€{capital}",
                        source_of(CAPITAL_EUR, stored),
                    ),
                    (
                        "risk_per_trade_pct",
                        f"{risk_config.risk_per_trade_pct if risk_pct is None else risk_pct}%",
                        source_of(RISK_PER_TRADE_PCT, stored),
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
                    ("watchlist", f"{len(config.watchlist)} symbols", source_of(WATCHLIST, stored)),
                    ("scan_interval_minutes", str(config.schedule.scan_interval_minutes), "yaml"),
                    ("screener_model", config.llm.screener_model, "yaml"),
                    ("analyst_model", config.llm.analyst_model, "yaml"),
                    ("owner_timezone", telegram.owner_timezone, "yaml"),
                    ("card_charts", ", ".join(telegram.card_chart_timeframes), "yaml"),
                ),
            ),
        )
    )
    await message.answer(settings_card(view))


__all__ = ["commands_router"]

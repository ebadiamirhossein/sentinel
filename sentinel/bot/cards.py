"""Message rendering — specs/TELEGRAM_UX.md §1 and §3.

**The bot renders; it never computes.** Every number on a signal card is a field
of ``TradePlan``, interpolated as the risk engine quantized it. That is not a
style preference: the engine rounds quantities down, rounds stops away from the
entry, and derives net RR from the *displayed* gross multiple so a card
reconciles by hand. A renderer that recomputed anything would silently undo all
three, and the owner would be placing orders against numbers no test covers.

``tests/bot/test_no_arithmetic.py`` enforces this two ways: an AST scan of this
module for arithmetic operators, and a check that every number in a rendered card
also appears in the plan it was rendered from. ``abs()`` survives the scan on
purpose — it removes a sign for display and invents no magnitude — and is used
only so a funding *credit* reads as one instead of as "€-0.18".

If a card needs a number the plan lacks, the fix is a field on ``TradePlan``,
computed and tested in ``sentinel/risk/`` (that is where ``target_distances_pct``
and ``EntryRung.distance_pct`` came from). It is never a subtraction here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sentinel.analyst.models import Direction
from sentinel.bot.formatting import DISCLAIMER, escape, local_and_utc, local_date_time
from sentinel.bot.models import SignalDecision, SignalRecord
from sentinel.bot.views import (
    AlertView,
    PositionView,
    SettingsView,
    StatsView,
    StatusView,
    TrackerEventView,
)
from sentinel.risk.models import GateDecision, TradePlan

#: specs/TELEGRAM_UX.md §2 — what each button says once it has been pressed.
DECISION_LABEL = {
    SignalDecision.TAKEN: "✅ Taken",
    SignalDecision.WATCHING: "👀 Watching",
    SignalDecision.SKIPPED: "❌ Skipped",
}


def _base(symbol: str) -> str:
    """``SOLUSDT`` → ``SOL`` — the unit a quantity is denominated in."""
    return symbol.removesuffix("USDT")


def _side(direction: Direction) -> str:
    return "🟢 LONG" if direction is Direction.LONG else "🔴 SHORT"


def signal_card(record: SignalRecord, tz: ZoneInfo) -> str:
    """The core message (§1). Charts go in an album above it, buttons below it."""
    plan = record.plan
    report = plan.report
    lines = [
        f"{_side(plan.direction)} — {escape(plan.symbol)}   "
        f"[{plan.setup_type.value} · {plan.timeframe_label.value} · conf {plan.confidence}]",
        f"{escape(report.prompt_version or 'prompt n/a')} · Signal #{record.number} · "
        f"{local_and_utc(plan.created_at, tz)}",
        "",
        "📊 <b>Thesis</b>",
        escape(report.thesis),
    ]
    if report.counter_thesis:
        lines.append("")
        lines.append(f"⚠️ <b>Against:</b> {escape(report.counter_thesis)}")

    lines.append("")
    lines.append(
        f"🎯 <b>Plan</b> (capital €{plan.capital_eur} · risk {plan.risk_per_trade_pct}% "
        f"= €{plan.planned_risk_eur} · EURUSD {plan.eurusd_rate})"
    )
    lines.append(f"Entry ladder (limit orders) — last price {plan.last_price}:")
    for index, entry in enumerate(plan.entries, start=1):
        lines.append(
            f"  {index}) <b>{entry.price}</b> ({entry.distance_pct}%) — "
            f"{entry.weight_pct}% of risk — {entry.qty} {_base(plan.symbol)} "
            f"(€{entry.notional_eur})"
        )
    lines.append(f"  Weighted entry {plan.avg_entry} · avg fill {plan.avg_fill_price}")

    lines.append("")
    lines.append(f"🛑 <b>Stop: {plan.stop}</b> (-{plan.stop_distance_pct}%)")
    lines.append(f"❌ Invalidation: {_invalidation(plan)}")
    for index, (target, gross, net, away) in enumerate(
        zip(
            plan.targets,
            plan.rr_targets,
            plan.rr_targets_net,
            plan.target_distances_pct,
            strict=True,
        ),
        start=1,
    ):
        lines.append(f"🥅 TP{index}: <b>{target}</b> (+{away}%) — {net}R net ({gross}R gross)")

    lines.append("")
    lines.append(
        f"💶 Notional €{plan.notional_eur} ({plan.notional_usdt} USDT) · "
        f"Margin €{plan.margin_eur} · Leverage {plan.suggested_leverage}x (isolated)"
    )
    lines.extend(_cost_lines(plan))
    lines.append(
        f"{'✅' if plan.liq_buffer_ok else '⚠️'} Liq. buffer "
        f"{'OK' if plan.liq_buffer_ok else 'FAIL'} "
        f"(liq ≈ {plan.liq_distance_pct}% vs stop {plan.stop_distance_pct}%)"
    )
    lines.append(f"⚖️ Actual risk €{plan.risk_eur} (planned €{plan.planned_risk_eur})")
    lines.append(f"📋 {escape(plan.management_plan)}")
    lines.append(f"⏳ Expires if unfilled: {local_date_time(plan.expires_at, tz)}")

    if record.decision is not None:
        lines.append("")
        lines.append(f"<b>Your call: {DECISION_LABEL[record.decision]}</b>")

    lines.append("")
    lines.append(f"<i>{DISCLAIMER}</i>")
    return "\n".join(lines)


def _invalidation(plan: TradePlan) -> str:
    """§1's invalidation line. Missing price and text degrade separately."""
    report = plan.report
    text = escape(report.invalidation_text) if report.invalidation_text else "not stated"
    if report.invalidation_price is None:
        return text
    return f"{report.invalidation_price} — {text}"


def _cost_lines(plan: TradePlan) -> list[str]:
    """§4.2 — what the trade costs, shown before it is taken rather than after.

    ``round_trip_cost_eur`` is the engine's own total, not a sum taken here.
    Funding is always labelled an estimate, and an unavailable rate says so rather
    than rendering a reassuring €0.00 (CLAUDE.md: degrade explicitly).
    """
    costs = plan.costs
    lines = [
        f"🧾 Costs: round trip €{costs.round_trip_cost_eur} = "
        f"{costs.cost_pct_of_risk}% of the €{plan.planned_risk_eur} risk budget",
        f"    fees maker {costs.maker_fee_pct}% in · taker {costs.taker_fee_pct}% out",
    ]
    if not costs.funding_available:
        lines.append("    funding n/a — no funding rate in the snapshot")
    else:
        credit = " credit" if costs.funding_eur < 0 else ""
        lines.append(
            f"    funding ~€{abs(costs.funding_eur)}{credit} est · "
            f"{costs.funding_settlements} settlement(s) @ {costs.funding_interval_hours}h"
        )
    return lines


def rejection_card(decision: GateDecision, tz: ZoneInfo, moment: datetime) -> str:
    """A gate verdict with no plan attached — never posted unprompted, but the
    demo tool and ``/status`` both need to show one honestly."""
    reason = decision.reason.value if decision.reason is not None else "-"
    return "\n".join(
        [
            f"🚫 <b>{escape(decision.symbol)}</b> — {decision.status.value} [{reason}]",
            escape(decision.message),
            "",
            local_and_utc(moment, tz),
        ]
    )


# --------------------------------------------------------------------------- #
# Command output (§3)
#
# These render database state rather than a plan, but the same rule applies and
# the same test enforces it: no arithmetic. That is why ages appear as absolute
# timestamps — "14 minutes ago" is a subtraction, and one that would go stale
# between rendering and reading.
# --------------------------------------------------------------------------- #


def status_card(view: StatusView, tz: ZoneInfo) -> str:
    """§3 ``/status`` — pipeline health, from what the pipeline actually measures."""
    lines = ["🩺 <b>Status</b>", ""]

    if view.dry_run:
        lines.append(
            "⚠️ <b>DRY RUN</b> — the full cycle runs and <b>nothing is published</b>. "
            "Signals are stored and tracked silently; /stats reports them as their "
            "own population."
        )
        lines.append("")

    if view.paused:
        until = "no expiry" if view.paused_until is None else local_date_time(view.paused_until, tz)
        lines.append(f"⏸️ <b>PAUSED</b> — {view.pause_reason} · until: {until}")
        lines.append("/resume to lift it.")
    else:
        lines.append("▶️ Active — not paused.")

    lines.append("")
    lines.append("<b>Sizing</b>")
    lines.append(
        f"  capital: €{view.capital_eur}"
        if view.capital_eur is not None
        else "  capital: <b>not set</b> — every signal is rejected with NO_CAPITAL "
        "until /capital runs"
    )
    lines.append(f"  risk per trade: {view.risk_per_trade_pct}%")
    lines.append(f"  watchlist: {view.watchlist_size} symbols")

    lines.append("")
    lines.append("<b>Data</b>")
    if not view.data_sources:
        lines.append("  no snapshot stored yet — nothing has been ingested on this database.")
    else:
        for source in view.data_sources:
            mark = "✅" if source.quality == "OK" else "⚠️"
            degraded = f" ({', '.join(source.degraded_fields)})" if source.degraded_fields else ""
            lines.append(
                f"  {mark} {escape(source.symbol)} {source.quality}{degraded} · "
                f"{local_date_time(source.captured_at, tz)}"
            )

    lines.append("")
    lines.append("<b>Signals</b>")
    lines.append(
        f"  delivered: {view.signals_total} · awaiting your call: {view.signals_undecided}"
    )
    lines.append(f"  marked taken: {view.signals_taken}")
    lines.append(
        f"  open now: {view.signals_open} · today: {view.signals_today}/{view.max_signals_per_day}"
    )
    if view.stuck_messages:
        lines.append(
            f"  ⚠️ {view.stuck_messages} message(s) claimed but never confirmed — "
            "not re-sent, to avoid a double post (specs/TELEGRAM_UX.md §6)."
        )

    lines.append("")
    lines.append("<b>Risk in use</b>")
    lines.append(f"  open risk: {view.open_risk_pct}% of {view.max_open_risk_pct}%")
    lines.append(f"  positions: {view.open_positions} of {view.max_positions}")

    lines.append("")
    lines.append("<b>Cycle</b>")
    if view.last_cycle_at is None:
        lines.append("  no cycle has completed yet on this database.")
    else:
        lines.append(
            f"  last: {local_date_time(view.last_cycle_at, tz)} · {view.last_cycle_status}"
        )
        lines.append(f"  completed: {view.cycles_completed} of {view.cycles_started} in 30d")

    if view.spend is not None:
        spend = view.spend
        at_least = "at least " if spend.is_floor else ""
        lines.append("")
        lines.append("<b>LLM spend</b> <i>(estimate, not a bill)</i>")
        lines.append(f"  today: {at_least}${spend.day_usd} of ${spend.limit_usd} · {spend.state}")
        lines.append(f"  month to date: {at_least}${spend.month_usd}")
        if spend.is_floor:
            lines.append(
                f"  ⚠️ {spend.unpriced_calls} call(s) used a model with no price in "
                "config — their cost is missing from the figures above, not zero."
            )
        if spend.state == "LIMIT_REACHED":
            lines.append(
                "  ⛔ new deep analysis is suspended until 00:00 UTC. The screener "
                "and the tracker keep running."
            )
    return "\n".join(lines)


def alert_card(view: AlertView, tz: ZoneInfo) -> str:
    """An admin alert (M8) — the system talking about itself.

    Deliberately unlike every other card here: no buttons, no charts, no
    disclaimer, no numbers from a plan. Nothing about it should be mistakable for
    something to trade at three in the morning. The body lines are assembled in
    ``readmodels.alert_view`` and escaped here, because one of them is an
    exception message and exception messages contain angle brackets.
    """
    lines = [f"<b>{escape(view.title)}</b>", ""]
    lines.extend(escape(line) for line in view.body)
    if view.at is not None:
        lines.append("")
        lines.append(f"<i>since {local_and_utc(view.at, tz)}</i>")
    return "\n".join(lines)


def positions_card(positions: Sequence[PositionView], tz: ZoneInfo) -> str:
    """§3 ``/positions`` — Taken signals, marked to market.

    M6 shipped this showing the plan as issued and said in words that live uPnL
    needed the tracker. It has it now, and every figure below was computed in
    ``risk/accounting.py`` before it reached this function — §1's rule is
    unchanged, the bot still renders and never computes.
    """
    if not positions:
        return "📭 <b>Positions</b>\n\nNothing marked ✅ Taken yet."

    lines = ["📈 <b>Positions</b> (marked ✅ Taken)", ""]
    for position in positions:
        lines.append(
            f"<b>#{position.number} {escape(position.symbol)} "
            f"{position.direction.upper()}</b> · {escape(position.setup_type)} · "
            f"{position.status}"
        )
        lines.append(
            f"  entry {position.avg_entry} · stop {position.stop} · "
            f"risk €{position.risk_eur} · {position.leverage}x"
        )
        lines.append(f"  targets {' / '.join(position.targets)}")

        if position.mark_price is None:
            lines.append("  unfilled — the ladder is still resting")
        else:
            lines.append(f"  filled {position.filled_pct}% · mark {position.mark_price}")
            if position.unrealized_r is not None:
                lines.append(f"  open: {position.unrealized_r}R (€{position.unrealized_eur})")
            if position.realized_r is not None:
                taken = f" · {position.tp_hits} target(s) taken" if position.tp_hits else ""
                lines.append(f"  banked: {position.realized_r}R (€{position.realized_eur}){taken}")
            if position.stop_moved_to is not None:
                lines.append(f"  🛡️ stop moved to {position.stop_moved_to} (breakeven)")

        lines.append(f"  expires {local_date_time(position.expires_at, tz)}")
        lines.append("")

    lines.append(
        "<i>Open PnL is marked at the tracker's last observed price and is not a "
        "fill. Banked figures are realized and gross of costs.</i>"
    )
    return "\n".join(lines)


def tracker_update_card(view: TrackerEventView) -> str:
    """§4's threaded replies. One function, one line per event kind.

    Every number arrives already computed by the tracker, including the realized R
    — recomputing it here would be a second implementation of the math §8.5
    specifies, in the one module that is forbidden arithmetic.
    """
    kind = view.kind
    tag = f"<b>#{view.number} {escape(view.symbol)}</b>"

    if kind == "ENTRY_FILLED":
        rung = view.payload.get("rung", "?")
        weight = view.payload.get("weight_pct", "?")
        return f"📥 {tag} Entry {rung} filled @ {view.price} ({weight}% of risk)"

    if kind == "LADDER_COMPLETE":
        return f"📥 {tag} Ladder complete, avg {view.price}"

    if kind == "TP_HIT":
        target = view.payload.get("target", "?")
        breakeven = view.payload.get("breakeven", "")
        plan = view.payload.get("management", "")
        parts = [f"🎯 {tag} TP{target} hit @ {view.price} → {view.realized_r}R banked"]
        if breakeven:
            parts.append(f"🛡️ Stop moved to breakeven {breakeven} (per plan)")
        if plan:
            parts.append(f"📋 {escape(plan)}")
        return "\n".join(parts)

    if kind == "STOPPED":
        partial = view.payload.get("partial_ladder", "")
        note = f" (partial ladder: only rung(s) {partial} filled)" if partial else ""
        return f"🛑 {tag} Stopped @ {view.price} → {view.realized_r}R{note} · €{view.realized_eur}"

    if kind == "CLOSED_MANUALLY":
        return (
            f"🔚 {tag} Closed manually @ {view.price} → {view.realized_r}R · €{view.realized_eur}"
        )

    if kind == "INVALIDATED":
        close = view.payload.get("close", "")
        level = view.payload.get("level", "")
        return (
            f"❌ {tag} Invalidation triggered (close {close} vs {level}) before "
            "entry → signal cancelled"
        )

    if kind == "EXPIRED":
        return f"⌛ {tag} Expired unfilled"

    if kind == "NOTE":
        return f"✏️ {tag} {escape(view.detail)}"

    return f"📌 {tag} {escape(view.detail)}"  # pragma: no cover — every kind is above


def decision_ack_card(decision: SignalDecision, number: int, symbol: str) -> str:
    """The confirmation reply under a card when a decision button is pressed.

    Owner requirement (M7): the keyboard marker alone is easy to miss on a phone,
    and this is the input that decides whether an outcome lands in the real
    statistics or the hypothetical ones. One message per signal, edited when the
    decision changes, so a corrected mis-tap does not leave a stale claim sitting
    under the card.
    """
    tag = f"<b>#{number} {escape(symbol)}</b>"
    if decision is SignalDecision.TAKEN:
        return (
            f"✅ {tag} marked <b>Taken</b> — it counts toward your real stats and "
            "the open-risk budget. Fills, targets and the stop will be posted here."
        )
    if decision is SignalDecision.WATCHING:
        return (
            f"👀 {tag} marked <b>Watching</b> — tracked for hypothetical stats only. "
            "No risk budget is used, and the outcome will still be posted here."
        )
    return (
        f"❌ {tag} marked <b>Skipped</b> — archived. The outcome is still resolved "
        "in the background, so you can see what skipping cost or saved."
    )


def stats_card(view: StatsView, tz: ZoneInfo) -> str:
    """§3 ``/stats [30d|90d|all]`` — three populations, then the breakdowns."""
    since = "all time" if view.since is None else f"since {local_date_time(view.since, tz)}"
    lines = [f"📊 <b>Stats</b> ({view.window} · {since})", ""]

    for group in view.groups:
        lines.append(f"<b>{group.label}</b> <i>{group.note}</i>")
        if not group.measured:
            lines.append(
                f"  nothing measured yet — {group.count} signal(s), {group.unfilled} never filled."
            )
            lines.append("")
            continue
        scratch = f" / {group.scratches} scratch" if group.scratches else ""
        lines.append(
            f"  {group.filled} trade(s): {group.wins}W / {group.losses}L{scratch} · "
            f"win rate {group.win_rate_pct}%"
        )
        lines.append(f"  avg {group.avg_r}R · total {group.total_r}R (€{group.total_eur})")
        lines.append(f"  profit factor {group.profit_factor} · max DD {group.max_drawdown_r}R")
        lines.append(
            f"  reached TP1+: {group.reached_tp1}/{group.filled} ({group.reached_tp1_pct}%)"
        )
        if group.unfilled:
            lines.append(f"  never filled: {group.unfilled} (excluded above)")
        lines.append(f"  costs paid: €{group.costs_eur}")
        lines.append("")

    if view.by_setup:
        lines.append("<b>By setup type</b> <i>(taken + watched + skipped)</i>")
        for row in view.by_setup:
            lines.append(f"  {escape(row.key)}: {row.count} · {row.win_rate_pct}% · {row.avg_r}R")
        lines.append("")

    if view.by_prompt_version:
        lines.append("<b>By prompt version</b>")
        for row in view.by_prompt_version:
            lines.append(f"  {escape(row.key)}: {row.count} · {row.win_rate_pct}% · {row.avg_r}R")
        lines.append("")

    lines.append(f"<i>{DISCLAIMER}</i>")
    return "\n".join(lines).rstrip()


def settings_card(view: SettingsView) -> str:
    """§3 ``/settings`` — the runtime values, each tagged with where it came from.

    Not the whole of ``AppConfig``: that is thousands of characters, most of them
    endpoint URLs, and Telegram caps a message at 4096. What is here is what
    shapes a signal — the specs/RISK_ENGINE.md §1 table — plus what the owner can
    change from this chat.
    """
    lines = ["⚙️ <b>Settings</b>", "<i>db = set from Telegram · yaml = config.yaml</i>", ""]
    for group, rows in view.groups:
        lines.append(f"<b>{group}</b>")
        for label, value, source in rows:
            lines.append(f"  {label}: {value} <i>[{source}]</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def watchlist_card(symbols: Sequence[str], source: str) -> str:
    """§3 ``/watchlist`` — the symbols the scan cycle will cover."""
    lines = [f"👁️ <b>Watchlist</b> ({len(symbols)} symbols) <i>[{source}]</i>", ""]
    lines.extend(f"  · {escape(symbol)}" for symbol in symbols)
    lines.append("")
    lines.append("<i>/watchlist add SOLUSDT · /watchlist remove SOLUSDT</i>")
    return "\n".join(lines)


__all__ = [
    "DECISION_LABEL",
    "decision_ack_card",
    "positions_card",
    "rejection_card",
    "settings_card",
    "signal_card",
    "stats_card",
    "status_card",
    "tracker_update_card",
    "watchlist_card",
]

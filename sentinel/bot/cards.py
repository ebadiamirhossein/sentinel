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
from decimal import Decimal
from zoneinfo import ZoneInfo

from sentinel.analyst.models import Direction
from sentinel.bot.formatting import DISCLAIMER, escape, local_and_utc, local_date_time
from sentinel.bot.models import (
    SignalDecision,
    SignalRecord,
    UserAccount,
    UserStatus,
    WatchlistRequest,
)
from sentinel.bot.views import (
    AlertView,
    PositionView,
    PulseDayView,
    PulseView,
    SettingsView,
    StatsView,
    StatusView,
    TrackerEventView,
    UserView,
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
        scope = f" [{view.pause_scope}]" if view.pause_scope else ""
        lines.append(f"⏸️ <b>PAUSED</b>{scope} — {view.pause_reason} · until: {until}")
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


def _pulse_truncation(view: PulseView | PulseDayView) -> list[str]:
    """What each section had to leave out. Never a silent cut."""
    return [
        f"<i>+{dropped} more under “{section}”, not shown.</i>"
        for section, dropped in view.truncated
    ]


def pulse_card(view: PulseView, tz: ZoneInfo) -> str:
    """``/pulse`` — the last completed cycle, told the same way to everybody (M8.4).

    Every approved user gets this identical text; only the spend line differs, and it
    differs because ``view.spend`` is ``None`` for a member rather than because this
    function checks a role. See :class:`~sentinel.bot.views.PulseView`.

    Nothing here is counted or summed: ``bot/pulse.py`` hands over finished rows and
    finished totals, exactly as ``TradePlan`` does for a signal card (§1).
    """
    if view.at is None:
        return (
            "📡 <b>Pulse</b>\n\n"
            "No cycle has completed yet on this database. The first scan runs one "
            "scan interval after start-up, not immediately."
        )

    lines = ["📡 <b>Pulse</b> — last cycle", f"{local_and_utc(view.at, tz)} · {view.status}"]
    if view.status == "FAILED":
        lines.append("⚠️ This cycle failed part-way. What follows is how far it got.")
        if view.error:
            lines.append(f"  {escape(view.error)}")
    if view.dry_run:
        lines.append("⚠️ <b>DRY RUN</b> — the cycle runs in full and nothing is published.")
    if view.suspended_reason:
        lines.append(f"⛔ Deep analysis held back: {escape(view.suspended_reason)}")

    lines.append("")
    if view.screener_silent:
        lines.append(
            "🔎 <b>Screener</b> — no usable verdict recorded. Nothing was escalated, "
            "and that is a degraded cycle rather than a quiet market."
        )
    else:
        lines.append(f"🔎 <b>Screener</b> — {len(view.escalated)} of {view.screened} escalated")
        for row in view.escalated:
            lines.append(f"  {escape(row.symbol)} ({row.direction_hint}) — {escape(row.reason)}")
        if not view.escalated:
            lines.append("  Nothing looked worth paying to analyse. This is the normal answer.")

    if view.skipped:
        lines.append("")
        lines.append("⏭️ <b>Not analysed</b>")
        for skip in view.skipped:
            lines.append(f"  {escape(skip.symbol)} — {escape(skip.wording)}")

    if view.verdicts:
        lines.append("")
        lines.append("🧠 <b>Analyst</b>")
        for verdict in view.verdicts:
            lines.append(
                f"  {escape(verdict.symbol)} — <b>{verdict.status}</b> · "
                f"conf {verdict.confidence} · {escape(verdict.setup_type)} "
                f"{verdict.direction}"
            )
            lines.append(f"    <i>{escape(verdict.thesis)}</i>")

    if view.gate:
        lines.append("")
        lines.append("🚦 <b>Gate</b>")
        for decision in view.gate:
            mark = "✅ " if decision.approved else ""
            code = "" if decision.approved or not decision.code else f"<b>{decision.code}</b> — "
            lines.append(f"  {escape(decision.symbol)} — {mark}{code}{escape(decision.wording)}")

    if view.spend is not None and view.spend_usd is not None:
        at_least = "at least " if view.spend.is_floor else ""
        lines.append("")
        lines.append(
            f"💵 <b>~${view.spend_usd} this cycle</b> · {at_least}${view.spend.day_usd} "
            f"today of ${view.spend.limit_usd} <i>(estimate, not a bill)</i>"
        )

    lines.extend(_pulse_truncation(view))
    lines.append("")
    lines.append("<i>The pipeline's reasoning, the same for everyone. /pulse 24h for the day.</i>")
    return "\n".join(lines)


def pulse_day_card(view: PulseDayView, tz: ZoneInfo) -> str:
    """``/pulse 24h`` — the same four sections, aggregated."""
    lines = [
        "📡 <b>Pulse</b> — last 24h",
        f"since {local_and_utc(view.since, tz)}",
        f"{view.cycles_completed} of {view.cycles_started} cycles completed",
    ]
    if view.dry_run:
        lines.append("⚠️ <b>DRY RUN</b> — nothing in this window was published.")

    if not view.cycles_completed:
        lines.append("")
        lines.append("No cycle completed in the last 24 hours.")
        return "\n".join(lines)

    lines.append("")
    lines.append("🔎 <b>Escalated</b>")
    lines.append(_counted(view.escalations, "  nothing was escalated"))
    lines.append("")
    lines.append("🧠 <b>Analyst verdicts</b>")
    lines.append(_counted(view.verdicts, "  nothing reached the analyst"))
    lines.append("")
    lines.append("⏭️ <b>Not analysed</b>")
    lines.append(_counted(view.skips, "  nothing was held back"))
    lines.append("")
    lines.append("🚦 <b>Gate</b>")
    lines.append(_counted(view.gate, "  nothing reached the gate"))

    if view.spend is not None:
        at_least = "at least " if view.spend.is_floor else ""
        lines.append("")
        lines.append(
            f"💵 <b>Spend</b> — {at_least}${view.spend.day_usd} today of "
            f"${view.spend.limit_usd} <i>(estimate, not a bill)</i>"
        )

    lines.extend(_pulse_truncation(view))
    lines.append("")
    lines.append(
        "<i>The pipeline's reasoning, the same for everyone. /pulse for the last cycle.</i>"
    )
    return "\n".join(lines)


def _counted(rows: Sequence[tuple[str, int]], empty: str) -> str:
    """``SOLUSDT x4 · LINKUSDT x3`` — one line, because a phone has one column."""
    if not rows:
        return empty
    counted = " · ".join(f"{escape(key)} x{count}" for key, count in rows)
    return f"  {counted}"


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


# --------------------------------------------------------------------------- #
# M8.1 — /help, and the messages a multi-user bot needs
# --------------------------------------------------------------------------- #

#: ``/help``, written for somebody who does not read the specs (M8.1). Every term a
#: card uses is defined here in plain words, in the order a card presents them, and
#: the two things that must never be misunderstood — that nothing is traded
#: automatically, and what Taken actually commits — are said first and last.
#:
#: A tuple joined at render time rather than one long string, because this module is
#: forbidden arithmetic and that includes ``+`` between strings.
HELP_LINES = (
    "🧭 <b>How this works</b>",
    "",
    "This system watches the market around the clock, analyses setups with an AI, "
    "and sends you a complete trade plan. <b>It never trades.</b> There is no "
    "connection to any exchange account and no order is ever placed for you — "
    "<b>you place every order yourself</b>, by hand, on your own exchange.",
    "",
    "<b>Capital</b> — <code>/capital 10000</code>",
    "The total money you are trading with, in euros. It is used only to work out "
    "position sizes. The bot never sees your exchange balance, and you can change "
    "this any time; open signals keep the sizing they were issued with.",
    "",
    "<b>Risk %</b> — <code>/risk 0.75</code>",
    "The most one trade can lose you if the whole entry ladder fills and the stop "
    "is hit. At 0.75% of €10,000 that is €75 — so €75 is what a card calls "
    "<i>1R</i>, and every reward figure is measured in those units. Allowed range "
    "is 0.25% to 1.5%.",
    "",
    "<b>Leverage</b>",
    "You never choose it and it is not a bet size. It is worked out from your "
    "margin budget, capped, and then reduced further until liquidation sits at "
    "least twice as far away as your stop — so a planned stop-out can never "
    "liquidate the position. <b>Leverage changes how much margin you post, not how "
    "much you can lose.</b> Your loss is fixed by the stop and your risk %.",
    "",
    "<b>Reading a card</b>",
    "· <b>Entry ladder</b> — one to three limit orders. “40% of risk” means that "
    "rung carries 40% of your €75, not 40% of the money. That is what makes a "
    "partial fill honest: if only the first rung fills and the stop is hit, you "
    "lose about 0.4R, and the card can promise that.",
    "· <b>Stop</b> — where the idea is wrong. Place it with the entries.",
    "· <b>Invalidation</b> — the idea failing before you are even in. It is a "
    "<i>close</i>, not a wick: a spike through it is noise, a candle closing "
    "through it is not.",
    "· <b>Targets</b> — TP1/TP2/TP3, with the management plan under them (take "
    "some off at TP1, move the stop to breakeven, and so on).",
    "· <b>net vs gross R</b> — gross is the raw reward-to-risk. <b>Net is after "
    "fees and estimated funding, and net is what you actually collect.</b> The "
    "cost line shows the round trip in euros and as a share of your risk budget. "
    "Signals are approved on the net figure, never the flattering one.",
    "",
    "<b>Taken / Watching / Skip</b>",
    "Three buttons under every card, and the distinction is the point of the whole system:",
    "· <b>✅ Taken</b> — you placed it. It counts in your <b>real</b> statistics "
    "and spends part of your open-risk budget.",
    "· <b>👀 Watching</b> — you did not place it, but you want to see how it went. "
    "Tracked and measured as <b>hypothetical</b>. No budget used.",
    "· <b>❌ Skip</b> — not for you. Still followed to the end, still measured as "
    "hypothetical, because what skipping cost or saved is worth knowing.",
    "",
    "Every outcome is resolved automatically either way. Answering honestly is "
    "what makes the measured win rate <i>yours</i> rather than a backtest — and you "
    "can change your answer at any time; the buttons stay live.",
    "",
    "<b>Getting started</b>",
    "Set <code>/capital</code>, then <code>/risk</code> if you want something "
    "other than the default, then wait. Quiet is normal: the system prefers saying "
    "nothing to sending a weak setup. Use <code>/stats</code> to see your own "
    "numbers and <code>/positions</code> for what is open.",
    "",
    "<b>Quiet day?</b> — <code>/pulse</code>",
    "What the last cycle actually did: what was escalated and why, what the "
    "analyst concluded, what the gate decided. It is the pipeline's reasoning, "
    "identical for everyone — no capital, sizing or decisions, yours or anyone "
    "else's. <code>/pulse 24h</code> for the whole day. Use it to tell a quiet "
    "market from a system that has stopped.",
    "",
    "This is experimental software and its win rate is not yet measured. You can lose money.",
    "",
    f"<i>{DISCLAIMER}</i>",
)

#: The first-run note (specs/TELEGRAM_UX.md §7). Deliberately short, deliberately
#: unflattering, and deliberately not a wall of legalese nobody reads.
ACKNOWLEDGEMENT_LINES = (
    "⚠️ <b>Before you get any signals — please read this.</b>",
    "",
    "· This system is <b>experimental</b>. Its win rate has <b>not been measured "
    "yet</b>; there is no track record to rely on.",
    "· What it sends is <b>research, not advice</b>. Nobody here is a licensed financial adviser.",
    "· <b>It never trades.</b> You place every order yourself, on your own "
    "exchange, with your own money.",
    "· <b>You can lose money</b> — including on signals the system was confident about.",
    "",
    "Tap below to confirm you have read this. Use /help for what the numbers on a "
    "card mean, and /leave at any time to stop receiving anything.",
)


def help_card() -> str:
    """§3 ``/help`` — plain language, no jargon left undefined."""
    return "\n".join(HELP_LINES)


def acknowledgement_card() -> str:
    """§7's first-run note. Nothing is delivered until it is acknowledged."""
    return "\n".join(ACKNOWLEDGEMENT_LINES)


def _who(account: UserAccount) -> str:
    """How a user is identified to the owner: @username, name, or the bare id.

    Escaped, because both a username and a display name are text the *user* chose
    and this bot renders HTML.
    """
    if account.username:
        return f"@{escape(account.username)}"
    if account.display_name:
        return escape(account.display_name)
    return f"id {account.telegram_user_id}"


def standing_card(account: UserAccount) -> str:
    """Where a non-approved caller stands (§7). Clear, and never silence.

    Silence for someone who has asked politely reads as a broken bot and produces
    another ``/start`` a minute later. Each state says what it is and what, if
    anything, the person can do — and none of them says who the owner is.
    """
    if account.status is UserStatus.PENDING:
        return (
            "⏳ <b>Your request is waiting.</b>\n"
            "The owner has been asked to approve it. You will get a message here "
            "either way — there is nothing else to do, and asking again will not "
            "make it faster."
        )
    if account.status is UserStatus.REJECTED:
        return "🚫 <b>This request was declined.</b>\nNothing will be sent to this chat."
    if account.status is UserStatus.SUSPENDED:
        return (
            "⏸️ <b>Your access is suspended.</b>\n"
            "Signals have stopped. Anything already open is still being tracked, "
            "and your history is intact."
        )
    if account.status is UserStatus.LEFT:
        return (
            "👋 <b>You left.</b>\n"
            "Nothing is being sent to this chat. Your past signals and statistics "
            "were kept, not deleted — if you want back in, ask the owner to "
            "re-approve you."
        )
    return (  # pragma: no cover — APPROVED never reaches a standing notice
        "✅ You are set up. Use /help to see what the cards mean."
    )


def welcome_card(account: UserAccount) -> str:
    """Sent on approval, above the acknowledgement note."""
    return (
        f"✅ <b>You're in.</b> Welcome, {_who(account)}.\n\nOne thing first, and then two settings:"
    )


def ready_card(*, capital_set: bool) -> str:
    """Sent once the note is acknowledged — what is still missing, if anything."""
    if capital_set:
        return (
            "🎉 <b>All set.</b> Signals will arrive here when the system finds "
            "something worth sending. Quiet is normal.\n"
            "/help · /stats · /positions"
        )
    return (
        "🎉 <b>Thank you.</b> One thing left: <b>set your capital</b>, because "
        "every position size is worked out from it.\n\n"
        "<code>/capital 10000</code>\n\n"
        "Until then no signals can be sized for you, so none will be sent. "
        "/help explains what capital and risk % actually mean."
    )


def no_capital_card() -> str:
    """Why a card that was approved for this user was not delivered to them."""
    return (
        "📭 <b>A signal was approved, and you did not get it.</b>\n"
        "Your capital is not set, so there is no way to work out a position size — "
        "sizing against a number you never chose would be worse than sending "
        "nothing.\n\n"
        "<code>/capital 10000</code>\n\n"
        "This message is sent at most once a day."
    )


def loss_pause_card(*, limit_pct: Decimal, at: datetime, tz: ZoneInfo) -> str:
    """specs/TELEGRAM_UX.md §4's daily-loss notice, delivered at last (M8.1).

    §4 has listed this line since M6 and the tracker has been raising the pause since
    M7, but nothing ever sent it — the same shape of gap M8 found in the spend guard.
    It matters more now: the pause is per user, so without this message a member's
    signals simply stop, and they have no ``/status`` to ask why.

    The percentage is the configured limit, not a measured loss: the figure a card
    shows must come from something computed, and the day's realized loss is not on
    this message's inputs. Saying which rail fired is the useful part anyway.
    """
    return (
        "⏸️ <b>Daily loss limit reached.</b>\n"
        f"Your realized loss today has reached the {limit_pct}% limit, so no new "
        "signals will be sized for you for the next 24 hours.\n"
        "Anything already open is still being tracked and you will still get its "
        "updates.\n"
        f"<i>{local_and_utc(at, tz)}</i>"
    )


def left_card() -> str:
    """Confirmation that a member has removed themselves."""
    return (
        "👋 <b>Done — you have been removed.</b>\n"
        "No further signals, updates or messages will be sent to this chat. Your "
        "past signals and statistics were kept rather than deleted; nobody can see "
        "your capital or your decisions.\n"
        "If you change your mind, ask the owner to re-approve you."
    )


def leave_confirm_card() -> str:
    """``/leave``'s confirmation prompt."""
    return (
        "🚪 <b>Leave Sentinel?</b>\n"
        "You will stop receiving signals and tracker updates immediately. Anything "
        "you have open will no longer be reported here.\n"
        "Your history is kept, not deleted."
    )


def registration_request_card(account: UserAccount, tz: ZoneInfo) -> str:
    """The request that reaches the owner, with Approve/Reject below it."""
    return (
        "🙋 <b>Access request</b>\n"
        f"{_who(account)}\n"
        f"user id: <code>{account.telegram_user_id}</code>\n"
        f"asked: {local_and_utc(account.requested_at, tz)}\n\n"
        "Approving lets them set their own capital and receive their own sized "
        "cards from the same analysis. They never see your numbers, and you never "
        "see theirs."
    )


def watchlist_request_card(
    request: WatchlistRequest, account: UserAccount | None, tz: ZoneInfo, *, size: int, cap: int
) -> str:
    """The request that reaches the owner, with Approve/Decline below it (M8.3).

    Carries who asked and what for, and — because approving costs money every cycle
    from now on rather than once — where the watchlist stands against its cap.
    """
    who = "a member" if account is None else _who(account)
    return (
        "👁️ <b>Watchlist request</b>\n"
        f"symbol: <code>{escape(request.symbol)}</code>\n"
        f"asked by: {who}\n"
        f"asked: {local_and_utc(request.requested_at, tz)}\n"
        f"watchlist: {size} of {cap}\n\n"
        "Approving adds it to the shared watchlist, so it is screened every cycle "
        "and may buy a deep analysis. Declining tells them, and needs no reason."
    )


def watchlist_request_ack_card(symbol: str) -> str:
    """What the requester sees when their ask is stored."""
    return (
        f"👁️ Asked for <code>{escape(symbol)}</code>.\n\n"
        "The owner decides what the watchlist covers, since the analysis is shared "
        "and runs on their budget. You will hear either way."
    )


def watchlist_request_decided_card(symbol: str, *, approved: bool) -> str:
    """What the requester sees once the owner has answered."""
    if approved:
        return (
            f"✅ <code>{escape(symbol)}</code> was added to the watchlist.\n\n"
            "It is screened from the next cycle. A signal only follows if the "
            "analysis and the risk gate both agree — being watched is not a setup."
        )
    return f"❌ <code>{escape(symbol)}</code> was not added to the watchlist."


def watchlist_full_card(symbol: str, *, cap: int, requester: bool) -> str:
    """The watchlist filled up between the request and the approval (M8.3).

    Deliberately **not** a rejection, and both people are told so. Nobody decided
    against this symbol — the list simply ran out of room while the request was
    waiting — so the row stays PENDING and the owner can approve it after removing
    something. Turning it into a rejection would make the member ask again for a
    thing the owner had just tried to give them.
    """
    if requester:
        return (
            f"⏳ <code>{escape(symbol)}</code> is still waiting.\n\n"
            f"The watchlist filled up ({cap} of {cap}) before it could be added. "
            "Your request has not been declined — it stays in the queue."
        )
    return (
        f"⏳ <code>{escape(symbol)}</code> was <b>not</b> added: the watchlist is "
        f"full ({cap} of {cap}).\n\n"
        "The request is still <b>pending</b>, not declined. Remove a symbol with "
        f"/watchlist remove, or raise <code>watchlist_max_symbols</code>, then "
        "approve it again."
    )


def users_card(views: Sequence[UserView], tz: ZoneInfo) -> str:
    """§7 ``/users`` — enough to operate the system, and nothing more.

    **The boundary is deliberate and it is the whole design of this card.** It shows
    standing, when someone joined, whether they have set a capital at all (yes or
    no), and whether a daily-loss pause is currently holding them. It does *not*
    show the amount, the risk %, the P&L, the win rate, or a single decision.

    Running a system for friends requires knowing who is set up and who is stuck.
    It does not require watching them trade, and a card that showed both would make
    the second one happen by accident every time the first was needed.
    """
    lines = [f"👥 <b>Users</b> ({len(views)})", ""]
    for view in views:
        flags = []
        if not view.capital_set:
            flags.append("no capital set")
        if view.loss_paused:
            flags.append("loss-paused")
        suffix = f" <i>· {' · '.join(flags)}</i>" if flags else ""
        lines.append(f"<b>{view.label}</b> — {view.status}{suffix}")
        lines.append(f"  <code>{view.user_id}</code> · {view.role.lower()}")
        if view.since is not None:
            lines.append(f"  {view.since_label} {local_date_time(view.since, tz)}")
        lines.append("")
    lines.append("<i>/approve &lt;id&gt; · /reject &lt;id&gt; · /suspend &lt;id&gt;</i>")
    lines.append(
        "<i>Capital amounts, decisions and P&amp;L are each user's own and are not shown here.</i>"
    )
    return "\n".join(lines)


__all__ = [
    "ACKNOWLEDGEMENT_LINES",
    "DECISION_LABEL",
    "HELP_LINES",
    "acknowledgement_card",
    "decision_ack_card",
    "help_card",
    "leave_confirm_card",
    "left_card",
    "loss_pause_card",
    "no_capital_card",
    "positions_card",
    "pulse_card",
    "pulse_day_card",
    "ready_card",
    "registration_request_card",
    "rejection_card",
    "settings_card",
    "signal_card",
    "standing_card",
    "stats_card",
    "status_card",
    "tracker_update_card",
    "users_card",
    "watchlist_card",
    "watchlist_full_card",
    "watchlist_request_ack_card",
    "watchlist_request_card",
    "watchlist_request_decided_card",
    "welcome_card",
]

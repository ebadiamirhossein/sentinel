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
from sentinel.bot.views import SettingsView, StatusView
from sentinel.risk.models import GateDecision, TradePlan
from sentinel.storage.models import SignalRow

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
    """§3 ``/status`` — pipeline health, honestly scoped to what M6 can know."""
    lines = ["🩺 <b>Status</b>", ""]

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
    if view.stuck_messages:
        lines.append(
            f"  ⚠️ {view.stuck_messages} message(s) claimed but never confirmed — "
            "not re-sent, to avoid a double post (specs/TELEGRAM_UX.md §6)."
        )

    lines.append("")
    lines.append(
        "<i>Cycle timing, open-risk usage and live position tracking arrive with the "
        "orchestrator and tracker at M7. Nothing above is estimated: what is not "
        "measured yet is not shown.</i>"
    )
    return "\n".join(lines)


def positions_card(signals: Sequence[SignalRow], tz: ZoneInfo) -> str:
    """§3 ``/positions`` — every signal marked Taken, with its plan as issued.

    The spec asks for live uPnL in R and EUR. That needs a mark price and the
    ladder-aware R accounting in ``risk/accounting.py``, which the tracker runs
    from M7; computing it here would put arithmetic in the renderer and duplicate
    math that already exists. So the plan's own numbers are shown and the gap is
    stated rather than filled with a zero.
    """
    if not signals:
        return "📭 <b>Positions</b>\n\nNothing marked ✅ Taken yet."

    lines = ["📈 <b>Positions</b> (marked ✅ Taken)", ""]
    for row in signals:
        plan = row.plan
        lines.append(
            f"<b>#{row.number} {escape(row.symbol)} {row.direction.upper()}</b> · "
            f"{escape(row.setup_type)} · {row.status}"
        )
        lines.append(
            f"  entry {plan['avg_fill_price']} · stop {plan['stop']} · "
            f"risk €{plan['risk_eur']} · {plan['suggested_leverage']}x"
        )
        lines.append(f"  targets {' / '.join(str(t) for t in plan['targets'])}")
        lines.append(f"  expires {local_date_time(row.expires_at, tz)}")
        lines.append("")

    lines.append(
        "<i>Live unrealised PnL in R and EUR needs the price-watch loop and the "
        "ladder-aware R accounting that land with the tracker at M7. The figures "
        "above are the plan as issued, not a mark-to-market.</i>"
    )
    return "\n".join(lines)


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
    "positions_card",
    "rejection_card",
    "settings_card",
    "signal_card",
    "status_card",
    "watchlist_card",
]

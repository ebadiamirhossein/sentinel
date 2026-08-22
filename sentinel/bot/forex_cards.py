"""The forex signal card (docs/specs/FOREX.md §16.8, specs/TELEGRAM_UX.md §1).

**A separate module, not a branch inside ``cards.py``.** Two reasons, and both are
about the milestone's binding constraint. The crypto renderer's diff stays empty, so
"crypto output did not change" is a fact about a file rather than a claim about a
conditional. And this module joins ``RENDERING_MODULES`` in
``tests/bot/test_no_arithmetic.py``, so the AST scan and the every-number-came-from-the-
plan check cover it from its first line rather than from whenever somebody remembers to
add it.

**The bot renders; it never computes** — the same rule, for the same reason. If this
card needs a number :class:`~sentinel.fx.plan.ForexPlan` lacks, the fix is a field on
the plan, computed and tested in ``sentinel/fx/``. It is never a subtraction here.

What this card carries that a crypto card has no room for:

* **pips beside percentages, and what a pip is worth.** Forex is discussed in pips,
  and "-0.15%" on a 1.1690 quote is a number nobody converts in their head at 3am.
  ``pip_value_eur`` is the number that turns a pip count into money, and it is the
  single most useful figure on the card — a 35-pip stop means nothing until it is
  €75. It also differs per instrument, which is exactly why §4.2's pip rule is
  cross-checked against ``TickSize x 10`` rather than assumed;
* **the measured spread and where it came from.** §7.3 made the spread a measurement
  rather than an assumption, and ``spread_basis`` travels with the figure so a reader
  can tell "this hour's median over 1,180 samples" from "UNMEASURED";
* **rollover, never funding.** §2's ledger replaces one with the other and they do not
  behave alike;
* **the ESMA words with the leverage.** §7.6: there is no ``MarginRates`` to read, so
  30:1 is a documented assumption and the number never appears without it;
* **the account-level stop-out sentence.** §7.6 again — the crypto card's liquidation
  buffer has no forex analogue, and its absence must read as a *different* fact rather
  than as a missing line;
* **the weekend gap warning** (§5.4), and **why the ladder expires when it does**,
  because §5.4 gives it two possible deadlines and one timestamp cannot say which.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from sentinel.analyst.models import Direction
from sentinel.bot.cards import DECISION_LABEL
from sentinel.bot.formatting import (
    DISCLAIMER,
    escape,
    local_and_utc,
    local_date_time,
    money_eur,
    percent_2dp,
)
from sentinel.bot.models import SignalRecord
from sentinel.fx.plan import ForexPlan

#: §7.6, in one sentence, on every card. Not a footnote: it is the difference between
#: this market and the one the owner has been trading for 54 cycles, and the crypto
#: card's "Liq. buffer OK" line is exactly what a reader would otherwise look for and
#: silently assume.
ACCOUNT_LEVEL_MARGIN = (
    "🏦 Stop-out risk here is <b>account-level</b>, not per position — there is no "
    "liquidation price for this trade."
)

WEEKEND_GAP = (
    "⚠️ <b>Weekend gap risk:</b> this idea can still be open when the market shuts on "
    "Friday. Price can gap over the break and reopen beyond the stop."
)


def _side(direction: Direction) -> str:
    return "🟢 LONG" if direction is Direction.LONG else "🔴 SHORT"


def forex_signal_card(
    record: SignalRecord, tz: ZoneInfo, *, show_market: bool = False, shared_only: bool = False
) -> str:
    """One forex signal, as the owner reads it.

    ``show_market`` follows ``AppConfig.multi_market`` exactly as the crypto card does.
    It defaults to **off** so that a caller which forgets cannot tag a card in a
    single-market deployment — the same default, and the same reasoning, as
    ``cards.signal_card``.

    ``shared_only`` (M11p) is the same subtraction ``cards.signal_card`` documents, over
    this market's own per-user figures: notional, margin, the euro pip value, the
    leverage line and the cost block all scale with one user's capital. Two lines that
    look like they belong to that block are **kept**, because they are facts about the
    market rather than about the reader: :data:`ACCOUNT_LEVEL_MARGIN` says this venue has
    no per-position liquidation price, and :data:`WEEKEND_GAP` says price can jump the
    Friday close. Both are safety statements a Persian summary should be able to repeat.
    """
    plan = record.plan
    if not isinstance(plan, ForexPlan):  # pragma: no cover — the publisher dispatches
        raise TypeError(
            f"forex_signal_card was handed a {type(plan).__name__}. Plans are dispatched "
            f"on SignalRecord.market; see FOREX.md §16.7 on why a fallback is forbidden."
        )
    report = plan.report
    tag = f"{record.market.value.upper()} · " if show_market else ""
    base = plan.instrument.base_currency

    lines = [
        f"{_side(plan.direction)} — {tag}{escape(plan.symbol)}   "
        f"[{plan.setup_type.value} · {plan.timeframe_label.value} · conf {plan.confidence}]",
        f"{escape(report.prompt_version or 'prompt n/a')} · "
        f"{'' if shared_only else f'Signal #{record.number} · '}"
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
        "🎯 <b>Plan</b>"
        if shared_only
        else f"🎯 <b>Plan</b> (capital €{money_eur(plan.capital_eur)} · "
        f"risk {percent_2dp(plan.risk_per_trade_pct)}% "
        f"= €{plan.planned_risk_eur} · EUR{plan.quote_currency} {plan.eur_quote_rate})"
    )
    lines.append(f"Entry ladder (limit orders) — last price {plan.last_price}:")
    for index, entry in enumerate(plan.entries, start=1):
        # A separate f-string rather than a ``+``, for the reason ``cards.py`` records
        # at the same place: the no-arithmetic scan reads ``ast.Add`` and nothing else.
        tail = "" if shared_only else f" — {entry.qty} {escape(base)} (€{entry.notional_eur})"
        lines.append(
            f"  {index}) <b>{entry.price}</b> ({entry.distance_pct}% · "
            f"{entry.distance_pips} pips) — {entry.weight_pct}% of risk{tail}"
        )
    lines.append(f"  Weighted entry {plan.avg_entry} · avg fill {plan.avg_fill_price}")

    lines.append("")
    lines.append(
        f"🛑 <b>Stop: {plan.stop}</b> (-{plan.stop_distance_pct}% · {plan.stop_distance_pips} pips)"
    )
    lines.append(f"❌ Invalidation: {_invalidation(plan)}")
    for index, (target, gross, net, away, pips) in enumerate(
        zip(
            plan.targets,
            plan.rr_targets,
            plan.rr_targets_net,
            plan.target_distances_pct,
            plan.target_distances_pips,
            strict=True,
        ),
        start=1,
    ):
        lines.append(
            f"🥅 TP{index}: <b>{target}</b> (+{away}% · {pips} pips) — {net}R net ({gross}R gross)"
        )

    lines.append("")
    if shared_only:
        lines.append(f"📐 pip = {plan.pip} ({plan.quote_currency})")
    else:
        lines.append(
            f"💶 Notional €{plan.notional_eur} "
            f"({plan.notional_quote} {plan.quote_currency}) · Margin €{plan.margin_eur} "
            f"({plan.margin_pct_of_equity}% of equity)"
        )
        lines.append(
            f"📐 Pip value €{plan.pip_value_eur} per pip · pip = {plan.pip} ({plan.quote_currency})"
        )
        lines.append(f"⚙️ Max leverage {plan.max_leverage}x — {escape(plan.leverage_basis)}")
        lines.extend(_cost_lines(plan))
    lines.append(ACCOUNT_LEVEL_MARGIN)
    if not shared_only:
        lines.append(f"⚖️ Actual risk €{plan.risk_eur} (planned €{plan.planned_risk_eur})")
    lines.append(f"📋 {escape(plan.management_plan)}")
    lines.append(
        f"⏳ Expires if unfilled: {local_date_time(plan.expires_at, tz)} — "
        f"{escape(plan.expiry_basis)}"
    )
    if plan.weekend_gap_warning:
        lines.append(WEEKEND_GAP)

    if record.decision is not None and not shared_only:
        lines.append("")
        lines.append(f"<b>Your call: {DECISION_LABEL[record.decision]}</b>")

    lines.append("")
    lines.append(f"<i>{DISCLAIMER}</i>")
    return "\n".join(lines)


def _invalidation(plan: ForexPlan) -> str:
    """Missing price and missing text degrade separately, as on the crypto card."""
    report = plan.report
    text = escape(report.invalidation_text) if report.invalidation_text else "not stated"
    if report.invalidation_price is None:
        return text
    return f"{report.invalidation_price} — {text}"


def _cost_lines(plan: ForexPlan) -> list[str]:
    """§7.3 and §7.4 — what the round trip costs, before it is taken rather than after.

    The spread is the headline because it is the one this market actually charges, and
    it is quoted **with its basis**: a median for this hour of the day is a different
    claim from a single current reading, and "UNMEASURED" is a third. §7.4's correction
    means the spread lands on both sides of the trade, which is why the net RR figures
    above are so much lower than a naive subtraction would suggest.

    Rollover, never funding. §2's ledger replaces one with the other and they do not
    behave alike — rollover is charged at 21:00 UTC and **tripled on Wednesday**.
    """
    costs = plan.costs
    lines = [
        # ``total_eur`` carries four decimals on purpose — ``fx/rounding.cost_money``
        # keeps them because at €200 cents-rounding a spread moves net RR by 0.01R,
        # and that precision is what fed the gate. The CARD is a different job: a
        # reader needs a cost in money, and "€2.3375" reads as a defect.
        f"🧾 Costs: round trip €{money_eur(costs.total_eur)} = "
        f"{costs.cost_pct_of_risk}% of the €{plan.planned_risk_eur} risk budget",
        f"    spread {costs.spread_pips} pips = €{money_eur(costs.spread_cost_eur)} "
        f"({escape(costs.spread_basis)})",
    ]
    if costs.entry_commission_eur > 0 or costs.exit_commission_eur > 0:
        lines.append(
            f"    commission €{costs.entry_commission_eur} in · €{costs.exit_commission_eur} out"
        )
    if costs.rollover_nights == 0:
        lines.append("    rollover none — this idea does not cross 21:00 UTC")
    elif costs.rollover_eur == 0:
        # CLAUDE.md: degrade explicitly, never fabricate. A configured zero and a
        # measured zero read identically as "€0.00", and only one of them means the
        # trade is free to hold. The swap table ships empty because swap rates are
        # published by the broker rather than derivable from a chart (§7.3), so a
        # plausible-looking number here would be an invented cost.
        lines.append(
            f"    rollover <b>not priced</b> — {costs.rollover_nights} night(s) held and "
            f"no swap rate configured for this pair"
        )
    else:
        credit = " credit" if costs.rollover_eur < 0 else ""
        lines.append(
            f"    rollover ~€{abs(costs.rollover_eur)}{credit} est · "
            f"{costs.rollover_nights} night(s), tripled on Wednesday"
        )
    return lines


__all__ = ["ACCOUNT_LEVEL_MARGIN", "WEEKEND_GAP", "forex_signal_card"]

"""Turning rows into the views ``cards.py`` renders.

This is where the arithmetic that a card needs actually happens — by calling the
tested functions in ``sentinel/risk/`` and ``sentinel/stats/``, never by doing it
here either. The module exists so that ``cards.py`` can stay literally
arithmetic-free (``tests/bot/test_no_arithmetic.py`` parses it and fails on any
binary operator) while ``/positions`` still shows live uPnL and ``/stats`` still
shows a win rate.

The rule M6 set and M7 keeps: **a number the card needs and the plan lacks is a
gap in a computing module, not a subtraction in the renderer.**
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sentinel.bot.views import (
    PositionView,
    SpendView,
    StatsBreakdownView,
    StatsGroupView,
    StatsView,
    TrackerEventView,
)
from sentinel.core.config import LLMConfig
from sentinel.llm.spend import SpendTotals, evaluate_spend
from sentinel.risk.accounting import Exit, Fill, unrealized_r
from sentinel.risk.models import TradePlan
from sentinel.risk.rounding import money, percent, ratio
from sentinel.stats.models import PerformanceStats, StatsReport
from sentinel.storage.models import SignalEventRow, SignalExitRow, SignalFillRow, SignalRow

HUNDRED = Decimal("100")

#: What each population's line is actually measuring, said on the card so the
#: three sets of numbers can never be read as one.
POPULATION_NOTES = {
    "REAL": "(✅ Taken — your record)",
    "HYPOTHETICAL": "(👀 Watching + ❌ Skipped — what the pipeline would have done)",
    "DRY RUN": "(rehearsal cycles — never published, never taken)",
}


def position_view(
    row: SignalRow,
    plan: TradePlan,
    fills: Sequence[SignalFillRow],
    exits: Sequence[SignalExitRow],
    *,
    mark_price: Decimal | None,
) -> PositionView:
    """One ✅ Taken signal, marked to the tracker's last observed price."""
    filled = tuple(Fill(price=fill.price, qty=fill.qty) for fill in fills)
    closed = tuple(Exit(price=exit_.price, qty=exit_.qty) for exit_ in exits if exit_.qty > 0)
    planned_risk_usdt = plan.planned_risk_eur * plan.eurusd_rate
    planned_qty = sum((rung.qty for rung in plan.entries), Decimal(0))

    open_r: str | None = None
    open_eur: str | None = None
    if mark_price is not None and filled:
        r = unrealized_r(
            direction=plan.direction,
            fills=filled,
            exits=closed,
            mark_price=mark_price,
            planned_risk_usdt=planned_risk_usdt,
        )
        open_r = str(ratio(r))
        open_eur = str(money(r * plan.planned_risk_eur))

    return PositionView(
        number=row.number,
        symbol=row.symbol,
        direction=row.direction,
        setup_type=row.setup_type,
        status=row.status,
        avg_entry=str(
            row.avg_fill_price if row.avg_fill_price is not None else plan.avg_fill_price
        ),
        stop=str(row.stop_price_current if row.stop_price_current is not None else plan.stop),
        targets=tuple(str(target) for target in plan.targets),
        risk_eur=str(plan.risk_eur),
        leverage=str(plan.suggested_leverage),
        expires_at=row.expires_at,
        filled_pct=str(
            percent(row.filled_qty / planned_qty * HUNDRED) if planned_qty > 0 else Decimal(0)
        ),
        tp_hits=row.tp_hits,
        mark_price=None if not filled or mark_price is None else str(mark_price),
        unrealized_r=open_r,
        unrealized_eur=open_eur,
        realized_r=None if row.realized_r is None else str(row.realized_r),
        realized_eur=None if row.realized_eur is None else str(row.realized_eur),
        stop_moved_to=None if row.stop_price_current is None else str(row.stop_price_current),
    )


def tracker_event_view(event: SignalEventRow, row: SignalRow) -> TrackerEventView:
    """A stored event as the §4 reply's inputs. No arithmetic — a lookup."""
    return TrackerEventView(
        kind=event.kind,
        symbol=row.symbol,
        number=row.number,
        price=None if event.price is None else str(event.price),
        realized_r=None if event.realized_r is None else str(event.realized_r),
        realized_eur=None if event.realized_eur is None else str(event.realized_eur),
        detail=event.detail,
        payload={key: str(value) for key, value in event.payload.items()},
    )


def spend_view(totals: SpendTotals, config: LLMConfig) -> SpendView:
    return SpendView(
        day_usd=money(totals.day_usd),
        month_usd=money(totals.month_usd),
        limit_usd=config.daily_spend_limit_usd,
        warn_usd=config.daily_spend_warn_usd,
        state=evaluate_spend(totals, config).value,
        is_floor=totals.is_floor,
        unpriced_calls=totals.unpriced_calls,
    )


def _group(label: str, stats: PerformanceStats) -> StatsGroupView:
    return StatsGroupView(
        label=label,
        note=POPULATION_NOTES[label],
        measured=stats.measured,
        count=stats.count,
        filled=stats.filled,
        unfilled=stats.unfilled,
        wins=stats.wins,
        losses=stats.losses,
        scratches=stats.scratches,
        win_rate_pct=str(stats.win_rate_pct),
        avg_r=str(stats.avg_r),
        total_r=str(stats.total_r),
        total_eur=str(stats.total_eur),
        costs_eur=str(stats.costs_eur),
        # "n/a" and not a number: a ratio with no losses in the denominator is
        # undefined, and printing a large figure would read as a measured edge.
        profit_factor="n/a (no losses yet)"
        if stats.profit_factor is None
        else str(stats.profit_factor),
        max_drawdown_r=str(stats.max_drawdown_r),
        reached_tp1=stats.reached_tp1,
        reached_tp1_pct=str(stats.reached_tp1_pct),
    )


def stats_view(report: StatsReport) -> StatsView:
    return StatsView(
        window=report.window,
        since=report.since,
        groups=(
            _group("REAL", report.real),
            _group("HYPOTHETICAL", report.hypothetical),
            *((_group("DRY RUN", report.dry_run),) if report.dry_run.count else ()),
        ),
        by_setup=tuple(
            StatsBreakdownView(
                key=row.key,
                count=row.stats.count,
                win_rate_pct=str(row.stats.win_rate_pct),
                avg_r=str(row.stats.avg_r),
            )
            for row in report.by_setup
        ),
        by_prompt_version=tuple(
            StatsBreakdownView(
                key=row.key,
                count=row.stats.count,
                win_rate_pct=str(row.stats.win_rate_pct),
                avg_r=str(row.stats.avg_r),
            )
            for row in report.by_prompt_version
        ),
    )


__all__ = [
    "POPULATION_NOTES",
    "position_view",
    "spend_view",
    "stats_view",
    "tracker_event_view",
]

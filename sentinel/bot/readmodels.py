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
from datetime import datetime
from decimal import Decimal

from sentinel.bot.models import UserAccount, UserStatus
from sentinel.bot.plans import AnyPlan, eur_quote_rate_of, leverage_of, rungs_of
from sentinel.bot.views import (
    AlertView,
    PositionView,
    SpendView,
    StatsBreakdownView,
    StatsGroupView,
    StatsView,
    TrackerEventView,
    UserView,
)
from sentinel.core.alerts import Alert, AlertKind
from sentinel.core.config import LLMConfig, MarketConfig
from sentinel.llm.spend import SpendTotals, evaluate_spend
from sentinel.risk.accounting import Exit, Fill, unrealized_r
from sentinel.risk.rounding import money, percent, ratio
from sentinel.stats.models import Book, PerformanceStats, Population, StatsReport
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
    plan: AnyPlan,
    fills: Sequence[SignalFillRow],
    exits: Sequence[SignalExitRow],
    *,
    mark_price: Decimal | None,
) -> PositionView:
    """One ✅ Taken signal, marked to the tracker's last observed price.

    Market-blind from M10c. Everything it reads is a name both plans share (§16.2),
    and the two that are spelled differently go through
    :func:`sentinel.bot.plans.eur_quote_rate_of` and :func:`~sentinel.bot.plans.leverage_of`
    — which is one branch each, in one place, with the reason attached.
    """
    filled = tuple(Fill(price=fill.price, qty=fill.qty) for fill in fills)
    closed = tuple(Exit(price=exit_.price, qty=exit_.qty) for exit_ in exits if exit_.qty > 0)
    # Named ``_usdt`` by ``risk/accounting``; it is really "risk in the quote currency",
    # which is USDT for crypto and USD or JPY for forex (§7.1).
    planned_risk_quote = plan.planned_risk_eur * eur_quote_rate_of(plan)
    planned_qty = sum((rung.qty for rung in rungs_of(plan)), Decimal(0))

    open_r: str | None = None
    open_eur: str | None = None
    if mark_price is not None and filled:
        r = unrealized_r(
            direction=plan.direction,
            fills=filled,
            exits=closed,
            mark_price=mark_price,
            planned_risk_usdt=planned_risk_quote,
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
        leverage=leverage_of(plan),
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


def alert_view(alert: Alert, *, spend: SpendView | None = None) -> AlertView:
    """One ``core.alerts.Alert`` as the lines an admin message is made of.

    The words are assembled here rather than in ``cards.py`` for the usual
    reason — the renderer stays a renderer — and every line answers the only two
    questions a 3am message has to answer: what stopped, and what is still
    running. "The tracker is unaffected" is on the failure alert on purpose: an
    owner with an open position needs to know that the part managing it did not
    stop with the part that failed.
    """
    if alert.kind is AlertKind.CYCLE_FAILURES:
        body = [
            f"The scan cycle has failed {alert.failures} times in a row.",
            "The tracker is unaffected — open positions are still being watched.",
        ]
        if alert.last_error:
            body.append(f"Last error: {alert.last_error}")
        body.append("Logs: docker compose logs --tail=200 app")
        return AlertView(
            kind=alert.kind.value,
            title=f"🚨 Sentinel — {alert.failures} cycles failed in a row",
            body=tuple(body),
            at=alert.since,
        )

    if alert.kind is AlertKind.CYCLE_RECOVERED:
        return AlertView(
            kind=alert.kind.value,
            title="✅ Sentinel — the scan cycle recovered",
            body=(
                f"A cycle completed normally after {alert.failures} consecutive failures.",
                "Nothing was lost: a failed cycle is skipped, not retried.",
            ),
            at=alert.since,
        )

    if alert.kind is AlertKind.FOREX_REAUTH_REQUIRED:
        # The one alert in this system that asks the owner to *do* something
        # specific, so it says what, why, and how long it takes — and it says what
        # kept running, because "forex is paused" must not read as "Sentinel is down".
        body = [
            "Forex is paused: the Saxo login chain has expired and cannot renew itself.",
            "Crypto is unaffected — it is still scanning, gating and publishing.",
            "",
            "This needs a two-minute browser login on your own Mac. Nothing is broken.",
        ]
        if alert.detail:
            body.append(alert.detail)
        if alert.last_error:
            body.append(f"Authorize here: {alert.last_error}")
        return AlertView(
            kind=alert.kind.value,
            title="🔑 Sentinel — forex needs you to log in to Saxo again",
            body=tuple(body),
            at=alert.since,
        )

    if spend is None:  # pragma: no cover — the caller always pairs these
        raise ValueError(f"{alert.kind} needs the spend totals to render")

    at_least = "at least " if spend.is_floor else ""
    figures = [
        f"Today: {at_least}${spend.day_usd} of ${spend.limit_usd} (warn at ${spend.warn_usd}).",
        f"Month to date: {at_least}${spend.month_usd}.",
    ]
    if spend.is_floor:
        figures.append(
            f"{spend.unpriced_calls} call(s) used a model with no price in config — "
            "their cost is missing from the figures above, not zero."
        )
    if alert.kind is AlertKind.SPEND_LIMIT:
        return AlertView(
            kind=alert.kind.value,
            title="⛔ Sentinel — daily LLM spend limit reached",
            body=(
                *figures,
                "New deep analysis is suspended until 00:00 UTC. The screener and "
                "the tracker keep running.",
                "It clears itself: the totals are recomputed every cycle, so there "
                "is nothing to /resume.",
            ),
        )
    return AlertView(
        kind=alert.kind.value,
        title="⚠️ Sentinel — LLM spend past the warn level",
        body=(*figures, "Nothing is suspended yet. Analysis stops at the limit."),
    )


def spend_view(
    totals: SpendTotals, config: LLMConfig, *, market: MarketConfig | None = None
) -> SpendView:
    """Today's spend as a card reads it.

    ``market`` names the budget the figure is being compared against (M10a): the
    totals are one market's, so the ceiling beside them has to be that market's too,
    or a reader would see "$7.42 of $10" where the real limit was $4. ``None`` keeps
    the pre-M10a global figures, which is what the deployment-wide surfaces want.

    The *state* still comes from :func:`evaluate_spend` against whichever budget is
    in play, so the word and the number can never disagree.
    """
    llm = (
        config
        if market is None
        else config.model_copy(
            update={
                "daily_spend_limit_usd": market.llm_daily_budget_usd,
                "daily_spend_warn_usd": market.llm_daily_warn_usd,
            }
        )
    )
    return SpendView(
        day_usd=money(totals.day_usd),
        month_usd=money(totals.month_usd),
        limit_usd=llm.daily_spend_limit_usd,
        warn_usd=llm.daily_spend_warn_usd,
        state=evaluate_spend(totals, llm).value,
        is_floor=totals.is_floor,
        unpriced_calls=totals.unpriced_calls,
    )


def _group(book: Book, stats: PerformanceStats) -> StatsGroupView:
    return StatsGroupView(
        label=book.label,
        # Keyed by the population, not by the rendered label: the label carries the
        # market from M10a and the note is about what the population *means*, which
        # is the same sentence in every market.
        note=POPULATION_NOTES[_note_key(book)],
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


def _note_key(book: Book) -> str:
    return "DRY RUN" if book.population is Population.DRY_RUN else book.population.value


def stats_view(report: StatsReport, *, header: str = "") -> StatsView:
    """One market's report as the card's inputs.

    ``header`` names the market and is empty unless more than one is enabled, so a
    crypto-only deployment renders the card exactly as it did before M10a. The
    DRY RUN block still appears only when it holds something — a population that has
    never happened is noise on a phone, and it was already hidden that way.
    """
    return StatsView(
        window=report.window,
        market=header,
        since=report.since,
        groups=tuple(
            _group(entry.book, entry.stats)
            for entry in report.books
            if entry.book.population is not Population.DRY_RUN or entry.stats.count
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


def user_view(account: UserAccount, *, now: datetime) -> UserView:
    """One ``/users`` row, and the point at which the privacy boundary is applied.

    Everything the owner is not entitled to see is dropped *here*, on the way out of
    the account object, rather than left for the renderer to remember not to print:
    ``capital_eur`` becomes a bool, and the risk %, the P&L and the decisions are
    simply not carried. See :class:`~sentinel.bot.views.UserView`.
    """
    label = (
        f"@{account.username}"
        if account.username
        else (account.display_name or f"id {account.telegram_user_id}")
    )
    approved = account.status is UserStatus.APPROVED
    return UserView(
        user_id=account.telegram_user_id,
        label=label,
        status=account.status.value,
        role=account.role.value,
        since=account.decided_at if approved else account.requested_at,
        since_label="joined" if approved else "requested",
        capital_set=account.capital_set,
        loss_paused=account.pause.is_active(now),
    )


__all__ = [
    "POPULATION_NOTES",
    "alert_view",
    "position_view",
    "spend_view",
    "stats_view",
    "tracker_event_view",
    "user_view",
]

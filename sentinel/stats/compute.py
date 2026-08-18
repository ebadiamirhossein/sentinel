"""The arithmetic behind ``/stats``. Pure, ``Decimal``, and explicit about zero.

Definitions, all settled with the owner at M7 because no spec fixed them:

* **A win is realized R > 0**, over signals that actually **filled at least one
  rung**. A signal that expired or invalidated before entry was not a trade; it is
  counted and reported on its own line, and kept out of the win rate, the average,
  the profit factor and the drawdown. Leaving it in at 0R would drag the average
  toward zero with non-trades and make the denominator answer a different question
  from profit factor's.
* **A scratch** — realized exactly 0R, which is what a stop moved to breakeven
  produces — is neither a win nor a loss, and is reported. Counting it as a win
  would flatter the rate; hiding it would make the counts stop adding up.
* **Profit factor** is gross profit over gross loss, in R. Undefined with no
  losses, and reported as undefined rather than as a large number.
* **Max drawdown** is the deepest peak-to-trough fall of the cumulative-R curve,
  ordered by when each signal resolved.

PRD G3's separate target — "reach at least TP1 or breakeven" — is reported beside
the win rate rather than instead of it. They measure different things, and a
system tuned against one is not tuned against the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from sentinel.risk.rounding import money, percent, ratio
from sentinel.stats.models import Breakdown, PerformanceStats, Population, ResolvedSignal

HUNDRED = Decimal("100")


def max_drawdown_r(resolved: Sequence[ResolvedSignal]) -> Decimal:
    """Deepest peak-to-trough fall of the cumulative R curve, as a positive number.

    Ordered by ``closed_at``: a drawdown is a statement about a sequence, and a set
    sorted any other way would produce a different, meaningless answer.
    """
    peak = Decimal(0)
    equity = Decimal(0)
    worst = Decimal(0)
    for signal in sorted(resolved, key=lambda item: item.closed_at):
        equity += signal.realized_r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return ratio(worst)


def summarize(resolved: Iterable[ResolvedSignal]) -> PerformanceStats:
    """Fold one population into the figures ``/stats`` prints."""
    items = list(resolved)
    if not items:
        return PerformanceStats()

    traded = [item for item in items if item.filled]
    unfilled = len(items) - len(traded)
    if not traded:
        # Signals existed and none of them ever became a trade. Reporting zeros
        # for win rate and average R would claim a measurement that was not made.
        return PerformanceStats(count=len(items), unfilled=unfilled)

    wins = [item for item in traded if item.realized_r > 0]
    losses = [item for item in traded if item.realized_r < 0]
    scratches = len(traded) - len(wins) - len(losses)

    total_r = sum((item.realized_r for item in traded), Decimal(0))
    gross_profit = sum((item.realized_r for item in wins), Decimal(0))
    gross_loss = -sum((item.realized_r for item in losses), Decimal(0))
    reached = [item for item in traded if item.reached_tp1]

    return PerformanceStats(
        count=len(items),
        filled=len(traded),
        unfilled=unfilled,
        wins=len(wins),
        losses=len(losses),
        scratches=scratches,
        win_rate_pct=percent(Decimal(len(wins)) / Decimal(len(traded)) * HUNDRED),
        avg_r=ratio(total_r / Decimal(len(traded))),
        total_r=ratio(total_r),
        total_eur=money(sum((item.realized_eur for item in traded), Decimal(0))),
        costs_eur=money(sum((item.realized_costs_eur for item in traded), Decimal(0))),
        profit_factor=ratio(gross_profit / gross_loss) if gross_loss > 0 else None,
        max_drawdown_r=max_drawdown_r(traded),
        reached_tp1=len(reached),
        reached_tp1_pct=percent(Decimal(len(reached)) / Decimal(len(traded)) * HUNDRED),
    )


def by_key(
    resolved: Iterable[ResolvedSignal], key: str, *, minimum: int = 1
) -> tuple[Breakdown, ...]:
    """Break a population down by ``setup_type`` or ``prompt_version``.

    Sorted by sample size, largest first: a 100% win rate over one signal is the
    least informative row in the table and should not lead it. ``minimum`` drops
    the rows too small to mean anything at all.
    """
    grouped: dict[str, list[ResolvedSignal]] = {}
    for signal in resolved:
        value = getattr(signal, key)
        label = str(getattr(value, "value", value)) if value is not None else "unknown"
        grouped.setdefault(label, []).append(signal)

    rows = [
        Breakdown(key=label, stats=summarize(items))
        for label, items in grouped.items()
        if len(items) >= minimum
    ]
    return tuple(sorted(rows, key=lambda row: (-row.stats.count, row.key)))


def split(resolved: Iterable[ResolvedSignal]) -> dict[Population, list[ResolvedSignal]]:
    books: dict[Population, list[ResolvedSignal]] = {group: [] for group in Population}
    for signal in resolved:
        books[signal.population].append(signal)
    return books


def tracked(resolved: Iterable[ResolvedSignal]) -> list[ResolvedSignal]:
    """Real plus hypothetical — everything the *pipeline* produced for a human.

    The breakdowns run over this rather than over the taken-only book: at PRD G3's
    <=5 signals/day the real sample stays too thin to split for months, and "which
    setups does the analyst get right" is a question about the analyst.
    """
    return [signal for signal in resolved if signal.population is not Population.DRY_RUN]


__all__ = ["by_key", "max_drawdown_r", "split", "summarize", "tracked"]

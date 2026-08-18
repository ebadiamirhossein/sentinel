"""``/stats`` arithmetic, hand-calculated (PRD G2, specs/TELEGRAM_UX.md §3).

Same discipline as the risk goldens: the sums are written out before the code is
asked for them. These figures are the ones M9 tunes the prompt against, so a
definition that quietly drifts — counting a non-trade, calling a breakeven a win —
would move the target without anyone noticing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sentinel.analyst.models import Direction, SetupType
from sentinel.bot.models import SignalDecision
from sentinel.stats.compute import by_key, max_drawdown_r, split, summarize, tracked
from sentinel.stats.models import Population, ResolvedSignal, population_of

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def outcome(
    r: str,
    *,
    filled: bool = True,
    population: Population = Population.REAL,
    setup: SetupType = SetupType.TREND_PULLBACK,
    prompt: str | None = "fable_v1",
    reached_tp1: bool | None = None,
    minutes: int = 0,
) -> ResolvedSignal:
    value = Decimal(r)
    return ResolvedSignal(
        signal_id=f"s{minutes}-{r}",
        number=minutes,
        symbol="SOLUSDT",
        direction=Direction.LONG,
        setup_type=setup,
        prompt_version=prompt,
        population=population,
        filled=filled,
        realized_r=value,
        realized_eur=value * Decimal("75"),
        realized_costs_eur=Decimal("3.16") if filled else Decimal("0"),
        reached_tp1=(value > 0) if reached_tp1 is None else reached_tp1,
        outcome="TP1" if value > 0 else "STOP",
        closed_at=NOW + timedelta(minutes=minutes),
    )


# --------------------------------------------------------------------------- #
# The headline figures
# --------------------------------------------------------------------------- #


def test_the_worked_example() -> None:
    """Six trades: +1.80, -1.00, +0.44, -1.00, +2.50, -0.40.

    wins        3 of 6                  -> 50% win rate
    total R     1.80-1.00+0.44-1.00+2.50-0.40 = 2.34R
    avg R       2.34 / 6                = 0.39R
    gross win   1.80+0.44+2.50          = 4.74R
    gross loss  1.00+1.00+0.40          = 2.40R
    profit factor 4.74 / 2.40           = 1.975 -> 1.98
    equity      1.80, 0.80, 1.24, 0.24, 2.74, 2.34
    peak/trough 1.80 -> 0.24 is the deepest fall = 1.56R
    EUR         2.34 x 75.00            = 175.50
    """
    stats = summarize(
        [
            outcome("1.80", minutes=1),
            outcome("-1.00", minutes=2),
            outcome("0.44", minutes=3),
            outcome("-1.00", minutes=4),
            outcome("2.50", minutes=5),
            outcome("-0.40", minutes=6),
        ]
    )
    assert stats.count == 6 and stats.filled == 6 and stats.unfilled == 0
    assert stats.wins == 3 and stats.losses == 3 and stats.scratches == 0
    assert stats.win_rate_pct == Decimal("50")
    assert stats.total_r == Decimal("2.34")
    assert stats.avg_r == Decimal("0.39")
    assert stats.profit_factor == Decimal("1.98")
    assert stats.max_drawdown_r == Decimal("1.56")
    assert stats.total_eur == Decimal("175.50")


def test_a_signal_that_never_filled_is_counted_and_excluded() -> None:
    """An expired-unfilled signal is not a trade. It appears in ``count`` and
    ``unfilled`` and touches nothing else — leaving it in the population at 0R
    would drag the average toward zero with something that never risked a euro.

        traded      +1.00, -1.00       -> 1 win of 2 = 50%, avg 0.00R
        unfilled    1                  -> reported, not averaged
    """
    stats = summarize(
        [
            outcome("1.00", minutes=1),
            outcome("-1.00", minutes=2),
            outcome("0", filled=False, minutes=3),
        ]
    )
    assert stats.count == 3
    assert stats.filled == 2 and stats.unfilled == 1
    assert stats.win_rate_pct == Decimal("50")
    assert stats.avg_r == Decimal("0")


def test_a_breakeven_stop_is_a_scratch_and_not_a_win() -> None:
    """§5 moves the stop to breakeven after TP1, so exactly-0R outcomes are a
    normal result, not an edge case. Counting one as a win would flatter the rate
    on the very trades the management plan protected."""
    stats = summarize([outcome("1.00", minutes=1), outcome("0", minutes=2)])
    assert stats.wins == 1 and stats.losses == 0 and stats.scratches == 1
    assert stats.win_rate_pct == Decimal("50"), "a scratch is in the denominator"


def test_profit_factor_is_undefined_rather_than_infinite_without_losses() -> None:
    """Three wins and no losses is a ratio with an empty denominator. Rendering it
    as a number would read as a measured result on a week of luck."""
    stats = summarize([outcome("1.00", minutes=i) for i in range(3)])
    assert stats.profit_factor is None
    assert stats.max_drawdown_r == Decimal("0")


def test_nothing_resolved_reports_nothing_rather_than_zero_percent() -> None:
    """An empty window must not claim a 0% win rate — that reads as "the system
    never wins" instead of "nothing has been measured"."""
    empty = summarize([])
    assert empty.count == 0 and empty.measured is False
    assert empty.win_rate_pct == Decimal("0") and empty.profit_factor is None


def test_only_unfilled_signals_is_also_unmeasured() -> None:
    stats = summarize([outcome("0", filled=False, minutes=i) for i in range(3)])
    assert stats.count == 3 and stats.unfilled == 3
    assert stats.measured is False


def test_the_prd_target_is_reported_beside_the_win_rate() -> None:
    """PRD G3 aims at ">=60% of signals reach at least TP1 or breakeven", which is
    a different question from "did it end green". A trade that took TP1 and then
    trailed into a loss counts for one and against the other."""
    stats = summarize(
        [
            outcome("-0.30", reached_tp1=True, minutes=1),
            outcome("1.80", reached_tp1=True, minutes=2),
            outcome("-1.00", reached_tp1=False, minutes=3),
            outcome("-1.00", reached_tp1=False, minutes=4),
        ]
    )
    assert stats.win_rate_pct == Decimal("25")
    assert stats.reached_tp1 == 2 and stats.reached_tp1_pct == Decimal("50")


# --------------------------------------------------------------------------- #
# Drawdown
# --------------------------------------------------------------------------- #


def test_drawdown_is_measured_along_the_sequence_not_over_the_set() -> None:
    """Same six results, opposite order, different drawdown. That is not a bug —
    a drawdown is a statement about a path, which is why the curve is ordered by
    ``closed_at`` and not by anything the caller happened to pass."""
    losses_first = [
        outcome("-1.00", minutes=1),
        outcome("-1.00", minutes=2),
        outcome("3.00", minutes=3),
    ]
    wins_first = [
        outcome("3.00", minutes=1),
        outcome("-1.00", minutes=2),
        outcome("-1.00", minutes=3),
    ]
    assert max_drawdown_r(losses_first) == Decimal("2.00")
    assert max_drawdown_r(wins_first) == Decimal("2.00")

    only_up = [outcome("1.00", minutes=1), outcome("1.00", minutes=2)]
    assert max_drawdown_r(only_up) == Decimal("0")


def test_drawdown_ignores_the_order_the_rows_arrive_in() -> None:
    """A query with no ORDER BY must not change the answer."""
    ordered = [outcome("2.00", minutes=1), outcome("-1.50", minutes=2), outcome("0.50", minutes=3)]
    assert max_drawdown_r(ordered) == max_drawdown_r(list(reversed(ordered)))


# --------------------------------------------------------------------------- #
# Populations and breakdowns
# --------------------------------------------------------------------------- #


def test_the_three_populations_never_merge() -> None:
    books = split(
        [
            outcome("1.00", population=Population.REAL, minutes=1),
            outcome("-1.00", population=Population.HYPOTHETICAL, minutes=2),
            outcome("2.00", population=Population.DRY_RUN, minutes=3),
        ]
    )
    assert [len(books[group]) for group in Population] == [1, 1, 1]
    assert summarize(books[Population.REAL]).total_r == Decimal("1.00")
    assert summarize(books[Population.DRY_RUN]).total_r == Decimal("2.00")


def test_dry_run_is_excluded_from_the_breakdowns() -> None:
    """The breakdowns answer "which setups does the pipeline get right", over real
    plus hypothetical. A rehearsal day would otherwise dominate the table on
    volume alone."""
    followed = tracked(
        [
            outcome("1.00", population=Population.REAL, minutes=1),
            outcome("-1.00", population=Population.HYPOTHETICAL, minutes=2),
            outcome("5.00", population=Population.DRY_RUN, minutes=3),
        ]
    )
    assert len(followed) == 2
    assert summarize(followed).total_r == Decimal("0")


def test_a_breakdown_leads_with_the_largest_sample() -> None:
    """A 100% win rate over one signal is the least informative row in the table
    and must not lead it."""
    rows = by_key(
        [
            outcome("1.00", setup=SetupType.BREAKOUT_RETEST, minutes=1),
            outcome("1.00", setup=SetupType.TREND_PULLBACK, minutes=2),
            outcome("-1.00", setup=SetupType.TREND_PULLBACK, minutes=3),
            outcome("0.50", setup=SetupType.TREND_PULLBACK, minutes=4),
        ],
        "setup_type",
    )
    assert [row.key for row in rows] == ["trend_pullback", "breakout_retest"]
    assert rows[0].stats.count == 3
    assert rows[0].stats.win_rate_pct == percent_of(2, 3)


def percent_of(part: int, whole: int) -> Decimal:
    from sentinel.risk.rounding import percent

    return percent(Decimal(part) / Decimal(whole) * Decimal("100"))


def test_a_breakdown_by_prompt_version_is_what_prompts_md_iterates_on() -> None:
    """specs/PROMPTS.md §5 step 3: "Compare /stats by-prompt-version". This is the
    table that decides whether a new prompt is kept."""
    rows = by_key(
        [
            outcome("1.00", prompt="fable_v1", minutes=1),
            outcome("-1.00", prompt="fable_v1", minutes=2),
            outcome("2.00", prompt="fable_v2", minutes=3),
        ],
        "prompt_version",
    )
    assert {row.key for row in rows} == {"fable_v1", "fable_v2"}


def test_a_missing_prompt_version_is_labelled_rather_than_dropped() -> None:
    rows = by_key([outcome("1.00", prompt=None, minutes=1)], "prompt_version")
    assert [row.key for row in rows] == ["unknown"]


# --------------------------------------------------------------------------- #
# Which book a signal belongs to
# --------------------------------------------------------------------------- #


def test_taken_is_real_and_everything_else_tracked_is_hypothetical() -> None:
    assert population_of(SignalDecision.TAKEN, dry_run=False) is Population.REAL
    assert population_of(SignalDecision.WATCHING, dry_run=False) is Population.HYPOTHETICAL
    assert population_of(SignalDecision.SKIPPED, dry_run=False) is Population.HYPOTHETICAL
    assert population_of(None, dry_run=False) is Population.HYPOTHETICAL


def test_a_dry_run_signal_is_never_real_whatever_its_decision_says() -> None:
    """It was never sent, so nobody can have pressed anything — and if a row ever
    said otherwise, it still was not a trade."""
    assert population_of(SignalDecision.TAKEN, dry_run=True) is Population.DRY_RUN

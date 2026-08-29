"""The M12 backtest, against the day it was written for.

The fixture is the 2026-08-28 cluster as the journal exported it: three longs opened
inside 36 minutes and stopped inside 61 seconds, then a fourth that filled four minutes
after they cleared. Every assertion below is about that day, because that day is the
whole evidence base and a test built from invented rows would prove the arithmetic
without proving it was pointed at anything.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.tools.concurrency_backtest import (
    DEFAULT_RISK_PER_TRADE_PCT,
    Trade,
    load,
    main,
    render,
    replay,
)

CAP_CANDIDATE = Decimal("1.5")
CAP_TODAY = Decimal("2.25")


def _at(hour: int, minute: int, *, day: int = 28) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


#: The cluster. Losses are -1R here rather than the window's measured -0.85R average:
#: these tests are about *which* trades a cap admits, and a hand-checkable R makes the
#: sums readable. The measured figures live in journal/M9_STATS_REVIEW.md.
CLUSTER: tuple[Trade, ...] = (
    Trade(
        number=18,
        symbol="AVAXUSDT",
        direction="long",
        time_in=_at(5, 21),
        time_out=_at(13, 3),
        pnl_r_net=Decimal("-1"),
        outcome="STOP",
        population="HYPOTHETICAL",
        setup_type="trend_pullback",
    ),
    Trade(
        number=20,
        symbol="SOLUSDT",
        direction="long",
        time_in=_at(5, 31),
        time_out=_at(13, 4),
        pnl_r_net=Decimal("-1"),
        outcome="STOP",
        population="HYPOTHETICAL",
        setup_type="trend_pullback",
    ),
    Trade(
        number=22,
        symbol="DOGEUSDT",
        direction="long",
        time_in=_at(5, 57),
        time_out=_at(13, 3),
        pnl_r_net=Decimal("-1"),
        outcome="STOP",
        population="REAL",
        setup_type="trend_pullback",
    ),
    Trade(
        number=21,
        symbol="BTCUSDT",
        direction="long",
        time_in=_at(13, 8),
        time_out=_at(19, 2),
        pnl_r_net=Decimal("-1"),
        outcome="STOP",
        population="HYPOTHETICAL",
        setup_type="trend_pullback",
    ),
)


def _blocked(cap: Decimal, trades: tuple[Trade, ...] = CLUSTER) -> list[int]:
    return [item.trade.number for item in replay(trades, cap_pct=cap).blocked]


# --------------------------------------------------------------------------- #
# The finding
# --------------------------------------------------------------------------- #


def test_a_1_5_cap_blocks_the_third_simultaneous_long_and_only_it() -> None:
    """#22 DOGEUSDT — the owner's real money — is the one trade the candidate refuses."""
    assert _blocked(CAP_CANDIDATE) == [22]


def test_the_fourth_trade_is_admitted_because_the_cluster_had_cleared() -> None:
    """#21 filled at 13:08, four minutes after #18 and #20 stopped.

    This is the assertion that proves the rail is not a daily quota. A blocked trade
    must not cascade into blocking everything after it.
    """
    assert 21 not in _blocked(CAP_CANDIDATE)


def test_todays_direction_blind_cap_is_a_no_op_on_this_window() -> None:
    """2.25% is ``max_open_risk_pct``. As a *same-direction* cap it changes nothing.

    Three concurrent longs was the account at its designed maximum on 2026-08-28
    (2.25 / 0.75 = 3), which is why this milestone is a new rail and not a bug fix.
    """
    assert replay(CLUSTER, cap_pct=CAP_TODAY).blocked == ()


def test_the_boundary_is_open_plus_this_greater_than_cap() -> None:
    """Mirrors ``risk/rails.check_portfolio_rails``. One comparison decides 2 versus 3.

    At exactly 1.5% open, a third 0.75% position takes the book to 2.25% and is refused;
    at 1.5% cap the *second* position lands exactly on the cap and is admitted.
    """
    result = replay(CLUSTER, cap_pct=CAP_CANDIDATE)
    assert [item.open_pct for item in result.blocked] == [Decimal("1.50")]
    assert _blocked(Decimal("0.75")) == [20, 22]


# --------------------------------------------------------------------------- #
# The rail is direction-aware, which is the entire point
# --------------------------------------------------------------------------- #


def test_an_opposite_direction_position_does_not_consume_the_budget() -> None:
    """A long and a short are a hedge, not a doubled bet. §9's reasoning, mirrored."""
    short = Trade(
        number=99,
        symbol="ETHUSDT",
        direction="short",
        time_in=_at(5, 40),
        time_out=_at(13, 30),
        pnl_r_net=Decimal("2"),
        outcome="TP2",
        population="HYPOTHETICAL",
        setup_type="breakout_retest",
    )
    trades = (CLUSTER[0], CLUSTER[1], short, CLUSTER[2])
    result = replay(trades, cap_pct=CAP_CANDIDATE)
    assert [item.trade.number for item in result.blocked] == [22]
    assert result.taken_net_r == Decimal("0")  # -1 -1 +2


def test_a_cap_that_blocks_a_winner_is_reported_as_a_cost() -> None:
    """The check the owner asked for: the harness must be able to say the cap cost money.

    A test that only ever shows the cap removing losses is a test that cannot report the
    finding that would stop this milestone.
    """
    winner = Trade(
        number=23,
        symbol="LINKUSDT",
        direction="long",
        time_in=_at(6, 10),
        time_out=_at(20, 0),
        pnl_r_net=Decimal("3"),
        outcome="TP3",
        population="HYPOTHETICAL",
        setup_type="breakout_retest",
    )
    result = replay((CLUSTER[0], CLUSTER[1], winner), cap_pct=CAP_CANDIDATE)
    assert [item.trade.number for item in result.blocked] == [23]
    assert result.blocked_net_r == Decimal("3")
    assert result.taken_net_r == Decimal("-2")
    assert "+3R" in render([CLUSTER[0], CLUSTER[1], winner], [result])


# --------------------------------------------------------------------------- #
# Reading the export
# --------------------------------------------------------------------------- #


def test_an_unfilled_signal_is_dropped_rather_than_counted() -> None:
    """No fill, no position: it never occupied the budget and cannot block anything."""
    rows = [
        {
            "number": "19",
            "symbol": "LTCUSDT",
            "direction": "long",
            "time_in": "",
            "time_out": "",
            "pnl_r_net": "",
            "outcome": "EXPIRY",
            "population": "HYPOTHETICAL",
            "setup_type": "trend_pullback",
        }
    ]
    assert load(rows) == []


def test_an_open_position_is_never_summed_as_zero() -> None:
    """A missing result and a flat result are different facts."""
    rows = [
        {
            "number": "24",
            "symbol": "BTCUSDT",
            "direction": "long",
            "time_in": "2026-08-29T09:00:00Z",
            "time_out": "",
            "pnl_r_net": "",
            "outcome": "",
            "population": "HYPOTHETICAL",
            "setup_type": "trend_pullback",
        }
    ]
    trades = load(rows)
    assert trades[0].pnl_r_net is None
    assert trades[0].time_out is None
    result = replay(trades, cap_pct=CAP_CANDIDATE)
    assert result.unresolved == 1
    assert result.taken_net_r == Decimal("0")
    assert "summed as nothing" in render(trades, [result])


def test_rows_are_replayed_in_fill_order_not_export_order() -> None:
    """The export is ordered by signal number; #22 filled before #21."""
    numbers = [trade.number for trade in load(_as_rows(CLUSTER))]
    assert numbers == [18, 20, 22, 21]


def test_a_naive_timestamp_is_refused_at_the_seam() -> None:
    rows = [
        {
            "number": "1",
            "symbol": "BTCUSDT",
            "direction": "long",
            "time_in": "2026-08-28T05:21:00",
        }
    ]
    with pytest.raises(ValueError, match="not timezone-aware"):
        load(rows)


def test_a_missing_column_names_itself() -> None:
    with pytest.raises(ValueError, match="direction"):
        load([{"number": "1", "symbol": "BTCUSDT", "time_in": "2026-08-28T05:21:00Z"}])


# --------------------------------------------------------------------------- #
# The limitation must be impossible to read past
# --------------------------------------------------------------------------- #


def test_every_result_carries_the_lower_bound_warning() -> None:
    """``time_in`` is the fill time; the rail fires at publication.

    A reader who quotes the net-R figure without this sentence is quoting a number that
    understates the cap, so the sentence is asserted rather than trusted to survive
    editing.
    """
    output = render(list(CLUSTER), [replay(CLUSTER, cap_pct=CAP_CANDIDATE)])
    assert "LOWER BOUND" in output
    assert "FILL" in output


def test_the_cli_reads_a_csv_and_reports_the_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "window.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_as_rows(CLUSTER)[0]))
        writer.writeheader()
        writer.writerows(_as_rows(CLUSTER))

    assert main([str(path), "--cap", "1.5"]) == 0
    output = capsys.readouterr().out
    assert "BLOCKED #22 DOGEUSDT" in output
    assert "net without them: -3R" in output


def test_a_csv_with_no_fills_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence is not success — an empty replay says so and fails."""
    path = tmp_path / "empty.csv"
    path.write_text("number,symbol,direction,time_in\n19,LTCUSDT,long,\n", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "nothing to replay" in capsys.readouterr().err


def test_the_default_risk_matches_the_window_that_was_measured() -> None:
    assert Decimal("0.75") == DEFAULT_RISK_PER_TRADE_PCT


def _as_rows(trades: tuple[Trade, ...]) -> list[dict[str, str]]:
    """Trades back into export rows, in signal-number order as the export writes them."""
    return [
        {
            "number": str(trade.number),
            "symbol": trade.symbol,
            "direction": trade.direction,
            "time_in": trade.time_in.isoformat(),
            "time_out": "" if trade.time_out is None else trade.time_out.isoformat(),
            "pnl_r_net": "" if trade.pnl_r_net is None else str(trade.pnl_r_net),
            "outcome": trade.outcome,
            "population": trade.population,
            "setup_type": trade.setup_type,
        }
        for trade in sorted(trades, key=lambda trade: trade.number)
    ]


def test_an_export_with_no_results_says_so_rather_than_reporting_zero() -> None:
    """+0R from an empty R column reads exactly like a flat window. It is not one.

    This is the report line a reader would quote, so the absence has to be visible on it
    rather than inferred from a count further down.
    """
    blank = tuple(
        Trade(
            number=trade.number,
            symbol=trade.symbol,
            direction=trade.direction,
            time_in=trade.time_in,
            time_out=trade.time_out,
            pnl_r_net=None,
            outcome=trade.outcome,
            population=trade.population,
            setup_type=trade.setup_type,
        )
        for trade in CLUSTER
    )
    output = render(list(blank), [replay(blank, cap_pct=CAP_CANDIDATE)])
    assert "ABSENCE" in output
    assert "from 0 of 4 with a result" in output


def test_a_window_with_results_does_not_carry_the_absence_warning() -> None:
    """Proof of teeth: the warning must be reachable *and* avoidable."""
    output = render(list(CLUSTER), [replay(CLUSTER, cap_pct=CAP_CANDIDATE)])
    assert "ABSENCE" not in output
    assert "from 4 of 4 with a result" in output


# --------------------------------------------------------------------------- #
# `outcome` is a label, never an input to the arithmetic
# --------------------------------------------------------------------------- #


def test_a_stop_with_positive_r_is_counted_as_a_win() -> None:
    """#10 and #8 in the real window are `STOP` with **positive** net R.

    Those are trailing stops after the management plan moved them to breakeven and
    beyond — the ladder working, not losses. Any code that reads `outcome == "STOP"`
    as a loss would misclassify two of the window's five winners and invert the
    finding. The replay must never read `outcome` at all: it sums `pnl_r_net`.
    """
    trailing = Trade(
        number=10,
        symbol="BNBUSDT",
        direction="long",
        time_in=_at(20, 2, day=24),
        time_out=_at(12, 51, day=25),
        pnl_r_net=Decimal("0.35"),
        outcome="STOP",
        population="HYPOTHETICAL",
        setup_type="trend_pullback",
    )
    result = replay((trailing,), cap_pct=CAP_CANDIDATE)
    assert result.taken_net_r == Decimal("0.35")
    assert "+0.35R" in render([trailing], [result])


def test_outcome_never_reaches_the_replay() -> None:
    """Proof of teeth for the test above: relabel every outcome, change nothing.

    If `outcome` ever became an input, this test fails while the one above still
    passes — a single trade cannot show that the *whole* replay ignores the column.
    """
    relabelled = tuple(
        Trade(
            number=trade.number,
            symbol=trade.symbol,
            direction=trade.direction,
            time_in=trade.time_in,
            time_out=trade.time_out,
            pnl_r_net=trade.pnl_r_net,
            outcome="TP3",
            population=trade.population,
            setup_type=trade.setup_type,
        )
        for trade in CLUSTER
    )

    def arithmetic(trades: tuple[Trade, ...]) -> tuple[list[int], Decimal, Decimal, int]:
        outcome = replay(trades, cap_pct=CAP_CANDIDATE)
        return (
            [item.trade.number for item in outcome.blocked],
            outcome.taken_net_r,
            outcome.blocked_net_r,
            outcome.unresolved,
        )

    # Compared by arithmetic, not by object: the Trades themselves differ in exactly
    # the field under test, so a whole-Result comparison would fail for the wrong
    # reason and prove nothing about whether the column is read.
    assert arithmetic(relabelled) == arithmetic(CLUSTER)

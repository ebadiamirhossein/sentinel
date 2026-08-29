"""Replay a measurement window against a candidate same-direction risk cap (M12).

    python -m sentinel.tools.concurrency_backtest window.csv
    python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25

Reads the columns ``/journal`` exports (``sentinel/stats/journal.py``'s ``JournalRow``)
and answers one question: **which signals would a same-direction open-risk cap have
blocked, and what would the window's net R have been without them.**

This is an analysis tool. It imports nothing from ``sentinel.risk`` and enforces
nothing — the rail it models does not exist yet, deliberately (journal/M12_REPORT.md).
It exists so the number in the review is reproducible rather than asserted, and so the
next measurement window can re-run it in one command.

---

**Two limitations, both structural, both printed with every result.**

1. ``time_in`` is the **fill** time. The real rail fires when a signal is *published*,
   and a published-but-unfilled signal already occupies the risk budget. So every
   figure here is a **lower bound** on how much a cap blocks: two signals that
   overlapped as pending orders and filled sequentially look independent to this
   script. Closing that gap needs ``signals.created_at``, which the export does not
   carry.
2. A blocked signal's counterfactual is unknowable. The script removes it and re-sums,
   which assumes the *other* trades would have played out identically. For a portfolio
   of independent bets that is fair; on a day when everything moved together it is the
   assumption under examination. Read the blocked list, not only the total.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: The window this tool was written for ran at ``risk.risk_per_trade_pct: 0.75``.
#: Every open signal occupies the percentage it was **issued** with (RISK_ENGINE.md §7),
#: which the export does not carry per row — so it is a flag rather than a column, and a
#: window whose risk setting moved cannot be replayed faithfully at all.
DEFAULT_RISK_PER_TRADE_PCT = Decimal("0.75")

#: 2.25 is today's ``max_open_risk_pct`` — direction-blind, so as a *same-direction* cap
#: it is a no-op and is included to prove the harness reports "nothing blocked" when
#: nothing should be. 1.5 is the candidate.
DEFAULT_CAPS = (Decimal("1.5"), Decimal("2.25"))

REQUIRED_COLUMNS = ("number", "symbol", "direction", "time_in")


@dataclass(frozen=True)
class Trade:
    """One exported journal row, reduced to what a concurrency rail can see."""

    number: int
    symbol: str
    direction: str
    time_in: datetime
    #: ``None`` while the position is still open — it never closed inside the window.
    time_out: datetime | None
    #: ``None`` for an open position. Never defaulted to zero: a missing result and a
    #: flat result are different facts, and summing the second over the first is how a
    #: backtest quietly flatters itself.
    pnl_r_net: Decimal | None
    outcome: str
    population: str
    setup_type: str


@dataclass(frozen=True)
class Blocked:
    """One trade a cap would have refused, and what it would have cost or saved."""

    trade: Trade
    #: Same-direction risk already open when it arrived, as a percentage of capital.
    open_pct: Decimal


@dataclass(frozen=True)
class Result:
    cap_pct: Decimal
    blocked: tuple[Blocked, ...]
    taken_net_r: Decimal
    blocked_net_r: Decimal
    unresolved: int


def _decimal(raw: str) -> Decimal | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover — malformed export
        raise ValueError(f"not a number: {raw!r}") from exc


def _moment(raw: str) -> datetime | None:
    """Parse an exported timestamp, requiring it to be timezone-aware.

    Every stored timestamp in this system is UTC (CLAUDE.md), and a naive one here would
    compare against aware ones by raising rather than by being silently wrong — but the
    error would surface halfway through a replay. Rejecting it at the seam says which
    row is bad.
    """
    text = raw.strip()
    if not text:
        return None
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {raw!r}")
    return moment


def load(rows: Iterable[dict[str, str]]) -> list[Trade]:
    """Journal-export rows -> trades, keeping only what actually opened a position.

    A signal that never filled has no ``time_in`` and never occupied the budget, so it
    cannot be blocked and cannot block anything. It is dropped here rather than carried
    as a special case through the replay.
    """
    trades: list[Trade] = []
    for row in rows:
        missing = [column for column in REQUIRED_COLUMNS if column not in row]
        if missing:
            raise ValueError(f"export is missing column(s): {', '.join(missing)}")
        time_in = _moment(row["time_in"])
        if time_in is None:
            continue
        trades.append(
            Trade(
                number=int(row["number"]),
                symbol=row["symbol"].strip(),
                direction=row["direction"].strip().lower(),
                time_in=time_in,
                time_out=_moment(row.get("time_out", "")),
                pnl_r_net=_decimal(row.get("pnl_r_net", "")),
                outcome=row.get("outcome", "").strip(),
                population=row.get("population", "").strip(),
                setup_type=row.get("setup_type", "").strip(),
            )
        )
    return sorted(trades, key=lambda trade: (trade.time_in, trade.number))


def replay(
    trades: Sequence[Trade],
    *,
    cap_pct: Decimal,
    risk_per_trade_pct: Decimal = DEFAULT_RISK_PER_TRADE_PCT,
) -> Result:
    """Walk the window in fill order, refusing a trade that breaches the cap.

    The comparison mirrors ``risk/rails.check_portfolio_rails``'s open-risk rail exactly
    — ``open + this > cap`` rejects, so a cap of 1.5 admits two 0.75% positions and
    refuses the third. Getting this boundary wrong by one comparison is the difference
    between a cap of 2 and a cap of 3, which is the whole question.

    **A blocked trade does not occupy the budget**, so it cannot cascade: refusing the
    third long leaves room for a fourth if the first has closed by then.
    """
    open_trades: list[Trade] = []
    blocked: list[Blocked] = []
    taken: list[Trade] = []

    for trade in trades:
        open_trades = [
            held for held in open_trades if held.time_out is None or held.time_out > trade.time_in
        ]
        same_direction = [held for held in open_trades if held.direction == trade.direction]
        open_pct = risk_per_trade_pct * len(same_direction)
        if open_pct + risk_per_trade_pct > cap_pct:
            blocked.append(Blocked(trade=trade, open_pct=open_pct))
            continue
        open_trades.append(trade)
        taken.append(trade)

    resolved = [trade for trade in taken if trade.pnl_r_net is not None]
    return Result(
        cap_pct=cap_pct,
        blocked=tuple(blocked),
        taken_net_r=sum((trade.pnl_r_net or Decimal(0) for trade in resolved), Decimal(0)),
        blocked_net_r=sum((item.trade.pnl_r_net or Decimal(0) for item in blocked), Decimal(0)),
        unresolved=len(taken) - len(resolved),
    )


def render(trades: Sequence[Trade], results: Sequence[Result]) -> str:
    resolved = [trade.pnl_r_net for trade in trades if trade.pnl_r_net is not None]
    baseline = sum(resolved, Decimal(0))
    lines = [
        "── same-direction cap, replayed against the window ──",
        f"trades with a fill: {len(trades)}   net as traded: {baseline:+}R"
        f"   (from {len(resolved)} of {len(trades)} with a result)",
        "",
    ]
    if not resolved:
        # An export with no R column sums to +0R, which reads exactly like a flat
        # window. Say which it is: an absence is not a measurement.
        lines.insert(
            2,
            "⚠️  no row carries pnl_r_net — every R figure below is an ABSENCE, not a zero.",
        )
    for result in results:
        lines.append(f"cap {result.cap_pct}% of capital in one direction")
        if not result.blocked:
            lines.append("  nothing blocked — this cap is a no-op on this window.")
        for item in result.blocked:
            outcome = item.trade.outcome or "open"
            cost = "unresolved" if item.trade.pnl_r_net is None else f"{item.trade.pnl_r_net:+}R"
            lines.append(
                f"  BLOCKED #{item.trade.number} {item.trade.symbol} {item.trade.direction}"
                f" at {item.trade.time_in:%Y-%m-%d %H:%M} — {item.open_pct}% already open"
                f" · {outcome} · {cost}"
            )
        lines.append(
            f"  net without them: {result.taken_net_r:+}R"
            f"   (removed {result.blocked_net_r:+}R across {len(result.blocked)} trade(s))"
        )
        if result.unresolved:
            lines.append(
                f"  {result.unresolved} admitted trade(s) had no result and are summed as nothing."
            )
        lines.append("")
    lines.extend(
        [
            "Both figures are a LOWER BOUND on what the cap blocks: time_in is the FILL",
            "time, and the real rail fires at publication, where a pending unfilled",
            "signal already occupies the budget. Overlaps that this file cannot see are",
            "overlaps the rail would have caught. See the module docstring.",
        ]
    )
    return "\n".join(lines)


def run(path: Path, caps: Sequence[Decimal], risk_per_trade_pct: Decimal) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        trades = load(csv.DictReader(handle))
    if not trades:
        print(f"{path}: no row carries a fill time — nothing to replay.", file=sys.stderr)
        return 1
    results = [replay(trades, cap_pct=cap, risk_per_trade_pct=risk_per_trade_pct) for cap in caps]
    print(render(trades, results))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sentinel.tools.concurrency_backtest",
        description="Replay a journal export against a candidate same-direction risk cap.",
    )
    parser.add_argument("csv_path", type=Path, help="journal export, saved as CSV")
    parser.add_argument(
        "--cap",
        dest="caps",
        type=Decimal,
        action="append",
        metavar="PCT",
        help="same-direction open-risk cap, in percent (repeatable)",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=Decimal,
        default=DEFAULT_RISK_PER_TRADE_PCT,
        metavar="PCT",
        help=f"risk each signal was issued with (default {DEFAULT_RISK_PER_TRADE_PCT})",
    )
    args = parser.parse_args(argv)
    return run(args.csv_path, args.caps or list(DEFAULT_CAPS), args.risk_per_trade)


if __name__ == "__main__":  # pragma: no cover — CLI entrypoint
    raise SystemExit(main())

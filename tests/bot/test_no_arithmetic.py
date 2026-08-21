"""The bot renders; it never computes (specs/TELEGRAM_UX.md §1).

Two layers, because either alone leaves a real hole.

**Layer 1 — the renderer performs no arithmetic.** An AST scan of the rendering
modules for ``+ - * / // % **`` in any form. This catches a renderer that computes
a *correct* number the plan happens not to carry — which is exactly the failure
mode that matters, because it would look right on the card and be untested
everywhere else.

**Layer 2 — every number on the card came from the plan.** Layer 1 says nothing
about a literal: ``leverage 3x`` typed into an f-string passes an AST scan and is
a lie. So every numeric token in a rendered card is matched against the set of
values reachable in the plan itself.

Why this matters more than it looks: the engine floors quantities, rounds stops
*away* from the entry, and derives net RR from the already-displayed gross
multiple so a card reconciles by hand (RISK_ENGINE §4, §4.2). A renderer that
recomputed any of that would silently undo all three, and the owner would place
orders against numbers no test covers.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sentinel.bot.cards import signal_card
from sentinel.bot.formatting import escape
from sentinel.bot.models import SignalRecord
from sentinel.risk.models import TradePlan

#: Every module that turns plan data into owner-facing text.
RENDERING_MODULES = (
    Path(__file__).resolve().parents[2] / "sentinel" / "bot" / "cards.py",
    Path(__file__).resolve().parents[2] / "sentinel" / "bot" / "formatting.py",
)

ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.MatMult)


# --------------------------------------------------------------------------- #
# Layer 1 — no arithmetic in the source
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", RENDERING_MODULES, ids=lambda p: p.name)
def test_the_renderer_contains_no_arithmetic(path: Path) -> None:
    """No binary, augmented or unary arithmetic anywhere in a rendering module.

    ``abs()`` and ``astimezone()`` are function calls and survive deliberately:
    the first removes a sign so a funding credit reads as a credit rather than as
    "€-0.18", and the second re-labels one instant rather than producing a new
    quantity. Neither invents a magnitude, which is the thing being forbidden.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        f"{path.name}:{node.lineno} {type(node.op).__name__}"
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(node.op, ARITHMETIC)
    ]
    offenders += [
        f"{path.name}:{node.lineno} USub"
        for node in ast.walk(tree)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
    ]
    assert not offenders, (
        "the bot must not compute — move the number onto TradePlan, in "
        f"sentinel/risk/, with tests: {offenders}"
    )


def test_the_scan_would_actually_catch_arithmetic() -> None:
    """A guard that cannot fail is not a guard — prove the scan detects one."""
    tree = ast.parse("total = plan.entry_fee_eur + plan.stop_exit_fee_eur\n")
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ARITHMETIC)
    ]
    assert found, "the AST scan failed to notice an addition"


# --------------------------------------------------------------------------- #
# Layer 2 — every number on the card is a plan value
# --------------------------------------------------------------------------- #

#: Structural digits: rung ordinals (1..3), target ordinals (TP1..TP3), the DB
#: signal number, and the two-digit clock/date parts of a rendered timestamp.
#: They are labels and identifiers, not quantities, and none of them is a price.
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


#: Plan fields that are identifiers rather than quantities. Leaving them in would
#: widen the allowlist for free — ``schema_version`` alone would excuse a rendered
#: "3" anywhere on the card.
NOT_QUANTITIES = frozenset({"schema_version", "renderer_version"})


def plan_numbers(payload: Any, into: set[str]) -> set[str]:
    """Every numeric value reachable in the serialized plan, as a string."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in NOT_QUANTITIES:
                plan_numbers(value, into)
    elif isinstance(payload, list | tuple):
        for value in payload:
            plan_numbers(value, into)
    elif isinstance(payload, bool):
        pass
    elif isinstance(payload, int | float | Decimal):
        into.add(str(payload))
    elif isinstance(payload, str) and NUMBER.fullmatch(payload):
        into.add(payload)
        into.add(str(Decimal(payload)))
    return into


#: Free-text fields quoted onto the card verbatim. Their digits ("4h uptrend",
#: "Fear & Greed at 74", "close 40%") are the analyst's words and the engine's
#: management template, not figures this renderer produced — they are traceable
#: precisely because they are copied rather than computed. They are removed by
#: matching the stored text, so a *changed* quotation would resurface as an
#: unexplained number rather than being waved through.
def _prose(plan: Any) -> tuple[str, ...]:
    report = plan.report
    return (
        escape(report.thesis),
        escape(report.counter_thesis),
        escape(report.invalidation_text),
        escape(plan.management_plan),
    )


def card_numbers(card: str, plan: Any) -> list[str]:
    """Numeric tokens the renderer itself emitted."""
    body = card
    for quoted in _prose(plan):
        if quoted:
            body = body.replace(quoted, " ")
    # Timestamps and the signal number are identifiers, not measurements. They are
    # removed wholesale rather than allowlisted digit by digit, so a price can
    # never sneak through by coincidentally looking like a date.
    body = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", " ", body)
    body = re.sub(r"\(\d{2}:\d{2} UTC\)", " ", body)
    body = re.sub(r"Signal #\d+", " ", body)
    body = re.sub(r"^\s*\d+\)", " ", body, flags=re.MULTILINE)  # rung ordinals
    body = re.sub(r"TP\d", " ", body)  # target ordinals
    return NUMBER.findall(body)


def untraceable(card: str, plan: Any) -> list[str]:
    """Numbers the renderer emitted that do not appear anywhere in the plan.

    A leading minus is allowed against an unsigned plan value: ``stop_distance_pct``
    is stored as a magnitude and the card's "-" is a literal, exactly as
    ``tools/size.py`` prints it. The digits still have to match.
    """
    known = plan_numbers(plan.model_dump(mode="json"), set())
    return [
        token
        for token in card_numbers(card, plan)
        if token not in known and token.lstrip("-") not in known
    ]


def test_every_number_on_the_card_comes_from_the_plan(record: SignalRecord, tz: ZoneInfo) -> None:
    """The card is a projection of ``TradePlan`` — no arithmetic, and no invention."""
    unexplained = untraceable(signal_card(record, tz), record.plan)
    assert not unexplained, (
        f"numbers on the card that are not on the plan: {sorted(set(unexplained))}"
    )


def test_the_traceability_check_would_catch_an_invented_number(
    record: SignalRecord, tz: ZoneInfo
) -> None:
    """Proof of teeth for layer 2: a figure typed into an f-string must be caught.

    The notional is mutated rather than something small like a leverage, because a
    one-digit number can collide with an unrelated plan value by chance and the
    guard would look like it worked when it had not.
    """
    card = signal_card(record, tz).replace("€4570.30", "€9999.99")
    assert "9999.99" in untraceable(card, record.plan)


def test_the_card_shows_the_engine_figures_verbatim(record: SignalRecord, tz: ZoneInfo) -> None:
    """Spot-check the quantized values arrive unrounded and unreformatted."""
    card = signal_card(record, tz)
    plan = record.plan
    assert isinstance(plan, TradePlan)  # this fixture is a crypto record, by construction
    for value in (
        plan.risk_eur,
        plan.notional_eur,
        plan.margin_eur,
        plan.stop_distance_pct,
        plan.liq_distance_pct,
        plan.costs.round_trip_cost_eur,
        plan.costs.cost_pct_of_risk,
        plan.avg_entry,
        plan.avg_fill_price,
        plan.last_price,
        *plan.target_distances_pct,
        *plan.rr_targets,
        *plan.rr_targets_net,
    ):
        assert str(value) in card, f"{value} is on the plan but not on the card"

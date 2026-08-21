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
from dataclasses import replace
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

    **Compared by value, not by string** (M10c, FOREX.md defect #22). A renderer is
    allowed to fix a display *scale* — ``Decimal("200.000000000000000000")`` printed as
    ``200.00`` — and is not allowed to change a value. A string comparison could not
    express that distinction, which is precisely why the eighteen-decimal capital
    survived: every check in this file passed with the defect present, because
    ``200.000000000000000000`` *was* the plan's value.

    The value check is not a loosening. ``9999.99`` against a plan holding ``4570.30``
    is still caught, and ``test_the_traceability_check_would_catch_an_invented_number``
    below is the proof. What it stops catching is a difference of trailing zeros, which
    was never a fact about the number.
    """
    known = plan_numbers(plan.model_dump(mode="json"), set())
    values = {Decimal(value) for value in known}
    return [
        token
        for token in card_numbers(card, plan)
        if token not in known
        and token.lstrip("-") not in known
        and Decimal(token) not in values
        and -Decimal(token) not in values
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


# --------------------------------------------------------------------------- #
# Layer 3 — the renderer may fix a scale, and may never change a value
# --------------------------------------------------------------------------- #
#
# The rule that was missing, and whose absence let ``capital €200.000000000000000000``
# reach a live card for 54 cycles. Nothing asserted on scale, and every check that
# compared *values* passed — because the value was right.


#: Every money figure a card renders through ``money_eur``, and where it comes from.
#: A pair rather than a bare field name, so the assertion reads the same way the card
#: does: this text, from that number.
MONEY_ON_THE_CARD = ("capital_eur",)


@pytest.mark.parametrize("field", MONEY_ON_THE_CARD)
def test_money_renders_at_cents_and_keeps_its_value(record: SignalRecord, field: str) -> None:
    """Two decimals on the card, and the same number underneath.

    Driven from a plan whose value carries the **eighteen** decimals a
    ``Numeric(38, 18)`` column hands back, because that is the case that actually
    happens and the one an in-process ``Decimal("10000")`` fixture cannot reach.
    """
    from_postgres = Decimal("200.000000000000000000")
    plan = record.plan.model_copy(update={field: from_postgres})
    card = signal_card(record.model_copy(update={"plan": plan}), ZoneInfo("Europe/Vilnius"))

    assert "200.000000000000000000" not in card
    assert "€200.00" in card
    assert Decimal("200.00") == from_postgres


def test_the_percentage_beside_it_gets_the_same_treatment(record: SignalRecord) -> None:
    """``risk_per_trade_pct`` comes from the same column type and had the same defect.

    Two decimals, **fixed** — see ``formatting.percent_2dp`` for why a budget figure
    pads where a measurement trims. What matters here is only that the eighteen
    decimals the column carries never reach the card, and that trimming does not
    produce ``2E+1`` for a whole number on the way.
    """
    plan = record.plan.model_copy(update={"risk_per_trade_pct": Decimal("0.750000000000000000")})
    card = signal_card(record.model_copy(update={"plan": plan}), ZoneInfo("Europe/Vilnius"))

    assert "risk 0.75%" in card
    assert "0.750000000000000000" not in card
    assert "E+" not in card


def test_the_scale_check_would_catch_a_changed_value(record: SignalRecord) -> None:
    """Proof of teeth. Fixing a scale is allowed; rendering a different number is not.

    Without this, ``untraceable``'s new value comparison could have been a loosening
    nobody measured.
    """
    card = signal_card(record, ZoneInfo("Europe/Vilnius")).replace("€4570.30", "€4570.31")

    assert "4570.31" in untraceable(card, record.plan)


def test_every_status_card_percentage_survives_the_database_scale(
    record: SignalRecord,
) -> None:
    """The two the audit found (HANDOFF §4 item 12), on the same card as the capital.

    ``/status`` prints three sizing percentages and **all** of them trace back to
    ``Numeric(38, 18)`` columns: ``risk per trade`` is the user's own setting, and
    ``open risk`` is a sum of the ``risk_per_trade_pct`` each open plan was issued
    with. Before M10c that line read ``open risk: 1.500000000000000000% of 2.25%``.

    Fixing the capital and leaving these would have been the worse outcome of the two,
    because the card would then have looked deliberate.
    """
    from sentinel.bot.cards import status_card
    from sentinel.core.config import load_config
    from tests.golden.surfaces import _status_view

    # The golden's own view, with the three figures replaced by the scale Postgres
    # actually returns. Built from ``_status_view`` rather than by hand so the rest of
    # the card is the card, not a stub that happens to render.
    from_postgres = Decimal("0.750000000000000000")
    view = replace(
        _status_view(load_config()),
        capital_eur=Decimal("200.000000000000000000"),
        risk_per_trade_pct=from_postgres,
        open_risk_pct=Decimal("1.500000000000000000"),
        max_open_risk_pct=Decimal("2.25"),
    )
    card = status_card(view, ZoneInfo("Europe/Vilnius"))

    assert "capital: €200.00" in card
    assert "risk per trade: 0.75%" in card
    assert "open risk: 1.50% of 2.25%" in card
    assert "000000000" not in card

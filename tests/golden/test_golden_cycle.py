"""The pipeline does not move. One byte different = this fails (M10a Step 1a).

This is the control for the whole milestone. M10a adds a market dimension to the
schema, the config, the spend guard, the rails, the statistics and six Telegram
surfaces, while a live measurement window is running — and the one thing that must
not happen is a change to what the crypto pipeline computes, draws, asks or decides.

Every assertion below compares against a committed golden produced by
``tests/fixtures/generate_goldens_m10a.py``. **A failure here means the refactor is
wrong, not the golden.** Regenerating to make it pass would delete the only evidence
that crypto output survived the milestone.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sentinel.core.config import AppConfig, load_config
from tests.golden.pipeline import (
    SECOND_SYMBOL,
    GoldenCycle,
    golden_symbol_charts,
    run_golden_cycle,
)

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_cycle"

REGENERATE = (
    "If this change was intended, regenerate deliberately with\n"
    "    .venv/bin/python -m tests.fixtures.generate_goldens_m10a\n"
    "and justify the diff in the milestone report. During M10a there is no such "
    "justification: crypto output is not allowed to move."
)


def _json(name: str) -> Any:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _text(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cycle() -> GoldenCycle:
    """One run, shared by every assertion — the charts take a few seconds to draw.

    Module-scoped, so it loads the repo config itself rather than taking the
    function-scoped ``repo_config`` fixture. Same file, same values.
    """
    return run_golden_cycle(load_config())


def test_feature_values_are_unchanged(cycle: GoldenCycle) -> None:
    """M2's whole output: every indicator, regime, level and touch count."""
    assert cycle.features == _json("features_BTCUSDT.json"), REGENERATE


def test_chart_bytes_are_unchanged(cycle: GoldenCycle) -> None:
    """PNG digests **and** render parameters.

    The digest catches a pixel changing; the parameters catch a *reason* for it —
    a candle window, a dpi, an EMA set. Both are pinned because M3's determinism
    tests only prove that the same input twice gives the same output, which stays
    true even if every chart in the system silently changes shape.
    """
    assert cycle.charts == _json("charts.json"), REGENERATE


def test_the_assembled_prompt_is_unchanged(cycle: GoldenCycle) -> None:
    """The exact text the analyst is sent: system prompt, snapshot payload, news
    fence, history block and task line.

    Prompt files are frozen for this milestone, and so is everything that renders
    into the user turn around them.
    """
    assert cycle.prompt == _text("prompt.txt"), REGENERATE


def test_the_approved_gate_decision_is_unchanged(cycle: GoldenCycle) -> None:
    """Status, and the whole sized plan — ladder, stop, targets, costs, leverage."""
    assert cycle.gate_approved == _json("gate_approved.json"), REGENERATE


def test_the_rejection_and_its_reason_are_unchanged(cycle: GoldenCycle) -> None:
    """A rejection is a decision too, and its ``RejectionReason`` is what M9 counts."""
    golden = _json("gate_rejected.json")
    assert cycle.gate_rejected == golden, REGENERATE
    # Stated separately so a failure names the code rather than diffing a document.
    assert golden["reason"] == "NET_RR_TOO_LOW"


def test_the_rendered_card_is_unchanged(cycle: GoldenCycle) -> None:
    """What the owner actually reads. The last link in the chain, and the one a
    market tag added carelessly would break."""
    assert cycle.card == _text("card.txt"), REGENERATE


#: Crypto defaults M10a is forbidden to touch, each with a perturbation that must
#: reach a golden. ``(label, mutate)`` — the mutation is applied to a copy of the
#: real config, never to the one the assertions above use.
FROZEN_DEFAULTS = (
    (
        "charts.candle_window",
        lambda c: {"charts": c.charts.model_copy(update={"candle_window": 100})},
    ),
    (
        "features.rsi_period",
        lambda c: {"features": c.features.model_copy(update={"rsi_period": 21})},
    ),
    (
        "risk.min_rr_tp1",
        lambda c: {"risk": c.risk.model_copy(update={"min_rr_tp1": Decimal("2.5")})},
    ),
    (
        "risk.risk_per_trade_pct",
        lambda c: {"risk": c.risk.model_copy(update={"risk_per_trade_pct": Decimal("1.0")})},
    ),
    (
        "ladder.weights_pct",
        lambda c: {
            "ladder": c.ladder.model_copy(
                update={"weights_pct": (Decimal("50"), Decimal("30"), Decimal("20"))}
            )
        },
    ),
)


@pytest.mark.parametrize("label,mutate", FROZEN_DEFAULTS, ids=[row[0] for row in FROZEN_DEFAULTS])
def test_the_goldens_are_not_vacuous(
    cycle: GoldenCycle, label: str, mutate: Callable[[AppConfig], dict[str, Any]]
) -> None:
    """Changing a frozen crypto default must break a golden.

    The sibling of ``tests/charts/test_determinism.py``'s
    ``test_different_data_yields_different_bytes``, and for the same reason: an
    assertion that everything is unchanged proves nothing unless *something* can
    change it. Each parameter here is one of the defaults the milestone brief names
    as untouchable, so this is also the evidence that the goldens would have caught
    it if one had moved.

    ``AppConfig`` is frozen, so every mutation is a copy; the module-scoped ``cycle``
    above is untouched.
    """
    config = load_config()
    try:
        mutated = run_golden_cycle(config.model_copy(update=mutate(config)))
    except AssertionError:
        # The plan stopped being approved at all — a louder failure than a diff, and
        # a genuine detection. `golden_card` asserts a plan exists before rendering.
        return

    assert (
        mutated.features != cycle.features
        or mutated.charts != cycle.charts
        or mutated.gate_approved != cycle.gate_approved
        or mutated.card != cycle.card
    ), f"changing {label} did not move any golden — the goldens are not watching it"


# --------------------------------------------------------------------------- #
# The second symbol (M10b-2, owner requirement H1)
# --------------------------------------------------------------------------- #

REGENERATE_SECOND = (
    "If this change was intended, regenerate deliberately with\n"
    "    .venv/bin/python -m tests.fixtures.generate_goldens_m10b2\n"
    "which writes these two files and cannot touch any other golden."
)


@pytest.fixture(scope="module")
def second() -> dict[str, Any]:
    """Features and chart digests for the sub-1000 symbol. Module-scoped: it draws."""
    return golden_symbol_charts(load_config(), SECOND_SYMBOL)


def test_the_second_symbols_chart_bytes_are_unchanged(second: dict[str, Any]) -> None:
    """The hole this fixture exists to close.

    ``charts.json`` pins BTCUSDT, which trades above 1000.
    ``charts/renderer._format_price`` — the function that labels every S/R line —
    branches at 1000 and again at 1, so one symbol pinned **one** of three branches.
    A change to the 1-to-1000 branch would have moved the stored bytes of every crypto
    chart in that range, and LINK, AVAX and LTC are all on the live watchlist.

    M10b-2 found this by needing that formatter to behave differently for forex, and
    having to leave it alone because nothing would have caught the difference.
    """
    assert second["charts"] == _json(f"charts_{SECOND_SYMBOL}.json"), REGENERATE_SECOND


def test_the_second_symbols_feature_values_are_unchanged(second: dict[str, Any]) -> None:
    assert second["features"] == _json(f"features_{SECOND_SYMBOL}.json"), REGENERATE_SECOND


def test_the_second_symbol_sits_in_a_different_formatter_branch(
    second: dict[str, Any],
) -> None:
    """Structural, so the addition cannot quietly stop covering what it was added for.

    If somebody later swaps the second symbol for another above 1000, both goldens
    would still pass while covering the same branch twice — which is the original
    defect wearing a second symbol's name.
    """
    from sentinel.charts.renderer import _format_price

    btc = Decimal(_json("charts.json")["1h"]["params"]["last_close"])
    other = Decimal(second["charts"]["1h"]["params"]["last_close"])
    assert btc >= 1000, "the first golden symbol must be in the >= 1000 branch"
    assert 1 <= other < 1000, "the second golden symbol must be in the 1-to-1000 branch"
    # And the two branches genuinely render differently.
    assert _format_price(float(btc)) != f"{float(btc):,.2f}"
    assert _format_price(float(other)) == f"{float(other):,.2f}"


def test_at_least_one_drawn_level_carries_a_label_only_this_branch_produces(
    second: dict[str, Any],
) -> None:
    """The digest is what actually pins the pixels, and it only pins them if a label
    was drawn. A chart with no levels would make the assertion above vacuous."""
    drawn = second["charts"]["1h"]["params"]["levels_drawn"]
    assert drawn, "the second symbol must draw at least one labelled S/R line"
    assert all(1 <= Decimal(level["price"]) < 1000 for level in drawn)

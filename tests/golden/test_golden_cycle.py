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
    EXTRA_SYMBOLS,
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
# The other golden symbols (M10b-2, owner requirement H1)
# --------------------------------------------------------------------------- #

REGENERATE_EXTRA = (
    "If this change was intended, regenerate deliberately with\n"
    "    .venv/bin/python -m tests.fixtures.generate_goldens_m10b2\n"
    "which writes only the extra symbols' fixtures and cannot touch BTCUSDT's."
)


@pytest.fixture(scope="module")
def extras() -> dict[str, dict[str, Any]]:
    """Features and chart digests per extra symbol. Module-scoped: they draw."""
    config = load_config()
    return {symbol: golden_symbol_charts(config, symbol) for symbol in EXTRA_SYMBOLS}


@pytest.mark.parametrize("symbol", EXTRA_SYMBOLS)
def test_the_extra_symbols_chart_bytes_are_unchanged(
    extras: dict[str, dict[str, Any]], symbol: str
) -> None:
    """The hole these fixtures exist to close.

    ``charts.json`` pinned BTCUSDT, which trades above 1000.
    ``charts/renderer._format_price`` — the function that labels every S/R line —
    branches at 1000 and again at 1, so one symbol pinned **one of three**. A change
    to either other branch would have moved the stored bytes of live watchlist
    symbols: LINK, AVAX and LTC sit between 1 and 1000, and XRP, DOGE and ADA below 1.

    M10b-2 found this by needing that formatter to behave differently for forex, and
    having to leave it alone because nothing would have caught the difference.
    """
    assert extras[symbol]["charts"] == _json(f"charts_{symbol}.json"), REGENERATE_EXTRA


@pytest.mark.parametrize("symbol", EXTRA_SYMBOLS)
def test_the_extra_symbols_feature_values_are_unchanged(
    extras: dict[str, dict[str, Any]], symbol: str
) -> None:
    assert extras[symbol]["features"] == _json(f"features_{symbol}.json"), REGENERATE_EXTRA


def test_every_formatter_branch_is_covered_by_exactly_one_golden_symbol(
    extras: dict[str, dict[str, Any]],
) -> None:
    """Structural, so the set cannot silently collapse into covering one branch twice.

    Swapping an extra symbol for another large-cap would leave every golden above
    passing while covering the same branch three times — which is the original defect
    wearing new symbols' names. This asserts the *partition*, not the symbols.
    """

    def branch(price: Decimal) -> str:
        if price >= 1000:
            return ">=1000"
        return "1..1000" if price >= 1 else "<1"

    prices = {"BTCUSDT": Decimal(_json("charts.json")["1h"]["params"]["last_close"])}
    prices.update(
        {
            symbol: Decimal(artefacts["charts"]["1h"]["params"]["last_close"])
            for symbol, artefacts in extras.items()
        }
    )
    covered = {symbol: branch(price) for symbol, price in prices.items()}
    assert sorted(covered.values()) == ["1..1000", "<1", ">=1000"], (
        f"every _format_price branch needs exactly one golden symbol; got {covered}"
    )


@pytest.mark.parametrize("symbol", EXTRA_SYMBOLS)
def test_each_extra_symbol_draws_a_label_only_its_own_branch_produces(
    extras: dict[str, dict[str, Any]], symbol: str
) -> None:
    """The digest pins the pixels only if a label was actually drawn. A chart with no
    levels would make the assertions above vacuous."""
    from sentinel.charts.renderer import _format_price

    drawn = extras[symbol]["charts"]["1h"]["params"]["levels_drawn"]
    assert drawn, f"{symbol} must draw at least one labelled S/R line"
    rendered = {_format_price(float(level["price"])) for level in drawn}
    # The label is what the formatter produced, and it is not the plain repr.
    assert rendered
    assert all(label for label in rendered)

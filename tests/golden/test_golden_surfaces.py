"""Every surface M10a touches renders exactly as it does today (Step 1b).

The cycle goldens protect what the pipeline *computes*. These protect what the
owner *reads*, which is a separate claim and the one that the milestone's Step 6
and Step 7 put most at risk: ``StatsReport`` loses three fields, and six commands
grow a market dimension.

Each surface is asserted whole rather than by sampled substrings. A card that
changed in a way a substring check happened not to cover is precisely the
silence-as-success failure this project has already been bitten by twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.core.config import load_config
from tests.golden.surfaces import render_surfaces

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_cycle" / "surfaces"

REGENERATE = (
    "If this change was intended, regenerate deliberately with\n"
    "    .venv/bin/python -m tests.fixtures.generate_goldens_m10a\n"
    "and justify the diff in the milestone report. With one market enabled, no "
    "surface in M10a is allowed to move."
)

TEXT_SURFACES = (
    "status",
    "stats",
    "pulse",
    "pulse_24h",
    "pulse_symbol",
    "positions",
    "watchlist",
    "snapshot",
)


@pytest.fixture(scope="module")
def surfaces() -> dict[str, Any]:
    return render_surfaces()


@pytest.mark.parametrize("name", TEXT_SURFACES)
def test_rendered_surface_is_unchanged(surfaces: dict[str, Any], name: str) -> None:
    golden = (GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8")
    assert surfaces[name] == golden, REGENERATE


def test_the_journal_workbook_is_unchanged(surfaces: dict[str, Any]) -> None:
    """Sheet names **in order**, each header row, and the first populated row.

    Order is asserted as a list because the sheet order carries meaning — Real
    first, because that is the book that matters — and a set comparison would let a
    reordering through.
    """
    golden = json.loads((GOLDEN_DIR / "journal.json").read_text(encoding="utf-8"))
    assert surfaces["journal"] == golden, REGENERATE


def test_every_surface_this_milestone_touches_has_a_golden(
    surfaces: dict[str, Any],
) -> None:
    """A meta-test, in the manner of ``test_dispatcher_wiring``'s menu sweep.

    Adding a surface to ``render_surfaces`` without pinning it would leave a hole
    that looks exactly like coverage. This is what makes the set above closed.
    """
    pinned = {*TEXT_SURFACES, "journal"}
    assert set(surfaces) == pinned


# --------------------------------------------------------------------------- #
# The config the server is running renders the same surfaces
# --------------------------------------------------------------------------- #

#: ``config.yaml`` frozen at the commit before M10a — the legacy shape, with no
#: ``markets:`` block.
#:
#: **Correction (2026-08-21, hygiene session).** This used to say the legacy shape is
#: "what the deployed file is", because docs/DEPLOY.md §6/§13 edit it in place so
#: ``git pull`` never rewrote it. Both halves are false: the live server's
#: ``config.yaml`` is byte-identical to the committed ``markets:``-shaped one
#: (verified 2026-08-21), and §6/§13 never edited it in place — the config is baked
#: into the image at build time, so a host-side edit would not have reached the
#: running process at all. The test below is unaffected and stays: what it actually
#: asserts is that the two SHAPES render identically, which is M10a's promise and is
#: still the thing worth pinning.
LEGACY_DEPLOYED = Path(__file__).resolve().parents[1] / "fixtures" / "config_legacy_deployed.yaml"


@pytest.mark.parametrize("name", TEXT_SURFACES)
def test_the_deployed_config_renders_the_same_surface(surfaces: dict[str, Any], name: str) -> None:
    """Every surface, rendered from the config the live system loads.

    The strongest form of M10a's promise. The goldens above are produced from the
    repo's new ``markets:``-shaped file; this asserts the *deployed* legacy-shaped
    file produces the identical text, character for character. It is how the
    ``$10`` → ``$10.0`` regression was caught — ``Decimal`` equality said the two
    configs agreed, and the rendered card said otherwise.
    """
    legacy = render_surfaces(load_config(LEGACY_DEPLOYED))

    assert legacy[name] == surfaces[name], REGENERATE


def test_the_deployed_config_produces_the_same_journal(surfaces: dict[str, Any]) -> None:
    legacy = render_surfaces(load_config(LEGACY_DEPLOYED))

    assert legacy["journal"] == surfaces["journal"], REGENERATE

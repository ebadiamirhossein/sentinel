"""What switch-on day changes, pinned **now** (FOREX.md defect #23).

The M10c brief asked for §11's regeneration to happen in this milestone — switching
forex on gives crypto cards their market tag, which changes their bytes — and asked for
``markets.forex.enabled: false`` in the same breath. Both cannot hold: with one market
enabled ``AppConfig.multi_market`` is ``False``, ``section_header`` returns ``""``, and
no crypto byte can move. There was nothing to regenerate.

Owner ruling: **pin the tagged form as an addition instead.** These goldens are what two
enabled markets render, produced from the shipped config with forex flipped on in
memory. The shipped config stays single-market, so
``test_the_deployed_config_renders_the_same_surface`` keeps holding — and switch-on day
becomes a config flip that moves no golden at all, rather than a regeneration performed
on the one day there is least attention to spare for it.

**The load-bearing test in this file is not any of the byte comparisons.** It is
``test_every_multi_market_surface_differs_by_its_header_alone``: a golden that merely
records whatever the code does today would ratify a mistake as readily as a fix. What
makes these meaningful is the *relationship* they pin — the tagged form is the untagged
form plus a header, and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.core.config import load_config
from sentinel.core.markets import Market
from tests.golden.pipeline import (
    APPROVED_REPORT,
    golden_card,
    golden_features,
    golden_gate,
    golden_report,
    golden_snapshot,
    single_market,
)
from tests.golden.surfaces import render_surfaces

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "golden_cycle"
MULTI_DIR = GOLDEN_DIR / "surfaces_multi"

REGENERATE = (
    "If this change was intended, regenerate deliberately with\n"
    "    .venv/bin/python -m tests.fixtures.generate_goldens_m10c\n"
    "and justify the diff in the milestone report. These pin what switching forex on "
    "does to crypto's surfaces; a diff here means switch-on day is no longer a no-op."
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

#: The surfaces a second market actually tags. specs/TELEGRAM_UX.md §3e settles the
#: other two: a symbol names its own market, so ``/pulse SYMBOL`` and ``/snapshot``
#: never carry a header a reader would have had to ask for.
TAGGED = ("status", "stats", "pulse", "pulse_24h", "positions", "watchlist")
UNTAGGED = ("pulse_symbol", "snapshot")


@pytest.fixture(scope="module")
def multi() -> dict[str, Any]:
    """The shipped config, which since M10d **is** the multi-market one.

    It was ``multi_market(load_config())`` while forex shipped disabled. Leaving it
    that way would have been harmless and useless: with the flag already on, the
    helper is a no-op and this fixture would still pass while pinning nothing about
    the config that actually ships.
    """
    return render_surfaces(load_config())


@pytest.fixture(scope="module")
def single() -> dict[str, Any]:
    """Forex switched back off in memory — the other half of the comparison.

    It was a bare ``load_config()``. After switch-on that made ``single`` and
    ``multi`` the *same config*, so
    ``test_every_multi_market_surface_differs_by_its_header_alone`` compared a string
    with itself and could never fail again — the exact shape of a test that looks
    like it is checking and is not (journal/M10c_REPORT.md §3).
    """
    return render_surfaces(single_market(load_config()))


# --------------------------------------------------------------------------- #
# The bytes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", TEXT_SURFACES)
def test_the_multi_market_surface_is_unchanged(multi: dict[str, Any], name: str) -> None:
    assert multi[name] == (MULTI_DIR / f"{name}.txt").read_text(encoding="utf-8"), REGENERATE


def test_the_multi_market_journal_is_unchanged(multi: dict[str, Any]) -> None:
    golden = json.loads((MULTI_DIR / "journal.json").read_text(encoding="utf-8"))
    assert multi["journal"] == golden, REGENERATE


def test_the_tagged_card_is_unchanged() -> None:
    config = load_config()
    snapshot = golden_snapshot()
    features = golden_features(snapshot, config)
    decision = golden_gate(golden_report(APPROVED_REPORT, config), snapshot, features, config)

    card = golden_card(decision, show_market=True)

    assert card == (GOLDEN_DIR / "card_multi.txt").read_text(encoding="utf-8"), REGENERATE


# --------------------------------------------------------------------------- #
# The relationship — what makes the bytes above worth having
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", TAGGED)
def test_every_multi_market_surface_differs_by_its_header_alone(
    multi: dict[str, Any], single: dict[str, Any], name: str
) -> None:
    """specs/TELEGRAM_UX.md §3e's promise, asserted rather than assumed.

    "With one market enabled, every surface renders exactly the text it rendered
    before M10a" is the rail the live system rests on. Its other half — that a second
    market adds a header and changes nothing else — has never been pinned, and it is
    the half switch-on day depends on.
    """
    header = "<b>— CRYPTO —</b>\n"

    assert multi[name].startswith(header), f"{name} is expected to carry a market header"
    assert multi[name].removeprefix(header) == single[name]


@pytest.mark.parametrize("name", UNTAGGED)
def test_a_symbol_scoped_surface_carries_no_header(
    multi: dict[str, Any], single: dict[str, Any], name: str
) -> None:
    """§3e: the **symbol** names its market, so a reader never types one — and a header
    there would be answering a question nobody asked."""
    assert multi[name] == single[name]


def test_the_tagged_card_differs_from_the_untagged_one_by_the_tag_alone() -> None:
    """The same property on the card, which is the surface that matters most."""
    config = load_config()
    snapshot = golden_snapshot()
    features = golden_features(snapshot, config)
    decision = golden_gate(golden_report(APPROVED_REPORT, config), snapshot, features, config)

    tagged = golden_card(decision, show_market=True)
    plain = golden_card(decision)

    assert tagged.replace("CRYPTO · ", "", 1) == plain


def test_the_shipped_config_is_multi_market_and_the_goldens_know_which_is_which() -> None:
    """M10c's exit criterion, **inverted at M10d — and the inversion is switch-on.**

    It read ``config.multi_market is False``, and while that held, ``surfaces/``
    described what the deployed system renders and ``surfaces_multi/`` described what
    it would render one day. Those roles have now swapped, and the swap is the whole
    milestone: the shipped config enables both markets, so ``surfaces_multi/`` is the
    live set and ``surfaces/`` is the historical one, still pinned because "with one
    market every surface renders exactly what it rendered before M10a" is the promise
    specs/TELEGRAM_UX.md §3e makes and it does not stop being worth checking.

    Asserted here rather than only in the fixtures, because a fixture that renders
    from the wrong config still renders *something* — and something is what a golden
    cannot tell you is wrong.
    """
    config = load_config()

    assert config.multi_market is True
    assert config.enabled_markets == (Market.CRYPTO, Market.FOREX)
    assert config.market(Market.FOREX).enabled is True
    assert single_market(config).multi_market is False


def test_the_multi_market_set_covers_every_pinned_surface(multi: dict[str, Any]) -> None:
    """A meta-test, in the manner of the single-market one it mirrors.

    Adding a surface to ``render_surfaces`` without pinning both forms would leave a
    hole that looks exactly like coverage on switch-on day.
    """
    assert set(multi) == {*TEXT_SURFACES, "journal"}
    assert set(TAGGED) | set(UNTAGGED) == set(TEXT_SURFACES)
    on_disk = {path.stem for path in MULTI_DIR.iterdir() if path.is_file()}
    assert on_disk == {*TEXT_SURFACES, "journal"}

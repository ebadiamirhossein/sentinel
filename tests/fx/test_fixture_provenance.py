"""Every Saxo fixture must state exactly what it is (owner requirement R-d).

The spike's 89 raw JSON evidence files were deleted along with its throwaway scripts
(commit 4c90819). Dropping the scripts was right; the raw responses were evidence and
should have been kept. So every Saxo fixture here was hand-authored **from the same
document the code was written from** — and if the spike had misread the API anywhere,
the fixture would reproduce the misreading and the test would pass against a wrong
world. That is how the pip bug nearly survived.

**Closed on 2026-08-21.** The owner ran
``python -m sentinel.tools.saxo_record_fixtures --login`` against the live API and every
asserted value matched: Uics 21/31/42, ``Format.Decimals`` 4/4/2, the derived pips,
``TickSize``, ``MinimumTradeSize`` 1000.0, and the chart row keys. The fixtures are now
**VERIFIED** rather than merely reconstructed.

The distinction between *verified* and *recorded* is kept rather than collapsed, because
they are not the same claim: these files' values have been checked against reality, but
the files are still not captures — the chart fixture's price levels, for instance, remain
invented, and only its keys, window and spreads were confirmed. A fixture that overstated
itself would be the same class of problem as one that said nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import CASSETTE_DIR, cassette

SAXO_FIXTURES = sorted(CASSETTE_DIR.glob("saxo_*.json"))


def test_there_are_saxo_fixtures_to_check() -> None:
    """Without this, an empty glob would make every assertion below vacuous."""
    assert len(SAXO_FIXTURES) >= 4, [p.name for p in SAXO_FIXTURES]


#: What a fixture may claim to be, in ascending order of evidence.
#:
#: RECONSTRUCTED — hand-authored from the findings, never checked against the API.
#: VERIFIED      — hand-authored, then checked field by field against the live API.
#: RECORDED      — a capture, written by the recorder tool.
STATUSES = frozenset({"RECONSTRUCTED", "VERIFIED", "RECORDED"})


@pytest.mark.parametrize("path", SAXO_FIXTURES, ids=lambda p: p.name)
def test_a_saxo_fixture_states_exactly_what_it_is(path: Path) -> None:
    payload = cassette(path.name)
    provenance = payload.get("_provenance")
    assert isinstance(provenance, dict), f"{path.name} carries no _provenance block"

    status = provenance.get("status")
    assert status in STATUSES, f"{path.name} claims status {status!r}"

    note = provenance.get("note", "")
    assert "tools/saxo_record_fixtures.py" in note or "saxo_record_fixtures" in note, (
        f"{path.name} must point at the way to re-verify or replace it"
    )

    source = provenance.get("source", "")
    assert "M10b_SPIKE.md" in source, (
        f"{path.name} must name the section of the findings it was derived from"
    )
    assert provenance.get("values_from_spike"), (
        f"{path.name} must list which values carry evidential weight — the rest is shape"
    )

    if status == "RECONSTRUCTED":
        assert "NOT RECORDED" in note, f"{path.name} must say plainly it is not a capture"
    if status == "VERIFIED":
        # A claim of verification has to carry a date, or it is a claim about nothing.
        assert provenance.get("verified_at"), f"{path.name} claims VERIFIED with no date"
        assert provenance.get("verified_against"), f"{path.name} must say verified against what"
        assert "not a raw capture" in note, (
            f"{path.name} is verified but not recorded, and must not blur the two"
        )


def test_the_fixtures_are_verified_against_the_live_api() -> None:
    """The state as of 2026-08-21, pinned so a regression to unverified is visible.

    If a fixture is ever edited by hand again it should drop back to RECONSTRUCTED and
    fail here, rather than keeping a verification badge it no longer deserves.
    """
    for path in SAXO_FIXTURES:
        provenance = cassette(path.name)["_provenance"]
        assert provenance["status"] == "VERIFIED", path.name
        assert provenance["verified_at"] == "2026-08-21", path.name


def test_the_chart_fixture_still_admits_its_prices_are_invented() -> None:
    """Verification covered the row keys, the window and the spreads — not the levels.

    A fixture that let a live check upgrade *everything* in it would be overstating
    itself, which is the same class of problem as one that said nothing at all.
    """
    provenance = cassette("saxo_chart_EURUSD_60.json")["_provenance"]
    assert "invented" in provenance["synthetic"]
    assert "row KEYS" in provenance["synthetic"]


def test_the_chart_fixture_records_null_chart_info_not_an_empty_object() -> None:
    """Owner correction, 2026-08-21: ``ChartInfo`` and ``DisplayAndFormat`` come back
    **null**, not ``{}`` as the spike recorded. D-h's wording is corrected in FOREX.md.

    It changes nothing in the adapter — precision comes from reference data and these
    are never read — but a fixture that disagreed with reality on a field the spec
    names would be exactly the trap the provenance discipline exists to prevent.
    """
    chart = cassette("saxo_chart_EURUSD_60.json")
    assert chart["ChartInfo"] is None
    assert chart["DisplayAndFormat"] is None

"""The image and ``.venv`` must resolve the same version of every pinned dependency.

``make check-image`` runs this for real against a built image. This file tests the
**comparison**, which is the part most likely to be wrong: the plumbing either produces
JSON or it does not, and the assertion is where a subtle mistake would hide and still
look like a working gate.

The two reconstructions are the point. A checker never seen to fail is
indistinguishable from one that cannot fail — this project's own named failure mode —
so both defects that motivated this check are rebuilt here and the check is asserted to
catch each.
"""

from __future__ import annotations

import pytest

from sentinel.tools.check_pins import (
    PROBE,
    Divergence,
    compare,
    normalise,
    normalised,
    report,
    requirements,
)


def test_it_catches_the_anthropic_defect_it_was_written_for() -> None:
    """journal/M10b_REPORT.md §3: .venv 0.122.0, a fresh image 1.0.0, a pinned package.

    A **major** bump on the one dependency that talks to the model, arriving as a side
    effect of the next deploy. The suite validated a version production did not run.
    """
    divergences = compare(
        {"anthropic": "0.125.0"}, {"anthropic": "0.122.0"}, {"anthropic": "1.0.0"}
    )
    failures, warnings = report(divergences)

    assert warnings == []
    assert len(failures) == 1
    assert "0.122.0" in failures[0] and "1.0.0" in failures[0], "both versions must be named"


def test_it_catches_the_numpy_defect_it_was_written_for() -> None:
    """journal/M10c_REPORT.md §12: .venv 2.2.6 against an image at 2.5.2.

    **Unpinned at the time**, which is why this warns rather than fails — and the
    warning is the form in which this check would have surfaced it before anybody
    pinned it. Failing on a ``>=`` would make the gate lie about its own declaration.
    """
    divergences = compare({"numpy": None}, {"numpy": "2.2.6"}, {"numpy": "2.5.2"})
    failures, warnings = report(divergences)

    assert failures == []
    assert len(warnings) == 1
    assert "2.2.6" in warnings[0] and "2.5.2" in warnings[0]


def test_a_package_missing_from_the_image_is_a_divergence() -> None:
    """The httpx shape (M10b-1 §2): present in .venv, absent from a fresh image.

    ``check-image``'s in-image import catches this one at boot; this catches it by
    name, which is a better error than ``ModuleNotFoundError`` three layers down.
    """
    divergences = compare({"httpx": "0.28.1"}, {"httpx": "0.28.1"}, {"httpx": None})
    failures, _ = report(divergences)

    assert len(failures) == 1
    assert "NOT INSTALLED" in failures[0]


def test_agreement_is_silent() -> None:
    """The non-vacuity control: a check that always reports is a check nobody reads."""
    declared = {"anthropic": "0.125.0", "numpy": None}
    same = {"anthropic": "0.125.0", "numpy": "2.2.6"}

    assert compare(declared, same, same) == []


def test_only_exact_pins_fail() -> None:
    """``>=`` and ``~=`` both permit movement; treating either as a pin would fail the
    gate for doing exactly what the declaration allows.

    ``ccxt`` and ``pandas`` were the unpinned half of this test until 2026-08-21, when
    the hygiene session pinned both (journal/HYGIENE_2026-08-21.md §2). ``uvicorn`` is
    now the unpinned control, and it is deliberately still unpinned rather than merely
    not got round to: it serves a loopback ``/health`` endpoint and touches no number
    the product produces, and one live divergence keeps the WARN branch of this check
    observable instead of leaving it exercised only by the synthetic cases below.
    """
    declared = requirements()

    assert declared["anthropic"] == "0.125.0"
    assert declared["numpy"] == "2.2.6"
    assert declared["pandas"] == "3.0.5"
    assert declared["ccxt"] == "4.5.75"
    assert declared["uvicorn"] is None


def test_names_are_normalised_on_both_sides() -> None:
    """``PyYAML`` in the image's metadata and ``pyyaml`` in pyproject are one package.

    Without this the gate would report a divergence for every differently-cased
    distribution — noise that teaches people to ignore it.
    """
    assert normalise("PyYAML") == "pyyaml"
    assert normalise("annotated_types") == "annotated-types"
    assert normalised({"PyYAML": "6.0.3"}) == {"pyyaml": "6.0.3"}

    assert compare({"pyyaml": None}, {"pyyaml": "6.0.3"}, normalised({"PyYAML": "6.0.3"})) == []


def test_every_declared_dependency_is_covered() -> None:
    """The comparison must span the whole runtime list, not a hand-kept subset.

    A curated list is how a new pin gets added without the gate ever hearing about it.
    """
    declared = requirements()

    assert len(declared) > 15
    assert {"anthropic", "ccxt", "httpx", "matplotlib", "mplfinance", "numpy", "pandas"} <= set(
        declared
    )


def test_dev_extras_are_excluded() -> None:
    """They are not installed in the image, so comparing them would compare a version
    against nothing. That a dev extra *shapes* what the image resolves is the whole
    point of this check, and it is caught through the packages it constrains —
    ``numpy`` — rather than through ``pandas-ta`` itself."""
    declared = requirements()

    assert "pandas-ta" not in declared
    assert "pytest" not in declared
    assert "numba" not in declared


def test_the_probe_reports_this_environment() -> None:
    """The probe runs inside the image, where nothing can debug it — so it is exercised
    here, against the interpreter running the tests."""
    scope: dict[str, object] = {}
    captured: list[str] = []
    exec(PROBE.replace("print(", "captured.append("), {"captured": captured}, scope)

    import json

    reported = json.loads(captured[0])
    assert normalise("numpy") in {normalise(name) for name in reported}
    assert reported


@pytest.mark.parametrize(
    ("pinned", "expected"),
    [("1.2.3", True), (None, False)],
)
def test_a_divergence_knows_whether_it_is_fatal(pinned: str | None, expected: bool) -> None:
    assert Divergence(name="x", pinned=pinned, local="1", image="2").is_pinned is expected

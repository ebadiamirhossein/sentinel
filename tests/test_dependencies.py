"""Every third-party module ``sentinel/`` imports must be a declared dependency.

The check itself lives in ``sentinel/tools/check_deps.py`` and runs as
``make check-deps``. This file is here because a checker that has never been seen to
fail is indistinguishable from one that cannot — the project's own named failure mode
(journal/M8_REPORT.md's "silence-as-success"). So the non-vacuity tests below
reconstruct the exact defect that motivated it: ``httpx``, imported by six modules and
declared by none, working perfectly right up until an unrelated upstream change.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from sentinel.tools.check_deps import (
    declared_distributions,
    imported_modules,
    undeclared,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_every_third_party_import_in_sentinel_is_declared() -> None:
    """The check. On its first ever run it found two defects besides httpx —
    ``numpy`` in the indicators and ``annotated_types`` in ``/pulse`` — both imported
    directly and both arriving transitively."""
    findings = undeclared()
    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_the_check_catches_the_defect_it_was_written_for(tmp_path: Path) -> None:
    """``httpx``, imported directly and declared nowhere. The M10b-1 defect, replayed.

    Nothing else in this repository could raise it: tests pass, mypy passes and the
    wheel builds, because all three run somewhere httpx happens to be installed.
    """
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "client.py").write_text("import httpx\n", encoding="utf-8")
    without_httpx = tmp_path / "pyproject.toml"
    without_httpx.write_text(
        textwrap.dedent("""
            [project]
            name = "sentinel"
            dependencies = ["pydantic>=2.9"]
        """),
        encoding="utf-8",
    )

    findings = undeclared(root=package, pyproject=without_httpx)
    assert [finding.module for finding in findings] == ["httpx"]
    assert "not declared" in str(findings[0])
    assert "pkg/client.py" in str(findings[0])


def test_a_module_that_is_not_installed_at_all_is_reported_too(tmp_path: Path) -> None:
    """ "Cannot be traced" and "is declared" must not be the same answer."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "thing.py").write_text("import a_module_nobody_has\n", encoding="utf-8")
    findings = undeclared(root=package, pyproject=PYPROJECT)
    assert [finding.module for finding in findings] == ["a_module_nobody_has"]
    assert findings[0].distribution is None
    assert "not installed" in str(findings[0])


def test_stdlib_and_first_party_imports_are_not_dependencies(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text(
        "import json\nimport asyncio\nfrom decimal import Decimal\n"
        "from sentinel.core.markets import Market\nfrom . import sibling\n",
        encoding="utf-8",
    )
    assert undeclared(root=package, pyproject=PYPROJECT) == []


def test_declared_names_are_normalised_and_extras_are_stripped() -> None:
    """``sqlalchemy[asyncio]>=2.0.36`` is a dependency on ``sqlalchemy``, and
    ``PyYAML`` and ``pyyaml`` are one distribution under two spellings."""
    declared = declared_distributions(PYPROJECT)
    assert {"sqlalchemy", "uvicorn", "pyyaml", "anthropic", "httpx"} <= declared
    assert not [name for name in declared if "[" in name or ">" in name or "=" in name]


def test_imports_inside_type_checking_blocks_still_count() -> None:
    """``ingestion/models.py`` imports pandas that way, and pandas is unambiguously a
    runtime dependency of the feature engine."""
    modules = imported_modules()
    assert "pandas" in modules
    assert any("ingestion/models.py" in site for site in modules["pandas"])


def test_the_six_httpx_importers_are_all_still_seen() -> None:
    """The scan reads the source tree, so it cannot be fooled by what is installed."""
    modules = imported_modules()
    assert len(set(modules["httpx"])) >= 6, modules["httpx"]
    assert any("llm/client.py" in site for site in modules["httpx"])

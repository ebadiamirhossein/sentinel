"""Every third-party module ``sentinel/`` imports must be a declared dependency.

    python -m sentinel.tools.check_deps        # part of `make check`

**Why this exists (owner ruling, 2026-08-21).** Six modules imported ``httpx``
directly — ``ingestion/http.py``, ``llm/client.py``, ``core/wiring.py`` and three
tools — and no dependency list mentioned it. It was always installed, arriving
transitively through ``anthropic``, so nothing anywhere was wrong until ``anthropic``
1.0.0 moved to ``httpx2``. A freshly resolved image then had no ``httpx`` at all and
the app died at boot on ``import sentinel.main``.

The defect had been latent for nine milestones, and what finally surfaced it was an
**unrelated upstream change**. Nothing in this repository could have raised it: tests
pass, mypy passes and the wheel builds, because all three run somewhere httpx happens
to be installed. Only ``make check-image`` caught it, and only because
journal/M7_REPORT.md §6 had made that target *import the app* rather than merely build
it — and it caught it a rebuild too late to be comfortable.

This is the same lesson one layer earlier: **an undeclared direct dependency is a
latent break whichever way the transitive chain moves next**, so the declaration is
what gets checked, not the installation.

The check runs on the source tree rather than on a resolved environment, so it fails
in CI on the commit that introduces the import rather than on some later day when a
third party reorganises its own requirements.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "sentinel"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Import names that are ours. Never third party, never declared.
FIRST_PARTY = frozenset({"sentinel", "tests"})


@dataclass(frozen=True)
class Finding:
    """One import that cannot be traced to a declared dependency."""

    module: str
    distribution: str | None
    where: tuple[str, ...]

    def __str__(self) -> str:
        sites = ", ".join(self.where[:4])
        more = f" (+{len(self.where) - 4} more)" if len(self.where) > 4 else ""
        if self.distribution is None:
            return (
                f"{self.module!r} is imported by {sites}{more} and is not installed, so "
                f"it cannot be traced to a distribution at all"
            )
        return (
            f"{self.module!r} (from the {self.distribution!r} distribution) is imported "
            f"by {sites}{more} but is not declared in pyproject.toml"
        )


def imported_modules(root: Path = PACKAGE_DIR) -> dict[str, list[str]]:
    """Every top-level module name imported under ``root``, and where from.

    Imports inside ``if TYPE_CHECKING`` count. They are real dependencies of the type
    checker and of anybody reading the file, and ``pandas`` — imported exactly that
    way in ``ingestion/models.py`` — is unambiguously a runtime dependency of the
    feature engine regardless.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = _display(path, root)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], []).append(where)
            # `level > 0` is a relative import, which is first-party by definition.
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.setdefault(node.module.split(".")[0], []).append(where)
    return found


def _display(path: Path, root: Path) -> str:
    """A readable path for an error message, whatever ``root`` was pointed at.

    Repo-relative for the real run; ``root``-relative for a test pointing at a
    temporary directory, which is not under the repository at all.
    """
    for base in (REPO_ROOT, root.parent):
        try:
            return str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def declared_distributions(pyproject: Path = PYPROJECT) -> set[str]:
    """Distribution names from ``[project].dependencies``, normalised.

    Optional-dependency groups are deliberately **not** included: a runtime import
    satisfied only by a dev extra is exactly the same latent break, one release of
    somebody else's package away.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requirements: Iterable[str] = data.get("project", {}).get("dependencies", [])
    return {_normalise(_requirement_name(requirement)) for requirement in requirements}


def _requirement_name(requirement: str) -> str:
    """``sqlalchemy[asyncio]>=2.0.36`` -> ``sqlalchemy``."""
    name = requirement.strip()
    for separator in ("[", ";", "=", ">", "<", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return name.strip()


def _normalise(name: str) -> str:
    """PEP 503 normalisation — ``PyYAML``, ``pyyaml`` and ``py_yaml`` are one name."""
    return name.lower().replace("_", "-").replace(".", "-")


def undeclared(root: Path = PACKAGE_DIR, pyproject: Path = PYPROJECT) -> list[Finding]:
    """Third-party imports under ``root`` that ``pyproject`` does not declare."""
    declared = declared_distributions(pyproject)
    provided_by = packages_distributions()
    findings: list[Finding] = []

    for module, sites in sorted(imported_modules(root).items()):
        if module in FIRST_PARTY or module in sys.stdlib_module_names:
            continue
        distributions = provided_by.get(module)
        if not distributions:
            findings.append(Finding(module, None, tuple(sorted(set(sites)))))
            continue
        # A module can be provided by more than one distribution; declaring any of
        # them is what makes the import safe.
        if any(_normalise(dist) in declared for dist in distributions):
            continue
        findings.append(Finding(module, distributions[0], tuple(sorted(set(sites)))))
    return findings


def main() -> int:
    findings = undeclared()
    if not findings:
        seen = len(imported_modules())
        print(f"dependencies: every third-party import in sentinel/ is declared ({seen} seen)")
        return 0
    print("UNDECLARED DEPENDENCIES\n", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "\nAdd each to [project].dependencies in pyproject.toml. An import that is only "
        "satisfied transitively works until the package in the middle reorganises its "
        "own requirements — which is how `httpx` broke the image in M10b-1.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

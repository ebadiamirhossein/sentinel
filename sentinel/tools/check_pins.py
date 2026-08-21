"""The image and ``.venv`` must resolve the **same version** of every pinned dependency.

    docker run --rm --entrypoint python sentinel:check \
        -c '<print installed versions as JSON>' \
      | python -m sentinel.tools.check_pins -        # part of `make check-image`

**Why this exists (owner ruling, 2026-08-21, M10c).** ``check_deps`` asks whether every
import is *declared*. That is a question about the source tree, and it is answered
hermetically, which is what makes it fast. This asks a different question — whether two
environments *resolve* the same version — and that one cannot be answered without
building both. So it lives here, beside the image build, rather than being bolted onto
a check whose whole value is that it needs nothing.

Two defects, one assertion. Both were found late and by accident:

* **anthropic** (journal/M10b_REPORT.md §3). The declaration was ``>=0.122``, so the
  server ran 0.125.0, the test venv ran 0.122.0 and a freshly built image resolved
  **1.0.0** — a major bump on the one dependency that talks to the model, arriving as a
  silent side effect of the next deploy. The suite validated a version production does
  not run.
* **numpy** (journal/M10c_REPORT.md §12). ``.venv`` resolved 2.2.6 and the image
  resolved **2.5.2**, because ``pandas-ta`` — a *test oracle*, a **dev extra** — pulls
  ``numba``, which refuses NumPy above 2.2. A dev-only package was silently
  version-controlling the numerical core of the product, and numpy sits under pinned
  matplotlib and mplfinance and under every RSI, ATR and EMA value.

**What makes this class invisible is that the constraint is invisible where it
matters.** Tests, mypy, ``check-deps`` and ``check-wheel`` all run in the environment
the dev extras shape. The image is the only place they are absent, and until now it was
the one place nothing compared versions. Named-but-unbuilt is how a known gap becomes a
surprise, so it is built.

**Pinned dependencies FAIL; unpinned ones WARN.** A ``>=`` declaration is an explicit
statement that the version is allowed to move, and failing on it would make the gate
lie about its own intent. But a divergence there is exactly how the numpy incident
looked *before* anybody pinned it — so it is reported, loudly, in the same place. That
warning is the form in which this check would have caught the original.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: What the image runs to report itself. It emits **every** installed distribution
#: rather than a requested subset, so the Makefile passes no argument list that could
#: fall out of step with ``pyproject.toml`` — and so a package that vanished from the
#: image entirely (the httpx defect) shows up as ``NOT INSTALLED`` rather than as a
#: name the probe was never asked about.
#:
#: Names are PEP 503-normalised on both sides. ``PyYAML`` and ``pyyaml`` are one
#: distribution, and a check that reported them as a divergence would be noise that
#: taught people to ignore it.
PROBE = (
    "import json,re;"
    "from importlib.metadata import distributions;"
    "print(json.dumps({"
    "re.sub(r'[-_.]+','-',d.metadata['Name']).lower(): d.version"
    " for d in distributions() if d.metadata['Name']}))"
)

MISSING = "NOT INSTALLED"


def normalise(name: str) -> str:
    """PEP 503 — ``PyYAML``, ``pyyaml`` and ``py_yaml`` are one name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Divergence:
    """One dependency the two environments disagree about."""

    name: str
    pinned: str | None
    local: str | None
    image: str | None

    @property
    def is_pinned(self) -> bool:
        return self.pinned is not None

    def __str__(self) -> str:
        local = self.local or MISSING
        image = self.image or MISSING
        where = f"pinned {self.pinned}" if self.pinned else "unpinned"
        return f"{self.name} ({where}): .venv has {local}, the image has {image}"


def requirements(pyproject: Path = PYPROJECT) -> dict[str, str | None]:
    """Every runtime dependency, mapped to its exact pin or ``None`` if unpinned.

    ``[project.optional-dependencies]`` is deliberately excluded: dev extras are not
    installed in the image, so comparing them would compare a version against nothing.
    That they *shape* what the image resolves is the whole point of this check and is
    caught through the packages they constrain, not through the packages themselves.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    declared: Iterable[str] = data.get("project", {}).get("dependencies", [])
    out: dict[str, str | None] = {}
    for requirement in declared:
        name, pin = _split(requirement)
        out[normalise(name)] = pin
    return out


def _split(requirement: str) -> tuple[str, str | None]:
    """``anthropic==0.125.0`` -> ``("anthropic", "0.125.0")``; ``numpy>=2.0`` -> pin None.

    Only ``==`` counts as a pin. ``~=`` and ``>=`` both permit movement, and treating
    either as a pin would fail the gate for doing what the declaration allows.
    """
    text = requirement.split(";", 1)[0].strip()
    if "==" in text:
        name, _, pin = text.partition("==")
        return _name(name), pin.strip()
    return _name(text), None


def _name(text: str) -> str:
    name = text.strip()
    for separator in ("[", "=", ">", "<", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return name.strip()


def installed_locally(names: Iterable[str]) -> dict[str, str | None]:
    """What **this** interpreter has — i.e. ``.venv``, the environment tests ran in."""
    out: dict[str, str | None] = {}
    for name in names:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def normalised(payload: Mapping[str, str | None]) -> dict[str, str | None]:
    """The image's report, keyed the way :func:`requirements` keys its own."""
    return {normalise(name): value for name, value in payload.items()}


def compare(
    declared: Mapping[str, str | None],
    local: Mapping[str, str | None],
    image: Mapping[str, str | None],
) -> list[Divergence]:
    """Every dependency the two environments disagree about, pinned or not.

    A pure function of three mappings, so the interesting cases are unit-testable
    without building anything — which matters, because the thing most likely to be
    wrong here is the comparison itself rather than the plumbing around it.
    """
    return [
        Divergence(name=name, pinned=declared[name], local=local.get(name), image=image.get(name))
        for name in sorted(declared)
        if local.get(name) != image.get(name)
    ]


def report(divergences: Iterable[Divergence]) -> tuple[list[str], list[str]]:
    """``(failures, warnings)`` — pinned divergences fail, unpinned ones warn."""
    failures = [str(d) for d in divergences if d.is_pinned]
    warnings = [str(d) for d in divergences if not d.is_pinned]
    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "-":
        print("usage: <image version JSON on stdin> | python -m sentinel.tools.check_pins -")
        return 2

    image = normalised(json.loads(sys.stdin.read()))
    declared = requirements()
    local = installed_locally(declared)
    failures, warnings = report(compare(declared, local, image))

    for line in warnings:
        print(f"  warning: {line}")
    if warnings:
        print(
            "  ^ unpinned, so this is allowed — but it is exactly how the numpy defect\n"
            "    looked before it was pinned (journal/M10c_REPORT.md §12). Pin it or\n"
            "    decide not to, deliberately."
        )
    if failures:
        print("PINNED DEPENDENCY VERSIONS DISAGREE BETWEEN .venv AND THE IMAGE:")
        for line in failures:
            print(f"  {line}")
        print(
            "\nThe suite validates one of these and the server runs the other. Rebuild\n"
            "the venv (`make install`) or the image, or correct the pin — but do not\n"
            "ship a version nothing tested."
        )
        return 1

    checked = sum(1 for pin in declared.values() if pin is not None)
    print(f"pins agree: {checked} pinned dependencies identical in .venv and the image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

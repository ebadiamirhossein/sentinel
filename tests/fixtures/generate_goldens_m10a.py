"""Regenerate M10a's goldens. Run **deliberately**, never automatically.

    .venv/bin/python -m tests.fixtures.generate_goldens_m10a

A golden that regenerates itself when it fails is not a golden — it is a
rubber stamp. Nothing in the test suite calls this module; a human runs it, reads
the diff, and decides whether the change it records was intended.

During M10a specifically, a diff here is a **failure**: the milestone's whole claim
is that crypto output does not move. The only legitimate reason to run this file is
the first commit, when the goldens are created.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.core.config import load_config
from tests.golden.pipeline import run_golden_cycle, single_market
from tests.golden.surfaces import render_surfaces

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "golden_cycle"
SURFACE_DIR = GOLDEN_DIR / "surfaces"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def main() -> None:
    config = load_config(ROOT / "config.yaml")

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    SURFACE_DIR.mkdir(parents=True, exist_ok=True)

    cycle = run_golden_cycle(config)
    _write_json(GOLDEN_DIR / "features_BTCUSDT.json", cycle.features)
    _write_json(GOLDEN_DIR / "charts.json", cycle.charts)
    _write_text(GOLDEN_DIR / "prompt.txt", cycle.prompt)
    _write_json(GOLDEN_DIR / "gate_approved.json", cycle.gate_approved)
    _write_json(GOLDEN_DIR / "gate_rejected.json", cycle.gate_rejected)
    _write_text(GOLDEN_DIR / "card.txt", cycle.card)

    # `single_market(config)`, not a bare `render_surfaces()`, since M10d.
    #
    # This directory is the SINGLE-MARKET set — what every surface renders with one
    # market enabled, which specs/TELEGRAM_UX.md §3e still promises after switch-on.
    # The shipped config now enables two, so a bare call regenerates these six files
    # in the TAGGED form and silently destroys the distinction the whole
    # `surfaces/` vs `surfaces_multi/` split exists for. It did exactly that once,
    # during M10d, and only the sha256 manifest noticed.
    #
    # The tagged set has its own generator: `generate_goldens_m10c`.
    for name, rendered in render_surfaces(single_market(config)).items():
        if isinstance(rendered, str):
            _write_text(SURFACE_DIR / f"{name}.txt", rendered)
        else:
            _write_json(SURFACE_DIR / f"{name}.json", rendered)

    print(f"goldens written to {GOLDEN_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

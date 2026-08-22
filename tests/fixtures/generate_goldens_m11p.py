"""Generate the **shared-form** golden. Run deliberately, never automatically.

    .venv/bin/python -m tests.fixtures.generate_goldens_m11p

A separate module with a hard write-allowlist, following the precedent
``generate_goldens_m10c`` set: this one can write exactly one file, ``card_shared.txt``,
and ``card.txt``, ``card_multi.txt``, ``surfaces/`` and ``surfaces_multi/`` are
unreachable from it. The fixtures whose whole job is to prove crypto output did not move
must not be reachable from the module that adds a new one.

**Why this exists as an addition rather than a regeneration.** ``card_shared.txt`` is
what ``signal_card(..., shared_only=True)`` renders — the text a Persian summary is
written from, and therefore the text the model sees. It pins two things at once: that
the shared form's bytes are stable, and that adding the flag moved nothing in the full
form, because ``card.txt`` sits beside it untouched and both are produced from the same
gate decision in the same run.

**Which branches this golden reaches, and which it does not** (HANDOFF §4 item 10 — a
golden pins the case it was built from and nothing else). BTCUSDT at €10,000 reaches the
three-rung ladder and the ``shared_only`` branch of every line the flag touches. It does
**not** reach the two-rung ladder a small account produces, nor the forex renderer's own
flag. Both of those are covered by ``tests/bot/test_shared_card.py``, which asserts on
structure rather than on bytes — deliberately, because a byte golden of a card that
changes with capital would pin one account size and call it the truth.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.core.config import load_config
from tests.golden.pipeline import (
    APPROVED_REPORT,
    golden_card,
    golden_features,
    golden_gate,
    golden_report,
    golden_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "golden_cycle"
CARD_SHARED = GOLDEN_DIR / "card_shared.txt"


def _write(path: Path, payload: str) -> None:
    assert path == CARD_SHARED, f"{path} is not writable from this module"
    path.write_text(payload, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    config = load_config(ROOT / "config.yaml")
    snapshot = golden_snapshot()
    features = golden_features(snapshot, config)
    decision = golden_gate(golden_report(APPROVED_REPORT, config), snapshot, features, config)
    _write(CARD_SHARED, golden_card(decision, shared_only=True))


if __name__ == "__main__":
    main()

"""Generate the **multi-market** goldens. Run deliberately, never automatically.

    .venv/bin/python -m tests.fixtures.generate_goldens_m10c

A separate module from ``generate_goldens_m10a`` on purpose, and with a hard
write-allowlist, following the precedent ``generate_goldens_m10b2`` set: that one
writes all six cycle goldens and all nine surfaces, and this one **cannot touch any of
them**. The fixtures whose whole job is to prove crypto output did not move must not be
reachable from the module that refreshes a different set.

**Why these exist at all** (FOREX.md defect #23). The M10c brief required §11's
regeneration — switching forex on gives crypto cards their market tag, which changes
their bytes — and required ``markets.forex.enabled: false`` in the same breath. Both
cannot hold: with one market enabled ``AppConfig.multi_market`` is ``False``,
``section_header`` returns ``""``, and no crypto byte can move. There was nothing to
regenerate.

Owner ruling, 2026-08-21: **pin the tagged form as an addition instead.** These files
are what two enabled markets render, produced from the shipped config with forex
flipped on *in memory*. The shipped config is unchanged and still single-market, so
``test_the_deployed_config_renders_the_same_surface`` — the strongest assertion in the
suite — keeps holding. And switch-on day becomes a config flip with **zero** golden
churn, instead of a regeneration performed on the one day there is least attention to
spare for it.

Four surfaces differ under two markets, and it is worth knowing which and why: the four
that take a ``header=`` (``status``, ``stats``, ``positions``, ``watchlist``) plus
``pulse`` and ``pulse_24h``, which take one from their handlers. ``pulse_symbol`` and
``snapshot`` do not, because specs/TELEGRAM_UX.md §3e settles that a symbol names its
own market and a reader never types one.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.core.config import load_config
from tests.golden.pipeline import (
    APPROVED_REPORT,
    golden_card,
    golden_features,
    golden_gate,
    golden_report,
    golden_snapshot,
    multi_market,
)
from tests.golden.surfaces import render_surfaces

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "golden_cycle"
SURFACE_DIR = GOLDEN_DIR / "surfaces_multi"
CARD = GOLDEN_DIR / "card_multi.txt"


#: The only files this module may write. ``card.txt`` and everything under
#: ``surfaces/`` are deliberately unreachable from here.
def _allowed(path: Path) -> bool:
    return path == CARD or path.parent == SURFACE_DIR


def _write(path: Path, payload: object) -> None:
    assert _allowed(path), f"{path} is not writable from this module"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def main() -> None:
    config = multi_market(load_config(ROOT / "config.yaml"))
    SURFACE_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = golden_snapshot()
    features = golden_features(snapshot, config)
    decision = golden_gate(golden_report(APPROVED_REPORT, config), snapshot, features, config)
    _write(CARD, golden_card(decision, show_market=True))

    for name, rendered in render_surfaces(config).items():
        _write(
            SURFACE_DIR / (f"{name}.txt" if isinstance(rendered, str) else f"{name}.json"), rendered
        )


if __name__ == "__main__":
    main()

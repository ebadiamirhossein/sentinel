"""Write the **second** golden symbol's fixtures. Run deliberately, never automatically.

    .venv/bin/python -m tests.fixtures.generate_goldens_m10b2

A separate module from ``generate_goldens_m10a`` on purpose. That one writes all six
cycle goldens and all nine surfaces; this one writes **two files and cannot touch any
other**, so adding or refreshing the second symbol can never overwrite the fixtures
whose whole job is to prove crypto output did not move.

Owner requirement H1, 2026-08-21. ``charts.json`` pinned BTCUSDT alone — one symbol,
one price magnitude, above 1000. ``charts/renderer._format_price`` branches at 1000
and at 1, so the golden pinned one of three branches; a change to another would have
silently moved the stored bytes of every crypto chart priced between 1 and 1000, and
the suite would have stayed green. LINK, AVAX and LTC are all on the live watchlist.

A golden pins the case it was built from. This is the second case.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.core.config import load_config
from tests.golden.pipeline import SECOND_SYMBOL, golden_symbol_charts

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "golden_cycle"

#: The only two files this module may write, named rather than derived so a typo
#: cannot widen its blast radius.
WRITES = (f"features_{SECOND_SYMBOL}.json", f"charts_{SECOND_SYMBOL}.json")


def main() -> None:
    config = load_config(ROOT / "config.yaml")
    artefacts = golden_symbol_charts(config, SECOND_SYMBOL)

    for name, payload in zip(WRITES, (artefacts["features"], artefacts["charts"]), strict=True):
        (GOLDEN_DIR / name).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {(GOLDEN_DIR / name).relative_to(ROOT)}")


if __name__ == "__main__":
    main()

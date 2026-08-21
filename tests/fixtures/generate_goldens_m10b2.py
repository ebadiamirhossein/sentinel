"""Write the **second** golden symbol's fixtures. Run deliberately, never automatically.

    .venv/bin/python -m tests.fixtures.generate_goldens_m10b2

A separate module from ``generate_goldens_m10a`` on purpose. That one writes all six
cycle goldens and all nine surfaces; this one writes **two files and cannot touch any
other**, so adding or refreshing the second symbol can never overwrite the fixtures
whose whole job is to prove crypto output did not move.

Owner requirement H1, 2026-08-21. ``charts.json`` pinned BTCUSDT alone — one symbol,
one price magnitude, above 1000. ``charts/renderer._format_price`` branches at 1000
and at 1, so the golden pinned one of three branches; a change to either other one
would have silently moved the stored bytes of live watchlist symbols — LINK, AVAX and
LTC between 1 and 1000, XRP, DOGE and ADA below 1 — and the suite would have stayed
green.

A golden pins the case it was built from. These are the other two cases.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentinel.core.config import load_config
from tests.golden.pipeline import EXTRA_SYMBOLS, golden_symbol_charts

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "tests" / "fixtures" / "golden_cycle"

#: The only files this module may write, derived from the extra symbols alone. The
#: six fixtures BTCUSDT owns are not in this set and cannot be reached from here.
WRITES = tuple(
    f"{kind}_{symbol}.json" for symbol in EXTRA_SYMBOLS for kind in ("features", "charts")
)


def main() -> None:
    config = load_config(ROOT / "config.yaml")
    for symbol in EXTRA_SYMBOLS:
        artefacts = golden_symbol_charts(config, symbol)
        for kind in ("features", "charts"):
            name = f"{kind}_{symbol}.json"
            assert name in WRITES, name
            (GOLDEN_DIR / name).write_text(
                json.dumps(artefacts[kind], indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"wrote {(GOLDEN_DIR / name).relative_to(ROOT)}")


if __name__ == "__main__":
    main()

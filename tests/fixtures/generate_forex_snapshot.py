"""Write the forex snapshot ``sentinel.tools.forex_prompt_cost`` measures against.

    .venv/bin/python -m tests.fixtures.generate_forex_snapshot

Its own module with its own **hard write allowlist**, following
``generate_goldens_m10b2``: it writes exactly one file and cannot reach any of the 37
goldens whose whole job is to prove crypto's output did not move.

**Why a fixture at all, rather than the tool building its own.** The measurement had to
be reproducible without a Saxo browser login (journal/M10d_REPORT.md, join 4): the cost
of an analyst call is a fact about the *payload* — a system prompt, a rendered snapshot
and three 1600x1000 PNGs — not about whether the prices in it are real. So the shape is
produced by the real ``assemble_forex`` over the synthetic venue, and a ``_provenance``
header on the file says in the file itself that the prices are not evidence. The tool
refuses to read a snapshot without one.

**Why it is not trimmed**, since 230 KB of candles is not nothing: the charts draw
EMA200, which is undefined below 200 closed candles, so a shorter tail would render a
chart with a missing line — a different album, and therefore a different measurement
than the one the pipeline actually sends.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sentinel.core.config import load_config
from sentinel.core.forex_cycle import assemble_forex
from sentinel.core.markets import Market
from tests.core.saxo_double import NOW, SyntheticSaxo, build

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"

SYMBOL = "EURUSD"
#: **All three**, even though only one snapshot is written. The USD strength index and
#: the cross-pair correlations are computed once per cycle across the whole watchlist
#: (spec defect #18: two thirds of a dollar index is not a dollar index, so a missing
#: pair yields ``None``). Assembling EURUSD alone produced a payload ~280 tokens
#: smaller than the one a real cycle sends -- an under-measurement of the figure the
#: spend rails are set from, which is exactly the kind of quietly-wrong number this
#: milestone exists to stop shipping.
WATCHLIST = ["EURUSD", "GBPUSD", "USDJPY"]
#: The only file this module may write.
WRITES = (f"forex_snapshot_{SYMBOL}.json",)

PROVENANCE = {
    "status": "SYNTHETIC",
    "note": (
        "Assembled by sentinel.core.forex_cycle.assemble_forex over "
        "tests/core/saxo_double.SyntheticSaxo. The SHAPE and SIZE are real -- real "
        "venue grids, the real feature engine, a real spread profile, and the real "
        "renderer draws from it -- and the PRICES are not. It exists so that "
        "sentinel.tools.forex_prompt_cost can measure the forex prompt's token "
        "profile without a Saxo browser login, because the cost of a call is a fact "
        "about the payload rather than about whether its prices are real. Nothing "
        "here is evidence about EURUSD."
    ),
    "generated_by": "python -m tests.fixtures.generate_forex_snapshot",
    "now": NOW.isoformat(),
}


async def build_snapshot() -> dict[str, object]:
    config = load_config(ROOT / "config.yaml")
    config.markets[Market.FOREX] = config.markets[Market.FOREX].model_copy(update={"enabled": True})
    transport = SyntheticSaxo(now=NOW)
    adapter, client = build(transport)
    try:
        assembly = await assemble_forex(adapter, WATCHLIST, config=config, now=NOW)
    finally:
        await client.aclose()
    if assembly.skipped:
        raise SystemExit(f"the synthetic venue skipped: {assembly.skipped}")
    snapshot = next(row for row in assembly.snapshots if row.symbol == SYMBOL)
    payload: dict[str, object] = json.loads(snapshot.model_dump_json())
    payload["_provenance"] = PROVENANCE
    return payload


def main() -> None:
    payload = asyncio.run(build_snapshot())
    name = WRITES[0]
    assert name in WRITES, name
    path = FIXTURES / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

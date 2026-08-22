"""Join 4 of journal/M10c_REPORT.md §13: what the forex analyst prompt actually costs.

    python -m sentinel.tools.forex_prompt_cost --count            # free, no API call
    python -m sentinel.tools.forex_prompt_cost --call             # ONE real call

"The forex analyst prompt has still never been sent to the model. No NO_SETUP rate, no
token count, no cost figure." Every forex spend figure written before this ran was an
estimate back-derived from crypto's, and the rails switch-on sets depend on it.

**No Saxo browser login is needed, and that is the point.** The cost of a call is a
fact about the *payload* — the system prompt, the rendered snapshot and three PNGs —
not about whether the prices in it are real. So this reads a snapshot assembled by the
real ``assemble_forex`` over ``tests/core/saxo_double.SyntheticSaxo``: real grids, real
feature engine, real spread profile, real renderer, synthetic prices. The fixture's
provenance header says so and so does every figure printed below.

**What this cannot tell you**, printed with the results rather than buried here:

* the **verdict** is the model's answer to a synthetic market. It is not evidence about
  EURUSD, and one call is not a NO_SETUP *rate* at any rate — that needs many real
  cycles and arrives in the first hours after switch-on.
* the input count is honest to within the digit-count difference between synthetic and
  real prices, which is a fraction of a percent of a ~10k-token payload.

``--count`` uses the ``count_tokens`` endpoint, costs nothing, and is the half that
settles the input side. ``--call`` makes exactly **one** analyst call — there is no
loop and no retry here on purpose — and is the only way to learn the output length,
which is where most of the money is at $50/Mtok against $10/Mtok in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic

from sentinel.analyst.providers.anthropic_fable import AnthropicFableAnalyst, order_charts
from sentinel.charts.models import album_specs
from sentinel.charts.renderer import render_album
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.core.markets import Market
from sentinel.features.models import SymbolFeatures
from sentinel.ingestion.models import MarketSnapshot
from sentinel.llm.client import AnthropicClient

DEFAULT_SNAPSHOT = Path("tests/fixtures/forex_snapshot_EURUSD.json")


def load_snapshot(path: Path) -> MarketSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    provenance = payload.pop("_provenance", None)
    if provenance is None:
        raise SystemExit(
            f"{path} carries no _provenance header. Every fixture in this repository "
            f"says where it came from and whether it is a capture; one that does not "
            f"is a number nobody can check."
        )
    print(f"snapshot: {path}  [{provenance.get('status', '?')}]")
    print(f"  {provenance.get('note', '')}\n")
    return MarketSnapshot.model_validate(payload)


def build_blocks(settings: Settings, snapshot: MarketSnapshot) -> tuple[str, list[Any], int]:
    """The exact system prompt and user turn a forex cycle sends. Returns the album size."""
    config = settings.config
    features = SymbolFeatures.model_validate(
        {key: value for key, value in (snapshot.features or {}).items() if key != "forex"}
    )
    charts = list(render_album(snapshot, features, album_specs(config.charts, snapshot.symbol)))
    analyst = AnthropicFableAnalyst(
        AnthropicClient(config.llm, api_key="not-used-for-blocks"),
        config,
        prompt_version=config.market(Market.FOREX).analyst_prompt_version,
    )
    from sentinel.analyst.prompts.loader import load_prompt

    system = load_prompt(analyst.prompt_version)
    blocks = analyst.user_blocks(snapshot, order_charts(charts, snapshot.symbol), "")
    return system, blocks, sum(len(chart.png) for chart in charts)


async def count(settings: Settings, snapshot: MarketSnapshot) -> None:
    key = settings.secrets.anthropic_api_key
    if key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    system, blocks, png_bytes = build_blocks(settings, snapshot)
    text_chars = sum(len(block["text"]) for block in blocks if block["type"] == "text")

    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value())
    try:
        result = await client.messages.count_tokens(
            model=settings.config.llm.analyst_model,
            system=system,
            messages=[{"role": "user", "content": blocks}],
        )
    finally:
        await client.close()

    pricing = settings.config.llm.pricing.get(settings.config.llm.analyst_model)
    tokens = result.input_tokens
    images = sum(1 for block in blocks if block["type"] == "image")
    print(f"system prompt      {len(system):>9,} chars   ({analyst_version(settings)})")
    print(f"user text          {text_chars:>9,} chars   ({len(blocks) - images} text blocks)")
    print(f"charts             {png_bytes:>9,} bytes   ({images} images)")
    print(f"INPUT TOKENS       {tokens:>9,}   (count_tokens)")
    if pricing is not None:
        cost = Decimal(tokens) * pricing.input_per_mtok / Decimal(1_000_000)
        print(f"input cost         ${cost:>8.4f}   @ ${pricing.input_per_mtok}/Mtok, uncached")
    print(
        "\nTwo things this UNDER-states, so --call is the authoritative figure:\n"
        "  * the output schema travels in output_config.format and is not counted here;\n"
        "  * the system block carries cache_control, so calls 2 and 3 of a cycle read it\n"
        "    from cache at a tenth of the price while the first one pays a write."
    )


async def call(settings: Settings, snapshot: MarketSnapshot) -> None:
    key = settings.secrets.anthropic_api_key
    if key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    config = settings.config
    features = SymbolFeatures.model_validate(
        {k: v for k, v in (snapshot.features or {}).items() if k != "forex"}
    )
    charts = list(render_album(snapshot, features, album_specs(config.charts, snapshot.symbol)))

    client = AnthropicClient(config.llm, api_key=key.get_secret_value())
    analyst = AnthropicFableAnalyst(
        client, config, prompt_version=config.market(Market.FOREX).analyst_prompt_version
    )
    started = time.monotonic()
    verdict = "AnalystUnavailable"
    try:
        report = await analyst.analyze(snapshot, charts, "")
        verdict = report.candidate_status.value
    finally:
        elapsed = time.monotonic() - started
        await client.aclose()

    print(f"prompt_version     {analyst.prompt_version}")
    print(f"model              {config.llm.analyst_model}  effort={config.llm.analyst_effort}")
    print(f"verdict            {verdict}   <- about a SYNTHETIC market, not about EURUSD")
    print(f"latency            {elapsed:>8.1f} s")
    for index, made in enumerate(analyst.calls, start=1):
        use = made.usage
        print(
            f"call {index}  status={made.status.value}  "
            f"in={use.input_tokens:,}  out={use.output_tokens:,}  "
            f"cache_read={use.cache_read_tokens:,}  cache_write={use.cache_write_tokens:,}  "
            f"{made.duration_ms:,}ms  cost=${made.cost_usd_estimate}"
        )
    total = sum((made.cost_usd_estimate for made in analyst.calls), Decimal(0))
    print(f"\nPER-CALL COST       ${total}")
    print(f"per cycle (3 pairs) ${total * 3}")
    print(
        "\nThe first call of a cycle pays a cache WRITE on the system prompt and the\n"
        "next two read it back, so a cycle costs less than three times this figure.\n"
        "The verdict above is about a synthetic market and is not a NO_SETUP rate."
    )


def analyst_version(settings: Settings) -> str:
    return settings.config.market(Market.FOREX).analyst_prompt_version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--count", action="store_true", help="count input tokens; no LLM call")
    parser.add_argument("--call", action="store_true", help="make ONE real analyst call")
    args = parser.parse_args()
    if not (args.count or args.call):
        parser.error("choose --count (free) or --call (one real analyst call)")

    configure_logging("WARNING", json_logs=False)
    settings = load_settings()
    snapshot = load_snapshot(args.snapshot)
    if args.count:
        asyncio.run(count(settings, snapshot))
    if args.call:
        asyncio.run(call(settings, snapshot))


if __name__ == "__main__":
    main()

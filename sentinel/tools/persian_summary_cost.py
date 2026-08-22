"""What one press of the 🇮🇷 فارسی button actually costs (M11p).

    python -m sentinel.tools.persian_summary_cost --call     # ONE real call

journal/M10d_REPORT.md §8's rule, obeyed rather than quoted: *price an LLM path from a
real call before setting a rail on it.* Four consecutive estimates of the forex prompt's
cost were wrong, all in the same direction, and a cost estimate that errs high argues
for a **higher spend rail** — a permanent widening bought with an imaginary number. So
``config.persian_summary``'s two caps are set from what this prints and not before.

**The input is the golden card**, ``tests/fixtures/golden_cycle/card_shared.txt``: the
bytes the real renderer produces from recorded real market data, in the ``shared_only``
form the handler sends. Not a hand-written mock — the shape, the length and the number
of numeric tokens are exactly what a live press would carry.

**What this cannot tell you**, printed with the results rather than buried here:

* one call is not a **failure rate**. Whether the model reliably obeys the numbers rule
  is a question about many calls, and the check is what makes being wrong survivable.
* the length of a Persian summary varies with how much the card had to say, so the
  output figure is one sample, not a mean.

There is no ``--count`` mode. The output side is where the money is here — a ~1k-token
card in against a few hundred tokens of Persian out — and ``count_tokens`` cannot see it.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from sentinel.analyst.persian.numbers import check_numbers, numeric_tokens
from sentinel.analyst.persian.summariser import PersianSummariser
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.llm.client import AnthropicClient

DEFAULT_CARD = Path("tests/fixtures/golden_cycle/card_shared.txt")


async def call(settings: Settings, card: str) -> None:
    key = settings.secrets.anthropic_api_key
    if key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    config = settings.config
    client = AnthropicClient(config.llm, api_key=key.get_secret_value())
    summariser = PersianSummariser(client, config)
    started = time.monotonic()
    try:
        result = await summariser.summarise(card, symbol="BTCUSDT")
    finally:
        elapsed = time.monotonic() - started
        await client.aclose()

    usage = result.call.usage
    print(f"prompt_version     {summariser.prompt_version}")
    print(f"model              {config.persian_summary.model}")
    print(f"outcome            {result.outcome.value}")
    settings_ = config.persian_summary
    print(f"latency            {elapsed:>8.1f} s   (timeout {settings_.timeout_seconds}s)")
    print(f"input              {usage.input_tokens:>8,} tokens")
    print(f"cache read         {usage.cache_read_tokens:>8,} tokens")
    print(f"output             {usage.output_tokens:>8,} tokens")
    print(
        f"summary length     {len(result.text):>8,} chars   (ceiling {settings_.max_output_chars})"
    )
    print(f"PER-PRESS COST     ${result.call.cost_usd_estimate}")

    check = check_numbers(card=card, summary=result.text)
    print(f"\nnumbers on the card    {len(set(numeric_tokens(card)))}")
    print(f"numbers in the summary {len(set(numeric_tokens(result.text)))}")
    print(f"numbers check          {'PASS' if check.ok else 'FAIL'} — {check.detail}")

    cap = config.persian_summary.daily_usd_cap
    per_press = result.call.cost_usd_estimate
    if per_press > 0:
        print(f"\npresses inside the ${cap} daily cap: {int(cap / per_press)}")
    print(
        "\nOne call is a COST, not a failure rate: whether the model reliably obeys the\n"
        "numbers rule is a question about many calls, and the rail is what makes being\n"
        "wrong survivable. The summary length is one sample and varies with the card."
    )
    print("\n" + "=" * 72)
    print(result.text if result.text else "(no text — see outcome above)")
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--call", action="store_true", help="make ONE real API call")
    args = parser.parse_args()

    configure_logging()
    settings = load_settings()
    card = args.card.read_text(encoding="utf-8")

    if not args.call:
        print(f"card               {args.card}  ({len(card):,} chars)")
        print(f"numeric tokens     {len(set(numeric_tokens(card)))}")
        print(
            "\nPass --call to measure. There is no free mode: the output side is where"
            "\nthe money is, and count_tokens cannot see it."
        )
        return
    asyncio.run(call(settings, card))


if __name__ == "__main__":
    main()

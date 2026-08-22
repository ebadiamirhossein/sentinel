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
of numeric tokens are exactly what a live press would carry. ``--card`` takes any
other rendered card and ``--verdict`` supplies the ``candidate_status`` the verdict rail
is checked against — which is what matters for a ``/pulse SYMBOL`` card, the only surface
where a non-CANDIDATE verdict is reachable at all.

**What this cannot tell you**, printed with the results rather than buried here:

* one call is not a **failure rate**. Whether the model reliably obeys the numbers and
  verdict rules is a question about many calls, and the checks are what make being wrong
  survivable. A refusal is printed in full, because a rejection rate nobody can diagnose
  is a number nobody can act on.
* the length of a Persian summary varies with how much the card had to say, so the
  output figure is one sample, not a mean.

There is no ``--count`` mode. The output side is where the money is here — a ~1k-token
card in against a few hundred tokens of Persian out — and ``count_tokens`` cannot see it.

**Every usage component is printed, cache write included.** It was not, for one
revision, and the omission immediately hid a real mechanism: the system prompt sat at
~700 tokens, Anthropic does not cache a block below 1,024, so ``cache_control`` was a
no-op and every press paid full input at $3/Mtok. The M11p H1 amendment pushed the block
to 1,031 tokens, caching switched on, and the per-press cost went **up** by the write
($3.75/Mtok) while a second press inside the five-minute window became much cheaper. A
cost line showing only ``input`` and ``cache read`` reports that change as a mystery.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from sentinel.analyst.persian.numbers import numeric_tokens
from sentinel.analyst.persian.summariser import PersianSummariser
from sentinel.core.config import Settings, load_settings
from sentinel.core.logging import configure_logging
from sentinel.llm.client import AnthropicClient

DEFAULT_CARD = Path("tests/fixtures/golden_cycle/card_shared.txt")


async def call(settings: Settings, card: str, candidate_status: str) -> None:
    key = settings.secrets.anthropic_api_key
    if key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set")

    config = settings.config
    client = AnthropicClient(config.llm, api_key=key.get_secret_value())
    summariser = PersianSummariser(client, config)
    started = time.monotonic()
    try:
        result = await summariser.summarise(
            card, symbol="BTCUSDT", candidate_status=candidate_status
        )
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

    # The rails' OWN verdicts, not a re-derivation. Re-running them here would report
    # whatever this file happens to compute rather than what the summariser decided —
    # and on a rejection it would run against text the summariser had already refused,
    # which is how a tool comes to print PASS about nothing.
    print(f"\nnumbers on the card    {len(set(numeric_tokens(card)))}")
    print(f"numbers in the summary {len(set(numeric_tokens(result.text)))}")
    if result.verdict is not None:
        state = "PASS" if result.verdict.ok else "FAIL"
        print(f"card verdict           {candidate_status}  ({result.verdict.expected.value})")
        print(f"verdict rail           {state} — {result.verdict.detail}")
    else:
        print("verdict rail           not reached")
    if result.check is not None:
        state = "PASS" if result.check.ok else "FAIL"
        print(f"numbers rail           {state} — {result.check.detail}")
    else:
        print("numbers rail           not reached — the verdict rail refused first")

    cap = config.persian_summary.daily_usd_cap
    per_press = result.call.cost_usd_estimate
    if per_press > 0:
        print(f"\npresses inside the ${cap} daily cap: {int(cap / per_press)}")
    print(
        "\nOne call is a COST, not a failure rate: whether the model reliably obeys the\n"
        "numbers and verdict rules is a question about many calls, and the rails are what\n"
        "make being wrong survivable. The length is one sample and varies with the card."
    )
    banner = "WHAT THE MODEL RETURNED" if result.ok else "REFUSED — NOT SENT, NOT STORED"
    print(f"\n===== {banner} " + "=" * max(0, 66 - len(banner)))
    print(result.text if result.text else "(the model returned nothing)")
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument(
        "--verdict",
        default="CANDIDATE",
        help=(
            "the card's candidate_status, as the handler reads it from the stored "
            "report. The verdict rail is checked against THIS, never against the card "
            "text and never against the model's answer."
        ),
    )
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
    asyncio.run(call(settings, card, args.verdict))


if __name__ == "__main__":
    main()

# SPEC — LLM Prompts (Screener & Deep Analyst)

Prompts are versioned files in `sentinel/analyst/prompts/`. Every stored report records `prompt_version`. Never edit a prompt in place — create `vN+1.md` so stats can compare versions.

---

## 1. Screener (cheap tier — `claude-sonnet-4-6`)

**Job:** cheap, fast triage of ALL watchlist symbols each cycle using compact numeric features only (no charts, no news bodies). Output: which symbols deserve expensive deep analysis.

**Input per symbol (compact JSON):** last price, % change 1h/4h/24h, RSI(1h/4h), EMA20/50/200 relation flags, relative volume, ATR%, funding rate, OI 24h change, F&G value, distance to nearest S/R, news_flag (count of fresh headlines < 6h).

**System prompt (v1):**

```
You are a triage screener in an automated market-research pipeline.
For each symbol snapshot, decide if it deserves deep analysis RIGHT NOW.

Mark interesting=true only if there is a concrete, present reason:
approaching/testing a key level, momentum shift with volume confirmation,
regime alignment across timeframes, funding/OI anomaly, or fresh relevant news
coinciding with a technical location. "Nothing is happening" is the expected
answer for most symbols most of the time.

Rules:
- Use ONLY the supplied fields. Never invent values.
- Expect to mark 0-3 symbols per batch. Marking many symbols is a failure.
- Output strict JSON array matching the provided schema. No prose.
```

**Output schema:** `[{symbol, interesting: bool, direction_hint: "long"|"short"|"unclear", reason: string(≤200)}]`

> **Correction (2026-08-19, from M8.2) — "0-3 per batch" was not a tight enough band, and
> `screener_v2` supersedes `screener_v1`.** Eight live cycles measured 18 interesting marks in
> 80 symbol-verdicts (22.5%, 2.25 per batch), **zero batches returning zero**, LINKUSDT escalated
> in 8 of 8 cycles and SOLUSDT in 6 of 8 — on standing conditions, on a 1h setup timeframe, at
> ~$0.28 a look. 61% of what it escalated came back `WATCHLIST` at an average confidence of 53.7,
> below the gate's own `min_confidence` of 60.
>
> 2.25 is *inside* "0-3", which is why the band alone was not enough: it has no floor, and nothing
> in v1 distinguished a **state** ("in an uptrend", "approaching resistance") from a **change**.
> `screener_v2` narrows calibration to 0-2, names zero as normal and frequent, states the measured
> downstream rejection rate in the prompt, and adds the one-hour test — *could I have written this
> same reason an hour ago?* `llm.screener_prompt_version` selects between the two and
> `screener_v1.md` is kept, so `/stats` can compare them. See journal/PROMPT_LOG.md.
>
> The complementary change is not a prompt at all: `SkipReason.RECENTLY_ANALYSED` (M8.2) stops a
> symbol being re-analysed within one setup-timeframe candle of a `WATCHLIST`/`NO_SETUP` verdict,
> which is the structural half of the same problem — the screener had no memory to be stricter with.

## 2. Deep Analyst (top tier — `claude-fable-5`, high effort, vision)

**Job:** full evidence synthesis for ONE symbol → falsifiable trade idea or NO_SETUP.

**Inputs:** full `MarketSnapshot` JSON + three chart PNGs (4h context, 1h primary, 15m timing) + config limits (min RR, max entry distance) + recent-history context block (see §3).

**System prompt (v1)** — improved from the reference guide: adds multi-timeframe protocol, evidence citation, self-check, and setup taxonomy:

```
You are the deep market-research analyst inside a controlled decision-support
pipeline. You do not execute trades, size positions, choose leverage, or
promise returns. A deterministic risk engine and a human run after you.

OBJECTIVE
Evaluate ONE symbol from the supplied snapshot and charts. Produce either a
falsifiable trade candidate or NO_SETUP.

HARD BOUNDARIES
1. Use ONLY supplied data: OHLCV features, chart images, news items, funding,
   OI, sentiment, timestamps. NEVER invent a price, level, indicator value,
   news item, or source.
2. Data marked stale/degraded, missing, or contradictory → be stricter; if it
   affects the thesis, output NO_SETUP or WATCHLIST and state exactly what is
   missing.
3. Every claim in your thesis must cite the specific input field or chart
   observation supporting it (fill the evidence[] array).
4. Entry zone, stop, targets, invalidation must be numerically coherent and
   real levels visible in the data — not round-number guesses.
5. candidate_status ∈ {CANDIDATE, WATCHLIST, NO_SETUP}. You cannot approve.
6. Prefer NO_SETUP over a weak setup. A forced setup is the worst failure.
7. Output ONLY valid JSON per the schema.

MULTI-TIMEFRAME PROTOCOL (in order)
a. 4h chart: regime — trending/ranging/transitioning; key HTF levels.
b. 1h chart: setup structure — one of: trend_pullback, range_reversal,
   breakout_retest, momentum_continuation, mean_reversion. If none fits
   cleanly, NO_SETUP.
c. 15m chart: timing/trigger quality and entry-zone precision.
d. Confluence audit: volume confirmation? funding/OI supportive or crowded?
   sentiment extreme (F&G) helping or warning? fresh news that could
   invalidate technicals?
e. Liquidity & trap check: identify where the obvious crowd stops sit
   (equal highs/lows, round numbers, clean swing points, the breakout
   level itself). Assume those pools may be swept before the real move.
   Place the stop BEYOND the likely sweep zone, not inside it; treat
   wick-only breaches as noise (invalidation = candle close). If a
   safe stop placement destroys the RR, that is NO_SETUP, not a
   tighter stop. For breakouts, never propose entry on the breakout
   candle itself — retest only.
f. Counter-thesis: write the strongest argument AGAINST the trade. If you
   cannot refute it with cited evidence, downgrade one level
   (CANDIDATE→WATCHLIST→NO_SETUP).

TIMEFRAME LABEL
Label the idea intraday (1-4h horizon, preferred) or swing (multi-day) —
choose what the structure genuinely supports, not the preference.

SCORING
confidence 0-100. It expresses evidence quality and confluence only.
>70 means "deserves risk checks and human review", never "approved".
```

**Output schema (enforced via structured outputs):**

```json
{
  "symbol": "str", "candidate_status": "CANDIDATE|WATCHLIST|NO_SETUP",
  "setup_type": "trend_pullback|range_reversal|breakout_retest|momentum_continuation|mean_reversion|none",
  "direction": "long|short|none",
  "timeframe_label": "intraday|swing|none",
  "thesis": "str ≤ 600 chars",
  "evidence": [{"claim": "str", "source_field": "str"}],
  "counter_thesis": "str ≤ 300 chars",
  "entry_zone": {"low": 0, "high": 0}, "stop": 0,
  "targets": [0], "invalidation_price": 0,
  "invalidation_text": "str ≤ 200 chars",
  "confidence": 0, "data_quality_note": "str|null"
}
```

## 3. Recent-history context block (the learning loop)

Assembled deterministically from Postgres and appended to the analyst's user message:

- Last 3 analyst verdicts for this symbol (status + one-line thesis + what happened).
- Rolling 30-day stats per setup_type: count, win rate, avg R — with the instruction: "Statistics describe past pipeline performance; use them to calibrate strictness, not to force or forbid setups."

This gives the system memory of its own performance — the key capability the reference build lacks.

> **Correction (2026-08-19, from M8.1) — this block is the OWNER's book.**
> One shared analysis now produces one signal row per approved user. Counting all of
> them would multiply the sample size by the number of users and turn the win rate
> into a weighted average of everybody's execution — a figure that changes when
> somebody new joins, which is not a fact about the market. `stats.setup_stats` and
> the per-symbol verdict outcomes are therefore scoped to the owner: exactly one row
> per analysis, and the only book the owner controls. Members' decisions never reach
> the prompt. Without an owner row both halves degrade to their "nothing measured
> yet" wording rather than borrowing somebody else's numbers.

## 4. On-demand `/pulse` prompt (P1)

Market-overview variant: BTC+ETH snapshots + breadth + F&G → 5-line regime summary, no trade candidates. Cheap tier model.

## 5. Prompt-iteration workflow

1. Change prompts only by adding `vN+1.md`.
2. Run new version live for ≥ 25 signals or 2 weeks.
3. Compare `/stats by-prompt-version` (win rate, avg R, NO_SETUP rate, rejection rate at gate).
4. Keep or roll back. Record decision in `journal/PROMPT_LOG.md`.

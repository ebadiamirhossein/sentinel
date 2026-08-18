# SPEC — Dual-Model Ensemble (Second Opinion) — ADDENDUM

**Status:** Approved addition. Provider abstraction lands in M5; shadow mode is milestone **M10**; consensus gate is **M11** (enabled only if shadow-mode stats justify it).
**Models:** Primary analyst = `claude-fable-5` (Anthropic). Second opinion = GPT-5.6 Sol (OpenAI API), chosen for flagship reasoning + strongest vision (chart reading).

---

## 1. Design principles

1. **Never average.** Each model produces its own complete `AnalystReport` against the identical input (same `MarketSnapshot` JSON, same chart PNGs, same JSON schema, equivalent system prompt). Blended numbers are forbidden.
2. **Disagreement is the signal.** Agreement between models trained on similar data is weak evidence; disagreement is strong evidence of a fragile setup.
3. **Measure before trusting.** The ensemble affects live signals only after shadow-mode data proves it improves precision.
4. **Fail open to primary.** OpenAI outage/timeout/schema failure → log `second_opinion: UNAVAILABLE`, continue Fable-only. The second opinion may never block the pipeline by being down.

## 2. Architecture change (do in M5, it's cheap)

```python
class AnalystProvider(Protocol):
    name: str  # "fable5" | "gpt56sol"
    async def analyze(self, snapshot: MarketSnapshot,
                      charts: list[ChartImage],
                      history_block: str) -> AnalystReport: ...
```

- `analyst/providers/anthropic_fable.py` (M5, exists anyway) and `analyst/providers/openai_sol.py` (M10).
- Same output schema enforced for both (OpenAI structured outputs / JSON schema mode). Same retry-once-then-discard rule.
- Prompt files per provider: `prompts/fable_vN.md`, `prompts/sol_vN.md` — same content adapted to each API's conventions; versions tracked independently.
- New `.env` entry: `OPENAI_API_KEY` (M10). Cost logging per provider.

## 3. Phase 1 — Shadow mode (M10)

- For every symbol that reaches deep analysis, call BOTH providers in parallel (`asyncio.gather`, per-provider timeout 90s).
- **Signals remain 100% Fable-driven.** GPT report stored in `analyst_reports` with `role=shadow`.
- Deterministic comparer stores per pair: `direction_match`, `status_match`, `zone_overlap_pct` (entry-zone intersection / union), `stop_side_match`, `confidence_delta`.
- Signal card gains one non-actionable footer line: `🔍 2nd opinion: agrees (long, conf 71)` or `🔍 2nd opinion: disagrees (NO_SETUP — "volume not confirming")` — informational only in this phase; the plan numbers never change.
- `/stats compare-models` output: win rate & avg R of taken+watched signals split by {both-agreed, direction-disagreed, status-disagreed, shadow-unavailable}; plus each model's solo hypothetical hit rate.

**Exit criteria to enable Phase 2:** ≥ 40 dual-analyzed candidates AND agreed-subset win rate exceeds all-signals win rate by ≥ 8 percentage points (or disagreed-subset is clearly worse). Otherwise stay in shadow or drop the ensemble — the data decides.

## 4. Phase 2 — Consensus gate (M11, feature-flagged `ensemble_gating: true`)

Deterministic rules applied AFTER both reports, BEFORE the risk gate:

| Fable says | GPT says | Result |
|---|---|---|
| CANDIDATE dir X | CANDIDATE dir X, zone_overlap ≥ 30% | Proceed to risk gate; card tagged `✅ consensus` |
| CANDIDATE dir X | CANDIDATE dir X, zone_overlap < 30% | Proceed with Fable's plan; card tagged `⚠️ level disagreement` |
| CANDIDATE | WATCHLIST | Downgrade to WATCHLIST |
| CANDIDATE dir X | CANDIDATE dir opposite / NO_SETUP | Downgrade to WATCHLIST; both theses stored; card suppressed |
| WATCHLIST/NO_SETUP | anything | Fable's verdict stands (GPT can veto, never promote) |
| any | UNAVAILABLE | Fable-only path, card tagged `2nd opinion offline` |

- GPT can **veto/downgrade but never create or upgrade** a signal — keeps one accountable primary and avoids doubling false positives.
- All consensus decisions stored with reasons for stats.

## 5. Costs & limits

- Doubles deep-analysis LLM cost only (screener stays Anthropic-only). At ≤ 3 candidates/cycle this is modest; log daily spend per provider, alert threshold in config.
- Rate limits: trivial at this volume; still route through the shared retry/backoff client wrapper.

## 6. Explicit non-goals

- No third model as tie-breaker "judge" (adds cost and a new failure mode; the table above is the judge).
- No cross-model debate loops in v1 of the ensemble (a "GPT critiques Fable's plan" mode is a possible M12 experiment, only after Phase-2 stats exist).
- Risk engine, sizing, leverage: untouched. Ensemble influences only WHETHER a candidate proceeds, never the numbers.

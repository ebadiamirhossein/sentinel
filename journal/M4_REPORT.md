# M4 — Deterministic Risk Engine · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (410 tests, 7 opt-in DB tests), **100% branch coverage on `sentinel/risk/`**, demo works, DB round-trip verified against real Postgres.

---

## 1. Method: tests first, red before green

Written and shown failing before a line of `sentinel/risk/` existed:

```
ImportError while loading conftest 'tests/risk/conftest.py'
E   ModuleNotFoundError: No module named 'sentinel.analyst.models'
```

| Test file | Tests | Spec source |
|---|---|---|
| `test_coherence.py` | 27 | §2 rules 1–6 + preconditions (§8.3 rejection matrix) |
| `test_ladder.py` | 18 | §3 ladder, §4 min-notional collapse (§8.4) |
| `test_sizing.py` | 23 | §4 rounding, leverage, liq buffer, margin guard |
| `test_rails.py` | 14 | §2 rule 7, §7 pause & daily-loss limit |
| `test_accounting.py` | 8 | §8.5 partial-fill R accounting |
| `test_properties.py` | 7 | §8.2 hypothesis invariants |
| `test_engine.py` | 7 | §6 plan completeness, audit trail |
| `test_golden_cases.py` | 5 | §8.1 hand-calculated goldens |
| `test_persistence.py` | 5 | §7 pause survives restart |

Every golden number was computed by hand in the test docstring first. Two of my hand calculations were
wrong and the engine caught them (single-entry quantity: 161.01, not my arithmetic slip of 160.99) —
which is the point of doing it in that order.

## 2. What was built

| Module | Contents |
|---|---|
| `risk/models.py` | `GateStatus`, `RejectionReason` (21 codes), `AccountState`, `PortfolioState`, `PauseState`, `MarketContext`, `LadderRung`, `EntryRung`, `TradePlan`, `GateDecision`. Frozen Pydantic, `Decimal` money. |
| `risk/rounding.py` | Tick/step grid: qty floors, entries round to nearest tick, **stops round away from the entry**. |
| `risk/coherence.py` | §2 rules 1–6, one pure function per rule, each returning a code or `None`. |
| `risk/ladder.py` | §3 construction (single vs 3-rung), §4 collapse, `effective_min_notional`. |
| `risk/sizing.py` | Risk budget, weighted entry, per-rung quantities, collapse loop, `solve_leverage` (ceil → clamp → liq-buffer reduction → reject). |
| `risk/rails.py` | §2 rule 7 and §7: open risk, positions, cooldown, pause, daily-loss auto-pause. |
| `risk/accounting.py` | §8.5 ladder-aware realized R — the M7 tracker's math, available now. |
| `risk/management.py` | §5 management template and TTL-based expiry. |
| `risk/engine.py` | `RiskEngine.evaluate()` — the gate, in a fixed nine-step order. |
| `analyst/models.py` | `AnalystReport` contract per PROMPTS §2 (models only, no LLM code) — defined once, here, rather than duplicated in M5. |
| `storage/` + `0003_risk_state` | `gate_decisions` (every verdict + reason code + full plan JSONB) and `risk_state` (one-row pause state). |
| `tools/size.py`, `examples/sol_long.json` | The M4 demo. |

`make coverage-risk` is now part of `make check` and fails the build below 100% branch coverage.

## 3. The worked example, every step

`python -m sentinel.tools.size --fixture examples/sol_long.json`. Inputs: capital **€10,000**,
risk **0.75%**, EURUSD **1.1593**, last price **83.40**, ATR(14,1h) **0.90**, SOLUSDT
(tick 0.01, qty step 0.01, exchange min-notional 5). Analyst: long, zone **82.10–83.10**,
stop **81.20**, targets **84.90 / 86.60 / 88.90**, confidence 78, intraday.

**1. Ladder (§3).** Zone width `83.10 − 82.10 = 1.00`. Threshold `0.5 × ATR = 0.45`. `1.00 ≥ 0.45`
→ three rungs: nearest price **83.10** @40%, midpoint **82.60** @35%, far edge **82.10** @25%.

**2. Weighted entry (§3).** `E = 0.40(83.10) + 0.35(82.60) + 0.25(82.10)`
`= 33.240 + 28.910 + 20.525 =` **82.675**.

**3. Stop distance (§2 rules 3–4).** `82.675 − 81.20 =` **1.475**; `1.475 / 0.90 =` **1.639 × ATR**,
inside [0.6, 3.0] ✓. As a fraction: `1.475 / 82.675 =` **1.7841%**.

**4. RR (§2 rule 5).** `(84.90 − 82.675) / 1.475 = 2.225 / 1.475 =` **1.51R** ≥ 1.5 ✓
(TP2 `3.925/1.475 =` 2.66R, TP3 `6.225/1.475 =` 4.22R).

**5. Risk budget (§4).** `€10,000 × 0.75% =` **€75.00** → in USDT: `75.00 × 1.1593 =` **86.9475 USDT**
(**multiplied** — see §4.1 below).

**6. Quantities (§4, risk-weighted).** `qty_i = risk_usdt × w_i / |p_i − stop|`:

| rung | price | weight | risk share | raw qty | floored (step 0.01) | notional |
|---|---|---|---|---|---|---|
| 1 | 83.10 | 40% | 34.7790 | 34.7790 / 1.90 = 18.30474 | **18.30** | 1,520.730 |
| 2 | 82.60 | 35% | 30.4316 | 30.4316 / 1.40 = 21.73688 | **21.73** | 1,794.898 |
| 3 | 82.10 | 25% | 21.7369 | 21.7369 / 0.90 = 24.15208 | **24.15** | 1,982.715 |

Each rung's notional clears `max(5, 20) = 20 USDT`, so no collapse. Total notional
**5,298.343 USDT** = `5298.343 / 1.1593 =` **€4,570.30**. Quantity-weighted average fill:
`5298.343 / 64.18 =` **82.55**.

**7. Actual risk after rounding.** `18.30(1.90) + 21.73(1.40) + 24.15(0.90)`
`= 34.770 + 30.422 + 21.735 =` **86.927 USDT** = **€74.98** — under the €75.00 budget by €0.02, the
cost of flooring three quantities. Rung 1's share is `34.770 / 86.927 =` **40.0%**, so §3's −0.40R
promise to the tracker holds exactly.

**8. Leverage (§4).** Margin budget `€10,000 × 10% = €1,000`. `leverage_raw = 4570.30 / 1000 = 4.570`
→ `ceil` → **5x**, inside the 10x cap.

**9. Liquidation buffer (§4).** `liq ≈ 1/5 = 20%`. Required: `2.0 × 1.7841% = 3.568%`.
`20% ≥ 3.568%` ✓ — the stop triggers about 11× earlier than liquidation.

**10. Margin.** `4570.30 / 5 =` **€914.06**, ≤ capital ✓.

**11. Expiry (§5).** intraday → `created_at + 12h`.

Actual output:

```
── SOLUSDT LONG [trend_pullback · intraday · conf 78] ──
gate          APPROVED_FOR_HUMAN
capital       €10000  ·  risk 0.75% = €75.00  ·  EURUSD 1.1593

entry ladder (limit orders)
  1) 83.10 — 40% of risk — 18.30 SOL (€1311.77)
  2) 82.60 — 35% of risk — 21.73 SOL (€1548.26)
  3) 82.10 — 25% of risk — 24.15 SOL (€1710.27)
  weighted entry 82.675  ·  avg fill 82.55

stop          81.20  (-1.7841%)
targets       TP1: 84.90 (1.51R)  TP2: 86.60 (2.66R)  TP3: 88.90 (4.22R)
notional      €4570.30  (5298.3430 USDT)
margin        €914.06  ·  leverage 5x (isolated)
liq buffer    OK (liq ≈ 20.0000% vs stop 1.7841%)
actual risk   €74.98  (planned €75.00)
management    TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder or trail by 1xATR.
expires       2026-08-18 22:57 UTC
```

## 4. Spec defects found — all raised, none guessed

Three genuine contradictions in `RISK_ENGINE.md`. Each was brought to you before any code depended on
it; each is now recorded as a dated correction in the spec itself.

1. **§4 inverted the FX conversion.** `notional_usdt = notional_eur / eurusd_rate`, but M1 fetches
   Frankfurter with `base=EUR`, so the stored rate is USD *per* EUR (1.1593). Dividing undersizes
   every position by ~26% (`1/1.1593² ≈ 0.744`) — on the worked example, €3,384 of notional instead
   of €4,570, and a real risk of ~€55 where the owner asked for €75. **Ruling: multiply.**
2. **§3/§8.5 and §4 disagreed about what the 40/35/25 weights weigh.** §3 promises the tracker and
   the card that a rung-1-only stop-out is −0.40R; §8.5 tests for it. But §4's
   `qty_i = (notional × w_i)/p_i` makes them *notional* shares, under which rung 1 — nearest price,
   furthest from the stop — carries **51.3%** of the risk, and that stop-out is −0.51R. **Ruling:
   risk weights; §3/§8.5 win.** The two readings coincide exactly for a single-entry plan.
3. **`TELEGRAM_UX.md` §1's example card does not reconcile with itself.** 1.208 SOL at 82.68 is ~€100,
   not the €4,190 notional on the next line, and its "4x" contradicts §4's `ceil` (4.19 → 5x). It is
   now marked **ILLUSTRATIVE** at the top, with a note to regenerate it from real engine output at M6.

Three under-specified points, ruled by you and now written into the spec: collapse order
(drop the far rung, renormalize), entry distance (**both** edges within 3%), and rails scope
(pure functions + a persisted `risk_state` row).

## 5. Decisions

1. **Nine-step evaluation order**, fixed so a rejection always names the *first* real problem:
   preconditions → geometry → entry distance → confidence → ladder → stop/RR → rails → sizing →
   leverage/liq/margin.
2. **§2 is re-checked after a collapse.** Dropping the far rung moves `E` towards price, which
   *lowers* RR — on the baseline setup, from 1.51R to 1.22R. The gate that ships a plan must be the
   gate that checked it, so rules 3–5 run again on the final ladder and the message says
   `(after collapsing to 2 rung(s))`.
3. **`gate_status` has three values**, not two: `APPROVED_FOR_HUMAN`, `REJECTED`, and
   `DOWNGRADED_WATCHLIST` for §2 rule 6, which is neither an approval nor a rejection.
4. **Stops round away from the entry**, entries to the nearest tick, quantities always down. A tick
   must never tighten a stop the analyst placed beyond a liquidity sweep — §2 rule 3's whole concern.
   Sizing then runs on the rounded numbers, so the reported risk is the real one.
5. **Two averages on the plan, both honest.** `avg_entry` is §3's `Σ(price × weight)` and drives every
   gate check (conservative for a long: it sits above the quantity-weighted price). `avg_fill_price`
   is what the owner actually averages if all rungs fill, and is what the card should show.
6. **Margin guard** (not in the spec): reject if `margin_eur > capital_eur`. A tight stop on a
   low-volatility symbol can demand >10× the account; a plan the owner cannot fund is not a plan.
7. **Every non-approved decision carries a `RejectionReason` code**, stored in its own indexed column
   in `gate_decisions` alongside the full plan JSONB — M9 needs to group rejections, and prose cannot
   be grouped.
8. **`AnalystReport` lives in `sentinel/analyst/models.py`**, defined now (contract only, no LLM code),
   so M5 fills an existing contract instead of inventing a second one.
9. **`hypothesis` added as a dev dependency** (~0.5MB, under the 1MB bar; §8.2 requires it by name).

## 6. Property tests (§8.2)

Scenarios are generated **valid by construction, never filtered**: an instrument fixes the tick/step
grid; the price is drawn on that grid at ≥ 20,000 ticks so one tick can never dominate an ATR; the
zone, stop and targets are placed so §2's rules 1–6 pass by arithmetic. Profile: `derandomize=True`,
`max_examples=300`, no deadline — reproducible in CI, no flakes.

Invariants asserted on every approved plan:

1. `risk_eur ≤ planned_risk_eur`, short by at most one qty step per rung (plus a cent of display
   rounding) — never over.
2. `liq_distance_pct ≥ 2 × stop_distance_pct`, leverage integral and in [1, 10], margin ≤ capital.
3. `Σ qty×price == notional_usdt`, and the EUR figure is the same conversion.
4. Weights sum to exactly 100; `avg_entry` inside the analyst's zone; every rung ≥
   `max(exchange_min, 20)`; `rr_tp1 ≥ 1.5`; `expires_at > created_at`.
5. **Totality**: over a second, deliberately hostile generator (inverted stops, negative prices, zero
   ATR, absurd capital) the gate never raises — every path ends in a status with a code.
6. **No floats**: a recursive walk of the serialized plan asserts no `float` anywhere.

## 7. Verification

```bash
make check                       # 410 tests, ruff, mypy --strict, 100% risk coverage
make coverage-risk               # branch coverage gate on sentinel/risk only
python -m sentinel.tools.size --fixture examples/sol_long.json
python -m sentinel.tools.size --fixture examples/sol_long.json --capital 120   # collapses to 2 rungs
python -m sentinel.tools.size --fixture examples/sol_long.json --capital 30    # REJECTED [MIN_NOTIONAL]
```

Verified on this machine:

- `410 passed, 7 skipped`; `mypy --strict` clean over 92 files; ruff clean.
- Branch coverage on `sentinel/risk/`: **100%** (456 statements, 116 branches, 0 missed).
- `alembic upgrade head` → `0003_risk_state`; the three opt-in Postgres tests pass against the real
  database, including a pause written, re-read through a fresh repository, and correctly expiring at
  +24h.
- The demo prints the plan above; `--capital 120` collapses to two rungs; `--capital 30` rejects with
  `MIN_NOTIONAL`.

## 8. Found during verification

- **`tests/ingestion/test_persistence.py::test_snapshot_round_trip` fails against a *dirty* database**
  (`assert 1068 == 4`): the M1 fixture cleans up after itself but not before, so the 1,068 candles left
  by the M1/M3 demos break its count assertions. It passes in the hermetic `make test` run (the DB
  tests are skipped without `SENTINEL_TEST_DATABASE_URL`). Pre-existing, not an M4 regression, left
  alone deliberately — flagged as a separate task.

## 9. Notes for M5

- The gate takes `AnalystReport` + `MarketContext` + `AccountState` + `PortfolioState` and returns a
  `GateDecision`. `MarketContext.from_snapshot(snapshot, features)` builds its half from M1/M2 output.
- `AnalystReport` in `analyst/models.py` is the schema M5's structured output must satisfy — including
  `prompt_version`, which travels into `gate_decisions` for the per-prompt A/B stats.
- The gate expects a *coherent* report; every incoherent one costs an analyst call. Worth feeding the
  rejection-reason distribution back into prompt v2.
- `PortfolioState` is a plain input today: M7 fills it from the DB (open risk, positions, cooldowns)
  and calls `evaluate_daily_loss` on every tick, persisting the result via `RiskStateRepository`.
- `sentinel/risk/accounting.py` is the tracker's R math, already written and tested — M7 should call
  it rather than write a second version.

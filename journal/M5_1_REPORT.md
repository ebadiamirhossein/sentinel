# M5.1 — Fee & funding awareness, and two fixes · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (594 tests with the DB suite enabled, 0 skipped),
**100% branch coverage on `sentinel/risk/`** including the new `costs.py`, both demos work,
the dirty-DB failure is fixed and verified against a database that already held 2,128 candles.

---

## 1. What changed, in one line

`min_rr_tp1` still reads 1.5, but it now measures **what the owner keeps** rather than what
the chart promises. Everything else in this milestone exists to make that number honest.

## 2. Task 1 — costs in the risk engine

| Module | Contents |
|---|---|
| `risk/costs.py` | **New.** `fee_eur`, `funding_settlements`, `estimate_funding_eur`, `estimate_costs`, `net_rr_multiples`. Pure, `Decimal`, no LLM. |
| `risk/models.py` | `PlanCosts`; `TradePlan.costs` + `rr_targets_net`; `schema_version` 1 → 2; `RejectionReason.NET_RR_TOO_LOW`; `MarketContext.funding_rate` / `next_funding_time`, filled by `from_snapshot`. |
| `risk/engine.py` | A tenth evaluation step: cost the round trip, re-run §2 rule 5 on the net figure. |
| `core/config.py`, `config.yaml` | `CostsConfig` / the `costs:` block. |
| `tools/size.py` | The cost line and both RR figures on the card. |
| Tests | `tests/risk/test_costs.py` (26 new), 4 new property invariants, 4 rejection goldens, cost assertions on all 4 §8.1 goldens. |

**The rates were verified, not remembered.** Binance USDⓈ-M VIP 0 is maker **0.0200%** /
taker **0.0500%**, checked against the published schedule
(<https://www.binance.com/en/fee/futureFee>) before anything was hardcoded. The BNB discount
and the VIP tiers are deliberately not modelled: these are the worst-case rates actually
paid, and a cost estimate that flatters the trade is worse than no estimate.

**The model.** Entries maker (the ladder is limit orders), **every** exit taker — stop, TP,
trail, manual. A TP may well rest as a limit and earn the maker rate, but assuming so lets
net RR depend on a fill quality the plan cannot promise; this is the same conservative bias
as flooring quantities and rounding stops away from the entry.

```
net_rr_i = (rr_i × risk_eur − entry_fee − tp_exit_fee_i − funding_charged)
           ÷ (risk_eur      + entry_fee + stop_exit_fee + funding_charged)
```

Derived from the *displayed* 2dp gross multiple, so the two figures on a card reconcile by
hand — which is how §8.1's goldens, and the owner reading a card, check the arithmetic.

## 3. ⚠️ The gate is now tighter — what that costs in signal frequency

**This is a real tightening and it will reduce signal count.** The parameter is unchanged;
the quantity it measures is not. The four §8.1 golden cases — every one of which the system
approved through M4 and M5 — are now rejected:

| Golden case | M4 TP1 | gross RR | **net RR** | verdict now | costs as % of the €75 budget |
|---|---|---|---|---|---|
| long, 3-rung ladder | 84.90 | 1.51 | **1.41** | `REJECTED [NET_RR_TOO_LOW]` | 4.21% |
| long, single entry | 82.91 | 1.50 | **1.26** | `REJECTED [NET_RR_TOO_LOW]` | 10.59% |
| short, 3-rung ladder | 80.90 | 1.51 | **1.44** | `REJECTED [NET_RR_TOO_LOW]` | 3.00% |
| short, single entry | 83.29 | 1.50 | **1.25** | `REJECTED [NET_RR_TOO_LOW]` | 10.95% |

Stated plainly: **a setup at 1.50–1.51 gross RR no longer passes the gate.** Anything the
analyst produces in that band — which, on M5's evidence, is where marginal candidates
cluster, because the analyst places TP1 at the nearest real level — is now rejected. To
survive, a setup needs roughly 1.6R gross on a wide stop and **1.8R gross on a tight one**.

The loss is not uniform, and the pattern is the important part:

- Cost as a share of risk is **≈ `0.07% / stop_distance`**. A tighter stop buys more notional
  per euro of risk, and fees are charged on notional.
- The widest stop in the suite (2.31 × ATR) gives up **0.07R**. The tightest (0.6 × ATR,
  exactly §2 rule 3's floor) gives up **0.25R** — more than three times as much.
- So the gate now discriminates *against tight-stop setups specifically*. That is the
  correct direction — it is the same class of setup M5 §9 flagged for routinely hitting the
  10× leverage ceiling — but it is a behavioural change in the analyst's incentives that
  M9's prompt-v2 review should look at deliberately rather than discover in the statistics.

The four cases are kept as parametrized rejection goldens
(`test_the_m4_goldens_are_now_rejected_on_net_rr`), so the change is asserted in the suite
rather than left implicit, and `examples/sol_long_thin.json` demonstrates it from the CLI.

**For M9 tuning:** `min_rr_tp1` is now a **net** threshold, and `RISK_ENGINE.md` §1 says so.
There is no single gross-equivalent of the old behaviour to tune back to — the gross-to-net
gap ranges from 0.07R to 0.25R depending on the stop. `NET_RR_TOO_LOW` is a distinct code
from `RR_TOO_LOW` precisely so the two populations can be counted separately: "the analyst
proposed a thin setup" and "the setup was sound and fees ate it" call for different fixes.

## 4. Decisions

1. **Every exit is taker.** See §2. Owner ruling.
2. **Favourable funding is shown but not spent.** A short collecting funding sees the real
   credit on its card (`funding_eur` is signed); the gate charges `max(0, funding)`. Funding
   is an estimate of a frequently-flipping input and must never be the reason a plan
   approves. Switchable via `costs.credit_favourable_funding`, so the choice is visible and
   reversible rather than buried in the arithmetic.
3. **The cost check runs LAST**, after leverage, the liquidation buffer and the margin guard.
   This was not the original design and the test suite caught it: cost-as-a-share-of-risk is
   driven by the same quantity as leverage, so *any* plan failing the margin guard
   (notional > 10× capital) necessarily spends >90% of its risk budget on fees. Gating net RR
   earlier made `INSUFFICIENT_MARGIN` unreachable and reported "thin RR" for a position the
   owner cannot fund at all. The harder blocker wins.
4. **`rr_targets` keeps its name and its gross meaning**; `rr_targets_net` is additive.
   `schema_version` is 2. Plans stored by M4 and M5 stay readable exactly as written, and
   `gate_decisions` needed no migration (the plan is JSONB).
5. **Net RR is derived from the quantized gross multiple**, not the raw one. Otherwise the
   card shows 1.51R and 1.43R on the same line and no one can reconcile them by hand.
6. **`funding_interval_hours` is a config constant (8), not ingested.** Binance settles most
   USDⓈ-M perps every 8h, some every 4h, and drops to 1h at the funding cap. ccxt's
   premiumIndex response carries `interval: null` — visible in the recorded cassettes
   (`tests/cassettes/binance_funding_SOLUSDT.json`) — so the real per-symbol interval needs a
   second endpoint. Not worth an ingestion change here; it is one more reason the funding
   figure is labelled an estimate everywhere it appears.
7. **A missing funding rate degrades explicitly.** `funding n/a (no funding rate in the
   snapshot)` on the card, never a reassuring €0.00. Fees are never optional.

## 5. Task 2 — the dirty-DB failure (M4_REPORT §8)

`tests/ingestion/test_persistence.py`'s session fixture cleaned up after itself but not
before. Since these tests assert absolute row counts (`len(candles) == 4`), anything already
in the database fails them — and the M1/M3 demos leave thousands of candles on exactly the
database a developer running these tests is most likely to have.

Fixed by extracting `_clean(session)` and calling it at **setup as well as teardown**, in the
existing child-first order. The tests' intent is untouched: upsert idempotency and the exact
counts (4 candles, 4 after a re-save, 2 snapshot audit rows) all still assert.

Verified against a real Postgres holding real ingestion data — first reproducing the failure,
then fixing it:

```
# without the fix, against a DB holding 10,640 candles
E       assert 10640 == 4

# with the fix
### DB now holds: candles=2128  snapshots=2
### RUN 1 against that dirty DB       → 8 passed
### re-dirtied via the M1 demo (2128) → 8 passed
### RUN 3 back-to-back                → 8 passed
```

## 6. `analyst_timeout_seconds` 90 → 150

M5 measured live analyst calls at 47–85s, so 90s left as little as 5s of headroom, and M10
runs two providers in parallel. Raised in `config.yaml` and in the `LLMConfig` default.
`ENSEMBLE.md` §3 specifies 90s, so the deviation is recorded there as a dated correction
rather than left as config silently disagreeing with a spec.

150s still fits comfortably inside PRD F1's 15-minute cycle. Worth re-measuring at M10 before
a *third* provider is ever added.

## 7. Spec changes (all dated corrections, none guessed)

| Spec | Change |
|---|---|
| `RISK_ENGINE.md` §1 | Four cost parameters added; a correction note stating that **`min_rr_tp1` gates NET RR**, with the M9 tuning guidance. |
| `RISK_ENGINE.md` §2 rule 5 | Correction: measured net; the gross check survives as a cheap early reject (net ≤ gross always). |
| `RISK_ENGINE.md` §4.2 | **New section.** The whole cost model, the verified rates and their source, the funding estimate and its two figures, the degradation rule, and why the check runs last. |
| `RISK_ENGINE.md` §6 | `schema_version` 2; the new output fields; `rr_targets` keeps its gross meaning. |
| `RISK_ENGINE.md` §8 | New test-plan item 6 for the cost tests and property invariants. |
| `ENSEMBLE.md` §3 | Per-provider timeout 90s → 150s, with the measured evidence. |

## 8. Demo

```bash
python -m sentinel.tools.size --fixture examples/sol_long.json
```

```
stop          81.20  (-1.7841%)
targets       TP1: 85.20 (1.59R net · 1.71R gross)  TP2: 86.60 (2.50R net · 2.66R gross)  TP3: 88.90 (3.99R net · 4.22R gross)
costs         fees €3.16  (maker 0.02% in · taker 0.05% out)
              funding ~€0.18 est  (0.00193% x 2 settlement(s) @ 8h)
              round trip €3.34 = 4.4533% of the €75.00 risk budget
notional      €4570.30  (5298.3430 USDT)
margin        €914.06  ·  leverage 5x (isolated)
liq buffer    OK (liq ≈ 20.0000% vs stop 1.7841%)
actual risk   €74.98  (planned €75.00)
```

The same plan with M4's original TP1, which is the whole point of this milestone:

```bash
python -m sentinel.tools.size --fixture examples/sol_long_thin.json
```

```
── SOLUSDT ─────────────────────────────────────
REJECTED  [NET_RR_TOO_LOW]
reward-to-risk at TP1 is below the minimum once fees and funding are paid
(1.40R net vs 1.51R gross — €3.34 of costs on a €74.98 risk)
```

## 9. Verification

```bash
make check
```

```bash
SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:<pw>@localhost:5432/sentinel .venv/bin/pytest
```

Verified on this machine (2026-08-18):

- `594 passed` with the opt-in DB suite enabled (**0 skipped**); `584 passed, 10 skipped`
  hermetically. `mypy --strict` clean over 131 files; ruff clean.
- Branch coverage on `sentinel/risk/`: **100%** (527 statements, 128 branches, 0 missed),
  `costs.py` included.
- `tests/ingestion/test_persistence.py` passes twice in a row against a database re-dirtied
  by the M1 demo between runs (§5), having first been shown to fail without the fix.
- Both demo fixtures produce the output above.

## 10. Notes for M6/M7

- **M6's signal card must show the cost line.** `TELEGRAM_UX.md` §1's card is still marked
  ILLUSTRATIVE from M4 and now also predates costs; regenerate it from real engine output,
  with both RR figures and the cost block.
- `PlanCosts` is on the plan and stored in `gate_decisions` JSONB, so M9 can ask what the
  system's cost load actually was per approved signal — and, via `NET_RR_TOO_LOW`, how many
  candidates costs killed.
- **M7's tracker should charge real costs, not estimates.** `risk/accounting.py` computes R
  from fills; once the owner marks a trade taken, the realized fees are knowable and the
  estimate should be replaced rather than carried forward. The funding figure especially:
  this milestone estimates it from one snapshot's rate, and a multi-day swing will diverge.
- The funding interval is assumed to be 8h. If M9's data suggests funding matters more than
  it appears to here, ingesting the real per-symbol interval is a small M1 addition
  (`fapiPublicGetFundingInfo`) and `funding_interval_hours` already has a per-plan field
  ready to carry it.

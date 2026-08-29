# M12 — The crypto correlation cap

Branch `m12-crypto-correlation-cap` off `main` (d776f5f). **Not deployed.**

## State at handoff

**The rail is not built.** That is the milestone's main fact and it is a decision, not a
shortfall: the owner held it pending the backtest, at the point in the session where the
data turned out not to be in the repo. What shipped is everything the decision needs.

- `journal/M9_STATS_REVIEW.md` — the measurement window's result, closing M9. Twelve
  sections; §9 puts the risk-budget decision to the owner as a decision, with both sides
  argued and neither recommended.
- `sentinel/tools/concurrency_backtest.py` + 17 tests — the cap's number, replayable in
  one command instead of asserted in prose.
- `config.yaml` — the forex spend review moved 2026-09-04 → ~2026-09-07. **Comments only;
  `git diff` touches no value line.**
- `docs/MILESTONES.md` — M12 renumbered (see §6), and the missing M10d/M10e sections
  written so the ledger and `journal/` agree.

**Verified, not asserted:**

- `make check` **exit 0**.
- `git diff main -- sentinel/risk/ sentinel/fx/ sentinel/analyst/prompts/` — **empty.**
  The gate, `min_rr_tp1`, `min_confidence`, every setup threshold, sizing arithmetic,
  forex's own rails and the prompts are untouched, and none of them can see this
  milestone.
- **No golden moved.** No generator was run. `sentinel/risk/`'s branch coverage is
  unchanged at 100% because `sentinel/risk/` is unchanged.
- The `risk:` block in `config.yaml` is byte-identical.

---

## 1. What I pushed back on

Four things. The first three were in the brief; the fourth was found while writing it.

### 1.1 The effective cap is 3, not 4 — and this changes the framing

`max_open_risk_pct 2.25 / risk_per_trade_pct 0.75 = 3`, and `MAX_OPEN_RISK` sits **above**
`MAX_POSITIONS` in `check_portfolio_rails`' fixed order (`sentinel/risk/rails.py:33-36`).
So `max_positions: 4` has never fired for this owner and cannot at 0.75% risk — it binds
only at ≤ 0.5625%.

The brief read the 2026-08-28 cluster as a rail failing. **It was the account at its
designed maximum.** The owner accepted the correction and asked for it in the review's
opening, which is where it now is (`M9_STATS_REVIEW.md` §0). It matters because reading
the cluster as a failure points at the wrong fix — there is no bug to repair, there is a
number to re-authorise.

A side finding worth its own line: the `/status` card reads `positions: N of 4`, which is
a limit the rail above it makes unreachable. Recorded, not fixed — changing it is a card
change and this milestone moved no golden.

### 1.2 A same-direction cap of 2 is a 33% cut to the risk budget

All 11 crypto signals in the window were long. In a long-only book, "max 2 same-direction"
is arithmetically "max 1.5% open risk" and the 2.25% budget becomes unreachable. The brief
said "nothing in the gate, the RR minimum or the thresholds is being changed" — true of
the *thresholds*, and the cap still reduces the maximum risk the system can ever deploy by
a third.

That may well be correct. It is not neutral, and it should be decided as what it is. The
owner's ruling (G1) was to surface it as a decision rather than record it, so
`M9_STATS_REVIEW.md` §9 states the choice in one paragraph and argues both sides at full
strength. **I do not pick, and the section says the cost side is blank until the export
arrives.**

This is also the argument that settled the rail's *shape* — see §5.

### 1.3 The cluster is worse than the brief stated

"Four of the five losses" reconciles only if the fifth is the forex GBPUSD stop. Within
crypto: **all four losses in the entire window happened on 2026-08-28**, three of them in
one simultaneous same-direction cluster, and all five winners were on other days.

### 1.4 `/pulse` has never shown a forex rejection — found while writing §4

`gate_outcome` coerces the stored reason with `RejectionReason(row.reason)` inside
`except ValueError: continue` (`sentinel/bot/pulse.py:255`). Forex writes
`ForexRejection` values into the same `gate_decisions.reason` column. There is no
`ForexRejection` import in `bot/pulse.py` and no forex wording map.

So every forex rejection code — including `MAX_CONCURRENT_POSITIONS`, the §9 cap this
milestone is modelled on — has rendered as the generic per-account line since forex
shipped. **The rail crypto is being given a version of has itself been invisible on the
surface built to make rails visible.**

The owner's ruling (G2) was to rank it rather than list it. It is **first of the five**
carry-forwards, and the reasoning is in `M9_STATS_REVIEW.md` §10. Two consequences I did
not expect when I found it:

1. **It may contaminate the review's forex paragraph.** All three forex signals were
   GBPUSD. `max_concurrent_positions: 1` is counted across **all users**
   (`orchestrator.py:1745`), so any EURUSD or USDJPY candidate reaching the rails while
   GBPUSD was open was rejected — invisibly. "All GBPUSD" may be the rail, not the market.
   The deciding query is in `M9_STATS_REVIEW.md` §10 and is that file's top next action.
2. **It has been spending money.** Forex has no screener, so a pair blocked at the rails
   had already bought a full analyst call. Relevant to the ~2026-09-07 spend review.

Recorded, not fixed, per the brief's section E.

---

## 2. Where the caps live — the answer to the brief's part A

| | file:line | config key |
|---|---|---|
| crypto concurrent positions | `sentinel/risk/rails.py:35-36` | `risk.max_positions: 4` |
| crypto open risk | `sentinel/risk/rails.py:33-34` | `risk.max_open_risk_pct: 2.25` |
| forex concurrency (§9) | `sentinel/fx/rails.py:63-64` | `forex.max_concurrent_positions: 1` |

Both crypto rails are in one function, called once from `sentinel/risk/engine.py:177`, in
the fixed order `PAUSED → MAX_OPEN_RISK → MAX_POSITIONS → DAILY_SIGNAL_CAP →
SYMBOL_COOLDOWN`. State is built per user at `orchestrator.py:1661-1679`.

**Does the change touch `sentinel/risk/`? YES** — when it is built. It cannot not: the
rail belongs in `check_portfolio_rails`, it needs a `RejectionReason` member and a
`PortfolioState` field. **In this milestone it does not, because the rail is held.**

**Is FOREX §9 reusable machinery? No, and reusing it would be wrong.**
`sentinel/fx/rails.py:48` is a deliberate name-twin, not shared code. Its docstring gives
the reason — *"this package depends on nothing in the frozen one"* — and the two differ in
a way that is not a parameter: **forex counts open positions across all users, crypto
counts per user.** There is no shared portfolio or exposure module in either market. What
is reused is the *pattern* — rail → named code → `gate_decisions.reason` → surface — not
the code. The brief asked me to prefer reuse; there is nothing here to reuse, and saying
so is the honest answer rather than manufacturing a shared abstraction over two rails that
disagree about whose book they are reading.

---

## 3. The backtest

`sentinel/tools/concurrency_backtest.py`. Reads the columns `/journal` already exports
(`stats/journal.py`'s `JournalRow`), replays them in fill order against a candidate
same-direction cap, and reports which signals it refuses and what the window's net R
becomes without them.

```bash
.venv/bin/python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25
```

**The full export was not available in this session** — `backups/` stops at 2026-08-21,
five days before the window opened, and there is no CLI or make target that exports the
journal; `/journal` in Telegram is the only path and it runs against the server. So the
committed evidence is the four rows the brief supplied:

```
── same-direction cap, replayed against the window ──
trades with a fill: 4   net as traded: +0R   (from 0 of 4 with a result)
⚠️  no row carries pnl_r_net — every R figure below is an ABSENCE, not a zero.

cap 1.5% of capital in one direction
  BLOCKED #22 DOGEUSDT long at 2026-08-28 05:57 — 1.50% already open · STOP · unresolved
  net without them: +0R   (removed +0R across 1 trade(s))

cap 2.25% of capital in one direction
  nothing blocked — this cap is a no-op on this window.
```

**What is certain from this, and it is not nothing:**

- **1.5% blocks exactly one trade on 2026-08-28: #22 DOGEUSDT — the owner's real money.**
- **#21 BTCUSDT is admitted.** It filled at 13:08, four minutes after the cluster cleared.
  A rail that blocked it too would be a daily quota, not a concurrency cap.
- **2.25% as a same-direction cap is a no-op.** It is the existing rail wearing a new name.
  So the choice really is between 1.5% and no change; there is no middle setting.

**What is not known, and the question that decides the number:** whether any of the five
winners was a third concurrent same-direction position. That needs the other rows.

**Three deliberate properties of the tool**, each of them a lesson from this repo applied
rather than restated:

1. **It prints its own lower bound, every time, and a test asserts the sentence survives
   editing.** `time_in` is the *fill* time; the real rail fires at *publication*, where a
   pending unfilled signal already occupies the budget. Every figure understates the cap.
2. **An open position is never summed as zero.** A missing result and a flat result are
   different facts (`test_an_open_position_is_never_summed_as_zero`).
3. **An empty R column says `ABSENCE, not a zero`.** `+0R` from a column of blanks reads
   exactly like a flat window — the same shape as the defect that took a day in M10e,
   where `status: OK, spend: 0` meant nothing had happened rather than nothing had gone
   wrong. Two tests: one that the warning fires, one that it *does not* fire when results
   exist, so the check cannot rot into always-on.

The test file's fixture is the 08-28 cluster itself rather than invented rows, and one
test (`test_a_cap_that_blocks_a_winner_is_reported_as_a_cost`) exists solely to prove the
harness can report the finding that would **stop** this milestone. A backtest that can
only ever show the cap removing losses is not a backtest.

---

## 4. The other deliverables

**`journal/M9_STATS_REVIEW.md`** — the window's result, per the brief's section D, plus
the two additions. Everything the brief listed is there: the HYPOTHETICAL and REAL
figures with the REAL sample named as variance; the setup split recorded as a hypothesis
and explicitly not acted on; the cluster in full; all-11-long; the euro column; forex.

Two things I added because the evidence forced them:

- **M9's exit criteria were not met** — ≥ 25 tracked signals required, 9 delivered, and
  no second prompt version to compare. The review states that every statistical
  conclusion in it is under-powered and that **the cap rests on the structural argument,
  not on these nine trades.** The brief already framed it that way; the review makes it a
  rule about how the file may be quoted.
- **The euro column is worse than a display problem.** Capital moving €200 → €400 → €1000
  mid-window did not merely rescale the euros. Per HANDOFF §4 item 15, sizing is not a
  scaling: at €200 the golden BTCUSDT setup gets two rungs and 1.50R net TP1, at €10,000
  three and 1.75R, and `min_rr_tp1` gates the net figure. **So moving capital changed
  which signals cleared the gate at all.** The nine trades came from an engine operating
  in three different regions of its own behaviour. Even R is comparable only as a unit of
  account, not as evidence about one consistent selection rule.

**`config.yaml`** — three dated comments moved from `REVIEW 2026-09-04` to `~2026-09-07`,
each carrying the reason: forex was configured 2026-08-21 but the daily-staleness defect
meant it produced nothing until 2026-08-24, so a two-week window that spent five days
broken has not had two weeks. The revert-value list is byte-identical — that list is the
way back from a rail raised for a rehearsal, and moving a date is not a licence to edit it.

**Carry-forwards** — all five recorded in `M9_STATS_REVIEW.md` §10, ranked, with the
ordering argued. Ranking criterion: *does a reader draw a false conclusion, or are they
merely inconvenienced?* Order: (1) the `/pulse` forex coercion, (2) `/pulse EURUSD`'s
nonexistent screener, (3) `cache_read_tokens: 0`, (4) the Persian 12-second freeze, (5)
`forex.calendar_loaded` logging twice a minute. **3-versus-4 is arguable and the review
says so; 1-and-2-above-the-rest is not** — those two are the only ones that make a reader
believe something false.

**Judgment call, flagged at plan time and not overruled:** section E is headed "RECORDED,
NOT FIXED", so the `⏳ در حال آماده‌سازی…` line was recorded as the fix's shape and not
written. The config date change was made because it was a concrete dated instruction and
because leaving it says "review Sept 4", which is now wrong.

---

## 5. The rail — designed, recorded, NOT built

Held at owner instruction pending §3's number. Written here so the next session builds
rather than re-derives.

**Shape: risk-based, not a position count.** `risk.max_same_direction_risk_pct: 1.5`.

The reason is §1.1. A count cap of 2 and a risk cap of 1.5% are identical at today's
0.75% risk and diverge the moment `/risk` moves — 2 positions is 2.0% at 1.0% risk and
0.5% at 0.25%. More to the point, **`max_positions: 4` is the worked example of what
happens to a count cap that sits under a risk cap: it never fires and becomes
decorative.** Adding a second count cap beneath the same risk cap would be repeating that
in the same function.

| piece | where | note |
|---|---|---|
| config | `RiskConfig`, `core/config.py` | `max_same_direction_risk_pct: 1.5` |
| state | `PortfolioState`, `risk/models.py:126` | same-direction open risk |
| rail | `check_portfolio_rails`, directly after `MAX_OPEN_RISK` | the specific budget before the count |
| code | `RejectionReason.MAX_SAME_DIRECTION_RISK` | |
| recording | free — `gate_decisions.reason` already stores every code | |

`Direction` is **already imported** into `risk/models.py:19` and `TradePlan.direction`
exists at `:299`, so the orchestrator can build the field from `plans` at
`orchestrator.py:1661` with no new import and no new dependency edge.

**`/pulse`, and the boundary question it raises.** The code belongs in `PERSONAL_REASONS`
(`bot/pulse.py:129`): it reads somebody's open book, and naming it publicly tells one
member how many longs another holds — `MAX_POSITIONS` and `MAX_OPEN_RISK` are personal for
exactly this reason. The brief required it be "visible on /pulse, not silently dropped".
Both are satisfiable and the owner chose how: **stored in `gate_decisions` either way, a
named line in the owner-only section** — the same boundary the existing spend line uses,
enforced the same way, by a type — **and the existing per-account fold for members.**

**Tests first**, per CLAUDE.md rule 4 and `make coverage-risk`'s 100% branch floor: the
rail's position in the order; the boundary at exactly 1.5% (`open + this > cap`, the one
comparison that separates a cap of 2 from a cap of 3); an opposite-direction position not
consuming the budget; and a meta-test that the new code has pulse wording and a
SHARED/PERSONAL classification, following the two closed classifications `bot/pulse.py`
already guards that way.

**Spec:** `RISK_ENGINE.md` §1's config table and §2 rule 7 gain the key with a dated
ruling, in the style of the existing corrections. `FOREX.md` §9 already anticipates it —
*"relax to a correlation-adjusted rail once there is data to calibrate against"* — and
gets the cross-reference.

**Goldens: expected to be zero.** The rail cannot fire in any fixture (`PortfolioState` is
an input, never serialised) and adds no field to `TradePlan` or `GateDecision`. The only
thing that would move a golden is adding a line to the `/status` card, which would move
exactly `surfaces/status.txt` and `surfaces_multi/status.txt` — a separate decision, to be
brought back rather than taken in passing. **If any golden moves unexpectedly, stop and
report before regenerating anything.**

---

## 6. Milestone numbering

`docs/MILESTONES.md` already assigned M12 to the consensus gate. Following the precedent
of the 2026-08-20 renumbering note: **M12 is now the crypto correlation cap**, consensus
gate M12 → **M13**, dashboard M13 → **M14**, with a dated note in the file. M11 (ensemble
shadow mode) is unaffected; the note also records that `journal/M11p_REPORT.md` is a side
milestone and not that M11.

`M10d` and `M10e` shipped, are reported in `journal/`, and had no sections in
`MILESTONES.md` — the file stopped at M10c. Both are now written, marked as added
retrospectively.

---

## 7. How to demo

```bash
.venv/bin/python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25
```

Export `/journal all` from Telegram, save the Hypothetical sheet as CSV, run the above.
Then read `journal/M9_STATS_REVIEW.md` §9 and answer it.

## 8. What the next session needs

1. **The forex `gate_decisions` query** (`M9_STATS_REVIEW.md` §10). It decides whether §8
   of the review is a finding or an artefact of an invisible rail.
2. **The full export**, run through §3's tool. It decides the cap's number.
3. **An answer to `M9_STATS_REVIEW.md` §9.** Until then the rail stays designed and
   unbuilt, which is where this milestone deliberately leaves it.

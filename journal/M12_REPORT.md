# M12 — The crypto correlation cap: CANCELLED

Branch `m12-crypto-correlation-cap` off `main` (d776f5f). **Not deployed.**

## State at handoff

**The rail was not built, and it is now cancelled — owner decision, 2026-08-29, on this
milestone's own evidence.** The analysis M12 was commissioned to justify the cap instead
killed it:

1. `check_portfolio_rails` reads `open_taken` — signals pressed ✅ Taken. The owner carried
   **at most one position at any instant** in the window, so the rail **would never have
   fired**, at any setting.
2. The evidence for the cap — three simultaneous same-direction longs on 2026-08-28 — lives
   in the **published** book, which that rail does not read.
3. Making it read the published book would cap *publication*, truncating the HYPOTHETICAL
   record precisely on the clustered days: **it buys protection by destroying the evidence
   that would justify it.**

`sentinel/risk/` was never touched. The design survives as §5 below, to be built **when a
real book carries two or more concurrent positions and the rail can actually bind.**

**The window's headline finding is not the cap.** It is that **`/stats` reports gross R
while `min_rr_tp1` gates net** — so every conclusion drawn from this window came from the
wrong column. Promoted to defect 1 of six at owner instruction, with a fix proposal (§4a).

### What shipped

- `journal/M9_STATS_REVIEW.md` — the window's result, closing M9. Fourteen sections: the
  cancellation banner, the gross/net correction, the taken-vs-published finding (§4a), the
  answered cost side (§9), six ranked defects with a fix proposal for the first (§10), the
  conviction hypothesis (§12) and the forex query (§13).
- `sentinel/tools/concurrency_backtest.py` + 19 tests — the cap's number, replayable. Kept
  despite the cancellation: the next window needs it to decide when the rail can bind.
- `config.yaml` — forex spend review 2026-09-04 → ~2026-09-07. **Comments only.**
- `docs/MILESTONES.md` — **the renumbering is reverted**: consensus gate keeps M12,
  dashboard keeps M13. M10d and M10e written up; the file had stopped at M10c.

### Verified, not asserted

- `make check` **exit 0**.
- `git diff main -- sentinel/risk/ sentinel/fx/ sentinel/analyst/prompts/` — **empty.**
- **No golden moved.** No generator was run. `docs/MILESTONES.md` is purely additive over
  `main` — zero lines removed.
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

The owner supplied the full export mid-session (2026-08-29, 14 data rows + header). Times
are **Europe/Vilnius**, UTC+3 throughout — the changeover is 2026-10-25, so no row
straddles it. Converted once to UTC; a constant offset cannot change an overlap, so every
concurrency figure is zone-invariant and the conversion matters only for reconciling
against logs.

### The answer

**1.5% costs no winners. Not one.** The five winners — #2 (+2.93R), #4 (+0.69R), #10
(+0.35R), #15 (+1.44R), #8 (+0.64R) — are all admitted. The cap blocks only losses.

**But which losses, and whether any, depends on a distinction the brief did not draw and
neither did I until I went looking for where `PortfolioState` comes from.**

| book | what it counts | blocked at 1.5% | net | at 2.25% |
|---|---|---|---|---|
| **A — taken** (`open_taken`, what crypto's rails read now) | 2 trades | **nothing** | −1.78R → −1.78R | nothing |
| **B — published** (`open_symbols_by_user`, what forex §9 reads) | 11 trades | #12, #22 — both losses | +0.69R → **+2.47R** | nothing |
| B without the undecided #8 | 10 trades | #22 — a loss | +0.05R → **+1.10R** | nothing |

**2.25% is a confirmed no-op on every book.** Peak same-direction concurrency in the
window was 3 — exactly the designed maximum. So the choice is 1.5% or no change.

**The 08-28 cluster:** #18 (02:21 UTC) admitted, #20 (02:31) admitted as the second,
**#22 (02:57) blocked**, #21 (10:08) admitted because #18 and #20 closed five minutes
earlier. Four stops totalling −3.86R become three totalling −2.81R.

**Lower-bound caveat, applied rather than restated.** `time_in` is the fill time and the
rail fires at publication, so the table understates blocking. Computed per admitted trade,
how much earlier it would have to be published to be refused: **every winner has ≥ 2 days
of margin** (#15 2d 0:33, #8 2d 21:47, #10 4d 0:07; #2 and #4 never had two
same-direction ahead of them). Signals publish hourly and expire within a day, so nothing
reaches back two days. The one tight case is **#21 at five minutes — and it is a loss.**
So publication times can only move the answer toward *more losses blocked, no winners*.
The measured result is a floor.

### What I found while answering it, and it is the milestone's pivot

`check_portfolio_rails` reads a `PortfolioState` built from `open_taken`
(`repositories.py:1535-1548`), which filters `decision == TAKEN`. Forex's §9 cap reads
`open_symbols_by_user` (`:1524-1533`), which filters on `OPEN_STATUSES` and nothing else.

**So crypto's rails count only signals the owner pressed Taken on; forex's counts every
open signal.** In this window he took two, non-overlapping.

> **The crypto rails saw at most ONE open position at any instant in the whole window.**
> On 2026-08-28 the system *published* three simultaneous same-direction longs; the owner
> *carried* one. `positions: 0 of 4` was an accurate reading of an empty book, not an
> unused limit.

"Give crypto the rail forex has" is therefore not adding a direction dimension to an
equivalent rail — **the two rails read different books**, and that choice decides whether
the cap does anything. Written up as `M9_STATS_REVIEW.md` §4a and folded into §9's
decision.

**One engineering recommendation, which is not the risk-budget call and so I do make it:**
a published-book cap suppresses signals, and the HYPOTHETICAL population is the record of
what the pipeline would have done. Capping publication truncates that record precisely on
the clustered days — **it buys protection by destroying the evidence that would justify
it.** The taken book constrains money and leaves the measurement intact.

### Three things that temper the result, all in the review

Two blocked trades out of eleven is the whole of the +1.78R. The two the cap blocks are
**exactly the two the owner took with real money** — a 1-in-55 coincidence, or a mechanism
in which a third same-direction signal arrives with two similar ones on screen and
clustering raises conviction. Recorded as a hypothesis for the next window, not a finding.
Under Book B neither of his real trades would have been published.

### Corrections the rows forced

- **The headline figures are GROSS.** `/stats` computes on `SignalRow.realized_r`, which
  `/journal` labels `pnl_r_gross`. The window is **+2.47R / PF 1.69 net**, not +2.86R /
  1.84. One parameter — 0.0433R/trade of costs — reconciles the net total, the PF *and*
  the REAL population's −1.69R from its net −1.78R. Three targets, one parameter. Now
  defect 5 in the review's ranking; it is what put wrong figures in the first draft.
- **Times were Vilnius, not UTC.** The review now carries both columns.
- **Two suspected findings died on contact with the code and are not reported as findings:**
  `by_setup` spanning REAL+HYPOTHETICAL is a documented M7 owner ruling
  (`stats/models.py:16-18`), not a population merge; and `Population` has no `UNDECIDED`
  member, so the system correctly counts #8 as HYPOTHETICAL — the label is the owner's
  annotation. Both were checked before writing rather than after.
- **`#10` and `#8` are `STOP` with positive net R** — trailing stops after the management
  plan moved them. The replay never reads `outcome`; two tests now pin that, one of them
  relabelling every outcome in the fixture to prove the whole replay ignores the column
  rather than one trade doing so.

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

## 4a. Defect 1's fix proposal, in short (owner requirement F1)

Full version in `journal/M9_STATS_REVIEW.md` §10 defect 1. **Not built — written to be
read.**

**The defect is narrower than "nobody labelled it".** `stats/journal.py`'s module
docstring item 3 settles the gross/net split deliberately — the win rate is gross so the
journal and `/stats` cannot contradict each other, the running balance is net because a
balance is money — and ends *"That is true of `/stats` too, and the Legend sheet says
so."* **The disclosure exists; it lives in the XLSX Legend sheet, which nobody reading
`/stats` in Telegram ever opens.**

**Recommended: T1, a label.** The card already prints `costs paid: €9.48` two lines under
`total 0.60R (€45.00)` (`bot/cards.py:1109-1116`) — everything needed is on screen, and
nothing says the first figure is before the second.

```
  avg 0.20R · total 0.60R (€45.00)   ->   avg 0.20R · total 0.60R gross (€45.00)
  <i>(taken + watched + skipped)</i>  ->   <i>(taken + watched + skipped · gross R)</i>
```

Renderer-only: no arithmetic, no field, no query, no migration —
`tests/bot/test_no_arithmetic.py` scans that file and a label is exactly what it permits.
**Moves two goldens**, `surfaces/stats.txt` and `surfaces_multi/stats.txt`, four lines
each; regenerated with `generate_goldens_m10a` and `generate_goldens_m10c`. No other
golden is reachable from the stats card.

**T2** adds a net EUR figure — free arithmetic (`total_eur − costs_eur`, both already on
`PerformanceStats`) but it must live in `stats/compute.py`, and it changes a Pydantic
contract, which CLAUDE.md says to ask about first.

**T3, switching `/stats` to net R: recommended against.** It breaks the documented
invariant that the journal's win rate equals `/stats`' (a trade winning gross and losing
net would flip); net R needs `planned_risk_eur`, which lives only inside the `plan` JSONB,
so it means loading and validating every plan — `/stats` has no degrade path for a plan
that no longer matches the model, `/journal` does — or a migration; and it silently
rewrites every figure the owner has already read.

## 5. The rail — CANCELLED, design kept as a dated note

> **Dated note, 2026-08-29. Cancelled, not deferred — but the design is kept rather than
> deleted, because the cap becomes correct the moment its premise does.**
>
> **Build it when a real book carries two or more concurrent positions.** Concretely: when
> `SELECT max(concurrent) FROM ...` over `signals` with `decision = 'TAKEN'` shows two
> same-direction positions overlapping in a measured window, the rail can bind and the
> question of its number becomes answerable. Until then it is a rail that cannot fire, and
> a rail that cannot fire reads on a checklist as a rail — `config.yaml`'s own words about
> `max_entry_distance_pct` at 3.0 for forex.
>
> Re-run `sentinel/tools/concurrency_backtest.py` against the taken book at that point.
> The tool is kept for exactly this.

The design as settled before cancellation, so the next session builds rather than
re-derives:

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

## 6. Milestone numbering — reverted

M12 was briefly reassigned to the correlation cap. **With the cap cancelled the
reassignment is reverted at owner instruction:** consensus gate keeps **M12**, dashboard
keeps **M13**, and `docs/MILESTONES.md` carries a dated note saying so and pointing here.
A cancelled milestone belongs in `journal/`; that file is the forward plan.

`M10d` and `M10e` shipped, are reported in `journal/`, and had no sections in
`MILESTONES.md` — the file stopped at M10c. Both are now written, marked as added
retrospectively. The file is **purely additive** over `main`: zero lines removed.

## 7. How to demo

There is no rail to demonstrate. What there is to read:

```bash
.venv/bin/python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25
```

Export `/journal all` from Telegram, save the Hypothetical sheet as CSV, run the above —
it reproduces §3's table. Then read `journal/M9_STATS_REVIEW.md`, starting at the
cancellation banner and §10 defect 1.

## 8. What the next session needs

1. **Run `M9_STATS_REVIEW.md` §13 Query A** — the forex `gate_decisions` histogram. It is
   the only open question in either document whose answer changes what is written in them:
   whether "all three forex signals were GBPUSD" is a finding or an artefact of a cap that
   `/pulse` could not display. Query B and C are there for each branch of the answer.
2. **Decide defect 1's fix** (§10, and §4a below). T1 is a label — renderer-only, two
   goldens, no arithmetic. I did not build it; the proposal is written to be read.
3. **Nothing on the cap.** Cancelled. §5's note says what would revive it.
4. Start accumulating §12's conviction counts; do not read them before ~50 decided signals.

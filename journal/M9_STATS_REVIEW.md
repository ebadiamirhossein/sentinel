# M9 — The measurement window's result

**Window:** 2026-08-15 → 2026-08-29. **Closed by:** M12 (journal/M12_REPORT.md).
**Source:** the owner's `/journal` export of 2026-08-29, 15 rows.

M9 was defined as analysis, not code (`docs/MILESTONES.md` §M9, HANDOFF.md §3). This
file is that analysis. **It changes no threshold**, and one section (§9) exists to put a
decision to the owner rather than to take one.

---

> ## Outcome: the correlation cap is CANCELLED
>
> **Owner decision, 2026-08-29, on the evidence in §4a and §9.** M12 was scoped as a cap
> on concurrent same-direction crypto risk. It is not being built, and the reasoning is
> recorded here rather than in a branch nobody will read again:
>
> 1. `check_portfolio_rails` reads `open_taken` — signals the owner pressed ✅ Taken on.
>    He carried **at most one position at any instant** in the whole window, so the rail
>    **would never have fired**, at any setting.
> 2. The evidence for the cap — three simultaneous same-direction longs on 2026-08-28 —
>    comes from the **published** book, which that rail does not read.
> 3. Making it read the published book would cap *publication*, and the HYPOTHETICAL
>    population is the record of what the pipeline would have done. That truncates the
>    record precisely on the clustered days: **it buys protection by destroying the
>    evidence that would justify it.**
>
> The design survives as a dated note in §5 of `journal/M12_REPORT.md`. **Build it when a
> real book carries two or more concurrent positions and the rail can actually bind.**
> Nothing in `sentinel/risk/` was touched.
>
> **The window's headline finding is instead defect 1 in §10:** `/stats` reports gross R
> while `min_rr_tp1` gates net, so every conclusion drawn this window came from the wrong
> column.

## 0. The framing, corrected before anything else

The 2026-08-28 cluster — three same-direction longs open at once, all stopped within 61
seconds — reads as a rail failing. **It is not. It is the account at its designed
maximum.**

`max_open_risk_pct` is 2.25 and `risk_per_trade_pct` is 0.75, so the book holds three
positions and refuses the fourth. `MAX_OPEN_RISK` sits *above* `MAX_POSITIONS` in
`check_portfolio_rails`' fixed order (`sentinel/risk/rails.py:33-36`), which has a second
consequence worth writing down on its own line:

> **`max_positions: 4` has never fired for this owner and cannot at 0.75% risk.** It
> binds only at `2.25 / 4 = 0.5625%` or below. The card that reads `positions: 0 of 4`
> is displaying a limit that the rail above it makes unreachable. The real concurrency
> limit has been **3** for the whole window.

Nothing malfunctioned on 2026-08-28. The finding is that the *number three* was
authorised on an assumption the day disproved — see §9. Reading the cluster as a failure
points at the wrong fix, which is why this correction is the first thing in the file.

---

## 1. Crypto, HYPOTHETICAL population

> **Correction (2026-08-29, from the export). The headline figures are GROSS. The
> window's net result is +2.47R, not +2.86R.**
>
> `/stats` computes on `SignalRow.realized_r` (`sentinel/stats/compute.py:87-93`), and
> `/journal` maps that same column to **`pnl_r_gross`** (`stats/journal.py:320`),
> deriving `pnl_r_net` separately. So the two surfaces report different numbers under
> similar names, and the figures quoted into this review came from the gross one.

| | gross (`/stats`) | **net (the export)** |
|---|---|---|
| trades | 9 | 9 |
| record | 5W / 4L — 55.6% | 5W / 4L — 55.6% |
| total | +2.86R | **+2.47R** |
| profit factor | 1.84 | **1.69** |

Summing the export's `pnl_r_net` over the nine gives **+2.47R and PF 1.69**. One free
parameter reconciles the two readings — total costs of **0.39R**, about **0.0433R per
trade** — and that same constant independently reproduces the stated PF of 1.84 (computed
1.8395) *and* the REAL population's stated −1.69R from its net −1.78R. Three targets, one
parameter: this is the gross/net split, not a transcription error.

**Why it matters beyond bookkeeping.** `min_rr_tp1 = 1.5` gates the **net** figure
(RISK_ENGINE.md §2 rule 5, corrected at M5.1) — so the number the gate admits a plan on
is net, and the number the owner has been reading his results in is gross. Costs ate
**14% of the window's net result**. Wins average 1.21R net; losses average 0.895R net.

**Recorded as a defect, not fixed — and it is now defect 1 in §10**, with a fix
proposal, at the owner's instruction: every conclusion drawn from this window came from
the wrong column.

**The pipeline is not failing.** Nothing in this section justifies touching the gate,
`min_rr_tp1`, `min_confidence` or any setup threshold, and M12 touches none of them.

## 2. Crypto, REAL population

| | |
|---|---|
| trades | 2 |
| record | 0W / 2L |
| gross | −1.69R |
| **net** | **−1.78R** |

**This is selection variance on a two-trade sample. It is not evidence about the
pipeline.** At the measured 55.6% win rate, two consecutive losses happen about 20% of
the time — roughly one window in five, for no reason at all. Two trades cannot
distinguish "the owner picked badly" from "the owner picked normally and got the common
unlucky outcome", and this file declines to try.

The populations stay separate (HANDOFF §5). The REAL figure is recorded because it is
what the owner's money did, not because it measures anything.

## 3. By setup type — recorded, not acted on

| setup | trades | win rate | gross avg/trade | net avg/trade | net total |
|---|---|---|---|---|---|
| `trend_pullback` | 8 | 37.5% | −0.33R | −0.37R | −2.95R |
| `breakout_retest` | 3 | 66.7% | +1.26R | +1.21R | +3.64R |

Two clarifications the rows forced, neither of them a defect:

- **The −0.33R and +1.26R are gross averages *per trade*, not totals.** Same gross/net
  split as §1, and the same 0.0433R/trade reconciles both rows.
- **The eleven trades here span REAL + HYPOTHETICAL**, which looks like the population
  merge HANDOFF §5 forbids and is not: `by_setup` and `by_prompt_version` carry a
  documented owner exception from M7 (`stats/models.py:16-18`) because *"which setups does
  the analyst get right"* is a question about the analyst rather than about which cards
  somebody acted on. Checked before reporting it as a finding.

**Too small to act on. No threshold moves on n = 3.** Recorded as a **hypothesis for the
next window**: *does `breakout_retest` outperform `trend_pullback`, and if so is it the
setup or the market regime it happened to appear in?* Both arms need an order of
magnitude more trades before the question is even askable.

Note the interaction with §4: `trend_pullback` supplied all four losses, and all four
losses were the same day. The setup split and the cluster are **not independent
observations** — they may be one observation counted twice.

## 4. 2026-08-28, in full

**Times below are UTC.** The export is in **Europe/Vilnius**, which was UTC+3 (EEST) for
every row in this window — the changeover is 2026-10-25, so no row straddles it and one
constant offset is correct throughout. Both columns are given because the owner reads the
export in local time and every stored row is UTC (CLAUDE.md). A constant offset cannot
change an overlap, so **every concurrency figure in this file is zone-invariant**; the
conversion matters only for reconciling against logs.

| # | symbol | dir | filled (UTC) | stopped (UTC) | filled (Vilnius) | stopped (Vilnius) | net R |
|---|---|---|---|---|---|---|---|
| 18 | AVAXUSDT | long | 02:21 | 10:03 | 05:21 | 13:03 | −1.04 |
| 20 | SOLUSDT | long | 02:31 | 10:04 | 05:31 | 13:04 | −1.04 |
| 22 | DOGEUSDT | long | 02:57 | 10:03 | 05:57 | 13:03 | −1.05 |
| 21 | BTCUSDT | long | 10:08 | 16:02 | 13:08 | 19:02 | −0.73 |

\#22 was **REAL** — the owner's money. Three positions were open simultaneously from
05:57 to 13:03, all long, and they stopped **within one minute of each other**. That is
one trade sized three times.

**A correction to how this was first stated.** "Four of the five losses in the window"
reconciles only if the fifth is the forex GBPUSD stop (§8). Within crypto the fact is
stronger:

> **All four crypto losses in the entire measurement window happened on 2026-08-28**,
> three of them in one simultaneous same-direction cluster. Every one of the five
> winners was on another day.

\#21 BTCUSDT is worth separating out: it filled at 13:08, **four minutes after** the
cluster cleared. It was not concurrent with the other three. Any rail modelled on this
day must admit it — see §10's test.

## 4a. The rails did not see that cluster — and this is the milestone's pivot

`check_portfolio_rails` reads `PortfolioState`, which the orchestrator builds from
`SignalRepository.open_taken` (`orchestrator.py:1652`, `repositories.py:1535-1548`):

```python
    SignalRow.status.in_([status.value for status in OPEN_STATUSES]),
    SignalRow.decision == SignalDecision.TAKEN.value,
    SignalRow.dry_run.is_(False),
```

with the docstring *"Watched, skipped and dry-run signals are tracked but commit nothing,
so they cannot occupy a budget the user never spent."*

**So crypto's open-risk and position rails count only what the owner pressed ✅ Taken
on.** In this window he took **two** signals — #12 and #22 — and they did not overlap
(#12 closed 2026-08-25 09:53 UTC; #22 opened 2026-08-28 02:57 UTC).

> **The crypto rails saw at most ONE open position at any instant in the entire
> measurement window.** `positions: 0 of 4` on the `/status` card was not a limit going
> unused — it was an accurate reading of an almost always empty book.

On 2026-08-28 the system **published** three simultaneous same-direction longs. The owner
**carried** one. The concentration is real and it is in the *published* book; it was never
in his account.

**And forex's §9 cap reads the other book.** `ForexPortfolioState.open_positions` is built
from `open_symbols_by_user` (`repositories.py:1524-1533`), which filters on
`OPEN_STATUSES` and **nothing else** — no decision, no dry-run test. So:

| | counts | scope |
|---|---|---|
| crypto `MAX_OPEN_RISK` / `MAX_POSITIONS` | signals **decided TAKEN** | per user |
| forex `MAX_CONCURRENT_POSITIONS` (§9) | **every open signal** | all users |

"Give crypto the rail forex has" is therefore not a matter of adding a direction
dimension to an equivalent rail. **The two rails read different books**, and which book
the new rail reads decides whether it does anything at all — see §9.

## 5. Every crypto signal in the window was long

**11 of 11. Not one short.**

Recorded as a finding in its own right, because it changes what other rails *mean*: in a
long-only book, a direction-blind cap of three is three of the same bet, and the
portfolio's diversification is entirely an assumption. It also sets a trap for §9 — a
same-direction rail calibrated on a long-only sample stops binding the day the analyst
produces a short, silently and with no configuration change.

Whether this is `fable_v1`, the screener, or a genuinely one-directional fortnight of
crypto is **not answerable from 11 signals** and is not answered here.

## 6. The euro column is unusable, and it is worse than a display problem

Capital moved **€200 → €400 → €1000 mid-window**. So 1R was €1.50, then €3.00, then
€7.50. The export shows **+2.86R and −€0.18 at the same time**, and both are correct.

> **R is the only comparable unit for this window. Capital must not move mid-window
> again.**

That much was known. The stronger version follows from HANDOFF §4 item 15 and the dated
block at `docs/MILESTONES.md` §M9:

> **Sizing is not a scaling.** On the golden BTCUSDT setup, €200 produces **two** ladder
> rungs and TP1 net RR **1.50R**; €10,000 produces three and **1.75R**. `min_notional`,
> the quantity step and the leverage cap all bite in absolute terms, and `min_rr_tp1 =
> 1.5` gates the **net** figure.

Therefore moving capital mid-window did not merely rescale the euro column. **It changed
which signals cleared the gate at all.** A setup that was rejected `NET_RR_TOO_LOW` at
€200 could have been approved at €1000, and vice versa on the margin. The nine trades
were produced by an engine operating in three different regions of its own behaviour.

**So even R is comparable only as a unit of account, not as evidence about one
consistent selection rule.** This is the single largest limitation on everything above,
and it is not fixable retrospectively — only prevented next time.

## 7. M9's exit criteria were not met

`docs/MILESTONES.md` §M9: *"≥ 25 tracked signals, JSON validity ≥ 98%, zero sizing bugs,
and a first prompt-version comparison."*

The window delivered **9 tracked crypto trades** from 11 signals. That is roughly a third
of the sample the milestone defined as sufficient, and there is no second prompt version
to compare against — `fable_v1` ran throughout.

The window is being closed anyway, deliberately. What follows from that is a rule about
how this file may be used:

> **Every statistical conclusion here is under-powered and must be quoted with its n.**
> The correlation cap M12 proposes rests on a **structural** argument — forex carries the
> rail, crypto does not, and altcoins track BTC at least as tightly as the majors track
> the dollar — **not on these nine trades.** The trades motivated the question. They do
> not answer it.

## 8. Forex

3 signals: 1 STOP (−1.09R), 1 EXPIRY, 1 still open. **All GBPUSD.** Sample of one pair;
no conclusion drawn about forex's viability, and the spend review moves to ~2026-09-07
(see `config.yaml`, and journal/M12_REPORT.md §4).

**And "all GBPUSD" may not be a fact about the market.** `forex.max_concurrent_positions`
is 1, counted **across all users** (`sentinel/core/orchestrator.py:1745`). Any EURUSD or
USDJPY candidate that reached the rails while a GBPUSD position was open was rejected
with `MAX_CONCURRENT_POSITIONS` — and, per §10 defect 2, **`/pulse` could not show it.**
This paragraph carries that caveat inline so it cannot be quoted without it.

Also unresolved: M10e's daily-staleness fix is **not deployed**, and the first cycle that
can falsify it is **Monday 2026-08-31** — after this window closed.

---

## 9. The decision this window puts to the owner

M12 proposes a cap on concurrent same-direction crypto risk. The number is not a detail;
it is the decision. **This section does not recommend one.**

> **The choice.** 2.25% open risk was authorised on the assumption that concurrent
> positions are independent bets. 2026-08-28 showed that in a long-only book they are one
> bet. So the owner must choose: **keep 2.25%** and accept that it can be a single
> correlated position, or **cut the effective same-direction budget to 1.5%** and accept
> 33% less exposure on the days the pipeline is right.

**And §4a adds a second choice that has to be made first, because it decides whether the
number matters at all:** does the rail read the **taken** book, as crypto's existing rails
do — in which case it would have changed nothing in this window — or the **published**
book, as forex's §9 does, in which case it would have blocked two losses and no winners?
The measured answer to the first choice is different under each. Both are below.

### The strongest case for keeping 2.25%

- **The edge is real and capping exposure caps it.** PF 1.84, average win 1.25R against
  average loss 0.85R. A cap bites hardest when the most positions are open, which is the
  same condition under which the pipeline makes its money.
- **A cap that binds only because the analyst produced no shorts is not a rail, it is a
  bet on the analyst.** §5: 11 of 11 long. If `fable_v1` starts producing shorts the cap
  stops binding, with no config change and no notice. A rail whose strength depends on a
  property of an LLM is not a deterministic rail, which is the thing `sentinel/risk/` is
  supposed to be.
- **Nine trades cannot separate "correlated cluster" from "bad day for crypto."** BTC led
  everything on 08-28. A market-wide down day hits any long book at any cap above one,
  and a cap of two would have taken two of those three stops rather than three.
- **`daily_loss_limit_pct` already exists for exactly this scenario** and is the rail
  designed for it. A portfolio cap is a second, blunter answer to a question the system
  already answers, and two rails aimed at one risk is how a rejection stops explaining
  itself.

### The strongest case for cutting to 1.5%

- **Three stops inside 61 seconds is the empirical refutation of independence.** 2.25%
  authorised a risk that was never the risk being taken. 1.5% is not a *reduction* in
  intended exposure; it is a correction of a mis-measurement.
- **Forex already carries this rail, at a stricter setting, on identical reasoning.**
  `FOREX.md` §9 caps forex at one open position because the three pairs share the dollar.
  Crypto's ten-symbol watchlist is more heterogeneous than three dollar crosses — but in
  a BTC-led move alt correlation approaches one, which is the case the rail is for. §9
  itself anticipates this: *"relax to a correlation-adjusted rail once there is data."*
- **The uncapped path has a hidden second cost.** Four full-1R stops in a day is 3.00%
  realized — **exactly `daily_loss_limit_pct`**. On 08-28 the crypto book took four
  stops. Partial fills softened them (§1: average loss 0.85R, ≈3.4R gross for the day)
  and only two were REAL, so no pause fired. But the uncapped configuration puts a
  correlated day **on the pause threshold**, and a 24-hour pause costs every signal in
  it — more forgone upside than the cap takes in a month.
- **At rehearsal capital the asymmetry favours the cap.** Forgone upside is a
  rehearsal-sized number; a correlated drawdown is a real one, plus the pause.

### The cost side, answered — 2026-08-29, from the full export

**DOES 1.5% COST ANY WINNERS? No. Not one — under either reading of the book.**

The five winners are #2 (+2.93R), #4 (+0.69R), #10 (+0.35R), #15 (+1.44R) and #8
(+0.64R). **Every one is admitted.** The cap blocks only losses.

**But the answer depends entirely on §4a's question — which book the rail reads — and
under the book the rail actually reads today, it does nothing at all.**

| book | what it counts | blocked at 1.5% | window net | at 2.25% |
|---|---|---|---|---|
| **A — taken** (what `check_portfolio_rails` reads now) | 2 trades | **nothing** | −1.78R → −1.78R | nothing |
| **B — published** (what forex §9 reads) | 11 trades | **#12, #22** — both losses | +0.69R → **+2.47R** | nothing |
| B without #8 | 10 trades | **#22** — a loss | +0.05R → **+1.10R** | nothing |

- **Book A: the cap is untestable against this window.** The owner took two signals and
  never held two at once, so no same-direction cap — 1.5%, 0.75%, anything — would have
  fired. It protects a future in which he trades more; this window contains no evidence
  about it either way.
- **Book B: +0.69R → +2.47R, and no winner is touched.** It blocks #12 BTCUSDT (−0.73R)
  and #22 DOGEUSDT (−1.05R).
- **2.25% is confirmed a no-op on every book.** Maximum same-direction concurrency
  reached in the window was **3** — exactly the designed maximum (§0). So the real choice
  is 1.5% or no change; there is no middle setting.

**The 2026-08-28 cluster, explicitly.** #18 (02:21 UTC) is admitted, #20 (02:31) is
admitted as the second, **#22 (02:57) is blocked**. #21 (10:08) is admitted because #18
and #20 had closed five minutes earlier. So the day goes from four stops totalling
−3.86R to **three stops totalling −2.81R**. The cluster of three becomes a pair.

### Three things that should temper this before it is read as a result

1. **It rests on two data points.** Two blocked trades out of eleven. The +1.78R is the
   sum of two losses that happened to arrive third in a queue, and §7's warning about
   sample size applies to this section more than to any other in the file.
2. **The two trades the cap blocks are exactly the two the owner took with real money.**
   At random that is a 1-in-55 coincidence, which is worth naming rather than enjoying.
   There is a plausible mechanism — a third same-direction signal arrives with two
   similar ones already on screen, and clustering raises conviction — under which the cap
   would systematically target the signals he is most likely to act on. **That is
   speculation and is recorded as a hypothesis for the next window**, not as a finding.
   Under Book B, neither of his two real trades would have been published, and his real
   P&L for the window would have been zero rather than −1.78R.
3. **A cap on the published book changes what the next measurement can measure.** The
   HYPOTHETICAL population exists to record what the pipeline would have done. Suppress
   publication and it stops being that record — truncated precisely on the clustered
   days, which are the days in question. **A Book B rail buys protection by destroying
   the evidence that would justify it.** Book A has the opposite property: it constrains
   only money, and leaves the measurement intact. This is an argument for the taken book
   that has nothing to do with the risk-budget question, and it is the one engineering
   recommendation in this section.

### The lower-bound caveat, applied

`time_in` is the **fill** time; the rail fires at **publication**, and a published but
unfilled signal already occupies the budget. So the table above understates blocking. How
much could it move? For each admitted trade, how much earlier it would have had to be
published to be refused:

| trade | net R | margin |
|---|---|---|
| #2, #4 | +2.93, +0.69 | never had two same-direction ahead of them |
| #15 | **+1.44** | 2 days 0:33 |
| #6 | −0.77 | 2 days 1:30 |
| #18 | −1.04 | 2 days 5:13 |
| #20 | −1.04 | 2 days 5:23 |
| #8 | **+0.64** | 2 days 21:47 |
| #10 | **+0.35** | 4 days 0:07 |
| #21 | −0.73 | **0:05** |

**Every winner has at least two days of margin.** Signals are published on a 60-minute
cycle and expire within a day, so no plausible publication time reaches back two days.
The single tight case is **#21, at five minutes — and it is a loss.**

> **So publication times can only move the answer in one direction: more losses blocked,
> no winners.** The measured result is a floor, and the finding is robust to the
> limitation that produced it.

Reproduce with:

```bash
.venv/bin/python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25
```

### On #8, the undecided row

The export labels #8 `UNDECIDED`; `stats/models.py`'s `Population` has no such member and
`population_of` returns `HYPOTHETICAL` for anything not TAKEN, so **the system counts it
as HYPOTHETICAL** — which is why the headline says nine trades over eight
`HYPOTHETICAL`-labelled rows. The label is the owner's annotation, not a stored value.

**Does including it change a conclusion? One, and it is worth knowing.** With #8 in the
book, #12 is blocked (it arrives with #8 and #10 both open); without it, #12 is admitted
and only #22 is blocked. So **#8 alone decides whether the cap catches one of the owner's
real losses or both.** It does not change the answer to the question that matters — no
winner is blocked either way, and the net improves either way (+1.10R or +1.78R).

### One caveat on the percentages above

Per §6, capital moved mid-window, so R cannot be converted to a percentage of capital
from the export alone. Every figure in this section stated in `%` is the **configured
rail arithmetic** (0.75% per trade against a 2.25% budget), not a measurement of what the
euro column did.

---

## 10. Known defects — recorded, not fixed, ranked

Six carry-forwards. **Ordered by severity, and the ordering is argued** rather than
listed, because the ordering is the useful part. The criterion:

> **Does a reader draw a false conclusion, or are they merely inconvenienced?**

### 1 — most serious. `/stats` reports GROSS R; the gate decides on NET

`/stats` computes every figure from `SignalRow.realized_r` (`stats/compute.py:87-93`);
`/journal` maps that same column to **`pnl_r_gross`** and derives `pnl_r_net` beside it
(`stats/journal.py:320-322`). So the same window reads **+2.86R** on one surface and
**+2.47R** on the other, and **`min_rr_tp1 = 1.5` gates the net figure** — the number the
system admits a plan on is not the number the owner is judging results by.

**Ranked first at owner instruction (2026-08-29), above the `/pulse` defects.** The
reasoning he gave is the correct one and outranks the criterion this list otherwise uses:
every conclusion drawn from this window came from the wrong column. A surface that hides a
rail (defect 2) is worse *per reading*; a surface that misstates the central number is
worse *per conclusion*, and conclusions are what a measurement window is for.

**It is narrower than "nobody labelled it", and the precise form matters for the fix.**
`stats/journal.py`'s module docstring, item 3, settles this deliberately:

> *"A win is **gross** realized R > 0 — `stats/compute.py`'s definition, unchanged — while
> the **running balance is net R**. The two bases differ on purpose: the win rate at the
> bottom of the Real sheet has to equal what `/stats` reports for the same window, or the
> two surfaces appear to contradict each other... **That is true of `/stats` too, and the
> Legend sheet says so.**"*

So the design is intentional, correct, and **already disclosed — in the XLSX's Legend
sheet.** The defect is that the disclosure lives in an artefact the `/stats` reader never
opens. That is why the fix is small.

#### Fix proposal — not built, for the owner to read (F1)

**Recommended: T1, a label. One word on one line, plus one header.**

The `/stats` card already prints `costs paid: €9.48` two lines below `total 0.60R
(€45.00)` (`bot/cards.py:1109-1116`). Everything needed is on screen; nothing says the
first figure is before the second.

```
  avg 0.20R · total 0.60R (€45.00)          ->   avg 0.20R · total 0.60R gross (€45.00)
  <b>By setup type</b> <i>(taken + watched + skipped)</i>
                                            ->   <i>(taken + watched + skipped · gross R)</i>
```

- **Why this and not more:** it is a **renderer-only** change. No arithmetic, no new field,
  no query, no migration — `tests/bot/test_no_arithmetic.py` scans this file and a label is
  exactly what it permits. It closes the gap that actually caused the error: a reader of
  the card cannot currently tell which basis they are on.
- **What it moves:** `tests/fixtures/golden_cycle/surfaces/stats.txt` and
  `surfaces_multi/stats.txt` — **two files, four lines each** (three population blocks plus
  the by-setup header). Regenerated with `generate_goldens_m10a` and
  `generate_goldens_m10c`. Nothing else in either file changes, and no other golden is
  reachable from the stats card.

**T2, if a label is judged too thin: add net EUR.** `total_eur` and `costs_eur` are both
already on `PerformanceStats`, so `net €35.52` costs one subtraction — but it must happen
in `stats/compute.py`, not the card, and it adds a field to a Pydantic contract, which
CLAUDE.md says to ask about first. Same two goldens, one extra line each.

**T3, switching `/stats` to net R: recommended against**, for three reasons that are not
about effort:

1. **It breaks a documented invariant.** The journal's Real-sheet win rate must equal
   `/stats`' win rate for the same window (docstring item 3). A trade that wins gross and
   loses after fees would flip populations, and the two surfaces would contradict each
   other — the exact failure the current design exists to prevent.
2. **Net R is not cheaply computable there.** It needs `planned_risk_eur` as the
   denominator, which lives only inside the `plan` JSONB. `/stats` would have to load and
   validate every plan in the window; `/journal` already carries a degrade path for plans
   that no longer match the model (`stats/journal.py:262`) and `/stats` has none. The
   alternative is a new column, i.e. a migration.
3. **It silently rewrites history.** Every figure the owner has read to date would change
   with no note on the card, which is a worse version of the problem being fixed.

**Whichever is chosen, the surface should keep saying `costs paid`** — it is the line that
makes the gap visible once the basis is named.

### 2 — `/pulse` silently discards every forex rejection code

`gate_outcome` coerces the stored reason with `RejectionReason(row.reason)` inside
`except ValueError: continue` (`sentinel/bot/pulse.py:255`, marked
`# pragma: no cover — a code retired from the enum`). Forex writes **`ForexRejection`**
values into the same `gate_decisions.reason` column. There is no `ForexRejection` import
in `bot/pulse.py` and no forex wording map.

So **every** forex rejection — `MAX_CONCURRENT_POSITIONS`, `SPREAD_TOO_WIDE`,
`EVENT_BLACKOUT`, all sixteen — has rendered as the generic *"cleared the shared checks
— the rest is per-account"* line since forex shipped.

**What a reader of `/pulse` would have concluded during the forex window:** that no forex
signal was ever stopped by a named rail. That the pairs were quiet, or that the gate was
passing them and something later declined them. **Forex's §9 cap — the most restrictive
rail in the system, one open position across all users — has never once appeared on the
surface built to make rails visible.**

**This is the project's oldest lesson, in the exact place built to prevent it.** `/pulse`
exists because a member *"cannot tell 'quiet market' from 'system down'"*
(`sentinel/bot/pulse.py:10`). For forex it does not do that job.

**It compounds with defect 3 into a coherent and entirely wrong story.** A reader sees
`/pulse EURUSD` say *"the screener triages it every cycle"* — forex has no screener — and
never sees a cap rejection. Together those read as *forex is being triaged and nothing is
being blocked*, when the truth may be the reverse.

**The question that decides whether this is a wording bug or a false picture is open**,
and it is §8's question: were EURUSD or USDJPY candidates rejected with
`MAX_CONCURRENT_POSITIONS` while GBPUSD was open? If so, "all three signals were GBPUSD"
is partly the rail rather than the market, and the owner has been reading a false picture
of why forex is quiet. **Settled by one query, which is this file's top next action:**

```sql
SELECT symbol, reason, count(*) FROM gate_decisions
WHERE market = 'forex' AND evaluated_at >= '2026-08-24'
GROUP BY symbol, reason ORDER BY count(*) DESC;
```

A second consequence for the spend review: forex has **no screener**, so every pair
blocked at the rails had already bought a full analyst call. The cap may have been
spending money on analyses it then discarded, invisibly.

### 3 — `/pulse EURUSD` claims a screener that does not exist

The text says *"the screener triages it every cycle"*. Forex has no screener; every pair
buys a full analyst call every cycle, which is why its budget is larger than crypto's
(`config.yaml`, the `llm_daily_budget_global_usd` block).

Third because it is also a false statement about how the system works — but it is static
text a careful reader can catch, and it hides no event. Most of its severity is borrowed
from defect 2, which it compounds.

### 4 — crypto screener calls report `cache_read_tokens: 0`

Prompt caching is not being read on the screener call. Real money, every cycle, and
possibly a symptom of caching being broken more widely than the one call it was noticed
on. Above the Persian freeze because it is unbounded and silent; below 1-3 because
nobody concludes anything false from it.

### 5 — Persian summary storage lookup is not wired

Every press regenerates the summary and freezes the UI for about 12 seconds. The
`persian_summaries` table exists (migration `0012`) and is not being read. User-visible
and it damages trust in a surface, but it misinforms nobody, and it has a known one-line
mitigation: show `⏳ در حال آماده‌سازی…` before the call.

### 6 — `forex.calendar_loaded` logs twice per minute

Log noise. Real, cheap, last.

### On the ordering

**4-versus-5 is arguable** — money against a user-facing freeze is a judgement, not a
measurement. **1-and-2-above-the-rest is not arguable:** those two are the only defects
here that cause a reader to believe something false.

---

## 11. Carried forward, unanswered

Both were added to this review by `docs/MILESTONES.md` §M9 and both need
`gate_decisions` rows and stored plans from the server, which this session did not have:

1. What share of `NET_RR_TOO_LOW` rejections would have been **approvals at a larger
   capital**? (§6 is why this matters: `min_rr_tp1 = 1.5` sits exactly where the €200
   figure landed.)
2. How many delivered signals **lost a ladder rung to `min_notional`**?

Neither is answerable from a golden — every golden sizes at €10,000 and the owner traded
at €200–€1000.

---

## 12. Hypothesis for the next window — does clustering raise conviction?

**This is about the owner, not about the pipeline, and it is the most actionable line in
this review.** It is recorded as a hypothesis because the window cannot test it.

**The observation.** Under §9's Book B, a 1.5% cap blocks exactly two trades — #12 and
#22 — and those are **exactly the two the owner took with real money**. Two blocks out of
eleven landing on the two TAKEN rows is roughly a 1-in-55 coincidence.

**The hypothesis, stated so it can fail.**

> *A crypto signal published while two or more same-direction signals are already open is
> more likely to be marked ✅ Taken than one published into an empty or single-position
> book.*

The mechanism, if there is one: a third same-direction card arrives with two similar ones
already on screen, and the agreement between them reads as confirmation rather than as
concentration. If true, **the correlation cap and the owner's own conviction pull in
opposite directions** — the rail would be most restrictive exactly where he is most
inclined to act, which is either the strongest argument for it or the strongest argument
that it will be overridden. Worth knowing before either is trusted.

**What answers it, and it is all already stored.** No new instrumentation:

| quantity | source |
|---|---|
| when the card arrived | `signals.created_at` |
| when the button was pressed | `signals.decided_at` |
| what was pressed | `signals.decision` |
| direction | `signals.direction` |
| what was open at press time | `signals.created_at` / `closed_at` of every other signal, `OPEN_STATUSES` |

For each signal, count same-direction signals open at its **`decided_at`** — not
`created_at`. The concurrency that could influence a decision is the concurrency visible
when the button was pressed, and the two differ by however long the owner took to answer.
Then compare `P(TAKEN | ≥2 concurrent same-direction)` against
`P(TAKEN | 0 or 1)`.

**It needs far more than two TAKEN rows.** With a base rate near 2/11 and an effect size
worth acting on (say a doubling), this needs on the order of **50+ decided signals** before
the comparison means anything — which is itself more than M9's unmet 25-signal exit
criterion (§7). **Do not read this from the next handful of trades.** Record the counts
each window and let it accumulate.

**A confound to control for, or the answer will be wrong.** Clustered signals arrive on
trending days, and a trending day changes both the signals and the owner's mood. Split by
day, or compare only signals published within the same 24 hours, so "he takes more on
clustered days" is not mistaken for "he takes the third of a cluster".

## 13. The forex query that decides §8

§8 records that all three forex signals were GBPUSD, and that this may be the rail rather
than the market: `forex.max_concurrent_positions` is **1**, counted across **all users**
(`orchestrator.py:1745`), so any EURUSD or USDJPY candidate reaching the rails while
GBPUSD was open was rejected — and per defect 2, `/pulse` could not show it.

Run on the server, against the app's database:

```bash
docker compose exec -T postgres psql -U sentinel -d sentinel
```

**Query A — what the forex gate actually decided, by symbol and reason.**

```sql
SELECT symbol,
       gate_status,
       COALESCE(reason, '(approved)') AS reason,
       count(*) AS n,
       min(evaluated_at) AS first_seen,
       max(evaluated_at) AS last_seen
FROM gate_decisions
WHERE market = 'forex'
  AND evaluated_at >= TIMESTAMPTZ '2026-08-24 00:00:00+00'
GROUP BY symbol, gate_status, reason
ORDER BY symbol, n DESC;
```

**How to read every possible result:**

| what comes back | what it means | verdict on §8 |
|---|---|---|
| EURUSD / USDJPY rows with `MAX_CONCURRENT_POSITIONS` | the §9 cap was firing and `/pulse` never said so | **artefact.** "All GBPUSD" is the rail. §8's forex paragraph must be rewritten and defect 2 is confirmed as a false picture, not a wording bug |
| EURUSD / USDJPY rows with other codes (`SPREAD_TOO_WIDE`, `EVENT_BLACKOUT`, `BELOW_MIN_TICKET`) | a different rail filtered them | **artefact, different cause.** Still not a market fact; the named rail becomes the thing to review at the ~2026-09-07 spend review |
| EURUSD / USDJPY rows, all `NOT_A_CANDIDATE` | the analyst reached them and declined them | **closest to a finding** — though on one pair-window it establishes very little |
| **no EURUSD / USDJPY rows at all** | they never reached the gate | neither; the filter is upstream — **run Query B** |

**Query B — only if Query A returns no rows for the other two pairs.** A symbol declined
before analysis is recorded in `cycles.skipped`, not in `gate_decisions`
(`orchestrator.py:1173` — *"nothing was evaluated, so there is no verdict"*).

```sql
SELECT kv.key AS symbol,
       kv.value ->> 'reason' AS skip_reason,
       count(*) AS n
FROM cycles c, jsonb_each(c.skipped) AS kv
WHERE c.market = 'forex'
  AND c.started_at >= TIMESTAMPTZ '2026-08-24 00:00:00+00'
GROUP BY 1, 2
ORDER BY 1, n DESC;
```

`MARKET_CLOSED` / `OUTSIDE_SCAN_HOURS` mean the window simply never looked; `NO_DATA`
points at the adapter and is a defect; `OPEN_SIGNAL` / `COOLDOWN` are the book again.

**Query C — was the cap even reachable?** How much of the window had a forex position
open at all:

```sql
SELECT symbol, direction, created_at, first_fill_at, closed_at, outcome, decision
FROM signals
WHERE market = 'forex'
ORDER BY created_at;
```

If GBPUSD held an open signal for most of the window, Query A's answer is close to
predetermined and the cap is doing the filtering by construction.

**Why this is the top next action rather than the cap's number:** it is the only open
question in this file whose answer changes what is written in it. §9's number is settled
and the rail it was for is cancelled.

## 14. Next actions, in order

1. **Run §13's Query A.** It is the only open question in this file whose answer changes
   what is written in it — whether §8's "all GBPUSD" is a finding or an artefact of a rail
   `/pulse` could not show.
2. **Decide defect 1's fix** (§10). T1 is a label, renderer-only, two goldens. Until it
   ships, quote `/stats` figures as gross and the journal's balance as net.
3. **Nothing to decide on the cap.** It is cancelled (see the banner above). The design is
   kept in `journal/M12_REPORT.md` §5 for the day a real book carries two concurrent
   positions.
4. Do not change a threshold on §3. Re-ask it after the next window.
5. Do not move capital mid-window again (§6). It changed which signals cleared the gate,
   not just the euro column.
6. Start accumulating §12's counts now, and do not read them until there are ~50 decided
   signals.
7. M9's exit criteria (§7) are unmet at 9 tracked signals against 25. The next window
   should run to the criterion before conclusions are drawn from it.

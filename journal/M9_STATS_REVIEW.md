# M9 — The measurement window's result

**Window:** 2026-08-15 → 2026-08-29. **Closed by:** M12 (journal/M12_REPORT.md).
**Source:** the owner's `/journal` export of 2026-08-29, 15 rows.

M9 was defined as analysis, not code (`docs/MILESTONES.md` §M9, HANDOFF.md §3). This
file is that analysis. **It changes no threshold**, and one section (§9) exists to put a
decision to the owner rather than to take one.

---

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

**Recorded as a defect, not fixed:** `/stats` and `/journal` should say which of the two
they are showing. Ranked with the others in §10.

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
with `MAX_CONCURRENT_POSITIONS` — and, per §10 defect 1, **`/pulse` could not show it.**
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

### 1 — most serious. `/pulse` silently discards every forex rejection code

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

**It compounds with defect 2 into a coherent and entirely wrong story.** A reader sees
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

### 2 — `/pulse EURUSD` claims a screener that does not exist

The text says *"the screener triages it every cycle"*. Forex has no screener; every pair
buys a full analyst call every cycle, which is why its budget is larger than crypto's
(`config.yaml`, the `llm_daily_budget_global_usd` block).

Second because it is also a false statement about how the system works — but it is static
text a careful reader can catch, and it hides no event. Most of its severity is borrowed
from defect 1, which it compounds.

### 3 — crypto screener calls report `cache_read_tokens: 0`

Prompt caching is not being read on the screener call. Real money, every cycle, and
possibly a symptom of caching being broken more widely than the one call it was noticed
on. Above the Persian freeze because it is unbounded and silent; below 1 and 2 because
nobody concludes anything false from it.

### 4 — Persian summary storage lookup is not wired

Every press regenerates the summary and freezes the UI for about 12 seconds. The
`persian_summaries` table exists (migration `0012`) and is not being read. User-visible
and it damages trust in a surface, but it misinforms nobody, and it has a known one-line
mitigation: show `⏳ در حال آماده‌سازی…` before the call.

### 5 — `/stats` and `/journal` report different R and neither says which

`/stats` computes on `SignalRow.realized_r` (`stats/compute.py:87-93`); `/journal` maps
that same column to **`pnl_r_gross`** and derives `pnl_r_net` beside it
(`stats/journal.py:320-322`). Both are correct and neither is labelled on the surface, so
the same window reads as **+2.86R** in one place and **+2.47R** in the other. It is what
sent the wrong headline figures into the first draft of this review (§1).

Placed fifth, not higher, on the criterion: a reader draws a conclusion that is *directionally*
right and *quantitatively* wrong — the pipeline is profitable either way. But it is the
only defect on this list that has already caused a documented error, which is an argument
for it being higher than its rank, and the argument is recorded rather than acted on.
`min_rr_tp1` gates the **net** figure, so net is the number the system actually decides on.

**The fix is a label, not arithmetic.** Neither number is wrong.

### 6 — `forex.calendar_loaded` logs twice per minute

Log noise. Real, cheap, last.

### On the ordering

**3-versus-4-versus-5 is arguable** — money, a user-facing freeze and a mislabelled
number are not commensurable, and defect 5 is the only one here that has already caused a
documented error. **1-and-2-above-the-rest is not arguable:** those two are the only defects
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

## 12. Next actions, in order

1. **Decide which book the rail reads** (§4a, §9). This comes before the number: on the
   taken book the cap would have changed nothing in this window, on the published book it
   blocks two losses and no winners — and a published-book cap truncates the evidence the
   next window would be measured on.
2. **Answer §9's yes/no.** The cost side is now filled in: **1.5% costs no winners.**
3. **Run the forex `gate_decisions` query** (§10 defect 1). It decides whether §8's forex
   paragraph is a finding or an artefact of an invisible rail.
4. Do not change a threshold on §3. Re-ask it after the next window.
5. Do not move capital mid-window again (§6).
6. Next window: check whether a third same-direction signal is disproportionately likely
   to be **taken** (§9, temper 2). If it is, the cap and the owner's own conviction are
   pulling in opposite directions and that is worth knowing before either is trusted.

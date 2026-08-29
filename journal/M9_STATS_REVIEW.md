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

| | |
|---|---|
| trades | 9 |
| record | 5W / 4L — 55.6% |
| net | **+2.86R** |
| profit factor | **1.84** |

Decomposed from those two aggregates (`W/L = 1.84`, `W − L = 2.86`): gross wins
**6.265R** across five, gross losses **3.405R** across four. So **average win 1.25R
against average loss 0.85R.** The sub-1R average loss is consistent with partial fills
and stops moved to breakeven after TP1 — the ladder doing what it is for.

**The pipeline is not failing.** Nothing in this section justifies touching the gate,
`min_rr_tp1`, `min_confidence` or any setup threshold, and M12 touches none of them.

## 2. Crypto, REAL population

| | |
|---|---|
| trades | 2 |
| record | 0W / 2L |
| net | −1.69R |

**This is selection variance on a two-trade sample. It is not evidence about the
pipeline.** At the measured 55.6% win rate, two consecutive losses happen about 20% of
the time — roughly one window in five, for no reason at all. Two trades cannot
distinguish "the owner picked badly" from "the owner picked normally and got the common
unlucky outcome", and this file declines to try.

The populations stay separate (HANDOFF §5). The REAL figure is recorded because it is
what the owner's money did, not because it measures anything.

## 3. By setup type — recorded, not acted on

| setup | trades | win rate | net |
|---|---|---|---|
| `trend_pullback` | 8 | 37.5% | −0.33R |
| `breakout_retest` | 3 | 66.7% | +1.26R |

**Too small to act on. No threshold moves on n = 3.** Recorded as a **hypothesis for the
next window**: *does `breakout_retest` outperform `trend_pullback`, and if so is it the
setup or the market regime it happened to appear in?* Both arms need an order of
magnitude more trades before the question is even askable.

Note the interaction with §4: `trend_pullback` supplied all four losses, and all four
losses were the same day. The setup split and the cluster are **not independent
observations** — they may be one observation counted twice.

## 4. 2026-08-28, in full

| # | symbol | dir | filled | stopped |
|---|---|---|---|---|
| 18 | AVAXUSDT | long | 05:21 | 13:03 |
| 20 | SOLUSDT | long | 05:31 | 13:04 |
| 22 | DOGEUSDT | long | 05:57 | 13:03 |
| 21 | BTCUSDT | long | 13:08 | 19:02 |

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

### What is still missing, and will not be guessed

**What 1.5% would have cost on the days that WON.** From the four rows in §4 it is
certain that 1.5% blocks **#22 DOGEUSDT and nothing else on 2026-08-28** — #21 filled
four minutes after the cluster cleared. What is *not* known is whether any of the five
winners was a third concurrent same-direction position.

That is one command against the full export:

```bash
.venv/bin/python -m sentinel.tools.concurrency_backtest window.csv --cap 1.5 --cap 2.25
```

`sentinel/tools/concurrency_backtest.py` exists so this number is reproducible rather
than asserted. **Two limitations it prints with every result:** `time_in` is the *fill*
time while the rail fires at *publication*, so every figure is a **lower bound** on what
the cap blocks; and removing a trade and re-summing assumes the others played out
identically, which is the assumption under examination on a day when everything moved
together.

**Until that number exists, §9 is not answerable and the rail is not built.**

### One caveat on the percentages above

Per §6, capital moved mid-window, so R cannot be converted to a percentage of capital
from the export alone. Every figure in this section stated in `%` is the **configured
rail arithmetic** (0.75% per trade against a 2.25% budget), not a measurement of what the
euro column did.

---

## 10. Known defects — recorded, not fixed, ranked

Five carry-forwards. **Ordered by severity, and the ordering is argued** rather than
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

### 5 — `forex.calendar_loaded` logs twice per minute

Log noise. Real, cheap, last.

### On the ordering

**3-versus-4 is arguable** — money against a user-facing freeze is a judgement, not a
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

## 12. Next actions, in order

1. **Run the forex `gate_decisions` query** (§10 defect 1). It decides whether §8's
   forex paragraph is a finding or an artefact.
2. **Export the full window and run the backtest** (§9). It decides the cap's number.
3. **Answer §9.** Until then M12's rail is designed and not built.
4. Do not change a threshold on §3. Re-ask it after the next window.
5. Do not move capital mid-window again (§6).

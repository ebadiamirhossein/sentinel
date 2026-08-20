# SPEC — Telegram Bot UX

**Module:** `sentinel/bot/` (aiogram 3). Single authorized user (Telegram user-id allowlist in `.env`; all other users get silence).

> **Correction (2026-08-19, from M8.1) — the allowlist is now a table, and one
> update is answered.** "Single authorized user … all other users get silence" was
> right for a system with one user and is impossible for one somebody has to be able
> to *join*. Authorization moved to the `users` table (§7); `TELEGRAM_ALLOWED_USER_IDS`
> survives only to name the owner, alongside the new `TELEGRAM_OWNER_USER_ID`.
>
> **Silence is narrowed, not dropped.** Exactly one update from an unknown id is
> answered — `/start`, which files an access request. Everything else from anyone
> not approved gets nothing at all: no refusal, no toast, no confirmation that a live
> private bot is there. `tests/bot/test_auth.py` states the whole rule as a truth
> table over (standing × update kind) and fails if a cell is missing.
>
> This supersedes M6 decision 6 ("an empty allowlist admits nobody"). The
> fail-closed *posture* is kept: an owner id that cannot be resolved is a fatal
> misconfiguration for migration 0007 and a loud warning at boot, never an open door.

---

## 1. Signal card (the core message)

> **Regenerated from real engine output, 2026-08-18 (M6).** Every number below is
> produced by `sentinel/risk/`, not written by hand — reproduce it exactly with:
>
> ```bash
> python -m sentinel.tools.signal --fixture examples/sol_long.json --doc
> ```
>
> The clock is frozen at `2026-08-18T12:00Z` so the card is byte-stable and can be
> re-verified at any time. The analyst report feeding it is the M4 fixture; the
> sizing, costs, RR figures and distances are the live engine's. This replaces the
> hand-written example that M4 flagged as not reconciling with itself, and adds the
> §4.2 cost block that M5.1 introduced.

```
🟢 LONG — SOLUSDT   [trend_pullback · intraday · conf 78]
fable_v1 · Signal #1 · 2026-08-18 15:00 EEST (12:00 UTC)

📊 Thesis
4h uptrend intact; 1h pullback into EMA50 and the prior breakout level 82.4 with volume drying up on the retrace. Funding neutral, OI rising with price. 15m shows demand absorption at the zone top.

⚠️ Against: Fear & Greed at 74 (greed) — crowded-long risk into resistance at 84.6.

🎯 Plan (capital €10000 · risk 0.75% = €75.00 · EURUSD 1.1593)
Entry ladder (limit orders) — last price 83.40:
  1) 83.10 (-0.36%) — 40% of risk — 18.30 SOL (€1311.77)
  2) 82.60 (-0.96%) — 35% of risk — 21.73 SOL (€1548.26)
  3) 82.10 (-1.56%) — 25% of risk — 24.15 SOL (€1710.27)
  Weighted entry 82.675 · avg fill 82.55

🛑 Stop: 81.20 (-1.78%)
❌ Invalidation: 81.40 — 1h close below 81.40
🥅 TP1: 85.20 (+3.05%) — 1.59R net (1.71R gross)
🥅 TP2: 86.60 (+4.75%) — 2.50R net (2.66R gross)
🥅 TP3: 88.90 (+7.53%) — 3.99R net (4.22R gross)

💶 Notional €4570.30 (5298.3430 USDT) · Margin €914.06 · Leverage 5x (isolated)
🧾 Costs: round trip €3.34 = 4.45% of the €75.00 risk budget
    fees maker 0.02% in · taker 0.05% out
    funding ~€0.18 est · 2 settlement(s) @ 8h
✅ Liq. buffer OK (liq ≈ 20% vs stop 1.78%)
⚖️ Actual risk €74.98 (planned €75.00)
📋 TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder or trail by 1xATR.
⏳ Expires if unfilled: 2026-08-19 03:00 EEST (2026-08-19 00:00 UTC)

Research tool — not financial advice. Past stats ≠ future results.

[✅ Taken]  [👀 Watching]  [❌ Skip]
```

Every number comes from `TradePlan` — the bot renders, never computes. Charts (1h + 4h PNGs) attached as an album above the card.

## 2. Buttons & effects

| Button | Effect |
|---|---|
| ✅ Taken | signal → ACTIVE; counts toward open-risk budget and **real** stats; tracker posts fill/TP/SL updates as replies |
| 👀 Watching | tracker follows outcome for **hypothetical** stats only; no risk budget used |
| ❌ Skip | archived; outcome still resolved in background for hypothetical stats (measures what skipping costs/saves) |

After entry fills, ACTIVE signals gain a second row: `[🔚 Closed manually] [✏️ Note]` — manual close asks for exit price to record honest realized R.

> **Deferred to M7 (2026-08-18, from M6).** This row appears "after entry fills",
> and a fill is detected by the tracker, which does not exist until M7. M6 ships
> the three decision buttons and the persisted state behind them; adding a
> manual-close button now would mean either a button that can never appear or one
> that appears on a signal the system cannot tell is filled.
>
> **Shipped (2026-08-18, M7).** The row is appended to the decision row — not
> instead of it — once `signals.filled_qty > 0`. The decisions stay correctable
> after a fill, which is when the owner is tapping fastest and a mis-tap costs the
> most (M6 decision 3).
>
> **How the manual close asks, and why there is no FSM.** aiogram's usual answer to
> "ask a question and wait" is a state machine keyed on the user and held in memory.
> For a trading system that means a restart mid-question silently swallows the next
> thing the owner types — or reads it as an answer to a question nobody remembers
> asking. Instead the prompt is sent with `ForceReply`, its message id is claimed in
> `telegram_messages` with an `event_key` naming the signal and the action, and the
> reply is matched back through that row. The question is as durable as the database.
> `✏️ Note` uses the same mechanism and stores a `NOTE` event.
>
> **Addition (2026-08-18, M7, owner requirement) — a decision replies in words.**
> The keyboard marker (`» ✅ Taken «`) is easy to miss on a phone, and this is the
> single most consequential input in the system: it decides whether an outcome lands
> in the real statistics or the hypothetical ones. Pressing a button now posts a
> short confirmation as a reply under the card, saying what that decision *means*
> (real stats and the risk budget, hypothetical only, or archived-but-still-resolved).
> There is **one** acknowledgement per signal per chat, **edited** when the decision
> changes: a fresh reply per press would bury the card, and leaving the first one in
> place would leave a stale "marked Taken" under a signal the owner later skipped.

## 3. Commands

| Command | Behavior |
|---|---|
| `/capital 10000` | Set capital EUR (validated > 0); confirms; applies to new signals only |
| `/risk 0.75` | Set per-trade risk % (0.25–1.5 enforced) |
| `/status` | Pipeline health: last cycle time, data sources OK/degraded, active signals, open risk used, paused? (see the M6 note below) |
| `/positions` | All ACTIVE signals with live uPnL in R and EUR |
| `/stats [30d|90d|all]` | Real stats (taken): count, win rate, avg R, profit factor, max DD; then hypothetical (watched/skipped); breakdown by setup_type and prompt version |
| `/pulse [24h\|SYMBOL]` | **Superseded 2026-08-20 (M8.4/M8.5) — see §3b.** Was: on-demand market regime summary (P1) |
| `/analyze SOLUSDT` | Force deep analysis of one symbol now (P1) |
| `/watchlist [add|remove SYMBOL]` | View/edit watchlist |
| `/pause` / `/resume` | Manual pause; resume from loss-limit pause requires confirming button "Yes, resume" |
| `/settings` | Show all runtime config values (see the M6 note below) |
| `/start` | **M8.1.** Request access (unknown id), or a set-up summary (approved) |
| `/help` | **M8.1.** What every number on a card means, in plain language |
| `/journal [30d\|90d\|all]` | **M8.6.** The caller's own book as an XLSX document — see §3c |
| `/snapshot <SYMBOL>` | **M8.6.** Everything code measured before the AI ran — see §3d |
| `/leave` | **M8.1.** Remove yourself; one confirmation, effective immediately |
| `/users` | **M8.1, owner only.** Who has access, who is waiting, who is stuck |
| `/approve <id>` / `/reject <id>` / `/suspend <id>` | **M8.1, owner only** |

> **Scope at M8.1 (2026-08-19, owner ruling) — the commands split in two.**
>
> A **member** may run `/start /help /capital /risk /positions /stats /leave`. Each
> is scoped to the caller: their own capital, their own risk %, their own signals,
> their own statistics.
>
> Everything else is **owner-only**: `/status`, `/settings`, `/watchlist`, `/pause`,
> `/resume`, `/users`, `/approve`, `/reject`, `/suspend`. `/status` and `/settings`
> because they report LLM spend and pipeline health, which are the operator's
> business and nothing a member could act on; `/watchlist` because it decides what
> the shared analyst spends the owner's key on; `/pause` because it is the system's
> stop button (see §7's split from the per-user loss pause).
>
> **A non-owner gets silence, not a refusal.** Owner handlers sit behind an
> `OwnerOnly` filter on their own router, and a filter that does not match means no
> handler runs — so a member typing `/approve` learns nothing: not that the command
> exists, not that they lack the role, not that anybody has it.
>
> **The `/` menu is registered** with `setMyCommands` at startup, in three scopes:
> `/start` and `/help` for every private chat, the member set for an approved
> member's own chat, and both halves for the owner's. The menu is therefore also an
> access-control surface — advertising `/approve` to everyone would undo the silence
> above. Scopes are re-published on approval and cleared on rejection, suspension
> and `/leave`. A failed `setMyCommands` never stops polling: the menu is a
> convenience, the signals are the product.

> **Scope at M6 (2026-08-18) — degrade explicitly rather than fabricate.**
> Three entries in this table ask for state the tracker and the cycle orchestrator
> produce, and neither exists before M7. The rule from CLAUDE.md applies: never
> invent a data path, degrade in words.
>
> * **`/positions`** lists every signal marked ✅ Taken with the plan as issued, and
>   says so. "Live uPnL in R and EUR" needs a mark price and the ladder-aware R
>   accounting already written in `risk/accounting.py`; computing it in the bot
>   would both duplicate that math and break §1's "the bot renders, never
>   computes". It arrives with the tracker.
> * **`/status`** reports what is measured: pause state and its reason, capital and
>   risk %, watchlist size, the newest snapshot per symbol with its OK/DEGRADED
>   quality, signal counts by decision, and any Telegram message claimed but never
>   confirmed (§6). Cycle timing and open-risk usage are named as not-yet-measured
>   rather than shown as zero.
> * **`/settings`** shows the specs/RISK_ENGINE.md §1 parameters, the §4.2 cost
>   parameters, the pipeline settings and the watchlist — each tagged `db` or
>   `yaml` so precedence is visible. Not the whole of `AppConfig`: that runs to
>   thousands of characters, most of them endpoint URLs, against Telegram's 4096
>   limit for one message.
>
> `/stats`, `/pulse` and `/analyze` are **not registered** at M6 — the first needs
> outcomes the tracker measures, the other two are P1. An unregistered command is
> silent rather than answered with a promise.

> **Scope at M7 (2026-08-18) — the three gaps above are closed.**
>
> * **`/positions`** marks each Taken signal to market: filled %, the tracker's last
>   observed price, open PnL in R and EUR, what is already banked, and the stop's
>   current level once §5's TP1 rule has moved it to breakeven. Every figure is
>   computed in `risk/accounting.py` and arrives on a view object — §1's rule is
>   unchanged, the bot still renders and never computes.
> * **`/status`** adds what it could only name before: the last cycle and the 30-day
>   completion ratio (PRD G4), open risk against the budget, positions against the
>   cap, signals published today against `max_signals_per_day`, and the LLM spend
>   line described below. It leads with a **DRY RUN** banner when the system is
>   rehearsing, because a silent phone must never be ambiguous between "no setups"
>   and "not talking".
> * **`/stats [30d|90d|all]`** is registered. Three populations, reported separately
>   and never merged: **REAL** (✅ Taken), **HYPOTHETICAL** (👀 Watching + ❌ Skipped)
>   and **DRY RUN**. A win is realized R > 0 over signals that filled at least one
>   rung; a signal that expired or invalidated before entry is counted on its own
>   line and excluded from the win rate, the average, the profit factor and the
>   drawdown, because it was not a trade. A breakeven exit is a *scratch* — neither
>   a win nor a loss, and reported. Profit factor with no losses is rendered
>   "n/a (no losses yet)", not a number. PRD G3's "reached TP1 or breakeven" is
>   reported beside the win rate rather than instead of it. Breakdowns by
>   `setup_type` and `prompt_version` (specs/PROMPTS.md §5 step 3) run over real +
>   hypothetical, largest sample first.
>
> **Addition (2026-08-18, M7) — the LLM spend line on `/status`.** Pulled forward
> from M8 at the owner's request, because M7 is the first milestone that runs
> unattended: today's and this month's estimated spend, the daily limit, and whether
> deep analysis is currently suspended. It gates `cost_usd_estimate`, which is an
> estimate and not a bill — and because `llm/pricing.py` prices an *unknown* model at
> 0 with a warning, calls with no configured price are counted separately and the
> figure is reported as a floor ("at least $4.00") rather than as a total. Reaching
> the limit suspends **new deep analysis only**: the screener keeps triaging and the
> tracker keeps managing open positions, which it can do without an LLM at all.

## 3a. `/request` — a member asking for a symbol (added 2026-08-19, M8.3)

**In no earlier version of this spec; recorded as an addition, not a deviation.**

`/watchlist add|remove` is owner-only because the watchlist is what the shared deep
analyst is pointed at: every symbol on it is screened every cycle and each one can
buy a ~$0.28 analyst call on the owner's key. A member editing it would be spending
somebody else's budget. But "not yours to edit" is not the same as "not yours to
suggest", so members get a door:

```
/request SOLUSDT
```

1. The symbol is validated **at the door** — the same `parse_symbol` → cached
   `instrument_meta` → keyless exchange call path `/watchlist add` uses. A typo is
   answered in a second by the person who made it, rather than arriving as a card the
   owner cannot evaluate.
2. The owner gets a card naming **who asked, what for, when, and where the watchlist
   stands against its cap**, with `[✅ Add to watchlist] [❌ Decline]`.
3. On approve the symbol joins the watchlist through the *same* write `/watchlist
   add` makes, so the `config_changes` audit row is identical either way, and the
   requester is told. On decline the requester is told, and **no reason is required**.

**One pending request per symbol**, guaranteed by a partial unique index over
`symbol WHERE status = 'PENDING'` rather than by a check — two members asking within
a second of each other produce one row and one card. A duplicate is a no-op, and the
second asker is told the symbol is spoken for **without being told by whom**: who else
uses this bot is not a member's business (§7's boundary).

**`watchlist_max_symbols` (default 15) binds everybody, the owner included.** It is a
spend control, and a cap that applied only to other people would not be a cap. It is
checked when a request is made *and again at the moment of approval*, because several
requests can be pending against fewer free slots and only the approval knows what the
list actually holds.

**A request the owner tries to approve into a full watchlist stays `PENDING`.** It is
not a rejection — nobody decided against the symbol, the list simply ran out of room
while it waited — so both people are told exactly that, and the owner can make space
and approve the same card instead of the member having to ask again.

`/request` is on the member menu and **not** on the owner's, for the same reason
`/leave` is not: the owner has `/watchlist add`, which does the thing directly, and a
request path would have them approving their own card. Both handlers still answer if
typed.

## 3b. `/pulse` — the pipeline's own story (added 2026-08-20, M8.4)

**This replaces §3's `/pulse` row, which read "on-demand market regime summary (P1)".**
That command was never built and never registered. The name is reused rather than
kept free because it is the right name for this and there is no stored value, no
menu entry and no user habit to break — nothing existed under it. A market-regime
summary is still wanted and is still P1; it will arrive as part of §5's digest,
where a regime line is already listed.

`/pulse` is **available to every approved user, member and owner alike** — the only
command in the system that is. Everything else here is either scoped to the caller
(`/positions`, `/stats`, `/capital`, `/risk`) or owner-only (`/status`,
`/settings`). This one is neither, and the asymmetry it removes is the point:

> One analysis per cycle, shared (§7). The *reasoning* behind it is therefore the
> one thing in this system that genuinely is identical for everybody, so it is
> shown identically to everybody.

It answers the gap journal/M8_1_REPORT.md §13 left open — a member "cannot tell
'quiet market' from 'system down'" — which M8.2's spend controls widened by making
quiet the expected state.

**`/pulse`** renders the **last completed cycle** (not the one currently running:
a cycle still in its screener has escalated nothing yet, and would read exactly like
a broken one). Four sections:

1. **Screener** — which symbols were escalated, with the one-line reason each was
   given, and how many were scanned.
2. **Not analysed** — every `cycles.skipped` entry with its `SkipReason` in words.
3. **Analyst** — each verdict: status, confidence, setup type, and a one-line thesis.
4. **Gate** — approved, or the rejection reason code.

**`/pulse 24h`** is the same four sections counted rather than listed, over
completed cycles in the last day, with the completion ratio in the header.

**`/pulse SOLUSDT`** (added 2026-08-20, M8.5) is the drill-down, and the deliberate
opposite of the two above: they cut every piece of prose to fit several symbols on a
phone, and this one **cuts nothing.** It shows one symbol's last analyst verdict in
full — the untruncated thesis, the evidence behind each claim with the snapshot field
it rests on, the counter-thesis, confidence, setup type, the invalidation text, the
model's own data-quality note when it raised one, provenance, and what the gate did.

*If it does not fit in one Telegram message it is **split across messages**, never
shortened.* A drill-down that abbreviated would have no reason to exist. How many
messages is not knowable in advance — `Evidence.claim` carries no length bound —
so the renderer paginates and the handler sends what it is handed.

**It shows no prices.** The stored report carries the analyst's `entry_zone`, `stop`,
`targets` and `invalidation_price`, and none of them appear: printing levels for a
symbol the gate *rejected* would be an unsized trade suggestion with no approval
behind it — a signal card with the safety removed. The prose `invalidation_text` stays
(it is the one field that says what would make the idea wrong, and it is on the signal
card too); the number does not. The boundary is the view type, which has no field for
a price.

The gate half folds through the **same** `gate_outcome` the summary card uses, so the
shared/personal split below is applied once and cannot drift between the two surfaces.
Two states get their own sentence rather than an empty section: a verdict with no
`cycle_id` (the column is nullable) cannot be matched to a gate run, and a `WATCHLIST`
or `NO_SETUP` verdict never reached the gate at all — the orchestrator returns before
it — which is the expected state for the commonest verdict in the system.

A symbol with nothing stored gets one of two answers, because they are different
problems: on the watchlist and never escalated far enough to be analysed, or not on
the watchlist at all (with `/request` and `/watchlist add` named).

The three argument forms cannot collide: `parse_symbol` requires five alphanumeric
characters and every window word (`24h`, `day`, `1d`, `today`) is shorter. The window
is still matched first, and an argument that is neither gets the usage line rather
than silently falling back to the bare form.

It is **read-only**: no new table, no new column, no write of any kind. The screener's
verdicts come out of the `llm_calls` audit row (they have never had a table of their
own), the skips out of M8.2's `cycles.skipped`, the verdicts out of `analyst_reports`,
the outcomes out of `gate_decisions`, and the per-cycle cost off the `cycles` row.

### What it must never show, and how that is guaranteed

**No per-user information of any kind** — no capital, no sizing, no decision, no
statistic, no user id, no count of users. Two of its four sources contain rows
derived from somebody's account, so the boundary is two **closed classifications**
with meta-tests, in the manner of §7's `UserView` and `bot/auth.TABLE`:

**1. `RejectionReason` splits into shared and personal.** `gate_decisions` holds one
row per (cycle, symbol, **user**). The line falls exactly where `RiskEngine.evaluate`
stops reading `(report, market, config)` and starts reading `AccountState` and
`PortfolioState`: everything above it is provably identical for every user and is
**named**; everything below it is arithmetic on somebody's money and is folded into
*"cleared the shared checks — the rest is per-account"*. Two entries that look shared
and are not:

* **`PAUSED`** is personal here. The operator's system-wide `/pause` never reaches
  the gate — the orchestrator stops earlier, at `SkipReason.PAUSED` — so a `PAUSED`
  row in `gate_decisions` can only be a user's own daily-loss pause (§7), which says
  they lost money today.
* **`NET_RR_TOO_LOW`** is personal. Cost-as-a-share-of-risk is scale-invariant, so it
  is *nearly* shared, but it is measured after ladder sizing and rung collapse, which
  depend on capital. The boundary has to be provable rather than nearly true.

**An approval is shown** — `✅ approved` — because it names no user and carries no
size: it says a plan cleared every rail somebody had, which is a fact about the
pipeline. Where one user was approved and another rejected on a personal rail, the
approval is what is reported; reporting the rejection would be reporting an empty
`/capital`.

**2. `SkipReason` is rendered through wording that names nobody.** All seven appear.
`cycles.skipped` is already union-level — a symbol lands there only when *no* eligible
user could have received it — and the three book-derived reasons are phrased about the
symbol rather than about whoever holds it (`OPEN_SIGNAL` → "an open signal is already
running on it"). The stored `detail` is **never rendered**: every one of the seven
either restates its own key or appends a raw timestamp, and two of those timestamps
are clock readings off somebody's last trade. It stays in the column for M9.

### The one thing the owner sees and a member does not

**LLM spend** — the cycle's own estimated cost and the day's total against the limit.
§7's owner-only channels: it is the owner's bill, and a member has no lever to pull in
response to it. It is carried as `PulseView.spend is None` for a member rather than as
a role check in the renderer, so the member's view object holds no figure to leak —
the same mechanism §7's `UserView` uses for capital.

### Registered on both menus

`/pulse` is on `MEMBER_COMMANDS` and reaches the owner's menu through the same
`commands_for` that already gives them the rest of the member set. It is deliberately
**not** on `OWNER_COMMANDS`: that half is the operator's, and putting it there would
take it off every member's menu. `tests/bot/test_dispatcher_wiring.py` proves it
reaches a handler **for both roles** against a real `Dispatcher`, and that a caller
with no standing still gets silence.

## 3c. `/journal` — the caller's own book, as a file (added 2026-08-20, M8.6)

**In no earlier version of this spec; recorded as an addition, not a deviation.**

`/stats` gives a member their aggregate and `/positions` gives them what is open
right now. Neither gives them the *rows* — the per-signal record a person reviews,
sorts and keeps. PRD F10 says a signal is fully reconstructable from the database,
and it is, by somebody with an SSH session. Nobody else could get their own history
out of this system at all.

```
/journal            → the whole history
/journal 30d | 90d | all
```

**The default is `all`, where `/stats` defaults to 30d.** A statistic is a report
about a recent period; an export is an archive. An argument that is neither a window
nor absent gets the usage line rather than a silent fall back to the default — M8.4's
ruling for `/pulse 7d`, because somebody who asked for a week and received two years
would read the file as a week's worth. The window is applied to `created_at`, not
`closed_at`: an export is windowed by when a signal was *issued*, or one sent inside
the window and still running would fall out of its own journal.

**One sheet per population, and they are never mixed.** `Real (Taken)`,
`Hypothetical` (👀 Watching + ❌ Skipped), and — only when they hold rows —
`Undecided` (no button was ever pressed) and `Dry run`, plus a `Legend` sheet. Every
row carries the population as a column as well, beside a separate **`Decided`**
column showing what was actually pressed: the two differ in exactly two places, since
Watching and Skipped share a population and a rehearsal has a population and no
decision at all.

The Real and Hypothetical sheets are written even when empty, so "you have none of
these" reads as an answer; the Legend counts all four regardless, so an absent tab is
never something a reader has to notice.

**The running columns are per sheet.** `Running R (net)` and `Win rate % (gross)` are
cumulative, and a series that walked from a trade somebody placed into one they
skipped would be a number nothing in this system endorses (§7, and `stats/models.py`'s
three populations "reported separately and never merged").

Columns: signal number, symbol, direction, setup, confidence, decided, population,
time in (first fill), time out (resolution), avg entry, avg exit, size EUR, leverage,
SL %, risk EUR (1R), PnL R gross, costs EUR, PnL R net, PnL EUR gross, PnL EUR net,
outcome, running R, win rate %.

**Euro figures appear on the Real sheet only.** Elsewhere nobody placed the trade, so
a euro figure would assert money that was never at risk. The R columns stay on every
sheet, which is the point of resolving a hypothetical at all.

Three definitions are settled here because each is a place this file could quietly
disagree with a surface that already exists:

* **1R is `planned_risk_eur`**, and the `Risk EUR (1R)` column is that number rather
  than `plan.risk_eur`, the step-floored actual. Against the wrong basis the gap
  between `PnL R x Risk EUR` and `PnL EUR` would be a *bias* growing with the figure
  rather than the half-a-hundredth-of-R rounding it is. R stays at 2dp, as on every
  card; the euro columns are the exact ones and the Legend says so.
* **Net subtracts costs from the numerator only** — deliberately *not*
  `risk/costs.net_rr_multiples`' convention, which also grows the denominator. That
  function prices a *prospective* trade where the cost is money additionally at risk;
  here the trade is over, R is a fixed unit of account and a running balance has to
  add up.
* **A win is counted on gross R**, exactly as `/stats` defines it, while the running
  balance is net. The win rate at the bottom of the Real sheet therefore equals what
  `/stats` reports for the same window. A trade can win gross and lower the balance
  after costs; that is true of `/stats` too, and the Legend says it.

**Open signals are rows with the running columns blank** (owner ruling). They sit at
the bottom of their sheet and join the balance and the rate when they resolve. Had
they participated, every earlier row's balance would silently renumber on the day an
open trade closed and landed elsewhere in history — a record that reorders itself is
not a record. Blank means "not yet part of this"; a zero would claim the trade
contributed nothing. A signal that never *filled* is a row too, and moves neither
figure: the price reached no entry rung, so it was not a trade (`stats/compute.py`).

**An empty journal is a sentence, not an empty file.** A workbook of nothing but
headers is indistinguishable from a broken export, and the reader would open it to
find out which it was.

### Strictly caller-scoped, and how that is guaranteed

Unlike `/pulse`, this is the most per-user surface in the system, and it is the only
one whose output leaves the chat as a **document** — forwardable, saveable, and
outliving its caption. Three mechanisms carry the scoping, none of them a filter
somebody has to remember:

1. `SignalRepository.journal_since` takes a keyword-only `user_id` with no default.
2. `stats.journal.JournalRow` has **no field** that could hold a user id, in the
   manner of §7's `UserView` and M8.4's `PulseGateView`.
3. The file is sent to the caller's own id rather than to the chat the command
   arrived in, and the **filename carries no id and no name** — it is the part of a
   document that travels furthest.

`tests/bot/test_journal.py` proves user A's file cannot contain user B's rows three
ways: at the view level, by reading the workbook back cell by cell, and by scanning
the raw XLSX zip's XML. The third is not redundant — a value can reach the artefact
without reaching a cell a reader would see — and a fourth test deliberately builds a
leaking file and asserts the same checks catch it.

**XLSX generation uses `openpyxl`** (owner-approved 2026-08-20 under CLAUDE.md's
dependency rule): 250KB wheel, ~1.3MB unpacked, pure Python with no native
extension. XLSX numbers are IEEE doubles, so `Decimal` ends at the file boundary
whatever writes it; every figure is asserted to round-trip through the file
unchanged, and the money columns carry a 2dp format so what Excel displays is the
cent figure `risk/rounding` produced.

## 3d. `/snapshot <SYMBOL>` — the deterministic view, before the AI (added 2026-08-20, M8.6)

**In no earlier version of this spec; recorded as an addition, not a deviation.**

§3b's `/pulse` made the pipeline's *reasoning* visible, and every word of it is model
output. This is the other half, and it has no model in it: the regimes, EMAs, RSI,
ATR, relative volume, funding, open interest, support and resistance with their touch
counts, orderbook imbalance, Fear & Greed, BTC dominance and data-quality flags that
`features/` and `ingestion/` compute and store on every `market_snapshots` row — and
that the analyst is then handed. It has been in the database since M1 and readable
only over SSH.

The point is that a reader can now check the analysis against its own inputs: when a
card claims "1h pullback into EMA50", the EMA50 the model was actually given is nine
characters away.

**Shared market data, so the same access as `/pulse`** — every approved user. Unlike
`/pulse` there is not even a spend line to differ on, so the owner and a member get
byte-identical text, and the handler takes no `Actor` at all: a parameter it does not
receive is a boundary it cannot cross. `SnapshotView` carries no user id, no capital,
no sizing and no decision, and a meta-test asserts no such field appears.

Sections: last price and capture time (local + UTC) · per-timeframe block for
15m/1h/4h/1d — trend regime with its `RegimeBasis`, volatility regime, EMA20/50/200,
RSI14, ATR14 and ATR%, relative volume, EMA stack, bars used · support and resistance
zones with touch counts and signed distance, nearest first · funding rate and next
settlement, open interest and its 24h change · orderbook imbalance, spread and top of
book · Fear & Greed with yesterday's delta · BTC dominance and total market cap 24h ·
data quality with the degraded fields **named**.

The card is labelled **"computed by code, before any AI analysis"** and points at
`/pulse <SYMBOL>` for the model's read of the same data. A wall of indicators that a
reader might mistake for an opinion would be worse than no card.

**Every absent block is named as absent**, never dropped and never zero-filled: a
funding rate that was not fetched is not a funding rate of zero, and telling those
apart is half of why this command exists. A `RegimeBasis` of `REDUCED` is shown
beside the regime rather than rounded off, because a trend classified without an
EMA200 is not the same finding as one classified with it. A stored block that no
longer validates costs that section and not the card.

It reads the **newest stored snapshot whatever its age** and prints when it was
captured — M8.5 decision 3's reasoning: a window would turn a stale answer into *no*
answer, which reads as "never ingested" and is a different fact. There is no exchange
round trip: the question is what the pipeline measured, not what is true this second.

A symbol with nothing stored gets one of the two answers `/pulse <SYMBOL>` already
distinguishes — on the watchlist and not yet ingested, or not on the watchlist at all.

Levels are capped at 8, nearest first, with the count of what was left out shown
beneath them (M8.4's no-silent-caps rule).

Both commands are **read-only**: no new table, no new column, no migration, no write
of any kind. Both are on `MEMBER_COMMANDS` and reach the owner through `commands_for`
— deliberately not on `OWNER_COMMANDS`, which would take them off every member's menu
— and `tests/bot/test_dispatcher_wiring.py` proves each reaches a handler **for both
roles** against a real `Dispatcher`, with silence for a caller with no standing.

> **A member command that fails now says so (added 2026-08-20, M8.6).** Every
> handler on the member router is wrapped in `answers_on_failure`, which replies
> *"something went wrong handling that"* and logs the traceback rather than letting
> aiogram swallow the exception. journal/M8_2_REPORT.md §1 found nine owner commands
> silent for a week for exactly this reason, and M8.6 shipped a `/snapshot` that was
> silent for six of ten symbols for the same one. On a bot where **silence is the
> designed response to anyone without standing** (§7), a crash and a command that
> does not exist are indistinguishable — so the one caller positioned to report it is
> told. It is a decorator on named handlers rather than a router-level error handler:
> the latter would also catch anything raised inside the authorization gate, and
> replying there would answer a stranger whose whole guarantee is that they hear
> nothing.

> **`/help` splits across messages from M8.6.** It reached 3,859 of Telegram's 4,096
> characters, and explaining two more commands needed ~470. The choice was to cut
> carefully-written explanation or to split, and M8.5 §3b already settled that for
> this codebase: split, never shorten. `help_card()` returns pages through the same
> `_paginate` `/pulse <SYMBOL>` uses.

## 3e. Markets on the surfaces (added 2026-08-20, M10a)

M10a gives the system a market dimension without adding a second market. This section
fixes what every user-facing command does with it, once and in one place, so six
renderers do not each decide it slightly differently.

**The default: no argument covers every enabled market, in the order `config.yaml`
names them.** Not "the first one", and not "crypto". A reader who types `/stats` and
is shown one market's numbers, with nothing on the card saying another exists, has
been told something false by omission — the failure mode this project has already met
twice (the dead admin router, the silent `/snapshot`).

**The rule that protects the live system: with one market enabled, every surface
renders exactly the text it rendered before M10a.** One function decides it —
`bot/markets.section_header`, which returns an empty string unless
`AppConfig.multi_market` — and `tests/golden` pins every surface byte for byte, both
from the repo's config and from the frozen pre-M10a config the server actually loads.

| Command | With one market | With two |
|---|---|---|
| `/status [market]` | unchanged | **one market at a time**, defaulting to the first enabled; every figure on the card is per market and summing two would produce totals no rail compares anything to |
| `/stats [market\|window]` | unchanged | one block per market, each headed; never a combined figure |
| `/pulse [market]` | unchanged | one "last completed cycle" block per market — the scheduler runs a job per market, so there is one answer each and none across them |
| `/pulse 24h` | unchanged | one aggregated block per market |
| `/pulse <SYMBOL>` | unchanged | the **symbol names its market**; a reader never types one |
| `/snapshot <SYMBOL>` | unchanged | as above |
| `/positions` | unchanged | one block per market; the mark, the R and the euro figures come from one market's own feed |
| `/watchlist [market] [add\|remove SYM]` | unchanged | lists every market; an edit names its market, and only needs to when more than one is enabled |
| `/journal [window]` | unchanged sheet names | one sheet per (population, market) pair — a running balance walks *within* a sheet, and one stepping from a EUR/USD trade into a BTC one would mean nothing |
| `/pause [market]` · `/resume [market]` | unchanged | **no argument is still system-wide.** `/pause` is what somebody types when something is wrong, often on a phone, often in a hurry, and it must not quietly have become narrower than it was. A market is named by typing it. |

**A disabled market named explicitly is an error, not an empty answer.** Somebody
asking for forex today is told it is switched off; handing them a blank card they
would read as "no signals yet" is the same lie by omission as above.

**A market name is not a window.** `/stats forex` must not fall through
`parse_window`'s "anything unrecognised is 30d" branch — right by accident today, and
wrong the day a market is named `all`.

**The signal card** takes the tag only when more than one market is enabled, and the
tagged card differs from the untagged one by the tag alone (asserted, not assumed).

## 4. Tracker notifications (replies to the original card)

- `📥 Entry 1 filled @ 83.10 (40%)` … `📥 Ladder complete, avg 82.68`
- `🎯 TP1 hit @ 84.90 → close 40%, stop moved to BE (per plan)`
- `🛑 Stopped @ 81.20 → −0.72R (partial ladder: only rungs 1–2 filled) · −€54`
- `❌ Invalidation triggered (1h close 81.05 < 81.40) before entry → signal cancelled`
- `⌛ Expired unfilled after 12h`
- `⏸️ Daily loss limit reached (−3.1%). New signals paused 24h. /resume to override.`

> **Implemented (2026-08-18, M7).** Each bullet is one `EventKind`, rendered by
> `bot/cards.tracker_update_card` from a stored `signal_events` row — every number
> on the reply was computed by the tracker, because the renderer is forbidden
> arithmetic (§1) and re-deriving realized R here would be a second implementation
> of specs/RISK_ENGINE.md §8.5's math.
>
> The chat-level notices — the daily-loss pause, and M7's two spend notices (warn
> level crossed, limit reached) — are not tied to a signal, and
> `telegram_messages.signal_id` is `NOT NULL`. They claim a deterministic
> `uuid5(<kind>, UTC date)` instead, so each is sent once per day and a restart
> never repeats it. No column was made nullable to achieve that.
>
> **Dry-run signals are never posted at all.** The tracker resolves them exactly as
> it would a real one and the notifier skips them, so a rehearsal day produces a
> measured record and a completely silent phone.

## 5. Digest (P1)

Daily 08:00 (owner timezone, config): yesterday's signals & outcomes, running week stats, market regime line, any degraded data sources.

## 6. Non-functional

- All messages idempotent (message ids stored; restarts never double-post).

> **How, and what happens in the crash window (2026-08-18, from M6).** A row in
> `telegram_messages` unique on `(signal_id, kind, chat_id)` is claimed `PENDING`
> and committed *before* the send, then updated to `SENT` with the returned message
> id. Claiming after the send would lose the id if the process died in between; a
> single transaction around both would roll the claim back on a crash and re-post
> the card on the next start.
>
> A process that dies between the send and the confirmation therefore leaves a
> `PENDING` claim, and that claim is **never retried**. A duplicated signal card is
> worse than a missing one the owner can ask for again — the duplicate looks like a
> second setup. Every stuck claim is logged and counted in `/status`, so the gap is
> visible rather than silent.
- No signal spam: hard cap `max_signals_per_day` (default 5) and per-symbol cooldown.

> **Implemented (2026-08-18, M7).** The cap had no config key and no rejection code
> until now. It is `risk.max_signals_per_day`, enforced in `check_portfolio_rails`
> as `RejectionReason.DAILY_SIGNAL_CAP` — its own code and not folded into
> `MAX_POSITIONS`, because "the account is full" and "the system has said enough for
> one day" are different findings and M9 cannot separate them if they share a code.
> The orchestrator also stops before the deep analyst once the cap is reached, and
> counts what the current cycle would add, so one cycle cannot publish three more
> and overshoot by two. Dry-run signals count: the cap is part of what a rehearsal
> day is meant to rehearse.
>
> **Correction (2026-08-18, from M7) — the message key is four columns.** §6's
> unique `(signal_id, kind, chat_id)` allows exactly **one** update per signal, and
> §4 describes a thread of a dozen: three fills, ladder-complete, three TP hits, a
> stop, an expiry, a manual close. `telegram_messages` gains `event_key` and the
> constraint becomes `(signal_id, kind, chat_id, event_key)`.
>
> The column is `NOT NULL DEFAULT ''`, and that is the load-bearing detail:
> Postgres treats two NULLs as **distinct** inside a unique index, so a nullable
> column would have silently un-guaranteed the *card's* own idempotency — the exact
> promise this table exists to make. Existing rows backfill to `''` and keep it.
>
> Recording an event and posting it stay separate steps, each idempotent on its own
> key (`signal_events` on `(signal_id, event_key)`). A crash between them therefore
> resolves forward: the event is written and not yet posted, and the next tick posts
> it. There is no state in which an event is both lost and believed sent.
- Language: English v1 (Farsi toggle listed as P2).

> **Timestamps (2026-08-18, from M6).** specs/DATA_SOURCES.md §4 says the owner's
> timezone is applied at Telegram render time, while §1's original example printed
> UTC. Both: `2026-08-19 03:00 EEST (00:00 UTC)`, from `telegram.owner_timezone`.
> Local is what the owner acts on at 3am; the UTC figure is what every log line and
> database row carries, so a card can always be matched to its audit trail by eye.
- Every card footer: `Research tool — not financial advice. Past stats ≠ future results.`

## 7. Users, roles and approval (M8.1, 2026-08-19)

One analysis per cycle, shared. Sizing, rails, decisions and statistics per user.
The analyst is never run per person: its output is a judgment about a market, and
at ~$0.32 a call it is the most expensive thing in the system.

**Standing.** A row in `users` per Telegram id, with one of five states —
`PENDING`, `APPROVED`, `REJECTED`, `SUSPENDED`, `LEFT`. Only `APPROVED` receives
anything; the other four are the same silence and four different facts, so each has
its own sentence when the person asks.

**Joining.** An unknown id sends `/start`. That files a request and messages the
owner once, with a `[✅ Approve] [❌ Reject]` row — **once per id, ever**, guaranteed
by the table's primary key rather than by a counter, so a restart can neither lose
nor duplicate it and a stranger cannot spam the owner by tapping. Re-`/start` is a
no-op that answers them with where they stand, throttled to one reply per hour.

**Approval and the first-run acknowledgement.** Approving publishes the member's
command menu and sends two messages: a welcome, and a note they must accept before
anything is delivered — *this system is experimental, its win rate is not yet
measured, signals are research and not advice, you place every trade yourself, and
you can lose money*. The acknowledgement is stored with a timestamp **and the
wording's version**: a disclaimer somebody agreed to only means something if the
words they saw are identifiable, so bumping `ACK_VERSION` asks everybody again.

**Leaving.** `/leave` is a member's own decision, one confirmation deep. Standing
becomes `LEFT`, the menu is cleared and delivery stops immediately. Their history
stays — deleting measured history is never automatic — and they appear as `LEFT` to
the owner, who can re-approve them if they ask.

**Per user:** capital, risk %, sized `TradePlan`, portfolio rails (open risk, max
positions, cooldowns, daily-loss pause), decisions, and statistics. A user whose
capital is unset is still evaluated by the gate, rejected with `NO_CAPITAL`, and
**told why** — at most once per UTC day, and only on a day when a plan was actually
approved and could not be sized for them.

**Owner-only channels.** The spend guard, cycle-failure alerts and ops notices go to
the owner alone. A member has no lever to pull in response to any of them.

**`/users`, and the boundary it keeps (owner ruling).** The card shows each member's
standing, when they joined, whether a capital is set (**yes or no**), and whether a
daily-loss pause is currently holding them. It shows **no amount, no risk %, no
P&L, no win rate and no decision.** Operating a system for friends requires knowing
who is set up and who is stuck; it does not require watching them trade, and a card
that showed both would make the second happen by accident every time the first was
needed. The boundary is a type — `UserView` carries no field a future card could
print — and `tests/bot/test_registration.py` asserts that it stays that way.

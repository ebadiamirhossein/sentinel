# M8.2 — Spend control, and the admin surface that was not there

**Date:** 2026-08-19
**Status:** DONE locally — `make check` green (1159 hermetic tests, **1193** with the
Postgres suite), `mypy --strict` clean over 213 files, **100% branch coverage on
`sentinel/risk/`**, migration `0008` applied, reversed and re-applied against a real
database, `alembic check` reporting no drift. Not yet deployed; §9 is the deploy plan.

This milestone has two halves that arrived in the wrong order. The intended work was
spend control — the deployed system was burning $5.82 in two hours against M7's
~$2.2/day estimate. The unintended work is §1: deploying M8.1 revealed that its
entire owner-command surface had never worked, and the way it failed is the more
important finding of the two.

---

## 1. The bug: every owner command was silent, and silence was the design

`/pause` produced no reply. `/start` and `/help`, sent a minute later, both worked.

```
Update id=311136588 is not handled. Duration 1 ms
Cause exception while process update id=311136588
TypeError: OwnerOnly.__call__() missing 1 required positional argument: 'actor'
  ... router.py:182 _propagate_event → observer.check_root_filters(event, **kwargs)
```

**The mechanism.** `AuthMiddleware` — which injects `actor` — was registered with
`dispatcher.message.middleware(gate)`, an **inner** middleware. `admin_router` carries
`OwnerOnly()` as a **root filter**. aiogram resolves a sub-router's root filters inside
`Router._propagate_event`, *before* that router's handlers and therefore before any
inner middleware has run. So `actor` was absent, aiogram could not fill a required
argument, the `TypeError` went to the dispatcher's error middleware, and the update was
logged "not handled".

Verified both ways against a real `Dispatcher` before changing anything:

| registration | `/help` | `/pause` |
|---|---|---|
| `middleware()` — inner, as shipped | handled | `TypeError`, update dropped |
| `outer_middleware()` | handled | handled, filter saw `OWNER` |

The fix is two words. `sentinel/bot/app.py` now uses `outer_middleware`, which also
matches what `auth.classify`'s docstring has claimed since M8.1: *"this runs before
routing — the point of the gate is that an unauthorized update never reaches a router
at all."* Inner middleware was never that.

**Blast radius: all nine owner commands plus the Approve/Reject buttons**, from the
10:56 deploy until the fix. `/status /settings /watchlist /pause /resume /users
/approve /reject /suspend`, and `AdminCallback` — which is how M8.1 expects an owner to
admit somebody, i.e. the milestone's headline feature. Member commands were unaffected,
which is exactly why `/start` and `/help` worked and nothing looked wrong.

### Why 1114 tests did not catch it

`tests/bot/` drives handlers and the gate **directly**, through `tests/bot_double.py`.
That is the right shape for behaviour — a real `aiogram.Message` cannot be constructed
without a bound `Bot` to answer through — but the defect is not in a handler, it is in
propagation, and calling a handler directly cannot observe propagation.

The second reason is worse and is the one worth carrying forward. M8.1 shipped 37 auth
tests and a 28-cell truth table, and **every one of them asserts that non-owners are
refused**. That property is true of a working system and equally true of a dead one.
`OwnerOnly` is *supposed* to produce silence for a non-owner, so a completely dead admin
router was indistinguishable from working access control — by test and by observation.

`tests/bot/test_dispatcher_wiring.py` (25 tests) closes both:

- feeds real `Update` objects to a real `Dispatcher`, and distinguishes "a handler ran
  and returned `None`" from aiogram's `UNHANDLED` sentinel — asserting against `None`
  would pass on a dead router;
- asserts **positive reachability for every owner command**, parametrised over
  `menu.OWNER_COMMANDS`, plus `/pause` actually setting the pause and the admin buttons
  reaching their handler;
- a **meta-test** that the exercised set equals the advertised set, so a tenth owner
  command cannot be added without a reachability test (M7 §3's pattern);
- keeps the negatives: a member still gets silence.
- asserts the invariant structurally too — the gate is on `outer_middleware` and *not*
  on `middleware`, for both observers — so a refactor fails here with the reason rather
  than in production as silence.

Proven able to fail: reverting the two words fails 18 of the 25.

**The general lesson, and it is M6.2's again one layer in.** `check-image` exists
because everything ran from a source checkout, so nobody noticed the image was
unbuildable. `check-ops` exists because `ops/*.sh` only run on the server. This is the
same shape: the dispatcher only runs in production, so the wiring was never exercised.
When a component's failure mode is *silence*, the absence of a complaint is not
evidence — only a positive test is.

## 1a. Silence as a success state — the class, and where else it lives

The `/pause` bug is worth less than the shape it belongs to, so the shape is stated
here rather than left implicit in one fix.

**The pattern.** A component whose *intended* success state produces no observable
output. When that is true, a working system and a broken one emit identical
observations — no reply, no log line, no row — and no amount of negative testing can
tell them apart, because the negative test's expected result is also the broken
system's result. M8.1 had 37 auth tests and a 28-cell truth table over exactly this
surface, all green, against a router that had never once run.

It is not a rare shape here. It is a *design principle* in this codebase, and
deliberately so — `specs/TELEGRAM_UX.md` §1's "all other users get silence" exists so a
stranger cannot learn that a private bot about somebody's capital is alive, and
`OwnerOnly`'s silence exists so a member cannot learn that `/approve` is a command. The
principle is right. The consequence is that the codebase has an unusual number of
places where *nothing happening* is the correct outcome, and every one of them is a
place where breakage hides.

**The rule this implies, and it is now a standing one:**

> Anywhere silence is the intended success state, there must be a **positive
> reachability test** proving the path is alive — and it must run against the **real
> machinery**, not a double. A negative test on a silent path proves nothing, because
> a dead path passes it.

The second clause is the half that would still have missed this bug. A positive test
against `tests/bot_double.py` would have called the handler directly and passed happily,
because the defect was in aiogram's propagation order, not in any handler. This is
`check-image`'s lesson (journal/M6_REPORT.md §12) and `check-ops`' (M8 §9) in a third
place: **the part that only runs in production must be exercised the way production
runs it.**

### Audit — where else this pattern lives

Listed, not fixed, per the owner's instruction. "Positive test?" asks whether anything
asserts the path is *alive*; "against real machinery?" asks whether that assertion would
survive a defect outside the unit under test.

| # | Site | Silence means | Positive test? | Real machinery? |
|---|---|---|---|---|
| 1 | `OwnerOnly` on `admin_router` | non-owner, or dead router | **now yes** | **now yes** — `test_dispatcher_wiring.py` |
| 2 | `AuthMiddleware.TABLE` — 20 of 28 cells are `DROP` | unauthorized, or gate broken | yes, `test_auth.py` | **no** — middleware called directly. The `PASS` cells for `/help` and the owner set are now covered for real; the **member** commands (`/capital /risk /positions /stats`), the ACK callback and the decision buttons are not |
| 3 | `AdminAlerter.after_cycle()` — "never raises into the scheduler" | healthy system, or dead alerter | yes, `test_admin_alerts.py` | **no** — `FakeBot`. This is the worst of the list: it is the component you only discover is broken at the moment you needed it, and its healthy-state output is silence by design |
| 4 | `publish_menu` / `_attempt` — swallows every failure | menu published, or `setMyCommands` failing | yes, `test_menu.py` | **no** — `FakeBot`. Verified live once by hand today via `getMyCommands`; nothing automates it |
| 5 | `dry_run` publishes nothing | rehearsal working, or publisher broken | partial | **no** — and this is why "24h in dry run" cannot validate the Telegram path. M7 §12 recommends the dry run precisely as a shakedown, and it is blind to exactly one thing |
| 6 | `UserNotifier` idempotency (`no_capital_key`, `notice_at` throttle) | already told them, or notifier broken | yes | no — the second call's silence is correct *and* is what a broken notifier looks like |
| 7 | `TrackerLoop.tick()` per-signal isolation | quiet market, or every check failing | yes | no — `checked=N events=0` reads identically both ways. Today's logs show `checked=0 events=0`, which is true (no open signals) and would look the same if the query were broken |
| 8 | `SignalEventRepository.unposted(chat_id)` | nothing new to post, or subquery wrong | yes | no — M8.1 §4 hole 1 was the *loud* direction of this; the quiet direction (nothing posts, ever) is the one that hides |
| 9 | Quiet cycles / `NO_SETUP` | market is quiet, or screener returning nothing usable | improved | `cycles.skipped` (M8.2) makes the *declined* half data. `candidates=0` still reads the same whether the screener judged or failed |
| 10 | `BotRunner.stop()` swallowing shutdown errors | clean stop, or a wedged polling task | no | no — low stakes, listed for completeness |

Not instances, and worth saying why so the list is not read as longer than it is:
`ingestion`'s per-symbol skip writes an `ingestion_failures` row *and* logs, so it leaves
evidence; the decision and manage-row ownership checks answer a visible toast; the spend
guard now alerts on transition (M8 §4); and `tracker/machine.py` refuses a transition
**with a reason** rather than dropping it (M7 §3).

Rows 3 and 5 are the two I would close next, and for the same reason: both sit on the
path between this system and the human it exists to inform, and both are currently
proven only against a double.


## 2. What the diagnostic measured

Eight cycles, 09:01–10:46 UTC, all `dry_run`, all `OK`. **$6.11 total.**

| kind | model | calls | in (uncached) | cache read | out | $/call | total |
|---|---|---|---|---|---|---|---|
| SCREENER | `claude-sonnet-4-6` | 8 | 4,574 | 0 | 618 | $0.0230 | $0.184 |
| ANALYST OK | `claude-fable-5` | 18 | 13,535 | 2,088 | 2,603 | $0.2842 | $5.116 |
| ANALYST INVALID_JSON | `claude-fable-5` | 3 | 13,052 | 3,416 | 2,753 | $0.2716 | $0.815 |

**$0.764 per cycle → $73.4/day at 96 cycles.** M7 §9's screener estimate was exactly
right ($0.023 × 96 = $2.21/day). What missed was the analyst call count: budgeted ~24/day,
actual **2.63/cycle = 252/day, 10.5×**.

Where it went:

| fate | calls | $ | share |
|---|---|---|---|
| `WATCHLIST`, never reached the gate | 11 | $2.858 | 47% |
| `CANDIDATE` → gate → `NO_CAPITAL` | 7 | $2.258 | 37% |
| discarded, schema-invalid | 3 | $0.815 | 13% |
| screener | 8 | $0.184 | 3% |

And $73/day never actually happens: `daily_spend_limit_usd: 10` bites around cycle 14
(~12:15 UTC), so the real behaviour is **the whole day's analysis budget spent in the
first 3¼ hours, then 21 hours of screener-only**. That is the failure this milestone
fixes — not the headline number.

## 3. The interval (#7)

`scan_interval_minutes: 15 → 60`. The setup timeframe is 1h; a 15-minute cycle asked the
analyst about the same unclosed candle four times. A 4× reduction on its own, and the
only change here that needs no judgment.

`stale_cycle_multiplier: 2 → 1` follows necessarily: it is measured in *scan intervals*,
so widening the scan would have silently moved "the process died" detection from 30
minutes to two hours. One interval holds it near an hour, and `ops/healthcheck.sh` covers
process death from the host every 15 minutes regardless.

## 4. The re-analysis cooldown, and why skips are now a column (#2)

`SkipReason.RECENTLY_ANALYSED`: a symbol whose last deep analysis returned `WATCHLIST`
or `NO_SETUP` stays quiet for `schedule.reanalysis_cooldown_minutes` (60 — one
setup-timeframe candle).

Three decisions inside it:

1. **`CANDIDATE` is not suppressed.** Only non-candidate verdicts arm the cooldown. A
   candidate the *gate* then rejected means the analysis was right and a rail stopped it;
   those rails have their own reasons and their own recovery.
2. **It is not per user.** Every other guard asks "may this person receive a signal"; this
   one asks "did we already pay to have this symbol looked at". The analysis is shared, so
   the answer is too — per user, a second member's fresh slate would re-buy the verdict
   the owner was just given.
3. **It is checked before the daily cap.** A symbol held back as already-analysed was
   never going to become a signal, so charging it against `max_signals_per_day` would let
   the cheapest guard in the system spend the day's budget on symbols nobody was offered.

**Skips are now data.** `cycles.skipped` is a JSONB map of
`{symbol: {"reason": SkipReason, "detail": str}}` (migration `0008`), with a closed
vocabulary — `OPEN_SIGNAL`, `COOLDOWN`, `DAILY_CAP`, `NO_FUNDED_USER`, `PAUSED`,
`SPEND_LIMIT`, `RECENTLY_ANALYSED`. Through M8.1 the reason existed only as a log line,
which was enough for "why was this cycle quiet" — asked once, at the time — and useless
for M9's "what is the system declining to analyse, and why", asked over weeks in
aggregate. Container logs here are capped at 10 MB × 5 per service, so that window is
days. The free text ("on cooldown until …") could not be grouped; it survives as
`detail` beside a countable key:

```sql
SELECT e.value->>'reason' AS reason, count(*)
FROM cycles c, jsonb_each(c.skipped) e GROUP BY 1 ORDER BY 2 DESC;
```

The one query with real subtlety is `latest_non_candidates`, and it has a
real-Postgres test rather than a faked one: a symbol analysed twice in the window must
be judged on the **later** verdict, so `WATCHLIST` then `CANDIDATE` is *not* suppressed.
Backwards, it would mute a symbol at the exact moment it became interesting — the most
expensive possible failure for a rail whose purpose is saving money.

## 5. The margin budget is now a limit (#9) — and it makes the system quieter

**This reduces signal frequency wherever stops are tight. That is the intended
outcome, and it is stated here so M9 does not rediscover it as a mystery.**

§4 called `margin_budget_pct` a *"Target margin per trade"* and recomputed
`margin_eur_final` after clamping to `max_leverage`, which discarded it. Two of seven
live candidate plans sized to **105% and 114% of capital** in notional, needing €21.00
and €22.76 against a €20.00 budget, and were approved. Now rejected, with its own code
`MARGIN_BUDGET_EXCEEDED` — separate from `INSUFFICIENT_MARGIN` because "cannot fund this
at all" and "exceeds the share you allocated" have different fixes and M9 cannot
separate them if they share a code.

**A correction to my own diagnostic.** I reported this as a small-capital problem. It is
not: `margin / capital = risk_pct / (stop_fraction × leverage)`, so both sides scale with
capital and €200 behaves exactly like €10,000. With `max_leverage: 10` and a 10% budget
the rail fires precisely when `stop_fraction < risk_per_trade_pct`. **Tight stops trigger
it, not small accounts.** It had simply never fired before because every golden in the
suite uses the 81.20 stop — 1.78% away against 0.75% of risk, sizing to 42% of capital.

The proof of that is uncomfortable and worth stating plainly: **two of M4's own §8
goldens are exactly this shape**, and `test_long_single_entry`'s docstring has recorded
`margin 11402.50/10 = €1,140.25` against a €1,000 budget since M4. They no longer ship at
the default config. Rather than re-tune their prices — which would invalidate
hand-calculations that have been correct since M4 and make the numbers in their
docstrings lies — they keep their prices, widen the budget by the *narrowest* band that
preserves the 10× clamp (12%, via `conftest.margin_budget`), and each carries an explicit
assertion that the default config now rejects it.

Ordering is tested, not assumed: the budget is checked **after** the liquidation-buffer
reduction (which lowers leverage and therefore raises margin), and **after**
`INSUFFICIENT_MARGIN`, which otherwise becomes unreachable — a dead branch under this
module's 100%-coverage requirement. `sentinel/risk/` is still at 100% branch coverage
(599 statements, 150 branches, 0 missed).

## 6. `screener_v2` (#3)

Full rationale and the measured table are in `journal/PROMPT_LOG.md`. The short version:
"expect 0-3 per batch" was already in v1, and v1 delivered 2.25 — *inside* the band,
which is why the band was not enough. It has no floor, and nothing in v1 distinguished a
**state** ("in an uptrend", "approaching resistance") from a **change**.

v2 narrows to 0-2, names zero as normal and frequent, puts the measured downstream
rejection rate into the prompt (61% `WATCHLIST`), and adds the one-hour test: *could I
have written this same reason an hour ago?* If yes, `interesting=false`.

`screener_v1.md` is kept. `llm.screener_prompt_version` selects between them — a
one-line rollback, no deploy — and every `llm_calls` row stores the version that produced
it, so `/stats` compares them retroactively. `check-image` now asserts the **configured**
screener prompt is present in the wheel, not just `fable_v1`: a typo there would be a
`KeyError` at the first cycle after a deploy, which is the one path that costs money.

## 7. Truncate, don't discard (#4)

Your ruling, and the evidence settled it. The caps were *already* stated in the field
descriptions with M5's structured-outputs lesson applied and a test keeping description
and bound in sync. They still failed three times in two hours:

| | thesis (cap 600) | counter_thesis (cap 300) | outcome |
|---|---|---|---|
| SOLUSDT | 539 | **307** | discarded |
| LINKUSDT | 553 | **301** | discarded |
| LINKUSDT | **603** | 235 | discarded |
| 18 others | 476–598 | 210–285 | OK |

**One was a single character over.** The parsing calls sit at 594, 598, 603−3. That is a
model aiming at a stated cap and landing inside ±1%, not one ignoring an instruction — so
a sterner prompt would only move where the misses cluster. These fields are prose for a
card; nothing computes on them. An overrun now costs a truncation and an
`analyst.field_truncated` log line carrying the overrun length, so M9 can see if it
drifts. `confidence` still **rejects**: it is a number the gate compares against
`min_confidence`, so out-of-range is a broken answer, not a long one.

## 8. `analyst_reports.llm_call_id` (#8)

NULL on all 18 rows. `save()` has accepted the argument since M5 and the orchestrator
never passed it. Now it does — the **last** OK call, since `analyze` returns as soon as
one parses. The `(cycle_id, symbol)` fallback was wrong exactly where it mattered: a
schema-invalid response is retried, so a symbol can have two calls in one cycle and the
retry's cost would be attributed to the report the other call produced.

## 9. Verification, and what is left

```
1159 passed, 34 skipped          hermetic
1193 passed,  1 skipped          with SENTINEL_TEST_DATABASE_URL
100% branch coverage             sentinel/risk (599 stmts, 150 branches, 0 missed)
mypy --strict                    clean, 213 files
ops scripts: bash -n clean · wheel builds · image imports, prompts ship, config loads
alembic 0007 → 0008 → 0007 → 0008 clean; alembic check: no drift
```

**Deferred by your ruling:** #5 (prompt caching — only ~2.1k of 13.5k input tokens
currently caches) and #6 (`analyst_effort: high`, which is 46% of each call's cost).
Both are quality-affecting and should be measured after the volume changes land.

**Left to do:**

1. **Deploy.** The server is on `be86e38` with the app container **stopped** (§10), so
   there is no spend and no polling right now.
2. **Verify `/pause` live** — it is the reason this milestone exists in this shape, and
   only the server can prove it.
3. **Re-measure after 24h**: escalations per batch, the `WATCHLIST` share of what is
   escalated, `cycles.skipped` grouped by reason, and cost per cycle. §6 names the way
   `screener_v2` can fail — quieter without being sharper.

## 10. State of the server as this was written

`be86e38`, schema `0007`, app container **stopped at 11:21 UTC** to halt spend after
`/pause` turned out not to work. Today's spend froze at **$6.74**. `risk_state` is empty —
the system was never actually paused, which is itself confirmation of §1. Postgres is up;
backups and the host watchdog cron are untouched, so the watchdog will alert on the
stopped app until the deploy brings it back.

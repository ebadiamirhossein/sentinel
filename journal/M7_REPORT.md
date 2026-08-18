# M7 — Orchestrator, Tracker & Stats · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (976 tests with the DB suite enabled, 949 hermetic),
**100% branch coverage on `sentinel/risk/`**, migration `0006_tracker_and_stats` applied and
reversed, a live dry-run cycle and a live tracker tick both verified end to end, and
`docker build` + an in-image import now part of the milestone gate.

---

## 1. What was built

| Module | Contents |
|---|---|
| `core/orchestrator.py` | `CycleOrchestrator.run()` — ARCHITECTURE §3's six steps, plus the rails, the audit trail and the guards. `select_symbols()` is the pure dedup/cooldown/cap function. |
| `core/app.py` | The `scan` and `tracker` APScheduler jobs (`max_instances=1`, `coalesce=True`); `last_cycle_at` seeded from `cycles` at boot so `/health` is honest immediately after a deploy. |
| `core/wiring.py` | `market_adapter()` — the exchange alone, for a tracker that needs candles and nothing else. |
| `tracker/models.py` | `EventKind`, `MarketEvent`, `RungFill`, `LegExit`, `SignalTracking`, `TrackerEvent`, `Transition`. |
| `tracker/machine.py` | **`advance(tracking, event, management)`** — the state machine as one pure function of (status, event). |
| `tracker/detect.py` | `observe()` — 1m high/low for fills and stops, closed 1h for invalidation, chronological, conservative. |
| `tracker/prices.py` | `PriceFeed`, and `lookback()` — how far back a tick must look. |
| `tracker/loop.py` | `TrackerLoop.tick()` — the 60s tick, per-signal isolation, roll-ups, and §7's daily-loss rail. |
| `stats/` | `models.py` (three populations), `compute.py` (win rate, avg R, profit factor, max DD), `queries.py` (`build_report`, `setup_stats`). |
| `llm/spend.py` | `SpendTotals`, `evaluate_spend`, `spend_window` — the guard pulled forward from M8. |
| `bot/notifier.py` | `TrackerNotifier` — §4's threaded replies, mirroring the publisher's claim-commit-send-confirm. |
| `bot/readmodels.py` | Where the arithmetic a card needs actually happens, so `cards.py` stays literally arithmetic-free. |
| `bot/handlers/replies.py` | §2's manage row: 🔚 Closed manually and ✏️ Note, with no FSM. |
| `bot/cards.py` | `tracker_update_card`, `decision_ack_card`, `stats_card`; `status_card` and `positions_card` rewritten. |
| `risk/` | `DAILY_SIGNAL_CAP`, `PortfolioState.signals_today`, the three `rails.py` builders, `costs.realized_costs_eur`, `accounting.unrealized_*`. |
| `storage/` + `0006` | `cycles`, `signal_fills`, `signal_exits`, `signal_events`; tracker columns and `dry_run` on `signals`; `event_key` on `telegram_messages`. |
| `tools/` | `cycle.py`, `track.py`, `stats.py`, `spend.py` — the demo. |
| Tests | 269 new across `tests/tracker/`, `tests/stats/`, `tests/core/`, `tests/llm/test_spend.py`, `tests/bot/`. |

## 2. The ten decisions, and why

Every one was brought to you before code depended on it.

1. **TP hits are events, not statuses.** ARCHITECTURE §3 writes `FILLED → (TP1_HIT → …)`, but
   `SignalStatus` shipped at M6 without `TP*_HIT` and with `CLOSED`, and `signals.status` already
   holds those values. A signal stays `FILLED` while size is open and becomes `CLOSED` when the
   last unit closes; the hits live in `signal_exits` and `signal_events`, where they can be
   counted. This is what lets the machine be one function of (status, event) — and lets the
   truth table cover every cell rather than a partly-ordered history.
2. **The daily-loss window is the UTC calendar day.** It is the instant every stored row and log
   line is stamped against, so a pause reconciles with the audit trail by eye.
3. **A win is realized R > 0, over signals that filled.** A never-filled signal is not a trade;
   it is counted on its own line and kept out of the win rate, the average, the profit factor and
   the drawdown. PRD G3's "reached TP1 or breakeven" is reported *beside* the win rate, because
   they measure different things and a system tuned against one is not tuned against the other.
4. **Cooldown also arms after a stop-out**, and ARCHITECTURE §3.6's "unless direction flips with
   strong evidence" is **not implemented**. The spec never defines strong evidence, and an
   undefined threshold in a rail that governs money is a guess. Deferred until you set a number,
   ideally from M9's data.
5. **`telegram_messages.event_key`, `NOT NULL DEFAULT ''`.** The old key allowed one update per
   signal against a §4 thread of a dozen. The not-null part is load-bearing: Postgres treats two
   NULLs as *distinct* in a unique index, so a nullable column would have silently
   un-guaranteed the card's own idempotency.
6. **One decision acknowledgement, edited in place.** Your addition. A fresh reply per press
   would bury the card; leaving the first would leave a stale "marked Taken" under a signal you
   later skipped — the one thing that must never be wrong.
7. **The daily cap is a rail**, with its own `DAILY_SIGNAL_CAP` code rather than folded into
   `MAX_POSITIONS`: "the account is full" and "the system has said enough for one day" are
   different findings, and M9 cannot separate them if they share a code.
8. **`make check` gains `check-wheel` and `check-image`**, both failing loudly. See §6.
9. **The spend guard suspends deep analysis only.** The screener keeps triaging; the tracker
   keeps running unconditionally, because it manages open positions.
10. **`dry_run` persists and tracks, silently.** 24 hours of it produces a measured paper record
    rather than only an absence of crashes.

Two calls I made rather than asked, both approved, and both about the same bias:

- **Fills and stops are read from 1m high/low, not a polled mark price.** A 60-second poll misses
  the wick that filled the rung or hit the stop, and the error is not symmetric — it
  under-reports stop-outs, so the measured win rate comes out better than the market was.
- **When one candle covers both the stop and a target, the stop is assumed first.** Intrabar
  order is unknowable from OHLC; assuming the target won would turn losses into wins in the
  record the whole system exists to produce.

To which detection adds a third, in the same direction: an entry fills at its **own limit price**
and never at the better one a gap would have given, while a stop fills at the **worse** of its
price and the candle's open. A gap through a stop really does fill below it, and that loss is
real; a gap through a limit really does fill better, and crediting the plan for a windfall it did
not plan would flatter every gapped setup.

## 3. The transition table, and what its meta-tests are for

`docs/MILESTONES.md` M7 asks for "state-machine transition table tests". The table states all
**56 cells** of `SignalStatus × EventKind` explicitly, and two meta-tests keep it honest:

- `test_the_table_covers_every_state_and_every_event` asserts the table's keys equal the full
  cartesian product. Without it, adding a status would leave eight silent untested cells — the
  failure a hand-written list of "interesting cases" cannot catch, because nothing tells you
  what you forgot.
- `test_the_table_would_catch_a_wrong_transition` proves the checker can fail, both ways. M6 §3's
  rule: a guard that cannot fail is not a guard.

The cells that **refuse** carry most of the value, and each is a plausible misreading of the spec:

| Situation | What the machine does, and why |
|---|---|
| Invalidation after a rung filled | **Refused.** §4 words it "before entry → signal cancelled". Once a rung fills the position is real and the stop closes it; honouring both would exit the same size twice. |
| Expiry after a rung filled | **Refused.** §5's time stop is "EXPIRES if no entry fill within `entry_ttl`". A filled position does not stop existing because its ladder's clock ran out. |
| Anything at all on a terminal signal | **Refused with a reason**, not silently dropped. A tick re-run after a crash replays events that were already handled — a normal Tuesday, not an error. |
| A target before any entry | **Refused.** There is no position to take profit on. |

## 4. Partial-fill R accounting — the hand-calculated goldens

Written in the M4 style you asked for: the arithmetic in the docstring **before** the code was
asked for it. All nine matched on the first run.

| Case | Working | R |
|---|---|---|
| rung 1, stopped | `(81.20−83.10)×18.30 = −34.77` ÷ 86.9475 | **−0.40** |
| rungs 1+2, stopped | `3250.436−3315.628 = −65.192` ÷ 86.9475 | **−0.75** |
| full ladder, stopped | `5211.416−5298.343 = −86.927` ÷ 86.9475 | **−1.00** |
| rungs 1+2, TP1 | 40% of 40.03 → 16.01 @ 85.20 = `+37.966` | **+0.44** |
| full scale-out TP1→TP2→TP3 | `67.912 + 90.864 + 101.846 = 260.622` | **+3.00** |
| TP1 then breakeven stop | the BE leg contributes exactly 0 | **+0.78** |
| short mirror, rung 1 stopped | `(85.60−83.70)×18.30×−1 = −34.77` | **−0.40** |
| manual close @ 84.00 | `0.90×18.30 = 16.47` | **+0.19** |

Three of these are worth reading twice:

- **−0.40R is only true because the weights are shares of risk.** M4 had to rule on that; this is
  the milestone where the promise is finally kept to a real number rather than a test fixture.
- **The full scale-out banks 3.00R where holding to TP3 would have banked the plan's 4.22R
  gross.** That is §5's management template making a trade-off on purpose — and now a measured
  one, which is the whole point of tracking outcomes.
- **Breakeven is `avg_fill_price` (82.55), not §3's `avg_entry` (82.675).** A stop set at a price
  the owner never paid is not breakeven, and the BE leg contributing exactly zero is the proof.

## 5. Crash recovery is structural, not a routine

ARCHITECTURE §6 asks that the tracker "rebuilds state from DB". There is no recovery routine to
read, and that is the design: **the loop holds no state between ticks.** Every tick asks which
signals are open, rebuilds each one's history from `signal_fills` and `signal_exits`, and
re-derives what the market did from candles that are still there. A restart is simply the next
tick.

What makes that safe rather than merely stateless is two unique constraints — a fill on
`(signal_id, rung_index)`, an event on `(signal_id, event_key)` — plus keeping *recording* and
*posting* as separate idempotent steps. A crash anywhere in a tick therefore resolves forward:
the event is either not recorded (and re-derived next tick from the same candles) or recorded and
not yet posted (and posted next tick). There is no state in which an event is both lost and
believed sent.

## 6. Packaging in the gate — and what the experiment actually showed

You asked for `docker build` in `make check` after M6.2. It is there, and so is a `check-wheel`
step that builds the wheel in ~2s. **Neither ever skips silently**: a missing Docker fails with a
message that names journal/M6_REPORT.md §12.

Then I tried to prove the gate has teeth by reintroducing the exact M6.2 defect — the redundant
`force-include` over a directory containing `.gitkeep` — and **it no longer reproduces**. Current
hatchling deduplicates the path instead of refusing it. The wheel built, the image built, the
container ran. A gate validated against a defect that cannot happen any more is not validated.

So I went looking for the failure that *is* live, and it is a different one: `pip install` never
imports the package, so **a dependency someone forgot to declare** — present in `.venv`, absent
from the image — builds perfectly and dies at boot. That is M6.2's class exactly: source tree
green, image broken.

`check-image` therefore builds **and runs**:

```
docker run --rm --entrypoint python sentinel:check -c \
  "import sentinel.main; \
   from sentinel.analyst.prompts.loader import load_prompt; \
   assert load_prompt('fable_v1').strip(), 'prompt text missing from the wheel'; \
   from sentinel.core.config import load_config; load_config(); \
   print('image imports, prompts ship, config loads')"
```

Proven by inducing it. With `import hypothesis` (a dev-only dependency) added to `sentinel/main.py`:

```
.venv       imports fine in .venv          <- tests, ruff and mypy all pass
docker      build succeeds                 <- the old gate would have stopped here
run         ModuleNotFoundError: hypothesis
make: *** [check-image] Error 1
```

The prompt-loading assertion is deliberate too: it is the packaging guarantee M5's `force-include`
was *trying* to make, now checked directly instead of configured hopefully.

`make check-fast` is the old gate, for mid-edit runs. Full `make check` takes 39s.

## 7. The bug the first live run found

`python -m sentinel.tools.track --once` against the real market resolved signal #41 as
**INVALIDATED**. It should have filled and stopped out.

The cause: signal #41 was published by M6, hours before the tracker existed, so its
`last_checked_at` was NULL. The window fell back to the five-candle floor, the tracker saw only
the last five minutes, and it judged the signal on its aftermath — a 1h close far below the
invalidation level — rather than on what actually happened to it.

A signal's first tick must cover the signal's whole life. `lookback()` now takes
`since = last_checked_at or created_at`. And because invalidation is read from 1h closes while
fills come from 1m bars, in separate passes, the events are now **sorted chronologically** before
the machine sees them: they were discovered separately, they did not happen separately. `sorted`
is stable, so events sharing an instant keep detection's order — fills, then the stop, then
targets — which preserves the within-candle rule.

Both are regression-tested (`test_a_signal_older_than_the_tracker_is_replayed_from_its_creation`,
`test_events_come_back_in_the_order_they_happened`). Neither could have been caught by a mocked
test: the first needed a signal older than the tracker, the second needed two timeframes
disagreeing about which came first.

After the fix, the same tick reported fills on all three rungs, `LADDER_COMPLETE`, and a stop-out.

## 8. What the live runs demonstrated

**A full dry-run cycle**, `python -m sentinel.tools.cycle --once --dry-run`:

```
── cycle b1e6faf4-e970-400b-864a-4b1857e6ab27 (DRY RUN) ──
symbols       10 scanned of 10 (0 skipped at ingestion)
screener      2 candidate(s)
analyst       1 analysed
gate          0 approved
telegram      0 stored (nothing sent)
spend         ~$0.375566 (estimate)
  skipped     SOLUSDT: a signal is already open for this symbol (PRD F11)
```

Three things happened there that no test could have arranged. The **dedup guard fired on real
data** — SOLUSDT was dropped because M6's signal #41 was still open, saving a $0.32 analyst call
for a plan the gate would have rejected anyway. The **gate rejected ETHUSDT with
`NET_RR_TOO_LOW`**, which is M5.1's cost correction doing its job eight milestones later. And the
whole cycle published nothing while doing everything else, which is what `dry_run` is for.

**A tracker tick** then resolved signal #41: three fills, ladder complete, stopped, realized R and
costs written, and the **daily-loss rail auto-paused the system** for 24h with a Telegram-ready
notice. `/stats` reported it. That is PRD F11 and RISK_ENGINE §7 working end to end.

### One thing to know about that number

The tick recorded **−4.62R**, not −1.00R, and the arithmetic is correct: signal #41's plan is
M6's *fixture* (a SOLUSDT ladder at 82–83 with a stop at 81.20) and live SOLUSDT is **76.98**. A
limit ladder sitting six dollars above the market fills instantly, and a stop above the market
fills at the candle's open. §2 rule 2 makes that impossible for a real signal — it bounds both
zone edges within 3% of the last price at gate time — so this can only happen to a demo row.

**It is still in your database, marked ✅ Taken, and `/stats` counts it.** I have not deleted it:
deleting measured history without being asked is exactly what M6.1's `db_guard` episode was
about. I did clear the **pause** it raised, because that pause asserts you lost 3.46% today and
you did not. Recommend deleting signal #41 before M9 starts measuring.

## 9. Spend guard — sizing the default

`llm.daily_spend_limit_usd = 10`, `daily_spend_warn_usd = 7`. The arithmetic behind that, so it
is a choice rather than a round number: the screener is ~$0.023 per cycle, so **~$2.2/day** at 96
cycles, leaving ~$7.8 for deep analysis — about **24 analyst calls** at M5's measured ~$0.32.
That is roughly 5× PRD G3's ≤5 signals/day target. The live cycle above cost $0.376.

The hole a spend guard must not have: `llm/pricing.py` prices an **unknown model at 0** with a
warning, which is right for keeping the audit row and would let an unpriced model spend
invisibly. So unpriced calls are counted separately and every figure derived from them is
reported as a floor — `today: at least $4.00` — never as a total.

## 10. Deviations from spec

| # | Deviation | Why |
|---|---|---|
| 1 | TP hits are events, not `SignalStatus` members | Decision 1. ARCHITECTURE §3 corrected. |
| 2 | Fills and stops read candles, not a polled mark price | The poll under-reports stop-outs. ARCHITECTURE §3 corrected. |
| 3 | ARCHITECTURE §3.6's "direction flips with strong evidence" is not implemented | Undefined threshold in a money rail. Recorded as a dated deferral. |
| 4 | `telegram_messages` unique key gained a fourth column | §6's three-column key allowed one update per signal against §4's dozen. |
| 5 | The spend guard ships at M7, not M8 | Owner request; MILESTONES updated on both sides. |
| 6 | `dry_run` is new and in no spec | Owner request; recorded in MILESTONES M7. |

## 11. Verification

```bash
make check                 # 949 hermetic tests, ruff, mypy --strict, 100% risk coverage,
                           # wheel build, image build + in-image import.  39s
```

```bash
SENTINEL_TEST_DATABASE_URL=postgresql+asyncpg://sentinel:<pw>@localhost:5432/sentinel_test \
  .venv/bin/pytest
```

Verified on this machine (2026-08-18):

- `949 passed, 21 skipped` hermetically; **`976 passed, 1 skipped`** with the opt-in DB suite.
- `mypy --strict` clean over 189 files; ruff clean.
- Branch coverage on `sentinel/risk/`: **100%** (595 statements, 148 branches, 0 missed).
- `alembic upgrade head` → `0006_tracker_and_stats`; `downgrade` to `0005` and up again, both
  clean; `alembic check` reports no drift between the models and the migration.
- The widened constraint verified in Postgres:
  `uq_telegram_messages_signal_id UNIQUE, btree (signal_id, kind, chat_id, event_key)`, with
  `event_key | character varying(64) | not null | ''::character varying`.
- The suite still makes **no live Telegram and no live Anthropic calls**; M1's autouse
  `no_network` fixture fails any test that tries.
- Live: one dry-run cycle (§8), one tracker tick resolving a taken signal, `/stats`,
  `python -m sentinel.tools.spend`.

## 12. Notes for M8/M9

- **Delete signal #41 before M9** (§8). It is a fixture plan carrying a fictional −4.62R in the
  real-stats book.
- **`docs/DEPLOY.md` should verify the spend guard rather than build it** — MILESTONES M8 is
  updated, and `python -m sentinel.tools.spend` is the check.
- **Run 24h in `dry_run: true` before letting it post.** That is what the flag is for, and
  `/stats`' DRY RUN population is how you read the result.
- **The funding interval is still an estimate** in realized costs, as in estimated ones: the plan
  does not carry the exchange's published next settlement, so a realized holding window is
  divided by the configured 8h. M5.1's note stands.
- **`realized_costs_eur` charges funding on the whole filled notional for the whole window**,
  ignoring that a partial close reduces the size being funded. Deliberately conservative, and the
  place to improve if M9's data says funding matters more than it looks.
- **The tracker's lookback is capped at 1000 candles** (~16.7h on 1m). A process down longer than
  that logs `tracker.lookback_capped` and cannot see fills older than the window. If M8's
  deployment expects longer outages, the fix is to page the request rather than raise the cap.

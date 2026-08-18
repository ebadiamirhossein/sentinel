# M6 — Telegram Bot · Report

**Date:** 2026-08-18
**Status:** DONE — `make check` green (732 tests hermetic, 752 with the DB suite enabled),
**100% branch coverage on `sentinel/risk/`** including the new distance code, migration
`0005_signals_and_telegram` applied and reversed, `TELEGRAM_UX.md` §1 regenerated from real
engine output, live card delivered to Telegram (§10).

---

## 1. What was built

| Module | Contents |
|---|---|
| `bot/models.py` | `SignalRecord`, `SignalDecision`, `SignalStatus`, `MessageKind`, `MessageStatus`, `PostedMessage` — the M6 half of ARCHITECTURE §3 contract 5. |
| `bot/formatting.py` | HTML escaping, `local_and_utc`, timezone resolution. No arithmetic. |
| `bot/cards.py` | `signal_card`, `rejection_card`, `status_card`, `positions_card`, `settings_card`, `watchlist_card`. **The arithmetic-free module.** |
| `bot/keyboards.py` | Typed `CallbackData` for the three decisions and the resume confirmation. |
| `bot/allowlist.py` | Middleware on messages **and** callback queries; fails closed. |
| `bot/runtime.py` | Parsing/validation for `/capital`, `/risk`, `/watchlist`; `AccountState` and `db_overrides` assembly; exchange-backed symbol verification. |
| `bot/publisher.py` | `SignalPublisher` — the claim-commit-send-confirm sequence, and the seam M7's orchestrator calls. |
| `bot/handlers/` | `/capital /risk /status /positions /pause /resume /watchlist /settings` and the button callbacks. |
| `bot/app.py`, `bot/context.py`, `bot/views.py` | Dispatcher assembly, injected handler context, read models. |
| `storage/` + `0005` | `signals`, `telegram_messages`, `runtime_settings`, `config_changes` + three repositories. |
| `risk/` | The owner-directed extension — see §2. |
| `tools/signal.py` | The demo: `--doc` regenerates the spec, `--post` delivers a real card. |
| Tests | 133 new tests across `tests/bot/`, plus `tests/risk/test_distances.py`; `tests/bot_double.py` and `tests/risk_double.py`. |

## 2. The owner directive: missing numbers became engine outputs

The plan I brought you dropped three figures from the §1 card because they were not on
`TradePlan` — target distance in %, and each rung's distance from the current price. Your
ruling: **a number the card needs and the plan lacks is a gap in the risk engine, not a line
to delete from the card.** Reading `TP1: 85.20` at 3am without a percentage means doing
mental arithmetic against a moving price.

`schema_version` **2 → 3**, additive only (`gate_decisions.plan` is JSONB, so nothing was
migrated or backfilled — the same posture M5.1 took going 1 → 2):

| Field | Definition | Sign |
|---|---|---|
| `last_price` | the market price the plan was built against | — |
| `target_distances_pct[]` | `\|TP_i − avg_entry\| / avg_entry`, parallel to `targets` | **unsigned** |
| `entries[].distance_pct` | `(price_i − last_price) / last_price` | **signed** |

Three decisions inside that, none of them cosmetic:

1. **Targets measure from `avg_entry`, rungs from `last_price`.** Two different reference
   prices because they answer two different questions. A target distance shares its basis
   with `stop_distance_pct` and every RR figure, so all three reconcile by hand on one card.
   A rung distance answers "how far below the current price does my ladder sit", which is
   what the owner is asking while placing the orders.
2. **The rung sign is stored, not derived.** §2 rule 2 bounds *both* zone edges within 3% of
   the last price; it does not force the zone to one side of it. A long's ladder can straddle
   price, and then rung 1 is above it and rung 3 below. A renderer inferring the sign from
   `direction` would print both as negative.
   `test_a_rung_above_the_last_price_is_positive` is that case.
3. **Target distances are unsigned magnitudes.** A short's reward is a falling price, and a
   minus sign in front of it reads as a loss. The card supplies the `+`, exactly as
   `tools/size.py` has always supplied the `−` in front of the unsigned `stop_distance_pct`.

All three are computed on the **final** ladder, after any min-notional collapse — a collapse
moves `avg_entry`, and M4 decision 2's rule ("the gate that ships a plan is the gate that
checked it") applies to the displayed distances too.
`test_distances_are_recomputed_after_a_collapse_moves_the_average_entry` asserts it.

`last_price` ships alongside because a percentage whose reference price was discarded is not
auditable (PRD G5) — `test_every_distance_re_derives_from_the_prices_on_the_plan` recomputes
every stored figure from the plan itself.

**Written test-first, per CLAUDE.md rule 4.** The goldens were hand-calculated in the test
docstring before the code existed:

```
rung 1  (83.10 - 83.40) / 83.40 = -0.30 / 83.40 = -0.00359712  -> -0.3597%
TP1     (85.20 - 82.675) / 82.675 = 2.525 / 82.675 = 0.03054128 -> +3.0541%
```

All six matched the engine — unlike M4, where two of my hand calculations were wrong and the
engine caught them. `make coverage-risk` still reports 100% branch coverage (535 statements,
130 branches, 0 missed) with `distance_pct` included.

## 3. The card, and what "renders, never computes" cost

`TELEGRAM_UX.md` §1 is now generated from real engine output and carries a reproduction
command; the ILLUSTRATIVE banner is gone. `test_the_spec_example_card_matches_what_the_tool
_renders` fails if the two ever drift, which is precisely how the old example rotted into not
reconciling with itself.

Four figures from the hand-written original are still absent, and each is a deliberate refusal
rather than an oversight:

| Dropped | Why |
|---|---|
| per-target `close 40%, stop→BE` | `management_plan` is one string covering all three targets. Splitting it per target means parsing prose. |
| per-rung `€40 margin share` | That is `notional ÷ leverage` — arithmetic with no stored counterpart. |
| `Expires: 12h` | Relative time is a subtraction, and it is wrong the moment the message is read. The card shows the absolute `expires_at`. |
| `fees €3.16` | `entry_fee_eur + stop_exit_fee_eur`, an addition `tools/size.py` performs. The card shows `round_trip_cost_eur` and both rates, all stored. **`PlanCosts` was not touched.** |

**The no-arithmetic test has two layers**, because either alone leaves a hole:

- **Layer 1** parses `cards.py` and `formatting.py` and fails on any `BinOp`, `AugAssign` or
  unary minus with an arithmetic operator. This catches a renderer that computes a *correct*
  number the plan happens not to carry — the failure that would look right on the card and be
  untested everywhere else. `abs()` and `astimezone()` survive on purpose: one removes a sign
  for display, the other re-labels an instant. Neither invents a magnitude.
- **Layer 2** extracts every numeric token from a rendered card and requires each to appear in
  the serialized plan. Layer 1 says nothing about a literal — `Leverage 3x` typed into an
  f-string passes an AST scan and is a lie.

Both layers carry a proof-of-teeth test, because a guard that cannot fail is not a guard.
Building layer 2 surfaced two things worth recording: quoted analyst prose ("4h uptrend",
"Fear & Greed at 74") had to be excluded by matching the stored text — so a *changed* quotation
still resurfaces as an unexplained number — and `schema_version` had to be excluded from the
known-values set, or a rendered "3" anywhere on the card would have been excused for free.

## 4. Idempotent posting, and the crash window

§6 says "restarts never double-post". The mechanism is claim-then-send, and the *ordering* is
the guarantee:

1. claim the signal (`plan_id` unique) and the message row
   (`(signal_id, kind, chat_id)` unique), then **commit**;
2. send;
3. record the returned message id and commit again.

Claiming after the send loses the id if the process dies in between. One transaction around
all three rolls the claim back on a crash and re-posts on the next start.
`test_the_claim_is_committed_before_anything_is_sent` asserts the ordering directly rather
than trusting the prose.

**A process that dies between the send and the confirmation leaves a `PENDING` claim, and it is
never retried.** A duplicated signal card is worse than a missing one the owner can ask for
again — the duplicate reads as a second setup. Every stuck claim is logged and counted in
`/status`, so the gap is visible rather than silent. Recorded as a dated ruling in §6.

This is one of two places where repositories commit outside the caller's unit of work; the
docstring says why. The other is the publisher itself.

## 5. Decisions

1. **The album and the card are two messages.** A Telegram media group cannot carry an inline
   keyboard, and its caption caps at 1024 characters against a card of ~1,580. So "charts
   attached as an album above the card" has to be an album plus a card that replies to it.
   `test_the_card_fits_a_telegram_message_but_not_a_caption` asserts both bounds rather than
   leaving the reasoning in a comment.
2. **Timestamps are local + UTC**: `2026-08-19 03:00 EEST (00:00 UTC)`. DATA_SOURCES §4 puts
   owner time on rendered output; §1's example printed UTC. Both, because local is what the
   owner acts on and UTC is what every log line and DB row carries — a card stays matchable to
   its audit row by eye. The tests run against Europe/Vilnius (UTC+3 in August) precisely so a
   UTC-only regression cannot hide behind a zero offset.
3. **The buttons stay live after a decision.** A mis-tap the owner cannot correct would
   permanently corrupt the real-vs-hypothetical split every M7 statistic rests on. Pressing the
   same button twice is a no-op; a different one records the change.
4. **The decision is persisted before the keyboard is touched**, and a failed edit never loses
   it. The database is the record; the keyboard is a view of it.
5. **`/resume` is asymmetric.** A manual pause lifts on the command. A daily-loss-limit pause
   requires the §3 confirmation button, because that pause exists precisely because the day has
   gone badly — which is when a reflexive tap costs the most.
6. **Silence is literal.** A non-allowlisted user gets zero outbound calls, not an
   "unauthorized" reply that would confirm they found a live private bot answering questions
   about someone's capital. An empty allowlist admits nobody: an unconfigured
   `TELEGRAM_ALLOWED_USER_IDS` is a misconfiguration, and failing open hands the bot to whoever
   finds it first.
7. **`/watchlist add` verifies the symbol against the exchange**, cache first. One keyless
   public call, against a typo that would otherwise produce a silently skipped symbol every
   cycle for as long as nobody reads the logs.
8. **A missing bot token degrades the app, it does not stop it.** `/health`, the scheduler and
   (from M7) the tracker are all useful without a chat attached; refusing to boot would turn a
   misconfigured `.env` into an outage.
9. **`runtime_settings` finally supplies `load_config`'s `db_overrides`.** ARCHITECTURE §2 has
   promised "DB > yaml > defaults" since M0 with no DB layer behind it. `AppConfig` is frozen,
   so an override rebuilds the config through the existing tested `_deep_merge` rather than
   poking attributes. `capital_eur` stays outside `AppConfig` entirely — it is an
   `AccountState` field, and `AppConfig` forbids unknown keys.
10. **`capital_eur` unset stays `None`.** Substituting a default would size positions against a
    number the owner never chose; `NO_CAPITAL` is the correct, visible behaviour, and `/status`
    names the code.
11. **Repository seams on the publisher and the handler context.** Idempotency and range
    validation are the headline requirements of this milestone; testing them only when a
    developer has `SENTINEL_TEST_DATABASE_URL` set would leave them effectively untested. The
    fakes subclass the real repositories, so a drifting signature fails `mypy --strict`, and
    `tests/bot/test_persistence.py` re-runs the same scenarios against real Postgres.
12. **`sentinel/bot/__init__.py` re-exports `models` only** — see §7.

## 6. Deviations from spec

| # | Deviation | Why |
|---|---|---|
| 1 | §2's `[🔚 Closed manually] [✏️ Note]` row is not built | It appears "after entry fills", and a fill is detected by the tracker (M7). A button that can never appear, or one that appears on a signal the system cannot tell is filled, are both worse than the gap. Recorded as a dated deferral in §2. |
| 2 | `/positions` shows the plan as issued, not live uPnL | Needs a mark price and `risk/accounting.py`'s ladder-aware R, which the tracker runs from M7. Computing it in the bot would duplicate that math and break §1. The card says so in words rather than showing zeros. |
| 3 | `/status` omits cycle timing and open-risk usage | No orchestrator until M7. It reports what *is* measured and names what is not. |
| 4 | `/settings` is a curated list, not all of `AppConfig` | Thousands of characters, mostly endpoint URLs, against a 4096-character message limit. |
| 5 | `/stats`, `/pulse`, `/analyze` not registered | M7 and P1. An unregistered command is silent rather than answered with a promise. |

All five are recorded as dated notes in `TELEGRAM_UX.md` itself, alongside the timezone and
crash-window rulings.

## 7. Found during verification

**A real circular import, caught by Alembic and not by the test suite.**
`storage/repositories.py` imports `sentinel.bot.models` to translate `SignalRecord` into rows —
the same shape as its existing imports from `risk.models` and `llm.models`. But
`sentinel/bot/__init__.py` re-exported `SignalPublisher`, which imports
`storage.repositories`. Importing `sentinel.bot.models` therefore ran `bot/__init__`, which
pulled in the publisher, which asked for a partially initialized `storage.repositories`.

It never failed in tests, because something always imported `bot.models` first. It failed
immediately under `alembic upgrade head`, which imports storage first:

```
ImportError: cannot import name 'SignalRepository' from partially initialized module
'sentinel.storage.repositories' (most likely due to a circular import)
```

Fixed by making `bot/__init__.py` re-export `models` only — a leaf that depends on nothing but
`risk.models` — which restores CLAUDE.md's direction (`bot → storage`, never back) and matches
what `storage/__init__.py` already does. Worth noting for M7: the tracker will want the same
discipline.

**Two smaller ones.** `Decimal("nan")` parses cleanly, so `/capital nan` was rejected with
"must be greater than zero" — true but misleading, and a NaN reaching `AccountState` would
poison every downstream calculation; it is now rejected as not-a-number. And
`record_decision` originally reported "already decided" for every press, because it read the
row's state *after* mutating it.

## 8. Demo

```bash
python -m sentinel.tools.signal --fixture examples/sol_long.json          # render
python -m sentinel.tools.signal --fixture examples/sol_long_thin.json     # rejection card
python -m sentinel.tools.signal --fixture examples/sol_long.json --doc    # the spec block
python -m sentinel.tools.signal --fixture examples/sol_long.json --post   # to Telegram
```

The rejection path, unchanged from M5.1 but now rendered as a card:

```
🚫 SOLUSDT — REJECTED [NET_RR_TOO_LOW]
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

- `732 passed, 21 skipped` hermetically; `752 passed, 1 skipped` with the opt-in DB suite
  enabled.
  `mypy --strict` clean over 158 files; ruff clean.
- Branch coverage on `sentinel/risk/`: **100%** (535 statements, 130 branches, 0 missed).
- `alembic upgrade head` → `0005_signals_and_telegram`; `downgrade` back to `0004_llm_audit`
  and up again, both clean.
- The suite makes **no live Telegram calls**: no aiogram `Bot` or HTTP session is constructed
  anywhere in `tests/`, and M1's autouse `no_network` fixture fails any test that tries.
- `--doc` output is byte-identical to `TELEGRAM_UX.md` §1, asserted by a test.

## 10. Live delivery — the card on the phone

Delivered to `@zyndix_trader_bot` → chat 7222549221, `message_id=3`, with the three §2 buttons
attached: `['✅ Taken', '👀 Watching', '❌ Skip']`.

Below is the message **as Telegram returned it**, not as the renderer produced it — captured
from the `Message.text` in the API response, which is the text Telegram stored and renders on
the phone. It is therefore the authoritative record of what arrived: HTML tags applied as
formatting, `&amp;` resolved back to `&`.

```
🟢 LONG — SOLUSDT   [trend_pullback · intraday · conf 78]
fable_v1 · Signal #6 · 2026-08-18 15:00 EEST (12:00 UTC)

📊 Thesis
4h uptrend intact; 1h pullback into EMA50 and the prior breakout level 82.4 with volume drying up on the retrace. Funding neutral, OI rising with price. 15m shows demand absorption at the zone top.

⚠️ Against: Fear & Greed at 74 (greed) — crowded-long risk into resistance at 84.6.

🎯 Plan (capital €10000 · risk 0.75% = €75.00 · EURUSD 1.1593)
Entry ladder (limit orders) — last price 83.40:
  1) 83.10 (-0.3597%) — 40% of risk — 18.30 SOL (€1311.77)
  2) 82.60 (-0.9592%) — 35% of risk — 21.73 SOL (€1548.26)
  3) 82.10 (-1.5588%) — 25% of risk — 24.15 SOL (€1710.27)
  Weighted entry 82.675 · avg fill 82.55

🛑 Stop: 81.20 (-1.7841%)
❌ Invalidation: 81.40 — 1h close below 81.40
🥅 TP1: 85.20 (+3.0541%) — 1.59R net (1.71R gross)
🥅 TP2: 86.60 (+4.7475%) — 2.50R net (2.66R gross)
🥅 TP3: 88.90 (+7.5295%) — 3.99R net (4.22R gross)

💶 Notional €4570.30 (5298.3430 USDT) · Margin €914.06 · Leverage 5x (isolated)
🧾 Costs: round trip €3.34 = 4.4533% of the €75.00 risk budget
    fees maker 0.02% in · taker 0.05% out
    funding ~€0.18 est · 2 settlement(s) @ 8h
✅ Liq. buffer OK (liq ≈ 20.0000% vs stop 1.7841%)
⚖️ Actual risk €74.98 (planned €75.00)
📋 TP1: close 40%, move stop to breakeven. TP2: close 35%. TP3: close remainder or trail by 1xATR.
⏳ Expires if unfilled: 2026-08-19 03:00 EEST (2026-08-19 00:00 UTC)

Research tool — not financial advice. Past stats ≠ future results.
```

It matches `TELEGRAM_UX.md` §1 line for line, which is the point — §1 is generated from the
same renderer and a test enforces the match.

**Idempotency, demonstrated rather than asserted.** The card was posted once (signal #6); three
further runs of the same command sent nothing:

```
### RUN 1                nothing sent — plan already published (specs/TELEGRAM_UX.md §6 working)
### RUN 2                nothing sent — plan already published (specs/TELEGRAM_UX.md §6 working)
### RUN 3 (as a restart) nothing sent — plan already published (specs/TELEGRAM_UX.md §6 working)
```

```
 number | symbol  |    status          kind |  chat_id   | message_id | status
--------+---------+---------------    ------+------------+------------+--------
      6 | SOLUSDT | PENDING_ENTRY      card | 7222549221 |          3 | SENT
```

### Two defects the live run exposed, both in the demo tool

Neither was in the bot; both would have made this report claim something untrue.

1. **The tool called a failed send "already delivered".** `post()` printed
   `nothing sent — {reason or 'already delivered'} (this is §6 working)` whenever nothing was
   published — and a Telegram error produces exactly that state with an empty reason. So the
   earlier `chat not found` failures printed a cheerful message about idempotency working. The
   two cases are now distinguished: only a `plan_id` collision may claim §6, and a recorded-but-
   undelivered signal says so explicitly and **exits non-zero**.
2. **Running `--post` twice proved nothing.** `TradePlan.plan_id` is a fresh `uuid4` per
   evaluation — correct in production, where every cycle's plan genuinely is a new plan, but it
   meant the second run posted a *second* card rather than colliding. The demo would have looked
   like idempotency and tested none of it. `--post` now derives a stable `plan_id` from the
   fixture's bytes (`demo_plan_id`, demo-only, called from nowhere in the pipeline), so the
   second run hits the real unique constraint. That is what the three runs above exercise.

The first defect is the more interesting one: the failure path had been verified against real
Postgres and behaved correctly — `status='FAILED'`, the reason stored, no crash — while the CLI
narrated it as a success. Correct behaviour reported wrongly is still a defect, and only a live
run with a real failure surfaced it.

### Still unverified

The button **callback** round-trip through Telegram needs the polling app running
(`make run`, or `docker compose up`), which is M7's deployment step. The buttons render and
their handlers are covered by `tests/bot/test_callbacks.py` (11 tests: persistence, the
double-press no-op, mis-tap correction, a failed keyboard edit not losing the decision), but
nobody has yet pressed one against a live dispatcher. Worth doing as the first act of M7, when
the orchestrator makes the app long-running anyway.

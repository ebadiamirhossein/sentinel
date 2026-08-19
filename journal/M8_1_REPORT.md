# M8.1 — Usability & multi-user with owner approval · Report

**Date:** 2026-08-19
**Status:** DONE — `make check` green (1114 hermetic tests, 1147 with the Postgres
suite), `mypy --strict` clean over 211 files, 100% branch coverage on
`sentinel/risk/` **without touching a line of it**, migration `0007` applied,
reversed and re-applied against the real local database, and the fan-out
demonstrated on a live dry-run cycle where the per-user dedup guard fired for real
(§9).

**Not deployed.** The server is mid dry-run and nothing here was shipped to it.
§11 is what deploying this will involve when the observation window closes.

---

## 1. What was built

| Area | Contents |
|---|---|
| `sentinel/bot/auth.py` | Replaces `allowlist.py`. The `(standing × update-kind)` truth table, `Actor`, `AuthMiddleware`, `OwnerOnly`. |
| `sentinel/bot/menu.py` | `setMyCommands` in three scopes; `publish_menu` never raises. |
| `sentinel/bot/notices.py` | `UserNotifier` — chat-level notices that belong to a user, idempotent on a `uuid5` of their own key. |
| `sentinel/bot/outbound.py` | `SupportsBot` = messages + menus, composed rather than widened. |
| `sentinel/bot/handlers/membership.py` | `/start`, `/help`, `/leave`, the acknowledgement button. |
| `sentinel/bot/handlers/admin.py` | `/users /approve /reject /suspend` + the Approve/Reject buttons, and the five owner commands moved out of `commands.py`. |
| `sentinel/bot/handlers/commands.py` | Now the member set only: `/capital /risk /positions /stats`, every one scoped to the caller. |
| `sentinel/bot/models.py` | `UserStatus`, `UserRole`, `UserAccount`, `ACK_VERSION`; `user_id` on `SignalRecord`. |
| `sentinel/bot/cards.py` | `help_card`, `acknowledgement_card`, `standing_card`, `welcome_card`, `ready_card`, `no_capital_card`, `loss_pause_card`, `left_card`, `leave_confirm_card`, `registration_request_card`, `users_card`. |
| `sentinel/storage/` + `0007` | `users` table, `UserRepository`, `user_id` on `signals` and `gate_decisions`, `user_id` required on every book-reading query. |
| `sentinel/core/orchestrator.py` | The fan-out: `_recipients`, `_allowed` over the union, `_gate_for` per user, `_say_no_capital`. |
| `sentinel/core/app.py` | Owner seed at boot, per-user publisher factory, owner-only alerter, the daily-loss notice finally delivered. |
| `sentinel/tracker/` | Per-user daily-loss rail; a per-tick candle cache in `PriceFeed`. |
| `sentinel/stats/queries.py` | `build_report(user_id=…)`; `setup_stats(owner_id=…)`. |
| Tests | `tests/bot/test_auth.py` (37), `test_registration.py` (21), `test_menu.py` (11), `test_help.py` (24), `tests/core/test_multi_user.py` (17), `tests/tracker/test_multi_user.py` (10), `tests/stats/test_per_user.py` (7), plus 6 Postgres round-trips. |
| Docs | Dated corrections in PRD §4, ARCHITECTURE §3, TELEGRAM_UX §1/§3 + a new §7, RISK_ENGINE §1/§7, PROMPTS §3; MILESTONES M8.1; DEPLOY §5 and a new §13a; README; `.env.example`. |

## 2. The design constraint, and what it bought

**One analysis per cycle, shared. Sizing, rails, decisions and stats per user.**

Steps 1–4 of ARCHITECTURE §3 — snapshot, features, screener, charts, deep analyst,
`analyst_reports` row — run **exactly once per symbol per cycle** however many
people are approved. Only step 5 fans out. The live cycle in §9 is the proof: two
candidate symbols, two approved users, **two** analyst calls.

The consequence worth stating is what it did *not* cost: **`sentinel/risk/` is
unchanged.** Not one line. `RiskEngine.evaluate` was already a pure function of
`(AccountState, PortfolioState)` and every function in `rails.py` was already pure
over already-selected iterables, so multi-user turned out to be a question of what
gets handed to them. The 100% branch-coverage requirement was never at risk, and no
risk default moved.

## 3. Why `signals.user_id` rather than a decisions table

The fork you settled before any code existed. The alternative — one `signals` row
per analysis plus a `(signal_id, user_id)` table for decisions — collapses on
contact with what per-user actually means: each user needs their own `plan`, their
own `filled_qty`, their own `status`, their own `realized_r`. The join table grows
into a second signals table with extra steps, and the tracker, the R accounting and
the state machine all have to be rewritten against it.

With `user_id` on the row, every per-user query downstream is a filter on a column,
and **the tracker, `tracker/machine.py`, `risk/accounting.py` and the whole M7 state
machine needed no changes at all.**

## 4. The four holes this opened, and how each is closed

Multi-user is mostly about what stops being true. Four things silently stopped
being true the moment a second person existed, and each is now a test.

**1. "Unposted" stopped meaning "mine".** `SignalEventRepository.unposted(chat_id)`
meant "events with no `telegram_messages` row for this chat". For one owner that is
the same thing as "my events". With two users it is not — no member has a message
row for the *owner's* fills either, so **every member would have received
fill-by-fill commentary on every other member's positions.** The subquery now
restricts candidates to signals belonging to that user first.

**2. PRD F11's dedup rail had nowhere to run.** "Max 1 active signal per symbol" was
enforced only in the pre-analyst guard, which now runs on the *union* — so a symbol
another user is owed is analysed, and the gate has no dedup rail of its own to stop
the user who already holds it from getting a second signal. `select_symbols` is
therefore applied a second time **per user** inside the fan-out. §9 shows this
firing on live data.

**3. A decision could be written onto somebody else's signal.** A card forwarded to
another approved user carries its buttons, and the callback names a signal id. Both
the decision buttons and the manage row now check the signal's owner before writing.
This is the one write in the system that must never be wrong — it decides whose real
statistics an outcome joins.

**4. The learning loop would have counted each analysis N times.**
`stats.setup_stats` feeds the *shared* analyst prompt, and one shared analysis now
produces one signal row per user. Unscoped, the sample size multiplies by the number
of friends and the win rate becomes a weighted average of everybody's execution — a
number that changes when somebody new joins, which is not a fact about the market.
Owner-scoped (your ruling): exactly one row per analysis, and the only book you
control.

## 5. The two pauses, and why they are different things

Your decision 5/6, and it needed the split. The **daily-loss pause is per user**:
the limit is a percentage of somebody's capital and there is no longer one capital.
It lives on the `users` row, is measured against that user's own realized EUR and
their own capital, lapses after 24h on its own, and — new — is **announced to
them**. `specs/TELEGRAM_UX.md` §4 has listed that notice since M6 and the tracker has
raised the pause since M7; nothing ever sent it. That mattered less when the owner
had `/status`; it matters a lot for a member who has no `/status` and would simply
stop hearing from the system.

The operator's `/pause` stays **system-wide** in `risk_state`'s single row.
Members have no `/pause` (your scope ruling), so making this one per-user too would
leave the person running the system without a stop button for it.
`PortfolioState.pause` composes them — the system pause wins when active, because it
is the wider statement.

## 6. `/users`, and the boundary you asked to be written down

> The owner needs to operate the system. The owner does not need to watch their
> friends trade, and a card that did both would make the second happen by accident
> every time the first was needed.

`/users` shows: standing, join date, **whether a capital is set (yes or no)**, and
whether a loss pause is currently holding them. It shows **no amount, no risk %, no
P&L, no win rate, and not one decision.**

The boundary is enforced as a **type**, not as a renderer's restraint.
`bot/views.UserView` has no field that could carry an amount or an outcome, and
`readmodels.user_view` is where `capital_eur` becomes a bool. A future card cannot
widen this without adding a field, which is a visible change with a test against it
(`test_users_never_shows_an_amount_a_pnl_or_a_decision` asserts both the rendered
card and the absent fields). The card itself ends with the sentence *"Capital
amounts, decisions and P&L are each user's own and are not shown here"*, so nobody
reading it wonders whether the data was merely omitted this time.

## 7. `/leave`, and the one account that cannot use it

A member can remove themselves in two taps and needs nobody's permission. Standing
becomes `LEFT`, the `/` menu is cleared, delivery stops immediately. **Their history
is kept, not deleted** — deleting measured history is never automatic — and they
appear as `LEFT` in `/users`, so re-approving them later is one command.

`LEFT` is a distinct status rather than a reuse of `REJECTED` because they are
completely different facts about a person, and the message each gets says which.

The owner is refused: an owner who left would leave a system with users and nobody
able to approve, suspend or operate anything, and no way back in from inside
Telegram. `/leave` is also dropped from the owner's `/` menu — a command that always
says no should not be offered.

## 8. Decisions

1. **The front door opens exactly one crack.** Only `/start` from an unknown id is
   answered. Everything else from anybody not approved gets *nothing* — no refusal,
   no toast. `tests/bot/test_auth.py` states the rule as a 28-cell truth table with a
   meta-test asserting the keys are the full cartesian product: a missing cell would
   be a `KeyError` inside the gate, which aiogram swallows, i.e. an update dropped
   for a reason nobody chose.
2. **One request per id, guaranteed by a primary key.** `ON CONFLICT DO NOTHING`; only
   an insert that created a row notifies you. Not a counter — there is nothing to
   lose in a restart and nothing to race.
3. **A rejected id cannot make the bot hold a conversation.** The standing notice is
   throttled to one per hour per id via `users.notice_at`.
4. **The acknowledgement records its wording's version**, not just a timestamp. A
   disclaimer somebody agreed to only means something if the words they saw are
   identifiable; bumping `ACK_VERSION` asks everybody again.
5. **Owner-only means silence, not a refusal.** `OwnerOnly` on its own router: a
   filter that does not match means no handler runs. A member typing `/approve`
   learns nothing — not that it exists, not that they lack the role.
6. **The `/` menu is an access-control surface**, so it is scoped: `/start` + `/help`
   for every private chat, the member set per approved chat, both halves for yours.
   It never raises — a failed `setMyCommands` must not stop the bot polling.
7. **`TELEGRAM_OWNER_USER_ID` is new**, falling back to a *sole* entry in
   `TELEGRAM_ALLOWED_USER_IDS`. A multi-id allowlist predates roles and says nothing
   about who owns the system, so the fallback declines rather than guesses.
8. **Migration 0007 fails loudly** rather than guessing an owner (§10).
9. **PRD F10's audit followed the value.** `capital_eur` and `risk_per_trade_pct`
   left `runtime_settings`, where `set()` wrote the `config_changes` row. `/capital`
   and `/risk` still append one, under a per-user key `user.<id>.capital_eur` —
   losing the audit for the only two settings that decide a position size would have
   been the wrong half of the table to stop auditing.
10. **A per-tick candle cache.** Several users' signals on one symbol are the same
    question asked N times a minute against a rate-limited public endpoint. Keyed on
    `(symbol, timeframe, limit)` and cleared at the top of every tick — a cache that
    survived a tick would be M7's polled-mark-price bug reintroduced.
11. **Charts are uploaded per recipient** rather than re-sent by Telegram `file_id`.
    At ≤5 signals/day and a handful of users the bandwidth is nothing, and per-chat
    claims in `telegram_messages` stay honest.
12. **`telegram.owner_timezone` stays global** — every card renders in it. A per-user
    timezone means a `/timezone` command nobody asked for. Named as a limitation
    rather than half-built.

## 9. The live dry-run cycle, and what it proved

Two approved users on the local database: you (€10,000 @ 0.75%) and a temporary demo
member (€2,500 @ 1.25%). One `python -m sentinel.tools.cycle --once --dry-run`:

```
── cycle 1b9b5392-4b09-4fc0-acf7-084f4d93f54e (DRY RUN) ──
symbols       10 scanned of 10 (0 skipped at ingestion)
screener      2 candidate(s)
analyst       2 analysed
gate          0 approved for 2 user(s)
spend         ~$0.733779 (estimate)
```

**Two candidates, two analyst calls, two `analyst_reports` rows** — not four. That is
the design constraint, measured rather than asserted.

The interesting line is the one nothing in the plan predicted:

```
cycle.not_approved   symbol=LINKUSDT user_id=900000001 reason=RR_TOO_LOW
cycle.user_skipped   symbol=LINKUSDT user_id=7222549221
                     reason='a signal is already open for this symbol (PRD F11)'
```

You hold signal #42 on LINKUSDT; the member did not. So the symbol was analysed
(union guard), the member was gated and rejected on its merits, and **you were
skipped by the per-user dedup check** — hole 2 in §4, firing on real data on its
first live run. Without that second application of `select_symbols` you would have
received a second open signal on a symbol you already hold.

`gate_decisions` recorded three rows for the cycle, one per (user, symbol) actually
evaluated, and none for the pair that was skipped before the gate — nothing was
evaluated, so there is no verdict to store.

**The demo member was then deleted**, along with the two `gate_decisions` rows it
produced, so M9's rejection statistics do not begin with a user who never existed.
Your own row for that cycle was left exactly where it is. Nothing else in your
database was touched by the demo: `signals` still holds one row (#42), and the
dry-run cycle approved nothing, so it created none.

## 10. Migration 0007, run against your real database

```
0006_tracker_and_stats -> 0007_users_and_multi_user
```

| Check | Result |
|---|---|
| OWNER row created | `7222549221` · APPROVED · OWNER · acknowledged `v1` |
| `runtime_settings` copied onto it | `capital_eur = 10000`, `risk_per_trade_pct = 0.75` |
| `runtime_settings` rows themselves | **untouched** — both keys still there |
| Existing signal #42 | `user_id = 7222549221`, nothing else changed |
| `uq_users_single_owner` | a second OWNER insert is refused by the database |
| downgrade → upgrade | clean both ways; signal #42 and both settings survive |
| `alembic check` | no drift between models and migration |

**It refused to run first, and that was the point working.** The migration read
`os.environ`, which does not carry `.env`, so it could not resolve an owner and
raised with the variable's name rather than backfilling every signal to a guess. It
now resolves through `Secrets` (the same file the app reads) while keeping the *rule*
frozen in the migration, so a later change to `Secrets.owner_user_id` cannot
retroactively alter what this migration did.

**A defect `alembic check` surfaced.** `users.telegram_user_id` came out as a
`BIGSERIAL` — an integer primary key is autoincrement by default — so an insert that
forgot the Telegram id would have quietly created user 1, 2, 3 instead of failing.
It is a natural key from Telegram and is never generated here.
`autoincrement=False`, in the model and the migration; verified that a bare insert
now raises a not-null violation.

## 11. Deviations from spec

| # | Deviation | Why |
|---|---|---|
| 1 | TELEGRAM_UX §1's "all other users get silence" now answers `/start` | A system somebody must be able to *join* cannot answer nothing. Narrowed, not dropped, and recorded as a dated correction; supersedes M6 decision 6. |
| 2 | `capital_eur` / `risk_per_trade_pct` left `runtime_settings` (RISK_ENGINE §1) | They are the two values that must not be shared. Dated correction; the engine is unchanged. |
| 3 | RISK_ENGINE §7's single pause became two | The loss limit is a percentage of somebody's capital. Dated correction; `/pause` is unchanged. |
| 4 | ARCHITECTURE §3 step 5 fans out; `signals` and `gate_decisions` gain `user_id` | The milestone's whole shape. Dated correction, and contract 5 named. |
| 5 | PROMPTS §3's history block is owner-scoped | Your ruling. Unscoped it would multiply the sample by the number of users. |
| 6 | PRD §4's "single persona" | Dated note. Every story in §4 is still yours and execution is still manual. |
| 7 | §4's daily-loss notice is *sent* for the first time | Same class of gap as M8 found in the spend guard: described since M6, raised since M7, never delivered. A member has no `/status` to discover it from. |
| 8 | `/status`, `/settings`, `/watchlist`, `/pause`, `/resume` moved to an owner-only router | Your scope ruling. Recorded in §3's M8.1 note. |

## 12. Verification

```bash
make check   # 1114 hermetic tests · ruff · mypy --strict (211 files)
             # · 100% risk branch coverage · check-ops · wheel · image + in-image import
```

```
1114 passed, 34 skipped                      hermetic
1147 passed, 1 skipped                       with SENTINEL_TEST_DATABASE_URL
Required test coverage of 100% reached       sentinel/risk (595 stmts, 148 branches, 0 missed)
ops scripts: bash -n clean
wheel builds cleanly
image imports, prompts ship, config loads
```

Verified on this machine (2026-08-19):

- `alembic upgrade head` → `0007`; `downgrade 0006` and up again, both clean;
  `alembic check` reports no drift.
- The opt-in Postgres suite ran against a scratch `sentinel_test` database, never
  the app's own — `tests/db_guard.py` refuses if the two resolve to the same place.
- One live dry-run cycle with two approved users (§9).
- The suite still makes **no live Telegram and no live Anthropic calls**; M1's
  autouse `no_network` fixture fails any test that tries.

## 13. What is left for you

1. **Set `TELEGRAM_OWNER_USER_ID`** in the server's `.env` before deploying this.
   Migration 0007 will refuse to run without it on a database with data to attribute
   — deliberately, and it names the variable in the error.
2. **The live Telegram round trip is unverified**, and cannot be verified from here:
   one bot token allows one long-polling client and the server holds it
   (journal/M8_REPORT.md §9a). What to walk through once deployed —
   `docs/DEPLOY.md` §13a has it as a runbook section:
   - type `/` and confirm the menu lists your commands, `/approve` included;
   - `/help` and read it as if you had not written it;
   - have somebody send `/start`, approve them, watch them acknowledge and set
     capital;
   - `/users` and confirm it shows their standing and nothing about their trading;
   - `/leave` from their side, and `/users` again.
3. **Deploy after the observation window**, not before. This changes the schema and
   the delivery path, and the point of a dry run is that nothing changes under it.
4. **Consider whether a member should have `/status`** at all once you have one.
   They currently cannot tell "quiet market" from "system down" — the notices cover
   *withheld* signals, not an outage. A spend-free `/status` is an hour's work if the
   first member asks for it.

## 14. Notes for M9

- **Statistics are per user from now on.** `python -m sentinel.tools.stats` reports
  your book; `--user <id>` reports somebody else's. There is no combined book, on
  purpose.
- **The union guard is the new place to look when a cycle analyses more than you
  expect.** `cycle.symbol_skipped` is now union-level; `cycle.user_skipped` is the
  per-user one, and the pair explains any cycle exactly.
- **`gate_decisions` is one row per (cycle, symbol, user).** M9's rejection-reason
  counts should be filtered by `user_id` or they will double-count once you have a
  member.
- **The acknowledgement version is a knob.** If the disclaimer's wording changes
  materially, bump `ACK_VERSION` and everybody is asked again — including you.
- **A member's timezone is yours.** Cards render in `telegram.owner_timezone` for
  everybody (decision 12). Worth revisiting only if a member is in a different one.

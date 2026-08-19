# M8.3 — Member watchlist requests

**Date:** 2026-08-19
**Status:** DONE locally — `make check` green (1185 hermetic tests, **1221** with the
Postgres suite), `mypy --strict` clean over 214 files, 100% branch coverage on
`sentinel/risk/` untouched, migration `0009` applied, reversed and re-applied against
a real database, `alembic check` reporting no drift. **Not deployed** — §7.

A member can now ask for a symbol without being able to spend on one.

---

## 1. What was built

| Area | Contents |
|---|---|
| `storage/models.py` + `0009` | `watchlist_requests`, with `uq_watchlist_requests_one_pending` |
| `storage/repositories.py` | `WatchlistRequestRepository` — `request`, `pending_for`, `pending`, `decide` |
| `bot/models.py` | `WatchlistRequestStatus`, `WatchlistRequest` |
| `bot/keyboards.py` | `WatchlistCallback`, `watchlist_request_keyboard` |
| `bot/cards.py` | `watchlist_request_card`, `watchlist_request_ack_card`, `watchlist_request_decided_card`, `watchlist_full_card` |
| `bot/handlers/commands.py` | `/request SOLUSDT` — the member half |
| `bot/handlers/admin.py` | `watchlist_request_button` behind `OwnerOnly`; the cap now binds `/watchlist add` |
| `bot/menu.py` | `/request` on the member menu, absent from the owner's |
| `core/config.py` + `config.yaml` | `watchlist_max_symbols: 15` |
| Tests | `test_watchlist_requests.py` (16), 4 new in `test_dispatcher_wiring.py`, 2 Postgres tests, menu updates |
| Docs | `TELEGRAM_UX.md` §3a (new), `MILESTONES.md` M8.3 |

## 2. The three rulings, and what each bought

**1. A declined symbol can be asked for again, with no cooldown.** The reason to
hold a symbol off the watchlist is a fact about the market, and markets change. This
is what makes the unique index **partial** rather than a plain unique on `symbol`:
`WHERE status = 'PENDING'` leaves decided rows in place — so the history is kept, and
asking again is still possible. A full index would have forced a choice between the
two. `/suspend` exists for somebody who abuses it.

**2. The cap binds `/watchlist add` too.** It is a spend control — every symbol is
screened every cycle and can buy a ~$0.28 analyst call — and a cap that applied only
to members would not be a cap. It is the owner's own config value to raise.

**3. The cap counts live symbols and is re-checked at approval.** Three requests can
be pending against two free slots, and each was legal when it was made; only the
moment of approval knows what the list actually holds.

Your adjustment to (3) is the part worth the most, and it changed the design rather
than the wording. A refusal at approval time leaves the request **`PENDING`**:

* it is **not a rejection**, because nobody decided against the symbol — the list ran
  out of room while it waited;
* **both people are told**, and told which of the two happened;
* the owner can `/watchlist remove` something and press the *same card* again.

Without it the member would have been declined for a symbol the owner was actively
trying to give them, and would have had to ask again. `WatchlistRequestStatus`'s
docstring records why `PENDING` is a state a request can *stay* in, so the next person
to read the enum does not tidy it away.

## 3. One pending per symbol is the database's job

`uq_watchlist_requests_one_pending` is a partial unique index, and `request()` is an
`ON CONFLICT DO NOTHING` against it that returns `None` when a row already exists.
No counter, no pre-check, nothing to race: two members asking for LINKUSDT in the same
second produce one row and one card.

The second asker is told the symbol is spoken for and **not told by whom**. Who else
uses this bot is not a member's business — the same boundary `/users` draws in M8.1 §6,
applied one surface over.

One detail that would have been a runtime error: `on_conflict_do_nothing` cannot name a
*partial* index with `index_elements` alone, so the predicate is repeated in
`index_where`. Postgres matches the conflict target against the index definition and
raises "no unique or exclusion constraint matching the ON CONFLICT specification"
without it — at runtime, on the first real request.

## 4. The bug the Postgres test caught, and why nothing else could

`sentinel/storage/repositories.py` used `text("status = 'PENDING'")` without importing
`text`. Every hermetic test passed. The whole 1185-test suite passed.

It could not have been caught there: `FakeWatchlistRequestRepository` **overrides
`request()` entirely**, so the real `insert(...).on_conflict_do_nothing(...)` never
ran. The fake models the *constraint*; only a real database runs the *statement*.

`NameError: name 'text' is not defined` — on the first `/request` in production, which
is the one path a member would have hit on day one.

This is journal/M8_2_REPORT.md §1a's rule showing up in a third place within a day, in
its "real machinery" clause rather than its "positive test" clause. The two Postgres
tests are there because the fake's dict is a *description* of the index; if the index
were missing from the migration, the hermetic suite would still be green and two
members asking for the same symbol would produce two cards.

## 5. Where this lands relative to the §1a audit

M8.3's owner half sits behind the same `OwnerOnly` root filter that silently dropped
every admin update on 2026-08-19, so it got the same treatment as the commands: a
positive reachability test against a real `Dispatcher`, plus the negative (a member's
tap on the button is silent).

The member half discharges **audit item 2's member commands**, as you called:

* `test_every_member_command_is_reachable_by_a_member` — all seven, through the real
  dispatcher;
* `test_the_reachability_list_covers_every_advertised_member_command` — the meta-test,
  now over `MEMBER_COMMANDS` as well as `OWNER_COMMANDS`.

**No command on either menu can now be advertised without something proving it reaches
a handler.** That is the half of audit item 2 that was about routing. What remains
open there is the `AuthMiddleware` truth table's *own* `DROP` cells — 20 of 28 — which
are still asserted by calling the middleware directly. Those are listed in §1a and are
untouched by this milestone.

Making the member commands run through the real dispatcher required `FakeSession` to
answer reads (`/stats` uses the real stats queries unmocked). It returns "no rows",
which is honest for an empty fake database and keeps those tests about routing.

## 6. Verification

```
1185 passed, 37 skipped          hermetic
1221 passed,  1 skipped          with SENTINEL_TEST_DATABASE_URL
100% branch coverage             sentinel/risk — untouched by this milestone
mypy --strict                    clean, 214 files
ops bash -n clean · wheel builds · image imports, prompts ship, config loads
alembic 0008 → 0009 → 0008 → 0009 clean; alembic check: no drift
```

## 7. Not deployed, and the measurement running underneath it

The server is on `db76c3a` (M8.2) with `dry_run: true` and the app running. Deploying
M8.3 restarts the app and applies `0009`; it changes no pipeline behaviour, but it does
interrupt the observation window you asked for.

**M8.2's settings are working**, measured on the same box:

| | 15-min cycle, `screener_v1` | 60-min cycle, `screener_v2` |
|---|---|---|
| $ per cycle | $0.764 | **$0.291** |
| interesting per batch | 2.25 | **1.33** |
| projected $/day | $73.4 | **$7.0** |

`RECENTLY_ANALYSED` has fired once, so the rail is live and not merely configured. Three
cycles is a small sample and the screener figure is the one to keep watching — §6 of
the M8.2 report names the way `screener_v2` can fail, and the WATCHLIST share of what
it does escalate is the number that tells quieter from sharper.

Deploying now is safe and adds nothing to the pipeline; deploying tomorrow keeps a
clean 24-hour window. Your call.

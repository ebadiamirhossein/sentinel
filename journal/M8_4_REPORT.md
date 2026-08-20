# M8.4 — `/pulse`, the pipeline transparency command

**Date:** 2026-08-20
**Status:** DONE — `make check` green (**1260** hermetic tests, **1306** with the
Postgres suite), `mypy --strict` clean over 216 files, 100% branch coverage on
`sentinel/risk/` **without touching a line of it**, and **no migration**: this
milestone adds no table, no column and no write of any kind.

Everybody can now see what the pipeline did. Nobody can see anybody else's book.

---

## 1. What was built

| Area | Contents |
|---|---|
| `sentinel/bot/pulse.py` | New. The two classifications, the wording maps, `gate_outcome`, `pulse_view`, `pulse_day_view` |
| `sentinel/bot/views.py` | `PulseView`, `PulseDayView`, `PulseSymbolView`, `PulseSkipView`, `PulseVerdictView`, `PulseGateView` |
| `sentinel/bot/cards.py` | `pulse_card`, `pulse_day_card`; a `/pulse` paragraph in `HELP_LINES` |
| `sentinel/bot/handlers/commands.py` | `/pulse [24h]` on `commands_router` — no owner filter, both roles reach it |
| `sentinel/bot/menu.py` | `/pulse` in `MEMBER_COMMANDS`; the owner inherits it through `commands_for` |
| `sentinel/bot/context.py` | `Repositories` gains `reports` and `gate_decisions` |
| `sentinel/storage/repositories.py` | Five read-only queries + the pure `screener_verdicts_of` |
| Tests | `tests/bot/test_pulse.py` (62), 7 in `test_dispatcher_wiring.py`, 8 Postgres round-trips, 1 in `test_menu.py`, doubles in `bot_double.py` |
| Docs | `TELEGRAM_UX.md` §3's `/pulse` row superseded + a new §3b; `MILESTONES.md` M8.4 |

## 2. Why this command exists, and why it is not `/status` for members

journal/M8_1_REPORT.md §13 item 4 asked whether a member should get `/status`, and
named the gap: they "cannot tell 'quiet market' from 'system down'". The answer turned
out to be no, and the reason is the useful part.

`/status` is the operator's instrument. Half of it is the caller's own book — capital,
open risk against the budget, positions against the cap, signals today — and the other
half is spend and pause state, which §7 already rules are owner-only channels because a
member has no lever to pull about either. A member's `/status` would be a different
command wearing the same name, and would report **their** rails, which tells them
nothing about whether the *pipeline* is alive.

`/pulse` is the other axis. It reports the run, not the reader:

> One analysis per cycle, shared (M8.1 §2). The reasoning behind it is therefore the
> one thing in this system that genuinely is identical for everybody, so it is shown
> identically to everybody.

That makes it the only command in the bot both roles run — and it makes the interesting
work not the rendering but the two places the underlying rows are *not* shared.

M8.2 also raised the stakes without meaning to. Sixty-minute cycles, `screener_v2`,
`RECENTLY_ANALYSED` and a hard margin budget were all correct and all made the system
quieter, and the server has been in `dry_run` on top of that. A silent phone is now the
expected state, and until today nothing anybody could type distinguished it from a dead
poller.

## 3. `RejectionReason`, split where the gate stops being about the market

`gate_decisions` holds one row per (cycle, symbol, **user**). Some codes are facts about
the analysis and some are facts about an account, and the split had to be defensible
rather than felt.

It is not a judgement call. It falls exactly at the `check_portfolio_rails` call in
`risk/engine.py`: everything above it is computed from `(report, market, config)` and is
provably identical for every user; everything below it reads `AccountState` and
`PortfolioState` and is arithmetic on somebody's money. Thirteen shared codes are
**named** on the card, with a plain sentence beside each. Eleven personal codes are
folded into *"cleared the shared checks — the rest is per-account"*.

**Two of the personal eleven look shared, and both are worth writing down:**

* **`PAUSED`.** The operator's system-wide `/pause` never reaches the gate — the
  orchestrator stops earlier, at `SkipReason.PAUSED`, so the pipeline does not buy a
  ~$0.28 rejection. Therefore a `PAUSED` row in `gate_decisions` can *only* be a user's
  own daily-loss pause (M8.1 §5), which is as private as a P&L figure: it says they lost
  money today. This is the entry most likely to be "tidied" into the shared set by
  somebody reading the enum rather than the call graph, so it has its own test.
* **`NET_RR_TOO_LOW`.** Cost-as-a-share-of-risk is scale-invariant, so this is *nearly*
  shared — but it is measured after ladder sizing and rung collapse, both of which
  depend on capital. Classified personal because the boundary has to be provable rather
  than nearly true, and the cost of being wrong is one-directional.

**An approval is shown**, as `✅ approved`. It names no user and carries no size: it says
a plan cleared every rail somebody had, which is a fact about the pipeline. Where one
user was approved and another rejected on a personal rail, the approval is what is
reported — reporting the rejection instead would be reporting an empty `/capital`.

**The partition is a meta-test.** `SHARED | PERSONAL == set(RejectionReason)`, disjoint.
A new code belonging to neither would fall through to the per-account bucket, which is
*safe* and therefore silent: a code that should have been named would simply never
appear and nothing would say so. That is the failure the meta-test exists to catch, and
it is the same shape as `auth.TABLE`'s and `tracker/machine.py`'s.

## 4. Skips: the `detail` column turned out to be worth nothing and cost something

`cycles.skipped` is already union-level — a symbol lands there only when *no* eligible
user could have received it — so no row names one person. Three of the seven reasons are
still derived from somebody's book, and those are phrased about the symbol rather than
about whoever holds it: `OPEN_SIGNAL` renders as "an open signal is already running on
it".

The plan was to keep the stored `detail` for the four pipeline reasons and drop it for
those three. Rendering it once settled the question differently: **every one of the seven
details either restates its own key or appends a raw ISO timestamp.**

```
BTCUSDT — analysed recently and not a candidate then (analysed since
          2026-08-20T10:00:00+00:00 and not a candidate then)
```

So the detail is never rendered. It costs nothing — the wording map is the whole story —
and it removes the case where a cooldown expiry, which is a clock reading off somebody's
last trade, reaches a card. It stays in the column, where M9 groups on the key and reads
the detail when one row needs explaining.

`SKIP_WORDING` is **keyed by string rather than by the enum**, because `SkipReason` lives
in `core/orchestrator.py` and `bot → core` is backwards (CLAUDE.md's dependency
direction; the orchestrator wires the stages and the bot is one of them). The meta-test
lives in `tests/bot/`, where a cross-import costs nothing, and asserts the keys are
exactly the enum's members — so an eighth reason fails a test instead of rendering as a
blank line on a member's phone.

## 5. The spend line is a type, not a rule the renderer remembers

The owner sees the cycle's own estimated cost and the day's total against the limit; a
member sees nothing. That is §7's owner-only channel — it is the owner's bill, and a
member has no lever to pull about it.

It is enforced the way M8.1 §6 enforced `/users`: `PulseView.spend` and
`PulseView.spend_usd` are `None` for a member, set from `actor.is_owner` in the handler,
so **the member's view object holds no figure to leak**. The figure is not even fetched
for them. `cards.py` contains no role check, and a future edit could not print a cost
without a field appearing on the view first — which is a visible change with a test
against it (`test_the_member_view_has_no_money_field_to_leak` asserts both the `None`s
and that no other field name looks like money).

One test states the design claim outright rather than leaving it implicit:
`test_the_same_cycle_renders_identically_for_a_member_and_the_owner` diffs the two cards
and asserts they differ by exactly one line, which contains a `$`.

## 6. Reading verdicts out of a table that does not exist

The screener has no table. Its verdicts live only inside `llm_calls.response`, under the
`parsed` key `llm/client._response_audit` fills when the model's text was JSON. Adding a
`screener_verdicts` table would have been the obvious move and was the wrong one: it
changes what the pipeline *writes*, and this milestone is read-only by design — nothing
about a transparency command should be able to break a cycle.

Three consequences, each handled in words rather than by guessing:

1. **Only `status='OK'` calls count.** An `INVALID_JSON` row is kept for M9 and is not an
   answer about a market; counting one would put a symbol on the pulse that
   `screener.reconcile` had already defaulted to not-interesting — the card would name a
   symbol the pipeline never escalated.
2. **The stored form is pre-reconciliation.** `reconcile` drops hallucinated symbols and
   defaults omitted ones, and only its *input* was ever persisted. Re-implementing it
   here against different inputs would be a second copy of the function; instead a symbol
   that was escalated and has no verdict simply has no analyst line, and the card does not
   invent one. The same happens when `AnalystUnavailable` fires, which writes no report
   row at all.
3. **A row that fails validation is dropped and logged**, not guessed at. A future
   `screener_v3` could widen the model, and `ScreenerVerdict` forbids extras, so old rows
   and new rows can each fail against the other's shape. The remaining verdicts still come
   through, so one bad row does not blank the section.

An OK call whose body was prose yields a cycle with **no** verdicts, and that is a
distinct rendered state: *"no usable verdict recorded — a degraded cycle rather than a
quiet market"*. This is precisely audit item 9 from journal/M8_2_REPORT.md §1a
("`candidates=0` still reads the same whether the screener judged or failed"), and it is
now visible to anybody who types six characters.

## 7. Decisions

1. **The name `/pulse` is reused rather than kept free.** `TELEGRAM_UX.md` §3 has listed
   it since M4 as "on-demand market regime summary (P1)" — never built, never registered,
   no stored value and no user habit. It is the right name for this. Recorded as a dated
   supersession with a new §3b, not slipped in; the regime summary is still wanted and
   §5's digest already lists a regime line for it.
2. **The last *completed* cycle, not the last cycle.** `CycleRepository.latest()` happily
   returns a `RUNNING` row mid-flight, and a cycle still in its screener has escalated
   nothing yet — which renders identically to a cycle that found nothing, and one of
   those is a working system. A new `latest_completed()` filters on `finished_at`.
3. **A `FAILED` cycle is still shown**, with a banner and its error. It finished, it has a
   partial story, and hiding it would leave the newest thing anybody could see silently
   stale — the exact failure this command exists to prevent.
4. **The day view counts one gate outcome per (cycle, symbol), not per row.** One shared
   analysis produces one `gate_decisions` row per user, so counting rows would make the
   totals grow when somebody joins — which is not a fact about the market. This is
   M8.1 §4's fourth hole (the one that would have multiplied the win rate) on a new
   surface, and it has its own test.
5. **`/pulse` is on `MEMBER_COMMANDS`, deliberately not on `OWNER_COMMANDS`.** It reaches
   the owner through the same `commands_for` that already gives them the member set.
   Putting it in the owner half would have taken it off every member's menu — the test
   says so in its assertion message.
6. **No silent caps.** Each section lists at most 8 rows and the card says `+N more` when
   it cuts. A card over 4096 characters is not truncated by Telegram, it is **not sent**,
   so the cap is load-bearing; `test_a_full_card_fits_one_telegram_message` fills all four
   sections past the cap with maximum-length prose and is what will fail if either
   constant is raised.
7. **Analyst prose is cut at a whole word and always marked with an ellipsis**, so a
   reader can tell a short thesis from a trimmed one. The full text stays in
   `analyst_reports`.

## 8. Verification

```
1260 passed, 47 skipped          hermetic
1306 passed,  1 skipped          with SENTINEL_TEST_DATABASE_URL
100% branch coverage             sentinel/risk — not one line touched by this milestone
mypy --strict                    clean, 216 files
ops bash -n clean · wheel builds · image imports, prompts ship, config loads
no migration — alembic head is unchanged at 0009
```

**Reachability, for both roles, against a real `Dispatcher`** — journal/M8_2_REPORT.md
§1a's standing rule, and the reason it needs its own test rather than riding on the
member sweep: `commands_router` is registered *before* `admin_router`, so a future
owner-only handler of the same name, or an `OwnerOnly` filter added to the wrong router,
would silence `/pulse` for the owner and nothing in the owner sweep would notice —
`/pulse` is not on `OWNER_COMMANDS`. Both the bare form and `/pulse 24h` are covered for
both roles, because the argument takes a different query path. A caller with no standing
still gets silence, asserted on **outbound calls** rather than on the `UNHANDLED`
sentinel: the gate is an outer middleware and a `DROP` returns `None`, which
`feed_update` hands back verbatim — indistinguishable from a handler that ran and
returned nothing. What is observable, and what matters, is that the stranger's phone
stayed quiet.

**Postgres round-trips for all three new reads** (M8.3 §4's lesson, applied before it
cost anything this time). The fake returns verdicts already made, so the JSONB path, the
`finished_at IS NOT NULL` ordering, the `role='primary'` filter and the three `IN`
filters had never run in the hermetic suite. A typo in any of them is a
`ProgrammingError` on the first `/pulse` in production — the one path every approved user
is invited to take on the day it ships. Eight tests, including the empty-id-list guard
each query carries.

**One bug the tests caught before a human could.** The first draft capped each section at
10 rows with a 150-character thesis, and a full card came out at **5821 characters** —
over Telegram's limit, which means not delivered at all. Caps are now 8 rows, 110
characters of thesis and 90 of screener reason, and the test that found it is written to
fail again if any of the three is raised.

## 9. Where this lands relative to the §1a audit

journal/M8_2_REPORT.md §1a listed ten places where silence is the intended success state.
`/pulse` does not close any of them as *tests* — it is a command, not a test — but it
changes what two of them cost:

* **Item 9 (quiet cycles / `NO_SETUP`).** "`candidates=0` still reads the same whether
  the screener judged or failed." It does not any more, to a human: `/pulse` distinguishes
  the two in words, on demand, from a phone.
* **Item 5 (`dry_run` publishes nothing).** A rehearsal still produces a completely silent
  phone by design, and now anybody can ask what the rehearsal did. The DRY RUN banner is
  on the card for members too, because a member in a dry run gets nothing at all and
  nothing else tells them why.

Its own new surfaces are covered the way §1a requires: positive reachability for both
roles, against real machinery.

## 10. Notes for M9

- **`/pulse 24h` is the fastest read of the numbers M8.3 §7 says to keep watching** —
  escalations per symbol, the verdict distribution (the `WATCHLIST` share of what
  `screener_v2` escalates is what tells quieter from sharper), skips by reason, and the
  gate's top rejection codes. It is the same data as the SQL in M8.2 §4, without an SSH
  session.
- **The gate line's `per-account` bucket is not a rejection reason** — it is everything
  the card declines to attribute. When M9 wants the real distribution, query
  `gate_decisions` directly with a `user_id` filter; `/pulse` is deliberately the
  lossier view.
- **Adding a `RejectionReason` or a `SkipReason` now fails a test** until it is
  classified. That is the intended cost: the alternative is a code that quietly never
  appears, or an enum name rendered raw on somebody's phone.
- **If a screener prompt version ever changes `ScreenerVerdict`'s shape**, older stored
  rows stop parsing and `/pulse` reports fewer verdicts for cycles before the change.
  It logs `storage.screener_verdict_unreadable` when that happens.

## 11. What is left for you

The live Telegram round trip, which cannot be verified from here — one bot token, one
long-polling client, and the server holds it (journal/M8_REPORT.md §9a):

1. `/pulse` and `/pulse 24h` from your own chat.
2. Type `/` and confirm `/pulse` is in the menu (it is republished at every start).
3. If you still have a member account to hand, the same two commands from theirs — the
   card should be identical apart from the missing `💵` line.

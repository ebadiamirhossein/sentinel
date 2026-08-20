# M10a — The market dimension

**Date:** 2026-08-20. **Status:** complete, `make check` green, **not deployed** —
the owner deploys after review, and §9 below is a deploy blocker to read first.

M10a teaches Sentinel to hold more than one market. It adds **no forex code**: no
adapter, no provider, no forex specification. What it adds is the dimension — in the
schema, the config, the spend guard, the rails, the statistics and every Telegram
surface — so that M10b can add a market rather than a market *and* the concept of one.

The binding constraint was that crypto output must not change while a measurement
window is running (REAL=0, HYPOTHETICAL=2). It did not. §2 is the evidence.

---

## 1. What was built

**A `Market` enum in a leaf module** (`sentinel/core/markets.py`), imported by
storage, stats, bot, llm and core alike without a cycle.

**Migration `0010_market_dimension`.** A `market` column on ten tables —
`market_snapshots`, `ohlcv_candles`, `instrument_meta`, `ingestion_failures`,
`llm_calls`, `analyst_reports`, `gate_decisions`, `signals`, `watchlist_requests`,
`cycles` — added `NOT NULL DEFAULT 'crypto'` in one statement, which is a
catalogue-only operation on PG 11+ and so does not rewrite `ohlcv_candles`. Three hot
indexes become market-leading; `uq_watchlist_requests_one_pending` becomes
`(market, symbol)`; two new tables (`market_pause_state`, `user_market_pauses`); and
the stored watchlist row is renamed `watchlist` → `watchlist:crypto`.

**`MarketScopedRepository`.** The market is a property of the repository *instance*,
not a parameter on thirty methods. A call site decides once; every write below stamps
it and every read filters on it. This is the choice that kept the diff reviewable —
`mypy --strict` finds a missing constructor argument, and could never have found a
missing `.where(market == ...)`.

**A `markets:` config block** with `enabled`, `dry_run`, `adapter`, `watchlist`,
`watchlist_max_symbols`, `scan_interval_minutes` and an LLM budget per market. A file
with **no** `markets:` block is read as crypto-only — which is what the deployed
server runs. A file carrying **both** shapes is refused at load: two places to set
`dry_run` is one place for it to disagree, silently, in the direction that publishes
real cards during a rehearsal.

**Two-tier spend guard.** Each market has a daily budget; a global ceiling sits above
both. Sub-budgets deliberately sum to more than the ceiling (10 + 4 against 11) so
markets compete rather than reserve. The ceiling is checked *first*, so a sub-budget
configured above it cannot mask it.

**Four pause rails** — global, per market, per user, per (user, market) — composed in
a new pure module `sentinel/core/pauses.py`, widest wins, and handed to the risk
engine as the single `PauseState` it has always taken.

**Statistics gain the market as a fourth dimension, and it is the strict one.**
`StatsReport` no longer has `real`/`hypothetical`/`dry_run` fields; it carries a tuple
of `BookStats`, each labelled with its `Book(population, market)`. There is no field
that could hold a merged figure. `summarize()` raises on rows from two markets.

**One scan job per enabled market.** With forex disabled, exactly one, at 60 minutes —
the same job, the same interval, the same behaviour as the cycle before it.

---

## 2. What deliberately did NOT change, and how that is known

`sentinel/risk/` and `sentinel/analyst/prompts/` have a **zero-line diff**:

```
$ git diff --stat sentinel/risk/ sentinel/analyst/prompts/
(no output)
```

Risk-engine branch coverage is unchanged at **100%** (604 statements, 152 branches).

### 2a. The golden suite, built before anything else

`tests/golden/` was written and passing **before the first refactor**, and re-run
after every change. Thirty assertions in two halves:

**The pipeline** (`test_golden_cycle.py`), driven from recorded cassettes with a
frozen clock: computed feature values, three chart PNG digests **and** their render
parameters, the fully assembled analyst prompt, an approved gate decision with its
whole sized plan, a rejected one with its `RejectionReason`, and the rendered signal
card. One byte different fails.

**Every surface** (`test_golden_surfaces.py`): `/status`, `/stats`, `/pulse`,
`/pulse 24h`, `/pulse SYMBOL`, `/positions`, `/watchlist`, `/snapshot`, and
`/journal`'s workbook read back through openpyxl — sheet names in order, header rows,
first populated row. These exist because Step 6 removes three fields from
`StatsReport` and Step 7 edits six commands; asserting they were unchanged required
capturing what they rendered first.

**And the same surfaces rendered from the frozen pre-M10a `config.yaml`** — the
legacy-shaped file the server actually loads — asserted equal to the goldens produced
from the new `markets:`-shaped file. That is the strongest form of the promise: not
"the code is equivalent" but "the config the live system has on disk produces the
identical characters".

**The goldens are not vacuous.** A parametrized test perturbs each frozen crypto
default — `charts.candle_window`, `features.rsi_period`, `risk.min_rr_tp1`,
`risk.risk_per_trade_pct`, `ladder.weights_pct` — and asserts a golden moves. Without
it, "everything is unchanged" would be a claim nothing could falsify.

### 2b. What the goldens caught

**`$10` became `$10.0` on `/status`.** The new `config.yaml` wrote
`llm_daily_budget_usd: 10.00`; YAML parses that as the float `10.0`, giving
`Decimal("10.0")`, which renders as `$10.0` where the live card says `$10`. The
config-equivalence test did **not** catch it — `Decimal("10.0") == Decimal("10")` is
`True`. The `/status` golden did. Fixed by writing `10` and `7` in the yaml, and a new
test now asserts the money values match as **strings**, not merely as Decimals.

This is the milestone's own justification for R1: a single character on a live card,
invisible to equality, found only because the surface was pinned before it was touched.

---

## 3. Migration safety

`ops/verify-migration.sh` restores a populated `pg_dump` into a scratch database,
runs `alembic upgrade head`, asserts every market-bearing table has zero rows that
are not `crypto` and that row counts are unchanged, then `downgrade` and `upgrade`
again — and then exercises the watchlist rename against a seeded row. Run against a
**fresh production-shaped dump** (signals=4, cycles=36, llm_calls=83):

```
restored at 0009_watchlist_requests · signals=4 cycles=36 llm_calls=83 gate_decisions=15 analyst_reports=41
step 2/6 — upgrade head
backfill OK — every row in 10 tables is 'crypto'
row counts unchanged — signals=4 cycles=36 llm_calls=83 gate_decisions=15 analyst_reports=41
no stored watchlist row (production's shape) — upgrade clean, nothing renamed
step 3/6 — downgrade to 0009_watchlist_requests
downgrade OK — columns dropped, signals=4 cycles=36 llm_calls=83 gate_decisions=15 analyst_reports=41
step 4/6 — upgrade head again
backfill OK — every row in 10 tables is 'crypto'
step 5/6 — the watchlist rename, against a seeded row
watchlist rename OK — renamed with its value and its config_changes history, and reversed
MIGRATION OK — 0009 → 0010 → 0009 → 0010
```

### 3a. The watchlist rename, and the case production actually hits

The live `runtime_settings` contains **only `capital_eur`**. There is no watchlist
row — the live watchlist comes from `config.yaml` — so 0010's rename of `watchlist` →
`watchlist:crypto` is a **no-op in production** and no real dump can exercise it.

That is a coverage gap, not a reason to skip the step, so the script now covers both
cases and **asserts** rather than logs:

* **Case A — no watchlist row (production's shape).** The upgrade runs clean and the
  downgrade runs clean, and the script asserts that the upgrade did not *invent* a
  row of either key. Previously this branch only printed a note, which is precisely
  the kind of "we didn't check" that reads like "we checked". Verified against the
  fresh dump above: both directions succeed with no watchlist row present.
* **Case B — a seeded row.** The script steps back to `0009`, inserts a
  `runtime_settings` row keyed `watchlist` with the value `["BTCUSDT", "SOLUSDT"]`
  plus a matching `config_changes` history row, upgrades, and asserts: the key became
  `watchlist:crypto`, the old key is gone, **the value survived intact**, and the
  `config_changes` history was renamed with it. Then it downgrades and asserts the
  key is `watchlist` again with its value still intact, and returns to head.

The value assertion is the load-bearing one. A rename that dropped or rewrote the
symbols would leave the owner screening a different watchlist after a deploy, with
nothing to notice it by.

### 3b. The script broke the local dev environment — twice — and what fixed it

Worth recording in full, because it is the project's own named failure mode and this
script is the one whose whole job is to be trusted about a database.

`ops/lib.sh`'s `compose` helper passes `-f docker-compose.yml` only. That is correct
on the server, where `docker-compose.dev.yml` must never be picked up (it publishes
5432 on a host whose 5432 belongs to a neighbour app). This script was the only ops
script that ran `compose run app`, and `run` starts the service's `depends_on`:
Compose compared the running postgres container against the narrower definition,
found the published port absent from it, and **recreated the container without it**.
Postgres kept running and kept reporting healthy while becoming unreachable from the
host — every DB test failing with `Connect call failed ('127.0.0.1', 5432)` — and the
script printed `MIGRATION OK`. A green result concealing a broken machine.

**The first fix made it worse, which is the part worth reading.** The script was
changed to derive its compose files from the running container's own
`com.docker.compose.project.config_files` label. Correct idea; the implementation read
them with `while IFS= read -r entry`, which **silently drops the last line when the
input has no trailing newline** — and the label has none. So it ran with
`-f docker-compose.yml` alone: exactly the narrow invocation it was written to
prevent, arriving by a new route. It broke the port again on its very first run.

**The fix that shipped removes the hazard class instead of guarding it.** Nothing in
this script now runs a compose command that can create, recreate or stop a container:

* `compose exec` and `compose ps` only — both read-only with respect to container
  definitions.
* the app image is built with plain `docker build`, tagged `sentinel:migration-check`
  so it can never be confused with what the deployment runs;
* alembic runs under plain `docker run`, attached to the project's existing network
  (resolved from the postgres container). `docker run` does not know what a compose
  service is, so it cannot reconcile one.
* credentials still never reach a process listing. Docker's `--env-file` is **not**
  Compose's — it does no comment stripping, so passing `.env` straight in set
  `SENTINEL_ENV` to `dev   # dev → text logs …` and the app refused to boot. A
  mode-600 temp file holding only `POSTGRES_USER` and `POSTGRES_PASSWORD`, parsed with
  `lib.sh`'s comment-aware `env_value`, is written and removed by the trap.

A belt-and-braces check remains: the published ports are captured before anything runs
and compared on **every** exit path — success, failure and Ctrl-C. It **reports and
fails; it does not repair**, because repairing means running `compose up`, which is
the very command that caused the damage. A guard whose remedy can reproduce the fault
is not a guard. If it ever fires it prints what changed, prints `make up`, and turns
the run red.

**Verified:** two consecutive full runs, both exit 0, with the published port and the
container's compose-files label unchanged and `127.0.0.1:5432` still reachable from
the host afterwards, and no temp env file left behind. The mismatch branch of the port
check was exercised directly by forcing a wrong "before" value; the matching branch
was confirmed silent.

If your environment is already in the broken state from an earlier run, `make up`
restores it — that is what re-creates the stack with the dev overlay layered on.

**The script hard-refuses the production database** (R4), mirroring
`tests/db_guard.py` one layer out. It compares the scratch name against `POSTGRES_DB`
and against the database component of `DATABASE_URL` with credentials stripped, and
exits non-zero on a match — before it touches anything. Verified both ways: naming the
live database exits 1, and an explicitly empty scratch name exits 1 (which required
changing `${VAR:-default}` to `${VAR-default}`, since the former made the guard
unreachable).

**The server default is kept, not dropped.** If the app is rolled back to the previous
commit against a migrated database — which `ops/update.sh`'s failure path tells the
owner to do — the old code inserts rows with no `market` at all. With the default in
place those inserts succeed and land as `crypto`. Without it the rollback would not be
a rollback.

**db_guard.** Every new test file is hermetic: `test_multi_market.py`,
`test_pauses.py`, `test_market_spend.py` and all of `tests/golden/` import no
`requires_db` and open no socket — the autouse `no_network` guard in
`tests/conftest.py` fails any test that tries. No new test can reach any database.

---

## 4. Spec corrections, dated

**2026-08-20 — crypto's daily LLM budget is 10.00, not the brief's 8.00.** The live
rail is `llm.daily_spend_limit_usd: 10`. Adopting 8.00 would tighten a spend rail in
the middle of the measurement window — a behaviour change, and precisely the kind the
milestone exists to prevent. Crypto keeps 10, forex gets 4, the ceiling is 11; the
sub-budgets still sum past it, so the competition the brief wanted is intact. Settled
with the owner before implementation.

**2026-08-20 — the pause composition lives in `sentinel/core/pauses.py`, not
`sentinel/risk/rails.py`, where it belongs.** The brief freezes `sentinel/risk/`
absolutely. The composition is pure, has its own truth table, and is handed to the
gate as the existing `PauseState`, so nothing in `risk/` needed to know a market
exists. **M10b should move it** once the package can be touched again.

---

## 5. Defects found while building

**The per-market loss rail re-paused people the combined rail had already stopped.**
An existing tracker test (`test_an_existing_pause_is_not_re_raised_for_that_user`)
failed the moment the rail was split — correctly. A user paused across all markets
would have been paused again per market and notified a second time for the same bad
day, which is the per-minute repetition M7 built `already_paused_for_loss` to prevent,
arriving by a new route. The narrower rail now yields to an active wider one, and
three new tests pin it.

**The tracker read only crypto's open signals, silently.** `TrackerLoop` took its
signal repository at the default market, so with a second market enabled its signals
would never have been tracked — fills missed, stops missed, outcomes never resolved,
and nothing saying so. Correct behaviour today (no forex signal can exist) arrived at
by accident rather than by decision. The loop now takes its market explicitly, because
a `PriceFeed` wraps exactly one adapter and can only price one market; `core/app.py`
names `Market.CRYPTO` at the call site. M10b adds the second feed and the second loop
against a stated constraint rather than a default.

**`ops/verify-migration.sh` recreated the local postgres container without its
published port**, reporting `MIGRATION OK` while leaving the machine unable to run its
own test suite — twice. And the first attempt at a fix reintroduced it through a
`while read` loop that dropped the last filename. Both are written up in §3b; the
second is the more instructive, because the newline looked cosmetic and was
load-bearing.

**`/status` declared a market header and never rendered it.** Caught by the
multi-market surface tests, not by types: the field existed on `StatusView` and no
renderer read it. The same tests found that `/status`'s spend line was comparing this
market's totals against the *global* budget, so a forex reader would have seen
"$7.42 of $10" where their real limit was $4.

---

## 6. Known consequences, for M10b to decide (R5)

**The global ceiling can squeeze the measured market.** Crypto's budget is 10 and the
ceiling is 11, so once forex is enabled a forex-heavy morning can stop crypto analysis
at, say, $7 of its own $10. This is the deliberate cost of letting markets compete
instead of reserve, it is **harmless today** — a disabled market cannot spend anything
— and it becomes real the day forex is switched on. It is asserted, not merely
described (`test_the_squeeze_is_real_and_asserted`).

**Open question for M10b:** should the measured market hold a reserved floor, so a new
and unmeasured one can never take budget from the book the whole project exists to
measure? M10a deliberately did not decide this; it belongs with the milestone that
makes forex able to spend.

**`ohlcv_candles` keeps its `(symbol, timeframe, open_time)` primary key** and gains
`market` as a plain column. Two markets sharing a symbol string would collide. Not a
risk today — `BTCUSDT` and `EURUSD` are disjoint — and widening the key means dropping
and rebuilding it on the largest table in the schema for a query that does not exist.
`bot/markets.market_of_symbol` rests on the same assumption. **M10b should decide
whether the key grows before any symbol namespace can overlap.**

---

## 7. The compose project name (asked in the brief)

**The pin is present and asserted.** `docker-compose.yml:19` has `name: sentinel`, and
`tests/test_compose.py::test_the_project_name_is_pinned` asserts it. The local
container is `ai-trader-postgres-1` only because `.env:45` sets
`COMPOSE_PROJECT_NAME=ai-trader`, which overrides the file's `name:` — and that
override is deliberate and documented in two places already (the compose header
comment, and `.env.example:34`), so a laptop keeps its existing volume. HANDOFF.md is
correct about the server. No discrepancy, and nothing was changed.

---

## 8. The Postgres round-trip suite — run, and how

**Run by the owner on 2026-08-20, against a scratch database migrated to `0010`:
the full suite reports 1 skipped and 0 failed, down from 55 skipped.** So the 55
`requires_db` tests — `bot`, `ingestion`, `llm`, `risk` and `tracker` persistence —
all executed against the new schema and passed. That closes the last verification gap
in this milestone: every claim about the market column, the scoped repositories and
the two new pause tables has now been exercised against real Postgres, not only
against the in-memory doubles.

Nothing in them needed changing, which is the expected result and worth saying why:
inserts that omit `market` are filled by the server default, and every repository read
now filters on `crypto`, which is what those fixtures write.

They were not run from this session. Setting `SENTINEL_TEST_DATABASE_URL` on a command
line would have put the database password into a process listing — the one thing
`ops/lib.sh` refuses to do — so it was left to the owner's own environment.

### The working procedure

It took several attempts to get right, so the two preconditions are recorded here
rather than rediscovered. **Both are required; either one missing makes every DB test
fail, and the failures do not name the cause.**

1. **The local Postgres must be up with the dev overlay**, so `127.0.0.1:5432` is
   published. `docker-compose.yml` alone publishes nothing — that is deliberate, the
   server's 5432 belongs to a neighbour app — so the port only exists when
   `docker-compose.dev.yml` is layered on:

   ```
   make up
   ```

   Without it the container is healthy and unreachable, and the tests fail with
   `Connect call failed ('127.0.0.1', 5432)`. (This is the same state
   `ops/verify-migration.sh` used to cause; see §3b.)

2. **The scratch database must be created *and migrated to head*.** Creating it is not
   enough — an empty database has no tables, and the failures look like application
   errors rather than a missing schema:

   ```
   docker compose exec postgres createdb -U sentinel sentinel_test
   # then, with SENTINEL_TEST_DATABASE_URL pointing at .../sentinel_test:
   .venv/bin/alembic upgrade head
   ```

   `alembic` reads `DATABASE_URL`, not `SENTINEL_TEST_DATABASE_URL`, so the upgrade has
   to be pointed at the scratch database explicitly — exporting
   `SENTINEL_TEST_DATABASE_URL` alone migrates nothing.

3. Then run the suite. `tests/db_guard.py` refuses to run if
   `SENTINEL_TEST_DATABASE_URL` resolves to the same host and database as
   `DATABASE_URL`, so a scratch name distinct from `sentinel` is not optional:

   ```
   .venv/bin/pytest
   ```

**Expected result:** `1 skipped`, `0 failed`. The remaining skip is not a database
test.

### Still not run

**No live cycle.** `python -m sentinel.tools.cycle --dry-run` makes real Anthropic
calls and costs money; the golden suite exercises the same deterministic path offline,
and the first live cycle after deploy is the owner's call.

---

## 9. Deploying this — read before `ops/update.sh` (R2)

**`ops/update.sh` will fail on this commit, at step 2, before it builds anything.**

`ops/update.sh:31` runs `git pull --ff-only`. `config.yaml` is a tracked file, and
docs/DEPLOY.md §6/§13 tell the operator to edit the deployed copy in place
(`sed -i 's/^dry_run: true/dry_run: false/'`), which was done on 2026-08-19. The
working tree on the server is therefore dirty in exactly the file this commit
changes, and the pull will refuse with "local changes would be overwritten".

**The fix, on the server, before running the update:**

```
cd /opt/sentinel && git checkout -- config.yaml && ops/update.sh
```

Nothing is lost: the repo's `config.yaml` already carries `dry_run: false`, and after
this commit that value lives at `markets.crypto.dry_run: false`.

**What the first boot does.** The container applies `alembic upgrade head`, which adds
the ten columns, backfills them to `crypto`, creates the two pause tables and renames
the stored watchlist row. The app then loads the new `markets:`-bearing config,
registers one `scan:crypto` job at 60 minutes, and runs a cycle that is behaviourally
identical to the one before it: same watchlist, same spend rail at $10, same
`dry_run: false`, same cards. Forex is disabled and has no adapter registered, so
nothing in that half of the config is reachable.

**If the app starts before the migration finishes** — it cannot, the entrypoint runs
migrations first — the per-market watchlist key would be missing. That case is handled
anyway: `bot/runtime.stored_watchlist` falls back to the pre-M10a bare `watchlist`
key, so the owner's list survives either ordering.

---

## 10. Numbers

* 1561 tests pass, 55 skipped, 0 failures in the hermetic suite.
* With the opt-in Postgres suite enabled (owner, 2026-08-20, scratch database at
  `0010`): **1 skipped, 0 failed** — all 55 database round-trip tests executed and
  passed against the new schema.
* `ops/verify-migration.sh`: two consecutive runs, exit 0, against a fresh
  production-shaped dump — both watchlist cases, and the dev environment intact.
* `mypy --strict`: clean over 236 source files.
* `make check` exit 0 — tests, ruff, mypy, 100% risk branch coverage, `check-ops`,
  wheel build, image build **and** in-image import.
* 41 files changed, ~2,950 insertions. `sentinel/risk/`: zero.

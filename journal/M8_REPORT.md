# M8 — Hardening & Deploy · Report

**Date:** 2026-08-19
**Status:** DONE — `make check` green (995 hermetic tests, 1022 with the Postgres
suite), `mypy --strict` clean over 200 files, 100% branch coverage on
`sentinel/risk/`, and the backup/restore path **proven by restoring a real dump of
the real database** rather than by writing a procedure and hoping.

One thing this report cannot claim: **the server steps have not been run on the
Hetzner box.** I have no access to it and did not ask for any. Everything was
verified locally against the same compose files, the same scripts and a real
Postgres — see §9 for exactly what that did and did not cover, and §10 for the
short list that is yours to run.

---

## 1. What was built

| Area | Contents |
|---|---|
| `docs/DEPLOY.md` | The runbook. Fourteen sections; §1–§8 are 24 minutes and end with a verified running system, §9–§11 add ten more for backups, the restore drill and the reboot test. Written to be followed without me. |
| `docker-compose.yml` | `name: sentinel`; **no published database port**; `/health` on `127.0.0.1:${SENTINEL_HTTP_PORT:-18080}`; per-service log caps. |
| `docker-compose.dev.yml` | New. The loopback database publish that only a development laptop needs. |
| `sentinel/core/alerts.py` | Pure: `CycleOutcome`, `consecutive_failures`, `cycle_alert`. The whole alerting rule, no clock, no DB, no bot. |
| `sentinel/bot/alerts.py` | `AdminAlerter.after_cycle()` — evaluates and delivers, and never raises into the scheduler. |
| `sentinel/bot/{views,readmodels,cards}.py` | `AlertView`, `alert_view()`, `alert_card()`. The renderer stays arithmetic-free. |
| `sentinel/core/app.py` | The `scan` job calls the alerter after every cycle — including the ones that succeed, which is how a recovery is announced. |
| `sentinel/core/orchestrator.py` | `spend_state_before` / `spend_state_after` on `CycleResult`. |
| `sentinel/core/config.py` + `config.yaml` | `AlertsConfig`: `consecutive_cycle_failures: 3`, `stale_cycle_multiplier: 2`. |
| `sentinel/storage/repositories.py` | `CycleRepository.recent(limit)`. |
| `sentinel/tools/alert.py` | `--simulate` / `--send` — the runbook's proof that the alert path reaches the phone. |
| `ops/` | `lib.sh`, `backup.sh`, `verify-backup.sh`, `restore.sh`, `healthcheck.sh`, `update.sh`, `crontab.example`, `logrotate.sentinel`. |
| `Makefile` | `check-ops` (now in the gate), `backup`, `verify-backup`; dev targets layer the dev overlay. |
| Tests | `tests/core/test_alerts.py` (23), `tests/bot/test_admin_alerts.py` (10), `tests/test_compose.py` (8), plus 2 in `tests/core/test_orchestrator.py`. |
| Docs | `docs/ARCHITECTURE.md` §5 dated correction, `docs/MILESTONES.md` M8 scope note, `CLAUDE.md` gate line, README rewritten. |

## 2. The shared server, and the three things that follow from it

The target is not a fresh VPS. It is a Hetzner CPX32 already running an unrelated
application **and that application's own Postgres**. Every deployment decision here
falls out of that, and each is asserted by `tests/test_compose.py` rather than only
described in a document — a promise about somebody else's production box that lives
only in a runbook is a promise that regresses the first time someone adds a port
"just for debugging".

**1. Sentinel's Postgres publishes nothing.** Not `0.0.0.0:5432`, not
`127.0.0.1:5432` — no `ports:` key at all. The app reaches its database over the
Compose network, where a publish buys nothing. The development overlay
(`docker-compose.dev.yml`) adds the loopback publish that `make run`, alembic and
the `sentinel.tools.*` CLIs need on a laptop, and the server never loads that file.
On the server the same commands go through the container:

```
docker compose run --rm --no-deps app python -m sentinel.tools.stats
```

**2. The app's host port is `127.0.0.1:${SENTINEL_HTTP_PORT:-18080}`.** Two
choices in one line. 18080 because 8000 and 8080 are what a neighbour is likely to
hold; loopback because **Sentinel needs no inbound port whatsoever** — Telegram is
long polling, i.e. outbound — so the honest number of internet-facing ports is
zero. The previous compose file published `0.0.0.0:8000`, which on a public VPS
would have exposed version, environment and last-cycle age to anyone scanning. The
container-internal port stays `HEALTH_PORT`, and the healthcheck interpolates the
same variable so the two cannot drift.

**3. The Compose project name is pinned:** `name: sentinel`. Without it Compose
names the project after the directory, so the guarantee "our volumes are ours"
would depend on what someone called a folder. Containers are `sentinel-app-1` and
`sentinel-postgres-1`; the volume is `sentinel_pgdata`.

That last one had a consequence on **this** machine, which is why it was brought
to you before it was written: the laptop's data lives in `ai-trader_pgdata`, created
before the key existed. `COMPOSE_PROJECT_NAME` outranks the file, so your local
`.env` now pins `ai-trader` and nothing was renamed, migrated or lost. Verified: the
stack was recreated on the new compose file and still reports the same 1 signal,
3 cycles and 11 LLM calls.

## 3. Alerting — three rulings the specs do not make

ARCHITECTURE §2 and §6 both say "Telegram admin alert after **3 consecutive cycle
failures**", and that is the only thing they say. Three questions had to be
answered, and the answers are in code with a truth table over them rather than in
someone's head.

**What counts as a failure.** `cycles.status` is written `RUNNING` before a cycle
starts and rewritten at the end (M7's decision: a cycle that dies leaves evidence
rather than vanishing). So a process killed mid-cycle leaves `RUNNING` behind for
ever, and no status check would ever notice. **A `RUNNING` row older than 2 x the
scan interval counts as a failure** — the most serious kind, since it means the
process died rather than the cycle. A younger one is the cycle in flight: not a
failure, and deliberately **not a reset either**, because every evaluation happens
with an in-flight row at the top of the list and a reset there would mean the alert
could never fire at all.

**How often to repeat.** Once at 3, then again every 3 further failures — 3, 6, 9.
This is computed from the rows each time, so there is **no "last alerted at" state
anywhere**: nothing to get stuck, nothing to lose in a restart, and no migration in
M8 at all. The alternative — alert once and go quiet — leaves an owner whose last
message was six hours ago unable to tell "still broken" from "fixed".

**Recovery is announced.** One notice on the first OK cycle after a qualifying
streak, and exactly one: the next cycle sees an OK in front of the run and computes
a streak of zero. Without it, the last thing the phone ever says about an outage is
that it was still broken.

There is no alert format in `specs/TELEGRAM_UX.md`, so `alert_card` is an
**addition, not a deviation**. It is deliberately unlike a signal card — no
buttons, no charts, no disclaimer, no numbers from a plan — because nothing should
be mistakable for something to trade at 3am. It leads with what stopped and, on the
failure alert, with what did **not**: *"The tracker is unaffected — open positions
are still being watched."* An owner woken by an alert with money at risk needs that
sentence before anything else.

## 4. Verifying the spend guard found that half of it did not exist

MILESTONES M8 says the guard ships in M7 and M8 should "verify it in the runbook
instead of building it". Writing that verification step is what found the hole.

`journal/M7_REPORT.md` §1 describes "a Telegram notice when either trips", and
`docs/MILESTONES.md` promised the same. **No code ever sent one.** `WARN` and
`LIMIT_REACHED` reached `structlog` and the `/status` card and stopped there — so
the guard suspending analysis overnight was something you would discover the next
morning by asking.

You chose to close it through the alert channel M8 was building anyway. The
mechanism is worth a paragraph because it needed no new state either:

The orchestrator now reads the guard **twice** — once before the expensive tier,
which is where it already read it, and once after the cycle's own calls are
committed. Both land on `CycleResult`, and the alerter messages only on a
**transition**. Exactly one cycle in a day sees `OK → WARN`, and it is the cycle
that caused it. A constant-state cycle says nothing, so a day spent above the warn
level does not send 60 messages. And because the state is derived from `llm_calls`
rather than remembered, a restart cannot duplicate or lose the notice — the same
property that already makes the guard self-clear at 00:00 UTC.

`tests/core/test_orchestrator.py` proves the *ordering* rather than the plumbing:
the fake accumulates spend as calls are recorded, so a second reading taken before
the commit would report the wrong side of the limit and fail.

## 5. Backups — and the restore that was actually run

**`ops/backup.sh`** — `pg_dump -Fc` to `$BACKUP_DIR/sentinel-<UTC>.dump`. Three
things it does that a one-line cron does not:

- writes `.part` and renames only after `pg_dump` exits 0, so a dump interrupted by
  a reboot never leaves a truncated file that *looks* like a backup;
- reads the finished file back with `pg_restore -l` before counting it — a file
  that cannot be listed cannot be restored, and it should fail tonight rather than
  in six months;
- messages Telegram on any failure. A backup job that fails silently is worse than
  none, because you believe you have one.

Retention is `BACKUP_RETENTION_DAYS` (14) **with the newest 3 always kept**,
whatever their age. A retention policy that can leave a box with zero backups is
not a policy.

**`ops/verify-backup.sh`** is the part that makes "we have backups" a statement of
fact. It restores the newest dump into a **scratch database inside the same
container**, asserts `alembic_version` and the row counts on `signals`, `cycles`
and `llm_calls`, and drops it. The live database is never opened for writing, so it
is safe to run at any time — which is why it is a weekly cron line rather than a
good intention.

Run here, against the real database:

```
verifying .../sentinel-20260819T072258Z.dump into scratch database sentinel_verify_20260819072302
RESTORE OK — migration 0006_tracker_and_stats · signals=1 cycles=3 llm_calls=11
```

**Postgres credentials are never read on the host.** Every database command is
`docker compose exec postgres sh -c '... "$POSTGRES_USER" ...'`, using the
environment already inside the container, so the password never appears in a
process listing, a log line or a shell history. Only the Telegram token is read
from `.env`, by name, and curl's stderr is discarded because the token is in the
URL.

**`ops/restore.sh`** is the destructive one: stop the app, drop and recreate the
database, `pg_restore`, check `alembic_version`, start the app. It requires typing
`restore`, because everything written since the dump — signals, your decisions,
tracked outcomes, the audit trail — is gone. I did not run it against your
database. I ran its exact commands, verbatim including the quoting, against a
scratch database created for the purpose (§9).

**Backups live on the same disk as the server.** They survive a rebuild and a
reboot; they do not survive losing the box. You declined the off-site copy hook, so
the runbook says this in plain words instead of implying more safety than exists.

## 6. Log rotation, and why it is not logrotate

Container logs are capped in the compose file — `json-file`, `max-size: 10m`,
`max-file: 5`, **per service** — so Sentinel can never hold more than ~100 MB. Two
things make this the right shape rather than a `/etc/docker/daemon.json` edit or a
logrotate rule:

- Docker's json-file default is **unlimited**, and this app writes structured JSON
  every minute for ever. This is a real disk-filling path, not a hypothetical one.
- The daemon config is shared with the neighbour application. Changing its logging
  because of our tenant is not ours to do.

Logrotate is used only for what actually is a host file: the cron logs in
`/var/log/sentinel/`, weekly, 8 kept, compressed (`ops/logrotate.sentinel`).

## 7. Reboot — what happens, stated plainly

`docs/DEPLOY.md` §11 is the section you asked for. In order: Docker starts at boot,
`restart: unless-stopped` starts both containers, the app runs `alembic upgrade
head` and then boots, the tracker ticks ~60s later and the **first scan runs 15
minutes later** (an interval trigger fires one interval in, not immediately), while
`/health` is honest from the first second because `last_cycle_at` is seeded from
the `cycles` table.

Three caveats are stated rather than left to be discovered: a container you stopped
by hand stays stopped (that is what *unless-stopped* means); the tracker's lookback
is capped at 1000 one-minute candles, so an outage longer than ~16.7h leaves a hole
it logs as `tracker.lookback_capped` (M7 §12); and `sentinel_pgdata` survives
everything except `docker compose down -v`.

**A finding worth the paragraph it costs.** The obvious way to test all this —
`docker kill sentinel-app-1` — **does not work, and looks like a bug in the
config.** Docker treats a manual kill as a deliberate stop and does not apply the
restart policy; the container simply stays dead, `RestartCount=0`. Someone testing
their deployment that way concludes the restart policy is broken and starts writing
a systemd unit they do not need. So the runbook says so, and gives the real test: a
process dying on its own (verified here — `RestartCount` went to 1, the app came
back, `alembic upgrade head` re-ran, and `/health` reported
`last_cycle_age_seconds: 988` from the database rather than claiming nothing had
ever run).

The reboot itself is still yours to run. Nothing I can do locally proves that
`systemctl is-enabled docker` is true on your box.

## 8. The watchdog, and the gap it closes

`sentinel/bot/alerts.py` messages you when cycles fail. It cannot message you when
the app is not running — and a stopped stack looks exactly like a quiet market.
That is the failure mode that matters most and the one nothing inside the process
can report.

`ops/healthcheck.sh` runs from host cron every 15 minutes, curls `/health`, and
alerts after 3 consecutive failures (~45 minutes), then every 3 further failures,
with one message when it recovers — the same cadence and the same reasoning as the
in-app alert. It checks **HTTP rather than `docker compose ps`** deliberately: a
container can be `Up` with a wedged event loop, and `/health` answers 503 when the
database is unreachable, which is a thing you need to know and `ps` will not say.

## 9. Verification

```bash
make check        # 995 hermetic tests · ruff · mypy --strict (200 files)
                  # · 100% risk branch coverage · check-ops · wheel · image + in-image import
```

```
995 passed, 28 skipped                       hermetic
1022 passed, 1 skipped                       with SENTINEL_TEST_DATABASE_URL
Required test coverage of 100% reached       sentinel/risk
image imports, prompts ship, config loads
ops scripts: bash -n clean
```

`check-ops` is new in the gate and follows the same logic that put `check-image`
there: `ops/*.sh` run **only on the server**, so a typo in them is found at 3am by
the person who needed the backup. `bash -n` needs no extra tooling; shellcheck is
used when installed and never required (it is not installed here — that is why the
run above says "bash -n clean").

Everything below was run on this machine, against the real local stack and the real
database:

| What | Result |
|---|---|
| Stack recreated on the new compose file | `127.0.0.1:18080->8000`, database unpublished, project still `ai-trader` via the local pin, data intact |
| `ops/backup.sh` | 440K dump written, `pg_restore -l` readback passed |
| `ops/verify-backup.sh` | **RESTORE OK — migration 0006, signals=1 cycles=3 llm_calls=11**, scratch database dropped |
| `ops/verify-backup.sh` on a deliberately corrupt dump | fails, alerts, exits 1 |
| `pg_restore -l` on the same corrupt file | rejects it — backup.sh's readback guard bites |
| Retention | 6 aged dumps + 2 fresh → 5 pruned, newest 3 kept, including one 44 days old |
| `ops/restore.sh`'s drop/create/restore commands | run verbatim against a scratch `sentinel_drill` database: `DROP DATABASE`, `CREATE DATABASE`, `pg_restore` exit 0, `alembic_version` = 0006. The live database was never touched |
| `ops/healthcheck.sh` | healthy → ok; 3 failures → alert; 4th → silent; recovery → one notice; state file resets |
| Restart policy | process killed inside the container → `RestartCount=1`, healthy again, `/health` seeded from the DB |
| `python -m sentinel.tools.spend` in-container | `today $0.97 of $10 · state OK` |
| `python -m sentinel.tools.alert` in-container | reads real `cycles` rows and correctly declines to alert |
| `python -m sentinel.tools.alert --simulate 3` | renders the card |

Two notes on how that was done honestly. The failure paths were exercised from a
**copy of `ops/` with a token-less `.env`**, so that testing the alerting did not
send real messages to your phone; the copy pointed at the same running stack.
And `--send` was **not** run: it is a live Telegram message, it is `docs/DEPLOY.md`
§8.6's step, and it is yours to fire.

## 10. What is left for you

1. **Run the runbook on the CPX32.** That is M8's demo and the only thing that can
   confirm the 30-minute claim.
2. **The reboot test** (`docs/DEPLOY.md` §11) — the step people skip and regret.
3. **`python -m sentinel.tools.alert --send`** once, so the alert path is proven
   before an outage tries to use it.
4. **Delete signal #41 before M9** (journal/M7_REPORT.md §8). It is a fixture plan
   carrying a fictional −4.62R in the real-stats book, and it is still in your
   local database — it will not follow you to the server, but M9's numbers should
   not start with it either.
5. **24 hours with `dry_run: true`**, then read `/stats`' DRY RUN population before
   flipping it (`docs/DEPLOY.md` §13).

## 11. Deviations from spec

| # | Deviation | Why |
|---|---|---|
| 1 | Postgres publishes no host port; ARCHITECTURE §5 assumed a plain `docker compose up -d` | The target host's 5432 belongs to another application. §5 corrected with a dated note. |
| 2 | `pg_dump` writes to a host directory via `docker compose exec`, not to a mounted backup dir (§5) | Same outcome, no container-uid ownership trap on the mount. |
| 3 | Watchtower ("optional" in §5) is not used | Automatic image updates would change the behaviour of a system whose output a human trades on, without anyone noticing. `ops/update.sh` is deliberate. |
| 4 | The admin alert card is in no spec | `specs/TELEGRAM_UX.md` has no alert format. Recorded as an addition; §3. |
| 5 | The spend guard's Telegram notice was built here, not verified | It did not exist. Owner-approved before implementation; §4. |
| 6 | `make check` gained `check-ops` | Same class of gap as `check-image`. CLAUDE.md updated. |
| 7 | A host watchdog cron is in no spec | Owner-approved addition; §8. |

## 12. Notes for M9

- **The first 24 hours are the shakedown's zero point.** `dry_run: true` produces a
  measured paper record; `/stats` keeps it in its own population and never merges it.
- **`tracker.lookback_capped` is the log line to grep after any outage.** If the
  server is ever down for more than ~16 hours, the tracker cannot see fills older
  than its window, and the exit criteria count tracked signals.
- **The alert threshold is a knob** (`alerts.consecutive_cycle_failures`). If M9
  turns up a noisy upstream that fails one cycle in ten, raise it rather than
  muting the bot.
- **Backups are on the same disk.** Two weeks of shakedown data is the first thing
  in this project that would genuinely hurt to lose; an off-site copy is a
  ten-minute job whenever you want it (§5).

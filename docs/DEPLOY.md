# DEPLOY — Sentinel on a VPS

**Goal: a fresh server running Sentinel in under 30 minutes, following only this
file.** No step needs Claude Code, and nothing here assumes you remember how any
of it works. Commands are meant to be pasted in order.

The target this was written for: **Hetzner CPX32, Ubuntu, Nuremberg,
already running an unrelated application and its own Postgres.** Everything about
sharing that box is handled in §3 — Sentinel publishes no database port, binds its
only HTTP port to loopback, and runs under its own Compose project name, so it
cannot collide with the neighbour.

| § | Step | Time |
|---|---|---|
| 1 | What you need before you start | 3 min |
| 2 | Check the box | 2 min |
| 3 | Ports, names and the neighbour app | *read* |
| 4 | Get the code onto the server | 4 min |
| 5 | `.env` | 5 min |
| 6 | `config.yaml` — start in dry run | 1 min |
| 7 | First boot | 5 min |
| 8 | Verify it actually works | 4 min |
| 9 | Backups, retention, log rotation | 4 min |
| 10 | The restore drill — do it once, now | 3 min |
| 11 | Reboot: what happens, and proving it | 3 min |
| 12 | Shipping the next commit | *when needed* |
| 13 | Going live (after 24h of dry run) | *when ready* |
| 13a | Letting somebody else in | *when needed* |
| 13b | Enabling forex — the switch-on procedure | **this release** |
| 14 | Troubleshooting | *when needed* |

**§1–§8 are 24 minutes and end with a running, verified system** — that is the
milestone's "fresh VPS → running in under 30 minutes". **§9–§11 add ten minutes**
of backups, one restore drill and one reboot test; do them the same day, while the
box is still uninteresting and nothing is at stake.

> **Sentinel never trades.** There are no exchange credentials anywhere in this
> file, and there is no code path that could use one. It analyses; you trade.

---

## 1. What you need before you start

- **SSH access** to the server as a user who can run `docker` (in the `docker`
  group, or use `sudo` on every docker command below).
- **The repository.** It is private, so the server needs its own read-only deploy
  key — §4.
- **An Anthropic API key** with credit on it.
- **A Telegram bot token** (from @BotFather) and **your numeric user id** (message
  @userinfobot). You already have both from M6; they are in your laptop's `.env`.
- *Optional:* a CryptoPanic API key. Without it, news falls back to RSS and the
  snapshot is marked degraded — the system runs fine.

Budget: the box, plus roughly **$2–8/day of Anthropic spend**, capped by the guard
in §8.5.

## 2. Check the box

```bash
ssh your-server
docker version && docker compose version     # both must print a version
df -h /                                      # want >5 GB free: image ~1 GB, DB grows slowly
timedatectl | grep "Time zone"               # UTC is strongly preferred — see below
```

Everything Sentinel stores and logs is UTC, and the cron times in §9 are written
as UTC. If the box is on another timezone, either switch it
(`sudo timedatectl set-timezone UTC`) or mentally shift the cron lines.

Check that Docker starts itself at boot — this is what brings Sentinel back after
a reboot (§11):

```bash
systemctl is-enabled docker      # must say: enabled
```

## 3. Ports, names and the neighbour app

Read this before §7; it is the whole reason the compose file looks the way it does.

| What | Where it binds | Why |
|---|---|---|
| Sentinel's Postgres | **nothing published** | The app reaches it over the Compose network. Port 5432 on this host belongs to the other application, and Sentinel never asks for it. |
| Sentinel's `/health` | `127.0.0.1:18080` | Set with `SENTINEL_HTTP_PORT`. Loopback only. |
| Telegram | outbound only | Long polling. Sentinel needs **no inbound port at all** — no firewall rule, no reverse proxy, no certificate. |

**Nothing Sentinel runs is reachable from the internet.** If `/health` is wanted
from elsewhere, tunnel it over SSH (`ssh -L 18080:127.0.0.1:18080 your-server`)
rather than publishing it.

The Compose **project name is pinned to `sentinel`** in `docker-compose.yml`, so
containers are `sentinel-app-1` / `sentinel-postgres-1` and the database volume is
`sentinel_pgdata` — regardless of what the directory is called. Without that pin,
Compose names the project after the directory, and two projects that happen to
share a directory name share volumes.

If 18080 is taken on your box:

```bash
ss -ltnp | grep 18080          # empty output = free
```

pick another and set `SENTINEL_HTTP_PORT` in `.env` (§5). Do **not** change
`HEALTH_PORT`: that is the port *inside* the container.

## 4. Get the code onto the server

A read-only deploy key, so the server can pull but never push:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/sentinel_deploy -N "" -C "sentinel@$(hostname)"
cat ~/.ssh/sentinel_deploy.pub
```

Paste that public key into GitHub → the repo → **Settings → Deploy keys → Add
deploy key**. Leave "Allow write access" **unchecked**.

```bash
cat >> ~/.ssh/config <<'EOF'

Host github-sentinel
  HostName github.com
  User git
  IdentityFile ~/.ssh/sentinel_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

sudo mkdir -p /opt/sentinel && sudo chown "$USER" /opt/sentinel
git clone github-sentinel:ebadiamirhossein/sentinel.git /opt/sentinel
cd /opt/sentinel
```

Everything from here runs in `/opt/sentinel`.

## 5. `.env`

```bash
cp .env.example .env
chmod 600 .env          # it holds every secret this system has
nano .env
```

Fill in exactly these:

| Key | Value |
|---|---|
| `SENTINEL_ENV` | `prod` — switches logging to JSON |
| `POSTGRES_PASSWORD` | a long random string: `openssl rand -base64 24` |
| `ANTHROPIC_API_KEY` | your key |
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_OWNER_USER_ID` | **your numeric id.** Required — see the note below |
| `TELEGRAM_ALLOWED_USER_IDS` | your numeric id (pre-M8.1; kept as a fallback for the line above) |
| `CRYPTOPANIC_API_KEY` | optional |
| `SENTINEL_HTTP_PORT` | `18080`, or whatever §3 said was free |
| `BACKUP_DIR` | `/opt/sentinel/backups` |

Leave alone: `HEALTH_PORT` (container-internal), `POSTGRES_HOST_PORT` (used only
by the local-development overlay, which the server never loads), `DATABASE_URL`
(the app service overrides it with the Compose network address — nothing on the
server reads the value in `.env`).

```bash
mkdir -p /opt/sentinel/backups
```

> **`TELEGRAM_OWNER_USER_ID` is not optional (M8.1).** It names the OWNER row: the
> only account that can approve, reject or suspend anybody, and the only destination
> for spend and failure alerts. Migration `0007` **refuses to run** without it on a
> database that already has signals or settings to attribute, and says so by name —
> guessing an owner would hand somebody else your signals and your measured history.
> A completely fresh database migrates fine and the app seeds the row at boot.
>
> `@userinfobot` on Telegram tells you your id. If your `.env` already has exactly
> one id in `TELEGRAM_ALLOWED_USER_IDS`, that is used as a fallback and nothing
> breaks — but set the explicit variable anyway, because a second id in that list
> makes the fallback decline rather than guess.

## 6. `config.yaml` — start in dry run

> **Correction (2026-08-21, from the hygiene session) — the command below was wrong
> twice, and both are fixed in place rather than left to be copy-pasted.**
> This is a runbook, not a spec: a wrong command here is a live hazard, so the text
> is corrected and the original is quoted inside this block for the record instead of
> being left standing beneath it.
>
> 1. **It was `grep -n "^dry_run" config.yaml`.** Since M10a there is no top-level
>    `dry_run:` key — the only two live at `markets.crypto.dry_run` and
>    `markets.forex.dry_run`, both indented. The `^` anchor matched nothing and the
>    command printed no output at all, which reads exactly like "the file is fine".
> 2. **`config.yaml` is read at IMAGE BUILD TIME, not at container start.**
>    `Dockerfile` does `COPY config.yaml ./` and `docker-compose.yml` mounts no config
>    volume onto the `app` service, so the running container reads `/app/config.yaml`
>    out of the image layer. Editing the file on the host changes nothing until the
>    image is rebuilt. §7's `docker compose up -d --build` is what applies it.
>
> The same two defects hit §13 and §13b step 5, which are corrected there.

```bash
grep -n -A1 "^  crypto:" config.yaml    # markets.crypto — dry_run is indented under it
grep -n "dry_run" config.yaml           # every dry_run in the file, at any indent
```

The crypto one must say `dry_run: true` for the first 24 hours. In this mode the
full cycle runs — ingestion, screener, charts, the analyst, the risk gate, storage,
the tracker — and **publishes nothing to Telegram**. The card that would have been
sent is written to the log verbatim, the signal is stored with `dry_run=true`, and
`/stats` reports it as its own population.

A day of this produces a *measured* paper record instead of merely an absence of
crashes. §13 is how you turn it off.

**Confirm it from the running process, not from the file.** After §7's first boot,
the value the container actually loaded is printed once at startup:

```bash
docker compose logs app | grep pipeline_scheduled     # dry_run={'crypto': True}
```

"The file on disk says `true`" is not evidence about what is running — see the
correction above for why those two can differ.

While you are in the file, `watchlist:` and the `risk:` block are worth a look —
but the defaults are the ones every milestone was tested against.

## 7. First boot

```bash
cd /opt/sentinel
docker compose up -d --build
```

The first build takes 3–5 minutes on a CPX32 (it compiles nothing, but pandas,
ccxt and matplotlib are large). Then:

```bash
docker compose ps
```

Both services should reach `healthy`. The app runs `alembic upgrade head` at
start, every start, so there is no separate migration command to remember.

> Use plain `docker compose` on the server, never `make`. The Makefile targets are
> for a development laptop and add `docker-compose.dev.yml`, which publishes the
> database port — the one thing §3 forbids here.

## 8. Verify it actually works

### 8.1 Health

```bash
curl -fsS localhost:18080/health
```

`"status":"ok"` with `"database":"ok"` and `"scheduler":"running"`. A `503` with
`"database":"error"` means Postgres is not up yet — wait for its healthcheck.

`last_cycle_age_seconds` is `null` on a brand-new database: no cycle has completed
yet. The first scan runs one scan interval (60 min) after boot.

### 8.2 Logs

```bash
docker compose logs --tail=50 app
```

Expect `app.started`, `scheduler.pipeline_scheduled` and `bot.polling_started`.
`bot.disabled` means the Telegram token is missing.

`scheduler.pipeline_scheduled` is the one line that says what the process actually
loaded, so it is worth reading rather than just counting:

```
scheduler.pipeline_scheduled markets=['crypto'] job_ids=['scan:crypto', 'tracker']
  scan_interval_minutes={'crypto': 60} tracker_interval_seconds=60
  dry_run={'crypto': True}
```

`dry_run` and `job_ids` are read from the config **inside the container** — which is
not necessarily the file you last edited on the host (§6's correction). This line is
the evidence; the file is not.

### 8.3 The bot

> **Stop any other copy of Sentinel first.** A Telegram bot token allows exactly
> one long-polling client. If the same token is still running on your laptop — or
> in an old container on this box — the two fight over `getUpdates` and the logs
> fill with `TelegramConflictError`. See §14.

Message your bot `/status` from your own account. You should get the status card —
pause state, sizing, data sources, signals, cycle and LLM spend. If nothing comes
back, `TELEGRAM_OWNER_USER_ID` is not your id: `/status` is owner-only and everyone
else gets silence, by design. Try `/start` — if the bot answers with an access
*request* being filed, the id in `.env` is somebody else's.

Typing `/` should also list the commands. If the menu is empty, check the logs for
`bot.menu_failed`; a failed menu never stops the bot from working.

Set your capital if this is a fresh database — until you do, every signal is
rejected with `NO_CAPITAL`:

```
/capital 10000
```

### 8.4 The first cycle

Wait for the scan (≤60 min) or force one:

```bash
docker compose run --rm --no-deps app python -m sentinel.tools.cycle --once
```

This costs real money — roughly $0.02 for the screener alone, plus ~$0.32 per
symbol that reaches the deep analyst.

### 8.5 The spend guard (shipped in M7 — verified here, not rebuilt)

```bash
docker compose run --rm --no-deps app python -m sentinel.tools.spend
```

```
── llm spend (estimate, not a bill) ──
today         $0.97 of $10
month to date $2.31
calls today   5 (5 priced)
state         OK
```

What it guarantees: at `$7` (`llm.daily_spend_warn_usd`) you get one Telegram
notice; at `$10` (`llm.daily_spend_limit_usd`) **new deep analysis is suspended** —
the screener keeps triaging and the tracker keeps managing open positions, because
it needs no LLM at all. It clears itself at 00:00 UTC because the totals are
recomputed from `llm_calls` every cycle; there is nothing to `/resume`.

The figures are an **estimate** derived from token counts and `config.llm.pricing`,
not a bill. If some call used a model with no configured price, every figure says
"at least" instead of quietly counting it as free.

### 8.6 The alert path — prove it reaches your phone

The alerts in §14 only fire during an outage, which is the worst possible time to
discover a wrong chat id. Send one on purpose:

```bash
docker compose run --rm --no-deps app python -m sentinel.tools.alert --send
```

A message headed **🚨 Sentinel — 3 cycles failed in a row** should arrive, with a
line saying it was simulated. Nothing is written to the database, and no real
alert state is touched.

## 9. Backups, retention, log rotation

### 9.1 Take one now, by hand

```bash
./ops/backup.sh
ls -lh /opt/sentinel/backups/
```

`pg_dump -Fc` into `$BACKUP_DIR/sentinel-<UTC>.dump`. The script writes to `.part`
first and renames only on success, then reads the finished file back with
`pg_restore -l` — a dump that cannot be listed cannot be restored, and it would
rather fail now than in six months. Any failure sends you a Telegram message.

### 9.2 Cron

```bash
sudo mkdir -p /var/log/sentinel
sudo crontab -e
```

Paste the contents of `ops/crontab.example` (adjust the path if you did not clone
into `/opt/sentinel`):

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
10 3 * * *   /opt/sentinel/ops/backup.sh         >> /var/log/sentinel/backup.log 2>&1
40 3 * * 0   /opt/sentinel/ops/verify-backup.sh  >> /var/log/sentinel/backup.log 2>&1
*/15 * * * * /opt/sentinel/ops/healthcheck.sh    >> /var/log/sentinel/health.log 2>&1
```

Three jobs, and each earns its line:

- **03:10 daily — the dump.** Keeps `BACKUP_RETENTION_DAYS` (14) of history and
  always keeps the newest 3 whatever their age, because a policy that can leave a
  box with zero backups is not a policy.
- **03:40 Sunday — the proof.** `verify-backup.sh` restores the newest dump into a
  throwaway database inside the same container, checks the migration version and
  the row counts, and drops it. This is what keeps "we have backups" true.
- **every 15 min — the watchdog.** The app messages you when cycles fail; it
  cannot message you when it is not running. This curls `/health` from the host
  and speaks up after 3 consecutive failures (~45 min), then once more every 3
  further failures, plus one message when it recovers.

Cron runs as root here so it can write `/var/log/sentinel` and reach the Docker
socket. If you prefer a non-root crontab, that user must be in the `docker` group
and own the log directory.

### 9.3 Log rotation

**Container logs** are capped in `docker-compose.yml` — `max-size: 10m`,
`max-file: 5` per service, so Sentinel's logs can never exceed ~100 MB. Docker's
default is *unlimited*, and this app writes JSON every minute for ever. The cap is
per service rather than in `/etc/docker/daemon.json` on purpose: the neighbour
application's logging config is not ours to change.

**Cron logs** are host files, so they use logrotate:

```bash
sudo cp ops/logrotate.sentinel /etc/logrotate.d/sentinel
sudo logrotate -d /etc/logrotate.d/sentinel     # dry run; prints what it would do
```

Weekly, 8 kept, compressed.

Check both later with:

```bash
docker ps -q | xargs -I{} sh -c 'docker inspect --format "{{.Name}} {{.HostConfig.LogConfig.Config}}" {}'
du -sh /var/lib/docker/containers/* 2>/dev/null | sort -h | tail -5
```

## 10. The restore drill — do it once, now

A backup you have never restored is a hope, not a backup. Do this while nothing is
wrong, so the procedure is familiar when something is.

**The safe check, which is also the weekly cron job:**

```bash
./ops/verify-backup.sh
```

```
verifying /opt/sentinel/backups/sentinel-20260819T072258Z.dump into scratch database sentinel_verify_20260819072302
RESTORE OK — migration 0006_tracker_and_stats · signals=1 cycles=3 llm_calls=11
```

It restores into a scratch database, reads it, and drops it. The live database is
never written to. If it prints `RESTORE OK` with a migration version, that dump is
genuinely restorable.

**The real thing, for the day you need it:**

```bash
./ops/restore.sh --latest                 # or: ./ops/restore.sh /path/to/one.dump
```

It stops the app, drops and recreates the database, restores the dump, checks
`alembic_version`, and starts the app again. It asks you to type `restore` first,
because everything written since that dump — signals, your Taken/Skip decisions,
tracked outcomes, the audit trail — is gone.

You do not need to run the destructive version today. Run `verify-backup.sh` and
read `ops/restore.sh` once so it holds no surprises.

**Where the backups live:** on this box's disk, in `$BACKUP_DIR`. They survive a
container rebuild and a reboot; they do **not** survive losing the server. If that
matters, copy them somewhere else — `scp`, `rclone` to object storage, anything on
a schedule.

## 11. Reboot: what happens, and proving it

**What happens when the server reboots, in order:**

1. Docker starts at boot (`systemctl is-enabled docker` — §2).
2. Both containers carry `restart: unless-stopped`, so Docker starts them again
   without anyone logging in. Postgres comes up first; the app waits for its
   healthcheck.
3. The app runs `alembic upgrade head` — a no-op when the schema is current — then
   starts the API, the scheduler and the Telegram bot.
4. **The tracker's first tick runs ~60 seconds after boot. The first scan runs one
   scan interval — 60 minutes — after boot**, because the schedule is an interval
   trigger and fires one interval in, not immediately.
5. `/health` is honest immediately: `last_cycle_age_seconds` is read from the
   `cycles` table at boot, so a fresh process does not claim that nothing ever ran.
6. Everything that matters is in Postgres and survives: signals and your decisions,
   fills and exits, the pause state, the tracked outcomes, the LLM audit trail. The
   spend guard needs no state at all — it recomputes today's total from `llm_calls`.
7. The tracker replays each open signal from its `last_checked_at`, so fills and
   stop-outs during the downtime are still detected.

**Three things to know rather than discover:**

- **A container you stopped by hand stays stopped.** `unless-stopped` means exactly
  that: if you ran `docker compose stop` or `docker compose down` before the
  reboot, nothing comes back until you run `docker compose up -d`.
- **The tracker's lookback is capped at 1000 one-minute candles (~16.7 hours).** A
  server down longer than that cannot see fills older than the window; it logs
  `tracker.lookback_capped` when this happens. Check `/positions` after a long
  outage.
- **The database volume `sentinel_pgdata` survives reboots, `docker compose down`
  and image rebuilds.** It does not survive `docker compose down -v`. There is no
  reason to ever type `-v` on this box.

**Prove it now — this is the step people skip and regret:**

```bash
sudo reboot
# wait ~60s, then:
ssh your-server
cd /opt/sentinel && docker compose ps         # both Up
curl -fsS localhost:18080/health              # status ok
docker compose logs --since=5m app | head -20 # app.started, scheduler.pipeline_scheduled
```

Note that `docker kill sentinel-app-1` is **not** a valid test of any of this:
Docker treats a manual kill as a deliberate stop and does not restart the
container. A genuine crash — the process dying on its own — is restarted, which
you can see in `docker inspect sentinel-app-1 --format '{{.RestartCount}}'`.

## 12. Shipping the next commit

```bash
cd /opt/sentinel
./ops/update.sh
```

Backs up first, then `git pull --ff-only`, rebuilds the image, restarts the app,
and waits for `/health`. Downtime is one container restart — seconds. Migrations
run from the app's own entrypoint, so there is no separate step to forget.

> **Correction (2026-08-21, from the hygiene session) — the blockquote below no
> longer describes this server, and what replaced it is more dangerous than what it
> warned about.** The original text is left standing beneath, unedited, because a
> runbook that quietly rewrites itself cannot be checked against the state it
> recorded. Read this first, then it.
>
> **What was verified on the live server, 2026-08-21:**
>
> ```
> $ grep -n "markets:\|dry_run\|llm_reserved_floor_usd" config.yaml
> 21:markets:
> 23:    enabled: true          # markets.crypto
> 31:    dry_run: false         # markets.crypto
> 72:    llm_reserved_floor_usd: 8
> 86:    enabled: false         # markets.forex
> 87:    dry_run: true          # markets.forex
>
> $ git diff config.yaml
> (no output — the server file is byte-identical to the committed one)
> ```
>
> So the server's `config.yaml` **has no local edits at all**, and has had a
> `markets:` block since some earlier deploy. The collision the blockquote below
> tells you to expect **did not happen on the M10c deploy and will not happen**
> while the file stays clean. Its recovery procedure is still correct *if* somebody
> ever does edit the file on the box — it is kept for that reason, not deleted.
>
> **THE STANDING HAZARD THIS CREATES.** Because the server file is clean,
> `dry_run: false` — live mode — now lives **in git**, not as a local edit on the
> box. The old safeguard was exactly the failure the blockquote complains about:
> `git pull --ff-only` refusing to run forced somebody to read the diff before
> discarding it. **That safeguard is gone, precisely because the file is clean.**
>
> Live mode can now be flipped from a laptop by a merge, silently, and this deploy
> will succeed: `ops/update.sh` pulls, rebuilds, restarts and reports healthy.
> `Dockerfile`'s `COPY config.yaml ./` bakes the committed file straight into the
> image and `docker-compose.yml` mounts nothing over it, so there is no server-side
> copy that would win.
>
> Two things now stand in its way, and neither is a substitute for reading:
>
> * `ops/update.sh` prints `git diff <previous> HEAD -- config.yaml` after the pull
>   and before the build. **Read it.** It says "config.yaml: unchanged in this
>   update" when there is nothing to see, so silence is never ambiguous.
> * `make check` fails if the shipped `config.yaml` has `markets.crypto.dry_run`
>   true or `markets.forex.enabled` true
>   (`tests/test_config.py::test_the_shipped_config_still_carries_the_live_settings`).
>   That gates the **commit**, and only if somebody runs the gate — there is no CI
>   in this repository. It does not protect the server.
>
> **To go the other way in a hurry, use `/pause`, not a config edit.** It is a
> Telegram command, needs no deploy and no rebuild, and stops new signals while the
> tracker keeps managing anything already open.

> **`config.yaml` is tracked, and §6 and §13 edit it in place on the server.** So
> `git pull --ff-only` fails whenever a commit also changes that file — which is
> not rare: M10a, M10b-1 and M10b-2 all changed it. The symptom is
> `error: Your local changes to the following files would be overwritten by merge`,
> and the fix is to discard the server's edits and re-apply them after:
>
> ```bash
> cd /opt/sentinel
> git diff config.yaml            # READ THIS FIRST — it is your live settings
> git checkout -- config.yaml
> ./ops/update.sh
> # then re-apply whatever the diff showed, e.g. dry_run, and restart
> ```
>
> Read the diff before discarding it. On a server that has gone live it contains
> at minimum `dry_run: false` (§13), and discarding that without re-applying it
> puts the system back into rehearsal silently.

If it does not come up, the script prints the exact rollback commands and messages
you. By hand they are:

```bash
git checkout <previous-short-sha>
docker compose up -d --build
```

and, only if the new commit's migration already ran and the old code cannot read
the schema:

```bash
./ops/restore.sh --latest
```

To watch it come up: `docker compose logs -f app`.

## 13. Going live (after 24h of dry run)

> **Correction (2026-08-21, from the hygiene session) — step 3 could not have
> worked, and was never run.** Corrected in place rather than left beneath a note,
> because this is the one command in the runbook that decides whether real money is
> at stake and nobody should be able to copy-paste the broken version. What it said
> was:
>
> ```bash
> sed -i 's/^dry_run: true/dry_run: false/' config.yaml
> docker compose restart app
> ```
>
> **Both lines are wrong, independently.**
>
> 1. The `sed` is anchored to line start with `^`. Since M10a the only `dry_run`
>    keys are `markets.crypto.dry_run` and `markets.forex.dry_run`, both indented
>    four spaces. It matched nothing, changed nothing, and **exited 0**.
> 2. `docker compose restart app` restarts the existing container from the existing
>    image. `Dockerfile` does `COPY config.yaml ./` and `docker-compose.yml` mounts
>    no config volume onto `app`, so the config lives in the image layer. A host-side
>    edit reaches the running system only after a **rebuild**. Plain
>    `docker compose up -d` is also a no-op here — Compose recreates a container only
>    when its *definition* changes, and editing a host file does not change it.
>
> Had anybody run step 3 as written, it would have printed nothing, exited 0, and
> left the system in rehearsal while every subsequent check said it was fine. Going
> live on 2026-08-19 evidently happened another way — by committing `dry_run: false`
> and deploying through §12, which rebuilds. The procedure survived unexecuted, which
> is why the defect survived with it.
>
> Note the consequence for §12's hazard block: because `dry_run` is now a committed
> value, the corrected procedure below is **not** how live mode normally changes any
> more. A merge does it. Read §12.

1. Read the rehearsal record: `/stats` in Telegram. The **DRY RUN** population is
   its own set of numbers and is never merged into the real book.
2. Confirm the spend was what you expected: `python -m sentinel.tools.spend`.
3. Then, and only then — edit the file by hand, because the key is nested:

```bash
cd /opt/sentinel
$EDITOR config.yaml                 # markets: → crypto: → dry_run: false
grep -n "dry_run" config.yaml       # confirm the CRYPTO one, indented, now says false
```

4. Rebuild and recreate. A restart is not enough — the config is baked into the
   image (see the correction above):

```bash
docker compose up -d --build app
```

5. Confirm from the **running process**, not from the file. This line is emitted at
   startup by the container, out of the config it actually loaded:

```bash
docker compose logs --tail=20 app | grep pipeline_scheduled
# markets=['crypto'] job_ids=['scan:crypto', 'tracker'] dry_run={'crypto': False}
```

If that says `True`, the rebuild did not take and you are still in rehearsal —
whatever the file on disk says.

The next approved plan is posted for real. `/pause` stops new signals at any time;
the tracker keeps managing anything already open, and it is the right lever for
going quiet in a hurry — it needs no deploy and no rebuild.

## 13a. Letting somebody else in (M8.1)

Nothing here needs a deploy. The person messages your bot:

1. They send `/start`. You get a card with their @username and numeric id, and
   `[✅ Approve] [❌ Reject]` under it. **One request per id, ever** — tapping
   `/start` again reaches you no further.
2. Tap Approve (or run `/approve <id>`). They receive a welcome, the note they must
   accept — experimental, win rate not yet measured, research not advice, they place
   every trade themselves, they can lose money — and a `/` menu.
3. They tap **I understand**, then set `/capital`. Until both are done nothing is
   sent to them, and they are told which one is missing.

They now receive the same analysis you do, sized against **their** capital and risk
%, with their own rails, decisions and `/stats`. They cannot see your numbers and
you cannot see theirs: `/users` shows standing, join date, whether a capital is set
at all, and whether a loss pause is holding them — no amounts, no P&L, no decisions.

`/suspend <id>` stops delivery without deleting anything, and `/approve <id>` puts
them back. They can remove themselves at any time with `/leave`; their history is
kept and they show as LEFT.

```bash
docker compose run --rm --no-deps app python -m sentinel.tools.stats --user <id>
```

is how you would read somebody else's book from the server if you ever had to —
deliberately a shell command on the box, not a Telegram command.

## 13b. Enabling forex — the switch-on procedure

> **Rewritten 2026-08-21 (M10d), and superseding everything this section said before.**
> The old text was written when forex shipped disabled and its prerequisite was a
> `markets:` block the server did not have. Both of those are gone: the server's
> `config.yaml` has carried the block since some earlier deploy (verified on the box
> 2026-08-21), and **`markets.forex.enabled` is now committed as `true`**. What follows
> is the procedure for the deploy that turns it on, not for a future one.

**What switch-on actually is.** A merge and a rebuild. There is **no migration** — the
schema stays at `0011_forex_spine` — and no config edit on the box, because
`config.yaml` is baked into the image and the committed file already says everything.

### What must be true before you start

| | |
|---|---|
| `SAXO_APP_KEY`, `SAXO_APP_SECRET` in the server's `.env` | forex cannot read a candle without them |
| `SAXO_REFRESH_TOKEN` in the server's `.env` | seeded by the browser login in step 3 |
| `sentinel/fx/data/calendar.yaml` populated | committed, covers to **2026-10-30** |
| `TELEGRAM_OWNER_USER_ID` set | or the re-auth and calendar alerts have nowhere to go |

Nothing goes in the repo. All three Saxo values are `.env` only.

### 1. Back up, and capture the before-picture

```bash
./ops/backup.sh
```

journal/M10c_REPORT.md's "Deployed" section names the cheap fix it wished it had:
capture the surfaces **before** the restart so "nothing else moved" is a diff rather
than a memory. Send yourself `/status`, `/pulse` and `/positions` and keep the text.

**One line of `/pulse` is expected to change and only one:** `today of $10` becomes
`today of $20`. That is the deployment ceiling this release raises, and the four
goldens that carry it were regenerated deliberately. Anything else moving on a crypto
card is a defect, not a regeneration.

### 2. Merge and deploy

```bash
cd /opt/sentinel && ./ops/update.sh
```

`ops/update.sh` pulls, rebuilds and recreates. **A rebuild is required, not a restart:**
`config.yaml`, the prompts and the calendar are all `COPY`-ed into the image with
nothing mounted over them, so `docker compose restart` cannot apply any of this.

### 3. The browser login — the one manual step, and it is on your Mac

The refresh token is single-use and lives about an hour, so this is a **single sitting**:
from step 3.1 to step 3.5 you have roughly an hour before the token you copied expires.
The owner's deploys take ~30 s, so that is comfortable — but do not start it and walk
away.

1. Put `SAXO_APP_KEY` and `SAXO_APP_SECRET` in your **local** `.env` as well as the
   server's.
2. On the Mac:
   ```bash
   python -m sentinel.tools.saxo_record_fixtures --login
   ```
3. It prints an authorize URL. Open it, log in to Saxo, approve. **The browser will land
   on `https://localhost:8080/callback` and show an error page. That is correct.** There
   is no listener there on purpose — it is what keeps the deployment's zero-inbound-ports
   property. Copy the **whole address bar**.
4. Paste it back at the prompt. The tool uses an in-memory store, so running it can never
   consume the server's live credential.
5. Put the refresh token it prints into the **server's** `.env` as
   `SAXO_REFRESH_TOKEN=…`, then:
   ```bash
   docker compose up -d --build app
   ```

### 4. Confirm from the running process, not from the file

```bash
docker compose logs --tail=50 app | grep -E "pipeline_scheduled|auth_bootstrapped|auth_refreshed|calendar_loaded"
```

Expect, in order:

- `forex.calendar_loaded … events=11 coverage_until=2026-10-30 known_gaps=1`
- `forex.auth_bootstrapped` — the `.env` token reached Postgres. **`forex.auth_bootstrap_skipped`
  is also correct** and means a credential was already stored; the stored one always wins,
  because the `.env` one was spent on the first refresh.
- `scheduler.pipeline_scheduled … markets=['crypto', 'forex'] job_ids=['scan:crypto',
  'scan:forex', 'tracker', 'tracker:forex', 'forex-token-refresh']` — **five** ids. Crypto's
  tracker keeps the bare `tracker` it has had since M7.
- within five minutes: `forex.auth_refreshed … refresh_rotated=True`. If this never appears,
  the credential chain is not being renewed and forex will die within the hour.

Then wait for the top of the next hour inside **07:00–19:00 UTC** and expect a forex card,
or a `cycle.forex_condition_blocked` / `cycle.outside_scan_hours` line saying why not.

### 5. If forex misbehaves — which lever stops it

In order of speed. **The first one is almost always the right one.**

| lever | stops | reaches the running system by |
|---|---|---|
| `/pause forex` | forex gating, publishing **and spending**; crypto untouched | the next forex cycle. No deploy, no restart. |
| `/resume forex` | lifts it | same |
| `markets.forex.enabled: false` | everything forex, including its three jobs | **`up -d --build` only** — the config is baked into the image |
| `/pause` *(no argument)* | **every market, including crypto's live measurement** | the next cycle |

**Do not reach for the bare `/pause` during this window.** It ends the crypto sample it
was not aimed at, and the crypto measurement is the thing this deployment exists to
produce. `/pause forex` is the switch you want, and since M10d it stops forex *spending*
as well as publishing — before that it cost ~$0.73 a cycle to be paused.

### 6. The window has an end date

This is a **two-week observation window, reviewed 2026-09-04** — one day after crypto's
measurement review, so both are read together. The spend rails were raised for it and
the revert values are written beside every raised number in `config.yaml` and asserted
in `tests/test_config.py::test_the_observation_windows_rails_are_the_committed_ones`.

The calendar runs out on **2026-10-30**. Sentinel will send a daily nudge for the
fortnight before that; when it does, extend `sentinel/fx/data/calendar.yaml` and rebuild.
It also repeats two outstanding jobs until they are done: verifying the two ECB dates
against the ECB's own page, and adding US PCE from bea.gov.


## 14. Troubleshooting

**`/health` returns 503, `"database":"error"`**
Postgres is down or unhealthy. `docker compose ps`, then
`docker compose logs --tail=50 postgres`. The app stays up and keeps answering —
that is deliberate, so the probe can tell you *which* part is broken.

**Nothing arrives in Telegram**
`docker compose logs app | grep -E "bot\.|telegram"`. `bot.disabled` = no token.
Silence with no error = the gate dropped the update. For your own account that
means `TELEGRAM_OWNER_USER_ID` is not your id; for somebody else's it means they are
not APPROVED, have not tapped **I understand**, or have no `/capital` set (they are
told which, at most once a day). Remember that `dry_run: true` is *supposed* to
produce a silent phone.

**`TelegramConflictError: terminated by other getUpdates request`**
Two processes are polling the same bot token. Telegram allows one. Almost always
the development stack on your own machine, left running — the deploy does not stop
it for you, and it will keep stealing updates until it does.

```bash
# on the OTHER machine, not the server:
docker compose stop app
```

The server's bot recovers on its own within a minute or so; aiogram retries with
backoff and logs `Connection established` when it wins. Nothing is lost — updates
that arrived during the fight were destined for whichever poller won, and the bot
drops queued updates at startup by design anyway. If it persists after stopping
every copy you know about, `curl -s "https://api.telegram.org/bot<token>/getWebhookInfo"`
will show a webhook still registered against the token.

**"🚨 Sentinel — N cycles failed in a row"**
The scan cycle failed N times consecutively. The tracker is unaffected and open
positions are still watched. `docker compose logs --tail=200 app | grep cycle.failed`.
Most common causes: an expired Anthropic key, no credit, or an upstream outage.
The alert repeats every 3 further failures and sends one message when it recovers.

**"🚨 Sentinel is DOWN"** (from the host watchdog)
The stack is not answering at all. `docker compose ps`; if the app is restarting in
a loop, `docker compose logs --tail=100 app` will show why — usually a bad `.env`
value or a migration that cannot apply.

**Cycles run but nothing is ever approved**
Normal. The gate rejects most plans. `/status` shows the pause state and today's
count; the reasons are in the logs as `cycle.not_approved` with a reason code.

**Disk filling up**
`docker system df`. Old images from previous builds are the usual cause:
`docker image prune -f`. Container logs are capped at ~100 MB total (§9.3).

**A command needs the database from the shell**

```bash
docker compose run --rm --no-deps app python -m sentinel.tools.stats
docker compose exec postgres psql -U sentinel -d sentinel
```

There is no host-published database port on this server, by design — go through
the container.

---

*Sentinel is a personal research tool. It produces analysis and never orders. Not
financial advice.*

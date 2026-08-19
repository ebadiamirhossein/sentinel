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
| `TELEGRAM_ALLOWED_USER_IDS` | your numeric id — everyone else gets silence |
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

## 6. `config.yaml` — start in dry run

```bash
grep -n "^dry_run" config.yaml
```

It must say `dry_run: true` for the first 24 hours. In this mode the full cycle
runs — ingestion, screener, charts, the analyst, the risk gate, storage, the
tracker — and **publishes nothing to Telegram**. The card that would have been
sent is written to the log verbatim, the signal is stored with `dry_run=true`, and
`/stats` reports it as its own population.

A day of this produces a *measured* paper record instead of merely an absence of
crashes. §13 is how you turn it off.

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
yet. The first scan runs one scan interval (15 min) after boot.

### 8.2 Logs

```bash
docker compose logs --tail=50 app
```

Expect `app.started`, `scheduler.pipeline_scheduled` (with `dry_run=true`) and
`bot.polling_started`. `bot.disabled` means the Telegram token is missing.

### 8.3 The bot

Message your bot `/status` from the allowlisted account. You should get the status
card — pause state, sizing, data sources, signals, cycle and LLM spend. If nothing
comes back, the id in `TELEGRAM_ALLOWED_USER_IDS` is not yours; the allowlist
answers strangers with silence, by design.

Set your capital if this is a fresh database — until you do, every signal is
rejected with `NO_CAPITAL`:

```
/capital 10000
```

### 8.4 The first cycle

Wait for the scan (≤15 min) or force one:

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
   scan interval — 15 minutes — after boot**, because the schedule is an interval
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

1. Read the rehearsal record: `/stats` in Telegram. The **DRY RUN** population is
   its own set of numbers and is never merged into the real book.
2. Confirm the spend was what you expected: `python -m sentinel.tools.spend`.
3. Then, and only then:

```bash
cd /opt/sentinel
sed -i 's/^dry_run: true/dry_run: false/' config.yaml
docker compose restart app
docker compose logs --tail=20 app | grep pipeline_scheduled   # dry_run=False
```

The next approved plan is posted for real. `/pause` stops new signals at any time;
the tracker keeps managing anything already open.

## 14. Troubleshooting

**`/health` returns 503, `"database":"error"`**
Postgres is down or unhealthy. `docker compose ps`, then
`docker compose logs --tail=50 postgres`. The app stays up and keeps answering —
that is deliberate, so the probe can tell you *which* part is broken.

**Nothing arrives in Telegram**
`docker compose logs app | grep -E "bot\.|telegram"`. `bot.disabled` = no token.
Silence with no error = your user id is not in `TELEGRAM_ALLOWED_USER_IDS`.
Remember that `dry_run: true` is *supposed* to produce a silent phone.

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

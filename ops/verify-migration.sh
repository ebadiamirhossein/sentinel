#!/usr/bin/env bash
# Prove a migration is safe against a POPULATED database — up, down, and up again.
#
#   ops/verify-migration.sh                      # the newest dump
#   ops/verify-migration.sh /path/to/one.dump
#
# M10a. `alembic upgrade head` on an empty scratch database proves the SQL parses.
# It does not prove the thing that matters: that a database holding a year of
# signals, decisions and settings survives the change, that the backfill actually
# reached every row, and that the downgrade path is real rather than aspirational.
# This restores a real dump and asserts all three.
#
# It is safe to run at any time. Everything happens in a throwaway database inside
# the same Postgres container, which is created here and dropped on exit.
#
# THE GUARD, and why it is not a comment asking for care. During M6's verification
# the test suite was pointed at a database that held a delivered signal and deleted
# the row the card's buttons referred to (journal/M6_REPORT.md; tests/db_guard.py is
# the fix, one layer in). This script runs `alembic downgrade`, which DROPS COLUMNS.
# Pointed at the live database it would destroy the market dimension and, on the way
# back up, silently re-backfill it — losing nothing today and everything the day
# forex has rows. So it refuses, by name, before it touches anything.
#
# A SECOND HAZARD, and the reason this script runs NO mutating compose command.
#
# `ops/lib.sh` invokes `docker compose -f docker-compose.yml` only — correct on the
# server, where docker-compose.dev.yml must never be picked up because it publishes
# 5432 on a host whose 5432 belongs to a neighbour application. This was the one ops
# script that ran `compose run app`, and `run` starts the service's `depends_on`:
# Compose compared the running postgres container against the definition in the
# narrower file set, found the published port absent from it, and RECREATED the
# container without it. Postgres kept running and kept reporting healthy while
# becoming unreachable from the host, so every database test failed with
# "Connect call failed ('127.0.0.1', 5432)" — and this script printed MIGRATION OK.
#
# Silence-as-success, in the script whose whole job is to be trusted about a
# database. It cost about an hour of debugging, twice, before it was found.
#
# The fix is not a cleverer set of compose files — an attempt at that made it worse,
# because a `while read` loop silently dropped the last filename and produced exactly
# the narrow invocation it was written to prevent. The fix is that **nothing here
# runs a compose command that can create, recreate or stop a container**:
#
#   * `compose exec` and `compose ps` only. Both are read-only with respect to
#     container definitions; neither can recreate anything.
#   * the app image is built with plain `docker build`, and alembic runs under plain
#     `docker run` attached to the project's existing network. Neither command knows
#     what a compose service is, so neither can reconcile one.
#   * credentials still never reach a process listing. Docker's `--env-file` is NOT
#     Compose's: it does no comment stripping, so handing it `.env` directly sets
#     SENTINEL_ENV to "dev   # dev → text logs …" and the app refuses to start. So a
#     minimal env file holding only the two Postgres variables is written to a
#     mode-600 temp file, parsed with `ops/lib.sh`'s comment-aware `env_value`, and
#     removed on exit. DATABASE_URL is still assembled inside the container.
#
# A belt-and-braces check remains: the published ports are recorded before anything
# runs and compared afterwards — on success, on failure and on Ctrl-C — so if some
# future edit reintroduces the hazard, this says so instead of printing OK.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

# What postgres publishes right now, as a stable string. Empty when nothing runs.
# `ps` is read-only, so this cannot itself disturb anything.
published_ports() {
  local container
  container="$(compose ps -q postgres 2>/dev/null | head -n 1)"
  [[ -n "$container" ]] || return 0
  docker inspect "$container" --format '{{json .NetworkSettings.Ports}}' 2>/dev/null || true
}

# The Docker network the compose project put postgres on, so a plain `docker run`
# can reach it by service name without compose being involved at all.
project_network() {
  local container
  container="$(compose ps -q postgres 2>/dev/null | head -n 1)"
  [[ -n "$container" ]] || die "postgres is not running — start the stack first"
  docker inspect "$container" \
    --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' |
    head -n 1
}

#: The revision this milestone adds, and the one below it.
# M10b-1 moves the target to 0011 and deliberately leaves PREVIOUS at 0009, so the
# round trip runs BOTH downgrades and BOTH upgrades. 0010's assertions all describe
# state at head and stay valid: 0011 adds two empty tables and relaxes one column,
# and touches nothing 0010 wrote.
#
# M11p moves the target to 0012 on the same principle and leaves PREVIOUS at 0009,
# so the round trip now spans three upgrades and three downgrades. 0012 adds ONE
# empty table and touches nothing any earlier revision wrote, so every assertion
# below stays valid unchanged; the only new one is assert_persian_table_absent.
TARGET_REVISION="0012_persian_summaries"
PREVIOUS_REVISION="0009_watchlist_requests"

#: Every table migration 0010 adds ``market`` to.
MARKET_TABLES=(
  market_snapshots ohlcv_candles instrument_meta ingestion_failures llm_calls
  analyst_reports gate_decisions signals watchlist_requests cycles
)

#: Row counts compared before and after. The audit trail, the outcome record and
#: the money — if a migration lost a row, it lost one of these.
COUNTED_TABLES=(signals cycles llm_calls gate_decisions analyst_reports)

BACKUP_DIR="$(backup_dir)"

#: Postgres' published ports as they were before this script ran. Set in `main`,
#: compared on every exit path. Empty is a valid value (the server publishes none).
PORTS_BEFORE=""

#: The image this script builds and runs alembic from. Deliberately not the tag the
#: deployment uses, so a half-finished verification build can never be what restarts.
IMAGE="sentinel:migration-check"

#: The compose project's network, resolved at run time in `main`.
NETWORK=""

#: A mode-600 file holding only POSTGRES_USER and POSTGRES_PASSWORD, for
#: `docker run --env-file`. Written in `main`, removed by the trap. The password
#: therefore never appears in argv, in `ps`, or in this script's own environment —
#: the standing rule in ops/lib.sh's header.
ENV_FILE=""
#: ``${VAR-default}``, not ``${VAR:-default}``: an explicitly empty
#: SENTINEL_MIGRATION_SCRATCH must reach the guard below and be refused, rather
#: than being silently replaced by the default. A guard that cannot be reached is
#: not a guard, and "" is exactly what an unset variable in a wrapper script
#: expands to.
SCRATCH="${SENTINEL_MIGRATION_SCRATCH-sentinel_migration_check}"

fail() {
  log "ERROR: $*"
  exit 1
}

cleanup() {
  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists --force "$SCRATCH"' >/dev/null 2>&1 || true
  [[ -z "$ENV_FILE" ]] || rm -f "$ENV_FILE"
  assert_ports_unchanged
}

# The belt-and-braces check. Runs on every exit path — success, failure and Ctrl-C —
# because the state it protects is the developer's machine, and the run that breaks
# it is exactly the run that did not finish tidily.
#
# It **reports and fails; it does not repair.** Repairing would mean running
# `compose up`, which is the very command that caused the damage this exists to
# catch: with the wrong file set it recreates postgres without its published port.
# A guard whose remedy can reproduce the fault is not a guard. So this says what
# changed and what to type, and turns a green run red — which is the whole point,
# since the original defect was a green run that concealed a broken machine.
assert_ports_unchanged() {
  local now
  now="$(published_ports)"
  [[ "$now" == "$PORTS_BEFORE" ]] && return 0

  log "ERROR: postgres' published ports changed during this run."
  log "  before: ${PORTS_BEFORE:-<none>}"
  log "  after:  ${now:-<none>}"
  log ""
  log "  Something in this script recreated the container — which it is written not"
  log "  to do (see the note at the top of this file). The database may now be"
  log "  unreachable from the host, and every DB test will fail with"
  log "  \"Connect call failed ('127.0.0.1', 5432)\"."
  log ""
  log "  Restore it with:    make up"
  log ""
  log "  This run's migration result above is NOT trustworthy — re-run it after."
  exit 1
}

psql_scratch() {
  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'exec psql -U "$POSTGRES_USER" -d "$SCRATCH" -At -c "$0"' "$1" | tr -d '\r'
}

# `alembic <args>` against the SCRATCH database. DATABASE_URL is rebuilt inside the
# container from the credentials already in its environment, so the password never
# reaches a host process listing — ops/lib.sh's standing rule.
# Writes the minimal env file `docker run` needs. See ENV_FILE above.
write_env_file() {
  ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/sentinel-migration-env.XXXXXX")"
  chmod 600 "$ENV_FILE"
  {
    printf 'POSTGRES_USER=%s\n' "$(env_value POSTGRES_USER sentinel)"
    printf 'POSTGRES_PASSWORD=%s\n' "$(env_value POSTGRES_PASSWORD)"
  } >"$ENV_FILE"
  [[ -s "$ENV_FILE" ]] || fail "could not read the Postgres credentials from .env"
}

alembic_scratch() {
  docker run --rm \
    --env-file "$ENV_FILE" \
    --network "$NETWORK" \
    -e SCRATCH="$SCRATCH" \
    --entrypoint sh \
    "$IMAGE" -c \
    'export DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER:-sentinel}:${POSTGRES_PASSWORD}@postgres:5432/${SCRATCH}"
     exec alembic "$@"' -- "$@"
}

# ---------------------------------------------------------------------------- #
# The guard
# ---------------------------------------------------------------------------- #

refuse_the_live_database() {
  local live_db live_url_db
  live_db="$(db_name)"

  [[ -n "$SCRATCH" ]] || fail "SENTINEL_MIGRATION_SCRATCH is empty — refusing to guess a database name"

  if [[ "$SCRATCH" == "$live_db" ]]; then
    fail "SENTINEL_MIGRATION_SCRATCH is '$SCRATCH', which is the database this
  deployment runs on (POSTGRES_DB). This script runs 'alembic downgrade', which
  DROPS COLUMNS. Refusing. Use a scratch name, e.g. sentinel_migration_check."
  fi

  # The DSN in .env is what the app itself opens. Compare the database component
  # after stripping credentials, exactly as tests/db_guard.py does: a guard fooled
  # by a password is not a guard.
  live_url_db="$(env_value DATABASE_URL | sed -E 's#^.*/##; s#\?.*$##')"
  if [[ -n "$live_url_db" && "$SCRATCH" == "$live_url_db" ]]; then
    fail "SENTINEL_MIGRATION_SCRATCH is '$SCRATCH', which is the database named in
  DATABASE_URL. Refusing — see the note at the top of this script."
  fi
}

# ---------------------------------------------------------------------------- #

assert_backfilled() {
  local table bad
  for table in "${MARKET_TABLES[@]}"; do
    bad="$(psql_scratch "SELECT count(*) FROM ${table} WHERE market IS NULL OR market <> 'crypto'")"
    [[ "$bad" == "0" ]] ||
      fail "$table has $bad row(s) that are not 'crypto' after the upgrade — the backfill did not reach them"
  done
  log "backfill OK — every row in ${#MARKET_TABLES[@]} tables is 'crypto'"
}

#: 0011's tables. Additive and empty by construction, which is what makes the
#: round trip below clean.
FOREX_TABLES=(forex_instruments saxo_oauth_tokens)

#: M11p's one table. Listed separately from FOREX_TABLES because it belongs to a
#: different revision, and a downgrade that stopped at 0011 must still drop it.
PERSIAN_TABLES=(persian_summaries)

assert_volume_relaxed() {
  # Migration 0011, and the owner's requirement R-a: the column becomes nullable and
  # NOTHING ELSE HAPPENS. No backfill, no rewrite, and above all no crypto row that
  # quietly loses its volume — "nullable" becoming "sometimes missing" for crypto
  # would disable relative volume with nothing to notice it by.
  local nullable nulls table
  nullable="$(psql_scratch "SELECT is_nullable FROM information_schema.columns
                            WHERE table_name = 'ohlcv_candles' AND column_name = 'volume'" | tr -d '[:space:]')"
  [[ "$nullable" == "YES" ]] ||
    fail "ohlcv_candles.volume is still NOT NULL after the upgrade — forex has no volume and needs somewhere to say so"

  nulls="$(psql_scratch "SELECT count(*) FROM ohlcv_candles WHERE market = 'crypto' AND volume IS NULL")"
  [[ "$nulls" == "0" ]] ||
    fail "$nulls crypto candle(s) acquired a NULL volume — 0011 must relax the column and touch no row"

  for table in "${FOREX_TABLES[@]}"; do
    [[ "$(psql_scratch "SELECT count(*) FROM information_schema.tables WHERE table_name = '${table}'")" == "1" ]] ||
      fail "0011 did not create $table"
    [[ "$(psql_scratch "SELECT count(*) FROM ${table}")" == "0" ]] ||
      fail "$table is not empty after the upgrade — it should be created and left alone"
  done
  log "0011 OK — volume is nullable, no crypto row lost one, ${FOREX_TABLES[*]} created empty"
}

assert_forex_tables_absent() {
  local table
  for table in "${FOREX_TABLES[@]}"; do
    [[ "$(psql_scratch "SELECT count(*) FROM information_schema.tables WHERE table_name = '${table}'")" == "0" ]] ||
      fail "downgrade left $table behind — the 0011 downgrade is not real"
  done
  [[ "$(psql_scratch "SELECT is_nullable FROM information_schema.columns
                      WHERE table_name = 'ohlcv_candles' AND column_name = 'volume'" | tr -d '[:space:]')" == "NO" ]] ||
    fail "downgrade left ohlcv_candles.volume nullable — the 0011 downgrade is not real"
}

assert_persian_table_absent() {
  # M11p. The whole 0012 downgrade is one DROP TABLE, which is exactly the kind of
  # migration nobody bothers to test — and this deployment runs `alembic upgrade head`
  # at every container start, so a downgrade that does not work is only discovered on
  # the day somebody needs to roll back at speed.
  local table
  for table in "${PERSIAN_TABLES[@]}"; do
    [[ "$(psql_scratch "SELECT count(*) FROM information_schema.tables WHERE table_name = '${table}'")" == "0" ]] ||
      fail "downgrade left $table behind — the 0012 downgrade is not real"
  done
}

assert_signals_untouched_by_0012() {
  # M11p's binding constraint, asserted here as well as in the suite: 0012 may add a
  # table and NOTHING else. `signals` is read by /journal, /stats and the three
  # populations, and two live measurement windows depend on its shape not moving.
  local columns
  columns="$(psql_scratch "SELECT count(*) FROM information_schema.columns WHERE table_name = 'signals'")"
  [[ "$(psql_scratch "SELECT count(*) FROM information_schema.table_constraints
                      WHERE table_name = 'signals' AND constraint_type = 'FOREIGN KEY'")" == "0" ]] ||
    fail "signals gained a foreign key — 0012 must add no dependency edge into it"
  log "signals: $columns columns, no foreign keys"
}

assert_column_absent() {
  local present
  present="$(psql_scratch "SELECT count(*) FROM information_schema.columns
                           WHERE table_name = 'signals' AND column_name = 'market'")"
  [[ "$present" == "0" ]] || fail "downgrade left signals.market behind — the downgrade is not real"
}

counts_now() {
  local table out=""
  for table in "${COUNTED_TABLES[@]}"; do
    out+="${table}=$(psql_scratch "SELECT count(*) FROM ${table}") "
  done
  printf '%s' "$out"
}

revision_now() {
  psql_scratch 'SELECT version_num FROM alembic_version' | tr -d '[:space:]'
}

# Seeds the row production does not have, and takes it through 0010 both ways.
verify_watchlist_rename() {
  local value='["BTCUSDT", "SOLUSDT"]'

  alembic_scratch downgrade "$PREVIOUS_REVISION" >/dev/null 2>&1 ||
    fail "could not step back to $PREVIOUS_REVISION to seed the rename check"

  psql_scratch "DELETE FROM runtime_settings WHERE key LIKE 'watchlist%';
                DELETE FROM config_changes WHERE key LIKE 'watchlist%';
                INSERT INTO runtime_settings (key, value, updated_at, updated_by_user_id)
                VALUES ('watchlist', '${value}'::jsonb, now(), 7222549221);
                INSERT INTO config_changes (key, old_value, new_value, actor_user_id, changed_at)
                VALUES ('watchlist', NULL, '${value}'::jsonb, 7222549221, now());" >/dev/null ||
    fail "could not seed a watchlist row"

  alembic_scratch upgrade head >/dev/null 2>&1 || fail "the upgrade failed with a watchlist row present"

  local renamed old_key changes
  renamed="$(psql_scratch "SELECT value::text FROM runtime_settings WHERE key = 'watchlist:crypto'")"
  old_key="$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key = 'watchlist'")"
  changes="$(psql_scratch "SELECT count(*) FROM config_changes WHERE key = 'watchlist:crypto'")"

  [[ -n "$renamed" ]] || fail "upgrade: the watchlist row was not renamed to watchlist:crypto"
  [[ "$old_key" == "0" ]] || fail "upgrade: the old 'watchlist' key survived the rename"
  # The value has to travel unchanged. A rename that dropped or rewrote the symbols
  # would leave the owner screening a different watchlist after a deploy, silently.
  [[ "$renamed" == *"BTCUSDT"* && "$renamed" == *"SOLUSDT"* ]] ||
    fail "upgrade: the watchlist value did not survive the rename (got: $renamed)"
  [[ "$changes" != "0" ]] || fail "upgrade: config_changes history was not renamed with it"

  alembic_scratch downgrade "$PREVIOUS_REVISION" >/dev/null 2>&1 ||
    fail "the downgrade failed with a renamed watchlist row present"

  local restored new_key
  restored="$(psql_scratch "SELECT value::text FROM runtime_settings WHERE key = 'watchlist'")"
  new_key="$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key = 'watchlist:crypto'")"

  [[ -n "$restored" ]] || fail "downgrade: the watchlist key was not restored"
  [[ "$new_key" == "0" ]] || fail "downgrade: 'watchlist:crypto' survived the downgrade"
  [[ "$restored" == *"BTCUSDT"* && "$restored" == *"SOLUSDT"* ]] ||
    fail "downgrade: the watchlist value did not survive (got: $restored)"

  alembic_scratch upgrade head >/dev/null 2>&1 || fail "could not return to $TARGET_REVISION"
  log "watchlist rename OK — renamed with its value and its config_changes history, and reversed"
}

main() {
  require_stack
  refuse_the_live_database

  local dump="${1-}"
  if [[ -z "$dump" ]]; then
    dump="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sentinel-*.dump' | sort -r | head -n 1)"
    [[ -n "$dump" ]] || fail "no dumps in $BACKUP_DIR — run ops/backup.sh first"
  fi
  [[ -f "$dump" ]] || fail "no such dump: $dump"

  PORTS_BEFORE="$(published_ports)"
  NETWORK="$(project_network)"
  log "verifying $TARGET_REVISION against $dump, in scratch database $SCRATCH"
  log "network: $NETWORK · image: $IMAGE · no compose container is created or recreated"
  trap cleanup EXIT INT TERM
  # Drop a scratch database left behind by an interrupted earlier run, THEN write the
  # env file — `cleanup` removes that file, so writing it first would delete it here.
  cleanup
  write_env_file

  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'createdb -U "$POSTGRES_USER" "$SCRATCH"' || fail "could not create the scratch database"

  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'exec pg_restore -U "$POSTGRES_USER" -d "$SCRATCH" --no-owner --no-privileges' \
    <"$dump" >/dev/null 2>&1 ||
    log "pg_restore reported warnings — the checks below are what decide"

  local before after started_at watchlist_before
  started_at="$(revision_now)"
  [[ -n "$started_at" ]] || fail "the restored database has no alembic_version row — that is not a Sentinel dump"
  before="$(counts_now)"
  watchlist_before="$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key = 'watchlist'")"
  log "restored at $started_at · $before"

  # The image has to carry the migration being tested. Building here rather than
  # assuming: a stale image would "pass" by running the previous head.
  log "step 1/6 — building the app image so it carries $TARGET_REVISION"
  # `docker build`, not `compose build`: a plain build cannot touch the running
  # stack, and this script must not. Tagged distinctly so it can never be confused
  # with the image the deployment is running.
  docker build -q -t "$IMAGE" "$SENTINEL_ROOT" >/dev/null || fail "the image did not build"

  log "step 2/6 — upgrade head"
  alembic_scratch upgrade head || fail "the upgrade failed"
  [[ "$(revision_now)" == "$TARGET_REVISION" ]] || fail "expected $TARGET_REVISION after the upgrade, found $(revision_now)"

  assert_backfilled
  assert_volume_relaxed
  assert_signals_untouched_by_0012
  after="$(counts_now)"
  [[ "$before" == "$after" ]] || fail "row counts changed across the upgrade:
  before: $before
  after:  $after"
  log "row counts unchanged — $after"

  # Case A — the shape production actually has. The live `runtime_settings` holds
  # only `capital_eur`; the watchlist comes from config.yaml, so 0010's rename is a
  # no-op there. "No-op" is a claim about behaviour and gets asserted rather than
  # logged: the failure it guards against is an UPDATE that errors, or a downgrade
  # that resurrects a key nobody ever set.
  if [[ "$watchlist_before" == "0" ]]; then
    [[ "$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key LIKE 'watchlist%'")" == "0" ]] ||
      fail "the upgrade invented a watchlist row where the dump had none"
    log "no stored watchlist row (production's shape) — upgrade clean, nothing renamed"
  else
    [[ "$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key = 'watchlist:crypto'")" != "0" ]] ||
      fail "the stored watchlist was not renamed to watchlist:crypto"
    [[ "$(psql_scratch "SELECT count(*) FROM runtime_settings WHERE key = 'watchlist'")" == "0" ]] ||
      fail "the old 'watchlist' key is still present after the rename"
    log "runtime_settings watchlist renamed to watchlist:crypto"
  fi

  log "step 3/6 — downgrade to $PREVIOUS_REVISION"
  alembic_scratch downgrade "$PREVIOUS_REVISION" || fail "the downgrade failed"
  [[ "$(revision_now)" == "$PREVIOUS_REVISION" ]] || fail "expected $PREVIOUS_REVISION after the downgrade, found $(revision_now)"
  assert_column_absent
  assert_forex_tables_absent
  assert_persian_table_absent

  after="$(counts_now)"
  [[ "$before" == "$after" ]] || fail "row counts changed across the downgrade:
  before: $before
  after:  $after"
  log "downgrade OK — columns dropped, $after"

  log "step 4/6 — upgrade head again"
  alembic_scratch upgrade head || fail "the second upgrade failed"
  [[ "$(revision_now)" == "$TARGET_REVISION" ]] || fail "expected $TARGET_REVISION after the second upgrade"
  assert_backfilled
  assert_volume_relaxed
  assert_signals_untouched_by_0012

  after="$(counts_now)"
  [[ "$before" == "$after" ]] || fail "row counts changed across the round trip:
  before: $before
  after:  $after"

  # Case B — the rename itself, which no real dump can exercise (see Case A). Seeded
  # deliberately, because a migration step that production never runs is a migration
  # step nobody has ever seen work, and it becomes load-bearing the moment somebody
  # types /watchlist add on the server.
  log "step 5/6 — the watchlist rename, against a seeded row"
  verify_watchlist_rename

  log "step 6/6 — dropping $SCRATCH"
  log "MIGRATION OK — $started_at → $TARGET_REVISION → $PREVIOUS_REVISION → $TARGET_REVISION, $after"
}

main "$@"

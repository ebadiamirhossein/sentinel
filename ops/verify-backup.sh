#!/usr/bin/env bash
# Restore a dump into a throwaway database and check that it is really there.
#
#   ops/verify-backup.sh                      # the newest dump
#   ops/verify-backup.sh /path/to/one.dump
#
# "A backup nobody has restored is not a backup." This is the part that makes the
# claim true, and it is safe to run at any time: it restores into a scratch
# database inside the same Postgres container, reads it, and drops it. The live
# database is never opened for writing.
#
# What it asserts, and why those three:
#   * alembic_version — a dump that restores but carries no schema version is not
#     a Sentinel database, it is an empty shell with the right name;
#   * row counts on signals / cycles / llm_calls — the audit trail, the outcome
#     record and the money. If those survived, the restore is worth having;
#   * a non-empty signals table is only *reported*, never required: a fresh
#     deployment legitimately has none, and a check that failed on a new server
#     would be turned off within a week.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

BACKUP_DIR="$(backup_dir)"
SCRATCH="sentinel_verify_$(date -u +%Y%m%d%H%M%S)"

cleanup() {
  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists "$SCRATCH"' >/dev/null 2>&1 || true
}

fail() {
  log "ERROR: $*"
  notify "🚨 Sentinel backup VERIFICATION failed on $(hostname): $*"
  exit 1
}

psql_scratch() {
  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'exec psql -U "$POSTGRES_USER" -d "$SCRATCH" -At -c "$0"' "$1"
}

main() {
  require_stack
  local dump="${1-}"
  if [[ -z "$dump" ]]; then
    dump="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sentinel-*.dump' | sort -r | head -n 1)"
    [[ -n "$dump" ]] || fail "no dumps in $BACKUP_DIR — run ops/backup.sh first"
  fi
  [[ -f "$dump" ]] || fail "no such dump: $dump"

  log "verifying $dump into scratch database $SCRATCH"
  trap cleanup EXIT

  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'createdb -U "$POSTGRES_USER" "$SCRATCH"' || fail "could not create the scratch database"

  # --no-owner: the dump's owner is whatever role the live database uses, and the
  # restore must not depend on that role existing under a different name later.
  compose exec -T -e SCRATCH="$SCRATCH" postgres sh -c \
    'exec pg_restore -U "$POSTGRES_USER" -d "$SCRATCH" --no-owner --no-privileges' \
    <"$dump" >/dev/null 2>&1 ||
    log "pg_restore reported warnings — the checks below are what decide"

  local version signals cycles calls
  version="$(psql_scratch 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')" ||
    fail "alembic_version is not readable in the restored database"
  [[ -n "$version" ]] || fail "the restored database has no alembic_version row"

  signals="$(psql_scratch 'SELECT count(*) FROM signals' | tr -d '[:space:]')"
  cycles="$(psql_scratch 'SELECT count(*) FROM cycles' | tr -d '[:space:]')"
  calls="$(psql_scratch 'SELECT count(*) FROM llm_calls' | tr -d '[:space:]')"

  log "RESTORE OK — migration $version · signals=$signals cycles=$cycles llm_calls=$calls"
  if [[ "$signals" == "0" ]]; then
    log "note: no signals in this dump. Expected on a fresh deployment; suspicious on an old one."
  fi
}

main "$@"

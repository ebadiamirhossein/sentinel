#!/usr/bin/env bash
# Restore a dump over the LIVE database — docs/DEPLOY.md §7.3.
#
#   ops/restore.sh --latest
#   ops/restore.sh /opt/sentinel/backups/sentinel-20260819T031000Z.dump
#
# This is destructive and says so: it stops the app, drops the database, recreates
# it from the dump, and starts the app again. Everything written since the dump is
# gone — signals, decisions, tracked outcomes, the LLM audit trail.
#
# To CHECK a backup without touching anything, use ops/verify-backup.sh instead.
# That is the one to run on a schedule; this one is for the day something is
# actually wrong.
#
# The app is stopped first on purpose. A restore under a live app would race the
# tracker's next tick, and Postgres will not drop a database that has connections
# anyway.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

BACKUP_DIR="$(backup_dir)"

usage() {
  cat >&2 <<'USAGE'
usage: ops/restore.sh (--latest | <dump file>) [--yes]

  --latest   use the newest dump in $BACKUP_DIR
  --yes      skip the confirmation prompt (for a documented, deliberate run)
USAGE
  exit 2
}

main() {
  require_stack
  local dump="" assume_yes=0 argument
  for argument in "$@"; do
    case "$argument" in
      --latest) dump="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sentinel-*.dump' | sort -r | head -n 1)" ;;
      --yes) assume_yes=1 ;;
      -h | --help) usage ;;
      *) dump="$argument" ;;
    esac
  done
  [[ -n "$dump" && -f "$dump" ]] || usage

  log "about to restore $(db_name) from: $dump"
  if ((assume_yes == 0)); then
    printf 'This DESTROYS the current database. Type "restore" to continue: '
    local answer
    read -r answer
    [[ "$answer" == "restore" ]] || die "aborted — nothing was changed"
  fi

  log "stopping the app (the database keeps running — it is what we restore into)"
  compose stop app

  log "dropping and recreating the database"
  compose exec -T postgres sh -c '
    psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '"'"'$POSTGRES_DB'"'"' AND pid <> pg_backend_pid()" \
      -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\"" \
      -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\""' >/dev/null

  log "restoring"
  compose exec -T postgres sh -c \
    'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' <"$dump" ||
    log "pg_restore reported warnings — checking the result below"

  local version
  version="$(compose exec -T postgres sh -c \
    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT version_num FROM alembic_version"' |
    tr -d '[:space:]')"
  [[ -n "$version" ]] || die "restored database has no alembic_version — do NOT start the app; restore another dump"
  log "restored at migration $version"

  log "starting the app (it runs alembic upgrade head first, which is a no-op if the dump was current)"
  compose up -d app

  notify "♻️ Sentinel database restored on $(hostname) from $(basename "$dump") (migration $version)."
  log "done. Watch it come up:  docker compose logs -f app"
}

main "$@"

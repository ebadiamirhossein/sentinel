#!/usr/bin/env bash
# Nightly pg_dump with retention — ARCHITECTURE.md §5, docs/DEPLOY.md §7.
#
#   ops/backup.sh
#
# Writes $BACKUP_DIR/sentinel-<UTC>.dump in pg_dump's custom format (-Fc: already
# compressed, and restorable table-by-table).
#
# Three things it does that a one-line `pg_dump > file` cron does not:
#
#   * it writes to `.part` and renames only after the dump exits 0, so a dump
#     interrupted by a reboot never leaves a truncated file that looks like a
#     backup;
#   * it reads the finished file back with `pg_restore -l` before counting it,
#     because a file that cannot be listed cannot be restored either;
#   * it messages Telegram when any of that fails. A backup job that fails
#     silently is worse than no backup job, because you believe you have one.
#
# It does NOT prove the dump restores — that is ops/verify-backup.sh, which
# actually restores it into a scratch database. Run it weekly.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

BACKUP_DIR="$(backup_dir)"
RETENTION_DAYS="$(env_value BACKUP_RETENTION_DAYS 14)"
#: Never pruned, whatever the retention says. A retention window is a policy; a
#: box with zero backups on it is an accident.
ALWAYS_KEEP=3

fail() {
  log "ERROR: $*"
  notify "🚨 Sentinel backup FAILED on $(hostname): $*"
  exit 1
}

main() {
  require_stack
  mkdir -p -- "$BACKUP_DIR" || fail "cannot create $BACKUP_DIR"

  local stamp target
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="$BACKUP_DIR/sentinel-$stamp.dump"

  log "dumping $(db_name) → $target"
  if ! compose exec -T postgres sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$target.part"; then
    rm -f -- "$target.part"
    fail "pg_dump exited non-zero (is the stack up? docker compose ps)"
  fi

  [[ -s "$target.part" ]] || { rm -f -- "$target.part"; fail "pg_dump produced an empty file"; }

  # Read it back. `pg_restore -l` parses the whole archive's table of contents,
  # so a truncated or corrupt dump fails here rather than in six months.
  if ! compose exec -T postgres pg_restore -l >/dev/null 2>&1 <"$target.part"; then
    rm -f -- "$target.part"
    fail "the dump could not be listed with pg_restore -l — treating it as corrupt"
  fi

  mv -- "$target.part" "$target"
  log "ok: $(du -h -- "$target" | cut -f1) $target"

  prune
}

prune() {
  local dumps=() index file
  while IFS= read -r file; do dumps+=("$file"); done < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sentinel-*.dump' | sort -r
  )
  for index in "${!dumps[@]}"; do
    ((index < ALWAYS_KEEP)) && continue
    file="${dumps[$index]}"
    if [[ -n "$(find "$file" -mtime "+${RETENTION_DAYS}" -print -quit)" ]]; then
      rm -f -- "$file" && log "pruned (older than ${RETENTION_DAYS}d): $file"
    fi
  done
  log "kept $(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'sentinel-*.dump' | wc -l | tr -d ' ') dump(s)"
}

main "$@"

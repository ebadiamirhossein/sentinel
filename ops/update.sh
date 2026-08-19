#!/usr/bin/env bash
# Ship the next commit — docs/DEPLOY.md §11.
#
#   ops/update.sh
#
# Back up, pull, rebuild, restart, and wait to see it healthy. Nothing clever:
# the point is that the backup happens *before* the migration, and that the
# script tells you the exact rollback command if the new build does not come up.
#
# Downtime is the length of one container restart (seconds). Migrations run from
# the app's own entrypoint (`alembic upgrade head`), so there is no separate
# migration step to forget.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

HEALTH_TIMEOUT_SECONDS=120

main() {
  require_stack
  cd -- "$SENTINEL_ROOT"

  local previous
  previous="$(git rev-parse --short HEAD)"
  log "current commit: $previous"

  log "step 1/5 — backing up before anything changes"
  "$SENTINEL_ROOT/ops/backup.sh"

  log "step 2/5 — fetching"
  git pull --ff-only || die "git pull failed (local changes? \`git status\`)"
  local target
  target="$(git rev-parse --short HEAD)"
  if [[ "$target" == "$previous" ]]; then
    log "already up to date at $previous — nothing to do"
    return 0
  fi
  log "updating $previous → $target"

  log "step 3/5 — building"
  compose build app || rollback "$previous" "the image did not build"

  log "step 4/5 — restarting (migrations run at container start)"
  # The whole stack, not just `app`: a commit can change docker-compose.yml too,
  # and `up -d app` would leave the database running under the old definition.
  # Compose recreates a container only when its definition actually changed, so
  # in the normal case Postgres is untouched.
  compose up -d

  log "step 5/5 — waiting for /health"
  local waited=0 port
  port="$(env_value SENTINEL_HTTP_PORT 18080)"
  until curl -fsS -m 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; do
    ((waited += 5))
    ((waited < HEALTH_TIMEOUT_SECONDS)) || rollback "$previous" "it never became healthy"
    sleep 5
  done

  log "updated to $target and healthy after ${waited}s"
  notify "🚀 Sentinel updated on $(hostname): $previous → $target, healthy."
}

rollback() {
  local previous="$1" reason="$2"
  log "ERROR: $reason"
  notify "🚨 Sentinel update FAILED on $(hostname): $reason. Still on/reverting to $previous."
  cat >&2 <<EOF

────────────────────────────────────────────────────────────────────────
The update failed: $reason

Go back to the commit that was running:

    cd $SENTINEL_ROOT
    git checkout $previous
    docker compose up -d --build

If the new commit's migration already ran and the old code cannot read the
schema, restore the backup this script took a minute ago:

    ops/restore.sh --latest
────────────────────────────────────────────────────────────────────────
EOF
  exit 1
}

main "$@"

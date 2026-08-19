#!/usr/bin/env bash
# The watchdog that lives OUTSIDE the app — docs/DEPLOY.md §8.
#
#   ops/healthcheck.sh
#
# sentinel/bot/alerts.py messages you when cycles fail. It cannot message you when
# the app is not running, and that is the failure that matters most: a stopped
# stack looks exactly like a quiet market. So this runs from cron on the host,
# curls /health, and speaks up after $WATCHDOG_FAILURES_BEFORE_ALERT consecutive
# failures — one message per outage, plus one when it comes back.
#
# It deliberately checks HTTP rather than `docker compose ps`: a container can be
# "Up" with a wedged event loop, and /health answers 503 when the database is
# unreachable, which is the same thing an owner needs to know.
set -euo pipefail
# shellcheck source=ops/lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

PORT="$(env_value SENTINEL_HTTP_PORT 18080)"
THRESHOLD="$(env_value WATCHDOG_FAILURES_BEFORE_ALERT 3)"
STATE_FILE="${WATCHDOG_STATE:-/var/lib/sentinel/watchdog.state}"
URL="http://127.0.0.1:${PORT}/health"

read_state() {
  [[ -r "$STATE_FILE" ]] && tr -dc '0-9' <"$STATE_FILE" || printf '0'
}

write_state() {
  mkdir -p -- "$(dirname -- "$STATE_FILE")" 2>/dev/null || true
  printf '%s' "$1" >"$STATE_FILE" 2>/dev/null ||
    log "WARNING: cannot write $STATE_FILE — every failure will alert"
}

main() {
  local previous body
  previous="$(read_state)"
  previous="${previous:-0}"

  if body="$(curl -fsS -m 10 "$URL" 2>/dev/null)"; then
    if ((previous >= THRESHOLD)); then
      notify "✅ Sentinel is answering /health again on $(hostname) after $previous failed checks."
    fi
    write_state 0
    log "ok: ${body:0:120}"
    return 0
  fi

  local failures=$((previous + 1))
  write_state "$failures"
  log "FAILED ($failures consecutive): $URL"

  # Alert on the threshold and then every further $THRESHOLD checks, so a long
  # outage repeats occasionally instead of once — the same cadence, and the same
  # reasoning, as the in-app cycle alert.
  if ((failures >= THRESHOLD && failures % THRESHOLD == 0)); then
    notify "🚨 Sentinel is DOWN on $(hostname): $URL has failed $failures checks in a row.
Try:  docker compose ps  ·  docker compose logs --tail=100 app"
  fi
  return 0
}

main "$@"

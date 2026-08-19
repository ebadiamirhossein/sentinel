#!/usr/bin/env bash
# Shared helpers for the host-side ops scripts (docs/DEPLOY.md).
#
# These run on the SERVER, not in the container: they drive `docker compose`, so
# putting them in the image would be backwards (.dockerignore excludes ops/).
#
# Two rules everything here follows:
#
#   * Postgres credentials are never read on the host. Every database command is
#     `docker compose exec postgres sh -c '... "$POSTGRES_USER" ...'`, which uses
#     the environment already inside the container. The password never appears in
#     a process listing, a log line or a shell history.
#   * The Telegram token is read from .env by name and never echoed — curl's
#     stderr is discarded for the same reason, since the token is in the URL.
#
# shellcheck shell=bash

SENTINEL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SENTINEL_ROOT

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

die() {
  log "ERROR: $*" >&2
  exit 1
}

# `docker compose` against the deployment file only. docker-compose.dev.yml is a
# local-development overlay (it publishes the database port) and must never be
# picked up here.
compose() { docker compose -f "$SENTINEL_ROOT/docker-compose.yml" "$@"; }

# One value out of .env, without sourcing it. Sourcing would execute whatever is
# in the file and would choke on the inline comments the template uses.
env_value() {
  local key="$1" default="${2-}" line value
  line="$(grep -E "^[[:space:]]*${key}=" "$SENTINEL_ROOT/.env" 2>/dev/null | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    printf '%s' "$default"
    return 0
  fi
  value="${line#*=}"
  value="$(printf '%s' "$value" | sed -E 's/[[:space:]]+#.*$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/\r$//')"
  value="$(printf '%s' "$value" | sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
  if [[ -z "$value" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$value"
  fi
}

# Send a plain-text message to every allowlisted chat. Never fails the caller: a
# script that died because Telegram was briefly unreachable would turn a warning
# into an outage — the same rule sentinel/bot/alerts.py follows in the app.
notify() {
  local text="$1" token chats chat
  token="$(env_value TELEGRAM_BOT_TOKEN)"
  chats="$(env_value TELEGRAM_ALLOWED_USER_IDS)"
  if [[ -z "$token" || -z "$chats" ]]; then
    log "no TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_IDS in .env — not sent: ${text}"
    return 0
  fi
  local old_ifs="$IFS"
  IFS=','
  for chat in $chats; do
    IFS="$old_ifs"
    chat="$(printf '%s' "$chat" | tr -d '[:space:]')"
    [[ -z "$chat" ]] || curl -sS -m 15 -o /dev/null \
      -X POST "https://api.telegram.org/bot${token}/sendMessage" \
      --data-urlencode "chat_id=${chat}" \
      --data-urlencode "text=${text}" 2>/dev/null ||
      log "WARNING: could not reach Telegram for chat ${chat}"
    IFS=','
  done
  IFS="$old_ifs"
}

require_stack() {
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH (cron PATH is short — use absolute paths or set PATH in the crontab)"
  [[ -f "$SENTINEL_ROOT/.env" ]] || die "$SENTINEL_ROOT/.env is missing — see docs/DEPLOY.md §4"
}

# Where dumps live. A relative BACKUP_DIR is resolved against the repo root, not
# against whatever directory cron happened to start in.
backup_dir() {
  local dir
  dir="$(env_value BACKUP_DIR /opt/sentinel/backups)"
  [[ "$dir" == /* ]] || dir="$SENTINEL_ROOT/$dir"
  printf '%s' "$dir"
}

# The database, named as Compose knows it. Used for log lines only; every command
# takes the real values from inside the container.
db_name() { env_value POSTGRES_DB sentinel; }

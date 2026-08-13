#!/usr/bin/env bash

# Independent host-side bootstrap watchdog.  systemd invokes this after the
# bounded bootstrap lease.  A completed lease is the commit point; every
# other state removes only the isolated shadow sidecar.

set -Eeuo pipefail

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

[ "$#" -eq 6 ] || fail 'usage'
readonly LEASE_FILE="$1"
readonly SIDECAR="$2"
readonly RUN_ID="$3"
readonly RUN_ATTEMPT="$4"
readonly AUDIT_FILE="$5"
readonly SECRET_ENV_FILE="$6"
readonly STATE_DIR="$(dirname "$AUDIT_FILE")"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly LATEST_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"

[ "$SIDECAR" = 'teplo_entitlement_shadow' ] || fail 'invalid_sidecar'
[[ "$RUN_ID" =~ ^[0-9]+$ ]] || fail 'invalid_run_id'
[[ "$RUN_ATTEMPT" =~ ^[0-9]+$ ]] || fail 'invalid_run_attempt'
[ "$(basename "$AUDIT_FILE")" = "bot-production.entitlement-shadow-watchdog.${RUN_ID}.${RUN_ATTEMPT}.audit" ] || fail 'invalid_audit_file'
[ "$SECRET_ENV_FILE" = "$STATE_DIR/entitlement-shadow-secrets-${RUN_ID}-${RUN_ATTEMPT}.env" ] || fail 'invalid_secret_env_file'

install -d -m 700 "$STATE_DIR"

lease_value() {
  key="$1"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$LEASE_FILE" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$LEASE_FILE"
}

sidecar_label() {
  key="$1"
  docker inspect --format "{{index .Config.Labels \"${key}\"}}" "$SIDECAR" 2>/dev/null || true
}

if [ -r "$LEASE_FILE" ] && \
  [ "$(lease_value format_version || true)" = '2' ] && \
  [ "$(lease_value phase || true)" = 'completed' ] && \
  [ "$(lease_value action || true)" = 'ENABLE_SHADOW' ] && \
  [ "$(lease_value workflow_run_id || true)" = "$RUN_ID" ] && \
  [ "$(lease_value workflow_run_attempt || true)" = "$RUN_ATTEMPT" ] && \
  [[ "$(lease_value expires_epoch || true)" =~ ^[0-9]+$ ]] && \
  [ "$(lease_value expires_epoch)" -gt "$(date +%s)" ] && \
  [ "$(docker inspect --format '{{.State.Running}}' "$SIDECAR" 2>/dev/null || true)" = 'true' ] && \
  [ "$(sidecar_label teplo.role)" = 'entitlement-shadow-readonly' ] && \
  [ "$(sidecar_label teplo.workflow_sha)" = "$(lease_value workflow_sha)" ] && \
  [ "$(sidecar_label teplo.workflow_run_id)" = "$RUN_ID" ] && \
  [ "$(sidecar_label teplo.workflow_run_attempt)" = "$RUN_ATTEMPT" ]; then
  rm -f -- "$SECRET_ENV_FILE"
  audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-watchdog.XXXXXX")"
  cp "$LEASE_FILE" "$audit_tmp"
  chmod 600 "$audit_tmp"
  if [ -e "$RUN_AUDIT_FILE" ]; then
    if ! cmp -s "$audit_tmp" "$RUN_AUDIT_FILE"; then
      rm -f -- "$audit_tmp" "$LEASE_FILE"
      docker rm -f "$SIDECAR" >/dev/null 2>&1 || true
      fail 'completed_audit_conflict'
    fi
    rm -f -- "$audit_tmp"
  else
    cp "$audit_tmp" "$RUN_AUDIT_FILE"
    chmod 600 "$RUN_AUDIT_FILE"
    mv "$audit_tmp" "$LATEST_AUDIT_FILE"
  fi
  exit 0
fi

rm -f -- "$SECRET_ENV_FILE"
rm -f -- "$LEASE_FILE"
docker rm -f "$SIDECAR" >/dev/null 2>&1 || true
audit_tmp="$(mktemp "${AUDIT_FILE}.XXXXXX")"
printf 'format_version=2\nphase=completed\naction=AUTO_DISABLE_BOOTSTRAP\nruntime_mode=disabled\nworkflow_run_id=%s\nworkflow_run_attempt=%s\ncompleted_at=%s\n' \
  "$RUN_ID" "$RUN_ATTEMPT" "$(date --iso-8601=seconds)" > "$audit_tmp"
chmod 600 "$audit_tmp"
mv "$audit_tmp" "$AUDIT_FILE"

#!/usr/bin/env bash

# Emergency Gate 2 kill switch.  It has no dependency on bot health, Git,
# PostgreSQL, Redis, RemnaWave, migrations, or CI and never restarts the bot.

set -Eeuo pipefail

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

[ "$#" -eq 7 ] || fail 'usage'
readonly WORKFLOW_SHA="$1"
readonly RUN_ID="$2"
readonly RUN_ATTEMPT="$3"
readonly ACTOR="$4"
readonly RELEASE_CARD="$5"
readonly STATE_DIR="$6"
readonly RUNTIME_DIR="$7"
readonly SIDECAR='teplo_entitlement_shadow'
readonly LEASE_FILE="$RUNTIME_DIR/lease.state"
readonly AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.lock"

[[ "$WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid_workflow_sha'
[[ "$RUN_ID" =~ ^[0-9]+$ ]] || fail 'invalid_run_id'
[[ "$RUN_ATTEMPT" =~ ^[0-9]+$ ]] || fail 'invalid_run_attempt'
[[ "$ACTOR" =~ ^[A-Za-z0-9-]{1,39}$ ]] || fail 'invalid_actor'
[[ "$RELEASE_CARD" =~ ^[A-Za-z0-9._:/@-]{1,160}$ ]] || fail 'invalid_release_card'
[ "$STATE_DIR" = '/var/lib/teplo-vpn/deploy-state' ] || fail 'invalid_state_dir'
[ "$RUNTIME_DIR" = '/var/lib/teplo-vpn/entitlement-shadow-runtime' ] || fail 'invalid_runtime_dir'

lease_value() {
  key="$1"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$LEASE_FILE" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$LEASE_FILE"
}

audit_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$file"
}

install -d -m 700 "$STATE_DIR"
install -d -m 755 "$RUNTIME_DIR"
exec 9>"$LOCK_FILE"
flock -w 30 9 || fail 'control_busy'

old_run_id="$(lease_value workflow_run_id || true)"
old_run_attempt="$(lease_value workflow_run_attempt || true)"
# Removing the lease is deliberately the first state change. A hard-killed
# disable therefore still makes the sidecar terminate itself.
rm -f -- "$LEASE_FILE"
if [[ "$old_run_id" =~ ^[0-9]+$ ]] && [[ "$old_run_attempt" =~ ^[0-9]+$ ]]; then
  old_unit="teplo-entitlement-shadow-watchdog-${old_run_id}-${old_run_attempt}"
  systemctl stop "${old_unit}.timer" "${old_unit}.service" >/dev/null 2>&1 || true
  rm -f -- "$STATE_DIR/entitlement-shadow-secrets-${old_run_id}-${old_run_attempt}.env"
fi

docker info >/dev/null 2>&1 || fail 'docker_unavailable_after_lease_removal'
if docker inspect "$SIDECAR" >/dev/null 2>&1; then
  docker rm -f "$SIDECAR" >/dev/null
fi
if docker inspect "$SIDECAR" >/dev/null 2>&1; then
  fail 'sidecar_still_present'
fi

if [ -r "$RUN_AUDIT_FILE" ]; then
  [ "$(audit_value action "$RUN_AUDIT_FILE" || true)" = 'DISABLE_SHADOW' ] || fail 'run_audit_conflict'
  [ "$(audit_value runtime_mode "$RUN_AUDIT_FILE" || true)" = 'disabled' ] || fail 'run_audit_conflict'
  [ "$(audit_value workflow_sha "$RUN_AUDIT_FILE" || true)" = "$WORKFLOW_SHA" ] || fail 'run_audit_conflict'
  [ "$(audit_value approval_actor "$RUN_AUDIT_FILE" || true)" = "$ACTOR" ] || fail 'run_audit_conflict'
  [ "$(audit_value release_card_reference "$RUN_AUDIT_FILE" || true)" = "$RELEASE_CARD" ] || fail 'run_audit_conflict'
  cp "$RUN_AUDIT_FILE" "$AUDIT_FILE"
  chmod 600 "$AUDIT_FILE"
  printf 'Gate 2 shadow already disabled by this approved run.\n'
  exit 0
fi

audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-control.XXXXXX")"
printf 'format_version=2\nphase=completed\naction=DISABLE_SHADOW\nruntime_mode=disabled\nworkflow_sha=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\napproval_actor=%s\nrelease_card_reference=%s\ncompleted_at=%s\n' \
  "$WORKFLOW_SHA" "$RUN_ID" "$RUN_ATTEMPT" "$ACTOR" "$RELEASE_CARD" \
  "$(date --iso-8601=seconds)" > "$audit_tmp"
chmod 600 "$audit_tmp"
cp "$audit_tmp" "$RUN_AUDIT_FILE"
chmod 600 "$RUN_AUDIT_FILE"
mv "$audit_tmp" "$AUDIT_FILE"
printf 'Gate 2 shadow disabled; production bot was not restarted.\n'

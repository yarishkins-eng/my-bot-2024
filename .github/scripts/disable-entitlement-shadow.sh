#!/usr/bin/env bash

# Emergency Gate 2 kill switch. It is independent of bot/DB/Panel health and
# generation-fenced so a stale timer can never remove a newer shadow sidecar.

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
readonly DISABLE_TOMBSTONE_FILE="$RUNTIME_DIR/disable.state"
readonly AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.lock"
readonly DISABLE_UNIT="teplo-entitlement-shadow-disable-${RUN_ID}-${RUN_ATTEMPT}"
readonly HELPER="$STATE_DIR/entitlement-shadow-disable-${WORKFLOW_SHA}-${RUN_ID}-${RUN_ATTEMPT}.sh"

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

replace_latest() {
  source_file="$1"
  latest_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-latest.XXXXXX")"
  cp "$source_file" "$latest_tmp"
  chmod 600 "$latest_tmp"
  mv "$latest_tmp" "$AUDIT_FILE"
}

install_helper() {
  helper_tmp="$(mktemp "$STATE_DIR/entitlement-shadow-disable.XXXXXX")"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -Eeuo pipefail' \
    'lease="$1"; tombstone="$2"; sidecar="$3"; expected_id="$4"; expected_run="$5"; expected_attempt="$6"; disable_run="$7"; disable_attempt="$8"' \
    'state="$(dirname "$0")"' \
    'if [ "${TEPLO_SHADOW_CONTROL_LOCK_HELD:-0}" != 1 ]; then exec 9>"$state/bot-production.entitlement-shadow-control.lock"; flock -w 30 9 || exit 1; fi' \
    'tombstone_run="$(sed -n "s/^workflow_run_id=//p" "$tombstone" 2>/dev/null || true)"' \
    'tombstone_attempt="$(sed -n "s/^workflow_run_attempt=//p" "$tombstone" 2>/dev/null || true)"' \
    '[[ "$tombstone_run" =~ ^[0-9]+$ ]] && [[ "$tombstone_attempt" =~ ^[0-9]+$ ]] || exit 1' \
    '[ "$tombstone_run" = "$disable_run" ] && [ "$tombstone_attempt" = "$disable_attempt" ] || exit 0' \
    'workflow_sha="$(sed -n "s/^workflow_sha=//p" "$tombstone" 2>/dev/null || true)"' \
    'actor="$(sed -n "s/^approval_actor=//p" "$tombstone" 2>/dev/null || true)"' \
    'release_card="$(sed -n "s/^release_card_reference=//p" "$tombstone" 2>/dev/null || true)"' \
    '[[ "$workflow_sha" =~ ^[0-9a-f]{40}$ ]] && [[ "$actor" =~ ^[A-Za-z0-9-]{1,39}$ ]] && [[ "$release_card" =~ ^[A-Za-z0-9._:/@-]{1,160}$ ]] || exit 1' \
    'lease_run="$(sed -n "s/^workflow_run_id=//p" "$lease" 2>/dev/null || true)"' \
    'lease_attempt="$(sed -n "s/^workflow_run_attempt=//p" "$lease" 2>/dev/null || true)"' \
    'if [ "$expected_run" != none ] && [ -e "$lease" ] && { [ "$lease_run" != "$expected_run" ] || [ "$lease_attempt" != "$expected_attempt" ]; }; then exit 0; fi' \
    'rm -f -- "$lease"' \
    'docker info >/dev/null 2>&1 || exit 1' \
    'target="$sidecar"; [ "$expected_id" = pending ] || target="$expected_id"' \
    'actual="$(docker inspect --format "{{.Id}}" "$target" 2>/dev/null || true)"' \
    'if [ -n "$actual" ]; then' \
    '  [ "$expected_id" = pending ] || [ "$actual" = "$expected_id" ] || exit 0' \
    '  role="$(docker inspect --format "{{index .Config.Labels \"teplo.role\"}}" "$actual" 2>/dev/null || true)"' \
    '  [ "$role" = entitlement-shadow-readonly ] || exit 1' \
    '  if [ "$expected_run" != none ]; then label_run="$(docker inspect --format "{{index .Config.Labels \"teplo.workflow_run_id\"}}" "$actual" 2>/dev/null || true)"; label_attempt="$(docker inspect --format "{{index .Config.Labels \"teplo.workflow_run_attempt\"}}" "$actual" 2>/dev/null || true)"; [ "$label_run" = "$expected_run" ] && [ "$label_attempt" = "$expected_attempt" ] || exit 0; fi' \
    '  docker rm --force "$actual" >/dev/null 2>&1 || exit 1' \
    'fi' \
    'docker info >/dev/null 2>&1 || exit 1' \
    '! docker inspect "$actual" >/dev/null 2>&1 || exit 1' \
    'keyed="$state/bot-production.entitlement-shadow-control.${disable_run}.${disable_attempt}.audit"' \
    'latest="$state/bot-production.entitlement-shadow-control.state"' \
    'if [ -e "$keyed" ]; then' \
    '  [ "$(sed -n "s/^action=//p" "$keyed")" = DISABLE_SHADOW ] && [ "$(sed -n "s/^runtime_mode=//p" "$keyed")" = disabled ] && [ "$(sed -n "s/^workflow_sha=//p" "$keyed")" = "$workflow_sha" ] && [ "$(sed -n "s/^approval_actor=//p" "$keyed")" = "$actor" ] && [ "$(sed -n "s/^release_card_reference=//p" "$keyed")" = "$release_card" ] || exit 1' \
    'else' \
    '  audit_tmp="$(mktemp "$state/bot-production.entitlement-shadow-disable.XXXXXX")"' \
    '  printf "format_version=2\nphase=completed\naction=DISABLE_SHADOW\nruntime_mode=disabled\nworkflow_sha=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\napproval_actor=%s\nrelease_card_reference=%s\ncompleted_at=%s\n" "$workflow_sha" "$disable_run" "$disable_attempt" "$actor" "$release_card" "$(date --iso-8601=seconds)" > "$audit_tmp"' \
    '  chmod 600 "$audit_tmp"; mv "$audit_tmp" "$keyed"' \
    'fi' \
    'latest_tmp="$(mktemp "$state/bot-production.entitlement-shadow-latest.XXXXXX")"' \
    'cp "$keyed" "$latest_tmp"; chmod 600 "$latest_tmp"; mv "$latest_tmp" "$latest"' \
    'rm -f -- "$tombstone"' > "$helper_tmp"
  chmod 700 "$helper_tmp"
  if [ -e "$HELPER" ]; then
    cmp -s "$helper_tmp" "$HELPER" || {
      rm -f -- "$helper_tmp"
      fail 'disable_helper_conflict'
    }
    rm -f -- "$helper_tmp"
  else
    mv "$helper_tmp" "$HELPER"
  fi
}

install -d -m 700 "$STATE_DIR"
install -d -m 755 "$RUNTIME_DIR"
exec 9>"$LOCK_FILE"
flock -w 30 9 || fail 'control_busy'

old_run_id="$(lease_value workflow_run_id || true)"
old_run_attempt="$(lease_value workflow_run_attempt || true)"
old_expires_epoch="$(lease_value expires_epoch || true)"
if ! [[ "$old_run_id" =~ ^[0-9]+$ ]] || ! [[ "$old_run_attempt" =~ ^[0-9]+$ ]]; then
  old_run_id='none'
  old_run_attempt='none'
fi

install_helper
systemctl stop "${DISABLE_UNIT}.timer" "${DISABLE_UNIT}.service" >/dev/null 2>&1 || true
systemctl reset-failed "${DISABLE_UNIT}.timer" "${DISABLE_UNIT}.service" >/dev/null 2>&1 || true
systemd-run --quiet --unit="$DISABLE_UNIT" --on-active=60s --property=Type=oneshot \
  --property=Restart=on-failure --property=RestartSec=30s \
  "$HELPER" "$LEASE_FILE" "$DISABLE_TOMBSTONE_FILE" "$SIDECAR" pending \
  "$old_run_id" "$old_run_attempt" "$RUN_ID" "$RUN_ATTEMPT"

# First state mutation: the tombstone blocks a new ENABLE/deploy before the
# independently armed helper can act. Lease loss then stops a live sidecar.
tombstone_tmp="$(mktemp "$RUNTIME_DIR/disable.XXXXXX")"
printf 'format_version=1\nworkflow_sha=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\napproval_actor=%s\nrelease_card_reference=%s\n' \
  "$WORKFLOW_SHA" "$RUN_ID" "$RUN_ATTEMPT" "$ACTOR" "$RELEASE_CARD" > "$tombstone_tmp"
chmod 444 "$tombstone_tmp"
mv "$tombstone_tmp" "$DISABLE_TOMBSTONE_FILE"
rm -f -- "$LEASE_FILE"
if [ "$old_run_id" != 'none' ]; then
  rm -f -- "$STATE_DIR/entitlement-shadow-secrets-${old_run_id}-${old_run_attempt}.env"
fi

TEPLO_SHADOW_CONTROL_LOCK_HELD=1 "$HELPER" "$LEASE_FILE" "$DISABLE_TOMBSTONE_FILE" "$SIDECAR" pending \
  "$old_run_id" "$old_run_attempt" "$RUN_ID" "$RUN_ATTEMPT" || fail 'sidecar_removal_unverified'
docker info >/dev/null 2>&1 || fail 'docker_unavailable_after_lease_removal'
! docker inspect "$SIDECAR" >/dev/null 2>&1 || fail 'sidecar_still_present'
[ ! -e "$DISABLE_TOMBSTONE_FILE" ] || fail 'disable_tombstone_still_present'

systemctl stop "${DISABLE_UNIT}.timer" "${DISABLE_UNIT}.service" >/dev/null 2>&1 || true
if [ "$old_run_id" != 'none' ]; then
  systemctl stop \
    "teplo-entitlement-shadow-watchdog-pending-${old_run_id}-${old_run_attempt}.timer" \
    "teplo-entitlement-shadow-watchdog-pending-${old_run_id}-${old_run_attempt}.service" \
    "teplo-entitlement-shadow-watchdog-exact-${old_run_id}-${old_run_attempt}.timer" \
    "teplo-entitlement-shadow-watchdog-exact-${old_run_id}-${old_run_attempt}.service" \
    >/dev/null 2>&1 || true
  if [[ "$old_expires_epoch" =~ ^[0-9]+$ ]]; then
    systemctl stop \
      "teplo-entitlement-shadow-expiry-${old_run_id}-${old_run_attempt}-${old_expires_epoch}.timer" \
      "teplo-entitlement-shadow-expiry-${old_run_id}-${old_run_attempt}-${old_expires_epoch}.service" \
      >/dev/null 2>&1 || true
  fi
fi

if [ -r "$RUN_AUDIT_FILE" ]; then
  [ "$(audit_value action "$RUN_AUDIT_FILE" || true)" = 'DISABLE_SHADOW' ] || fail 'run_audit_conflict'
  [ "$(audit_value runtime_mode "$RUN_AUDIT_FILE" || true)" = 'disabled' ] || fail 'run_audit_conflict'
  [ "$(audit_value workflow_sha "$RUN_AUDIT_FILE" || true)" = "$WORKFLOW_SHA" ] || fail 'run_audit_conflict'
  [ "$(audit_value approval_actor "$RUN_AUDIT_FILE" || true)" = "$ACTOR" ] || fail 'run_audit_conflict'
  [ "$(audit_value release_card_reference "$RUN_AUDIT_FILE" || true)" = "$RELEASE_CARD" ] || fail 'run_audit_conflict'
  replace_latest "$RUN_AUDIT_FILE"
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

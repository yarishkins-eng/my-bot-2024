#!/usr/bin/env bash

# Host-owned generation-fenced watchdog. BOOTSTRAP removes an uncommitted
# sidecar or records a completed lease and arms an independent hard expiry.
# EXPIRY removes only the exact container generation it was created for.

set -Eeuo pipefail

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

[ "$#" -eq 8 ] || fail 'usage'
readonly MODE="$1"
readonly LEASE_FILE="$2"
readonly SIDECAR="$3"
readonly RUN_ID="$4"
readonly RUN_ATTEMPT="$5"
readonly AUDIT_FILE="$6"
readonly SECRET_ENV_FILE="$7"
readonly EXPECTED_CONTAINER_ID="$8"
readonly STATE_DIR="$(dirname "$AUDIT_FILE")"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly LATEST_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"
readonly LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.lock"
readonly RUN_LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-watchdog.${RUN_ID}.${RUN_ATTEMPT}.lock"
readonly POLICY_VERSION='gate2-readonly-v1'
readonly WATCHDOG_SELF="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"

[ "$MODE" = 'BOOTSTRAP' ] || [ "$MODE" = 'EXPIRY' ] || fail 'invalid_mode'
[ "$SIDECAR" = 'teplo_entitlement_shadow' ] || fail 'invalid_sidecar'
[[ "$RUN_ID" =~ ^[0-9]+$ ]] || fail 'invalid_run_id'
[[ "$RUN_ATTEMPT" =~ ^[0-9]+$ ]] || fail 'invalid_run_attempt'
[ "$EXPECTED_CONTAINER_ID" = 'pending' ] || \
  [[ "$EXPECTED_CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]] || fail 'invalid_container_id'
[ "$(basename "$AUDIT_FILE")" = "bot-production.entitlement-shadow-watchdog.${RUN_ID}.${RUN_ATTEMPT}.audit" ] || fail 'invalid_audit_file'
[ "$SECRET_ENV_FILE" = "$STATE_DIR/entitlement-shadow-secrets-${RUN_ID}-${RUN_ATTEMPT}.env" ] || fail 'invalid_secret_env_file'
[ -x "$WATCHDOG_SELF" ] || fail 'watchdog_not_persistent'

install -d -m 700 "$STATE_DIR"
if [ "${TEPLO_SHADOW_CONTROL_LOCK_HELD:-0}" != '1' ]; then
  exec 8>"$LOCK_FILE"
  flock -w 30 8 || fail 'control_busy'
fi
exec 9>"$RUN_LOCK_FILE"
flock -w 30 9 || fail 'watchdog_busy'

lease_value() {
  key="$1"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$LEASE_FILE" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$LEASE_FILE"
}

lease_generation() {
  [ -e "$LEASE_FILE" ] || {
    printf 'none\n'
    return
  }
  lease_run_id="$(lease_value workflow_run_id || true)"
  lease_run_attempt="$(lease_value workflow_run_attempt || true)"
  if [ "$lease_run_id" = "$RUN_ID" ] && [ "$lease_run_attempt" = "$RUN_ATTEMPT" ]; then
    printf 'exact\n'
  elif [[ "$lease_run_id" =~ ^[0-9]+$ ]] && [[ "$lease_run_attempt" =~ ^[0-9]+$ ]]; then
    printf 'different\n'
  else
    printf 'invalid\n'
  fi
}

container_target() {
  if [ "$EXPECTED_CONTAINER_ID" = 'pending' ]; then
    printf '%s\n' "$SIDECAR"
  else
    printf '%s\n' "$EXPECTED_CONTAINER_ID"
  fi
}

container_value() {
  format="$1"
  docker inspect --format "$format" "$(container_target)" 2>/dev/null || true
}

container_id_for_removal() {
  inspect_tmp="$(mktemp "$STATE_DIR/entitlement-shadow-inspect.XXXXXX")"
  if docker inspect --format '{{.Id}}' "$(container_target)" > "$inspect_tmp" 2>/dev/null; then
    inspect_rc=0
  else
    inspect_rc=$?
  fi
  inspected_id="$(cat "$inspect_tmp")"
  rm -f -- "$inspect_tmp"

  case "$inspect_rc" in
    0)
      [[ "$inspected_id" =~ ^[0-9a-f]{64}$ ]] || return 1
      printf '%s\n' "$inspected_id"
      ;;
    1)
      # Docker uses rc=1 for a container that is provably absent.
      printf '\n'
      ;;
    *)
      # A daemon/transport/client failure is never evidence of absence.
      return 1
      ;;
  esac
}

fixed_sidecar_is_absent() {
  remaining="$({
    docker container ls -a --no-trunc \
      --filter "name=^/${SIDECAR}$" --format '{{.ID}}'
  } 2>/dev/null)" || return 1
  [ -z "$remaining" ]
}

container_label() {
  key="$1"
  container_value "{{index .Config.Labels \"${key}\"}}"
}

exact_env() {
  key="$1"
  expected="$2"
  [ "$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$(container_target)" 2>/dev/null | grep -Fxc "${key}=${expected}" || true)" = '1' ]
}

container_is_exact_generation() {
  actual_id="$(container_value '{{.Id}}')"
  [[ "$actual_id" =~ ^[0-9a-f]{64}$ ]] &&
    { [ "$EXPECTED_CONTAINER_ID" = 'pending' ] || [ "$actual_id" = "$EXPECTED_CONTAINER_ID" ]; } &&
    [ "$(container_label teplo.role)" = 'entitlement-shadow-readonly' ] &&
    [ "$(container_label teplo.workflow_run_id)" = "$RUN_ID" ] &&
    [ "$(container_label teplo.workflow_run_attempt)" = "$RUN_ATTEMPT" ]
}

container_matches_completed_lease() {
  container_is_exact_generation &&
    [ "$(container_value '{{.State.Running}}')" = 'true' ] &&
    [ "$(container_value '{{.State.Paused}}')" = 'false' ] &&
    [ "$(container_value '{{.Image}}')" = "$(lease_value image)" ] &&
    [ "$(container_label teplo.workflow_sha)" = "$(lease_value workflow_sha)" ] &&
    [ "$(container_label teplo.deployed_sha)" = "$(lease_value deployed_sha)" ] &&
    [ "$(container_label teplo.policy_version)" = "$POLICY_VERSION" ] &&
    exact_env ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED false &&
    exact_env ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED false &&
    exact_env ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED false &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_ENABLED true &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH false &&
    exact_env DATABASE_POOL_SIZE 2 &&
    exact_env DATABASE_MAX_OVERFLOW 0 &&
    exact_env DATABASE_POOL_TIMEOUT 5 &&
    exact_env REMNAWAVE_API_CONNECT_TIMEOUT 4 &&
    exact_env REMNAWAVE_API_TOTAL_TIMEOUT 4 &&
    exact_env REMNAWAVE_AUTH_TYPE api_key &&
    exact_env TZ Europe/Moscow &&
    exact_env BOT_TOKEN 123456789:shadow-sidecar-does-not-use-telegram &&
    exact_env ADMIN_NOTIFICATIONS_ENABLED false &&
    exact_env REMNAWAVE_WEBHOOK_ENABLED false &&
    exact_env REMNAWAVE_AUTO_SYNC_ENABLED false &&
    exact_env ACCESS_POINT_INVENTORY_DRY_RUN_ENABLED false &&
    exact_env ACCESS_POINT_INVENTORY_CATALOG_APPLY_ENABLED false &&
    exact_env MULTI_TARIFF_ENABLED false &&
    exact_env DEFAULT_TRAFFIC_RESET_STRATEGY MONTH &&
    exact_env DEVICES_SELECTION_ENABLED true &&
    exact_env DEVICES_SELECTION_DISABLED_AMOUNT '' &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_COHORT_BASIS_POINTS 1000 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_IDENTITIES_PER_CYCLE 18 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_SCHEDULE_SECONDS 900 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_PANEL_READS_PER_MINUTE 12 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_PANEL_TIMEOUT_SECONDS 4 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_DB_STATEMENT_TIMEOUT_MS 5000 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CYCLE_SECONDS 180 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MIN_RATIO_SAMPLE 10 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERRORS 2 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERROR_BASIS_POINTS 1000 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_COUNT 2 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_BASIS_POINTS 1000 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_COUNT 2 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_BASIS_POINTS 1000 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_COUNT 4 &&
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_BASIS_POINTS 2000
}

remove_own_generation() {
  rm -f -- "$SECRET_ENV_FILE"
  generation="$(lease_generation)"

  # Removing the exact lease first makes a live sidecar stop itself even when
  # Docker is temporarily unavailable. A malformed lease is never deleted.
  if [ "$generation" = 'exact' ]; then
    rm -f -- "$LEASE_FILE"
  fi
  docker info >/dev/null 2>&1 || return 1

  actual_id="$(container_id_for_removal)" || return 1
  if [ -n "$actual_id" ]; then
    container_is_exact_generation || {
      [ "$generation" = 'different' ] || return 1
      return 10
    }
    docker rm --force "$actual_id" >/dev/null 2>&1 || return 1
  fi
  # An exact target from an older run may already be absent while the fixed
  # sidecar name legitimately belongs to the newer lease. Never inspect or
  # remove that newer generation.
  [ "$generation" != 'different' ] || return 10
  docker info >/dev/null 2>&1 || return 1
  fixed_sidecar_is_absent || return 1
  [ "$generation" != 'invalid' ] || return 1
  return 0
}

replace_latest() {
  source_file="$1"
  latest_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-latest.XXXXXX")"
  cp "$source_file" "$latest_tmp"
  chmod 600 "$latest_tmp"
  mv "$latest_tmp" "$LATEST_AUDIT_FILE"
}

write_disabled_audit() {
  action="$1"
  disabled_audit="$STATE_DIR/bot-production.entitlement-shadow-watchdog.${RUN_ID}.${RUN_ATTEMPT}.${action}.audit"
  audit_tmp="$(mktemp "${disabled_audit}.XXXXXX")"
  printf 'format_version=2\nphase=completed\naction=%s\nruntime_mode=disabled\npolicy_version=%s\ncontainer_generation=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\ncompleted_at=%s\n' \
    "$action" "$POLICY_VERSION" "$EXPECTED_CONTAINER_ID" "$RUN_ID" "$RUN_ATTEMPT" \
    "$(date --iso-8601=seconds)" > "$audit_tmp"
  chmod 600 "$audit_tmp"
  if [ -e "$disabled_audit" ]; then
    existing_action="$(sed -n 's/^action=//p' "$disabled_audit" 2>/dev/null || true)"
    existing_mode="$(sed -n 's/^runtime_mode=//p' "$disabled_audit" 2>/dev/null || true)"
    [ "$existing_action" = "$action" ] && [ "$existing_mode" = 'disabled' ] || {
      rm -f -- "$audit_tmp"
      return 1
    }
    rm -f -- "$audit_tmp"
  else
    mv "$audit_tmp" "$disabled_audit"
  fi
  [ "$(lease_generation)" != 'different' ] || return 10
  replace_latest "$disabled_audit"
}

materialize_completed_audits() {
  audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-watchdog.XXXXXX")"
  cp "$LEASE_FILE" "$audit_tmp"
  chmod 600 "$audit_tmp"
  for target in "$RUN_AUDIT_FILE" "$AUDIT_FILE"; do
    if [ -e "$target" ]; then
      cmp -s "$audit_tmp" "$target" || {
        rm -f -- "$audit_tmp"
        return 1
      }
    else
      cp "$audit_tmp" "$target"
      chmod 600 "$target"
    fi
  done
  rm -f -- "$audit_tmp"
  replace_latest "$RUN_AUDIT_FILE"
}

arm_expiry() {
  expires_epoch="$1"
  actual_id="$(container_value '{{.Id}}')"
  [[ "$actual_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  remaining="$(( expires_epoch - $(date +%s) ))"
  [ "$remaining" -gt 0 ] || return 1
  expiry_unit="teplo-entitlement-shadow-expiry-${RUN_ID}-${RUN_ATTEMPT}-${expires_epoch}"
  if systemctl is-active --quiet "${expiry_unit}.timer"; then
    return 0
  fi
  systemctl stop "${expiry_unit}.timer" "${expiry_unit}.service" >/dev/null 2>&1 || true
  systemctl reset-failed "${expiry_unit}.timer" "${expiry_unit}.service" >/dev/null 2>&1 || true
  systemd-run --quiet --unit="$expiry_unit" --on-active="${remaining}s" --property=Type=oneshot \
    --property=Restart=on-failure --property=RestartSec=30s \
    "$WATCHDOG_SELF" EXPIRY "$LEASE_FILE" "$SIDECAR" "$RUN_ID" "$RUN_ATTEMPT" \
    "$AUDIT_FILE" "$SECRET_ENV_FILE" "$actual_id"
}

generation="$(lease_generation)"

if [ "$MODE" = 'EXPIRY' ]; then
  remove_own_generation_rc=0
  remove_own_generation || remove_own_generation_rc=$?
  [ "$remove_own_generation_rc" = '10' ] && exit 0
  [ "$remove_own_generation_rc" = '0' ] || fail 'expiry_container_removal_unverified'
  write_disabled_audit AUTO_DISABLE_EXPIRY || fail 'expiry_audit_unverified'
  exit 0
fi

if [ "$generation" = 'different' ]; then
  remove_own_generation_rc=0
  remove_own_generation || remove_own_generation_rc=$?
  [ "$remove_own_generation_rc" = '10' ] && exit 0
  [ "$remove_own_generation_rc" = '0' ] || fail 'stale_bootstrap_cleanup_unverified'
  exit 0
fi

if [ -r "$LEASE_FILE" ] && \
  [ "$(lease_value format_version || true)" = '2' ] && \
  [ "$(lease_value phase || true)" = 'completed' ] && \
  [ "$(lease_value action || true)" = 'ENABLE_SHADOW' ] && \
  [ "$(lease_value policy_version || true)" = "$POLICY_VERSION" ] && \
  [ "$(lease_value workflow_run_id || true)" = "$RUN_ID" ] && \
  [ "$(lease_value workflow_run_attempt || true)" = "$RUN_ATTEMPT" ] && \
  [[ "$(lease_value expires_epoch || true)" =~ ^[0-9]+$ ]] && \
  [ "$(lease_value expires_epoch)" -gt "$(date +%s)" ] && \
  container_matches_completed_lease; then
  rm -f -- "$SECRET_ENV_FILE"
  if ! arm_expiry "$(lease_value expires_epoch)"; then
    remove_own_generation_rc=0
    remove_own_generation || remove_own_generation_rc=$?
    [ "$remove_own_generation_rc" = '0' ] || fail 'expiry_arm_and_cleanup_failed'
    write_disabled_audit AUTO_DISABLE_EXPIRY_ARM_FAILED || fail 'expiry_arm_failure_audit_unverified'
    fail 'expiry_watchdog_arm_failed'
  fi
  materialize_completed_audits || {
    remove_own_generation || true
    fail 'completed_audit_conflict'
  }
  exit 0
fi

remove_own_generation_rc=0
remove_own_generation || remove_own_generation_rc=$?
[ "$remove_own_generation_rc" = '10' ] && exit 0
[ "$remove_own_generation_rc" = '0' ] || fail 'bootstrap_container_removal_unverified'
write_disabled_audit AUTO_DISABLE_BOOTSTRAP || fail 'bootstrap_audit_unverified'

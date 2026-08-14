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
readonly RUNTIME_DIR="$(dirname "$LEASE_FILE")"
readonly CLEANUP_INTENT_FILE="$RUNTIME_DIR/failed-enable-cleanup.state"
readonly CONTROLLER_GUARD_FILE="$RUNTIME_DIR/failed-enable-guard.state"
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

state_file_value() {
  file="$1"
  key="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$file"
}

cleanup_intent_is_exact() {
  [ -r "$CLEANUP_INTENT_FILE" ] &&
    [ "$(state_file_value "$CLEANUP_INTENT_FILE" format_version || true)" = '2' ] &&
    { [ "$(state_file_value "$CLEANUP_INTENT_FILE" phase || true)" = 'prepared' ] ||
      [ "$(state_file_value "$CLEANUP_INTENT_FILE" phase || true)" = 'completed' ]; } &&
    [ "$(state_file_value "$CLEANUP_INTENT_FILE" action || true)" = 'ENABLE_SHADOW' ] &&
    [ "$(state_file_value "$CLEANUP_INTENT_FILE" policy_version || true)" = "$POLICY_VERSION" ] &&
    [ "$(state_file_value "$CLEANUP_INTENT_FILE" workflow_run_id || true)" = "$RUN_ID" ] &&
    [ "$(state_file_value "$CLEANUP_INTENT_FILE" workflow_run_attempt || true)" = "$RUN_ATTEMPT" ]
}

controller_guard_is_exact() {
  [ -r "$CONTROLLER_GUARD_FILE" ] &&
    [ "$(state_file_value "$CONTROLLER_GUARD_FILE" format_version || true)" = '2' ] &&
    { [ "$(state_file_value "$CONTROLLER_GUARD_FILE" phase || true)" = 'prepared' ] ||
      [ "$(state_file_value "$CONTROLLER_GUARD_FILE" phase || true)" = 'completed' ]; } &&
    [ "$(state_file_value "$CONTROLLER_GUARD_FILE" action || true)" = 'ENABLE_SHADOW' ] &&
    [ "$(state_file_value "$CONTROLLER_GUARD_FILE" policy_version || true)" = "$POLICY_VERSION" ] &&
    [ "$(state_file_value "$CONTROLLER_GUARD_FILE" workflow_run_id || true)" = "$RUN_ID" ] &&
    [ "$(state_file_value "$CONTROLLER_GUARD_FILE" workflow_run_attempt || true)" = "$RUN_ATTEMPT" ]
}

ensure_controller_guard() {
  if [ -e "$CONTROLLER_GUARD_FILE" ]; then
    controller_guard_is_exact
    return
  fi
  [ -r "$LEASE_FILE" ] || return 1
  [ "$(lease_generation)" = 'exact' ] || return 1
  guard_tmp="$(mktemp "$RUNTIME_DIR/failed-enable-guard.XXXXXX")" || return 1
  if ! cp "$LEASE_FILE" "$guard_tmp" || ! chmod 444 "$guard_tmp"; then
    rm -f -- "$guard_tmp"
    return 1
  fi
  if cmp -s "$guard_tmp" "$LEASE_FILE"; then
    :
  else
    cmp_rc=$?
    rm -f -- "$guard_tmp"
    [ "$cmp_rc" = '1' ] || return 1
    return 1
  fi
  mv "$guard_tmp" "$CONTROLLER_GUARD_FILE" || {
    rm -f -- "$guard_tmp"
    return 1
  }
  controller_guard_is_exact
}

ensure_cleanup_intent() {
  if [ -e "$CLEANUP_INTENT_FILE" ]; then
    cleanup_intent_is_exact
    return
  fi
  [ -r "$LEASE_FILE" ] || return 1
  [ "$(lease_generation)" = 'exact' ] || return 1
  cleanup_tmp="$(mktemp "$RUNTIME_DIR/failed-enable-cleanup.XXXXXX")" || return 1
  if ! cp "$LEASE_FILE" "$cleanup_tmp" || ! chmod 444 "$cleanup_tmp"; then
    rm -f -- "$cleanup_tmp"
    return 1
  fi
  if cmp -s "$cleanup_tmp" "$LEASE_FILE"; then
    :
  else
    cmp_rc=$?
    rm -f -- "$cleanup_tmp"
    [ "$cmp_rc" = '1' ] || return 1
    return 1
  fi
  mv "$cleanup_tmp" "$CLEANUP_INTENT_FILE" || {
    rm -f -- "$cleanup_tmp"
    return 1
  }
  cleanup_intent_is_exact
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

exact_env() {
  key="$1"
  expected="$2"
  [ "$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$(container_target)" 2>/dev/null | grep -Fxc "${key}=${expected}" || true)" = '1' ]
}

capture_container_snapshot() {
  container_snapshot_raw="$(docker inspect --format '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}|{{index .Config.Labels "teplo.role"}}|{{index .Config.Labels "teplo.workflow_run_id"}}|{{index .Config.Labels "teplo.workflow_run_attempt"}}|{{index .Config.Labels "teplo.workflow_sha"}}|{{index .Config.Labels "teplo.deployed_sha"}}|{{index .Config.Labels "teplo.policy_version"}}' "$(container_target)" 2>/dev/null)" || return 1
  IFS='|' read -r CONTAINER_SNAPSHOT_ID CONTAINER_SNAPSHOT_IMAGE CONTAINER_SNAPSHOT_RUNNING \
    CONTAINER_SNAPSHOT_PAUSED CONTAINER_SNAPSHOT_ROLE CONTAINER_SNAPSHOT_RUN_ID \
    CONTAINER_SNAPSHOT_RUN_ATTEMPT CONTAINER_SNAPSHOT_WORKFLOW_SHA \
    CONTAINER_SNAPSHOT_DEPLOYED_SHA CONTAINER_SNAPSHOT_POLICY_VERSION \
    CONTAINER_SNAPSHOT_EXTRA <<< "$container_snapshot_raw"
  [ -z "$CONTAINER_SNAPSHOT_EXTRA" ] || return 1
  [[ "$CONTAINER_SNAPSHOT_ID" =~ ^[0-9a-f]{64}$ ]] || return 1
  [[ "$CONTAINER_SNAPSHOT_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
}

snapshot_is_exact_generation() {
  { [ "$EXPECTED_CONTAINER_ID" = 'pending' ] || [ "$CONTAINER_SNAPSHOT_ID" = "$EXPECTED_CONTAINER_ID" ]; } &&
    [ "$CONTAINER_SNAPSHOT_ROLE" = 'entitlement-shadow-readonly' ] &&
    [ "$CONTAINER_SNAPSHOT_RUN_ID" = "$RUN_ID" ] &&
    [ "$CONTAINER_SNAPSHOT_RUN_ATTEMPT" = "$RUN_ATTEMPT" ]
}

container_is_exact_generation() {
  capture_container_snapshot && snapshot_is_exact_generation
}

snapshot_matches_completed_lease() {
  snapshot_is_exact_generation &&
    [ "$CONTAINER_SNAPSHOT_RUNNING" = 'true' ] &&
    [ "$CONTAINER_SNAPSHOT_PAUSED" = 'false' ] &&
    [ "$CONTAINER_SNAPSHOT_IMAGE" = "$(lease_value image)" ] &&
    [ "$CONTAINER_SNAPSHOT_WORKFLOW_SHA" = "$(lease_value workflow_sha)" ] &&
    [ "$CONTAINER_SNAPSHOT_DEPLOYED_SHA" = "$(lease_value deployed_sha)" ] &&
    [ "$CONTAINER_SNAPSHOT_POLICY_VERSION" = "$POLICY_VERSION" ]
}

container_matches_completed_lease() {
  capture_container_snapshot &&
    snapshot_matches_completed_lease &&
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
    exact_env ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_BASIS_POINTS 2000 &&
    capture_container_snapshot &&
    snapshot_matches_completed_lease
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
  if ! cp "$source_file" "$latest_tmp" || ! chmod 600 "$latest_tmp" || ! mv "$latest_tmp" "$LATEST_AUDIT_FILE"; then
    rm -f -- "$latest_tmp"
    return 1
  fi
}

publish_keyed_audit() {
  source_file="$1"
  target="$2"
  if [ -e "$target" ]; then
    cmp -s "$source_file" "$target"
    return
  fi

  target_tmp="$(mktemp "${target}.XXXXXX")"
  if ! cp "$source_file" "$target_tmp" || ! chmod 600 "$target_tmp"; then
    rm -f -- "$target_tmp"
    return 1
  fi
  # The per-generation lock serializes normal publishers.  Keep the existence
  # recheck so a manually recovered receipt can never be overwritten.
  if [ -e "$target" ]; then
    cmp -s "$source_file" "$target" || {
      rm -f -- "$target_tmp"
      return 1
    }
    rm -f -- "$target_tmp"
    return 0
  fi
  mv "$target_tmp" "$target" || {
    rm -f -- "$target_tmp"
    return 1
  }
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
    existing_policy="$(sed -n 's/^policy_version=//p' "$disabled_audit" 2>/dev/null || true)"
    existing_run_id="$(sed -n 's/^workflow_run_id=//p' "$disabled_audit" 2>/dev/null || true)"
    existing_run_attempt="$(sed -n 's/^workflow_run_attempt=//p' "$disabled_audit" 2>/dev/null || true)"
    [ "$existing_action" = "$action" ] && [ "$existing_mode" = 'disabled' ] && \
      [ "$existing_policy" = "$POLICY_VERSION" ] && [ "$existing_run_id" = "$RUN_ID" ] && \
      [ "$existing_run_attempt" = "$RUN_ATTEMPT" ] || {
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
  if ! cp "$LEASE_FILE" "$audit_tmp" || ! chmod 600 "$audit_tmp"; then
    rm -f -- "$audit_tmp"
    return 1
  fi
  for target in "$RUN_AUDIT_FILE" "$AUDIT_FILE"; do
    publish_keyed_audit "$audit_tmp" "$target" || {
      rm -f -- "$audit_tmp"
      return 1
    }
  done
  rm -f -- "$audit_tmp"
  replace_latest "$RUN_AUDIT_FILE"
}

remove_matching_active_audits() {
  preimage_file="$1"
  for target in "$RUN_AUDIT_FILE" "$AUDIT_FILE" "$LATEST_AUDIT_FILE"; do
    [ -e "$target" ] || continue
    if cmp -s "$target" "$preimage_file"; then
      rm -f -- "$target" || return 1
      continue
    else
      cmp_rc=$?
    fi
    [ "$cmp_rc" = '1' ] || return 1
  done
}

remove_lease_matching_cleanup_intent() {
  [ -e "$LEASE_FILE" ] || return 0
  if cmp -s "$LEASE_FILE" "$CLEANUP_INTENT_FILE"; then
    rm -f -- "$LEASE_FILE"
    return
  else
    cmp_rc=$?
  fi
  [ "$cmp_rc" = '1' ] || return 1
  return 1
}

cleanup_failed_enable_generation() {
  ensure_cleanup_intent || return 1
  remove_matching_active_audits "$CLEANUP_INTENT_FILE" || return 1
  remove_lease_matching_cleanup_intent || return 1
  remove_own_generation_rc=0
  remove_own_generation || remove_own_generation_rc=$?
  [ "$remove_own_generation_rc" = '0' ] || return "$remove_own_generation_rc"
  write_disabled_audit AUTO_DISABLE_FAILED_ENABLE || return 1
  rm -f -- "$CLEANUP_INTENT_FILE" || return 1
  rm -f -- "$CONTROLLER_GUARD_FILE" || return 1
}

arm_expiry() {
  expires_epoch="$1"
  capture_container_snapshot || return 1
  snapshot_matches_completed_lease || return 1
  actual_id="$CONTAINER_SNAPSHOT_ID"
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

# Once cleanup intent is durable, this generation can never be reconsidered
# active.  Every controller/watchdog retry must finish the same exact cleanup.
if [ -e "$CLEANUP_INTENT_FILE" ]; then
  cleanup_intent_is_exact || fail 'failed_enable_cleanup_intent_conflict'
  cleanup_failed_enable_generation || fail 'failed_enable_cleanup_unverified'
  exit 0
fi

if [ -e "$CONTROLLER_GUARD_FILE" ]; then
  controller_guard_is_exact || fail 'failed_enable_controller_guard_conflict'
  if [ "${TEPLO_SHADOW_CONTROLLER_GUARD_HELD:-0}" != '1' ]; then
    cleanup_failed_enable_generation || fail 'failed_enable_guard_cleanup_unverified'
    exit 0
  fi
fi

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
    cleanup_failed_enable_generation || fail 'expiry_arm_and_cleanup_failed'
    fail 'expiry_watchdog_arm_failed'
  fi
  materialize_completed_audits || {
    cleanup_failed_enable_generation || fail 'conflicting_active_audit_cleanup_unverified'
    fail 'completed_audit_conflict'
  }
  watchdog_owns_guard=0
  if [ ! -e "$CONTROLLER_GUARD_FILE" ]; then
    ensure_controller_guard || fail 'watchdog_owner_death_guard_failed'
    watchdog_owns_guard=1
  fi
  if ! capture_container_snapshot || ! snapshot_matches_completed_lease; then
    cleanup_failed_enable_generation || fail 'post_audit_cleanup_unverified'
  fi
  if [ "$watchdog_owns_guard" = '1' ]; then
    rm -f -- "$CONTROLLER_GUARD_FILE" || fail 'watchdog_owner_death_guard_release_failed'
  fi
  exit 0
fi

# A prior watchdog may have been killed while removing provisional active
# receipts after the completed sidecar became invalid.  Finish that cleanup
# while the exact lease is still available for byte-for-byte fencing, then
# remove the generation and publish the disabled receipt.
if [ -r "$LEASE_FILE" ]; then
  cleanup_failed_enable_generation || fail 'bootstrap_cleanup_unverified'
else
  remove_own_generation_rc=0
  remove_own_generation || remove_own_generation_rc=$?
  [ "$remove_own_generation_rc" = '10' ] && exit 0
  [ "$remove_own_generation_rc" = '0' ] || fail 'bootstrap_container_removal_unverified'
  write_disabled_audit AUTO_DISABLE_BOOTSTRAP || fail 'bootstrap_audit_unverified'
fi

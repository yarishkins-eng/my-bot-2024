#!/usr/bin/env bash

# Protected Gate 2 runtime switch. The production dotenv file is a permanent
# fail-closed baseline and is never edited. A fixed, temporary Compose override
# changes only the five entitlement interlocks on the existing bot container.

set -Eeuo pipefail

fail() {
  printf 'STOP:%s\n' "$1" >&2
  exit 64
}

[ "$#" -eq 9 ] || fail 'usage'

readonly CONTROL_ACTION="$1"
readonly WORKFLOW_SHA="$2"
readonly WORKFLOW_RUN_ID="$3"
readonly WORKFLOW_RUN_ATTEMPT="$4"
readonly REPO_DIR="$5"
readonly STATE_DIR="$6"
readonly EXPECTED_DEPLOYED_SHA="$7"
readonly CLASSIFIER="$8"
readonly BASELINE_VERIFIER="$9"
readonly SERVICE='bot'
readonly CONTAINER='remnawave_bot'
readonly ENV_FILE="$REPO_DIR/.env"
readonly DEPLOY_STATE_FILE="$STATE_DIR/bot-production.state"
readonly MIGRATION_STATE_FILE="$STATE_DIR/bot-production.migration-recovery.state"
readonly AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${WORKFLOW_RUN_ID}.${WORKFLOW_RUN_ATTEMPT}.audit"
readonly MIN_FREE_KB=1048576

CONTROL_DIR=''
OVERRIDE_FILE=''
CURRENT_IMAGE_ID=''
CURRENT_SERVICE_IMAGE=''
ENV_FINGERPRINT_BEFORE=''
MUTATION_STARTED=0
RECOVERY_IN_PROGRESS=0

case "$CONTROL_ACTION" in
  ENABLE_SHADOW|DISABLE_SHADOW) ;;
  *) fail 'action_not_allowlisted' ;;
esac
[[ "$WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid_workflow_sha'
[[ "$EXPECTED_DEPLOYED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid_expected_deployed_sha'
[[ "$WORKFLOW_RUN_ID" =~ ^[0-9]+$ ]] || fail 'invalid_workflow_run_id'
[[ "$WORKFLOW_RUN_ATTEMPT" =~ ^[0-9]+$ ]] || fail 'invalid_workflow_run_attempt'

state_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" || true)"
  [ "$count" = '1' ] || fail "missing_or_duplicate_${key}"
  sed -n "s/^${key}=//p" "$file"
}

cleanup() {
  if [ -n "$OVERRIDE_FILE" ]; then
    rm -f -- "$OVERRIDE_FILE"
  fi
  if [ -n "$CONTROL_DIR" ]; then
    rmdir -- "$CONTROL_DIR" 2>/dev/null || true
  fi
}

write_override() {
  mode="$1"
  case "$mode" in
    enabled)
      shadow='true'
      kill_switch='false'
      ;;
    disabled)
      shadow='false'
      kill_switch='true'
      ;;
    *) fail 'invalid_internal_mode' ;;
  esac
  umask 077
  override_tmp="$(mktemp "$CONTROL_DIR/override.XXXXXX")"
  printf '%s\n' \
    'services:' \
    '  bot:' \
    '    environment:' \
    "      ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED: 'false'" \
    "      ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED: 'false'" \
    "      ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED: 'false'" \
    "      ENTITLEMENT_AUTHORITY_SHADOW_ENABLED: '${shadow}'" \
    "      ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH: '${kill_switch}'" > "$override_tmp"
  chmod 600 "$override_tmp"
  mv "$override_tmp" "$OVERRIDE_FILE"
}

compose_control() {
  docker compose -f "$REPO_DIR/docker-compose.yml" -f "$OVERRIDE_FILE" "$@"
}

container_env_has_exactly() {
  expected="$1"
  opposite="$2"
  matches="$(docker inspect --format "{{range .Config.Env}}{{if eq . \"${expected}\"}}x{{end}}{{end}}" "$CONTAINER")"
  conflicts="$(docker inspect --format "{{range .Config.Env}}{{if eq . \"${opposite}\"}}x{{end}}{{end}}" "$CONTAINER")"
  [ "$matches" = 'x' ] && [ -z "$conflicts" ]
}

verify_container_flags() {
  mode="$1"
  container_env_has_exactly 'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=false' \
    'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=true' || return 1
  container_env_has_exactly 'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false' \
    'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=true' || return 1
  container_env_has_exactly 'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=false' \
    'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=true' || return 1
  if [ "$mode" = 'enabled' ]; then
    container_env_has_exactly 'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=true' \
      'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false' || return 1
    container_env_has_exactly 'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=false' \
      'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=true' || return 1
  else
    container_env_has_exactly 'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false' \
      'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=true' || return 1
    container_env_has_exactly 'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=true' \
      'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=false' || return 1
  fi
}

wait_for_health() {
  attempts=0
  while [ "$attempts" -lt 120 ]; do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$health" = 'healthy' ] && return 0
    attempts=$((attempts + 1))
    sleep 2
  done
  return 1
}

verify_runtime() {
  mode="$1"
  [ "$(docker inspect --format '{{.Image}}' "$CONTAINER")" = "$CURRENT_IMAGE_ID" ] || return 1
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER")" = 'healthy' ] || return 1
  verify_container_flags "$mode" || return 1
  process_count="$(docker top "$CONTAINER" -eo args | awk 'NR > 1 && $0 ~ /python .*main.py/ { count += 1 } END { print count + 0 }')"
  [ "$process_count" = '1' ] || return 1
  started_at="$(docker inspect --format '{{.State.StartedAt}}' "$CONTAINER")" || return 1
  if [ "$mode" = 'enabled' ]; then
    docker logs --since "$started_at" "$CONTAINER" 2>&1 | grep -F 'Read-only shadow запущен' >/dev/null || return 1
  else
    docker logs --since "$started_at" "$CONTAINER" 2>&1 | grep -F 'SHADOW=false' >/dev/null || return 1
  fi
}

recreate_with_mode() {
  mode="$1"
  write_override "$mode" || return 1
  compose_control stop "$SERVICE" || return 1
  compose_control create --force-recreate --no-build --no-deps "$SERVICE" || return 1
  [ "$(docker inspect --format '{{.Image}}' "$CONTAINER")" = "$CURRENT_IMAGE_ID" ] || return 1
  verify_container_flags "$mode" || return 1
  docker start "$CONTAINER" >/dev/null || return 1
  wait_for_health || return 1
  verify_runtime "$mode" || return 1
}

write_audit() {
  mode="$1"
  install -d -m 700 "$STATE_DIR"
  audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-control.XXXXXX")"
  printf 'format_version=1\nphase=completed\naction=%s\nruntime_mode=%s\nworkflow_sha=%s\ndeployed_sha=%s\nimage=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\ncompleted_at=%s\n' \
    "$CONTROL_ACTION" "$mode" "$WORKFLOW_SHA" "$deployed_sha" "$CURRENT_IMAGE_ID" "$WORKFLOW_RUN_ID" \
    "$WORKFLOW_RUN_ATTEMPT" \
    "$(date --iso-8601=seconds)" > "$audit_tmp"
  chmod 600 "$audit_tmp"
  if [ -e "$RUN_AUDIT_FILE" ]; then
    rm -f -- "$audit_tmp"
    return 1
  fi
  cp "$audit_tmp" "$RUN_AUDIT_FILE" || return 1
  chmod 600 "$RUN_AUDIT_FILE" || return 1
  mv "$audit_tmp" "$AUDIT_FILE" || return 1
}

recover_disabled() {
  original_rc=$?
  trap - ERR
  set +e
  if [ "$MUTATION_STARTED" = '1' ] && [ "$RECOVERY_IN_PROGRESS" = '0' ]; then
    RECOVERY_IN_PROGRESS=1
    echo 'Control action failed; forcing the reviewed disabled runtime matrix.' >&2
    recreate_with_mode disabled
    recovery_rc=$?
    if [ "$recovery_rc" = '0' ]; then
      write_audit disabled
      recovery_rc=$?
    fi
    current_env_fingerprint="$(sha256sum "$ENV_FILE" 2>/dev/null | awk '{ print $1 }')"
    if [ "$current_env_fingerprint" != "$ENV_FINGERPRINT_BEFORE" ]; then
      recovery_rc=1
    fi
    if [ "$recovery_rc" != '0' ]; then
      docker stop "$CONTAINER" >/dev/null 2>&1 || true
      echo 'CRITICAL: fail-safe disable could not be proven; bot was left stopped.' >&2
      cleanup
      exit 91
    fi
    echo 'Fail-safe disabled runtime restored and verified.' >&2
  fi
  cleanup
  exit "$original_rc"
}

trap cleanup EXIT

cd "$REPO_DIR"
[ -z "$(git status --porcelain)" ] || fail 'server_worktree_not_exact'
[ "$(df -Pk "$REPO_DIR" | awk 'NR == 2 { print $4 }')" -ge "$MIN_FREE_KB" ] || fail 'insufficient_disk'
[ -x "$BASELINE_VERIFIER" ] || fail 'baseline_verifier_not_executable'
git fetch --no-tags origin '+refs/heads/main:refs/remotes/origin/main'
current_main_sha="$(git rev-parse origin/main)"
[ -r "$DEPLOY_STATE_FILE" ] || fail 'deploy_state_missing'
deployed_sha="$(state_value sha "$DEPLOY_STATE_FILE")"
[ "$(git rev-parse HEAD)" = "$deployed_sha" ] || fail 'server_source_mismatch'
CURRENT_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CONTAINER")"
CURRENT_SERVICE_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")"
[[ "$CURRENT_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'invalid_image_id'
deploy_state_image="$(state_value image "$DEPLOY_STATE_FILE")"
[ -x "$CLASSIFIER" ] || fail 'control_classifier_not_executable'
classification="$("$CLASSIFIER" "$CONTROL_ACTION" "$WORKFLOW_SHA" "$EXPECTED_DEPLOYED_SHA" \
  "$current_main_sha" "$deployed_sha" "$deploy_state_image" "$CURRENT_IMAGE_ID")"
if [ "$classification" = 'disable_compatible_check_required' ]; then
  git cat-file -e "${WORKFLOW_SHA}^{commit}" || fail 'workflow_commit_missing'
  if ! git merge-base --is-ancestor "$WORKFLOW_SHA" "$current_main_sha" && \
    ! git merge-base --is-ancestor "$current_main_sha" "$WORKFLOW_SHA"; then
    fail 'workflow_and_main_diverged'
  fi
fi
[ "$(git rev-parse "$deployed_sha":app/services/entitlement_authority/shadow_runtime.py)" = \
  "$(git rev-parse "$WORKFLOW_SHA":app/services/entitlement_authority/shadow_runtime.py)" ] || fail 'shadow_code_mismatch'
[ "$(git rev-parse "$deployed_sha":main.py)" = "$(git rev-parse "$WORKFLOW_SHA":main.py)" ] || fail 'shadow_wiring_mismatch'
[ "$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER")" = 'healthy' ] || fail 'bot_not_healthy'
[ "$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")" = "$CURRENT_SERVICE_IMAGE" ] || fail 'service_image_mismatch'
[ -r "$MIGRATION_STATE_FILE" ] || fail 'migration_state_missing'
current_migration_phase="$(state_value phase "$MIGRATION_STATE_FILE")"
if [ "$CONTROL_ACTION" = 'ENABLE_SHADOW' ]; then
  [ "$current_migration_phase" = 'completed' ] || fail 'migration_state_not_completed'
else
  case "$current_migration_phase" in
    prepared|completed|recovered) ;;
    *) fail 'migration_state_invalid' ;;
  esac
fi
actual_schema="$(docker compose -f docker-compose.yml exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "BEGIN READ ONLY; SELECT version_num FROM alembic_version ORDER BY version_num; COMMIT"' \
  | sort | paste -sd, -)"
[ "$actual_schema" = '0103' ] || fail 'schema_not_0103'
"$BASELINE_VERIFIER" --env-file "$ENV_FILE" >/dev/null
ENV_FINGERPRINT_BEFORE="$(sha256sum "$ENV_FILE" | awk '{ print $1 }')"
[[ "$ENV_FINGERPRINT_BEFORE" =~ ^[0-9a-f]{64}$ ]] || fail 'env_fingerprint_failed'

install -d -m 700 "$STATE_DIR"
CONTROL_DIR="$(mktemp -d "$STATE_DIR/entitlement-shadow-control.XXXXXX")"
chmod 700 "$CONTROL_DIR"
OVERRIDE_FILE="$CONTROL_DIR/override.yml"

if [ "$CONTROL_ACTION" = 'ENABLE_SHADOW' ]; then
  desired_mode='enabled'
else
  desired_mode='disabled'
fi

MUTATION_STARTED=1
trap recover_disabled ERR
recreate_with_mode "$desired_mode"
[ "$(sha256sum "$ENV_FILE" | awk '{ print $1 }')" = "$ENV_FINGERPRINT_BEFORE" ]
write_audit "$desired_mode"
MUTATION_STARTED=0
trap - ERR
cleanup
trap - EXIT
echo "Gate 2 shadow control completed: action=$CONTROL_ACTION runtime=$desired_mode deployed_sha=$deployed_sha"

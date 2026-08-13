#!/usr/bin/env bash

# Protected ENABLE path for the isolated Gate 2 read-only sidecar.  It never
# recreates or restarts the production bot and never writes dotenv, DB, Panel,
# payment, webhook, notification, or user/business data.

set -Eeuo pipefail

fail() {
  printf 'STOP:%s\n' "$1" >&2
  if [ "${MUTATION_STARTED:-0}" = '1' ]; then
    cleanup_runtime
  fi
  exit 64
}

[ "$#" -eq 12 ] || fail 'usage'
readonly ACTION="$1"
readonly WORKFLOW_SHA="$2"
readonly RUN_ID="$3"
readonly RUN_ATTEMPT="$4"
readonly EXPECTED_DEPLOYED_SHA="$5"
readonly ACTOR="$6"
readonly RELEASE_CARD="$7"
readonly REPO_DIR="$8"
readonly STATE_DIR="$9"
readonly BASELINE_VERIFIER="${10}"
readonly SIDECAR_ENTRYPOINT="${11}"
readonly WATCHDOG="${12}"
readonly RUNTIME_DIR='/var/lib/teplo-vpn/entitlement-shadow-runtime'
readonly SIDECAR='teplo_entitlement_shadow'
readonly BOT_CONTAINER='remnawave_bot'
readonly DEPLOY_STATE_FILE="$STATE_DIR/bot-production.state"
readonly MIGRATION_STATE_FILE="$STATE_DIR/bot-production.migration-recovery.state"
readonly LEASE_FILE="$RUNTIME_DIR/lease.state"
readonly AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.state"
readonly RUN_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly WATCHDOG_AUDIT_FILE="$STATE_DIR/bot-production.entitlement-shadow-watchdog.${RUN_ID}.${RUN_ATTEMPT}.audit"
readonly LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.lock"
readonly WATCHDOG_UNIT="teplo-entitlement-shadow-watchdog-${RUN_ID}-${RUN_ATTEMPT}"
readonly BOOTSTRAP_SECONDS=300
readonly OBSERVATION_SECONDS=604800
readonly MIN_FREE_KB=1048576
MUTATION_STARTED=0
SIDECAR_ENV_FILE=''

[ "$ACTION" = 'ENABLE_SHADOW' ] || fail 'action_not_enable'
[[ "$WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid_workflow_sha'
[[ "$EXPECTED_DEPLOYED_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'invalid_expected_deployed_sha'
[[ "$RUN_ID" =~ ^[0-9]+$ ]] || fail 'invalid_run_id'
[[ "$RUN_ATTEMPT" =~ ^[0-9]+$ ]] || fail 'invalid_run_attempt'
[[ "$ACTOR" =~ ^[A-Za-z0-9-]{1,39}$ ]] || fail 'invalid_actor'
[[ "$RELEASE_CARD" =~ ^[A-Za-z0-9._:/@-]{1,160}$ ]] || fail 'invalid_release_card'
[ "$REPO_DIR" = '/opt/remnawave-bedolaga-telegram-bot' ] || fail 'invalid_repo_dir'
[ "$STATE_DIR" = '/var/lib/teplo-vpn/deploy-state' ] || fail 'invalid_state_dir'

state_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" || true)"
  [ "$count" = '1' ] || fail "missing_or_duplicate_${key}"
  sed -n "s/^${key}=//p" "$file"
}

audit_value() {
  key="$1"
  file="$2"
  count="$(grep -Ec "^${key}=[^[:space:]]+$" "$file" 2>/dev/null || true)"
  [ "$count" = '1' ] || return 1
  sed -n "s/^${key}=//p" "$file"
}

write_lease() {
  phase="$1"
  expires="$2"
  completed_at="$3"
  lease_tmp="$(mktemp "$RUNTIME_DIR/lease.XXXXXX")"
  printf 'format_version=2\nphase=%s\naction=ENABLE_SHADOW\nruntime_mode=enabled\nworkflow_sha=%s\ndeployed_sha=%s\nimage=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\napproval_actor=%s\nrelease_card_reference=%s\nexpires_epoch=%s\ncompleted_at=%s\n' \
    "$phase" "$WORKFLOW_SHA" "$deployed_sha" "$CURRENT_IMAGE_ID" "$RUN_ID" "$RUN_ATTEMPT" \
    "$ACTOR" "$RELEASE_CARD" "$expires" "$completed_at" > "$lease_tmp"
  chmod 444 "$lease_tmp"
  mv "$lease_tmp" "$LEASE_FILE"
}

exact_env() {
  key="$1"
  expected="$2"
  [ "$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$SIDECAR" | grep -Fxc "${key}=${expected}" || true)" = '1' ]
}

verify_fixed_environment() {
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
    exact_env BOT_TOKEN 123456789:shadow-sidecar-does-not-use-telegram &&
    exact_env ADMIN_NOTIFICATIONS_ENABLED false &&
    exact_env REMNAWAVE_WEBHOOK_ENABLED false &&
    exact_env REMNAWAVE_AUTO_SYNC_ENABLED false &&
    exact_env ACCESS_POINT_INVENTORY_DRY_RUN_ENABLED false &&
    exact_env ACCESS_POINT_INVENTORY_CATALOG_APPLY_ENABLED false &&
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
    exact_env MULTI_TARIFF_ENABLED false &&
    exact_env DEFAULT_TRAFFIC_RESET_STRATEGY MONTH &&
    exact_env DEVICES_SELECTION_ENABLED true &&
    exact_env DEVICES_SELECTION_DISABLED_AMOUNT ''
}

authority_counts() {
  docker compose -f "$REPO_DIR/docker-compose.yml" exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "BEGIN READ ONLY; SELECT concat_ws('"'"','"'"', (SELECT count(*) FROM entitlement_identities), (SELECT count(*) FROM entitlement_source_revisions), (SELECT count(*) FROM entitlement_overlays), (SELECT count(*) FROM entitlement_projection_commands), (SELECT count(*) FROM entitlement_observations), (SELECT count(*) FROM entitlement_webhook_inbox), (SELECT count(*) FROM entitlement_notification_intents), (SELECT count(*) FROM entitlement_cleanup_commands), (SELECT count(*) FROM entitlement_cleanup_tombstones)); COMMIT"'
}

cleanup_runtime() {
  set +e
  [ -z "$SIDECAR_ENV_FILE" ] || rm -f -- "$SIDECAR_ENV_FILE"
  rm -f -- "$LEASE_FILE"
  docker rm -f "$SIDECAR" >/dev/null 2>&1 || true
  if [ -n "${RUN_AUDIT_FILE:-}" ]; then
    audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-control.XXXXXX" 2>/dev/null || true)"
    if [ -n "$audit_tmp" ]; then
      printf 'format_version=2\nphase=completed\naction=AUTO_DISABLE_FAILED_ENABLE\nruntime_mode=disabled\nworkflow_sha=%s\nworkflow_run_id=%s\nworkflow_run_attempt=%s\napproval_actor=%s\nrelease_card_reference=%s\ncompleted_at=%s\n' \
        "$WORKFLOW_SHA" "$RUN_ID" "$RUN_ATTEMPT" "$ACTOR" "$RELEASE_CARD" \
        "$(date --iso-8601=seconds)" > "$audit_tmp"
      chmod 600 "$audit_tmp"
      cp "$audit_tmp" "$RUN_AUDIT_FILE"
      chmod 600 "$RUN_AUDIT_FILE"
      mv "$audit_tmp" "$AUDIT_FILE"
    fi
  fi
  set -e
}

cleanup_failed_enable() {
  original_rc=$?
  trap - ERR
  cleanup_runtime
  exit "$original_rc"
}

build_sidecar_env_file() {
  SIDECAR_ENV_FILE="$STATE_DIR/entitlement-shadow-secrets-${RUN_ID}-${RUN_ATTEMPT}.env"
  rm -f -- "$SIDECAR_ENV_FILE"
  umask 077
  : > "$SIDECAR_ENV_FILE"
  chmod 600 "$SIDECAR_ENV_FILE"
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$BOT_CONTAINER" | awk '
    /^(POSTGRES_DB|POSTGRES_USER|POSTGRES_PASSWORD|REMNAWAVE_API_URL|REMNAWAVE_API_KEY|REMNAWAVE_SECRET_KEY|REMNAWAVE_USERNAME|REMNAWAVE_PASSWORD|REMNAWAVE_CADDY_TOKEN|REMNAWAVE_AUTH_TYPE|TZ)=/ { print }
  ' > "$SIDECAR_ENV_FILE"
  for required_key in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD REMNAWAVE_API_URL REMNAWAVE_API_KEY; do
    [ "$(grep -Ec "^${required_key}=.+$" "$SIDECAR_ENV_FILE" || true)" = '1' ] || fail "missing_or_duplicate_${required_key}"
  done
}

cd "$REPO_DIR"
[ -z "$(git status --porcelain)" ] || fail 'server_worktree_not_exact'
[ "$(df -Pk "$REPO_DIR" | awk 'NR == 2 { print $4 }')" -ge "$MIN_FREE_KB" ] || fail 'insufficient_disk'
[ -x "$BASELINE_VERIFIER" ] || fail 'baseline_verifier_not_executable'
[ -x "$SIDECAR_ENTRYPOINT" ] || fail 'sidecar_entrypoint_not_executable'
[ -x "$WATCHDOG" ] || fail 'watchdog_not_executable'
git fetch --no-tags origin '+refs/heads/main:refs/remotes/origin/main'
[ "$(git rev-parse origin/main)" = "$WORKFLOW_SHA" ] || fail 'workflow_is_not_current_main'
[ "$(git rev-parse HEAD)" = "$WORKFLOW_SHA" ] || fail 'enable_requires_deployed_workflow_sha'
[ -r "$DEPLOY_STATE_FILE" ] || fail 'deploy_state_missing'
deployed_sha="$(state_value sha "$DEPLOY_STATE_FILE")"
[ "$deployed_sha" = "$EXPECTED_DEPLOYED_SHA" ] || fail 'owner_expected_deployed_sha_mismatch'
[ "$deployed_sha" = "$WORKFLOW_SHA" ] || fail 'enable_requires_exact_deployed_main'
CURRENT_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$BOT_CONTAINER")"
[[ "$CURRENT_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fail 'invalid_image_id'
[ "$(state_value image "$DEPLOY_STATE_FILE")" = "$CURRENT_IMAGE_ID" ] || fail 'deploy_state_image_mismatch'
[ "$(docker inspect --format '{{.State.Health.Status}}' "$BOT_CONTAINER")" = 'healthy' ] || fail 'bot_not_healthy'
BOT_CONTAINER_ID="$(docker inspect --format '{{.Id}}' "$BOT_CONTAINER")"
BOT_STARTED_AT="$(docker inspect --format '{{.State.StartedAt}}' "$BOT_CONTAINER")"
[ -r "$MIGRATION_STATE_FILE" ] || fail 'migration_state_missing'
[ "$(state_value phase "$MIGRATION_STATE_FILE")" = 'completed' ] || fail 'migration_state_not_completed'
actual_schema="$(docker compose -f docker-compose.yml exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "BEGIN READ ONLY; SELECT version_num FROM alembic_version ORDER BY version_num; COMMIT"' \
  | sort | paste -sd, -)"
[ "$actual_schema" = '0103' ] || fail 'schema_not_0103'
"$BASELINE_VERIFIER" --env-file "$REPO_DIR/.env" >/dev/null
ENV_FINGERPRINT_BEFORE="$(sha256sum "$REPO_DIR/.env" | awk '{ print $1 }')"
AUTHORITY_COUNTS_BEFORE="$(authority_counts)"
[ "$AUTHORITY_COUNTS_BEFORE" = '0,0,0,0,0,0,0,0,0' ] || fail 'authority_tables_not_empty'

install -d -m 700 "$STATE_DIR"
install -d -m 755 "$RUNTIME_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || fail 'control_busy'

if [ -r "$RUN_AUDIT_FILE" ]; then
  [ "$(audit_value phase "$RUN_AUDIT_FILE" || true)" = 'completed' ] || fail 'run_audit_conflict'
  [ "$(audit_value action "$RUN_AUDIT_FILE" || true)" = 'ENABLE_SHADOW' ] || fail 'run_audit_conflict'
  [ "$(audit_value runtime_mode "$RUN_AUDIT_FILE" || true)" = 'enabled' ] || fail 'run_audit_conflict'
  [ "$(audit_value workflow_sha "$RUN_AUDIT_FILE" || true)" = "$WORKFLOW_SHA" ] || fail 'run_audit_conflict'
  [ "$(audit_value approval_actor "$RUN_AUDIT_FILE" || true)" = "$ACTOR" ] || fail 'run_audit_conflict'
  [ "$(audit_value release_card_reference "$RUN_AUDIT_FILE" || true)" = "$RELEASE_CARD" ] || fail 'run_audit_conflict'
  [ -r "$LEASE_FILE" ] && cmp -s "$RUN_AUDIT_FILE" "$LEASE_FILE" || fail 'completed_lease_missing'
  [ "$(docker inspect --format '{{.State.Running}}' "$SIDECAR" 2>/dev/null || true)" = 'true' ] || fail 'completed_sidecar_not_running'
  cp "$RUN_AUDIT_FILE" "$AUDIT_FILE"
  chmod 600 "$AUDIT_FILE"
  printf 'Gate 2 isolated read-only shadow was already enabled by this approved run.\n'
  exit 0
fi

if [ -r "$LEASE_FILE" ]; then
  [ "$(state_value workflow_run_id "$LEASE_FILE")" = "$RUN_ID" ] || fail 'another_shadow_control_run_active'
  [ "$(state_value workflow_run_attempt "$LEASE_FILE")" = "$RUN_ATTEMPT" ] || fail 'another_shadow_control_run_active'
  lease_phase="$(state_value phase "$LEASE_FILE")"
  if [ "$lease_phase" = 'completed' ]; then
    [ "$(state_value action "$LEASE_FILE")" = 'ENABLE_SHADOW' ] || fail 'completed_lease_conflict'
    [ "$(state_value workflow_sha "$LEASE_FILE")" = "$WORKFLOW_SHA" ] || fail 'completed_lease_conflict'
    [ "$(state_value approval_actor "$LEASE_FILE")" = "$ACTOR" ] || fail 'completed_lease_conflict'
    [ "$(state_value release_card_reference "$LEASE_FILE")" = "$RELEASE_CARD" ] || fail 'completed_lease_conflict'
    [ "$(docker inspect --format '{{.State.Running}}' "$SIDECAR" 2>/dev/null || true)" = 'true' ] || fail 'completed_sidecar_not_running'
    cp "$LEASE_FILE" "$RUN_AUDIT_FILE"
    chmod 600 "$RUN_AUDIT_FILE"
    cp "$RUN_AUDIT_FILE" "$AUDIT_FILE"
    chmod 600 "$AUDIT_FILE"
    printf 'Gate 2 isolated read-only shadow completion was recovered from its durable lease.\n'
    exit 0
  fi
  [ "$lease_phase" = 'prepared' ] || fail 'lease_phase_conflict'
elif docker inspect "$SIDECAR" >/dev/null 2>&1; then
  fail 'sidecar_without_control_lease'
fi

systemctl stop "${WATCHDOG_UNIT}.timer" "${WATCHDOG_UNIT}.service" >/dev/null 2>&1 || true
MUTATION_STARTED=1
trap cleanup_failed_enable ERR
rm -f -- "$LEASE_FILE"
docker rm -f "$SIDECAR" >/dev/null 2>&1 || true
SIDECAR_INSTALLED="$STATE_DIR/entitlement-shadow-sidecar-${WORKFLOW_SHA}.py"
WATCHDOG_INSTALLED="$STATE_DIR/entitlement-shadow-watchdog-${WORKFLOW_SHA}.sh"
install -m 555 "$SIDECAR_ENTRYPOINT" "$SIDECAR_INSTALLED"
install -m 700 "$WATCHDOG" "$WATCHDOG_INSTALLED"
prepared_expires="$(( $(date +%s) + BOOTSTRAP_SECONDS ))"
write_lease prepared "$prepared_expires" pending

systemd-run --quiet --unit="$WATCHDOG_UNIT" --on-active="${BOOTSTRAP_SECONDS}s" --property=Type=oneshot \
  "$WATCHDOG_INSTALLED" "$LEASE_FILE" "$SIDECAR" "$RUN_ID" "$RUN_ATTEMPT" "$WATCHDOG_AUDIT_FILE" \
  "$STATE_DIR/entitlement-shadow-secrets-${RUN_ID}-${RUN_ATTEMPT}.env"
build_sidecar_env_file
docker create \
  --name "$SIDECAR" \
  --restart=no \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --network remnawave-bedolaga-telegram-bot_bot_network \
  --env-file "$SIDECAR_ENV_FILE" \
  --env DOCKER_ENV=true \
  --env DATABASE_MODE=auto \
  --env POSTGRES_HOST=postgres \
  --env POSTGRES_PORT=5432 \
  --env DATABASE_POOL_SIZE=2 \
  --env DATABASE_MAX_OVERFLOW=0 \
  --env DATABASE_POOL_TIMEOUT=5 \
  --env REMNAWAVE_API_CONNECT_TIMEOUT=4 \
  --env REMNAWAVE_API_TOTAL_TIMEOUT=4 \
  --env BOT_TOKEN=123456789:shadow-sidecar-does-not-use-telegram \
  --env ADMIN_NOTIFICATIONS_ENABLED=false \
  --env REMNAWAVE_WEBHOOK_ENABLED=false \
  --env REMNAWAVE_AUTO_SYNC_ENABLED=false \
  --env ACCESS_POINT_INVENTORY_DRY_RUN_ENABLED=false \
  --env ACCESS_POINT_INVENTORY_CATALOG_APPLY_ENABLED=false \
  --env ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=false \
  --env ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false \
  --env ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=false \
  --env ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=true \
  --env ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=false \
  --env ENTITLEMENT_AUTHORITY_SHADOW_COHORT_BASIS_POINTS=1000 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_IDENTITIES_PER_CYCLE=18 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_SCHEDULE_SECONDS=900 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_PANEL_READS_PER_MINUTE=12 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_PANEL_TIMEOUT_SECONDS=4 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_DB_STATEMENT_TIMEOUT_MS=5000 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CYCLE_SECONDS=180 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MIN_RATIO_SAMPLE=10 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERRORS=2 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERROR_BASIS_POINTS=1000 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_COUNT=2 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_BASIS_POINTS=1000 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_COUNT=2 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_BASIS_POINTS=1000 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_COUNT=4 \
  --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_BASIS_POINTS=2000 \
  --env MULTI_TARIFF_ENABLED=false \
  --env DEFAULT_TRAFFIC_RESET_STRATEGY=MONTH \
  --env DEVICES_SELECTION_ENABLED=true \
  --env DEVICES_SELECTION_DISABLED_AMOUNT= \
  --env TEPLO_SHADOW_LEASE_FILE=/run/teplo-shadow/lease.state \
  --env TEPLO_SHADOW_WORKFLOW_SHA="$WORKFLOW_SHA" \
  --env TEPLO_SHADOW_DEPLOYED_SHA="$deployed_sha" \
  --env TEPLO_SHADOW_IMAGE_ID="$CURRENT_IMAGE_ID" \
  --env TEPLO_SHADOW_WORKFLOW_RUN_ID="$RUN_ID" \
  --env TEPLO_SHADOW_WORKFLOW_RUN_ATTEMPT="$RUN_ATTEMPT" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env PYTHONUNBUFFERED=1 \
  --volume "$RUNTIME_DIR:/run/teplo-shadow:ro" \
  --volume "$SIDECAR_INSTALLED:/app/shadow-sidecar-entrypoint.py:ro" \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --pids-limit 128 \
  --memory 256m \
  --cpus 0.5 \
  --label teplo.role=entitlement-shadow-readonly \
  --label "teplo.workflow_sha=$WORKFLOW_SHA" \
  --label "teplo.deployed_sha=$deployed_sha" \
  --label "teplo.workflow_run_id=$RUN_ID" \
  --label "teplo.workflow_run_attempt=$RUN_ATTEMPT" \
  "$CURRENT_IMAGE_ID" python /app/shadow-sidecar-entrypoint.py >/dev/null
rm -f -- "$SIDECAR_ENV_FILE"
SIDECAR_ENV_FILE=''
docker network connect remnawave-network "$SIDECAR"
[ "$(docker inspect --format '{{.Image}}' "$SIDECAR")" = "$CURRENT_IMAGE_ID" ] || fail 'sidecar_image_mismatch'
[ "$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$SIDECAR")" = 'no' ] || fail 'sidecar_restart_policy_not_disabled'
verify_fixed_environment || fail 'sidecar_policy_mismatch'
SIDE_STARTED_AT="$(date --iso-8601=seconds)"
docker start "$SIDECAR" >/dev/null

cycle_seen=0
for _ in $(seq 1 115); do
  [ "$(docker inspect --format '{{.State.Running}}' "$SIDECAR" 2>/dev/null || true)" = 'true' ] || fail 'sidecar_stopped_before_first_cycle'
  sidecar_logs="$(docker logs --since "$SIDE_STARTED_AT" "$SIDECAR" 2>&1 || true)"
  if printf '%s\n' "$sidecar_logs" | grep -Eq 'entitlement_shadow_circuit_open|entitlement_shadow_sidecar_(stopped|refused)|entitlement_shadow_lease_lost'; then
    fail 'sidecar_failed_before_first_cycle'
  fi
  if printf '%s\n' "$sidecar_logs" | grep -F 'entitlement_shadow_cycle' >/dev/null; then
    cycle_seen=1
    break
  fi
  sleep 2
done
[ "$cycle_seen" = '1' ] || fail 'first_shadow_cycle_timeout'
[ "$(docker inspect --format '{{.Id}}' "$BOT_CONTAINER")" = "$BOT_CONTAINER_ID" ] || fail 'bot_container_changed'
[ "$(docker inspect --format '{{.State.StartedAt}}' "$BOT_CONTAINER")" = "$BOT_STARTED_AT" ] || fail 'bot_restarted'
[ "$(docker inspect --format '{{.Image}}' "$BOT_CONTAINER")" = "$CURRENT_IMAGE_ID" ] || fail 'bot_image_changed'
[ "$(sha256sum "$REPO_DIR/.env" | awk '{ print $1 }')" = "$ENV_FINGERPRINT_BEFORE" ] || fail 'dotenv_changed'
[ "$(authority_counts)" = "$AUTHORITY_COUNTS_BEFORE" ] || fail 'authority_tables_changed'

completed_expires="$(( $(date +%s) + OBSERVATION_SECONDS ))"
write_lease completed "$completed_expires" "$(date --iso-8601=seconds)"
sleep 3
[ "$(docker inspect --format '{{.State.Running}}' "$SIDECAR")" = 'true' ] || fail 'sidecar_not_running_after_commit'

audit_tmp="$(mktemp "$STATE_DIR/bot-production.entitlement-shadow-control.XXXXXX")"
cp "$LEASE_FILE" "$audit_tmp"
chmod 600 "$audit_tmp"
if [ -e "$RUN_AUDIT_FILE" ]; then
  cmp -s "$audit_tmp" "$RUN_AUDIT_FILE" || fail 'run_audit_conflict'
  rm -f -- "$audit_tmp"
else
  cp "$audit_tmp" "$RUN_AUDIT_FILE"
  chmod 600 "$RUN_AUDIT_FILE"
  mv "$audit_tmp" "$AUDIT_FILE"
fi
MUTATION_STARTED=0
trap - ERR
printf 'Gate 2 isolated read-only shadow enabled after first successful cycle.\n'

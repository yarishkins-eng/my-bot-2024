#!/usr/bin/env bash
set -Eeuo pipefail

readonly FIXED_NAME='teplo-entitlement-shadow-one-shot'
readonly ROLE_LABEL='teplo.role=entitlement-shadow-one-shot'
readonly COMPATIBLE_SHA='103094b96f96a412463753e56e3d996311b182ec'
readonly COMPATIBLE_IMAGE='sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e'
readonly REPO_DIR='/opt/remnawave-bedolaga-telegram-bot'
readonly STATE_FILE='/var/lib/teplo-vpn/deploy-state/bot-production.state'
readonly ENV_FINGERPRINT='dc35bf7aa92d570c5f190b3e7ccb8e2f22aa87b5d3d46f9277d63252fbd1057c'
readonly CYCLE_SECONDS='180'
readonly TERM_SECONDS='195'
readonly KILL_AFTER_SECONDS='10'
readonly ABSENT_BY_SECONDS='210'

require_exact_role() {
  local actual_role
  actual_role="$(docker inspect --format '{{ index .Config.Labels "teplo.role" }}' "$FIXED_NAME")"
  if [ "$actual_role" != 'entitlement-shadow-one-shot' ]; then
    echo 'foreign fixed-name container; refusing action' >&2
    return 1
  fi
}

validate_evidence() {
  jq -ceS '
    def count: type == "number" and . == floor and . >= 0 and . <= 100;
    def counter_map($allowed):
      type == "object" and all(to_entries[]; (.key as $key | $allowed | index($key)) != null and (.value | count));
    select(
      type == "object" and
      keys == (["event","schema","sampled","exact","drift","missing","panel_read_errors",
                "contract_errors","owner_mismatches","comparator_instability","rate_limit_violations",
                "critical_drift","mismatch_fields","cohorts","elapsed_ms","stopped","stop_reason"] | sort) and
      .event == "entitlement_shadow_one_shot_complete" and
      .schema == "entitlement_shadow_metrics_v1" and
      (.sampled | count) and (.exact | count) and (.drift | count) and (.missing | count) and
      (.panel_read_errors | count) and (.contract_errors | count) and (.owner_mismatches | count) and
      (.comparator_instability | count) and (.rate_limit_violations | count) and (.critical_drift | count) and
      (.elapsed_ms | type == "number" and . == floor and . >= 0 and . <= 180000) and
      (.stopped | type == "boolean") and
      (.stop_reason as $reason |
        ["none","multiple_current_subscriptions","owner_uuid_binding_mismatch","cross_owner_panel_uuid",
         "legacy_shadow_row_invalid","multi_tariff_not_supported","owner_mismatch","panel_contract_error",
         "comparator_instability","rate_limit_violation","cycle_deadline_exceeded","panel_read_error_count",
         "panel_read_error_ratio","panel_missing_count","panel_missing_ratio","critical_access_drift_count",
         "critical_access_drift_ratio","total_drift_count","total_drift_ratio","panel_cycle_open_failed"] |
        index($reason) != null) and
      .stopped == (.stop_reason != "none") and
      (.mismatch_fields | counter_map(["status","expire_at","traffic_limit_bytes","traffic_limit_strategy",
                                        "hwid_device_limit","internal_squads","external_squad_uuid"])) and
      (.cohorts | counter_map(["active_paid","trial","limited","grace","access_point","direct_v2"])) and
      (.exact + .drift + .missing <= .sampled) and
      (([.cohorts[]] | add // 0) <= (.sampled * 3))
    )
  '
}

unlink_run_primitives() {
  local entrypoint_path="$1" controller_path entrypoint_real
  controller_path="$(readlink -f "$0")"
  entrypoint_real="$(readlink -f "$entrypoint_path")"
  test "$(basename "$controller_path")" = 'entitlement-shadow-one-shot-control.sh'
  test "$(basename "$entrypoint_real")" = 'entitlement_shadow_one_shot.py'
  test "$(dirname "$controller_path")" = "$(dirname "$entrypoint_real")"
  rm -f "$entrypoint_real" "$controller_path"
}

disable_shadow() {
  local entrypoint_path="${1:-}" expected_entrypoint_sha="${2:-}" raw_output='' validated=''
  if ! docker inspect "$FIXED_NAME" >/dev/null 2>&1; then
    unlink_run_primitives "$entrypoint_path"
    echo 'observation_evidence=unproved'
    echo 'cleanup_result=absent_noop'
    return 0
  fi
  # Ownership is proven from the exact teplo.role label; foreign containers fail closed.
  require_exact_role
  raw_output="$(docker logs "$FIXED_NAME" 2>/dev/null || true)"
  docker rm -f "$FIXED_NAME" >/dev/null
  if docker inspect "$FIXED_NAME" >/dev/null 2>&1; then
    echo 'fixed-name one-shot container remains after cleanup' >&2
    return 1
  fi
  if [ -f "$entrypoint_path" ] && [ "$(sha256sum "$entrypoint_path" | awk '{print $1}')" = "$expected_entrypoint_sha" ] \
    && validated="$(printf '%s' "$raw_output" | validate_evidence 2>/dev/null)"; then
    printf 'observation_evidence=%s\n' "$validated"
  else
    echo 'observation_evidence=unproved'
  fi
  unlink_run_primitives "$entrypoint_path"
  echo 'cleanup_result=removed_owned_one_shot'
}

enable_shadow() {
  local entrypoint_path="$1"
  local expected_entrypoint_sha="$2"
  local actual_entrypoint_sha current_sha current_image db_network raw_output validated deadline
  local bot_container='remnawave_bot' db_container='remnawave_bot_db' panel_network='remnawave-network'
  local bot_id_before='' bot_started_before='' bot_restart_before=''

  if docker inspect "$FIXED_NAME" >/dev/null 2>&1; then
    echo 'fixed-name container already exists; refusing adoption or replacement' >&2
    return 1
  fi
  actual_entrypoint_sha="$(sha256sum "$entrypoint_path" | awk '{print $1}')"
  test "$actual_entrypoint_sha" = "$expected_entrypoint_sha"
  if [ "${ONE_SHOT_E2E_MODE:-}" = 'exact-isolated-contract-v1' ]; then
    bot_container="${ONE_SHOT_E2E_BOT_CONTAINER:?}"
    db_container="${ONE_SHOT_E2E_DB_CONTAINER:?}"
    panel_network="${ONE_SHOT_E2E_PANEL_NETWORK:?}"
    test "$(docker inspect --format '{{ index .Config.Labels "teplo.e2e" }}' "$bot_container")" = 'gate2-shadow-one-shot'
    test "$(docker inspect --format '{{ index .Config.Labels "teplo.e2e" }}' "$db_container")" = 'gate2-shadow-one-shot'
    test "$(docker network inspect --format '{{ index .Labels "teplo.e2e" }}' "$panel_network")" = 'gate2-shadow-one-shot'
  else
    current_sha="$(sed -n 's/^sha=//p' "$STATE_FILE")"
    test "$current_sha" = "$COMPATIBLE_SHA"
    test "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$COMPATIBLE_SHA"
  fi
  current_image="$(docker inspect --format '{{.Image}}' "$bot_container")"
  test "$current_image" = "$COMPATIBLE_IMAGE"
  db_network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$db_container" | head -n 1)"
  test -n "$db_network"

  container_env() {
    local key="$1" value count
    count="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$bot_container" | grep -Ec "^${key}=")"
    test "$count" = '1'
    value="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$bot_container" | sed -n "s/^${key}=//p")"
    test -n "$value"
    printf '%s' "$value"
  }

  production_preflight() {
    local authority_total schema_revision runtime_flags
    test "$(docker inspect --format '{{.State.Running}}' "$bot_container")" = 'true'
    test "$(docker inspect --format '{{.State.Paused}}' "$bot_container")" = 'false'
    test "$(docker inspect --format '{{.State.Health.Status}}' "$bot_container")" = 'healthy'
    test "$(docker inspect --format '{{.Image}}' "$bot_container")" = "$COMPATIBLE_IMAGE"
    test "$(git -C "$REPO_DIR" rev-parse HEAD)" = "$COMPATIBLE_SHA"
    test "$(sed -n 's/^sha=//p' "$STATE_FILE")" = "$COMPATIBLE_SHA"
    runtime_flags="$(docker exec "$bot_container" python -c "from app.config import settings; print('|'.join(str(int(v)) for v in (settings.ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED,settings.ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED,settings.ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED,settings.ENTITLEMENT_AUTHORITY_SHADOW_ENABLED,settings.ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH,settings.MULTI_TARIFF_ENABLED)))")"
    test "$runtime_flags" = '0|0|0|0|1|0'
    test "$(sha256sum "$REPO_DIR/.env" | awk '{print $1}')" = "$ENV_FINGERPRINT"
    schema_revision="$(docker exec "$db_container" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version"')"
    test "$schema_revision" = '0103'
    authority_total="$(docker exec "$db_container" sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT (SELECT count(*) FROM entitlement_identities)+(SELECT count(*) FROM entitlement_source_revisions)+(SELECT count(*) FROM entitlement_overlays)+(SELECT count(*) FROM entitlement_projection_commands)+(SELECT count(*) FROM entitlement_observations)+(SELECT count(*) FROM entitlement_webhook_inbox)+(SELECT count(*) FROM entitlement_notification_intents)+(SELECT count(*) FROM entitlement_cleanup_commands)+(SELECT count(*) FROM entitlement_cleanup_tombstones)"')"
    test "$authority_total" = '0'
  }

  if [ "${ONE_SHOT_E2E_MODE:-}" != 'exact-isolated-contract-v1' ]; then
    production_preflight
    bot_id_before="$(docker inspect --format '{{.Id}}' "$bot_container")"
    bot_started_before="$(docker inspect --format '{{.State.StartedAt}}' "$bot_container")"
    bot_restart_before="$(docker inspect --format '{{.RestartCount}}' "$bot_container")"
  fi

  docker create --rm \
    --name "$FIXED_NAME" \
    --label "$ROLE_LABEL" \
    --user 1000:1000 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --memory 256m \
    --cpus 0.50 \
    --pids-limit 128 \
    --restart no \
    --no-healthcheck \
    --network "$db_network" \
    --mount "type=bind,src=$entrypoint_path,dst=/opt/teplo/entitlement_shadow_one_shot.py,readonly" \
    --env BOT_TOKEN='0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' \
    --env POSTGRES_HOST="$(container_env POSTGRES_HOST)" \
    --env POSTGRES_PORT="$(container_env POSTGRES_PORT)" \
    --env POSTGRES_DB="$(container_env POSTGRES_DB)" \
    --env POSTGRES_USER="$(container_env POSTGRES_USER)" \
    --env POSTGRES_PASSWORD="$(container_env POSTGRES_PASSWORD)" \
    --env REMNAWAVE_API_URL="$(container_env REMNAWAVE_API_URL)" \
    --env REMNAWAVE_API_KEY="$(container_env REMNAWAVE_API_KEY)" \
    --env REMNAWAVE_AUTH_TYPE="$(container_env REMNAWAVE_AUTH_TYPE)" \
    --env DEFAULT_TRAFFIC_RESET_STRATEGY="$(container_env DEFAULT_TRAFFIC_RESET_STRATEGY)" \
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
    --env ENTITLEMENT_AUTHORITY_SHADOW_MAX_CYCLE_SECONDS="$CYCLE_SECONDS" \
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
    "$COMPATIBLE_IMAGE" \
    timeout --signal=TERM --kill-after="${KILL_AFTER_SECONDS}s" "${TERM_SECONDS}s" \
      python /opt/teplo/entitlement_shadow_one_shot.py >/dev/null

  cleanup_created() {
    docker rm -f "$FIXED_NAME" >/dev/null 2>&1 || true
  }
  trap cleanup_created EXIT HUP INT TERM
  docker network connect "$panel_network" "$FIXED_NAME"
  test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$FIXED_NAME")" = 'true'
  test "$(docker inspect --format '{{.Config.User}}' "$FIXED_NAME")" = '1000:1000'
  test "$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$FIXED_NAME")" = '2'
  if [ "${ONE_SHOT_E2E_MODE:-}" != 'exact-isolated-contract-v1' ]; then
    production_preflight
  fi
  command -v jq >/dev/null
  deadline=$((SECONDS + ABSENT_BY_SECONDS))
  docker start "$FIXED_NAME" >/dev/null
  unlink_run_primitives "$entrypoint_path"
  echo 'security_evidence=uid-1000,readonly-rootfs,two-networks'
  echo 'start_confirmed=true'

  monitor_seconds=$((deadline - SECONDS))
  test "$monitor_seconds" -gt 0
  raw_output="$(timeout --signal=TERM --kill-after=2s "${monitor_seconds}s" docker logs -f "$FIXED_NAME" 2>&1 || true)"
  if validated="$(printf '%s' "$raw_output" | validate_evidence 2>/dev/null)"; then
    printf 'observation_evidence=%s\n' "$validated"
  else
    echo 'observation_evidence=unproved'
  fi

  while docker inspect "$FIXED_NAME" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo 'container still present after 210 seconds' >&2
      return 1
    fi
    sleep 1
  done
  if [ "${ONE_SHOT_E2E_MODE:-}" != 'exact-isolated-contract-v1' ]; then
    test "$(docker inspect --format '{{.Id}}' "$bot_container")" = "$bot_id_before"
    test "$(docker inspect --format '{{.State.StartedAt}}' "$bot_container")" = "$bot_started_before"
    test "$(docker inspect --format '{{.RestartCount}}' "$bot_container")" = "$bot_restart_before"
    production_preflight
    echo 'production_bot_unchanged=true'
  fi
  trap - EXIT HUP INT TERM
  echo 'container_absent=true'
}

main() {
  local action="${1:-}" entrypoint_path="${2:-}" expected_entrypoint_sha="${3:-}"
  case "$action" in
    DISABLE_SHADOW) disable_shadow "$entrypoint_path" "$expected_entrypoint_sha" ;;
    ENABLE_SHADOW)
      test -f "$entrypoint_path"
      [[ "$expected_entrypoint_sha" =~ ^[0-9a-f]{64}$ ]]
      enable_shadow "$entrypoint_path" "$expected_entrypoint_sha"
      ;;
    *) echo 'unsupported action' >&2; return 64 ;;
  esac
}

main "$@"

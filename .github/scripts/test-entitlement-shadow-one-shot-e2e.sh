#!/usr/bin/env bash
set -Eeuo pipefail

readonly CONFIG_DIGEST='sha256:090dc7c8340dab6c90400f8f9d9554878ff3c998c16c883a4f2b44d03ca68ab3'
readonly OCI_INDEX_DIGEST='sha256:35dd4dfcd12932fc2cba9c84ef0345ada97ec848e1c3cb8efe52d098873f9f86'
readonly COMPATIBLE_SHA='39a0a0dcc5467f6cfe802629213dc3a57273ea25'
readonly FIXED_NAME='teplo-entitlement-shadow-one-shot'
readonly ENTRYPOINT="${1:?entrypoint path required}"
readonly CONTROLLER="${2:?controller path required}"
readonly READONLY_PROBE="${3:?read-only probe path required}"
readonly SCHEMA_HELPER="${4:?schema helper path required}"
readonly RUN_KEY="${5:?run key required}"
readonly RUNTIME_SOURCE_DIR="${6:?runtime source dir required}"
readonly IMAGE="${7:?exact runtime image reference required}"
readonly RUNTIME_SOURCE_SHA="${8:-$COMPATIBLE_SHA}"
readonly WORK_DIR="$(mktemp -d "/tmp/teplo-shadow-e2e-${RUN_KEY}.XXXXXX")"
readonly DB_NAME="teplo-shadow-db-${RUN_KEY}"
readonly BOT_NAME="teplo-shadow-config-${RUN_KEY}"
readonly PANEL_NAME="teplo-shadow-panel-${RUN_KEY}"
readonly DB_NETWORK="teplo-shadow-dbnet-${RUN_KEY}"
readonly PANEL_NETWORK="teplo-shadow-panelnet-${RUN_KEY}"
readonly PANEL_UUID='11111111-2222-3333-4444-555555555555'
readonly TELEGRAM_ID='900000001'
ACTIVE_RUN_DIR=''
ACTIVE_CONTROLLER=''
ACTIVE_ENTRYPOINT=''

chmod 777 "$WORK_DIR"

cleanup() {
  set +e
  if docker inspect "$FIXED_NAME" >/dev/null 2>&1 \
    && [ "$(docker inspect --format '{{ index .Config.Labels \"teplo.e2e-run\" }}' "$FIXED_NAME")" = "$RUN_KEY" ]; then
    docker rm -f "$FIXED_NAME" >/dev/null 2>&1
  fi
  docker rm -f "$PANEL_NAME" "$BOT_NAME" "$DB_NAME" >/dev/null 2>&1
  docker network rm "$PANEL_NETWORK" "$DB_NETWORK" >/dev/null 2>&1
  if [ -n "$ACTIVE_RUN_DIR" ]; then
    rm -f "$ACTIVE_CONTROLLER" "$ACTIVE_ENTRYPOINT"
    rmdir "$ACTIVE_RUN_DIR" 2>/dev/null
  fi
  rm -f "$WORK_DIR/fake_panel.py" "$WORK_DIR/panel-counts.json" \
    "$WORK_DIR/controller-sigkill.out" "$WORK_DIR/query-fail-bin/docker" \
    "$WORK_DIR/query-fail-bin/timeout"
  rmdir "$WORK_DIR/query-fail-bin" 2>/dev/null
  rmdir "$WORK_DIR" 2>/dev/null
}
trap cleanup EXIT HUP INT TERM

test "$(git -c safe.directory="$RUNTIME_SOURCE_DIR" -C "$RUNTIME_SOURCE_DIR" rev-parse HEAD)" = "$RUNTIME_SOURCE_SHA"
docker image inspect "$IMAGE" >/dev/null
test "$RUNTIME_SOURCE_SHA" = "$COMPATIBLE_SHA"
expected_shadow_sha="$(sha256sum "$RUNTIME_SOURCE_DIR/app/services/entitlement_authority/shadow.py" | awk '{print $1}')"
actual_shadow_sha="$(docker run --rm --entrypoint sha256sum "$IMAGE" /app/app/services/entitlement_authority/shadow.py | awk '{print $1}')"
test "$actual_shadow_sha" = "$expected_shadow_sha"
if docker inspect "$FIXED_NAME" >/dev/null 2>&1; then
  echo 'fixed one-shot name is occupied before isolated test' >&2
  exit 1
fi

docker network create --internal --label teplo.e2e=gate2-shadow-one-shot "$DB_NETWORK" >/dev/null
docker network create --internal --label teplo.e2e=gate2-shadow-one-shot "$PANEL_NETWORK" >/dev/null
docker run -d --name "$DB_NAME" --label teplo.e2e=gate2-shadow-one-shot \
  --network "$DB_NETWORK" \
  -e POSTGRES_DB=shadow -e POSTGRES_USER=shadow -e POSTGRES_PASSWORD=shadow \
  postgres:15-alpine >/dev/null
for _ in $(seq 1 60); do
  docker exec "$DB_NAME" pg_isready -U shadow -d shadow >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB_NAME" pg_isready -U shadow -d shadow >/dev/null

docker run --rm --network "$DB_NETWORK" \
  --user 1000:1000 --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m \
  --cap-drop ALL --security-opt no-new-privileges:true --memory 256m --cpus 0.50 --pids-limit 128 \
  --mount "type=bind,src=$SCHEMA_HELPER,dst=/opt/teplo/entitlement_shadow_create_schema.py,readonly" \
  -e BOT_TOKEN='0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' \
  -e POSTGRES_HOST="$DB_NAME" -e POSTGRES_PORT=5432 -e POSTGRES_DB=shadow \
  -e POSTGRES_USER=shadow -e POSTGRES_PASSWORD=shadow \
  "$IMAGE" python /opt/teplo/entitlement_shadow_create_schema.py

docker exec -i "$DB_NAME" psql -v ON_ERROR_STOP=1 -U shadow -d shadow <<SQL
INSERT INTO users
       (telegram_id, auth_type, has_had_paid_subscription, email_verified,
        auto_promo_group_assigned, auto_promo_group_threshold_kopeks,
        promo_offer_discount_percent, has_made_first_topup,
        restriction_topup, restriction_subscription, partner_status, remnawave_uuid)
VALUES ($TELEGRAM_ID, 'test', false, false, false, 0, 0, false, false, false,
        'none', '$PANEL_UUID');
INSERT INTO subscriptions
       (user_id, status, is_trial, start_date, end_date, traffic_limit_gb,
        traffic_used_gb, purchased_traffic_gb, device_limit, connected_squads,
        is_daily_paused, in_grace, remnawave_short_id, remnawave_uuid)
SELECT id, 'limited', false, now(), '2030-01-01T00:00:00.084771Z', 10,
       0, 0, 1, '[]'::json, false, false, 'gate2synthetic01', '$PANEL_UUID'
  FROM users WHERE telegram_id=$TELEGRAM_ID;
SQL

cat > "$WORK_DIR/fake_panel.py" <<'PY'
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTS = os.environ['COUNTS']
SCENARIO = os.environ.get('SCENARIO', 'success')
UUID = '11111111-2222-3333-4444-555555555555'
counts = {'GET': 0, 'POST': 0, 'PATCH': 0, 'PUT': 0, 'DELETE': 0}

def persist():
    with open(COUNTS, 'w', encoding='utf-8') as stream:
        json.dump(counts, stream, sort_keys=True)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return
    def _answer(self):
        counts[self.command] += 1
        persist()
        if self.command != 'GET':
            self.send_response(405); self.end_headers(); return
        if SCENARIO == 'delay':
            time.sleep(8)
        if SCENARIO == 'error':
            self.send_response(503); self.end_headers(); return
        expire_at = '2030-01-01T00:00:00.084000Z'
        if SCENARIO == 'adjacent_ms':
            expire_at = '2030-01-01T00:00:00.085000Z'
        body = json.dumps({'response': {
            'uuid': UUID, 'telegramId': 900000001, 'status': 'LIMITED',
            'expireAt': expire_at,
            'trafficLimitBytes': 10737418240, 'trafficLimitStrategy': 'NO_RESET',
            'hwidDeviceLimit': 1, 'activeInternalSquads': [],
            'externalSquadUuid': None,
        }}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

persist()
ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
PY
chmod 644 "$WORK_DIR/fake_panel.py"

start_panel() {
  local scenario="$1"
  # The fake-panel is isolated and records every allowed or mutating HTTP verb.
  docker rm -f "$PANEL_NAME" >/dev/null 2>&1 || true
  rm -f "$WORK_DIR/panel-counts.json"
  docker run -d --name "$PANEL_NAME" --network "$PANEL_NETWORK" --no-healthcheck \
    --mount "type=bind,src=$WORK_DIR,dst=/e2e" \
    -e COUNTS=/e2e/panel-counts.json -e SCENARIO="$scenario" \
    "$IMAGE" python /e2e/fake_panel.py >/dev/null
  sleep 1
}

docker create --name "$BOT_NAME" --label teplo.e2e=gate2-shadow-one-shot \
  --network "$DB_NETWORK" \
  -e POSTGRES_HOST="$DB_NAME" -e POSTGRES_PORT=5432 -e POSTGRES_DB=shadow \
  -e POSTGRES_USER=shadow -e POSTGRES_PASSWORD=shadow \
  -e REMNAWAVE_API_URL="http://$PANEL_NAME:8080" -e REMNAWAVE_API_KEY=e2e-key \
  -e REMNAWAVE_AUTH_TYPE=api_key -e DEFAULT_TRAFFIC_RESET_STRATEGY=NO_RESET \
  "$IMAGE" true >/dev/null

fingerprint() {
  docker exec "$DB_NAME" psql -U shadow -d shadow -Atc "
    WITH row_hashes(table_name, row_hash) AS (
      SELECT 'users', md5(row_to_json(t)::text) FROM users t
      UNION ALL SELECT 'subscriptions', md5(row_to_json(t)::text) FROM subscriptions t
      UNION ALL SELECT 'subscription_checkouts', md5(row_to_json(t)::text) FROM subscription_checkouts t
      UNION ALL SELECT 'subscription_entitlement_terms', md5(row_to_json(t)::text) FROM subscription_entitlement_terms t
      UNION ALL SELECT 'entitlement_identities', md5(row_to_json(t)::text) FROM entitlement_identities t
      UNION ALL SELECT 'entitlement_source_revisions', md5(row_to_json(t)::text) FROM entitlement_source_revisions t
      UNION ALL SELECT 'entitlement_overlays', md5(row_to_json(t)::text) FROM entitlement_overlays t
      UNION ALL SELECT 'entitlement_projection_commands', md5(row_to_json(t)::text) FROM entitlement_projection_commands t
      UNION ALL SELECT 'entitlement_observations', md5(row_to_json(t)::text) FROM entitlement_observations t
      UNION ALL SELECT 'entitlement_webhook_inbox', md5(row_to_json(t)::text) FROM entitlement_webhook_inbox t
      UNION ALL SELECT 'entitlement_notification_intents', md5(row_to_json(t)::text) FROM entitlement_notification_intents t
      UNION ALL SELECT 'entitlement_cleanup_commands', md5(row_to_json(t)::text) FROM entitlement_cleanup_commands t
      UNION ALL SELECT 'entitlement_cleanup_tombstones', md5(row_to_json(t)::text) FROM entitlement_cleanup_tombstones t
    )
    SELECT md5(coalesce(string_agg(table_name || ':' || row_hash, '' ORDER BY table_name, row_hash), ''))
      FROM row_hashes"
}

prepare_run_primitives() {
  ACTIVE_RUN_DIR="$(mktemp -d "$WORK_DIR/controller-run.XXXXXX")"
  ACTIVE_CONTROLLER="$ACTIVE_RUN_DIR/entitlement-shadow-one-shot-control.sh"
  ACTIVE_ENTRYPOINT="$ACTIVE_RUN_DIR/entitlement_shadow_one_shot.py"
  cp "$CONTROLLER" "$ACTIVE_CONTROLLER"
  cp "$ENTRYPOINT" "$ACTIVE_ENTRYPOINT"
  chmod 700 "$ACTIVE_CONTROLLER"
  chmod 444 "$ACTIVE_ENTRYPOINT"
}

finish_run_primitives() {
  local successful="$1"
  if [ "$successful" = 'true' ]; then
    test ! -e "$ACTIVE_CONTROLLER"
    test ! -e "$ACTIVE_ENTRYPOINT"
  else
    rm -f "$ACTIVE_CONTROLLER" "$ACTIVE_ENTRYPOINT"
  fi
  rmdir "$ACTIVE_RUN_DIR"
  ACTIVE_RUN_DIR=''
  ACTIVE_CONTROLLER=''
  ACTIVE_ENTRYPOINT=''
}

invoke_control() {
  local action="$1" output rc
  prepare_run_primitives
  set +e
  output="$(ONE_SHOT_E2E_MODE=exact-isolated-contract-v1 \
    ONE_SHOT_E2E_BOT_CONTAINER="$BOT_NAME" \
    ONE_SHOT_E2E_DB_CONTAINER="$DB_NAME" \
    ONE_SHOT_E2E_PANEL_NETWORK="$PANEL_NETWORK" \
    ONE_SHOT_E2E_RUN_KEY="$RUN_KEY" \
    ONE_SHOT_E2E_IMAGE_REFERENCE="$IMAGE" \
    ONE_SHOT_E2E_RUNTIME_SOURCE_SHA="$RUNTIME_SOURCE_SHA" \
    ONE_SHOT_E2E_OCI_INDEX_REFERENCE="$OCI_INDEX_DIGEST" \
    "$ACTIVE_CONTROLLER" "$action" "$ACTIVE_ENTRYPOINT" \
      "$(sha256sum "$ACTIVE_ENTRYPOINT" | awk '{print $1}')" 2>&1)"
  rc=$?
  set -e
  if [ "$rc" = '0' ]; then
    finish_run_primitives true
  else
    finish_run_primitives false
  fi
  printf '%s' "$output"
  return "$rc"
}

run_controller() {
  invoke_control ENABLE_SHADOW
}

start_panel success
before="$(fingerprint)"
success_output="$(run_controller)"
after="$(fingerprint)"
if [ "$before" != "$after" ]; then
  printf 'synthetic_db_fingerprint_changed before=%s after=%s\n' "$before" "$after" >&2
  exit 1
fi
assert_summary() {
  local summary="$1" expected="$2"
  if ! printf '%s' "$summary" | grep -q "$expected"; then
    printf 'controller_summary_missing=%s\n%s\n' "$expected" "$summary" >&2
    return 1
  fi
}
assert_summary "$success_output" '"sampled":1'
assert_summary "$success_output" '"exact":1'
assert_summary "$success_output" 'security_evidence=exact-image,uid-1000,readonly-rootfs,limits,caps,no-new-privileges,restart-no,no-healthcheck,one-readonly-mount,two-networks,forbidden-env-absent'
assert_summary "$success_output" 'container_absent=true'
python3 - "$WORK_DIR/panel-counts.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    counts = json.load(stream)
assert counts == {'GET': 1, 'POST': 0, 'PATCH': 0, 'PUT': 0, 'DELETE': 0}, counts
PY
echo 'scenario=same-millisecond sampled=1 exact=1 mutations=0 container-absent readonly-rootfs two-networks uid-1000'

start_panel adjacent_ms
before="$(fingerprint)"
adjacent_output="$(run_controller)"
after="$(fingerprint)"
test "$before" = "$after"
assert_summary "$adjacent_output" '"sampled":1'
assert_summary "$adjacent_output" '"exact":0'
assert_summary "$adjacent_output" '"drift":1'
assert_summary "$adjacent_output" '"critical_drift":1'
assert_summary "$adjacent_output" '"mismatch_fields":{"expire_at":1}'
assert_summary "$adjacent_output" 'container_absent=true'
python3 - "$WORK_DIR/panel-counts.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    counts = json.load(stream)
assert counts == {'GET': 1, 'POST': 0, 'PATCH': 0, 'PUT': 0, 'DELETE': 0}, counts
PY
echo 'scenario=adjacent-millisecond sampled=1 drift=1 expire-at-only mutations=0 container-absent'

probe_output="$(docker run --rm --network "$DB_NETWORK" --user 1000:1000 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=8m --cap-drop ALL \
  --security-opt no-new-privileges:true --memory 128m --cpus 0.25 --pids-limit 64 \
  --mount "type=bind,src=$READONLY_PROBE,dst=/opt/teplo/entitlement_shadow_readonly_probe.py,readonly" \
  -e BOT_TOKEN='0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' \
  -e POSTGRES_HOST="$DB_NAME" -e POSTGRES_PORT=5432 -e POSTGRES_DB=shadow \
  -e POSTGRES_USER=shadow -e POSTGRES_PASSWORD=shadow \
  "$IMAGE" python /opt/teplo/entitlement_shadow_readonly_probe.py)"
test "$probe_output" = 'injected-dml-rejected=true'
echo 'scenario=injected-dml-rejected'

start_panel error
error_output="$(run_controller)"
printf '%s' "$error_output" | grep -q '"panel_read_errors":1'
printf '%s' "$error_output" | grep -q 'container_absent=true'
echo 'scenario=error aggregate-only container-absent'

start_panel delay
timeout_output="$(run_controller)"
printf '%s' "$timeout_output" | grep -q '"panel_read_errors":1'
printf '%s' "$timeout_output" | grep -q 'container_absent=true'
echo 'scenario=actual-panel-timeout aggregate-only container-absent'

docker run -d --rm --name "$FIXED_NAME" --label teplo.role=entitlement-shadow-one-shot \
  --label "teplo.e2e-run=$RUN_KEY" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --restart no \
  "$IMAGE" timeout --signal=TERM --kill-after=1s 2s sleep 60 >/dev/null
deadline=$((SECONDS + 8))
while docker inspect "$FIXED_NAME" >/dev/null 2>&1; do
  test "$SECONDS" -lt "$deadline"
  sleep 1
done
echo 'scenario=hard-deadline-primitive container-absent'

start_panel delay
prepare_run_primitives
ONE_SHOT_E2E_MODE=exact-isolated-contract-v1 \
ONE_SHOT_E2E_BOT_CONTAINER="$BOT_NAME" \
ONE_SHOT_E2E_DB_CONTAINER="$DB_NAME" \
ONE_SHOT_E2E_PANEL_NETWORK="$PANEL_NETWORK" \
ONE_SHOT_E2E_RUN_KEY="$RUN_KEY" \
ONE_SHOT_E2E_IMAGE_REFERENCE="$IMAGE" \
ONE_SHOT_E2E_RUNTIME_SOURCE_SHA="$RUNTIME_SOURCE_SHA" \
ONE_SHOT_E2E_OCI_INDEX_REFERENCE="$OCI_INDEX_DIGEST" \
  "$ACTIVE_CONTROLLER" ENABLE_SHADOW "$ACTIVE_ENTRYPOINT" \
    "$(sha256sum "$ACTIVE_ENTRYPOINT" | awk '{print $1}')" \
  >"$WORK_DIR/controller-sigkill.out" 2>&1 &
controller_pid=$!
for _ in $(seq 1 30); do
  if docker inspect "$FIXED_NAME" >/dev/null 2>&1 \
    && [ ! -e "$ACTIVE_CONTROLLER" ] && [ ! -e "$ACTIVE_ENTRYPOINT" ]; then
    break
  fi
  sleep 1
done
docker inspect "$FIXED_NAME" >/dev/null
test ! -e "$ACTIVE_CONTROLLER"
test ! -e "$ACTIVE_ENTRYPOINT"
kill -9 "$controller_pid"
wait "$controller_pid" 2>/dev/null || true
rmdir "$ACTIVE_RUN_DIR"
ACTIVE_RUN_DIR=''
ACTIVE_CONTROLLER=''
ACTIVE_ENTRYPOINT=''
if run_controller >/dev/null 2>&1; then
  echo 'lost response allowed a second concurrent cycle' >&2
  exit 1
fi
deadline=$((SECONDS + 30))
while docker inspect "$FIXED_NAME" >/dev/null 2>&1; do
  test "$SECONDS" -lt "$deadline"
  sleep 1
done
echo 'scenario=controller-sigkill container-absent'

mkdir "$WORK_DIR/query-fail-bin"
printf '%s\n' '#!/bin/sh' 'exit 125' > "$WORK_DIR/query-fail-bin/docker"
printf '%s\n' '#!/bin/sh' 'shift' 'exec "$@"' > "$WORK_DIR/query-fail-bin/timeout"
chmod 700 "$WORK_DIR/query-fail-bin/docker" "$WORK_DIR/query-fail-bin/timeout"
set +e
query_failure_output="$(PATH="$WORK_DIR/query-fail-bin:$PATH" invoke_control DISABLE_SHADOW 2>&1)"
query_failure_rc=$?
set -e
if [ "$query_failure_rc" = '0' ] \
  || printf '%s' "$query_failure_output" | grep -q 'cleanup_result=absent_noop'; then
  echo 'Docker query failure was misclassified as absent' >&2
  exit 1
fi
rm -f "$WORK_DIR/query-fail-bin/docker" "$WORK_DIR/query-fail-bin/timeout"
rmdir "$WORK_DIR/query-fail-bin"
echo 'scenario=disable-query-failure fail-closed'

disable_output="$(invoke_control DISABLE_SHADOW)"
printf '%s' "$disable_output" | grep -q 'cleanup_result=absent_noop'
for state in running stopped paused; do
  case "$state" in
    running)
      docker run -d --name "$FIXED_NAME" --label teplo.role=entitlement-shadow-one-shot \
        --label "teplo.e2e-run=$RUN_KEY" "$IMAGE" sleep 60 >/dev/null
      ;;
    stopped)
      docker create --name "$FIXED_NAME" --label teplo.role=entitlement-shadow-one-shot \
        --label "teplo.e2e-run=$RUN_KEY" "$IMAGE" true >/dev/null
      ;;
    paused)
      docker run -d --name "$FIXED_NAME" --label teplo.role=entitlement-shadow-one-shot \
        --label "teplo.e2e-run=$RUN_KEY" "$IMAGE" sleep 60 >/dev/null
      docker pause "$FIXED_NAME" >/dev/null
      ;;
  esac
  disable_output="$(invoke_control DISABLE_SHADOW)"
  printf '%s' "$disable_output" | grep -q 'cleanup_result=removed_owned_one_shot'
  ! docker inspect "$FIXED_NAME" >/dev/null 2>&1
done
docker create --name "$FIXED_NAME" --label teplo.role=foreign \
  --label "teplo.e2e-run=$RUN_KEY" "$IMAGE" true >/dev/null
if invoke_control DISABLE_SHADOW >/dev/null 2>&1; then
  echo 'foreign ownership was not rejected' >&2
  exit 1
fi
docker rm -f "$FIXED_NAME" >/dev/null
echo 'scenario=disable absent-noop running stopped paused foreign-fail-closed'

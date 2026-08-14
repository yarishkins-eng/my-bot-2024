from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / '.github/scripts'
BASELINE = SCRIPTS / 'verify-entitlement-shadow-baseline.py'
ENABLE = SCRIPTS / 'control-entitlement-shadow.sh'
DISABLE = SCRIPTS / 'disable-entitlement-shadow.sh'
SIDECAR = SCRIPTS / 'run-entitlement-shadow-sidecar.py'
WATCHDOG = SCRIPTS / 'watchdog-entitlement-shadow-bootstrap.sh'
WORKFLOW = ROOT / '.github/workflows/control-entitlement-shadow.yml'
DEPLOY_WORKFLOWS = (
    ROOT / '.github/workflows/deploy.yml',
    ROOT / '.github/workflows/deploy-migration.yml',
    ROOT / '.github/workflows/deploy-infrastructure.yml',
    ROOT / '.github/workflows/recover-after-migration.yml',
)


def _run_baseline(tmp_path: Path, content: str, mode: int = 0o600) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / '.env'
    env_file.write_text(content)
    env_file.chmod(mode)
    return subprocess.run(  # noqa: S603 - fixed repository verifier
        [str(BASELINE), '--env-file', str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    'content',
    [
        'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=1\n',
        'export ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH="true"\n',
        ' entitlement_authority_projector_enabled = false # comment\n',
        '# entitlement_authority_shadow_max_identities_per_cycle=18\n',
        'MULTI_TARIFF_ENABLED=false\n',
    ],
)
def test_baseline_refuses_every_managed_key_mention(tmp_path: Path, content: str) -> None:
    result = _run_baseline(tmp_path, content)

    assert result.returncode == 64
    assert result.stderr.strip() == 'STOP:managed_shadow_setting_present'


def test_baseline_accepts_existing_unmanaged_policy_and_refuses_writable_file(tmp_path: Path) -> None:
    content = (
        'DEFAULT_TRAFFIC_RESET_STRATEGY=MONTH\n'
        'DEVICES_SELECTION_ENABLED=true\n'
        'DEVICES_SELECTION_DISABLED_AMOUNT=\n'
        'UNRELATED_SECRET=not-printed\n'
    )
    accepted = _run_baseline(tmp_path, content, 0o644)
    refused = _run_baseline(tmp_path, content, 0o666)

    assert accepted.returncode == 0
    assert accepted.stdout.strip() == 'BASELINE_SAFE'
    assert refused.returncode == 64
    assert refused.stderr.strip() == 'STOP:env_is_group_or_world_writable'
    assert 'not-printed' not in refused.stderr


def test_workflow_has_only_allowlisted_protected_actions() -> None:
    workflow_text = WORKFLOW.read_text()
    workflow = yaml.safe_load(workflow_text)
    dispatch = workflow[True]['workflow_dispatch']['inputs']
    jobs = workflow['jobs']

    assert dispatch['action']['type'] == 'choice'
    assert dispatch['action']['options'] == ['DISABLE_SHADOW', 'ENABLE_SHADOW']
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['concurrency'] == {
        'group': 'teplo-bot-production-deploy',
        'cancel-in-progress': False,
    }
    assert set(jobs) == {'enable_verify', 'enable', 'disable'}
    assert jobs['enable_verify']['if'] == "inputs.action == 'ENABLE_SHADOW'"
    assert jobs['enable_verify']['uses'] == './.github/workflows/lint.yml'
    assert jobs['enable']['needs'] == ['enable_verify']
    assert jobs['enable']['environment'] == 'teplo-vpn-production-controlled-change'
    assert jobs['disable']['environment'] == 'teplo-vpn-production-controlled-change'
    assert 'needs' not in jobs['disable']
    assert workflow_text.count('actions/checkout@') == 2
    assert 'test "$OWNER_GO_NO_GO" = \'OWNER_APPROVED\'' in workflow_text
    assert workflow_text.count('git rev-parse origin/main') == 2
    assert "hashFiles('.github/scripts/control-entitlement-shadow.sh')" in workflow_text
    assert "hashFiles('.github/scripts/disable-entitlement-shadow.sh')" in workflow_text
    assert 'classify-entitlement-shadow-control.sh' not in workflow_text
    assert 'raw.githubusercontent.com' not in workflow_text
    assert 'sed -i' not in workflow_text
    assert '>> .env' not in workflow_text


def test_enable_uses_isolated_immutable_bounded_sidecar() -> None:
    source = ENABLE.read_text()

    assert source.index('flock -n 9') < source.index('git fetch --no-tags origin')
    assert source.index('flock -n 9') < source.index('CURRENT_IMAGE_ID=')
    assert source.index('flock -n 9') < source.index('actual_schema=')
    assert '--name "$SIDECAR"' in source
    assert '--restart=no' in source
    assert '--no-healthcheck' in source
    assert '--read-only' in source
    assert '--security-opt no-new-privileges:true' in source
    assert '--cap-drop ALL' in source
    assert '"$CURRENT_IMAGE_ID" python /app/shadow-sidecar-entrypoint.py' in source
    assert '--env-file "$SIDECAR_ENV_FILE"' in source
    assert 'docker create' in source
    docker_create = source.split('docker create', 1)[1].split('>/dev/null', 1)[0]
    assert '--env-file "$REPO_DIR/.env"' not in docker_create
    assert 'BOT_TOKEN=123456789:shadow-sidecar-does-not-use-telegram' in source
    assert 'ADMIN_NOTIFICATIONS_ENABLED=false' in source
    assert 'REMNAWAVE_WEBHOOK_ENABLED=false' in source
    assert 'REMNAWAVE_AUTO_SYNC_ENABLED=false' in source
    assert 'TELEGRAM' not in source
    assert 'systemd-run' in source
    assert 'write_lease prepared' in source
    assert 'write_lease completed' in source
    assert "AUTHORITY_COUNTS_BEFORE = '0,0,0,0,0,0,0,0,0'" not in source
    assert '[ "$AUTHORITY_COUNTS_BEFORE" = \'0,0,0,0,0,0,0,0,0\' ]' in source
    assert 'docker compose' in source  # read-only schema/count checks only
    assert 'docker compose stop' not in source
    assert 'docker compose create' not in source
    assert 'docker compose up' not in source
    assert 'docker restart' not in source
    assert 'docker rm -f "$BOT_CONTAINER"' not in source
    sidecar_snapshot_body = source.split('verify_sidecar_active() {', 1)[1].split('\n}', 1)[0]
    assert sidecar_snapshot_body.count('docker inspect') == 1
    assert '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}' in sidecar_snapshot_body
    watchdog_source = WATCHDOG.read_text()
    watchdog_snapshot_body = watchdog_source.split('capture_container_snapshot() {', 1)[1].split('\n}', 1)[0]
    assert watchdog_snapshot_body.count('docker inspect') == 1
    assert '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}|' in watchdog_snapshot_body
    assert 'control_plane_transition_prepared' in source
    assert source.index('control_plane_transition_prepared') < source.index('docker create')
    snapshot_body = source.split('capture_bot_snapshot() {', 1)[1].split('\n}', 1)[0]
    assert snapshot_body.count('docker inspect') == 1
    assert (
        '{{.Id}}|{{.Image}}|{{.State.StartedAt}}|{{.State.Health.Status}}|{{.State.Running}}|{{.State.Paused}}'
        in snapshot_body
    )
    assert source.count('verify_bot_snapshot_unchanged') >= 4
    first_commit_check = source.index('verify_bot_snapshot_unchanged', source.index('cycle_seen=0'))
    second_commit_check = source.index('verify_bot_snapshot_unchanged', first_commit_check + 1)
    assert first_commit_check < source.index('write_lease completed') < second_commit_check
    final_commit_check = source.index('verify_bot_snapshot_unchanged', second_commit_check + 1)
    assert source.index('latest_audit_not_durable') < final_commit_check < source.rindex('MUTATION_STARTED=0')
    completed_recovery = source.split('if [ "$lease_phase" = \'completed\' ]; then', 1)[1].split(
        '[ "$lease_phase" = \'prepared\' ]',
        1,
    )[0]
    assert completed_recovery.index('verify_bot_snapshot_unchanged') < completed_recovery.index('exit 0')
    assert completed_recovery.index('verify_sidecar_active') < completed_recovery.index('exit 0')
    assert 'WATCHDOG_PENDING_UNIT' in source
    assert 'WATCHDOG_EXACT_UNIT' in source
    assert source.index('--unit="$WATCHDOG_EXACT_UNIT"') < source.rindex(
        'systemctl stop "${WATCHDOG_PENDING_UNIT}.timer"'
    )
    assert 'pending\n' in source
    assert 'sampled[^0-9]*[1-9][0-9]*.*entitlement_shadow_cycle' in source
    assert 'entitlement_shadow_cycle.*sampled[^0-9]*[1-9][0-9]*' in source


def test_prepared_enable_retry_only_cleans_and_requires_new_workflow_run() -> None:
    source = ENABLE.read_text()
    prepared = source.split('[ "$lease_phase" = \'prepared\' ]', 1)[1].split('elif docker inspect "$SIDECAR"', 1)[0]

    assert prepared.index('trap cleanup_failed_enable ERR') < prepared.index('"$WATCHDOG_INSTALLED" BOOTSTRAP')
    assert prepared.index('"$WATCHDOG_INSTALLED" BOOTSTRAP') < source.index(
        'systemctl stop "${WATCHDOG_PENDING_UNIT}.timer"'
    )
    assert 'prepared_sidecar_cleanup_unverified' in prepared
    assert 'prepared_secret_cleanup_unverified' in prepared
    assert 'prepared_watchdog_stop_unverified' in prepared
    assert 'prepared_generation_cleaned_start_new_workflow_run' in prepared
    assert 'exit 64' in prepared


def test_enable_rerun_without_completed_lease_is_rejected() -> None:
    source = ENABLE.read_text()
    rerun_gate = "[ \"$RUN_ATTEMPT\" = '1' ] || fail 'rerun_without_completed_lease'"

    assert rerun_gate in source
    assert source.index(rerun_gate) < source.index('write_lease prepared')
    assert source.index(rerun_gate) < source.index('docker create')


def test_every_production_switch_refuses_an_active_shadow() -> None:
    for workflow in DEPLOY_WORKFLOWS:
        source = workflow.read_text()
        assert 'readonly CONTROL_PLANE_JOURNAL="$STATE_DIR/bot-production.control-plane-transition.state"' in source
        assert "readonly SHADOW_RUNTIME_DIR='/var/lib/teplo-vpn/entitlement-shadow-runtime'" in source
        assert 'readonly SHADOW_CONTROL_LOCK_FILE="$STATE_DIR/bot-production.entitlement-shadow-control.lock"' in source
        assert 'exec 8>"$SHADOW_CONTROL_LOCK_FILE"' in source
        assert 'flock -w 30 8' in source
        assert source.index('flock -w 30 8') < source.index('test ! -e "$SHADOW_RUNTIME_DIR/lease.state"')
        if workflow.name == 'deploy-infrastructure.yml':
            assert '[ "$infrastructure_paths_changed" = \'1\' ] && [ -e "$CONTROL_PLANE_JOURNAL" ]' in source
        else:
            assert source.index('flock -w 30 8') < source.index('test ! -e "$CONTROL_PLANE_JOURNAL"')
            assert 'test ! -e "$CONTROL_PLANE_JOURNAL"' in source
        assert 'test ! -e "$SHADOW_RUNTIME_DIR/lease.state"' in source
        assert 'test ! -e "$SHADOW_RUNTIME_DIR/disable.state"' in source
        assert 'docker info >/dev/null 2>&1' in source
        assert "--filter 'name=^/teplo_entitlement_shadow$'" in source
        assert "--format '{{.ID}}'" in source


def test_ordinary_deploy_routes_control_plane_changes_to_protected_infrastructure() -> None:
    source = (ROOT / '.github/workflows/deploy.yml').read_text()
    assert 'git diff --quiet "$PREVIOUS_SHA" "$TARGET_SHA" -- .github' in source
    assert 'a production control-plane change is present' in source
    assert 'Deploy bot infrastructure to production' in source


def test_protected_infrastructure_allowlists_only_reviewed_shadow_control_plane() -> None:
    source = (ROOT / '.github/workflows/deploy-infrastructure.yml').read_text()
    for path in (
        '.github/scripts/control-entitlement-shadow.sh',
        '.github/scripts/disable-entitlement-shadow.sh',
        '.github/scripts/run-entitlement-shadow-sidecar.py',
        '.github/scripts/verify-entitlement-shadow-baseline.py',
        '.github/scripts/watchdog-entitlement-shadow-bootstrap.sh',
        '.github/workflows/control-entitlement-shadow.yml',
        '.github/workflows/deploy-infrastructure.yml',
        '.github/workflows/deploy-migration.yml',
        '.github/workflows/deploy.yml',
        '.github/workflows/lint.yml',
        '.github/workflows/recover-after-migration.yml',
    ):
        assert path in source
    assert 'paths outside the reviewed allowlist' in source
    assert 'database or migration-risk changes require the migration workflow' in source


def test_sidecar_receives_only_minimal_secret_names() -> None:
    source = ENABLE.read_text()
    extractor = source.split('build_sidecar_env_file() {', 1)[1].split('\n}', 1)[0]

    assert 'POSTGRES_PASSWORD' in extractor
    assert 'REMNAWAVE_API_KEY' in extractor
    for forbidden in (
        'BOT_TOKEN',
        'TELEGRAM',
        'PLATEGA',
        'YOOKASSA',
        'STRIPE',
        'SMTP',
        'REDIS',
        'REMNAWAVE_SECRET_KEY',
        'REMNAWAVE_USERNAME',
        'REMNAWAVE_PASSWORD',
        'REMNAWAVE_CADDY_TOKEN',
        'REMNAWAVE_AUTH_TYPE',
        'TZ',
    ):
        assert forbidden not in extractor


def test_disable_is_independent_of_bot_and_business_systems() -> None:
    source = DISABLE.read_text()
    commands = '\n'.join(line for line in source.lower().splitlines() if not line.lstrip().startswith('#'))

    assert "readonly SIDECAR='teplo_entitlement_shadow'" in source
    assert 'rm -f -- "$LEASE_FILE"' in source
    assert 'docker rm --force "$actual"' in source
    assert 'docker info' in source
    assert 'systemd-run' in source
    assert 'on-active=60s' in source
    assert '--property=Restart=on-failure' in source
    for forbidden in (
        'remnawave_bot',
        'docker compose',
        'git ',
        'psql',
        'postgres',
        'redis',
        'curl',
        'repo_dir/.env',
    ):
        assert forbidden not in commands


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _write_fake_flock(fake_bin: Path) -> None:
    _write_executable(fake_bin / 'flock', '#!/usr/bin/env bash\nexit 0\n')


def _write_real_flock(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / 'flock',
        """#!/usr/bin/env python3
import fcntl
import sys

fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)
""",
    )


def _write_fake_watchdog_docker(fake_bin: Path, container_id: str) -> None:
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
if [ "$1" = info ]; then exit 0; fi
if [ "$1" = inspect ]; then
  if [ -e "${FAIL_INSPECT_ONCE_FILE:-/nonexistent}" ]; then
    rm -f "$FAIL_INSPECT_ONCE_FILE"
    exit 125
  fi
  [ -e "$CONTAINER_PRESENT" ] || exit 1
  target="${@: -1}"
  if [ "$target" != teplo_entitlement_shadow ] && [ "$target" != "$FAKE_CONTAINER_ID" ]; then exit 1; fi
  case "$*" in
    *'{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.Paused}}|'*)
      snapshot_calls_file="${DOCKER_CALLS}.snapshots"
      snapshot_calls=0
      [ ! -e "$snapshot_calls_file" ] || snapshot_calls="$(cat "$snapshot_calls_file")"
      snapshot_calls="$(( snapshot_calls + 1 ))"
      printf '%s\n' "$snapshot_calls" > "$snapshot_calls_file"
      fake_image="$(printf '%064d' 0 | tr 0 b)"
      fake_sha="$(printf '%040d' 0 | tr 0 a)"
      printf '%s|sha256:%s|%s|%s|entitlement-shadow-readonly|%s|%s|%s|%s|gate2-readonly-v1\n' \
        "$FAKE_CONTAINER_ID" "$fake_image" "${FAKE_CONTAINER_RUNNING:-true}" \
        "${FAKE_CONTAINER_PAUSED:-false}" "${FAKE_LABEL_RUN_ID:-$EXPECTED_RUN_ID}" \
        "${FAKE_LABEL_RUN_ATTEMPT:-$EXPECTED_RUN_ATTEMPT}" "$fake_sha" "$fake_sha"
      if [ "${FAKE_STOP_AFTER_SNAPSHOT_CALL:-0}" = "$snapshot_calls" ]; then
        rm -f "$CONTAINER_PRESENT"
      fi
      ;;
    *'{{.Id}}'*) if [ -e "$CONTAINER_PRESENT" ]; then printf '%s\n' "$FAKE_CONTAINER_ID"; fi ;;
    *State.Running*) printf 'true\n' ;;
    *State.Paused*) printf '%s\n' "${FAKE_CONTAINER_PAUSED:-false}" ;;
    *'{{.Image}}'*) printf 'sha256:%064d\n' 0 | tr 0 b ;;
    *teplo.role*) printf 'entitlement-shadow-readonly\n' ;;
    *teplo.workflow_sha*) printf '%040d\n' 0 | tr 0 a ;;
    *teplo.deployed_sha*) printf '%040d\n' 0 | tr 0 a ;;
    *teplo.policy_version*) printf 'gate2-readonly-v1\n' ;;
    *teplo.workflow_run_id*) printf '%s\n' "${FAKE_LABEL_RUN_ID:-$EXPECTED_RUN_ID}" ;;
    *teplo.workflow_run_attempt*) printf '%s\n' "${FAKE_LABEL_RUN_ATTEMPT:-$EXPECTED_RUN_ATTEMPT}" ;;
    *Config.Env*)
      if [ -n "${EXPECTED_ENV_FILE:-}" ] && [ -r "$EXPECTED_ENV_FILE" ]; then cat "$EXPECTED_ENV_FILE"; fi ;;
    *) : ;;
  esac
  exit 0
fi
if [ "$1" = container ] && [ "$2" = ls ]; then
  [ -e "$CONTAINER_PRESENT" ] && printf '%s\n' "$FAKE_CONTAINER_ID"
  exit 0
fi
if [ "$1" = rm ]; then rm -f "$CONTAINER_PRESENT"; printf '%s\n' "$*" >> "$DOCKER_CALLS"; exit 0; fi
printf '%s\n' "$*" >> "$DOCKER_CALLS"
exit 0
""",
    )
    _write_executable(fake_bin / 'systemctl', '#!/usr/bin/env bash\nexit 1\n')
    _write_executable(fake_bin / 'systemd-run', '#!/usr/bin/env bash\nexit 0\n')


def _fixed_policy_env() -> str:
    values = {
        'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED': 'false',
        'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED': 'false',
        'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED': 'false',
        'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED': 'true',
        'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH': 'false',
        'DATABASE_POOL_SIZE': '2',
        'DATABASE_MAX_OVERFLOW': '0',
        'DATABASE_POOL_TIMEOUT': '5',
        'REMNAWAVE_API_CONNECT_TIMEOUT': '4',
        'REMNAWAVE_API_TOTAL_TIMEOUT': '4',
        'REMNAWAVE_AUTH_TYPE': 'api_key',
        'TZ': 'Europe/Moscow',
        'BOT_TOKEN': '123456789:shadow-sidecar-does-not-use-telegram',
        'ADMIN_NOTIFICATIONS_ENABLED': 'false',
        'REMNAWAVE_WEBHOOK_ENABLED': 'false',
        'REMNAWAVE_AUTO_SYNC_ENABLED': 'false',
        'ACCESS_POINT_INVENTORY_DRY_RUN_ENABLED': 'false',
        'ACCESS_POINT_INVENTORY_CATALOG_APPLY_ENABLED': 'false',
        'MULTI_TARIFF_ENABLED': 'false',
        'DEFAULT_TRAFFIC_RESET_STRATEGY': 'MONTH',
        'DEVICES_SELECTION_ENABLED': 'true',
        'DEVICES_SELECTION_DISABLED_AMOUNT': '',
        'ENTITLEMENT_AUTHORITY_SHADOW_COHORT_BASIS_POINTS': '1000',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_IDENTITIES_PER_CYCLE': '18',
        'ENTITLEMENT_AUTHORITY_SHADOW_SCHEDULE_SECONDS': '900',
        'ENTITLEMENT_AUTHORITY_SHADOW_PANEL_READS_PER_MINUTE': '12',
        'ENTITLEMENT_AUTHORITY_SHADOW_PANEL_TIMEOUT_SECONDS': '4',
        'ENTITLEMENT_AUTHORITY_SHADOW_DB_STATEMENT_TIMEOUT_MS': '5000',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_CYCLE_SECONDS': '180',
        'ENTITLEMENT_AUTHORITY_SHADOW_MIN_RATIO_SAMPLE': '10',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERRORS': '2',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERROR_BASIS_POINTS': '1000',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_COUNT': '2',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_BASIS_POINTS': '1000',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_COUNT': '2',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_BASIS_POINTS': '1000',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_COUNT': '4',
        'ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_BASIS_POINTS': '2000',
    }
    return ''.join(f'{key}={value}\n' for key, value in values.items())


def _lease(run_id: str, run_attempt: str, phase: str, expires: int) -> str:
    completed = 'pending' if phase == 'prepared' else '2026-08-13T00:00:00+03:00'
    return (
        'format_version=2\n'
        f'phase={phase}\n'
        'action=ENABLE_SHADOW\n'
        'runtime_mode=enabled\n'
        'policy_version=gate2-readonly-v1\n'
        f'workflow_sha={"a" * 40}\n'
        f'deployed_sha={"a" * 40}\n'
        f'image=sha256:{"b" * 64}\n'
        f'workflow_run_id={run_id}\n'
        f'workflow_run_attempt={run_attempt}\n'
        'approval_actor=owner\n'
        'release_card_reference=gate2-test\n'
        f'expires_epoch={expires}\n'
        f'completed_at={completed}\n'
    )


def test_watchdog_removes_uncommitted_sidecar_after_controller_kill(tmp_path: Path) -> None:
    run_id, run_attempt = '123', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'prepared', 1))
    docker_calls = tmp_path / 'docker-calls'
    container_id = 'c' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret_env.write_text('SECRET=redacted\n')
    container_present = tmp_path / 'container-present'
    container_present.touch()
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
    }

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret_env),
            'pending',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not lease.exists()
    assert not secret_env.exists()
    assert not container_present.exists()
    assert docker_calls.read_text().strip() == f'rm --force {container_id}'
    disabled_audit = (
        state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.AUTO_DISABLE_BOOTSTRAP.audit'
    )
    assert 'action=AUTO_DISABLE_BOOTSTRAP\n' in disabled_audit.read_text()
    assert stat.S_IMODE(disabled_audit.stat().st_mode) == 0o600


def test_watchdog_materializes_completed_audit_without_removing_sidecar(tmp_path: Path) -> None:
    run_id, run_attempt = '456', '2'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    docker_calls = tmp_path / 'docker-calls'
    container_id = 'd' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret_env.write_text('SECRET=redacted\n')
    container_present = tmp_path / 'container-present'
    container_present.touch()
    expected_env = tmp_path / 'expected-env'
    expected_env.write_text(_fixed_policy_env())
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
        'EXPECTED_ENV_FILE': str(expected_env),
    }

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret_env),
            container_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    keyed = state / f'bot-production.entitlement-shadow-control.{run_id}.{run_attempt}.audit'
    latest = state / 'bot-production.entitlement-shadow-control.state'
    assert result.returncode == 0, result.stderr
    assert lease.exists()
    assert not secret_env.exists()
    assert keyed.read_text() == lease.read_text()
    assert latest.read_text() == lease.read_text()
    assert container_present.exists()


def test_watchdog_does_not_materialize_active_audit_after_sidecar_exit(tmp_path: Path) -> None:
    run_id, run_attempt = '458', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_id = '8' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret_env.write_text('SECRET=redacted\n')
    container_present = tmp_path / 'container-present'
    container_present.touch()
    expected_env = tmp_path / 'expected-env'
    expected_env.write_text(_fixed_policy_env())
    docker_calls = tmp_path / 'docker-calls'

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret_env),
            container_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            'PATH': f'{fake_bin}:{os.environ["PATH"]}',
            'DOCKER_CALLS': str(docker_calls),
            'EXPECTED_RUN_ID': run_id,
            'EXPECTED_RUN_ATTEMPT': run_attempt,
            'FAKE_CONTAINER_ID': container_id,
            'CONTAINER_PRESENT': str(container_present),
            'EXPECTED_ENV_FILE': str(expected_env),
            'FAKE_STOP_AFTER_SNAPSHOT_CALL': '1',
        },
    )

    active_audit = state / f'bot-production.entitlement-shadow-control.{run_id}.{run_attempt}.audit'
    disabled_audit = state / (
        f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.AUTO_DISABLE_BOOTSTRAP.audit'
    )
    assert result.returncode == 0, result.stderr
    assert not container_present.exists()
    assert not lease.exists()
    assert not active_audit.exists()
    assert disabled_audit.exists()
    assert 'runtime_mode=disabled' in disabled_audit.read_text()


def test_watchdog_refuses_paused_completed_sidecar(tmp_path: Path) -> None:
    run_id = '24242424'
    run_attempt = '1'
    container_id = '2' * 64
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    container_present = tmp_path / 'container-present'
    container_present.touch()
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)

    result = subprocess.run(  # noqa: S603 - executes the fixed watchdog under test
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret),
            container_id,
        ],
        env=os.environ
        | {
            'PATH': f'{fake_bin}:{os.environ["PATH"]}',
            'CONTAINER_PRESENT': str(container_present),
            'FAKE_CONTAINER_ID': container_id,
            'EXPECTED_RUN_ID': run_id,
            'EXPECTED_RUN_ATTEMPT': run_attempt,
            'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
            'FAKE_CONTAINER_PAUSED': 'true',
            'TEPLO_SHADOW_CONTROL_LOCK_HELD': '1',
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not container_present.exists()
    assert not lease.exists()
    disabled_audit = state / (
        f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.AUTO_DISABLE_BOOTSTRAP.audit'
    )
    assert disabled_audit.exists()
    assert 'action=AUTO_DISABLE_BOOTSTRAP' in disabled_audit.read_text()


def test_watchdog_recovers_missing_latest_from_existing_durable_audits(tmp_path: Path) -> None:
    run_id, run_attempt = '457', '3'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_id = 'e' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    watchdog_audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    keyed_audit = state / f'bot-production.entitlement-shadow-control.{run_id}.{run_attempt}.audit'
    watchdog_audit.write_text(lease.read_text())
    keyed_audit.write_text(lease.read_text())
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    container_present = tmp_path / 'container-present'
    container_present.touch()
    expected_env = tmp_path / 'expected-env'
    expected_env.write_text(_fixed_policy_env())
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
        'EXPECTED_ENV_FILE': str(expected_env),
    }

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(watchdog_audit),
            str(secret_env),
            container_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    latest = state / 'bot-production.entitlement-shadow-control.state'
    assert result.returncode == 0, result.stderr
    assert latest.read_text() == lease.read_text()
    assert container_present.exists()


@pytest.mark.parametrize(
    ('mode', 'expected_container'),
    [('BOOTSTRAP', 'pending'), ('BOOTSTRAP', 'exact'), ('EXPIRY', 'exact')],
)
def test_stale_watchdog_never_removes_newer_generation(
    tmp_path: Path,
    mode: str,
    expected_container: str,
) -> None:
    old_run_id, old_attempt = '600', '1'
    new_run_id, new_attempt = '601', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    lease = runtime / 'lease.state'
    lease.write_text(_lease(new_run_id, new_attempt, 'completed', 4102444800))
    new_container_id = 'f' * 64
    old_container_id = 'a' * 64
    _write_fake_watchdog_docker(fake_bin, new_container_id)
    _write_fake_flock(fake_bin)
    container_present = tmp_path / 'container-present'
    container_present.touch()
    audit = state / f'bot-production.entitlement-shadow-watchdog.{old_run_id}.{old_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{old_run_id}-{old_attempt}.env'
    docker_calls = tmp_path / 'docker-calls'
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': old_run_id,
        'EXPECTED_RUN_ATTEMPT': old_attempt,
        'FAKE_LABEL_RUN_ID': new_run_id,
        'FAKE_LABEL_RUN_ATTEMPT': new_attempt,
        'FAKE_CONTAINER_ID': new_container_id,
        'CONTAINER_PRESENT': str(container_present),
    }
    target = 'pending' if expected_container == 'pending' else old_container_id

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            mode,
            str(lease),
            'teplo_entitlement_shadow',
            old_run_id,
            old_attempt,
            str(audit),
            str(secret_env),
            target,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert lease.read_text() == _lease(new_run_id, new_attempt, 'completed', 4102444800)
    assert container_present.exists()
    assert not docker_calls.exists() or 'rm --force' not in docker_calls.read_text()


def test_stale_disable_helper_never_removes_newer_generation(tmp_path: Path) -> None:
    old_run_id, old_attempt = '700', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    patched_disable = tmp_path / 'disable.sh'
    patched_disable.write_text(
        DISABLE.read_text()
        .replace('/var/lib/teplo-vpn/deploy-state', str(state))
        .replace('/var/lib/teplo-vpn/entitlement-shadow-runtime', str(runtime))
    )
    patched_disable.chmod(0o755)
    _write_fake_watchdog_docker(fake_bin, '1' * 64)
    _write_fake_flock(fake_bin)
    lease = runtime / 'lease.state'
    lease.write_text(_lease(old_run_id, old_attempt, 'completed', 4102444800))
    container_present = tmp_path / 'container-present'
    container_present.touch()
    docker_calls = tmp_path / 'docker-calls'
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': old_run_id,
        'EXPECTED_RUN_ATTEMPT': old_attempt,
        'FAKE_CONTAINER_ID': '1' * 64,
        'CONTAINER_PRESENT': str(container_present),
    }

    result = subprocess.run(  # noqa: S603 - isolated patched production primitive
        [
            str(patched_disable),
            'a' * 40,
            old_run_id,
            old_attempt,
            'owner',
            'gate2-test',
            str(state),
            str(runtime),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert not container_present.exists()
    keyed = state / f'bot-production.entitlement-shadow-control.{old_run_id}.{old_attempt}.audit'
    latest = state / 'bot-production.entitlement-shadow-control.state'
    assert 'action=DISABLE_SHADOW\n' in keyed.read_text()
    assert latest.read_text() == keyed.read_text()

    # A lost response after the keyed write is repaired by the durable helper.
    latest.unlink()
    tombstone = runtime / 'disable.state'
    tombstone.write_text(
        'format_version=1\n'
        f'workflow_sha={"a" * 40}\n'
        f'workflow_run_id={old_run_id}\n'
        f'workflow_run_attempt={old_attempt}\n'
        'approval_actor=owner\n'
        'release_card_reference=gate2-test\n'
    )
    helper = state / f'entitlement-shadow-disable-{"a" * 40}-{old_run_id}-{old_attempt}.sh'
    recovery = subprocess.run(  # noqa: S603 - generated immutable helper under test
        [
            str(helper),
            str(lease),
            str(tombstone),
            'teplo_entitlement_shadow',
            'pending',
            old_run_id,
            old_attempt,
            old_run_id,
            old_attempt,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert recovery.returncode == 0, recovery.stderr
    assert latest.read_text() == keyed.read_text()


def test_async_disable_helper_serializes_before_reading_generation(tmp_path: Path) -> None:
    old_run_id, old_attempt = '710', '1'
    new_run_id, new_attempt = '711', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    patched_disable = tmp_path / 'disable.sh'
    patched_disable.write_text(
        DISABLE.read_text()
        .replace('/var/lib/teplo-vpn/deploy-state', str(state))
        .replace('/var/lib/teplo-vpn/entitlement-shadow-runtime', str(runtime))
    )
    patched_disable.chmod(0o755)
    old_container_id = '2' * 64
    _write_fake_watchdog_docker(fake_bin, old_container_id)
    _write_fake_flock(fake_bin)
    lease = runtime / 'lease.state'
    lease.write_text(_lease(old_run_id, old_attempt, 'completed', 4102444800))
    container_present = tmp_path / 'container-present'
    container_present.touch()
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
        'EXPECTED_RUN_ID': old_run_id,
        'EXPECTED_RUN_ATTEMPT': old_attempt,
        'FAKE_CONTAINER_ID': old_container_id,
        'CONTAINER_PRESENT': str(container_present),
    }

    initial = subprocess.run(  # noqa: S603 - isolated patched production primitive
        [
            str(patched_disable),
            'b' * 40,
            old_run_id,
            old_attempt,
            'owner',
            'gate2-test',
            str(state),
            str(runtime),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert initial.returncode == 0, initial.stderr

    helper = state / f'entitlement-shadow-disable-{"b" * 40}-{old_run_id}-{old_attempt}.sh'
    tombstone = runtime / 'disable.state'
    tombstone.write_text(
        'format_version=1\n'
        f'workflow_sha={"b" * 40}\n'
        f'workflow_run_id={old_run_id}\n'
        f'workflow_run_attempt={old_attempt}\n'
        'approval_actor=owner\n'
        'release_card_reference=gate2-test\n'
    )
    lease.write_text(_lease(old_run_id, old_attempt, 'completed', 4102444800))
    container_present.touch()

    # The generated async helper must acquire the same host lock as ENABLE and
    # DISABLE before it reads either tombstone or lease generation.
    _write_real_flock(fake_bin)
    lock_file = state / 'bot-production.entitlement-shadow-control.lock'
    with lock_file.open('a+') as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        stale = subprocess.Popen(  # noqa: S603 - generated immutable helper under test
            [
                str(helper),
                str(lease),
                str(tombstone),
                'teplo_entitlement_shadow',
                'pending',
                old_run_id,
                old_attempt,
                old_run_id,
                old_attempt,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        time.sleep(0.2)
        assert stale.poll() is None

        # Model a newer serialized control generation completing while the old
        # helper is fenced out. Once admitted, the stale helper must observe
        # the mismatch and leave both the lease and sidecar intact.
        lease.write_text(_lease(new_run_id, new_attempt, 'completed', 4102444800))
        tombstone.write_text(
            'format_version=1\n'
            f'workflow_sha={"c" * 40}\n'
            f'workflow_run_id={new_run_id}\n'
            f'workflow_run_attempt={new_attempt}\n'
            'approval_actor=owner\n'
            'release_card_reference=gate2-new\n'
        )
        fcntl.flock(lock_handle, fcntl.LOCK_UN)

    stdout, stderr = stale.communicate(timeout=5)
    assert stale.returncode == 0, f'{stdout}\n{stderr}'
    assert lease.read_text() == _lease(new_run_id, new_attempt, 'completed', 4102444800)
    assert container_present.exists()


def test_disable_helper_retries_when_timer_fires_before_tombstone_commit(tmp_path: Path) -> None:
    run_id, run_attempt = '720', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    patched_disable = tmp_path / 'disable.sh'
    patched_disable.write_text(
        DISABLE.read_text()
        .replace('/var/lib/teplo-vpn/deploy-state', str(state))
        .replace('/var/lib/teplo-vpn/entitlement-shadow-runtime', str(runtime))
    )
    patched_disable.chmod(0o755)
    container_id = '3' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_present = tmp_path / 'container-present'
    container_present.touch()
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
    }

    initial = subprocess.run(  # noqa: S603 - installs the generated helper
        [
            str(patched_disable),
            'd' * 40,
            run_id,
            run_attempt,
            'owner',
            'gate2-test',
            str(state),
            str(runtime),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert initial.returncode == 0, initial.stderr

    helper = state / f'entitlement-shadow-disable-{"d" * 40}-{run_id}-{run_attempt}.sh'
    tombstone = runtime / 'disable.state'
    keyed = state / f'bot-production.entitlement-shadow-control.{run_id}.{run_attempt}.audit'
    latest = state / 'bot-production.entitlement-shadow-control.state'
    keyed.unlink()
    latest.unlink()
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_present.touch()

    # Model the independent timer firing before the controller's first durable
    # state mutation. It must fail so systemd Restart=on-failure keeps recovery
    # armed instead of treating the missing tombstone as successful cleanup.
    early = subprocess.run(  # noqa: S603 - generated immutable helper under test
        [
            str(helper),
            str(lease),
            str(tombstone),
            'teplo_entitlement_shadow',
            'pending',
            run_id,
            run_attempt,
            run_id,
            run_attempt,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert early.returncode != 0
    assert lease.exists()
    assert container_present.exists()

    # The controller commits the tombstone and then dies. The already armed
    # helper retry must finish removal and durable audit recovery by itself.
    tombstone.write_text(
        'format_version=1\n'
        f'workflow_sha={"d" * 40}\n'
        f'workflow_run_id={run_id}\n'
        f'workflow_run_attempt={run_attempt}\n'
        'approval_actor=owner\n'
        'release_card_reference=gate2-test\n'
    )
    recovered = subprocess.run(  # noqa: S603 - generated immutable helper under test
        [
            str(helper),
            str(lease),
            str(tombstone),
            'teplo_entitlement_shadow',
            'pending',
            run_id,
            run_attempt,
            run_id,
            run_attempt,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert not lease.exists()
    assert not tombstone.exists()
    assert not container_present.exists()
    assert 'action=DISABLE_SHADOW\n' in keyed.read_text()
    assert latest.read_text() == keyed.read_text()


def test_disable_helper_never_audits_transient_inspect_as_absent(tmp_path: Path) -> None:
    run_id, run_attempt = '730', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    patched_disable = tmp_path / 'disable.sh'
    patched_disable.write_text(
        DISABLE.read_text()
        .replace('/var/lib/teplo-vpn/deploy-state', str(state))
        .replace('/var/lib/teplo-vpn/entitlement-shadow-runtime', str(runtime))
    )
    patched_disable.chmod(0o755)
    container_id = '4' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_present = tmp_path / 'container-present'
    container_present.touch()
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
    }

    installed = subprocess.run(  # noqa: S603 - installs generated helper under test
        [
            str(patched_disable),
            'e' * 40,
            run_id,
            run_attempt,
            'owner',
            'gate2-test',
            str(state),
            str(runtime),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert installed.returncode == 0, installed.stderr

    helper = state / f'entitlement-shadow-disable-{"e" * 40}-{run_id}-{run_attempt}.sh'
    tombstone = runtime / 'disable.state'
    keyed = state / f'bot-production.entitlement-shadow-control.{run_id}.{run_attempt}.audit'
    latest = state / 'bot-production.entitlement-shadow-control.state'
    keyed.unlink()
    latest.unlink()
    lease.write_text(_lease(run_id, run_attempt, 'completed', 4102444800))
    container_present.touch()
    tombstone.write_text(
        'format_version=1\n'
        f'workflow_sha={"e" * 40}\n'
        f'workflow_run_id={run_id}\n'
        f'workflow_run_attempt={run_attempt}\n'
        'approval_actor=owner\n'
        'release_card_reference=gate2-test\n'
    )
    fail_once = tmp_path / 'fail-inspect-once'
    fail_once.touch()
    environment['FAIL_INSPECT_ONCE_FILE'] = str(fail_once)

    transient = subprocess.run(  # noqa: S603 - generated immutable helper under test
        [
            str(helper),
            str(lease),
            str(tombstone),
            'teplo_entitlement_shadow',
            'pending',
            run_id,
            run_attempt,
            run_id,
            run_attempt,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert transient.returncode != 0
    assert container_present.exists()
    assert tombstone.exists()
    assert not keyed.exists()
    assert not latest.exists()


def test_watchdog_never_audits_transient_inspect_as_absent(tmp_path: Path) -> None:
    run_id, run_attempt = '731', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    fake_bin = tmp_path / 'bin'
    for directory in (state, runtime, fake_bin):
        directory.mkdir()
    container_id = '5' * 64
    _write_fake_watchdog_docker(fake_bin, container_id)
    _write_fake_flock(fake_bin)
    lease = runtime / 'lease.state'
    lease.write_text(_lease(run_id, run_attempt, 'prepared', 1))
    secret = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret.write_text('SECRET=redacted\n')
    container_present = tmp_path / 'container-present'
    container_present.touch()
    fail_once = tmp_path / 'fail-inspect-once'
    fail_once.touch()
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(tmp_path / 'docker-calls'),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
        'FAKE_CONTAINER_ID': container_id,
        'CONTAINER_PRESENT': str(container_present),
        'FAIL_INSPECT_ONCE_FILE': str(fail_once),
    }

    first = subprocess.run(  # noqa: S603 - fixed repository watchdog under test
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret),
            'pending',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert first.returncode != 0
    assert container_present.exists()
    assert not list(state.glob('*AUTO_DISABLE_BOOTSTRAP.audit'))

    retry = subprocess.run(  # noqa: S603 - fixed repository watchdog under test
        [
            str(WATCHDOG),
            'BOOTSTRAP',
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret),
            'pending',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert retry.returncode == 0, retry.stderr
    assert not container_present.exists()
    assert list(state.glob('*AUTO_DISABLE_BOOTSTRAP.audit'))


def _docker_path() -> str | None:
    return shutil.which('docker')


def test_real_docker_watchdog_removes_running_restart_no_sidecar(tmp_path: Path) -> None:
    """Adversarial proof uses a local scratch image and no network pull."""
    require_real_docker = os.environ.get('TEPLO_REQUIRE_REAL_DOCKER_SHADOW_TEST') == '1'
    docker = _docker_path()
    compiler = shutil.which('cc')
    if docker is None or compiler is None:
        if require_real_docker:
            pytest.fail('real Docker and a C compiler are required by CI')
        pytest.skip('real Docker daemon and C compiler are unavailable')
    docker_info = subprocess.run(  # noqa: S603 - resolved fixed Docker executable
        [docker, 'info'],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if docker_info.returncode != 0:
        if require_real_docker:
            pytest.fail('a working real Docker daemon is required by CI')
        pytest.skip('real Docker daemon is unavailable')

    run_id, run_attempt = '987654321', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    state.mkdir()
    runtime.mkdir()
    source = tmp_path / 'sleeper.c'
    binary = tmp_path / 'sleeper'
    source.write_text('#include <unistd.h>\nint main(void) { sleep(300); return 0; }\n')
    compile_result = subprocess.run(  # noqa: S603 - fixed compiler and generated source
        [compiler, '-static', '-Os', '-o', str(binary), str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    if compile_result.returncode != 0:
        if require_real_docker:
            pytest.fail(f'static C runtime is required in CI: {compile_result.stderr}')
        pytest.skip('static C runtime is unavailable')
    dockerfile = tmp_path / 'Dockerfile'
    dockerfile.write_text('FROM scratch\nCOPY sleeper /sleeper\nENTRYPOINT ["/sleeper"]\n')
    image = f'teplo-shadow-watchdog-test:{os.getpid()}'
    lease = runtime / 'lease.state'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'

    try:
        subprocess.run(  # noqa: S603 - fixed Docker path and generated tag/context
            [docker, 'build', '--quiet', '--tag', image, str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        lease.write_text(_lease(run_id, run_attempt, 'prepared', 1))
        secret_env.write_text('SECRET=redacted\n')
        subprocess.run(  # noqa: S603 - fixed Docker path and generated test image
            [
                docker,
                'run',
                '--detach',
                '--name',
                'teplo_entitlement_shadow',
                '--restart=no',
                '--label',
                'teplo.role=entitlement-shadow-readonly',
                '--label',
                f'teplo.workflow_sha={"a" * 40}',
                '--label',
                f'teplo.workflow_run_id={run_id}',
                '--label',
                f'teplo.workflow_run_attempt={run_attempt}',
                image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # noqa: S603 - fixed Docker path and isolated test container
            [docker, 'pause', 'teplo_entitlement_shadow'],
            check=True,
            capture_output=True,
            text=True,
        )

        result = subprocess.run(  # noqa: S603 - fixed repository watchdog
            [
                str(WATCHDOG),
                'BOOTSTRAP',
                str(lease),
                'teplo_entitlement_shadow',
                run_id,
                run_attempt,
                str(audit),
                str(secret_env),
                'pending',
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        inspect = subprocess.run(  # noqa: S603 - resolved fixed Docker executable
            [docker, 'inspect', 'teplo_entitlement_shadow'],
            check=False,
            capture_output=True,
        )
        assert inspect.returncode != 0
        assert not lease.exists()
        assert not secret_env.exists()
        disabled_audit = (
            state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.AUTO_DISABLE_BOOTSTRAP.audit'
        )
        assert 'action=AUTO_DISABLE_BOOTSTRAP\n' in disabled_audit.read_text()
    finally:
        subprocess.run(  # noqa: S603 - resolved fixed Docker executable
            [docker, 'rm', '--force', 'teplo_entitlement_shadow'],
            check=False,
            capture_output=True,
        )
        subprocess.run(  # noqa: S603 - fixed Docker path and generated test image
            [docker, 'image', 'rm', image],
            check=False,
            capture_output=True,
        )


def test_control_primitives_are_executable_and_shell_is_valid() -> None:
    for path in (BASELINE, ENABLE, DISABLE, SIDECAR, WATCHDOG):
        assert os.access(path, os.X_OK), path
    for path in (ENABLE, DISABLE, WATCHDOG):
        result = subprocess.run(  # noqa: S603 - fixed repository script
            ['/bin/bash', '-n', str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

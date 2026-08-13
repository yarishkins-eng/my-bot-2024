from __future__ import annotations

import os
import shutil
import stat
import subprocess
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

    assert '--name "$SIDECAR"' in source
    assert '--restart=no' in source
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
    ):
        assert forbidden not in extractor


def test_disable_is_independent_of_bot_and_business_systems() -> None:
    source = DISABLE.read_text()
    commands = '\n'.join(line for line in source.lower().splitlines() if not line.lstrip().startswith('#'))

    assert "readonly SIDECAR='teplo_entitlement_shadow'" in source
    assert 'rm -f -- "$LEASE_FILE"' in source
    assert 'docker rm -f "$SIDECAR"' in source
    assert 'docker info' in source
    assert 'systemd-run' in source
    assert 'on-active=60s' in source
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


def _write_fake_watchdog_docker(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
if [ "$1" = inspect ]; then
  case "$*" in
    *State.Running*) printf 'true\n' ;;
    *teplo.role*) printf 'entitlement-shadow-readonly\n' ;;
    *teplo.workflow_sha*) printf '%040d\n' 0 | tr 0 a ;;
    *teplo.workflow_run_id*) printf '%s\n' "$EXPECTED_RUN_ID" ;;
    *teplo.workflow_run_attempt*) printf '%s\n' "$EXPECTED_RUN_ATTEMPT" ;;
  esac
  exit 0
fi
printf '%s\n' "$*" >> "$DOCKER_CALLS"
exit 0
""",
    )


def _lease(run_id: str, run_attempt: str, phase: str, expires: int) -> str:
    completed = 'pending' if phase == 'prepared' else '2026-08-13T00:00:00+03:00'
    return (
        'format_version=2\n'
        f'phase={phase}\n'
        'action=ENABLE_SHADOW\n'
        'runtime_mode=enabled\n'
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
    _write_fake_watchdog_docker(fake_bin)
    _write_fake_flock(fake_bin)
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret_env.write_text('SECRET=redacted\n')
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
    }

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret_env),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not lease.exists()
    assert not secret_env.exists()
    assert docker_calls.read_text().strip() == 'rm -f teplo_entitlement_shadow'
    assert 'action=AUTO_DISABLE_BOOTSTRAP\n' in audit.read_text()
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600


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
    _write_fake_watchdog_docker(fake_bin)
    _write_fake_flock(fake_bin)
    audit = state / f'bot-production.entitlement-shadow-watchdog.{run_id}.{run_attempt}.audit'
    secret_env = state / f'entitlement-shadow-secrets-{run_id}-{run_attempt}.env'
    secret_env.write_text('SECRET=redacted\n')
    environment = os.environ | {
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        'DOCKER_CALLS': str(docker_calls),
        'EXPECTED_RUN_ID': run_id,
        'EXPECTED_RUN_ATTEMPT': run_attempt,
    }

    result = subprocess.run(  # noqa: S603 - fixed repository watchdog
        [
            str(WATCHDOG),
            str(lease),
            'teplo_entitlement_shadow',
            run_id,
            run_attempt,
            str(audit),
            str(secret_env),
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
    assert not docker_calls.exists()


def _docker_path() -> str | None:
    return shutil.which('docker')


def _docker_is_available() -> bool:
    docker = _docker_path()
    if docker is None or shutil.which('cc') is None:
        return False
    return (
        subprocess.run(  # noqa: S603 - resolved fixed Docker executable
            [docker, 'info'],
            check=False,
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )


@pytest.mark.skipif(not _docker_is_available(), reason='real Docker daemon and C compiler are required')
def test_real_docker_watchdog_removes_running_restart_no_sidecar(tmp_path: Path) -> None:
    """Adversarial proof uses a local scratch image and no network pull."""
    run_id, run_attempt = '987654321', '1'
    state = tmp_path / 'state'
    runtime = tmp_path / 'runtime'
    state.mkdir()
    runtime.mkdir()
    source = tmp_path / 'sleeper.c'
    binary = tmp_path / 'sleeper'
    docker = _docker_path()
    compiler = shutil.which('cc')
    assert docker is not None and compiler is not None
    source.write_text('#include <unistd.h>\nint main(void) { sleep(300); return 0; }\n')
    compile_result = subprocess.run(  # noqa: S603 - fixed compiler and generated source
        [compiler, '-static', '-Os', '-o', str(binary), str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    if compile_result.returncode != 0:
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

        result = subprocess.run(  # noqa: S603 - fixed repository watchdog
            [
                str(WATCHDOG),
                str(lease),
                'teplo_entitlement_shadow',
                run_id,
                run_attempt,
                str(audit),
                str(secret_env),
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
        assert 'action=AUTO_DISABLE_BOOTSTRAP\n' in audit.read_text()
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

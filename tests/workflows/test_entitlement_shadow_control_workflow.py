from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
BASELINE_VERIFIER = ROOT / '.github/scripts/verify-entitlement-shadow-baseline.py'
CONTROL_SCRIPT = ROOT / '.github/scripts/control-entitlement-shadow.sh'
CONTROL_CLASSIFIER = ROOT / '.github/scripts/classify-entitlement-shadow-control.sh'
CONTROL_WORKFLOW = ROOT / '.github/workflows/control-entitlement-shadow.yml'

TARGET_SHA = 'a' * 40
DEPLOYED_SHA = 'b' * 40
IMAGE_ID = f'sha256:{"c" * 64}'
SERVICE_IMAGE = 'teplo-bot:production'


def _safe_env() -> str:
    return 'UNRELATED_SECRET=must-stay-unchanged\n'


def _explicit_safe_env() -> str:
    return (
        _safe_env()
        + 'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=false\n'
        + 'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false\n'
        + 'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=false\n'
        + 'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false\n'
        + 'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=true\n'
    )


@pytest.mark.parametrize(
    ('replacement', 'stop_code'),
    [
        ('ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=true', 'managed_flag_not_safe'),
        ('ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=true', 'managed_flag_not_safe'),
        ('ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=false', 'managed_flag_not_safe'),
        (
            'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false\nENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false',
            'managed_flag_duplicate',
        ),
    ],
)
def test_baseline_verifier_refuses_unsafe_or_ambiguous_matrix(
    tmp_path: Path,
    replacement: str,
    stop_code: str,
) -> None:
    env_file = tmp_path / '.env'
    content = _explicit_safe_env()
    if 'PROJECTOR' in replacement:
        content = content.replace('ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false', replacement)
    elif 'KILL_SWITCH' in replacement:
        content = content.replace('ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=true', replacement)
    else:
        content = content.replace('ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false', replacement)
    env_file.write_text(content)
    env_file.chmod(0o600)

    result = subprocess.run(  # noqa: S603 - fixed repository verifier
        [str(BASELINE_VERIFIER), '--env-file', str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert result.stderr.strip() == f'STOP:{stop_code}'
    assert 'UNRELATED_SECRET' not in result.stderr


@pytest.mark.parametrize('content', [_safe_env(), _explicit_safe_env()])
def test_baseline_verifier_accepts_absent_defaults_or_explicit_safe_values(tmp_path: Path, content: str) -> None:
    env_file = tmp_path / '.env'
    env_file.write_text(content)
    env_file.chmod(0o644)

    result = subprocess.run(  # noqa: S603 - fixed repository verifier
        [str(BASELINE_VERIFIER), '--env-file', str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == 'BASELINE_SAFE'
    env_file.chmod(0o666)
    refused = subprocess.run(  # noqa: S603 - fixed repository verifier
        [str(BASELINE_VERIFIER), '--env-file', str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert refused.returncode == 64
    assert refused.stderr.strip() == 'STOP:env_is_group_or_world_writable'


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _control_integration(
    tmp_path: Path,
    action: str,
    *,
    deployed_sha: str = TARGET_SHA,
    fail_enabled_health_once: bool = False,
    fail_disabled_health_once: bool = False,
    migration_phase: str = 'completed',
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    repo = tmp_path / 'repo'
    state = tmp_path / 'state'
    fake = tmp_path / 'fake'
    fake_bin = tmp_path / 'bin'
    for directory in (repo, state, fake, fake_bin):
        directory.mkdir()
    (repo / '.github/scripts').mkdir(parents=True)
    verifier = repo / '.github/scripts/verify-entitlement-shadow-baseline.py'
    verifier.write_bytes(BASELINE_VERIFIER.read_bytes())
    verifier.chmod(0o755)
    classifier = repo / '.github/scripts/classify-entitlement-shadow-control.sh'
    classifier.write_bytes(CONTROL_CLASSIFIER.read_bytes())
    classifier.chmod(0o755)
    (repo / 'docker-compose.yml').write_text('services:\n  bot: {}\n')
    (repo / '.env').write_text(_safe_env())
    (repo / '.env').chmod(0o600)
    (state / 'bot-production.state').write_text(f'sha={deployed_sha}\nimage={IMAGE_ID}\n')
    (state / 'bot-production.migration-recovery.state').write_text(f'phase={migration_phase}\n')
    (fake / 'source').write_text(deployed_sha)
    (fake / 'image').write_text(IMAGE_ID)
    (fake / 'service_image').write_text(SERVICE_IMAGE)
    (fake / 'health').write_text('healthy')
    (fake / 'mode').write_text('disabled')
    (fake / 'started_at').write_text('2026-08-13T00:00:00Z')
    if fail_enabled_health_once:
        (fake / 'fail_enabled_health_once').touch()
    if fail_disabled_health_once:
        (fake / 'fail_disabled_start_once').touch()

    _write_executable(
        fake_bin / 'git',
        r"""#!/usr/bin/env bash
set -eu
case "$1" in
  status) exit 0 ;;
  fetch) exit 0 ;;
  rev-parse)
    case "$2" in
      HEAD) cat "$FAKE_STATE/source" ;;
      origin/main) printf '%s\n' "$TARGET_SHA" ;;
      *:*) printf '%s\n' 'dddddddddddddddddddddddddddddddddddddddd' ;;
      *) printf 'unexpected rev-parse: %s\n' "$2" >&2; exit 97 ;;
    esac
    ;;
  merge-base|cat-file) exit 0 ;;
  *) printf 'unexpected git: %s\n' "$*" >&2; exit 98 ;;
esac
""",
    )
    _write_executable(
        fake_bin / 'df',
        "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\nx 1 1 9999999 1%% /\\n'\n",
    )
    _write_executable(
        fake_bin / 'date',
        '#!/usr/bin/env bash\nif [ "$1" = \'--iso-8601=seconds\' ]; then printf \'2026-08-13T00:00:00+03:00\\n\'; else /bin/date "$@"; fi\n',
    )
    _write_executable(fake_bin / 'sleep', '#!/usr/bin/env bash\nexit 0\n')
    _write_executable(
        fake_bin / 'sha256sum',
        '#!/usr/bin/env bash\n/usr/bin/shasum -a 256 "$1" | awk \'{ print $1 "  " $2 }\'\n',
    )
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
set -eu
if [ "$1" = 'compose' ]; then
  shift
  override=''
  while [ "$1" = '-f' ]; do
    if [ -n "$override" ]; then override="$2"; else override='base'; fi
    shift 2
  done
  command="$1"
  case "$command" in
    exec) printf '0103\n' ;;
    stop) printf 'stopped\n' > "$FAKE_STATE/health" ;;
    create)
      if grep -F "ENTITLEMENT_AUTHORITY_SHADOW_ENABLED: 'true'" "$override" >/dev/null; then
        printf 'enabled\n' > "$FAKE_STATE/mode"
      else
        printf 'disabled\n' > "$FAKE_STATE/mode"
      fi
      printf 'created\n' > "$FAKE_STATE/health"
      ;;
    *) printf 'unexpected compose: %s\n' "$command" >&2; exit 97 ;;
  esac
elif [ "$1" = 'inspect' ]; then
  format="$3"
  case "$format" in
    *'.Image'*) cat "$FAKE_STATE/image" ;;
    *'.Config.Image'*) cat "$FAKE_STATE/service_image" ;;
    *'.State.Health.Status'*) cat "$FAKE_STATE/health" ;;
    *'.State.StartedAt'*) cat "$FAKE_STATE/started_at" ;;
    *'.Config.Env'*)
      mode="$(cat "$FAKE_STATE/mode")"
      if [ "$mode" = 'enabled' ]; then
        envs='ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=false ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=false ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=true ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=false'
      else
        envs='ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED=false ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED=false ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED=false ENTITLEMENT_AUTHORITY_SHADOW_ENABLED=false ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH=true'
      fi
      expected="$(printf '%s' "$format" | sed -n 's/.*eq \. "\([^"]*\)".*/\1/p')"
      case " $envs " in *" $expected "*) printf x ;; esac
      ;;
    *) printf 'unexpected inspect: %s\n' "$format" >&2; exit 96 ;;
  esac
elif [ "$1" = 'start' ]; then
  mode="$(cat "$FAKE_STATE/mode")"
  if [ "$mode" = 'disabled' ]; then
    marker="$FAKE_STATE/fail_disabled_start_once"
  else
    marker="$FAKE_STATE/fail_enabled_health_once"
  fi
  if [ -e "$marker" ]; then
        rm "$marker"
        printf 'unhealthy\n' > "$FAKE_STATE/health"
        exit 55
      else
        printf 'healthy\n' > "$FAKE_STATE/health"
      fi
  printf 'started\n'
elif [ "$1" = 'stop' ]; then
  printf 'stopped\n' > "$FAKE_STATE/health"
elif [ "$1" = 'top' ]; then
  printf 'ARGS\n/app/.venv/bin/python main.py\n'
elif [ "$1" = 'logs' ]; then
  if [ "$(cat "$FAKE_STATE/mode")" = 'enabled' ]; then
    printf 'Read-only shadow запущен\n'
  else
    printf 'SHADOW=false\n'
  fi
else
  printf 'unexpected docker: %s\n' "$*" >&2
  exit 95
fi
""",
    )

    environment = os.environ.copy()
    environment.update(
        {
            'PATH': f'{fake_bin}:{environment["PATH"]}',
            'FAKE_STATE': str(fake),
            'TARGET_SHA': TARGET_SHA,
        }
    )
    result = subprocess.run(  # noqa: S603 - fixed repository control script
        [
            str(CONTROL_SCRIPT),
            action,
            TARGET_SHA,
            '12345',
            '1',
            str(repo),
            str(state),
            deployed_sha,
            str(classifier),
            str(verifier),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    return result, {'repo': repo, 'state': state, 'fake': fake}


def test_enable_transition_preserves_dotenv_and_writes_keyed_audit(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'ENABLE_SHADOW')

    assert result.returncode == 0, result.stderr
    assert (paths['fake'] / 'mode').read_text() == 'enabled\n'
    assert (paths['repo'] / '.env').read_text() == _safe_env()
    keyed = paths['state'] / 'bot-production.entitlement-shadow-control.12345.1.audit'
    assert keyed.is_file()
    assert stat.S_IMODE(keyed.stat().st_mode) == 0o600
    assert 'runtime_mode=enabled\n' in keyed.read_text()


def test_failed_enable_recovers_disabled_without_changing_dotenv(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'ENABLE_SHADOW', fail_enabled_health_once=True)

    assert result.returncode != 0
    assert (paths['fake'] / 'mode').read_text() == 'disabled\n'
    assert (paths['fake'] / 'health').read_text() == 'healthy\n'
    assert (paths['repo'] / '.env').read_text() == _safe_env()
    assert 'Fail-safe disabled runtime restored and verified.' in result.stderr


def test_failed_recovery_leaves_bot_stopped(tmp_path: Path) -> None:
    result, paths = _control_integration(
        tmp_path,
        'ENABLE_SHADOW',
        fail_enabled_health_once=True,
        fail_disabled_health_once=True,
    )

    assert result.returncode == 91, result.stderr
    assert (paths['fake'] / 'health').read_text() == 'stopped\n'
    assert 'CRITICAL: fail-safe disable could not be proven; bot was left stopped.' in result.stderr


def test_disable_can_run_when_main_is_newer_than_deployed_source(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'DISABLE_SHADOW', deployed_sha=DEPLOYED_SHA)

    assert result.returncode == 0, result.stderr
    assert (paths['fake'] / 'mode').read_text() == 'disabled\n'


def test_disable_remains_available_during_prepared_migration_journal(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'DISABLE_SHADOW', migration_phase='prepared')

    assert result.returncode == 0, result.stderr
    assert (paths['fake'] / 'mode').read_text() == 'disabled\n'


def test_enable_requires_completed_migration_journal(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'ENABLE_SHADOW', migration_phase='prepared')

    assert result.returncode == 64
    assert 'STOP:migration_state_not_completed' in result.stderr
    assert (paths['fake'] / 'mode').read_text() == 'disabled'


def test_enable_refuses_when_deployed_source_is_not_exact_main(tmp_path: Path) -> None:
    result, paths = _control_integration(tmp_path, 'ENABLE_SHADOW', deployed_sha=DEPLOYED_SHA)

    assert result.returncode == 64
    assert 'STOP:enable_requires_exact_main' in result.stderr
    assert (paths['fake'] / 'mode').read_text() == 'disabled'


def test_control_workflow_has_only_allowlisted_actions_and_protected_environment() -> None:
    workflow_text = CONTROL_WORKFLOW.read_text()
    workflow = yaml.safe_load(workflow_text)
    dispatch = workflow[True]['workflow_dispatch']['inputs']

    assert dispatch['action']['type'] == 'choice'
    assert dispatch['action']['options'] == ['DISABLE_SHADOW', 'ENABLE_SHADOW']
    assert dispatch['expected_deployed_sha']['type'] == 'string'
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['concurrency']['group'] == 'teplo-bot-production-deploy'
    assert workflow['jobs']['control']['environment'] == 'teplo-vpn-production-controlled-change'
    assert workflow['jobs']['verify']['uses'] == './.github/workflows/lint.yml'
    assert workflow['jobs']['control']['needs'] == ['verify']
    assert 'github.ref' in workflow_text
    assert 'refs/heads/main' in workflow_text
    assert "hashFiles('.github/scripts/control-entitlement-shadow.sh')" in workflow_text
    assert "hashFiles('.github/scripts/classify-entitlement-shadow-control.sh')" in workflow_text
    assert "hashFiles('.github/scripts/verify-entitlement-shadow-baseline.py')" in workflow_text
    assert 'raw.githubusercontent.com' in workflow_text
    assert 'CONTROL_SCRIPT_SHA256' in workflow_text
    assert 'sed -i' not in workflow_text
    assert '>> .env' not in workflow_text
    assert 'POST' not in workflow_text
    assert 'PATCH' not in workflow_text
    assert 'DELETE' not in workflow_text
    assert 'alembic' not in workflow_text.lower()


def test_control_scripts_are_executable() -> None:
    assert os.access(BASELINE_VERIFIER, os.X_OK)
    assert os.access(CONTROL_CLASSIFIER, os.X_OK)
    assert os.access(CONTROL_SCRIPT, os.X_OK)


@pytest.mark.parametrize(
    ('action', 'workflow_sha', 'main_sha', 'deployed_sha', 'expected_result'),
    [
        ('ENABLE_SHADOW', TARGET_SHA, TARGET_SHA, TARGET_SHA, 'enable_exact'),
        ('DISABLE_SHADOW', DEPLOYED_SHA, TARGET_SHA, DEPLOYED_SHA, 'disable_compatible_check_required'),
    ],
)
def test_control_classifier_accepts_only_bound_state(
    action: str,
    workflow_sha: str,
    main_sha: str,
    deployed_sha: str,
    expected_result: str,
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository classifier
        [
            str(CONTROL_CLASSIFIER),
            action,
            workflow_sha,
            deployed_sha,
            main_sha,
            deployed_sha,
            IMAGE_ID,
            IMAGE_ID,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected_result


@pytest.mark.parametrize(
    ('action', 'workflow_sha', 'main_sha', 'deployed_sha', 'owner_sha', 'live_image', 'stop_code'),
    [
        ('ENABLE_SHADOW', DEPLOYED_SHA, TARGET_SHA, TARGET_SHA, TARGET_SHA, IMAGE_ID, 'workflow_is_not_current_main'),
        ('ENABLE_SHADOW', TARGET_SHA, TARGET_SHA, DEPLOYED_SHA, DEPLOYED_SHA, IMAGE_ID, 'enable_requires_exact_main'),
        (
            'DISABLE_SHADOW',
            TARGET_SHA,
            TARGET_SHA,
            DEPLOYED_SHA,
            TARGET_SHA,
            IMAGE_ID,
            'owner_expected_deployed_sha_mismatch',
        ),
        (
            'DISABLE_SHADOW',
            TARGET_SHA,
            TARGET_SHA,
            TARGET_SHA,
            TARGET_SHA,
            f'sha256:{"e" * 64}',
            'deploy_state_image_mismatch',
        ),
    ],
)
def test_control_classifier_refuses_mismatched_state(
    action: str,
    workflow_sha: str,
    main_sha: str,
    deployed_sha: str,
    owner_sha: str,
    live_image: str,
    stop_code: str,
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository classifier
        [
            str(CONTROL_CLASSIFIER),
            action,
            workflow_sha,
            owner_sha,
            main_sha,
            deployed_sha,
            IMAGE_ID,
            live_image,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert result.stderr.strip() == f'STOP:{stop_code}'

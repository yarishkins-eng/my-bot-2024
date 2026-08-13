from __future__ import annotations

import subprocess
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLASSIFIER = ROOT / '.github/scripts/classify-migration-recovery.sh'
DEPLOY_WORKFLOW = ROOT / '.github/workflows/deploy-migration.yml'
RECOVERY_WORKFLOW = ROOT / '.github/workflows/recover-after-migration.yml'
ORDINARY_DEPLOY_WORKFLOW = ROOT / '.github/workflows/deploy.yml'
INFRASTRUCTURE_DEPLOY_WORKFLOW = ROOT / '.github/workflows/deploy-infrastructure.yml'

TARGET_SHA = 'a' * 40
ROLLBACK_SHA = 'b' * 40
PRIOR_TARGET_SHA = 'e' * 40
HISTORICAL_TARGET_SHA = '1' * 40
HISTORICAL_ROLLBACK_SHA = '2' * 40
MIGRATION_IMAGE = f'sha256:{"c" * 64}'
ROLLBACK_IMAGE = f'sha256:{"d" * 64}'
PREVIOUS_SCHEMA = '0102'
TARGET_SCHEMA = '0103'


@dataclass(frozen=True)
class Scenario:
    phase: str = 'prepared'
    actual_schema: str = PREVIOUS_SCHEMA
    compatible: bool = True
    current_source: str = TARGET_SHA
    current_image: str = ROLLBACK_IMAGE
    deploy_state: str = 'previous'


def _recovery_state(scenario: Scenario) -> str:
    return (
        'format_version=2\n'
        f'phase={scenario.phase}\n'
        f'deployed_sha={TARGET_SHA}\n'
        f'rollback_source_sha={ROLLBACK_SHA}\n'
        f'rollback_image_tag=teplo-vpn-rollback/bot:pre-migration-{TARGET_SHA}\n'
        f'rollback_image_id={ROLLBACK_IMAGE}\n'
        f'migration_image_id={MIGRATION_IMAGE}\n'
        f'previous_schema_revisions={PREVIOUS_SCHEMA}\n'
        f'target_schema_revisions={TARGET_SCHEMA}\n'
        f'old_image_target_schema_compatible={int(scenario.compatible)}\n'
    )


def _deploy_state(scenario: Scenario) -> str:
    if scenario.deploy_state == 'previous':
        return f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    if scenario.deploy_state == 'target':
        return f'sha={TARGET_SHA}\nimage={MIGRATION_IMAGE}\n'
    if scenario.deploy_state == 'recovery':
        return (
            f'sha={ROLLBACK_SHA}\n'
            f'image={ROLLBACK_IMAGE}\n'
            'mode=recovery\n'
            f'schema_revisions={scenario.actual_schema}\n'
            f'recovery_from_sha={TARGET_SHA}\n'
        )
    raise AssertionError(f'unknown test deploy state: {scenario.deploy_state}')


def _completed_v1_recovery_journal(*, phase: str = 'completed') -> str:
    return (
        'format_version=1\n'
        f'phase={phase}\n'
        f'deployed_sha={HISTORICAL_TARGET_SHA}\n'
        f'rollback_source_sha={HISTORICAL_ROLLBACK_SHA}\n'
        f'rollback_image_tag=teplo-vpn-rollback/bot:pre-migration-{HISTORICAL_TARGET_SHA}\n'
        f'rollback_image_id={ROLLBACK_IMAGE}\n'
        f'migration_image_id={MIGRATION_IMAGE}\n'
        'previous_schema_revisions=0101\n'
        f'target_schema_revisions={PREVIOUS_SCHEMA}\n'
    )


def _recovery_audit(*, schema: str = PREVIOUS_SCHEMA, image: str = ROLLBACK_IMAGE) -> str:
    return (
        'recovered_at=2026-08-13T12:00:00+03:00\n'
        f'source_sha={ROLLBACK_SHA}\n'
        f'image={image}\n'
        f'schema_revisions={schema}\n'
        f'from_migration_sha={TARGET_SHA}\n'
    )


def classify(tmp_path: Path, scenario: Scenario) -> subprocess.CompletedProcess[str]:
    recovery = tmp_path / 'migration-recovery.state'
    deployed = tmp_path / 'production.state'
    recovery.write_text(_recovery_state(scenario))
    deployed.write_text(_deploy_state(scenario))
    return subprocess.run(  # noqa: S603 - executes the fixed repository script under test
        [
            str(CLASSIFIER),
            str(recovery),
            str(deployed),
            TARGET_SHA,
            ROLLBACK_SHA,
            TARGET_SHA,
            scenario.current_source,
            scenario.current_image,
            scenario.actual_schema,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _write_fake_flock(path: Path, *, barrier: bool = False) -> None:
    if not barrier:
        _write_executable(path, '#!/usr/bin/env bash\nexit 0\n')
        return
    _write_executable(
        path,
        r"""#!/usr/bin/env bash
set -eu
touch "$FAKE_STATE/flock_entered"
while [ ! -e "$FAKE_STATE/flock_release" ]; do sleep 0.02; done
[ ! -e "$FAKE_STATE/mutations" ] || touch "$FAKE_STATE/mutation_before_flock_release"
exit 0
""",
    )


def _extract_recovery_shell(repo_dir: Path, state_dir: Path) -> str:
    workflow = RECOVERY_WORKFLOW.read_text()
    step = workflow.index('- name: Recover only the captured pre-migration bot image')
    marker = workflow.index('          script: |\n', step)
    script = textwrap.dedent(workflow[marker + len('          script: |\n') :])
    return script.replace(
        "readonly REPO_DIR='/opt/remnawave-bedolaga-telegram-bot'",
        f"readonly REPO_DIR='{repo_dir}'",
    ).replace(
        "readonly STATE_DIR='/var/lib/teplo-vpn/deploy-state'",
        f"readonly STATE_DIR='{state_dir}'",
    )


def _extract_deploy_shell(repo_dir: Path, state_dir: Path) -> str:
    workflow = DEPLOY_WORKFLOW.read_text()
    step = workflow.index('- name: Deploy the exact migration revision and wait for health')
    marker = workflow.index('          script: |\n', step)
    script = textwrap.dedent(workflow[marker + len('          script: |\n') :])
    return (
        script.replace(
            "readonly REPO_DIR='/opt/remnawave-bedolaga-telegram-bot'",
            f"readonly REPO_DIR='{repo_dir}'",
        )
        .replace(
            "readonly STATE_DIR='/var/lib/teplo-vpn/deploy-state'",
            f"readonly STATE_DIR='{state_dir}'",
        )
        .replace("readonly TARGET_SHA='${{ github.sha }}'", f"readonly TARGET_SHA='{TARGET_SHA}'")
    )


def _extract_ordinary_deploy_shell(repo_dir: Path, state_dir: Path) -> str:
    workflow = ORDINARY_DEPLOY_WORKFLOW.read_text()
    step = workflow.index('- name: Deploy the exact non-migration revision and wait for health')
    marker = workflow.index('          script: |\n', step)
    script = textwrap.dedent(workflow[marker + len('          script: |\n') :])
    return (
        script.replace(
            "readonly REPO_DIR='/opt/remnawave-bedolaga-telegram-bot'",
            f"readonly REPO_DIR='{repo_dir}'",
        )
        .replace(
            "readonly STATE_DIR='/var/lib/teplo-vpn/deploy-state'",
            f"readonly STATE_DIR='{state_dir}'",
        )
        .replace("readonly TARGET_SHA='${{ github.sha }}'", f"readonly TARGET_SHA='{PRIOR_TARGET_SHA}'")
    )


def _extract_infrastructure_deploy_shell(repo_dir: Path, state_dir: Path) -> str:
    workflow = INFRASTRUCTURE_DEPLOY_WORKFLOW.read_text()
    step = workflow.index('- name: Deploy the exact controlled infrastructure revision and wait for health')
    marker = workflow.index('          script: |\n', step)
    script = textwrap.dedent(workflow[marker + len('          script: |\n') :])
    return (
        script.replace(
            "readonly REPO_DIR='/opt/remnawave-bedolaga-telegram-bot'",
            f"readonly REPO_DIR='{repo_dir}'",
        )
        .replace(
            "readonly STATE_DIR='/var/lib/teplo-vpn/deploy-state'",
            f"readonly STATE_DIR='{state_dir}'",
        )
        .replace("readonly TARGET_SHA='${{ github.sha }}'", f"readonly TARGET_SHA='{PRIOR_TARGET_SHA}'")
    )


def _recovery_integration(
    tmp_path: Path,
    scenario: Scenario,
    *,
    fail_up_once: bool = False,
    fail_checkout_once: bool = False,
    fail_audit_once: bool = False,
    fail_journal_mark_once: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    repo = tmp_path / 'repo'
    state = tmp_path / 'state'
    fake_bin = tmp_path / 'bin'
    fake = tmp_path / 'fake'
    for directory in (repo, state, fake_bin, fake):
        directory.mkdir()

    (repo / '.github/scripts').mkdir(parents=True)
    classifier_copy = repo / '.github/scripts/classify-migration-recovery.sh'
    classifier_copy.write_bytes(CLASSIFIER.read_bytes())
    classifier_copy.chmod(0o755)
    (state / 'bot-production.migration-recovery.state').write_text(_recovery_state(scenario))
    (state / 'bot-production.state').write_text(_deploy_state(scenario))
    (fake / 'source').write_text(scenario.current_source)
    (fake / 'image').write_text(scenario.current_image)
    (fake / 'service_image').write_text('teplo-bot:production')
    (fake / 'service_target').write_text(scenario.current_image)
    (fake / 'schema').write_text(scenario.actual_schema)
    if fail_up_once:
        (fake / 'fail_up_once').touch()
    if fail_checkout_once:
        (fake / 'fail_checkout_once').touch()
    if fail_audit_once:
        (fake / 'fail_audit_once').touch()
    if fail_journal_mark_once:
        (fake / 'fail_journal_mark_once').touch()

    _write_executable(
        fake_bin / 'git',
        r"""#!/usr/bin/env bash
set -eu
case "$1" in
  status) exit 0 ;;
  fetch|cat-file) exit 0 ;;
  rev-parse)
    if [ "$2" = 'HEAD' ]; then cat "$FAKE_STATE/source"; else printf '%s\n' "$TARGET_SHA"; fi
    ;;
  merge-base|diff) exit 0 ;;
  checkout)
    if [ -e "$FAKE_STATE/fail_checkout_once" ]; then
      rm "$FAKE_STATE/fail_checkout_once"
      exit 57
    fi
    printf '%s\n' "$3" > "$FAKE_STATE/source"
    ;;
  *) printf 'unexpected fake git call: %s\n' "$*" >&2; exit 98 ;;
esac
""",
    )
    _write_fake_flock(fake_bin / 'flock')
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
set -eu
if [ "$1" = 'compose' ]; then
  case " $* " in
    *' exec -T postgres '*) cat "$FAKE_STATE/schema" ;;
    *' up -d '*)
      if [ -e "$FAKE_STATE/fail_up_once" ]; then
        rm "$FAKE_STATE/fail_up_once"
        exit 56
      fi
      cp "$FAKE_STATE/service_target" "$FAKE_STATE/image"
      ;;
    *' ps bot '*) printf 'bot healthy\n' ;;
    *) printf 'unexpected fake compose call: %s\n' "$*" >&2; exit 97 ;;
  esac
elif [ "$1" = 'info' ]; then
  exit 0
elif [ "$1" = 'inspect' ] && [ "${@: -1}" = 'teplo_entitlement_shadow' ]; then
  exit 1
elif [ "$1" = 'inspect' ]; then
  case "$3" in
    *'.Image'*) cat "$FAKE_STATE/image" ;;
    *'.Config.Image'*) cat "$FAKE_STATE/service_image" ;;
    *'.State.Health.Status'*) printf 'healthy\n' ;;
    *'.State.StartedAt'*) printf '2026-08-13T00:00:00Z\n' ;;
    *) printf 'unexpected fake inspect format: %s\n' "$3" >&2; exit 96 ;;
  esac
elif [ "$1" = 'image' ] && [ "$2" = 'inspect' ]; then
  if [ "$3" = '--format' ]; then printf '%s\n' "$ROLLBACK_IMAGE"; else exit 0; fi
elif [ "$1" = 'image' ] && [ "$2" = 'tag' ]; then
  case "$3" in
    teplo-vpn-rollback/bot:*) printf '%s\n' "$ROLLBACK_IMAGE" > "$FAKE_STATE/service_target" ;;
    *) printf '%s\n' "$3" > "$FAKE_STATE/service_target" ;;
  esac
  printf 'tag\n' >> "$FAKE_STATE/mutations"
elif [ "$1" = 'logs' ]; then
  printf 'SKIP_MIGRATION=true\nAiogram polling запущен\n'
else
  printf 'unexpected fake docker call: %s\n' "$*" >&2
  exit 95
fi
""",
    )
    _write_executable(
        fake_bin / 'df',
        "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/fake 9999999 1 9999998 1%% /\\n'\n",
    )
    _write_executable(
        fake_bin / 'date',
        "#!/usr/bin/env bash\nprintf '2026-08-13T00:00:00+00:00\\n'\n",
    )
    _write_executable(
        fake_bin / 'mv',
        r"""#!/usr/bin/env bash
set -eu
target="${@: -1}"
if [[ "$target" == *.audit ]] && [ -e "$FAKE_STATE/fail_audit_once" ]; then
  rm "$FAKE_STATE/fail_audit_once"
  exit 58
fi
if [[ "$target" == *.migration-recovery.state ]] && grep -q '^phase=recovered$' "$1" && [ -e "$FAKE_STATE/fail_journal_mark_once" ]; then
  rm "$FAKE_STATE/fail_journal_mark_once"
  exit 59
fi
exec /bin/mv "$@"
""",
    )

    shell = tmp_path / 'recovery.sh'
    _write_executable(shell, _extract_recovery_shell(repo, state))
    env = {
        'PATH': f'{fake_bin}:/usr/bin:/bin',
        'TARGET_SHA': TARGET_SHA,
        'REQUESTED_ROLLBACK_SHA': ROLLBACK_SHA,
        'FAKE_STATE': str(fake),
        'ROLLBACK_IMAGE': ROLLBACK_IMAGE,
    }
    result = subprocess.run(  # noqa: S603 - executes extracted production workflow shell
        ['/bin/bash', str(shell)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, {
        'repo': repo,
        'state': state,
        'fake': fake,
        'shell': shell,
    }


def _rerun_recovery_integration(paths: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    env = {
        'PATH': f'{paths["shell"].parent / "bin"}:/usr/bin:/bin',
        'TARGET_SHA': TARGET_SHA,
        'REQUESTED_ROLLBACK_SHA': ROLLBACK_SHA,
        'FAKE_STATE': str(paths['fake']),
        'ROLLBACK_IMAGE': ROLLBACK_IMAGE,
    }
    return subprocess.run(  # noqa: S603 - executes extracted production workflow shell
        ['/bin/bash', str(paths['shell'])],
        cwd=paths['repo'],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _deploy_normalization_integration(
    tmp_path: Path,
    deploy_state: str,
    *,
    fail_git_guard: str | None = None,
    hard_kill_at: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    repo = tmp_path / 'repo'
    state = tmp_path / 'state'
    fake_bin = tmp_path / 'bin'
    fake = tmp_path / 'fake'
    for directory in (repo, state, fake_bin, fake):
        directory.mkdir()
    (state / 'bot-production.state').write_text(deploy_state)
    (fake / 'schema').write_text(PREVIOUS_SCHEMA)
    (fake / 'source').write_text(ROLLBACK_SHA)
    if fail_git_guard is not None:
        (fake / fail_git_guard).touch()
    if hard_kill_at is not None:
        (fake / f'hard_kill_{hard_kill_at}').touch()
        (fake / 'continue_after_normalization').touch()

    _write_executable(
        fake_bin / 'git',
        r"""#!/usr/bin/env bash
set -eu
case "$1" in
  status|fetch) exit 0 ;;
  cat-file)
    if [[ "$3" == "$PRIOR_TARGET_SHA"* ]] && [ -e "$FAKE_STATE/fail_cat_file" ]; then exit 61; fi
    exit 0
    ;;
  merge-base)
    if [ "$3" = "$ROLLBACK_SHA" ] && [ "$4" = "$PRIOR_TARGET_SHA" ] && [ -e "$FAKE_STATE/fail_expected_ancestry" ]; then exit 62; fi
    if [ "$3" = "$PRIOR_TARGET_SHA" ] && [ "$4" = "$TARGET_SHA" ] && [ -e "$FAKE_STATE/fail_target_ancestry" ]; then exit 63; fi
    exit 0
    ;;
  rev-parse)
    if [ "$2" = 'HEAD' ]; then cat "$FAKE_STATE/source"; else printf '%s\n' "$TARGET_SHA"; fi
    ;;
  archive) /usr/bin/tar -cf - -T /dev/null ;;
  checkout)
    if [ -e "$FAKE_STATE/hard_kill_checkout" ]; then kill -9 "$PPID"; exit 137; fi
    printf '%s\n' "$3" > "$FAKE_STATE/source"
    ;;
  diff)
    case " $* " in
      *' migrations '*) exit 1 ;;
      *' Dockerfile '*) exit 0 ;;
      *) printf 'unexpected fake git diff: %s\n' "$*" >&2; exit 94 ;;
    esac
    ;;
  *) printf 'unexpected fake git call: %s\n' "$*" >&2; exit 98 ;;
esac
""",
    )
    _write_fake_flock(fake_bin / 'flock')
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
set -eu
if [ "$1" = 'info' ]; then
  exit 0
elif [ "$1" = 'inspect' ] && [ "${@: -1}" = 'teplo_entitlement_shadow' ]; then
  exit 1
elif [ "$1" = 'inspect' ]; then
  case "$3" in
    *'.Image'*) printf '%s\n' "$ROLLBACK_IMAGE" ;;
    *'.Config.Image'*) printf 'teplo-bot:production\n' ;;
    *'.State.Health.Status'*) printf 'healthy\n' ;;
    *) printf 'unexpected fake inspect format: %s\n' "$3" >&2; exit 96 ;;
  esac
elif [ "$1" = 'compose' ]; then
  case " $* " in
    *' exec -T postgres '*) cat "$FAKE_STATE/schema" ;;
    *' config '*) printf 'SKIP_MIGRATION: false\nALLOW_MIGRATION_FAILURE: false\n' ;;
    *' build bot '*)
      if [ -e "$FAKE_STATE/hard_kill_build" ]; then kill -9 "$PPID"; exit 137; fi
      ;;
    *' run --rm --no-deps --entrypoint /app/.venv/bin/alembic bot heads '*) printf '0103 (head)\n' ;;
    *) printf 'unexpected fake compose call before normalization stop: %s\n' "$*" >&2; exit 97 ;;
  esac
elif [ "$1" = 'image' ] && [ "$2" = 'inspect' ]; then
  if [ "$3" = '--format' ]; then printf '%s\n' "$MIGRATION_IMAGE"; else exit 0; fi
elif [ "$1" = 'image' ] && [ "$2" = 'tag' ]; then
  printf 'tag\n' >> "$FAKE_STATE/mutations"
  if [ ! -e "$FAKE_STATE/continue_after_normalization" ]; then exit 66; fi
else
  printf 'unexpected fake docker call: %s\n' "$*" >&2
  exit 95
fi
""",
    )
    _write_executable(
        fake_bin / 'df',
        "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/fake 9999999 1 9999998 1%% /\\n'\n",
    )

    shell = tmp_path / 'deploy-migration.sh'
    _write_executable(shell, _extract_deploy_shell(repo, state))
    env = {
        'PATH': f'{fake_bin}:/usr/bin:/bin',
        'TARGET_SHA': TARGET_SHA,
        'ROLLBACK_SHA': ROLLBACK_SHA,
        'ROLLBACK_IMAGE': ROLLBACK_IMAGE,
        'MIGRATION_IMAGE': MIGRATION_IMAGE,
        'PRIOR_TARGET_SHA': PRIOR_TARGET_SHA,
        'FAKE_STATE': str(fake),
        'RELEASE_CARD_REFERENCE': 'recovery-prerequisite-test',
        'APPROVAL_ACTOR': 'test-reviewer',
        'OLD_IMAGE_TARGET_SCHEMA_COMPATIBILITY': 'OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE',
    }
    result = subprocess.run(  # noqa: S603 - executes extracted production workflow shell
        ['/bin/bash', str(shell)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, {'state': state, 'fake': fake}


def _ordinary_deploy_interlock_integration(
    tmp_path: Path,
    *,
    current_source: str,
    deploy_state: str,
    recovery_journal: str | None,
    recovery_audit: str | None = None,
    shadow_lock_barrier: bool = False,
    control_plane_changed: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    repo = tmp_path / 'repo'
    state = tmp_path / 'state'
    fake_bin = tmp_path / 'bin'
    fake = tmp_path / 'fake'
    for directory in (repo, state, fake_bin, fake):
        directory.mkdir()
    (state / 'bot-production.state').write_text(deploy_state)
    if recovery_journal is not None:
        (state / 'bot-production.migration-recovery.state').write_text(recovery_journal)
    if recovery_audit is not None:
        (state / f'bot-production.migration-recovery.{TARGET_SHA}.audit').write_text(recovery_audit)
    (fake / 'source').write_text(current_source)

    _write_executable(
        fake_bin / 'git',
        r"""#!/usr/bin/env bash
set -eu
case "$1" in
  status|fetch|cat-file) exit 0 ;;
  rev-parse)
    if [ "$2" = 'HEAD' ]; then cat "$FAKE_STATE/source"; else printf '%s\n' "$ORDINARY_TARGET_SHA"; fi
    ;;
  merge-base) exit 0 ;;
  diff)
    case " $* " in
      *' -- .github '*) [ "${CONTROL_PLANE_CHANGED:-0}" != 1 ] ;;
      *) exit 0 ;;
    esac
    ;;
  *) printf 'unexpected fake ordinary git call: %s\n' "$*" >&2; exit 98 ;;
esac
""",
    )
    _write_fake_flock(fake_bin / 'flock', barrier=shadow_lock_barrier)
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
set -eu
if [ "$1" = 'info' ]; then
  exit 0
elif [ "$1" = 'inspect' ] && [ "${@: -1}" = 'teplo_entitlement_shadow' ]; then
  exit 1
elif [ "$1" = 'inspect' ]; then
  case "$3" in
    *'.Image'*) printf '%s\n' "$ROLLBACK_IMAGE" ;;
    *'.State.Health.Status'*) printf 'healthy\n' ;;
    *) printf 'unexpected fake ordinary inspect: %s\n' "$3" >&2; exit 96 ;;
  esac
elif [ "$1" = 'compose' ]; then
  printf '%s\n' "$PREVIOUS_SCHEMA"
elif [ "$1" = 'image' ] && [ "$2" = 'tag' ]; then
  printf 'tag\n' >> "$FAKE_STATE/mutations"
  exit 66
else
  printf 'unexpected fake ordinary docker call: %s\n' "$*" >&2
  exit 95
fi
""",
    )
    _write_executable(
        fake_bin / 'df',
        "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/fake 9999999 1 9999998 1%% /\\n'\n",
    )
    shell = tmp_path / 'ordinary-deploy.sh'
    _write_executable(shell, _extract_ordinary_deploy_shell(repo, state))
    env = {
        'PATH': f'{fake_bin}:/usr/bin:/bin',
        'FAKE_STATE': str(fake),
        'ORDINARY_TARGET_SHA': PRIOR_TARGET_SHA,
        'ROLLBACK_IMAGE': ROLLBACK_IMAGE,
        'PREVIOUS_SCHEMA': PREVIOUS_SCHEMA,
        'CONTROL_PLANE_CHANGED': '1' if control_plane_changed else '0',
    }
    release_thread: threading.Thread | None = None
    if shadow_lock_barrier:

        def release_shadow_lock() -> None:
            deadline = time.monotonic() + 5
            while not (fake / 'flock_entered').exists():
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.02)
            time.sleep(0.2)
            (fake / 'flock_release').touch()

        release_thread = threading.Thread(target=release_shadow_lock, daemon=True)
        release_thread.start()

    result = subprocess.run(  # noqa: S603 - executes extracted ordinary deployment shell
        ['/bin/bash', str(shell)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if release_thread is not None:
        release_thread.join(timeout=5)
    return result, {'state': state, 'fake': fake}


def _infrastructure_control_plane_integration(
    tmp_path: Path,
    *,
    unexpected_github_path: bool = False,
    migration_risk_changed: bool = False,
    unexpected_business_path: bool = False,
    source_already_at_target: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    repo = tmp_path / 'repo'
    state = tmp_path / 'state'
    fake_bin = tmp_path / 'bin'
    fake = tmp_path / 'fake'
    for directory in (repo, state, fake_bin, fake):
        directory.mkdir()
    (state / 'bot-production.state').write_text(f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n')
    (fake / 'source').write_text(PRIOR_TARGET_SHA if source_already_at_target else ROLLBACK_SHA)
    (fake / 'container').write_text('f' * 64)
    (fake / 'started').write_text('2026-08-13T00:00:00Z')

    _write_executable(
        fake_bin / 'git',
        r"""#!/usr/bin/env bash
set -eu
case "$1" in
  status|fetch) exit 0 ;;
  rev-parse)
    if [ "$2" = HEAD ]; then cat "$FAKE_STATE/source"; else printf '%s\n' "$INFRA_TARGET_SHA"; fi
    ;;
  merge-base) exit 0 ;;
  diff)
    if [ "$2" = --quiet ] || [ "$4" = --quiet ]; then
      case " $* " in
        *' Dockerfile .dockerignore docker-compose.yml '*) exit 0 ;;
        *' migrations alembic.ini app/database main.py app/config.py '*) [ "$MIGRATION_RISK_CHANGED" != 1 ]; exit ;;
        *'.github/scripts/control-entitlement-shadow.sh'*) exit 1 ;;
        *) exit 0 ;;
      esac
    fi
    if [ "$2" = --name-only ] || [ "$4" = --name-only ]; then
      if [ "$UNEXPECTED_BUSINESS_PATH" = 1 ]; then
        printf '.github/scripts/control-entitlement-shadow.sh\n'
        printf 'app/services/payment_service.py\n'
        printf 'pyproject.toml\n'
      elif [ "$UNEXPECTED_GITHUB_PATH" = 1 ]; then
        printf '.github/workflows/unreviewed.yml\n'
      else
        printf '.github/scripts/control-entitlement-shadow.sh\n'
        printf '.github/workflows/deploy-infrastructure.yml\n'
      fi
      exit 0
    fi
    exit 98
    ;;
  checkout) printf '%s\n' "$3" > "$FAKE_STATE/source" ;;
  archive) /usr/bin/tar -cf - -T /dev/null ;;
  *) printf 'unexpected fake infrastructure git call: %s\n' "$*" >&2; exit 98 ;;
esac
""",
    )
    _write_fake_flock(fake_bin / 'flock')
    _write_executable(
        fake_bin / 'docker',
        r"""#!/usr/bin/env bash
set -eu
if [ "$1" = info ]; then
  exit 0
elif [ "$1" = inspect ] && [ "${@: -1}" = teplo_entitlement_shadow ]; then
  exit 1
elif [ "$1" = inspect ]; then
  case "$3" in
    *'.Id'*) cat "$FAKE_STATE/container" ;;
    *'.Image'*) cat "$FAKE_STATE/image" ;;
    *'.Config.Image'*) printf 'teplo-bot:production\n' ;;
    *'.State.StartedAt'*) cat "$FAKE_STATE/started" ;;
    *'.State.Health.Status'*) printf 'healthy\n' ;;
    *) exit 96 ;;
  esac
elif [ "$1" = compose ]; then
  case " $* " in
    *' build bot '*) printf 'build\n' >> "$FAKE_STATE/mutations" ;;
    *' up -d --wait '* ) printf 'up\n' >> "$FAKE_STATE/mutations"; printf '%s\n' "$MIGRATION_IMAGE" > "$FAKE_STATE/image" ;;
    *' ps bot '*) printf 'bot healthy\n' ;;
    *) exit 97 ;;
  esac
else
  exit 95
fi
""",
    )
    _write_executable(
        fake_bin / 'df',
        "#!/usr/bin/env bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/fake 9999999 1 9999998 1%% /\\n'\n",
    )
    _write_executable(fake_bin / 'find', '#!/usr/bin/env bash\nexit 0\n')
    (fake / 'image').write_text(ROLLBACK_IMAGE)
    shell = tmp_path / 'deploy-infrastructure.sh'
    _write_executable(shell, _extract_infrastructure_deploy_shell(repo, state))
    env = {
        'PATH': f'{fake_bin}:/usr/bin:/bin',
        'FAKE_STATE': str(fake),
        'INFRA_TARGET_SHA': PRIOR_TARGET_SHA,
        'MIGRATION_IMAGE': MIGRATION_IMAGE,
        'UNEXPECTED_GITHUB_PATH': '1' if unexpected_github_path else '0',
        'UNEXPECTED_BUSINESS_PATH': '1' if unexpected_business_path else '0',
        'MIGRATION_RISK_CHANGED': '1' if migration_risk_changed else '0',
    }
    result = subprocess.run(  # noqa: S603 - extracted exact production workflow shell
        ['/bin/bash', str(shell)],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, {'state': state, 'fake': fake}


@pytest.mark.parametrize(
    ('scenario', 'decision'),
    [
        # Migration preparation wrote the journal and checked out the target,
        # but the old image is still running and schema is untouched.
        (Scenario(), 'continue_recovery'),
        # Candidate startup committed 0103 and failed before writing either
        # production state or recovery phase=completed.
        (
            Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE),
            'start_recovery',
        ),
        # Candidate passed health and wrote production state, then crashed
        # before the journal could move from prepared to completed.
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_image=MIGRATION_IMAGE,
                deploy_state='target',
            ),
            'start_recovery',
        ),
        # Fully recorded migration deployment still supports an explicitly
        # approved immediate recovery.
        (
            Scenario(
                phase='completed',
                actual_schema=TARGET_SCHEMA,
                current_image=MIGRATION_IMAGE,
                deploy_state='target',
            ),
            'start_recovery',
        ),
        # Interruption during Compose switch: old image is already selected,
        # but source/state finalization has not happened yet.
        (
            Scenario(actual_schema=TARGET_SCHEMA, current_image=ROLLBACK_IMAGE),
            'continue_recovery',
        ),
        # Interruption after source checkout and before production state write.
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_source=ROLLBACK_SHA,
                current_image=ROLLBACK_IMAGE,
            ),
            'continue_recovery',
        ),
        # Interruption after atomic production state write; both the first
        # retry and every later retry are a verified no-op/final-audit path.
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_source=ROLLBACK_SHA,
                current_image=ROLLBACK_IMAGE,
                deploy_state='recovery',
            ),
            'already_recovered',
        ),
        (
            Scenario(
                phase='recovered',
                actual_schema=TARGET_SCHEMA,
                current_source=ROLLBACK_SHA,
                current_image=ROLLBACK_IMAGE,
                deploy_state='recovery',
            ),
            'already_recovered',
        ),
    ],
)
def test_real_classifier_covers_every_recovery_failure_window(
    tmp_path: Path,
    scenario: Scenario,
    decision: str,
) -> None:
    result = classify(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == decision


def test_real_classifier_is_idempotent_across_one_interrupted_recovery(tmp_path: Path) -> None:
    checkpoints = [
        # prepared journal, before migration
        (Scenario(), 'continue_recovery'),
        # 0103 committed, before phase=completed
        (Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE), 'start_recovery'),
        # deploy state committed, before phase=completed
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_image=MIGRATION_IMAGE,
                deploy_state='target',
            ),
            'start_recovery',
        ),
        # retry while Compose is switching or checking the old image
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_image=ROLLBACK_IMAGE,
                deploy_state='target',
            ),
            'continue_recovery',
        ),
        # retry after source checkout, before the atomic recovery-state write
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_source=ROLLBACK_SHA,
                current_image=ROLLBACK_IMAGE,
                deploy_state='target',
            ),
            'continue_recovery',
        ),
        # retry after the atomic state write, before or after audit creation
        (
            Scenario(
                actual_schema=TARGET_SCHEMA,
                current_source=ROLLBACK_SHA,
                current_image=ROLLBACK_IMAGE,
                deploy_state='recovery',
            ),
            'already_recovered',
        ),
    ]

    for index, (scenario, expected) in enumerate(checkpoints):
        checkpoint = tmp_path / str(index)
        checkpoint.mkdir()
        first = classify(checkpoint, scenario)
        second = classify(checkpoint, scenario)
        assert (first.returncode, first.stdout.strip()) == (0, expected)
        assert (second.returncode, second.stdout.strip()) == (0, expected)


@pytest.mark.parametrize(
    'scenario',
    [
        Scenario(),
        Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE),
        Scenario(
            actual_schema=TARGET_SCHEMA,
            current_image=MIGRATION_IMAGE,
            deploy_state='target',
        ),
        Scenario(
            phase='completed',
            actual_schema=TARGET_SCHEMA,
            current_image=MIGRATION_IMAGE,
            deploy_state='target',
        ),
    ],
)
def test_extracted_production_recovery_shell_completes_real_failure_windows(
    tmp_path: Path,
    scenario: Scenario,
) -> None:
    result, paths = _recovery_integration(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    assert paths['fake'].joinpath('source').read_text().strip() == ROLLBACK_SHA
    assert paths['fake'].joinpath('image').read_text().strip() == ROLLBACK_IMAGE
    deployed = paths['state'].joinpath('bot-production.state').read_text()
    assert f'sha={ROLLBACK_SHA}\n' in deployed
    assert f'image={ROLLBACK_IMAGE}\n' in deployed
    assert 'mode=recovery\n' in deployed
    assert paths['state'].joinpath(f'bot-production.migration-recovery.{TARGET_SHA}.audit').is_file()
    assert 'phase=recovered\n' in paths['state'].joinpath('bot-production.migration-recovery.state').read_text()

    repeated = _rerun_recovery_integration(paths)
    assert repeated.returncode == 0, repeated.stderr
    assert 'Recovery already complete' in repeated.stdout


def test_extracted_recovery_shell_restores_candidate_when_compose_switch_fails(
    tmp_path: Path,
) -> None:
    scenario = Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE)
    result, paths = _recovery_integration(tmp_path, scenario, fail_up_once=True)
    assert result.returncode == 56
    assert paths['fake'].joinpath('source').read_text().strip() == TARGET_SHA
    assert paths['fake'].joinpath('image').read_text().strip() == MIGRATION_IMAGE
    assert _deploy_state(scenario) == paths['state'].joinpath('bot-production.state').read_text()

    repeated = _rerun_recovery_integration(paths)
    assert repeated.returncode == 0, repeated.stderr
    assert paths['fake'].joinpath('image').read_text().strip() == ROLLBACK_IMAGE


def test_extracted_recovery_shell_retries_after_old_image_health_before_source_state(
    tmp_path: Path,
) -> None:
    scenario = Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE)
    result, paths = _recovery_integration(tmp_path, scenario, fail_checkout_once=True)
    assert result.returncode == 57
    assert paths['fake'].joinpath('source').read_text().strip() == TARGET_SHA
    assert paths['fake'].joinpath('image').read_text().strip() == ROLLBACK_IMAGE
    assert 'safe partial state' in result.stderr

    repeated = _rerun_recovery_integration(paths)
    assert repeated.returncode == 0, repeated.stderr
    assert paths['fake'].joinpath('source').read_text().strip() == ROLLBACK_SHA


def test_extracted_recovery_shell_retries_after_state_write_before_audit(
    tmp_path: Path,
) -> None:
    scenario = Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE)
    result, paths = _recovery_integration(tmp_path, scenario, fail_audit_once=True)
    assert result.returncode == 58
    assert paths['fake'].joinpath('source').read_text().strip() == ROLLBACK_SHA
    assert 'mode=recovery\n' in paths['state'].joinpath('bot-production.state').read_text()
    audit = paths['state'] / f'bot-production.migration-recovery.{TARGET_SHA}.audit'
    assert not audit.exists()

    repeated = _rerun_recovery_integration(paths)
    assert repeated.returncode == 0, repeated.stderr
    assert audit.is_file()
    assert 'phase=recovered\n' in paths['state'].joinpath('bot-production.migration-recovery.state').read_text()


def test_extracted_recovery_shell_retries_after_audit_before_recovered_journal(
    tmp_path: Path,
) -> None:
    scenario = Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE)
    result, paths = _recovery_integration(tmp_path, scenario, fail_journal_mark_once=True)
    assert result.returncode == 59
    assert 'mode=recovery\n' in paths['state'].joinpath('bot-production.state').read_text()
    audit = paths['state'] / f'bot-production.migration-recovery.{TARGET_SHA}.audit'
    assert audit.is_file()
    journal = paths['state'] / 'bot-production.migration-recovery.state'
    assert 'phase=prepared\n' in journal.read_text()

    repeated = _rerun_recovery_integration(paths)
    assert repeated.returncode == 0, repeated.stderr
    assert 'phase=recovered\n' in journal.read_text()


def test_extracted_recovery_shell_stops_recovered_journal_without_keyed_audit(
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        phase='recovered',
        actual_schema=TARGET_SCHEMA,
        current_source=ROLLBACK_SHA,
        current_image=ROLLBACK_IMAGE,
        deploy_state='recovery',
    )
    result, paths = _recovery_integration(tmp_path, scenario)
    assert result.returncode != 0
    assert not paths['state'].joinpath(f'bot-production.migration-recovery.{TARGET_SHA}.audit').exists()
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_recovery_shell_never_mutates_on_unapproved_target_schema(
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        actual_schema=TARGET_SCHEMA,
        compatible=False,
        current_image=MIGRATION_IMAGE,
    )
    result, paths = _recovery_integration(tmp_path, scenario)
    assert result.returncode == 64
    assert 'old_image_not_approved_for_target_schema' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()
    assert paths['fake'].joinpath('image').read_text().strip() == MIGRATION_IMAGE


def _prior_recovery_deploy_state(*, schema: str = PREVIOUS_SCHEMA, extra: str = '') -> str:
    return (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={schema}\n'
        f'recovery_from_sha={PRIOR_TARGET_SHA}\n'
        f'{extra}'
    )


def test_extracted_deploy_shell_validates_then_normalizes_prior_recovery_state(
    tmp_path: Path,
) -> None:
    result, paths = _deploy_normalization_integration(tmp_path, _prior_recovery_deploy_state())
    # The fake deliberately stops at the first Docker image-tag operation,
    # immediately after the exact production shell normalized durable state.
    assert result.returncode == 66, result.stderr
    assert paths['state'].joinpath('bot-production.state').read_text() == (
        f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    )
    assert paths['fake'].joinpath('mutations').read_text() == 'tag\n'


@pytest.mark.parametrize(
    'deploy_state',
    [
        _prior_recovery_deploy_state(schema='9999'),
        _prior_recovery_deploy_state(extra='unknown=value\n'),
        _prior_recovery_deploy_state(extra='mode=recovery\n'),
        _prior_recovery_deploy_state().replace(
            f'recovery_from_sha={PRIOR_TARGET_SHA}',
            'recovery_from_sha=not-a-sha',
        ),
    ],
)
def test_extracted_deploy_shell_stops_before_overwriting_bad_recovery_metadata(
    tmp_path: Path,
    deploy_state: str,
) -> None:
    result, paths = _deploy_normalization_integration(tmp_path, deploy_state)
    assert result.returncode != 0
    assert paths['state'].joinpath('bot-production.state').read_text() == deploy_state
    assert not paths['fake'].joinpath('mutations').exists()


@pytest.mark.parametrize(
    'failed_guard',
    ['fail_cat_file', 'fail_expected_ancestry', 'fail_target_ancestry'],
)
def test_extracted_deploy_shell_stops_on_unproven_recovery_ancestry(
    tmp_path: Path,
    failed_guard: str,
) -> None:
    deploy_state = _prior_recovery_deploy_state()
    result, paths = _deploy_normalization_integration(
        tmp_path,
        deploy_state,
        fail_git_guard=failed_guard,
    )
    assert result.returncode != 0
    assert paths['state'].joinpath('bot-production.state').read_text() == deploy_state
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_deploy_shell_hard_kill_before_journal_keeps_previous_baseline(
    tmp_path: Path,
) -> None:
    previous_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _deploy_normalization_integration(
        tmp_path,
        previous_state,
        hard_kill_at='build',
    )
    assert result.returncode < 0
    assert paths['fake'].joinpath('source').read_text().strip() == ROLLBACK_SHA
    assert paths['state'].joinpath('bot-production.state').read_text() == previous_state
    assert not paths['state'].joinpath('bot-production.migration-recovery.state').exists()


def test_extracted_deploy_shell_hard_kill_at_checkout_has_prepared_journal(
    tmp_path: Path,
) -> None:
    previous_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _deploy_normalization_integration(
        tmp_path,
        previous_state,
        hard_kill_at='checkout',
    )
    assert result.returncode < 0
    assert paths['fake'].joinpath('source').read_text().strip() == ROLLBACK_SHA
    assert paths['state'].joinpath('bot-production.state').read_text() == previous_state
    journal = paths['state'].joinpath('bot-production.migration-recovery.state').read_text()
    assert 'format_version=2\n' in journal
    assert 'phase=prepared\n' in journal
    assert f'deployed_sha={TARGET_SHA}\n' in journal
    assert f'rollback_source_sha={ROLLBACK_SHA}\n' in journal


def test_extracted_ordinary_deploy_stops_on_unresolved_prepared_migration(
    tmp_path: Path,
) -> None:
    previous_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=TARGET_SHA,
        deploy_state=previous_state,
        recovery_journal=_completed_v1_recovery_journal(phase='prepared'),
    )
    assert result.returncode != 0
    assert paths['state'].joinpath('bot-production.state').read_text() == previous_state
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_on_source_state_mismatch_without_journal(
    tmp_path: Path,
) -> None:
    previous_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=TARGET_SHA,
        deploy_state=previous_state,
        recovery_journal=None,
    )
    assert result.returncode != 0
    assert paths['state'].joinpath('bot-production.state').read_text() == previous_state
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_reaches_first_mutation_from_consistent_state(
    tmp_path: Path,
) -> None:
    normal_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=normal_state,
        recovery_journal=_completed_v1_recovery_journal(),
    )
    assert result.returncode == 66, result.stderr
    assert paths['fake'].joinpath('mutations').read_text() == 'tag\n'


def test_ordinary_deploy_waits_for_shadow_control_lock_before_first_mutation(
    tmp_path: Path,
) -> None:
    normal_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=normal_state,
        recovery_journal=_completed_v1_recovery_journal(),
        shadow_lock_barrier=True,
    )

    assert result.returncode == 66, result.stderr
    assert paths['fake'].joinpath('flock_entered').exists()
    assert not paths['fake'].joinpath('mutation_before_flock_release').exists()
    assert paths['fake'].joinpath('mutations').read_text() == 'tag\n'


def test_ordinary_deploy_stops_control_plane_change_before_first_mutation(
    tmp_path: Path,
) -> None:
    normal_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=normal_state,
        recovery_journal=_completed_v1_recovery_journal(),
        control_plane_changed=True,
    )

    assert result.returncode == 16
    assert 'production control-plane change is present' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_protected_infrastructure_deploy_accepts_allowlisted_control_plane_only(
    tmp_path: Path,
) -> None:
    result, paths = _infrastructure_control_plane_integration(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not paths['fake'].joinpath('mutations').exists()
    assert paths['fake'].joinpath('container').read_text() == 'f' * 64
    assert paths['fake'].joinpath('image').read_text() == ROLLBACK_IMAGE
    assert paths['fake'].joinpath('started').read_text() == '2026-08-13T00:00:00Z'
    assert f'sha={PRIOR_TARGET_SHA}\n' in paths['state'].joinpath('bot-production.state').read_text()
    assert f'image={ROLLBACK_IMAGE}\n' in paths['state'].joinpath('bot-production.state').read_text()


def test_protected_control_plane_release_recovers_kill_after_source_checkout(
    tmp_path: Path,
) -> None:
    result, paths = _infrastructure_control_plane_integration(
        tmp_path,
        source_already_at_target=True,
    )

    assert result.returncode == 0, result.stderr
    assert 'Recovering an interrupted source-only transition' in result.stdout
    assert not paths['fake'].joinpath('mutations').exists()
    assert paths['fake'].joinpath('container').read_text() == 'f' * 64
    assert paths['fake'].joinpath('image').read_text() == ROLLBACK_IMAGE
    assert paths['fake'].joinpath('started').read_text() == '2026-08-13T00:00:00Z'
    assert f'sha={PRIOR_TARGET_SHA}\n' in paths['state'].joinpath('bot-production.state').read_text()
    assert f'image={ROLLBACK_IMAGE}\n' in paths['state'].joinpath('bot-production.state').read_text()


def test_protected_infrastructure_deploy_rejects_unreviewed_github_path(
    tmp_path: Path,
) -> None:
    result, paths = _infrastructure_control_plane_integration(tmp_path, unexpected_github_path=True)

    assert result.returncode == 37
    assert 'paths outside the reviewed allowlist' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_protected_control_plane_route_rejects_mixed_business_or_dependency_paths(
    tmp_path: Path,
) -> None:
    result, paths = _infrastructure_control_plane_integration(tmp_path, unexpected_business_path=True)

    assert result.returncode == 37
    assert 'control-plane-only mode contains paths outside the reviewed allowlist' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_protected_infrastructure_deploy_rejects_mixed_migration_risk(
    tmp_path: Path,
) -> None:
    result, paths = _infrastructure_control_plane_integration(tmp_path, migration_risk_changed=True)

    assert result.returncode == 36
    assert 'database or migration-risk changes require the migration workflow' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_accepts_verified_recovery_state(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_recovery_state(Scenario(phase='recovered')),
        recovery_audit=_recovery_audit(),
    )
    assert result.returncode == 66, result.stderr
    assert paths['fake'].joinpath('mutations').read_text() == 'tag\n'


def test_extracted_ordinary_deploy_stops_recovered_state_without_exact_audit(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_recovery_state(Scenario(phase='recovered')),
        recovery_audit=None,
    )
    assert result.returncode != 0
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_recovered_state_on_audit_mismatch(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_recovery_state(Scenario(phase='recovered')),
        recovery_audit=_recovery_audit(schema=TARGET_SCHEMA),
    )
    assert result.returncode != 0
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_recovery_mode_without_journal(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=None,
    )
    assert result.returncode != 0
    assert 'has no migration journal' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_prepared_even_with_recovery_state_and_audit(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_recovery_state(Scenario(phase='prepared')),
        recovery_audit=_recovery_audit(),
    )
    assert result.returncode != 0
    assert 'prepared migration journal' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_same_schema_incompatible_recovered_state(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    journal = _recovery_state(Scenario(phase='recovered', compatible=False)).replace(
        'target_schema_revisions=0103', 'target_schema_revisions=0102'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=journal,
        recovery_audit=_recovery_audit(),
    )
    assert result.returncode != 0
    assert 'lacks target-schema compatibility approval' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_normal_state_with_recovered_journal(
    tmp_path: Path,
) -> None:
    post_crossing_state = f'sha={PRIOR_TARGET_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=PRIOR_TARGET_SHA,
        deploy_state=post_crossing_state,
        recovery_journal=_recovery_state(Scenario(phase='recovered')),
        recovery_audit=_recovery_audit(),
    )
    assert result.returncode != 0
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_on_malformed_completed_journal(
    tmp_path: Path,
) -> None:
    normal_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=normal_state,
        recovery_journal=_completed_v1_recovery_journal() + 'unexpected=value\n',
    )
    assert result.returncode != 0
    assert 'malformed migration journal' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_on_semantically_contradictory_completed_journal(
    tmp_path: Path,
) -> None:
    normal_state = f'sha={ROLLBACK_SHA}\nimage={ROLLBACK_IMAGE}\n'
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=normal_state,
        recovery_journal=_recovery_state(Scenario(phase='completed')),
    )
    assert result.returncode != 0
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_completed_journal_on_previous_recovered_schema(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={PREVIOUS_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_recovery_state(Scenario(phase='completed')),
        recovery_audit=_recovery_audit(),
    )
    assert result.returncode != 0
    assert not paths['fake'].joinpath('mutations').exists()


def test_extracted_ordinary_deploy_stops_on_recovery_state_schema_mismatch(
    tmp_path: Path,
) -> None:
    recovery_state = (
        f'sha={ROLLBACK_SHA}\n'
        f'image={ROLLBACK_IMAGE}\n'
        'mode=recovery\n'
        f'schema_revisions={TARGET_SCHEMA}\n'
        f'recovery_from_sha={TARGET_SHA}\n'
    )
    result, paths = _ordinary_deploy_interlock_integration(
        tmp_path,
        current_source=ROLLBACK_SHA,
        deploy_state=recovery_state,
        recovery_journal=_completed_v1_recovery_journal(),
    )
    assert result.returncode != 0
    assert 'disagrees with live schema' in result.stderr
    assert not paths['fake'].joinpath('mutations').exists()


@pytest.mark.parametrize('phase', ['prepared', 'completed'])
def test_real_classifier_accepts_same_schema_migration_risk_release(
    tmp_path: Path,
    phase: str,
) -> None:
    scenario = Scenario(
        phase=phase,
        actual_schema=PREVIOUS_SCHEMA,
        current_image=MIGRATION_IMAGE,
        deploy_state='target',
    )
    recovery = tmp_path / 'migration-recovery.state'
    deployed = tmp_path / 'production.state'
    recovery.write_text(
        _recovery_state(scenario).replace('target_schema_revisions=0103', 'target_schema_revisions=0102')
    )
    deployed.write_text(_deploy_state(scenario))
    result = subprocess.run(  # noqa: S603 - executes the fixed repository script under test
        [
            str(CLASSIFIER),
            str(recovery),
            str(deployed),
            TARGET_SHA,
            ROLLBACK_SHA,
            TARGET_SHA,
            TARGET_SHA,
            MIGRATION_IMAGE,
            PREVIOUS_SCHEMA,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'start_recovery'


def test_real_classifier_stops_same_schema_when_old_image_is_explicitly_incompatible(
    tmp_path: Path,
) -> None:
    scenario = Scenario(
        phase='completed',
        actual_schema=PREVIOUS_SCHEMA,
        compatible=False,
        current_image=MIGRATION_IMAGE,
        deploy_state='target',
    )
    recovery = tmp_path / 'migration-recovery.state'
    deployed = tmp_path / 'production.state'
    recovery.write_text(
        _recovery_state(scenario).replace('target_schema_revisions=0103', 'target_schema_revisions=0102')
    )
    deployed.write_text(_deploy_state(scenario))
    result = subprocess.run(  # noqa: S603 - executes the fixed repository script under test
        [
            str(CLASSIFIER),
            str(recovery),
            str(deployed),
            TARGET_SHA,
            ROLLBACK_SHA,
            TARGET_SHA,
            TARGET_SHA,
            MIGRATION_IMAGE,
            PREVIOUS_SCHEMA,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert 'old_image_not_approved_for_target_schema' in result.stderr
    assert not result.stdout


def test_real_classifier_accepts_retry_after_prior_recovery_is_normalized(tmp_path: Path) -> None:
    # deploy-migration atomically replaces the prior mode=recovery marker with
    # this exact ordinary baseline before it writes the new prepared journal.
    scenario = Scenario(
        actual_schema=TARGET_SCHEMA,
        current_image=MIGRATION_IMAGE,
        deploy_state='previous',
    )
    result = classify(tmp_path, scenario)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'start_recovery'


@pytest.mark.parametrize(
    'scenario',
    [
        # Target schema is never accepted without explicit old-image proof.
        Scenario(actual_schema=TARGET_SCHEMA, compatible=False, current_image=MIGRATION_IMAGE),
        # Neither guessed nor partially applied revisions are allowed.
        Scenario(actual_schema='0102,0103', current_image=MIGRATION_IMAGE),
        # A completed journal cannot truthfully coexist with the old schema.
        Scenario(phase='completed'),
        # Source/image ordering that the recovery workflow never produces is
        # treated as corruption, not as a prompt to retag or restart.
        Scenario(current_source=ROLLBACK_SHA, current_image=MIGRATION_IMAGE),
        # A target deploy-state record cannot exist on the previous schema.
        Scenario(deploy_state='target'),
        # An unnormalized durable recovered marker cannot coexist with the new
        # migration image. deploy-migration must rewrite it to ordinary
        # previous state before recording the new journal.
        Scenario(
            actual_schema=TARGET_SCHEMA,
            current_image=MIGRATION_IMAGE,
            deploy_state='recovery',
        ),
    ],
)
def test_real_classifier_stops_on_incompatible_or_contradictory_state(
    tmp_path: Path,
    scenario: Scenario,
) -> None:
    result = classify(tmp_path, scenario)
    assert result.returncode == 64
    assert result.stderr.startswith('STOP:')
    assert not result.stdout


def test_real_classifier_rejects_legacy_journal_without_compatibility_proof(tmp_path: Path) -> None:
    scenario = Scenario(actual_schema=TARGET_SCHEMA, current_image=MIGRATION_IMAGE)
    recovery = tmp_path / 'migration-recovery.state'
    deployed = tmp_path / 'production.state'
    recovery.write_text(_recovery_state(scenario).replace('format_version=2\n', 'format_version=1\n'))
    deployed.write_text(_deploy_state(scenario))
    result = subprocess.run(  # noqa: S603 - executes the fixed repository script under test
        [
            str(CLASSIFIER),
            str(recovery),
            str(deployed),
            TARGET_SHA,
            ROLLBACK_SHA,
            TARGET_SHA,
            scenario.current_source,
            scenario.current_image,
            scenario.actual_schema,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 64
    assert 'unsupported_recovery_state_version' in result.stderr


def test_workflows_persist_and_consume_the_exact_compatibility_decision() -> None:
    deploy = DEPLOY_WORKFLOW.read_text()
    recovery = RECOVERY_WORKFLOW.read_text()
    assert 'old_image_target_schema_compatibility:' in deploy
    assert 'OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE|OLD_IMAGE_TARGET_SCHEMA_INCOMPATIBLE' in deploy
    assert 'format_version=2' in deploy
    assert 'old_image_target_schema_compatible=%s' in deploy
    assert 'write_previous_deployed_state' in deploy
    assert deploy.index('write_previous_deployed_state\n') < deploy.index('write_migration_recovery_state prepared')
    assert deploy.index('write_migration_recovery_state prepared') < deploy.index('git checkout --detach "$TARGET_SHA"')
    assert '.github/scripts/classify-migration-recovery.sh' in recovery
    assert 'RECOVERY_OLD_IMAGE_VERIFIED=1' in recovery
    assert 'already_recovered' in recovery
    assert 'continue_recovery' in recovery
    assert 'bot-production.migration-recovery.${TARGET_SHA}.audit' in recovery
    assert recovery.index('.github/scripts/classify-migration-recovery.sh') < recovery.index(
        'docker image tag "$RECOVERY_IMAGE_TAG"'
    )
    assert 'if [ "$CURRENT_IMAGE_ID" != "$RECOVERY_IMAGE_ID" ]' in recovery
    assert "grep -F 'SKIP_MIGRATION=true'" in recovery

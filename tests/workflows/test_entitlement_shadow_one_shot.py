from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CONTROL_WORKFLOW = ROOT / '.github/workflows/entitlement-shadow-one-shot.yml'
CI_WORKFLOW = ROOT / '.github/workflows/entitlement-shadow-one-shot-ci.yml'
CONTROLLER = ROOT / '.github/scripts/entitlement-shadow-one-shot-control.sh'
ENTRYPOINT = ROOT / '.github/scripts/entitlement_shadow_one_shot.py'
E2E = ROOT / '.github/scripts/test-entitlement-shadow-one-shot-e2e.sh'
DEPLOY = ROOT / '.github/workflows/deploy.yml'

COMPATIBLE_SHA = '103094b96f96a412463753e56e3d996311b182ec'
COMPATIBLE_IMAGE = 'sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e'
AMD64_MANIFEST = 'sha256:39545077b550badb008c76b81312706f69085a0f79a79705b6bbe6ad3ad6c276'
AMD64_CONFIG = 'sha256:133309254d834f18ec0a50f9b57d7c37cdd73fda9b57bf7bdcb7ae8084f1fe67'
FIXED_NAME = 'teplo-entitlement-shadow-one-shot'
FIXED_ROLE = 'entitlement-shadow-one-shot'


def _workflow(path: Path) -> dict[str, object]:
    return yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)  # noqa: S506


def _load_entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location('entitlement_shadow_one_shot_entrypoint', ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_control_workflow_has_only_two_allowlisted_actions_and_safe_inputs() -> None:
    document = _workflow(CONTROL_WORKFLOW)
    triggers = document['on']
    assert isinstance(triggers, dict) and set(triggers) == {'workflow_dispatch'}
    dispatch = triggers['workflow_dispatch']
    assert isinstance(dispatch, dict)
    inputs = dispatch['inputs']
    assert isinstance(inputs, dict)
    assert set(inputs) == {
        'action',
        'exact_deployed_sha',
        'owner_approval_phrase',
        'release_card_reference',
    }
    action = inputs['action']
    assert isinstance(action, dict)
    assert action['type'] == 'choice'
    assert action['options'] == ['ENABLE_SHADOW', 'DISABLE_SHADOW']

    rendered = CONTROL_WORKFLOW.read_text(encoding='utf-8')
    for forbidden in (
        'command_input',
        'remote_command',
        'image_input',
        'timeout_input',
        'url_input',
        'env_key',
        'env_value',
        'approval_actor',
    ):
        assert forbidden not in rendered.lower()
    assert 'request_actor' in rendered
    assert 'github.actor' in rendered


def test_control_workflow_is_main_exact_sha_protected_and_serialized() -> None:
    document = _workflow(CONTROL_WORKFLOW)
    concurrency = document['concurrency']
    assert concurrency == {
        'group': 'teplo-bot-production-deploy',
        'cancel-in-progress': 'false',
    }
    jobs = document['jobs']
    assert isinstance(jobs, dict) and jobs
    rendered = CONTROL_WORKFLOW.read_text(encoding='utf-8')
    assert 'teplo-vpn-production-controlled-change' in rendered
    assert 'refs/heads/main' in rendered
    assert 'origin/main' in rendered
    assert 'github.sha' in rendered
    assert COMPATIBLE_SHA in rendered
    assert COMPATIBLE_IMAGE in rendered
    assert re.search(r'sha256sum|shasum -a 256', rendered)
    assert 'github.run_id' in rendered and 'github.run_attempt' in rendered
    assert 'appleboy/scp-action@' in rendered
    assert 'appleboy/ssh-action@029f5b4aeeeb58fdfe1410a5d17f967dacf36262' in rendered


def test_entrypoint_uses_public_one_cycle_runtime_and_strict_evidence() -> None:
    source = ENTRYPOINT.read_text(encoding='utf-8')
    tree = ast.parse(source)
    imported_names = {
        alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }
    assert {
        'LegacyPostgresShadowSource',
        'ReadOnlyShadowRunner',
        'RemnaWaveShadowPanelProvider',
        'shadow_policy_from_settings',
    }.issubset(imported_names)
    assert 'EntitlementShadowService' not in imported_names
    assert '_runner' not in source
    assert 'create_task' not in source
    assert source.count('.run_once(') == 1
    assert 'SET TRANSACTION READ ONLY' not in source  # existing source owns this boundary
    assert 'EVENT_FIELDS' in source and 'validate_evidence_event' in source


def test_evidence_validator_accepts_only_bounded_aggregate_types() -> None:
    module = _load_entrypoint()
    valid = {
        'event': 'entitlement_shadow_one_shot_complete',
        'schema': 'entitlement_shadow_metrics_v1',
        'sampled': 1,
        'exact': 1,
        'drift': 0,
        'missing': 0,
        'panel_read_errors': 0,
        'contract_errors': 0,
        'owner_mismatches': 0,
        'comparator_instability': 0,
        'rate_limit_violations': 0,
        'critical_drift': 0,
        'mismatch_fields': {},
        'cohorts': {'limited': 1},
        'elapsed_ms': 7,
        'stopped': False,
        'stop_reason': 'none',
    }
    assert module.validate_evidence_event(valid) == valid
    for changed in (
        {**valid, 'telegram_id': 123},
        {**valid, 'sampled': True},
        {**valid, 'sampled': 101},
        {**valid, 'mismatch_fields': {'panel_uuid': 1}},
        {**valid, 'cohorts': {'unknown': 1}},
        {**valid, 'stop_reason': 'raw exception'},
    ):
        with pytest.raises((TypeError, ValueError)):
            module.validate_evidence_event(changed)


def test_controller_has_fixed_identity_hard_deadlines_and_security_options() -> None:
    source = CONTROLLER.read_text(encoding='utf-8')
    for required in (
        FIXED_NAME,
        f'teplo.role={FIXED_ROLE}',
        '--user',
        '1000:1000',
        '--read-only',
        '--tmpfs',
        '--cap-drop',
        '--security-opt',
        'no-new-privileges',
        '--memory',
        '--cpus',
        '--pids-limit',
        '--restart',
        'no',
        '--no-healthcheck',
        '--rm',
        '180',
        '195',
        '10',
        '210',
        'remnawave-network',
        'one-readonly-mount',
        'forbidden-env-absent',
        'docker start -a',
    ):
        assert required in source
    assert '/var/run/docker.sock' not in source
    assert 'docker compose down' not in source
    assert 'docker system prune' not in source
    assert re.search(r'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED[^\n]*true', source)
    assert re.search(r'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH[^\n]*false', source)
    assert re.search(r'MULTI_TARIFF_ENABLED[^\n]*false', source)


def test_future_enable_has_double_exact_production_preflight() -> None:
    source = CONTROLLER.read_text(encoding='utf-8')
    assert source.count('production_preflight') == 4  # definition + pre-create + pre-start + post-cycle
    for required in (
        'remnawave_bot_db',
        'dc35bf7aa92d570c5f190b3e7ccb8e2f22aa87b5d3d46f9277d63252fbd1057c',
        "runtime_flags\" = '0|0|0|0|1|0'",
        "schema_revision\" = '0103'",
        "authority_total\" = '0'",
        'production_bot_unchanged=true',
    ):
        assert required in source


def test_disable_is_independent_and_fail_closed() -> None:
    source = CONTROLLER.read_text(encoding='utf-8')
    disable_start = source.index('disable_shadow()')
    enable_start = source.index('enable_shadow()')
    disable = source[disable_start:enable_start]
    assert 'docker rm -f' in disable
    assert 'teplo.role' in disable
    assert 'remnawave_bot' not in disable
    for forbidden in ('psql', 'postgres', 'redis', 'curl', '.env', 'schema'):
        assert forbidden not in disable.lower()
    assert 'cleanup_result=absent_noop' in disable
    assert 'foreign' in disable.lower()


def test_controller_never_publishes_raw_container_output() -> None:
    source = CONTROLLER.read_text(encoding='utf-8')
    assert 'grep' not in source[source.index('validate_evidence') : source.index('disable_shadow()')]
    assert 'printf \'%s\\n\' "$raw_output"' not in source
    assert 'validate_evidence' in source
    assert 'observation_evidence=unproved' in source
    assert 'attached_rc' in source
    assert 'evidence_proven=false' in source


def test_candidate_ci_is_isolated_from_production_credentials_and_host() -> None:
    workflow = CI_WORKFLOW.read_text(encoding='utf-8')
    assert 'secrets.' not in workflow
    assert 'ssh-action' not in workflow
    assert 'scp-action' not in workflow
    assert 'docker build' not in workflow
    assert 'docker load' not in workflow
    assert 'gh release download' not in workflow
    assert 'teplo-gate2-private-ci' not in workflow
    assert 'private_exact_image_e2e=required' in workflow
    document = _workflow(CI_WORKFLOW)
    assert set(document['jobs']) == {'verify', 'candidate-boundary'}


def test_e2e_cleanup_is_bound_to_the_exact_run_label() -> None:
    controller = CONTROLLER.read_text(encoding='utf-8')
    script = E2E.read_text(encoding='utf-8')
    assert 'ONE_SHOT_E2E_RUN_KEY' in controller
    assert 'teplo.e2e-run' in controller
    assert 'teplo.e2e-run' in script
    cleanup = script[script.index('cleanup()') : script.index('trap cleanup')]
    assert 'docker rm -f "$FIXED_NAME"' in cleanup
    assert '!= "$RUN_KEY"' not in cleanup
    assert ' = "$RUN_KEY"' in cleanup


def test_mandatory_private_e2e_contract_is_exact_image_and_cannot_skip() -> None:
    script = E2E.read_text(encoding='utf-8')
    assert COMPATIBLE_SHA in script and AMD64_CONFIG in script
    assert 'ONE_SHOT_E2E_IMAGE_REFERENCE' in script
    assert 'docker network create --internal' in script
    assert 'skip' not in script.lower()
    for required in (
        'postgres:15-alpine',
        'LIMITED',
        'fake-panel',
        'POST',
        'PATCH',
        'PUT',
        'DELETE',
        'sampled',
        'controller-sigkill',
        'readonly-rootfs',
        'container-absent',
        'two-networks',
        'uid-1000',
        'injected-dml-rejected',
        'actual-panel-timeout',
        'hard-deadline-primitive',
        'forbidden-env-absent',
    ):
        assert required in script


def test_oci_index_is_not_used_as_the_portable_docker_image_id() -> None:
    controller = CONTROLLER.read_text(encoding='utf-8')
    script = E2E.read_text(encoding='utf-8')
    design = (ROOT / 'docs/entitlement_authority/gate2-shadow-one-shot-prerequisite-design.md').read_text(
        encoding='utf-8'
    )
    assert 'PRODUCTION_ENGINE_IMAGE_ID' in controller
    assert 'PORTABLE_AMD64_CONFIG_DIGEST' in controller
    assert 'runtime_image="${ONE_SHOT_E2E_IMAGE_REFERENCE:?}"' in controller
    assert 'test "$IMAGE" = "$CONFIG_DIGEST"' in script
    assert COMPATIBLE_IMAGE in design
    assert AMD64_MANIFEST in design
    assert AMD64_CONFIG in design


def test_ordinary_deploy_has_exact_control_only_allowlist_before_ssh() -> None:
    source = DEPLOY.read_text(encoding='utf-8')
    classifier = source.index('Classify exact Gate 2 control-only merge')
    ssh = source.index('appleboy/ssh-action@')
    assert classifier < ssh
    for allowed in (
        '.github/workflows/deploy.yml',
        '.github/workflows/entitlement-shadow-one-shot.yml',
        '.github/workflows/entitlement-shadow-one-shot-ci.yml',
        '.github/scripts/entitlement-shadow-one-shot-control.sh',
        '.github/scripts/entitlement_shadow_one_shot.py',
        '.github/scripts/entitlement_shadow_readonly_probe.py',
        '.github/scripts/entitlement_shadow_create_schema.py',
        '.github/scripts/test-entitlement-shadow-one-shot-e2e.sh',
        'tests/workflows/test_entitlement_shadow_one_shot.py',
        'docs/entitlement_authority/gate2-shadow-one-shot-prerequisite-design.md',
        'docs/entitlement_authority/gate2-shadow-one-shot-prerequisite-release-card.md',
    ):
        assert allowed in source
    assert 'control_only=true' in source
    assert "control_only != 'true'" in source


def test_shell_files_have_valid_bash_syntax() -> None:
    for path in (CONTROLLER, E2E):
        result = subprocess.run(  # noqa: S603 - fixed repository scripts
            ['/bin/bash', '-n', str(path)],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'LC_ALL': 'C'},
        )
        assert result.returncode == 0, result.stderr


def test_entrypoint_cli_rejects_non_json_and_unknown_fields() -> None:
    for payload in ('not-json', json.dumps({'event': 'wrong'})):
        result = subprocess.run(  # noqa: S603 - fixed repository entrypoint
            [str(ENTRYPOINT), '--validate-evidence'],
            input=payload,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, 'BOT_TOKEN': 'dummy'},
        )
        assert result.returncode != 0
        assert payload not in result.stdout

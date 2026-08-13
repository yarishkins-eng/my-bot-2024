from __future__ import annotations

import ast
import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from app.config import settings
from app.external import remnawave_api as remnawave_api_module
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError
from app.services.entitlement_authority import shadow_runtime
from app.services.entitlement_authority.shadow import (
    ReadOnlyShadowEvaluator,
    ShadowCandidate,
    ShadowCycleCounters,
    ShadowMetric,
    ShadowObservationError,
    ShadowPanelObservation,
    ShadowPolicy,
    normalize_shadow_observation,
)
from app.services.entitlement_authority.shadow_runtime import (
    EntitlementShadowService,
    LegacyPostgresShadowSource,
    ReadOnlyShadowRunner,
    ShadowSourceInvariantError,
    build_production_shadow_service,
)
from app.services.entitlement_authority.types import EntitlementSnapshot


NOW = datetime(2026, 8, 13, tzinfo=UTC)


def snapshot(**updates: object) -> EntitlementSnapshot:
    values: dict[str, object] = {
        'owner_key': 'internal-owner-proof-never-log',
        'panel_uuid': 'panel-uuid-never-log',
        'status': 'ACTIVE',
        'expire_at': NOW + timedelta(days=30),
        'traffic_limit_bytes': 100 * 1024**3,
        'traffic_limit_strategy': 'MONTH',
        'hwid_device_limit': 2,
        'internal_squads': ('squad-never-log',),
        'external_squad_uuid': None,
        'provenance': 'legacy_readonly_shadow',
        'generation': 1,
    }
    values.update(updates)
    return EntitlementSnapshot(**values)  # type: ignore[arg-type]


def candidate(**updates: object) -> ShadowCandidate:
    expected = updates.pop('expected', snapshot())
    return ShadowCandidate(
        expected=expected,  # type: ignore[arg-type]
        legacy_telegram_id=updates.pop('legacy_telegram_id', 100_001),  # type: ignore[arg-type]
        cohorts=updates.pop('cohorts', ('active_paid',)),  # type: ignore[arg-type]
    )


def observation(expected: EntitlementSnapshot | None = None, **updates: object) -> ShadowPanelObservation:
    desired = expected or snapshot()
    values: dict[str, object] = {
        'panel_uuid': desired.panel_uuid,
        'telegram_id': 100_001,
        'status': desired.status,
        'expire_at': desired.expire_at.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
        'traffic_limit_bytes': desired.traffic_limit_bytes,
        'traffic_limit_strategy': desired.traffic_limit_strategy,
        'hwid_device_limit': desired.hwid_device_limit,
        'internal_squads': desired.internal_squads,
        'external_squad_uuid': desired.external_squad_uuid,
    }
    values.update(updates)
    return ShadowPanelObservation(**values)  # type: ignore[arg-type]


class _Source:
    def __init__(self, candidates: Sequence[ShadowCandidate]) -> None:
        self.candidates = candidates
        self.calls = 0

    async def load_candidates(self, policy: ShadowPolicy, *, now: datetime) -> Sequence[ShadowCandidate]:
        self.calls += 1
        return self.candidates


class _FailingSource:
    def __init__(self, code: str) -> None:
        self.code = code

    async def load_candidates(self, policy: ShadowPolicy, *, now: datetime) -> Sequence[ShadowCandidate]:
        raise ShadowSourceInvariantError(self.code)


class _BlockingSource:
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    async def load_candidates(self, policy: ShadowPolicy, *, now: datetime) -> Sequence[ShadowCandidate]:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _PanelCycle:
    def __init__(self, observations: Sequence[ShadowPanelObservation | None | Exception]) -> None:
        self.observations = list(observations)
        self.calls: list[str] = []

    async def get_canonical(self, panel_uuid: str) -> ShadowPanelObservation | None:
        self.calls.append(panel_uuid)
        result = self.observations.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _PanelProvider:
    def __init__(self, cycle: _PanelCycle, *, fail_open: bool = False) -> None:
        self.cycle = cycle
        self.fail_open = fail_open
        self.opens = 0

    @asynccontextmanager
    async def open_cycle(self) -> AsyncIterator[_PanelCycle]:
        self.opens += 1
        if self.fail_open:
            raise RuntimeError('raw provider detail must not escape')
        yield self.cycle


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _CircuitRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> tuple[ShadowCycleCounters, float]:
        self.calls += 1
        counters = ShadowCycleCounters(stop_reason='owner_mismatch')
        return counters, 0.01


def policy(**updates: object) -> ShadowPolicy:
    values: dict[str, object] = {
        'cohort_basis_points': 1_000,
        'max_identities_per_cycle': 18,
        'schedule_seconds': 900,
        'panel_reads_per_minute': 12,
        'panel_timeout_seconds': 4.0,
        'db_statement_timeout_ms': 5_000,
        'max_cycle_seconds': 180.0,
        'min_ratio_sample': 10,
        'max_panel_read_errors': 2,
        'max_panel_read_error_basis_points': 1_000,
        'max_missing_count': 2,
        'max_missing_basis_points': 1_000,
        'max_critical_drift_count': 2,
        'max_critical_drift_basis_points': 1_000,
        'max_total_drift_count': 4,
        'max_total_drift_basis_points': 2_000,
    }
    values.update(updates)
    return ShadowPolicy(**values)  # type: ignore[arg-type]


def test_shadow_defaults_are_dormant_and_double_interlocked() -> None:
    assert settings.ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_SHADOW_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH is True
    assert build_production_shadow_service() is None


def test_shadow_refuses_writer_flag_even_if_double_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED', True)
    monkeypatch.setattr(settings, 'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH', False)
    monkeypatch.setattr(settings, 'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED', True)
    with pytest.raises(RuntimeError, match='writer_flags_false'):
        build_production_shadow_service()


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('cohort_basis_points', 0),
        ('max_identities_per_cycle', 101),
        ('schedule_seconds', 59),
        ('panel_reads_per_minute', 61),
        ('panel_timeout_seconds', 0),
        ('panel_timeout_seconds', 6),
        ('db_statement_timeout_ms', 30_001),
        ('max_cycle_seconds', 901),
        ('min_ratio_sample', 21),
        ('max_missing_basis_points', 0),
        ('max_total_drift_count', 21),
    ],
)
def test_policy_rejects_unsafe_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        policy(**{field: value})


def test_metrics_and_runtime_representations_contain_no_linkable_values() -> None:
    item = candidate(cohorts=('active_paid', 'direct_v2'))
    counters = ShadowCycleCounters()
    counters.add_candidate(item)
    counters.add_metric(ShadowMetric('drift', ('status', 'traffic_limit_bytes')))
    fields = counters.aggregate_log_fields(elapsed_seconds=1.25)
    rendered = repr((item, observation(), fields))
    for forbidden in (
        'internal-owner-proof-never-log',
        'panel-uuid-never-log',
        'squad-never-log',
        '100001',
        snapshot().desired_hash[:12],
    ):
        assert forbidden not in rendered
    assert set(fields) == {
        'schema',
        'sampled',
        'exact',
        'drift',
        'missing',
        'panel_read_errors',
        'contract_errors',
        'owner_mismatches',
        'comparator_instability',
        'rate_limit_violations',
        'critical_drift',
        'mismatch_fields',
        'cohorts',
        'elapsed_ms',
        'stopped',
        'stop_reason',
    }


def test_observation_requires_exact_panel_and_owner_binding() -> None:
    item = candidate()
    with pytest.raises(ShadowObservationError, match='panel_uuid_mismatch'):
        normalize_shadow_observation(item, observation(panel_uuid='foreign'))
    with pytest.raises(ShadowObservationError, match='owner_mismatch'):
        normalize_shadow_observation(item, observation(telegram_id=999))
    exact = normalize_shadow_observation(item, observation())
    assert ReadOnlyShadowEvaluator.evaluate(item.expected, exact).state == 'exact'


@pytest.mark.asyncio
async def test_runner_rate_limits_sequential_gets_and_emits_aggregates_only() -> None:
    items = [candidate(), candidate(), candidate()]
    cycle = _PanelCycle([observation(), observation(), observation()])
    clock = _Clock()
    counters, elapsed = await ReadOnlyShadowRunner(
        _Source(items),
        _PanelProvider(cycle),
        policy(),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run_once(now=NOW)
    assert counters.stop_reason is None
    assert (counters.sampled, counters.exact, counters.rate_limit_violations) == (3, 3, 0)
    assert cycle.calls == ['panel-uuid-never-log'] * 3
    assert clock.sleeps == [5.0, 5.0]
    assert elapsed == 10.0


@pytest.mark.asyncio
async def test_runner_stops_immediately_on_owner_mismatch() -> None:
    cycle = _PanelCycle([observation(telegram_id=999), observation()])
    counters, _ = await ReadOnlyShadowRunner(
        _Source([candidate(), candidate()]),
        _PanelProvider(cycle),
        policy(),
    ).run_once(now=NOW)
    assert counters.stop_reason == 'owner_mismatch'
    assert counters.sampled == 1
    assert len(cycle.calls) == 1


@pytest.mark.asyncio
async def test_runner_stops_on_two_missing_even_below_ratio_sample() -> None:
    cycle = _PanelCycle([None, None, observation()])
    counters, _ = await ReadOnlyShadowRunner(
        _Source([candidate(), candidate(), candidate()]),
        _PanelProvider(cycle),
        policy(),
    ).run_once(now=NOW)
    assert counters.stop_reason == 'panel_missing_count'
    assert (counters.sampled, counters.missing) == (2, 2)


@pytest.mark.asyncio
async def test_runner_stops_on_two_critical_identity_drifts_not_field_count() -> None:
    first = observation(status='DISABLED', traffic_limit_bytes=1)
    second = observation(status='DISABLED')
    counters, _ = await ReadOnlyShadowRunner(
        _Source([candidate(), candidate(), candidate()]),
        _PanelProvider(_PanelCycle([first, second, observation()])),
        policy(),
    ).run_once(now=NOW)
    assert counters.stop_reason == 'critical_access_drift_count'
    assert counters.critical_drift == 2
    assert counters.mismatch_fields == {'status': 2, 'traffic_limit_bytes': 1}


@pytest.mark.asyncio
async def test_runner_stops_on_error_ratio_at_minimum_sample() -> None:
    observations: list[ShadowPanelObservation | None | Exception] = [RuntimeError('secret detail')]
    observations.extend(observation() for _ in range(9))
    counters, _ = await ReadOnlyShadowRunner(
        _Source([candidate() for _ in range(10)]),
        _PanelProvider(_PanelCycle(observations)),
        policy(max_panel_read_errors=18),
    ).run_once(now=NOW)
    assert counters.stop_reason == 'panel_read_error_ratio'
    assert (counters.sampled, counters.panel_read_errors) == (10, 1)


@pytest.mark.asyncio
async def test_source_invariant_and_panel_open_fail_without_reads_or_raw_error() -> None:
    counters, _ = await ReadOnlyShadowRunner(
        _FailingSource('cross_owner_panel_uuid'),
        _PanelProvider(_PanelCycle([])),
        policy(),
    ).run_once(now=NOW)
    assert counters.stop_reason == 'cross_owner_panel_uuid'
    provider = _PanelProvider(_PanelCycle([]), fail_open=True)
    counters, _ = await ReadOnlyShadowRunner(_Source([]), provider, policy()).run_once(now=NOW)
    assert counters.stop_reason == 'panel_cycle_open_failed'
    assert 'raw provider detail' not in repr(counters.aggregate_log_fields(elapsed_seconds=0))


@pytest.mark.asyncio
async def test_whole_cycle_deadline_cancels_blocked_source_before_panel_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _BlockingSource()
    provider = _PanelProvider(_PanelCycle([]))
    timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def force_timeout(awaitable: Any, timeout: float | None = None) -> Any:
        timeouts.append(timeout)
        task = asyncio.create_task(awaitable)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise TimeoutError

    monkeypatch.setattr(shadow_runtime.asyncio, 'wait_for', force_timeout)
    counters, _ = await ReadOnlyShadowRunner(source, provider, policy()).run_once(now=NOW)
    monkeypatch.setattr(shadow_runtime.asyncio, 'wait_for', real_wait_for)

    assert source.started is True
    assert source.cancelled is True
    assert provider.opens == 0
    assert timeouts == [180.0]
    assert counters.stop_reason == 'cycle_deadline_exceeded'


@pytest.mark.asyncio
async def test_service_never_restarts_after_automatic_circuit_stop() -> None:
    runner = _CircuitRunner()
    service = EntitlementShadowService(runner, policy())  # type: ignore[arg-type]
    await service.run()
    assert runner.calls == 1


def test_legacy_row_conversion_is_strict_and_grace_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    row: dict[str, Any] = {
        'owner_id': 42,
        'telegram_id': 100_001,
        'panel_uuid': 'panel-uuid-never-log',
        'status': 'expired',
        'is_trial': False,
        'end_date': NOW - timedelta(days=1),
        'in_grace': True,
        'grace_until': NOW + timedelta(days=1),
        'traffic_limit_gb': 100,
        'device_limit': 2,
        'connected_squads': ['squad-never-log'],
        'traffic_reset_mode': 'MONTH',
        'external_squad_uuid': None,
        'is_access_point': False,
        'is_direct_v2': False,
    }
    item = LegacyPostgresShadowSource._candidate_from_row(row, now=NOW)
    assert item.expected.status == 'ACTIVE'
    assert item.expected.expire_at == NOW + timedelta(days=1)
    assert item.cohorts == ('grace',)
    rendered = repr(item)
    assert 'owner_id' not in rendered
    assert '100001' not in rendered
    assert 'panel-uuid-never-log' not in rendered
    assert item.expected.owner_key not in rendered
    with pytest.raises(ShadowSourceInvariantError, match='legacy_shadow_row_invalid'):
        LegacyPostgresShadowSource._candidate_from_row({**row, 'connected_squads': 'not-an-array'}, now=NOW)


@pytest.mark.asyncio
async def test_shadow_api_is_one_redacted_get_without_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = RemnaWaveAPI('https://panel.invalid', 'test-key')
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    response = {
        'uuid': 'panel-uuid-never-log',
        'telegramId': 100_001,
        'status': 'ACTIVE',
        'expireAt': '2026-09-12T00:00:00.000000Z',
        'trafficLimitBytes': 0,
        'trafficLimitStrategy': 'NO_RESET',
        'hwidDeviceLimit': None,
        'activeInternalSquads': [],
        'externalSquadUuid': None,
        'email': 'must-be-discarded@example.test',
        'subscriptionUrl': 'must-be-discarded',
    }

    async def request(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append((args, kwargs))
        return {'response': response}

    monkeypatch.setattr(api, '_make_request', request)
    monkeypatch.setattr(
        api,
        'enrich_user_with_happ_link',
        lambda value: pytest.fail('shadow GET must not enrich subscription links'),
    )
    filtered = await api.get_user_by_uuid_shadow_once('panel-uuid-never-log')
    assert filtered is not None
    assert set(filtered) == set(response) - {'email', 'subscriptionUrl'}
    assert calls == [
        (
            ('GET', '/api/users/panel-uuid-never-log'),
            {
                'log_endpoint': '/api/users/{redacted-shadow-uuid}',
                'redact_error_details': True,
                'log_http_errors_as_warning': True,
                'max_retries': 0,
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [400, 401, 403, 429, 500])
async def test_shadow_http_failure_is_redacted_warning_without_error_log(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    class Response:
        headers: dict[str, str] = {}

        def __init__(self, response_status: int) -> None:
            self.status = response_status

        async def text(self) -> str:
            return '{"message":"secret-panel-detail"}'

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class Session:
        def request(self, method: str, **kwargs: object) -> Response:
            return Response(status)

    class CapturingLogger:
        def __init__(self) -> None:
            self.warnings: list[tuple[object, ...]] = []
            self.errors: list[tuple[object, ...]] = []

        def warning(self, *args: object, **kwargs: object) -> None:
            self.warnings.append((*args, kwargs))

        def error(self, *args: object, **kwargs: object) -> None:
            self.errors.append((*args, kwargs))

    api = RemnaWaveAPI('https://panel.invalid', 'test-key')
    api.session = Session()  # type: ignore[assignment]
    logger = CapturingLogger()
    monkeypatch.setattr(remnawave_api_module, 'logger', logger)

    with pytest.raises(RemnaWaveAPIError, match='Protected inventory request failed'):
        await api.get_user_by_uuid_shadow_once('panel-uuid-never-log')

    rendered = repr(logger.warnings)
    assert logger.errors == []
    assert len(logger.warnings) == 1
    assert 'panel-uuid-never-log' not in rendered
    assert 'secret-panel-detail' not in rendered
    assert 'Protected inventory request failed' in rendered


@pytest.mark.parametrize(
    ('field', 'invalid'),
    [
        ('status', 'active'),
        ('trafficLimitBytes', False),
        ('trafficLimitBytes', '0'),
        ('trafficLimitStrategy', ['MONTH']),
        ('hwidDeviceLimit', True),
        ('telegramId', '100001'),
        ('expireAt', NOW),
        ('activeInternalSquads', 'squad'),
        ('externalSquadUuid', False),
    ],
)
def test_shadow_panel_decoder_rejects_legacy_coercions(field: str, invalid: object) -> None:
    raw: dict[str, object] = {
        'uuid': 'panel-uuid-never-log',
        'telegramId': 100_001,
        'status': 'ACTIVE',
        'expireAt': '2026-09-12T00:00:00.000000Z',
        'trafficLimitBytes': 0,
        'trafficLimitStrategy': 'NO_RESET',
        'hwidDeviceLimit': None,
        'activeInternalSquads': [],
        'externalSquadUuid': None,
    }
    raw[field] = invalid
    with pytest.raises(ShadowObservationError, match='panel_contract_error'):
        shadow_runtime._RemnaWaveShadowPanelCycle._convert(raw)


def test_shadow_panel_decoder_requires_all_and_only_comparison_fields() -> None:
    raw: dict[str, object] = {
        'uuid': 'panel-uuid-never-log',
        'telegramId': 100_001,
        'status': 'ACTIVE',
        'expireAt': '2026-09-12T00:00:00.000000Z',
        'trafficLimitBytes': 0,
        'trafficLimitStrategy': 'NO_RESET',
        'hwidDeviceLimit': None,
        'activeInternalSquads': [],
        'externalSquadUuid': None,
    }
    for changed in ({key: value for key, value in raw.items() if key != 'hwidDeviceLimit'}, {**raw, 'email': 'x'}):
        with pytest.raises(ShadowObservationError, match='panel_contract_error'):
            shadow_runtime._RemnaWaveShadowPanelCycle._convert(changed)


def test_shadow_runtime_has_no_mutating_sql_or_writer_dependencies() -> None:
    sql = shadow_runtime._SOURCE_PREFLIGHT_SQL + shadow_runtime._SOURCE_COHORT_SQL
    assert not re.search(
        r'\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|CALL)\b',
        sql,
        re.IGNORECASE,
    )
    source_path = Path(shadow_runtime.__file__ or '')
    tree = ast.parse(source_path.read_text(encoding='utf-8'))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
    }
    assert imported.isdisjoint(
        {
            'ProjectionCoordinator',
            'PostgresEntitlementStore',
            'append_source_and_command',
            'public_access_point_service',
            'device_first_recovery_service',
            'remnawave_webhook_service',
            'notification_service',
            'payment_service',
        }
    )
    api_method = inspect.getsource(RemnaWaveAPI.get_user_by_uuid_shadow_once)
    assert "'GET'" in api_method
    assert not re.search(r"['\"](?:POST|PATCH|DELETE|PUT|ACTION)['\"]", api_method)
    service_source = inspect.getsource(EntitlementShadowService)
    assert 'logger.error' not in service_source
    assert 'logger.critical' not in service_source


def test_candidate_diff_does_not_touch_migrations_or_legacy_flow_modules() -> None:
    project_root = Path(__file__).resolve().parents[2]
    protected = (
        project_root / 'app/services/device_first_recovery_service.py',
        project_root / 'app/services/public_access_point_service.py',
        project_root / 'app/services/remnawave_webhook_service.py',
        project_root / 'app/services/notification_delivery_service.py',
        project_root / 'app/services/payment_service.py',
    )
    for path in protected:
        assert path.exists()
    migrations = sorted((project_root / 'migrations/alembic/versions').glob('*entitlement_authority*'))
    assert [path.name for path in migrations] == ['0103_entitlement_authority_dormant.py']

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .types import EntitlementSnapshot, compare_snapshots


_ALLOWED_COHORTS = frozenset({'active_paid', 'trial', 'limited', 'grace', 'access_point', 'direct_v2'})
_CRITICAL_ACCESS_FIELDS = frozenset(
    {
        'status',
        'expire_at',
        'traffic_limit_bytes',
        'traffic_limit_strategy',
        'hwid_device_limit',
        'internal_squads',
        'external_squad_uuid',
    }
)


@dataclass(frozen=True, slots=True)
class ShadowMetric:
    state: str
    mismatch_fields: tuple[str, ...]


class ReadOnlyShadowEvaluator:
    """Pure comparator with aggregate-safe output and no persistence/client boundary."""

    @staticmethod
    def evaluate(desired: EntitlementSnapshot, observed: EntitlementSnapshot | None) -> ShadowMetric:
        if observed is None:
            return ShadowMetric('missing', ('panel_uuid',))
        comparison = compare_snapshots(desired, observed)
        mismatch_fields = comparison.mismatch_fields
        if 'expire_at' in mismatch_fields and _same_utc_millisecond_bucket(desired.expire_at, observed.expire_at):
            mismatch_fields = tuple(field for field in mismatch_fields if field != 'expire_at')
        return ShadowMetric(
            'exact' if not mismatch_fields else 'drift',
            mismatch_fields,
        )


def _same_utc_millisecond_bucket(left: datetime, right: datetime) -> bool:
    def truncate(value: datetime) -> datetime:
        normalized = value.astimezone(UTC)
        return normalized.replace(microsecond=normalized.microsecond // 1_000 * 1_000)

    return truncate(left) == truncate(right)


@dataclass(frozen=True, slots=True)
class ShadowPolicy:
    cohort_basis_points: int = 1_000
    max_identities_per_cycle: int = 18
    schedule_seconds: int = 900
    panel_reads_per_minute: int = 12
    panel_timeout_seconds: float = 4.0
    db_statement_timeout_ms: int = 5_000
    max_cycle_seconds: float = 180.0
    min_ratio_sample: int = 10
    max_panel_read_errors: int = 2
    max_panel_read_error_basis_points: int = 1_000
    max_missing_count: int = 2
    max_missing_basis_points: int = 1_000
    max_critical_drift_count: int = 2
    max_critical_drift_basis_points: int = 1_000
    max_total_drift_count: int = 4
    max_total_drift_basis_points: int = 2_000

    def __post_init__(self) -> None:
        if not 1 <= self.cohort_basis_points <= 10_000:
            raise ValueError('shadow cohort must be between 1 and 10000 basis points')
        if not 1 <= self.max_identities_per_cycle <= 100:
            raise ValueError('shadow cycle identity cap must be between 1 and 100')
        if not 60 <= self.schedule_seconds <= 86_400:
            raise ValueError('shadow schedule must be between 60 seconds and 24 hours')
        if not 1 <= self.panel_reads_per_minute <= 60:
            raise ValueError('shadow Panel rate must be between 1 and 60 reads per minute')
        if not 0 < self.panel_timeout_seconds <= 30:
            raise ValueError('shadow Panel timeout must be in (0, 30] seconds')
        if not 100 <= self.db_statement_timeout_ms <= 30_000:
            raise ValueError('shadow DB statement timeout must be between 100 and 30000 ms')
        if not 10 <= self.max_cycle_seconds <= self.schedule_seconds:
            raise ValueError('shadow cycle timeout must fit inside its schedule')
        worst_case_seconds = self.max_identities_per_cycle * self.panel_timeout_seconds + (
            self.max_identities_per_cycle - 1
        ) * (60.0 / self.panel_reads_per_minute)
        if worst_case_seconds > self.max_cycle_seconds:
            raise ValueError('shadow read and rate budgets must fit inside the cycle deadline')
        if not 1 <= self.min_ratio_sample <= self.max_identities_per_cycle:
            raise ValueError('shadow ratio sample must fit inside the identity cap')
        for name in (
            'max_panel_read_error_basis_points',
            'max_missing_basis_points',
            'max_critical_drift_basis_points',
            'max_total_drift_basis_points',
        ):
            if not 1 <= getattr(self, name) <= 10_000:
                raise ValueError(f'{name} must be between 1 and 10000')
        for name in (
            'max_panel_read_errors',
            'max_missing_count',
            'max_critical_drift_count',
            'max_total_drift_count',
        ):
            if not 1 <= getattr(self, name) <= self.max_identities_per_cycle:
                raise ValueError(f'{name} must fit inside the identity cap')

    @property
    def minimum_read_interval_seconds(self) -> float:
        return 60.0 / self.panel_reads_per_minute


@dataclass(frozen=True, slots=True, repr=False)
class ShadowCandidate:
    expected: EntitlementSnapshot
    legacy_telegram_id: int
    cohorts: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.legacy_telegram_id) is not int or self.legacy_telegram_id <= 0:
            raise ValueError('shadow candidate requires a positive legacy owner identifier')
        if not self.cohorts or any(cohort not in _ALLOWED_COHORTS for cohort in self.cohorts):
            raise ValueError('shadow candidate has an unsupported cohort')


@dataclass(frozen=True, slots=True, repr=False)
class ShadowPanelObservation:
    panel_uuid: str
    telegram_id: int | None
    status: str
    expire_at: str
    traffic_limit_bytes: int
    traffic_limit_strategy: str
    hwid_device_limit: int | None
    internal_squads: tuple[str, ...]
    external_squad_uuid: str | None


class ShadowObservationError(ValueError):
    """Fixed-code observation failure; raw Panel values must never enter logs."""


def normalize_shadow_observation(
    candidate: ShadowCandidate,
    observation: ShadowPanelObservation,
) -> EntitlementSnapshot:
    expected = candidate.expected
    if observation.panel_uuid != expected.panel_uuid:
        raise ShadowObservationError('panel_uuid_mismatch')
    if observation.telegram_id != candidate.legacy_telegram_id:
        raise ShadowObservationError('owner_mismatch')
    return EntitlementSnapshot.from_mapping(
        {
            'owner_key': expected.owner_key,
            'panel_uuid': observation.panel_uuid,
            'status': observation.status,
            'expire_at': observation.expire_at,
            'traffic_limit_bytes': observation.traffic_limit_bytes,
            'traffic_limit_strategy': observation.traffic_limit_strategy,
            'hwid_device_limit': observation.hwid_device_limit,
            'internal_squads': list(observation.internal_squads),
            'external_squad_uuid': observation.external_squad_uuid,
            'provenance': expected.provenance,
            'generation': expected.generation,
            'reset_epoch': expected.reset_epoch,
            'revoke_epoch': expected.revoke_epoch,
            'deny_overlays': list(expected.deny_overlays),
        }
    )


@dataclass(slots=True)
class ShadowCycleCounters:
    sampled: int = 0
    exact: int = 0
    drift: int = 0
    missing: int = 0
    panel_read_errors: int = 0
    contract_errors: int = 0
    owner_mismatches: int = 0
    comparator_instability: int = 0
    rate_limit_violations: int = 0
    critical_drift: int = 0
    mismatch_fields: Counter[str] = field(default_factory=Counter)
    cohorts: Counter[str] = field(default_factory=Counter)
    stop_reason: str | None = None

    @staticmethod
    def _basis_points(count: int, total: int) -> int:
        return count * 10_000 // max(total, 1)

    def add_candidate(self, candidate: ShadowCandidate) -> None:
        self.sampled += 1
        self.cohorts.update(candidate.cohorts)

    def add_metric(self, metric: ShadowMetric) -> None:
        if metric.state == 'exact':
            self.exact += 1
        elif metric.state == 'missing':
            self.missing += 1
        elif metric.state == 'drift':
            self.drift += 1
            self.mismatch_fields.update(metric.mismatch_fields)
            if _CRITICAL_ACCESS_FIELDS.intersection(metric.mismatch_fields):
                self.critical_drift += 1
        else:
            self.contract_errors += 1

    def apply_thresholds(self, policy: ShadowPolicy, *, elapsed_seconds: float) -> str | None:
        if self.owner_mismatches:
            self.stop_reason = 'owner_mismatch'
        elif self.contract_errors:
            self.stop_reason = 'panel_contract_error'
        elif self.comparator_instability:
            self.stop_reason = 'comparator_instability'
        elif self.rate_limit_violations:
            self.stop_reason = 'rate_limit_violation'
        elif elapsed_seconds > policy.max_cycle_seconds:
            self.stop_reason = 'cycle_deadline_exceeded'
        elif self.panel_read_errors >= policy.max_panel_read_errors:
            self.stop_reason = 'panel_read_error_count'
        elif self.sampled >= policy.min_ratio_sample and (
            self._basis_points(self.panel_read_errors, self.sampled) >= policy.max_panel_read_error_basis_points
        ):
            self.stop_reason = 'panel_read_error_ratio'
        elif self.missing >= policy.max_missing_count:
            self.stop_reason = 'panel_missing_count'
        elif self.sampled >= policy.min_ratio_sample and (
            self._basis_points(self.missing, self.sampled) >= policy.max_missing_basis_points
        ):
            self.stop_reason = 'panel_missing_ratio'
        elif self.critical_drift >= policy.max_critical_drift_count:
            self.stop_reason = 'critical_access_drift_count'
        elif self.sampled >= policy.min_ratio_sample and (
            self._basis_points(self.critical_drift, self.sampled) >= policy.max_critical_drift_basis_points
        ):
            self.stop_reason = 'critical_access_drift_ratio'
        elif self.drift >= policy.max_total_drift_count:
            self.stop_reason = 'total_drift_count'
        elif self.sampled >= policy.min_ratio_sample and (
            self._basis_points(self.drift, self.sampled) >= policy.max_total_drift_basis_points
        ):
            self.stop_reason = 'total_drift_ratio'
        return self.stop_reason

    def aggregate_log_fields(self, *, elapsed_seconds: float) -> dict[str, object]:
        """Return a strict allowlist of aggregate-only, non-linkable log fields."""

        return {
            'schema': 'entitlement_shadow_metrics_v1',
            'sampled': self.sampled,
            'exact': self.exact,
            'drift': self.drift,
            'missing': self.missing,
            'panel_read_errors': self.panel_read_errors,
            'contract_errors': self.contract_errors,
            'owner_mismatches': self.owner_mismatches,
            'comparator_instability': self.comparator_instability,
            'rate_limit_violations': self.rate_limit_violations,
            'critical_drift': self.critical_drift,
            'mismatch_fields': dict(sorted(self.mismatch_fields.items())),
            'cohorts': dict(sorted(self.cohorts.items())),
            'elapsed_ms': round(elapsed_seconds * 1_000),
            'stopped': self.stop_reason is not None,
            'stop_reason': self.stop_reason or 'none',
        }

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.external.remnawave_api import RemnaWaveAPI
from app.services.remnawave_service import RemnaWaveService

from .shadow import (
    ReadOnlyShadowEvaluator,
    ShadowCandidate,
    ShadowCycleCounters,
    ShadowObservationError,
    ShadowPanelObservation,
    ShadowPolicy,
    normalize_shadow_observation,
)
from .types import EntitlementSnapshot


logger = structlog.get_logger(__name__)

_COHORT_SEED = 'gate2-shadow-v1'
_SOURCE_INVARIANT_CODES = frozenset(
    {
        'multiple_current_subscriptions',
        'owner_uuid_binding_mismatch',
        'cross_owner_panel_uuid',
        'legacy_shadow_row_invalid',
        'multi_tariff_not_supported',
    }
)

_SOURCE_PREFLIGHT_SQL = """
WITH current_access AS (
    SELECT user_id, count(*) AS current_count
      FROM subscriptions
     WHERE (status IN ('active', 'trial', 'limited') AND end_date > :now)
        OR (in_grace IS TRUE AND grace_until > :now)
     GROUP BY user_id
), all_refs AS (
    SELECT id AS owner_id, remnawave_uuid AS panel_uuid
      FROM users WHERE remnawave_uuid IS NOT NULL
    UNION ALL
    SELECT user_id AS owner_id, remnawave_uuid AS panel_uuid
      FROM subscriptions WHERE remnawave_uuid IS NOT NULL
)
SELECT EXISTS (
           SELECT 1 FROM current_access WHERE current_count > 1
       ) AS multiple_current_subscriptions,
       EXISTS (
           SELECT 1
             FROM users u
            JOIN subscriptions s ON s.user_id=u.id
            WHERE (
                      (s.status IN ('active', 'trial', 'limited') AND s.end_date > :now)
                   OR (s.in_grace IS TRUE AND s.grace_until > :now)
                  )
              AND u.remnawave_uuid IS NOT NULL
              AND s.remnawave_uuid IS NOT NULL
              AND u.remnawave_uuid <> s.remnawave_uuid
       ) AS owner_uuid_binding_mismatch,
       EXISTS (
           SELECT 1 FROM all_refs
            GROUP BY panel_uuid
           HAVING count(DISTINCT owner_id) > 1
       ) AS cross_owner_panel_uuid
"""

_SOURCE_COHORT_SQL = """
WITH current_access AS (
    SELECT *
      FROM subscriptions
     WHERE (status IN ('active', 'trial', 'limited') AND end_date > :now)
        OR (in_grace IS TRUE AND grace_until > :now)
), ranked AS (
    SELECT s.*,
           row_number() OVER (
               PARTITION BY s.user_id
               ORDER BY CASE WHEN s.status IN ('active', 'trial', 'limited') THEN 0 ELSE 1 END,
                        s.created_at DESC,
                        s.id DESC
           ) AS rn
      FROM current_access s
), eligible AS (
    SELECT u.id AS owner_id,
           u.telegram_id,
           coalesce(u.remnawave_uuid, s.remnawave_uuid) AS panel_uuid,
           s.status,
           s.is_trial,
           s.end_date,
           s.in_grace,
           s.grace_until,
           s.traffic_limit_gb,
           s.device_limit,
           s.connected_squads,
           t.traffic_reset_mode,
           t.external_squad_uuid,
           EXISTS (
               SELECT 1 FROM subscription_entitlement_terms term
                WHERE term.subscription_id=s.id
                  AND term.starts_at <= :now
                  AND term.ends_at > :now
           ) AS is_access_point,
           EXISTS (
               SELECT 1 FROM subscription_checkouts checkout
                WHERE checkout.created_subscription_id=s.id
                  AND checkout.lifecycle_state='ready'
                  AND checkout.settlement_mode='direct_purchase_v2'
           ) AS is_direct_v2,
           (hashtextextended(u.id::text || :cohort_seed, 0) & 9223372036854775807) AS sample_hash
      FROM users u
      JOIN ranked s ON s.user_id=u.id AND s.rn=1
 LEFT JOIN tariffs t ON t.id=s.tariff_id
     WHERE u.telegram_id IS NOT NULL
       AND coalesce(u.remnawave_uuid, s.remnawave_uuid) IS NOT NULL
       AND u.account_erasure_requested_at IS NULL
       AND u.account_erased_at IS NULL
)
SELECT *
  FROM eligible
 WHERE status='limited'
    OR in_grace IS TRUE
    OR is_access_point
    OR is_direct_v2
    OR mod(sample_hash, 10000) < :cohort_basis_points
 ORDER BY (status='limited' OR in_grace IS TRUE OR is_access_point OR is_direct_v2) DESC,
          sample_hash,
          owner_id
 LIMIT :max_identities
"""


class ShadowSourceInvariantError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in _SOURCE_INVARIANT_CODES:
            code = 'legacy_shadow_row_invalid'
        super().__init__(code)
        self.code = code


class ReadOnlyShadowSource(Protocol):
    async def load_candidates(self, policy: ShadowPolicy, *, now: datetime) -> Sequence[ShadowCandidate]: ...


class ShadowPanelCycle(Protocol):
    async def get_canonical(self, panel_uuid: str) -> ShadowPanelObservation | None: ...


class ShadowPanelProvider(Protocol):
    def open_cycle(self) -> AbstractAsyncContextManager[ShadowPanelCycle]: ...


def _exact_string_list(value: object) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise ShadowSourceInvariantError('legacy_shadow_row_invalid')
    return tuple(value)


class LegacyPostgresShadowSource:
    """Read current legacy projection state through a forced read-only transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def load_candidates(self, policy: ShadowPolicy, *, now: datetime) -> Sequence[ShadowCandidate]:
        if settings.MULTI_TARIFF_ENABLED:
            raise ShadowSourceInvariantError('multi_tariff_not_supported')
        async with self._sessions() as session:
            await session.begin()
            try:
                await session.execute(text('SET TRANSACTION READ ONLY'))
                await session.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {'timeout': f'{policy.db_statement_timeout_ms}ms'},
                )
                preflight = (await session.execute(text(_SOURCE_PREFLIGHT_SQL), {'now': now})).mappings().one()
                for code in (
                    'multiple_current_subscriptions',
                    'owner_uuid_binding_mismatch',
                    'cross_owner_panel_uuid',
                ):
                    if preflight[code]:
                        raise ShadowSourceInvariantError(code)
                rows = (
                    (
                        await session.execute(
                            text(_SOURCE_COHORT_SQL),
                            {
                                'now': now,
                                'cohort_seed': _COHORT_SEED,
                                'cohort_basis_points': policy.cohort_basis_points,
                                'max_identities': policy.max_identities_per_cycle,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            finally:
                await session.rollback()
        return tuple(self._candidate_from_row(row, now=now) for row in rows)

    @staticmethod
    def _candidate_from_row(row: Mapping[str, object], *, now: datetime) -> ShadowCandidate:
        try:
            owner_id = row['owner_id']
            telegram_id = row['telegram_id']
            panel_uuid = row['panel_uuid']
            status = row['status']
            end_date = row['end_date']
            grace_until = row['grace_until']
            if type(owner_id) is not int or type(telegram_id) is not int:
                raise ValueError
            if type(panel_uuid) is not str or not panel_uuid:
                raise ValueError
            if type(status) is not str or status not in {'active', 'trial', 'limited', 'expired'}:
                raise ValueError
            if not isinstance(end_date, datetime) or end_date.tzinfo is None:
                raise ValueError
            in_grace = row['in_grace'] is True and isinstance(grace_until, datetime) and grace_until > now
            if status == 'expired' and not in_grace:
                raise ValueError
            expire_at = grace_until if in_grace else end_date
            if not isinstance(expire_at, datetime) or expire_at.tzinfo is None:
                raise ValueError
            traffic_gb = row['traffic_limit_gb']
            device_limit = row['device_limit']
            if type(traffic_gb) is not int or traffic_gb < 0:
                raise ValueError
            if device_limit is not None and (type(device_limit) is not int or device_limit < 0):
                raise ValueError
            reset_mode = row['traffic_reset_mode'] or settings.DEFAULT_TRAFFIC_RESET_STRATEGY
            if type(reset_mode) is not str or reset_mode.upper() not in {
                'NO_RESET',
                'DAY',
                'WEEK',
                'MONTH',
                'MONTH_ROLLING',
            }:
                raise ValueError
            external_squad = row['external_squad_uuid']
            if external_squad is not None and (type(external_squad) is not str or not external_squad):
                raise ValueError
            squads = _exact_string_list(row['connected_squads'])
        except (KeyError, TypeError, ValueError) as error:
            raise ShadowSourceInvariantError('legacy_shadow_row_invalid') from error

        desired_status = 'LIMITED' if status == 'limited' else 'ACTIVE'
        if in_grace:
            base_cohort = 'grace'
        elif status == 'limited':
            base_cohort = 'limited'
        elif row['is_trial'] is True:
            base_cohort = 'trial'
        else:
            base_cohort = 'active_paid'
        cohorts = [base_cohort]
        if row['is_access_point'] is True:
            cohorts.append('access_point')
        if row['is_direct_v2'] is True:
            cohorts.append('direct_v2')
        owner_key = hashlib.sha256(f'legacy-readonly-shadow:{owner_id}'.encode()).hexdigest()
        hwid_device_limit = device_limit or None
        if not settings.is_devices_selection_enabled():
            forced_device_limit = settings.get_disabled_mode_device_limit()
            if forced_device_limit is not None and forced_device_limit > 0:
                hwid_device_limit = forced_device_limit
        expected = EntitlementSnapshot(
            owner_key=owner_key,
            panel_uuid=panel_uuid,
            status=desired_status,
            expire_at=expire_at,
            traffic_limit_bytes=traffic_gb * 1024**3,
            traffic_limit_strategy=reset_mode.upper(),
            hwid_device_limit=hwid_device_limit,
            internal_squads=squads,
            external_squad_uuid=external_squad,
            provenance='legacy_readonly_shadow',
            generation=1,
            deny_overlays=('limited',) if status == 'limited' else (),
        )
        return ShadowCandidate(expected=expected, legacy_telegram_id=telegram_id, cohorts=tuple(cohorts))


class _RemnaWaveShadowPanelCycle:
    def __init__(self, api: RemnaWaveAPI) -> None:
        self._api = api

    async def get_canonical(self, panel_uuid: str) -> ShadowPanelObservation | None:
        user = await self._api.get_user_by_uuid_shadow_once(panel_uuid)
        if user is None:
            return None
        return self._convert(user)

    @staticmethod
    def _convert(user: Mapping[str, object]) -> ShadowPanelObservation:
        try:
            required = {
                'uuid',
                'telegramId',
                'status',
                'expireAt',
                'trafficLimitBytes',
                'trafficLimitStrategy',
                'hwidDeviceLimit',
                'activeInternalSquads',
                'externalSquadUuid',
            }
            if set(user) != required:
                raise ValueError
            panel_uuid = user['uuid']
            telegram_id = user['telegramId']
            status = user['status']
            expire_raw = user['expireAt']
            traffic_limit_bytes = user['trafficLimitBytes']
            traffic_strategy = user['trafficLimitStrategy']
            hwid_device_limit = user['hwidDeviceLimit']
            active_squads = user['activeInternalSquads']
            external = user['externalSquadUuid']
            if type(panel_uuid) is not str or not panel_uuid:
                raise ValueError
            if telegram_id is not None and type(telegram_id) is not int:
                raise ValueError
            if type(status) is not str or status not in {'ACTIVE', 'DISABLED', 'LIMITED', 'EXPIRED'}:
                raise ValueError
            if type(expire_raw) is not str or not expire_raw:
                raise ValueError
            expire_at = datetime.fromisoformat(expire_raw.replace('Z', '+00:00'))
            if expire_at.tzinfo is None:
                raise ValueError
            if type(traffic_limit_bytes) is not int or traffic_limit_bytes < 0:
                raise ValueError
            if type(traffic_strategy) is not str or traffic_strategy not in {
                'NO_RESET',
                'DAY',
                'WEEK',
                'MONTH',
                'MONTH_ROLLING',
            }:
                raise ValueError
            if hwid_device_limit is not None and (type(hwid_device_limit) is not int or hwid_device_limit < 0):
                raise ValueError
            if external is not None and (type(external) is not str or not external):
                raise ValueError
            squads: list[str] = []
            if type(active_squads) is not list:
                raise ValueError
            for item in active_squads:
                if type(item) is not dict or type(item.get('uuid')) is not str or not item['uuid']:
                    raise ValueError
                squads.append(item['uuid'])
            normalized_expiry = expire_at.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            return ShadowPanelObservation(
                panel_uuid=panel_uuid,
                telegram_id=telegram_id,
                status=status,
                expire_at=normalized_expiry,
                traffic_limit_bytes=traffic_limit_bytes,
                traffic_limit_strategy=traffic_strategy,
                hwid_device_limit=hwid_device_limit,
                internal_squads=tuple(squads),
                external_squad_uuid=external,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ShadowObservationError('panel_contract_error') from error


class RemnaWaveShadowPanelProvider:
    def __init__(self, service: RemnaWaveService | None = None) -> None:
        self._service = service or RemnaWaveService()

    @asynccontextmanager
    async def open_cycle(self) -> AsyncIterator[ShadowPanelCycle]:
        async with self._service.get_api_client() as api:
            yield _RemnaWaveShadowPanelCycle(api)


class ReadOnlyShadowRunner:
    def __init__(
        self,
        source: ReadOnlyShadowSource,
        panel: ShadowPanelProvider,
        policy: ShadowPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._source = source
        self._panel = panel
        self._policy = policy
        self._monotonic = monotonic
        self._sleep = sleep

    async def run_once(self, *, now: datetime | None = None) -> tuple[ShadowCycleCounters, float]:
        started = self._monotonic()
        counters = ShadowCycleCounters()
        timed_out = False
        try:
            await asyncio.wait_for(
                self._run_cycle(counters, now=now or datetime.now(UTC), started=started),
                timeout=self._policy.max_cycle_seconds,
            )
        except TimeoutError:
            timed_out = True
        elapsed = self._monotonic() - started
        counters.apply_thresholds(self._policy, elapsed_seconds=elapsed)
        if timed_out:
            counters.stop_reason = 'cycle_deadline_exceeded'
        return counters, elapsed

    async def _run_cycle(
        self,
        counters: ShadowCycleCounters,
        *,
        now: datetime,
        started: float,
    ) -> None:
        try:
            candidates = await self._source.load_candidates(self._policy, now=now)
        except ShadowSourceInvariantError as error:
            counters.stop_reason = error.code
            return
        if len(candidates) > self._policy.max_identities_per_cycle:
            counters.stop_reason = 'legacy_shadow_row_invalid'
            return

        try:
            async with self._panel.open_cycle() as panel:
                previous_read_started: float | None = None
                for candidate in candidates:
                    if previous_read_started is not None:
                        remaining = self._policy.minimum_read_interval_seconds - (
                            self._monotonic() - previous_read_started
                        )
                        if remaining > 0:
                            await self._sleep(remaining)
                    read_started = self._monotonic()
                    if previous_read_started is not None and (
                        read_started - previous_read_started + 0.001 < self._policy.minimum_read_interval_seconds
                    ):
                        counters.rate_limit_violations += 1
                    previous_read_started = read_started
                    counters.add_candidate(candidate)
                    try:
                        observation = await asyncio.wait_for(
                            panel.get_canonical(candidate.expected.panel_uuid or ''),
                            timeout=self._policy.panel_timeout_seconds,
                        )
                        if observation is None:
                            metric = ReadOnlyShadowEvaluator.evaluate(candidate.expected, None)
                        else:
                            observed = normalize_shadow_observation(candidate, observation)
                            metric = ReadOnlyShadowEvaluator.evaluate(candidate.expected, observed)
                            repeated = ReadOnlyShadowEvaluator.evaluate(candidate.expected, observed)
                            if repeated != metric:
                                counters.comparator_instability += 1
                    except ShadowObservationError as error:
                        if str(error) == 'owner_mismatch':
                            counters.owner_mismatches += 1
                        else:
                            counters.contract_errors += 1
                        metric = None
                    except ValueError:
                        counters.contract_errors += 1
                        metric = None
                    except Exception:
                        counters.panel_read_errors += 1
                        metric = None
                    if metric is not None:
                        counters.add_metric(metric)
                    elapsed = self._monotonic() - started
                    if counters.apply_thresholds(self._policy, elapsed_seconds=elapsed):
                        break
        except Exception:
            counters.panel_read_errors += 1
            counters.stop_reason = 'panel_cycle_open_failed'


class EntitlementShadowService:
    def __init__(self, runner: ReadOnlyShadowRunner, policy: ShadowPolicy) -> None:
        self._runner = runner
        self._policy = policy
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            counters, elapsed = await self._runner.run_once()
            fields = counters.aggregate_log_fields(elapsed_seconds=elapsed)
            if counters.stop_reason:
                logger.warning('entitlement_shadow_circuit_open', **fields)
                return
            logger.info('entitlement_shadow_cycle', **fields)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._policy.schedule_seconds)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()


def shadow_policy_from_settings() -> ShadowPolicy:
    return ShadowPolicy(
        cohort_basis_points=settings.ENTITLEMENT_AUTHORITY_SHADOW_COHORT_BASIS_POINTS,
        max_identities_per_cycle=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_IDENTITIES_PER_CYCLE,
        schedule_seconds=settings.ENTITLEMENT_AUTHORITY_SHADOW_SCHEDULE_SECONDS,
        panel_reads_per_minute=settings.ENTITLEMENT_AUTHORITY_SHADOW_PANEL_READS_PER_MINUTE,
        panel_timeout_seconds=settings.ENTITLEMENT_AUTHORITY_SHADOW_PANEL_TIMEOUT_SECONDS,
        db_statement_timeout_ms=settings.ENTITLEMENT_AUTHORITY_SHADOW_DB_STATEMENT_TIMEOUT_MS,
        max_cycle_seconds=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_CYCLE_SECONDS,
        min_ratio_sample=settings.ENTITLEMENT_AUTHORITY_SHADOW_MIN_RATIO_SAMPLE,
        max_panel_read_errors=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERRORS,
        max_panel_read_error_basis_points=(settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_PANEL_READ_ERROR_BASIS_POINTS),
        max_missing_count=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_COUNT,
        max_missing_basis_points=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_MISSING_BASIS_POINTS,
        max_critical_drift_count=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_COUNT,
        max_critical_drift_basis_points=(settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_CRITICAL_DRIFT_BASIS_POINTS),
        max_total_drift_count=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_COUNT,
        max_total_drift_basis_points=settings.ENTITLEMENT_AUTHORITY_SHADOW_MAX_TOTAL_DRIFT_BASIS_POINTS,
    )


def build_production_shadow_service() -> EntitlementShadowService | None:
    if not settings.ENTITLEMENT_AUTHORITY_SHADOW_ENABLED:
        return None
    if settings.ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH:
        return None
    if any(
        (
            settings.ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED,
            settings.ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED,
            settings.ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED,
        )
    ):
        raise RuntimeError('shadow_requires_all_writer_flags_false')
    if settings.MULTI_TARIFF_ENABLED:
        raise RuntimeError('shadow_requires_verified_single_tariff_mode')
    policy = shadow_policy_from_settings()
    runner = ReadOnlyShadowRunner(
        LegacyPostgresShadowSource(AsyncSessionLocal),
        RemnaWaveShadowPanelProvider(),
        policy,
    )
    return EntitlementShadowService(runner, policy)

#!/usr/bin/env python3
"""Run exactly one aggregate-only entitlement shadow observation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import structlog

from app.database.database import AsyncSessionLocal
from app.services.entitlement_authority.shadow_runtime import (
    LegacyPostgresShadowSource,
    ReadOnlyShadowRunner,
    RemnaWaveShadowPanelProvider,
    shadow_policy_from_settings,
)


EVENT_FIELDS = frozenset(
    {
        'event',
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
)
COUNT_FIELDS = EVENT_FIELDS - {'event', 'schema', 'mismatch_fields', 'cohorts', 'elapsed_ms', 'stopped', 'stop_reason'}
MISMATCH_FIELDS = frozenset(
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
COHORTS = frozenset({'active_paid', 'trial', 'limited', 'grace', 'access_point', 'direct_v2'})
STOP_REASONS = frozenset(
    {
        'none',
        'multiple_current_subscriptions',
        'owner_uuid_binding_mismatch',
        'cross_owner_panel_uuid',
        'legacy_shadow_row_invalid',
        'multi_tariff_not_supported',
        'owner_mismatch',
        'panel_contract_error',
        'comparator_instability',
        'rate_limit_violation',
        'cycle_deadline_exceeded',
        'panel_read_error_count',
        'panel_read_error_ratio',
        'panel_missing_count',
        'panel_missing_ratio',
        'critical_access_drift_count',
        'critical_access_drift_ratio',
        'total_drift_count',
        'total_drift_ratio',
        'panel_cycle_open_failed',
    }
)


def _drop_incidental_log(_logger: object, _method_name: str, _event: dict[str, object]) -> dict[str, object]:
    raise structlog.DropEvent


def _silence_incidental_logging() -> None:
    structlog.configure(processors=[_drop_incidental_log], cache_logger_on_first_use=True)


def _bounded_counts(value: object, *, keys: frozenset[str]) -> dict[str, int]:
    if type(value) is not dict or not set(value).issubset(keys):
        raise TypeError('invalid aggregate counter map')
    result: dict[str, int] = {}
    for key, count in value.items():
        if type(key) is not str or type(count) is not int or not 0 <= count <= 100:
            raise TypeError('invalid aggregate counter')
        result[key] = count
    return result


def validate_evidence_event(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != EVENT_FIELDS:
        raise TypeError('invalid evidence fields')
    event = dict(value)
    if event['event'] != 'entitlement_shadow_one_shot_complete':
        raise ValueError('invalid evidence event')
    if event['schema'] != 'entitlement_shadow_metrics_v1':
        raise ValueError('invalid evidence schema')
    for field in COUNT_FIELDS:
        count = event[field]
        if type(count) is not int or not 0 <= count <= 100:
            raise TypeError('invalid evidence count')
    elapsed_ms = event['elapsed_ms']
    if type(elapsed_ms) is not int or not 0 <= elapsed_ms <= 180_000:
        raise TypeError('invalid elapsed time')
    if type(event['stopped']) is not bool:
        raise TypeError('invalid stopped flag')
    stop_reason = event['stop_reason']
    if type(stop_reason) is not str or stop_reason not in STOP_REASONS:
        raise ValueError('invalid stop reason')
    if event['stopped'] != (stop_reason != 'none'):
        raise ValueError('inconsistent stopped state')
    event['mismatch_fields'] = _bounded_counts(event['mismatch_fields'], keys=MISMATCH_FIELDS)
    event['cohorts'] = _bounded_counts(event['cohorts'], keys=COHORTS)
    if event['exact'] + event['drift'] + event['missing'] > event['sampled']:
        raise ValueError('inconsistent sample counts')
    if sum(event['cohorts'].values()) > event['sampled'] * 3:
        raise ValueError('inconsistent cohort counts')
    return event


async def _run_one_cycle() -> dict[str, object]:
    policy = shadow_policy_from_settings()
    runner = ReadOnlyShadowRunner(
        LegacyPostgresShadowSource(AsyncSessionLocal),
        RemnaWaveShadowPanelProvider(),
        policy,
    )
    counters, elapsed = await runner.run_once()
    return validate_evidence_event(
        {
            'event': 'entitlement_shadow_one_shot_complete',
            **counters.aggregate_log_fields(elapsed_seconds=elapsed),
        }
    )


def _validated_stdin() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError('invalid evidence JSON') from error
    return validate_evidence_event(value)


def main() -> int:
    _silence_incidental_logging()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--validate-evidence', action='store_true')
    args = parser.parse_args()
    try:
        evidence = _validated_stdin() if args.validate_evidence else asyncio.run(_run_one_cycle())
    except Exception:
        print('entitlement_shadow_one_shot_failed', file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

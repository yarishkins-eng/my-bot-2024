"""Bounded, rate-limited, aggregate-only production Panel reconciliation scan.

Safety envelope: GET only, one request at a time, 100 users/page, at most 20
pages, 200 ms between pages, abort after any error or a page slower than 10 s.
No identity, UUID, URL, email, Telegram ID, or raw response is emitted.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.config import settings
from app.database.database import AsyncSessionLocal, engine
from app.services.public_location_entitlement_service import ResolvedEntitlement
from app.services.subscription_service import SubscriptionService


PAGE_SIZE = 100
MAX_PAGES = 20
CONCURRENCY = 1
PAGE_DELAY_SECONDS = 0.2
ABORT_PAGE_SECONDS = 10.0


COUNTER_SQL = """
SELECT key, value FROM (
  SELECT 'users' AS key, concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) AS value FROM users
  UNION ALL SELECT 'subscriptions', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM subscriptions
  UNION ALL SELECT 'checkouts', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM subscription_checkouts
  UNION ALL SELECT 'attempts', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM checkout_payment_attempts
  UNION ALL SELECT 'device_outbox', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM device_first_outbox
  UNION ALL SELECT 'notifications', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM device_first_notification_outbox
  UNION ALL SELECT 'provider_events', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM device_first_provider_events
  UNION ALL SELECT 'erasures', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM account_erasure_requests
  UNION ALL SELECT 'terms', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM subscription_entitlement_terms
  UNION ALL SELECT 'traffic_purchases', concat(count(*), '|', coalesce(sum(hashtextextended(id::text||':'||xmin::text,0)),0)) FROM traffic_purchases
) q ORDER BY key
"""


DB_STATE_SQL = """
WITH ranked AS (
  SELECT s.*,
         row_number() OVER (
           PARTITION BY s.user_id
           ORDER BY CASE WHEN s.status IN ('active','trial','limited') THEN 0 ELSE 1 END,
                    s.created_at DESC, s.id DESC
         ) AS rn
  FROM subscriptions s
), current_sub AS (
  SELECT * FROM ranked WHERE rn=1
)
SELECT u.id AS owner_id, u.telegram_id,
       u.account_erasure_requested_at, u.account_erased_at,
       u.remnawave_uuid AS user_uuid, s.remnawave_uuid AS subscription_uuid,
       s.status, s.is_trial, s.end_date, s.in_grace, s.grace_until,
       s.traffic_limit_gb, s.device_limit, s.connected_squads,
       t.external_squad_uuid
FROM users u
LEFT JOIN current_sub s ON s.user_id=u.id
LEFT JOIN tariffs t ON t.id=s.tariff_id
WHERE u.remnawave_uuid IS NOT NULL OR s.remnawave_uuid IS NOT NULL
"""


ALL_REFS_SQL = """
SELECT remnawave_uuid AS uuid, user_id AS owner_id FROM subscriptions WHERE remnawave_uuid IS NOT NULL
UNION ALL
SELECT remnawave_uuid AS uuid, id AS owner_id FROM users WHERE remnawave_uuid IS NOT NULL
"""


DIRECT_READY_EVIDENCE_SQL = """
SELECT c.id AS checkout_id,
       coalesce(u.remnawave_uuid, s.remnawave_uuid) AS uuid,
       c.sale_snapshot, c.financial_committed_at, c.fulfilled_end_at,
       c.funding_mode, c.external_payable_kopeks,
       s.status AS subscription_status, s.is_trial, s.end_date,
       s.traffic_limit_gb, s.device_limit, s.connected_squads,
       a.status AS attempt_status, a.provider_payment_id IS NOT NULL AS attempt_has_provider_id,
       a.requested_amount_kopeks, a.provider_returned_amount_kopeks,
       a.provider_returned_currency, a.platega_payment_id,
       p.is_paid AS payment_is_paid, p.amount_kopeks AS payment_amount_kopeks,
       p.currency AS payment_currency, p.transaction_id IS NOT NULL AS payment_has_ledger,
       p.platega_transaction_id = a.provider_payment_id AS payment_identity_matches,
       o.status AS outbox_status, n.status AS notification_status
FROM subscription_checkouts c
JOIN subscriptions s ON s.id=c.created_subscription_id
JOIN users u ON u.id=s.user_id
LEFT JOIN LATERAL (
  SELECT * FROM checkout_payment_attempts x
  WHERE x.checkout_id=c.id ORDER BY x.id DESC LIMIT 1
) a ON true
LEFT JOIN platega_payments p ON p.id=a.platega_payment_id
LEFT JOIN LATERAL (
  SELECT status FROM device_first_outbox x WHERE x.checkout_id=c.id ORDER BY x.id DESC LIMIT 1
) o ON true
LEFT JOIN LATERAL (
  SELECT status FROM device_first_notification_outbox x
  WHERE x.checkout_id=c.id AND x.notification_type='ready' ORDER BY x.id DESC LIMIT 1
) n ON true
WHERE c.lifecycle_state='ready' AND c.settlement_mode='direct_purchase_v2'
"""


async def counters(db) -> dict[str, str]:
    rows = (await db.execute(text(COUNTER_SQL))).mappings().all()
    return {str(row['key']): str(row['value']) for row in rows}


def squad_ids(panel_user) -> set[str]:
    return {
        str(value)
        for item in (panel_user.active_internal_squads or [])
        for value in [item.get('uuid') if isinstance(item, dict) else None]
        if value
    }


def direct_ready_evidence(row: dict) -> tuple[bool, list[str], dict[str, object]]:
    """Validate immutable commercial evidence without exposing its identifiers."""

    reasons: list[str] = []
    snapshot = dict(row['sale_snapshot'] or {})
    raw_entitlement = snapshot.get('entitlement')
    try:
        if not isinstance(raw_entitlement, dict):
            raise ValueError('missing entitlement')
        entitlement = ResolvedEntitlement(
            tuple(raw_entitlement['location_ids']),
            tuple(raw_entitlement['technical_squad_uuids']),
            int(raw_entitlement['policy_revision']),
            str(raw_entitlement['provenance']),
            (
                str(raw_entitlement['inventory_fingerprint'])
                if raw_entitlement.get('inventory_fingerprint') is not None
                else None
            ),
        )
        if entitlement.snapshot_hash != snapshot.get('entitlement_hash'):
            reasons.append('entitlement_hash_mismatch')
    except (KeyError, TypeError, ValueError):
        entitlement = None
        reasons.append('invalid_entitlement_snapshot')
    if snapshot.get('currency') != 'RUB' or snapshot.get('funding_mode') != row['funding_mode']:
        reasons.append('sale_snapshot_financial_mismatch')
    if int(snapshot.get('tariff_total_kopeks') or 0) != int(row['external_payable_kopeks'] or 0):
        reasons.append('sale_snapshot_amount_mismatch')
    if int(snapshot.get('traffic_limit_gb') or 0) != int(row['traffic_limit_gb'] or 0):
        reasons.append('subscription_traffic_changed_after_sale')
    if int(snapshot.get('device_limit') or 0) != int(row['device_limit'] or 0):
        reasons.append('subscription_device_changed_after_sale')
    if entitlement is None or set(entitlement.squad_uuids) != set(row['connected_squads'] or []):
        reasons.append('subscription_squads_changed_after_sale')
    fulfilled_end = row['fulfilled_end_at']
    subscription_end = row['end_date']
    if fulfilled_end is None or subscription_end is None or abs((fulfilled_end - subscription_end).total_seconds()) > 2:
        reasons.append('subscription_expiry_changed_after_sale')
    if not row['financial_committed_at']:
        reasons.append('financial_commit_missing')
    if not (
        row['attempt_status'] == 'paid_processing'
        and row['attempt_has_provider_id']
        and row['platega_payment_id'] is not None
        and row['payment_is_paid']
        and row['payment_has_ledger']
        and row['payment_identity_matches']
        and int(row['requested_amount_kopeks'] or 0) == int(row['external_payable_kopeks'] or 0)
        and int(row['provider_returned_amount_kopeks'] or 0) == int(row['external_payable_kopeks'] or 0)
        and int(row['payment_amount_kopeks'] or 0) == int(row['external_payable_kopeks'] or 0)
        and row['provider_returned_currency'] == 'RUB'
        and row['payment_currency'] == 'RUB'
    ):
        reasons.append('provider_financial_evidence_incomplete')
    if row['outbox_status'] != 'done' or row['notification_status'] != 'sent':
        reasons.append('delivery_evidence_incomplete')
    desired = {
        'status': row['subscription_status'],
        'is_trial': bool(row['is_trial']),
        'end_date': subscription_end,
        'traffic_limit_gb': int(snapshot.get('traffic_limit_gb') or 0),
        'device_limit': int(snapshot.get('device_limit') or 0),
        'connected_squads': list(entitlement.squad_uuids) if entitlement else [],
    }
    return not reasons, reasons, desired


async def main() -> None:
    if settings.MULTI_TARIFF_ENABLED:
        raise RuntimeError('audit scope assumes the verified single-tariff production mode')
    started = datetime.now(UTC)
    service = SubscriptionService()
    result: dict[str, object] = {
        'scan_started_utc': started.isoformat(),
        'safety_envelope': {
            'method': 'GET-only',
            'page_size': PAGE_SIZE,
            'max_pages': MAX_PAGES,
            'max_identities': PAGE_SIZE * MAX_PAGES,
            'concurrency': CONCURRENCY,
            'delay_seconds': PAGE_DELAY_SECONDS,
            'abort_page_seconds': ABORT_PAGE_SECONDS,
            'abort_errors': 1,
        },
    }

    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        before = await counters(db)
        refs = (await db.execute(text(ALL_REFS_SQL))).mappings().all()
        states = (await db.execute(text(DB_STATE_SQL))).mappings().all()
        direct_rows = (await db.execute(text(DIRECT_READY_EVIDENCE_SQL))).mappings().all()
        await db.rollback()

    direct_evidence_by_uuid: dict[str, dict[str, object]] = {}
    direct_evidence_classes = Counter()
    for raw_row in direct_rows:
        row = dict(raw_row)
        valid, reasons, desired = direct_ready_evidence(row)
        direct_evidence_classes['proven_immutable_current'] += int(valid)
        if not valid:
            for reason in reasons:
                direct_evidence_classes[f'quarantine/{reason}'] += 1
        if row['uuid'] is not None:
            direct_evidence_by_uuid[str(row['uuid'])] = {'valid': valid, 'desired': desired}

    uuid_owners: dict[str, set[int]] = {}
    for row in refs:
        uuid_owners.setdefault(str(row['uuid']), set()).add(int(row['owner_id']))
    state_by_uuid: dict[str, dict] = {}
    for row in states:
        # Production is single-tariff; prefer the User-owned identity and use
        # the subscription reference only for legacy rows not yet backfilled.
        chosen = row['user_uuid']
        if chosen is None:
            chosen = row['subscription_uuid']
        if chosen is not None:
            state_by_uuid[str(chosen)] = dict(row)

    panel_users = []
    page_latencies: list[float] = []
    cursor = None
    completed = False
    metadata_version = 'unknown'
    error = None
    async with service.get_api_client() as api:
        metadata = await api.get_system_metadata()
        metadata_version = str(metadata.get('version', 'unknown'))
        for page_number in range(1, MAX_PAGES + 1):
            tick = time.monotonic()
            try:
                page = await api.get_all_users_page_stream(cursor=cursor, size=PAGE_SIZE, enrich_happ_links=False)
            except Exception as exc:  # aggregate type only; never leak response body
                error = type(exc).__name__
                break
            latency = time.monotonic() - tick
            page_latencies.append(round(latency, 3))
            panel_users.extend(page['users'])
            if latency > ABORT_PAGE_SECONDS:
                error = 'page_latency_abort'
                break
            if not page['hasMore'] or not page['nextCursor']:
                completed = True
                break
            cursor = page['nextCursor']
            await asyncio.sleep(PAGE_DELAY_SECONDS)

    panel_by_uuid = {user.uuid: user for user in panel_users}
    panel_status = Counter(user.status.value for user in panel_users)
    panel_orphan_status = Counter(user.status.value for user in panel_users if user.uuid not in uuid_owners)
    comparisons = Counter()
    missing_classes = Counter()
    drift_by_cohort = Counter()
    drift_fields_by_uuid: dict[str, set[str]] = {}
    now = datetime.now(UTC)
    for uuid, state in state_by_uuid.items():
        panel = panel_by_uuid.get(uuid)
        if panel is None:
            comparisons['db_identity_missing_panel'] += 1
            if state['account_erasure_requested_at'] or state['account_erased_at']:
                missing_classes['account_erasure'] += 1
            else:
                missing_classes[f'subscription_{state["status"] or "none"}'] += 1
            continue
        drift_fields: set[str] = set()
        comparisons['db_identity_found_panel'] += 1
        if state['telegram_id'] is None or panel.telegram_id is None:
            comparisons['owner_match_unknown'] += 1
        elif int(state['telegram_id']) == int(panel.telegram_id):
            comparisons['owner_match'] += 1
        else:
            comparisons['owner_mismatch'] += 1

        end = state['end_date']
        if end is not None and end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        grace_until = state['grace_until']
        if grace_until is not None and grace_until.tzinfo is None:
            grace_until = grace_until.replace(tzinfo=UTC)
        normal_active = state['status'] in ('active', 'trial') and end is not None and end > now
        in_grace = bool(state['in_grace'] and grace_until and grace_until > now)
        desired_active = bool(normal_active or in_grace)
        expected_expiry = (
            end if normal_active else grace_until if in_grace else max(end or now, now + timedelta(minutes=1))
        )
        if desired_active and panel.status.value == 'ACTIVE':
            comparisons['status_exact'] += 1
        elif desired_active and panel.status.value in ('LIMITED', 'DISABLED'):
            comparisons[f'runtime_deny_{panel.status.value.lower()}'] += 1
        elif not desired_active and panel.status.value in ('DISABLED', 'EXPIRED'):
            comparisons['status_exact'] += 1
        else:
            comparisons['status_drift'] += 1
            drift_fields.add('status')

        expected_traffic = int(state['traffic_limit_gb'] or 0) * 1024**3
        if panel.traffic_limit_bytes == expected_traffic:
            comparisons['traffic_exact'] += 1
        else:
            comparisons['traffic_drift'] += 1
            drift_fields.add('traffic')
        expected_device = int(state['device_limit']) if state['device_limit'] else None
        if panel.hwid_device_limit == expected_device:
            comparisons['device_exact'] += 1
        else:
            comparisons['device_drift'] += 1
            drift_fields.add('device')
        expected_squads = {str(item) for item in (state['connected_squads'] or [])}
        if squad_ids(panel) == expected_squads:
            comparisons['squads_exact'] += 1
        else:
            comparisons['squads_drift'] += 1
            drift_fields.add('squads')
        expected_external = state['external_squad_uuid']
        if panel.external_squad_uuid == expected_external:
            comparisons['external_squad_exact'] += 1
        else:
            comparisons['external_squad_drift'] += 1
            drift_fields.add('external_squad')
        if expected_expiry is not None and abs((panel.expire_at - expected_expiry).total_seconds()) <= 2:
            comparisons['expiry_exact'] += 1
        else:
            comparisons['expiry_drift'] += 1
            if desired_active:
                drift_fields.add('expiry')
        if drift_fields:
            drift_by_cohort[f'{state["status"] or "none"}/{"trial" if state["is_trial"] else "nontrial"}'] += 1
            drift_fields_by_uuid[uuid] = drift_fields

    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        after = await counters(db)
        await db.rollback()
    changed_groups = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    direct_ready_with_observed_drift = sum(1 for uuid in direct_evidence_by_uuid if drift_fields_by_uuid.get(uuid))
    direct_ready_with_immutable_drift_but_no_deny_authority = sum(
        1 for uuid, evidence in direct_evidence_by_uuid.items() if evidence['valid'] and drift_fields_by_uuid.get(uuid)
    )

    result.update(
        {
            'panel_version': metadata_version,
            'pages': len(page_latencies),
            'page_latencies_seconds': page_latencies,
            'completed_full_scan': completed,
            'abort_reason': error,
            'panel_identity_count': len(panel_users),
            'panel_status_counts': dict(sorted(panel_status.items())),
            'panel_orphan_count': sum(panel_orphan_status.values()),
            'panel_orphan_status_counts': dict(sorted(panel_orphan_status.items())),
            'db_unique_identity_count': len(uuid_owners),
            'db_cross_owner_identity_count': sum(1 for owners in uuid_owners.values() if len(owners) > 1),
            'comparisons': dict(sorted(comparisons.items())),
            'db_missing_panel_classes': dict(sorted(missing_classes.items())),
            'drift_identity_cohorts': dict(sorted(drift_by_cohort.items())),
            'direct_ready_checkout_count': len(direct_rows),
            'direct_ready_immutable_evidence_classes': dict(sorted(direct_evidence_classes.items())),
            'direct_ready_checkout_with_observed_drift': direct_ready_with_observed_drift,
            # A hash-valid sale still cannot override an unmodelled
            # channel/admin/financial/reversal/erasure deny. This read-only
            # scan never labels access restoration as proven authority.
            'direct_ready_checkout_with_immutable_drift_but_no_deny_authority': (
                direct_ready_with_immutable_drift_but_no_deny_authority
            ),
            'mutation_counters_before': before,
            'mutation_counters_after': after,
            'changed_counter_groups': changed_groups,
            'scan_finished_utc': datetime.now(UTC).isoformat(),
        }
    )
    await engine.dispose()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, default=str))


if __name__ == '__main__':
    asyncio.run(main())

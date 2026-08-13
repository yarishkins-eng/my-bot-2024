"""Owner-only report for observed Direct-ready/Panel contradictions.

Execute read-only inside the bot container and redirect stdout to a chmod 0600
file outside every Git worktree. Never pass its output to reviewer agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import structlog
from sqlalchemy import text


logging.disable(logging.CRITICAL)
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))

from app.database.database import AsyncSessionLocal, engine  # noqa: E402
from app.services.public_location_entitlement_service import ResolvedEntitlement  # noqa: E402
from app.services.subscription_service import SubscriptionService  # noqa: E402


SQL = """
SELECT c.id AS checkout_id, c.user_id, c.created_subscription_id AS subscription_id,
       c.period_days, c.quoted_price_kopeks, c.financial_committed_at,
       c.sale_snapshot, c.fulfilled_end_at, c.funding_mode, c.external_payable_kopeks,
       u.telegram_id,
       nullif(trim(concat_ws(' ',u.first_name,u.last_name)), '') AS display_name,
       u.username,
       coalesce(u.remnawave_uuid,s.remnawave_uuid) AS panel_uuid,
       s.status, s.end_date, s.in_grace, s.grace_until,
       s.traffic_limit_gb, s.device_limit, s.connected_squads,
       t.external_squad_uuid,
       a.status AS attempt_status, a.provider_payment_id IS NOT NULL AS attempt_has_provider_id,
       a.requested_amount_kopeks, a.provider_returned_amount_kopeks,
       a.provider_returned_currency, a.platega_payment_id,
       p.is_paid AS payment_is_paid, p.amount_kopeks AS payment_amount_kopeks,
       p.currency AS payment_currency, p.transaction_id IS NOT NULL AS payment_has_ledger,
       p.platega_transaction_id = a.provider_payment_id AS payment_identity_matches
FROM subscription_checkouts c
JOIN subscriptions s ON s.id=c.created_subscription_id
JOIN users u ON u.id=s.user_id
LEFT JOIN tariffs t ON t.id=s.tariff_id
LEFT JOIN LATERAL (
  SELECT * FROM checkout_payment_attempts x
  WHERE x.checkout_id=c.id ORDER BY x.id DESC LIMIT 1
) a ON true
LEFT JOIN platega_payments p ON p.id=a.platega_payment_id
WHERE c.lifecycle_state='ready' AND c.settlement_mode='direct_purchase_v2'
ORDER BY c.id
"""


def panel_squads(panel_user) -> set[str]:
    return {
        str(item.get('uuid'))
        for item in (panel_user.active_internal_squads or [])
        if isinstance(item, dict) and item.get('uuid')
    }


def immutable_sale_evidence(row: dict) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []
    snapshot = dict(row['sale_snapshot'] or {})
    raw = snapshot.get('entitlement')
    try:
        if not isinstance(raw, dict):
            raise ValueError('missing entitlement')
        entitlement = ResolvedEntitlement(
            tuple(raw['location_ids']),
            tuple(raw['technical_squad_uuids']),
            int(raw['policy_revision']),
            str(raw['provenance']),
            str(raw['inventory_fingerprint']) if raw.get('inventory_fingerprint') is not None else None,
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
    if (
        row['fulfilled_end_at'] is None
        or row['end_date'] is None
        or abs((row['fulfilled_end_at'] - row['end_date']).total_seconds()) > 2
    ):
        reasons.append('subscription_expiry_changed_after_sale')
    if not (
        row['financial_committed_at']
        and row['attempt_status'] == 'paid_processing'
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
    if reasons:
        return None, reasons
    return {
        'period_days': int(snapshot['period_days']),
        'amount_kopeks': int(snapshot['tariff_total_kopeks']),
        'end_date': row['fulfilled_end_at'],
        'traffic_limit_gb': int(snapshot.get('traffic_limit_gb') or 0),
        'device_limit': int(snapshot.get('device_limit') or 0),
        'squads': set(entitlement.squad_uuids),
    }, []


async def main() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        rows = (await db.execute(text(SQL))).mappings().all()
        await db.rollback()

    service = SubscriptionService()
    impacted: list[dict] = []
    async with service.get_api_client() as api:
        for row in rows:
            row = dict(row)
            evidence, evidence_gaps = immutable_sale_evidence(row)
            # Current main has no unified durable deny/reversal overlay. Even
            # a commercially valid sale cannot authorize re-enabling access.
            evidence_gaps.append('durable_deny_reversal_authority_not_available')
            mismatches: list[str] = []
            panel = await api.get_user_by_uuid(str(row['panel_uuid'])) if row['panel_uuid'] else None
            if panel is None:
                mismatches.append('panel_identity_missing')
            else:
                now = datetime.now(UTC)
                end = evidence['end_date'] if evidence is not None else row['end_date']
                grace_until = row['grace_until']
                desired_active = bool(end and end > now)
                if evidence is None:
                    desired_active = bool(
                        (row['status'] in ('active', 'trial') and end and end > now)
                        or (row['in_grace'] and grace_until and grace_until > now)
                    )
                desired_status = 'ACTIVE' if desired_active else 'DISABLED_OR_EXPIRED'
                if desired_active and panel.status.value != 'ACTIVE':
                    mismatches.append(f'status:{panel.status.value}->{desired_status}')
                expected_traffic = (
                    int(evidence['traffic_limit_gb'] if evidence is not None else row['traffic_limit_gb'] or 0)
                    * 1024**3
                )
                if panel.traffic_limit_bytes != expected_traffic:
                    mismatches.append(f'traffic_bytes:{panel.traffic_limit_bytes}->{expected_traffic}')
                source_device = evidence['device_limit'] if evidence is not None else row['device_limit']
                expected_device = int(source_device) if source_device else None
                if panel.hwid_device_limit != expected_device:
                    mismatches.append(f'device_limit:{panel.hwid_device_limit}->{expected_device}')
                expected_squads = {
                    str(value)
                    for value in (evidence['squads'] if evidence is not None else row['connected_squads'] or [])
                }
                if panel_squads(panel) != expected_squads:
                    mismatches.append(f'squad_count:{len(panel_squads(panel))}->{len(expected_squads)}')
                if panel.external_squad_uuid != row['external_squad_uuid']:
                    mismatches.append('external_squad:mismatch')
                    evidence_gaps.append('external_squad_only_mutable_tariff_evidence')
                if desired_active and end and abs((panel.expire_at - end).total_seconds()) > 2:
                    mismatches.append('expiry:mismatch')
            if mismatches:
                impacted.append(
                    {
                        'checkout_id': row['checkout_id'],
                        'user_id': row['user_id'],
                        'subscription_id': row['subscription_id'],
                        'telegram_id': row['telegram_id'],
                        'display_name': row['display_name'] or row['username'] or 'unknown',
                        'paid_period_days': evidence['period_days'] if evidence is not None else row['period_days'],
                        'amount_kopeks': evidence['amount_kopeks']
                        if evidence is not None
                        else row['quoted_price_kopeks'],
                        'classification': 'quarantine',
                        'evidence_gaps': evidence_gaps,
                        'access_mismatches': mismatches,
                        'recommended_manual_action': (
                            'Investigate immutable payment, entitlement and deny/reversal evidence only. '
                            'Do not mutate automatically; this report alone never authorizes re-projection.'
                        ),
                    }
                )
    await engine.dispose()
    print(
        json.dumps(
            {
                'generated_utc': datetime.now(UTC).isoformat(),
                'scope': 'direct_purchase_v2 ready checkouts only',
                'impacted_count': len(impacted),
                'proven_count': sum(1 for item in impacted if item['classification'] == 'proven'),
                'quarantine_count': sum(1 for item in impacted if item['classification'] == 'quarantine'),
                'users': impacted,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == '__main__':
    asyncio.run(main())

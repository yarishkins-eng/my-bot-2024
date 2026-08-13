"""Sanitized, read-only Phase 0 audit executed inside the production bot container.

The script emits aggregates only. It never prints Telegram/user identity, Panel UUID,
subscription URL, provider identifier, payload, credential, or financial description.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import settings
from app.database.database import AsyncSessionLocal, engine


SAFE_FLAGS = (
    'MULTI_TARIFF_ENABLED',
    'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED',
    'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED',
    'DEVICE_FIRST_RECOVERY_WORKER_INTERVAL_SECONDS',
    'REMNAWAVE_AUTO_SYNC_ENABLED',
    'REMNAWAVE_WEBHOOK_ENABLED',
    'CHANNEL_IS_REQUIRED_SUB',
    'TRAFFIC_MONITORING_ENABLED',
    'TRAFFIC_FAST_CHECK_ENABLED',
    'GRACE_ENABLED',
    'RESET_DEVICES_ON_RENEWAL',
    'SALES_MODE',
)


QUERIES: dict[str, str] = {
    'migration_revision': 'SELECT version_num AS value FROM alembic_version',
    'postgres_version': "SELECT current_setting('server_version') AS value",
    'table_counts': """
        SELECT table_name AS key, (xpath('/row/c/text()', query_to_xml(
          format('SELECT count(*) AS c FROM %I', table_name), false, true, '')))[1]::text::bigint AS value
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = ANY(:tables)
        ORDER BY table_name
    """,
    'checkout_states': """
        SELECT concat_ws('/', settlement_mode, lifecycle_state, funding_state,
                         fulfillment_state, provisioning_state) AS key,
               count(*)::bigint AS value
        FROM subscription_checkouts GROUP BY 1 ORDER BY 1
    """,
    'attempt_states': """
        SELECT concat_ws('/', settlement_mode, status) AS key, count(*)::bigint AS value
        FROM checkout_payment_attempts GROUP BY 1 ORDER BY 1
    """,
    'device_outbox_states': """
        SELECT concat_ws('/', settlement_mode, status) AS key, count(*)::bigint AS value
        FROM device_first_outbox GROUP BY 1 ORDER BY 1
    """,
    'deposit_outbox_states': """
        SELECT concat_ws('/', settlement_mode, status, event_status, referral_status,
                         fulfillment_status) AS key, count(*)::bigint AS value
        FROM device_first_deposit_outbox GROUP BY 1 ORDER BY 1
    """,
    'notification_states': """
        SELECT concat_ws('/', notification_type, status) AS key, count(*)::bigint AS value
        FROM device_first_notification_outbox GROUP BY 1 ORDER BY 1
    """,
    'reconciliation_states': """
        SELECT concat_ws('/', status, coalesce(resolution, 'none')) AS key, count(*)::bigint AS value
        FROM device_first_reconciliation_credits GROUP BY 1 ORDER BY 1
    """,
    'erasure_states': """
        SELECT concat_ws('/', state, panel_state) AS key, count(*)::bigint AS value
        FROM account_erasure_requests GROUP BY 1 ORDER BY 1
    """,
    'term_projection_states': """
        SELECT state AS key, count(*)::bigint AS value
        FROM subscription_entitlement_term_projection_outbox GROUP BY 1 ORDER BY 1
    """,
    'subscription_states': """
        SELECT concat_ws('/', status, CASE WHEN is_trial THEN 'trial' ELSE 'nontrial' END) AS key,
               count(*)::bigint AS value
        FROM subscriptions GROUP BY 1 ORDER BY 1
    """,
    'contradictions': """
        SELECT key, value FROM (
          SELECT 'ready_without_bound_subscription' AS key, count(*)::bigint AS value
          FROM subscription_checkouts c
          WHERE (c.lifecycle_state IN ('ready','completed') OR c.fulfillment_state IN ('ready','completed','fulfilled')
                 OR c.provisioning_state IN ('ready','completed'))
            AND c.created_subscription_id IS NULL
          UNION ALL
          SELECT 'ready_bound_missing_uuid_or_url', count(*)::bigint
          FROM subscription_checkouts c JOIN subscriptions s ON s.id=c.created_subscription_id
          WHERE (c.lifecycle_state IN ('ready','completed') OR c.fulfillment_state IN ('ready','completed','fulfilled')
                 OR c.provisioning_state IN ('ready','completed'))
            AND (s.remnawave_uuid IS NULL OR s.subscription_url IS NULL)
          UNION ALL
          SELECT 'ready_notification_for_nonready_checkout', count(*)::bigint
          FROM device_first_notification_outbox n JOIN subscription_checkouts c ON c.id=n.checkout_id
          WHERE n.notification_type='ready' AND n.status IN ('pending','sending','sent')
            AND c.lifecycle_state NOT IN ('ready','completed')
          UNION ALL
          SELECT 'paid_attempt_without_financial_commit', count(*)::bigint
          FROM checkout_payment_attempts a JOIN subscription_checkouts c ON c.id=a.checkout_id
          WHERE a.status IN ('paid','paid_processing','succeeded','completed')
            AND c.financial_committed_at IS NULL
          UNION ALL
          SELECT 'ready_direct_without_outbox', count(*)::bigint
          FROM subscription_checkouts c LEFT JOIN device_first_outbox o ON o.checkout_id=c.id
          WHERE c.lifecycle_state='ready' AND o.id IS NULL
            AND c.settlement_mode='direct_purchase_v2'
          UNION ALL
          SELECT 'ready_direct_outbox_not_done', count(*)::bigint
          FROM subscription_checkouts c JOIN device_first_outbox o ON o.checkout_id=c.id
          WHERE c.lifecycle_state='ready' AND o.status<>'done'
            AND c.settlement_mode='direct_purchase_v2'
          UNION ALL
          SELECT 'ready_direct_attempt_not_paid_processing', count(*)::bigint
          FROM subscription_checkouts c JOIN checkout_payment_attempts a ON a.checkout_id=c.id
          WHERE c.lifecycle_state='ready' AND a.status<>'paid_processing'
            AND c.settlement_mode='direct_purchase_v2'
        ) q ORDER BY key
    """,
    'identity_conflicts': """
        WITH identities AS (
          SELECT remnawave_uuid AS uuid, user_id AS owner FROM subscriptions WHERE remnawave_uuid IS NOT NULL
          UNION ALL
          SELECT remnawave_uuid AS uuid, id AS owner FROM users WHERE remnawave_uuid IS NOT NULL
        )
        SELECT key, value FROM (
          SELECT 'uuid_repeated_rows' AS key, count(*)::bigint AS value
          FROM (SELECT uuid FROM identities GROUP BY uuid HAVING count(*) > 1) d
          UNION ALL
          SELECT 'uuid_cross_owner', count(*)::bigint
          FROM (SELECT uuid FROM identities GROUP BY uuid HAVING count(DISTINCT owner) > 1) d
          UNION ALL
          SELECT 'subscription_uuid_duplicate', count(*)::bigint
          FROM (SELECT remnawave_uuid FROM subscriptions WHERE remnawave_uuid IS NOT NULL
                GROUP BY remnawave_uuid HAVING count(*) > 1) d
        ) q ORDER BY key
    """,
    'cohort_counts': """
        SELECT key, value FROM (
          SELECT 'db_identity_users' AS key, count(*)::bigint AS value FROM users WHERE remnawave_uuid IS NOT NULL
          UNION ALL SELECT 'db_identity_subscriptions', count(*)::bigint FROM subscriptions WHERE remnawave_uuid IS NOT NULL
          UNION ALL SELECT 'active_subscription_missing_effective_uuid', count(*)::bigint
            FROM subscriptions s JOIN users u ON u.id=s.user_id
            WHERE s.status IN ('active','trial','limited')
              AND coalesce(u.remnawave_uuid,s.remnawave_uuid) IS NULL
          UNION ALL SELECT 'active_subscription_missing_url', count(*)::bigint FROM subscriptions
            WHERE status IN ('active','trial','limited') AND subscription_url IS NULL
          UNION ALL SELECT 'expired_traffic_purchase', count(*)::bigint FROM traffic_purchases WHERE expires_at <= now()
          UNION ALL SELECT 'account_erasure_requested', count(*)::bigint FROM users WHERE account_erasure_requested_at IS NOT NULL
          UNION ALL SELECT 'access_point_terms', count(*)::bigint FROM subscription_entitlement_terms
          UNION ALL SELECT 'legacy_snapshot', count(*)::bigint FROM subscriptions WHERE entitlement_provenance='legacy'
          UNION ALL SELECT 'access_point_snapshot', count(*)::bigint FROM subscriptions WHERE entitlement_provenance='access_point'
          UNION ALL SELECT 'operator_review_credit', count(*)::bigint FROM device_first_reconciliation_credits
            WHERE status='operator_review'
        ) q ORDER BY key
    """,
    'mutation_counters': """
        SELECT key, value FROM (
          SELECT 'users' AS key, concat(count(*), '|', coalesce(max(updated_at)::text,'none')) AS value FROM users
          UNION ALL SELECT 'subscriptions', concat(count(*), '|', coalesce(max(updated_at)::text,'none')) FROM subscriptions
          UNION ALL SELECT 'checkouts', concat(count(*), '|', coalesce(max(updated_at)::text,'none')) FROM subscription_checkouts
          UNION ALL SELECT 'attempts', concat(count(*), '|', coalesce(max(updated_at)::text,'none')) FROM checkout_payment_attempts
          UNION ALL SELECT 'device_outbox', concat(count(*), '|', coalesce(max(updated_at)::text,'none')) FROM device_first_outbox
          UNION ALL SELECT 'notifications', concat(count(*), '|', coalesce(max(updated_at)::text,'none')) FROM device_first_notification_outbox
        ) q ORDER BY key
    """,
}


TABLES = [
    'account_erasure_requests',
    'checkout_payment_attempts',
    'device_first_deposit_outbox',
    'device_first_notification_outbox',
    'device_first_outbox',
    'device_first_provider_events',
    'device_first_reconciliation_credits',
    'subscription_checkouts',
    'subscription_entitlement_term_projection_outbox',
    'subscription_entitlement_terms',
    'subscription_entitlement_snapshots',
    'subscriptions',
    'traffic_purchases',
    'users',
]


def normalize(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in row.items()} for row in rows]


async def main() -> None:
    output: dict[str, object] = {
        'audit_started_utc': datetime.now(UTC).isoformat(),
        'flags': {name: getattr(settings, name, None) for name in SAFE_FLAGS},
    }
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        existing = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        'SELECT table_name FROM information_schema.tables '
                        "WHERE table_schema='public' AND table_name = ANY(:tables)"
                    ),
                    {'tables': TABLES},
                )
            ).all()
        }
        output['tables_present'] = sorted(existing)
        for name, sql in QUERIES.items():
            required = {table for table in TABLES if table in sql and table not in existing}
            if required:
                output[name] = {'skipped_missing_tables': sorted(required)}
                continue
            params = {'tables': TABLES} if name == 'table_counts' else {}
            rows = (await db.execute(text(sql), params)).mappings().all()
            output[name] = normalize(list(rows))
        await db.rollback()
    await engine.dispose()
    output['audit_finished_utc'] = datetime.now(UTC).isoformat()
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, default=str))


if __name__ == '__main__':
    asyncio.run(main())

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.security import encrypt_restricted_identifier, hmac_fingerprint


_PURPOSE = 'entitlement-panel-cleanup-v1'


async def request_erasure_cleanup(
    session: AsyncSession,
    *,
    identity_id: int,
    secret: str,
    now: datetime,
) -> str:
    """Atomically sever new authority PII and retain one restricted cleanup target."""

    identity = (
        (
            await session.execute(
                text(
                    'SELECT panel_uuid, generation, lifecycle_state '
                    'FROM entitlement_identities WHERE id=:identity_id FOR UPDATE'
                ),
                {'identity_id': identity_id},
            )
        )
        .mappings()
        .one()
    )
    existing = (
        await session.execute(
            text(
                """
                SELECT operation_id FROM entitlement_cleanup_commands
                 WHERE identity_id=:identity_id
                 ORDER BY id DESC LIMIT 1
                """
            ),
            {'identity_id': identity_id},
        )
    ).scalar_one_or_none()
    if existing:
        return str(existing)
    operation_id = str(uuid.uuid4())
    panel_uuid = str(identity['panel_uuid']) if identity['panel_uuid'] else None
    panel_hmac = hmac_fingerprint(
        panel_uuid or f'no-panel-binding:{operation_id}',
        secret=secret,
        purpose='entitlement-panel-uuid',
    )
    identity_hmac = hmac_fingerprint(str(identity_id), secret=secret, purpose='entitlement-identity')
    erased_owner_key = hmac_fingerprint(operation_id, secret=secret, purpose='entitlement-erased-owner')
    encrypted = (
        encrypt_restricted_identifier(panel_uuid, secret=secret, purpose=_PURPOSE) if panel_uuid is not None else None
    )
    prior_remote_unknown = bool(
        (
            await session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM entitlement_projection_commands
                         WHERE identity_id=:identity_id
                           AND (remote_outcome_unknown OR mutation_sent_at IS NOT NULL)
                           AND stage NOT IN ('ready', 'cancelled')
                    )
                    """
                ),
                {'identity_id': identity_id},
            )
        ).scalar_one()
    )
    generation = int(identity['generation']) + 1
    no_remote_cleanup = panel_uuid is None and not prior_remote_unknown
    lifecycle_state = 'cleanup_terminal' if no_remote_cleanup else 'erasure_requested'
    terminal_at = now if no_remote_cleanup else None
    retention_until = now + timedelta(days=90) if no_remote_cleanup else None
    await session.execute(
        text(
            """
            UPDATE entitlement_identities
               SET user_id = NULL,
                   panel_uuid = NULL,
                   panel_uuid_hmac = :panel_hmac,
                   deterministic_owner_key = :erased_owner_key,
                   generation = :generation,
                   lifecycle_state = :lifecycle_state,
                   quarantine_code = CASE WHEN :prior_remote_unknown
                                          THEN 'prior_projection_outcome_unknown' ELSE NULL END,
                   remote_outcome_unknown_generation = CASE WHEN :prior_remote_unknown
                                                            THEN CAST(:prior_generation AS bigint)
                                                            ELSE NULL::bigint END,
                   erasure_requested_at = :now,
                   cleanup_terminal_at = :terminal_at,
                   updated_at = :now
             WHERE id = :identity_id
            """
        ),
        {
            'identity_id': identity_id,
            'panel_hmac': panel_hmac,
            'erased_owner_key': erased_owner_key,
            'generation': generation,
            'prior_generation': identity['generation'],
            'prior_remote_unknown': prior_remote_unknown,
            'lifecycle_state': lifecycle_state,
            'terminal_at': terminal_at,
            'now': now,
        },
    )
    await session.execute(
        text(
            """
            UPDATE entitlement_projection_commands
               SET source_revision_id=NULL,
                   idempotency_key='erased:' || :operation_id || ':' || id::text,
                   stage=CASE WHEN remote_outcome_unknown OR mutation_sent_at IS NOT NULL
                              THEN 'remote_outcome_unknown' ELSE 'cancelled' END,
                   remote_outcome_unknown=(remote_outcome_unknown OR mutation_sent_at IS NOT NULL),
                   lease_owner=NULL, lease_expires_at=NULL,
                   last_error_code=CASE WHEN remote_outcome_unknown OR mutation_sent_at IS NOT NULL
                                        THEN 'erasure_prior_outcome_unknown' ELSE 'erasure_requested' END,
                   updated_at=:now
             WHERE identity_id=:identity_id
            """
        ),
        {'identity_id': identity_id, 'operation_id': operation_id, 'now': now},
    )
    await session.execute(
        text('DELETE FROM entitlement_notification_intents WHERE identity_id=:identity_id'),
        {'identity_id': identity_id},
    )
    await session.execute(
        text('DELETE FROM entitlement_observations WHERE identity_id=:identity_id'),
        {'identity_id': identity_id},
    )
    await session.execute(
        text('UPDATE entitlement_webhook_inbox SET identity_id=NULL WHERE identity_id=:identity_id'),
        {'identity_id': identity_id},
    )
    await session.execute(
        text('DELETE FROM entitlement_overlays WHERE identity_id=:identity_id'),
        {'identity_id': identity_id},
    )
    await session.execute(
        text('DELETE FROM entitlement_source_revisions WHERE identity_id=:identity_id'),
        {'identity_id': identity_id},
    )
    cleanup_state = (
        'quarantined' if prior_remote_unknown else ('cleanup_terminal' if no_remote_cleanup else 'erasure_requested')
    )
    error_code = 'prior_projection_outcome_unknown' if prior_remote_unknown else None
    await session.execute(
        text(
            """
            INSERT INTO entitlement_cleanup_commands
                   (operation_id, identity_id, generation, state, encrypted_panel_uuid,
                    panel_uuid_hmac, identity_hmac, remote_outcome_unknown, last_error_code,
                    requested_at, alert_after, operator_alerted_at, terminal_at,
                    retention_until, created_at, updated_at)
            VALUES (:operation_id, :identity_id, :generation, :state, :encrypted,
                    :panel_hmac, :identity_hmac, :prior_remote_unknown, :error_code,
                    :now, :alert_after, NULL, :terminal_at, :retention_until, :now, :now)
            """
        ),
        {
            'operation_id': operation_id,
            'identity_id': identity_id,
            'generation': generation,
            'encrypted': encrypted,
            'panel_hmac': panel_hmac,
            'identity_hmac': identity_hmac,
            'state': cleanup_state,
            'prior_remote_unknown': prior_remote_unknown,
            'error_code': error_code,
            'now': now,
            'alert_after': now + timedelta(days=30),
            'terminal_at': terminal_at,
            'retention_until': retention_until,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO entitlement_cleanup_tombstones
                   (operation_id, identity_hmac, panel_uuid_hmac, state,
                    last_error_code, requested_at, terminal_at, retention_until, created_at)
            VALUES (:operation_id, :identity_hmac, :panel_hmac, :state,
                    :error_code, :now, :terminal_at, :retention_until, :now)
            """
        ),
        {
            'operation_id': operation_id,
            'identity_hmac': identity_hmac,
            'panel_hmac': panel_hmac,
            'state': cleanup_state,
            'error_code': error_code,
            'now': now,
            'terminal_at': terminal_at,
            'retention_until': retention_until,
        },
    )
    return operation_id


async def mark_cleanup_terminal(
    session: AsyncSession,
    *,
    operation_id: str,
    verified_absent: bool,
    now: datetime,
) -> None:
    if not verified_absent:
        raise ValueError('cleanup terminal requires verified DELETE or canonical 404')
    cleanup = (
        (
            await session.execute(
                text(
                    """
                    SELECT c.identity_id, c.remote_outcome_unknown, c.state,
                           i.lifecycle_state
                      FROM entitlement_cleanup_commands c
                      JOIN entitlement_identities i ON i.id=c.identity_id
                     WHERE c.operation_id=:operation_id
                     FOR UPDATE OF c, i
                    """
                ),
                {'operation_id': operation_id},
            )
        )
        .mappings()
        .one()
    )
    if cleanup['state'] == 'cleanup_terminal':
        if cleanup['lifecycle_state'] not in {'cleanup_terminal', 'final_erasure'}:
            raise ValueError('cleanup terminal durable state mismatch')
        return
    if cleanup['lifecycle_state'] == 'final_erasure':
        raise ValueError('final erasure cannot regress to cleanup terminal')
    if cleanup['remote_outcome_unknown']:
        raise ValueError('cleanup terminal requires contract-safe resolution of prior remote outcome')
    retention_until = now + timedelta(days=90)
    await session.execute(
        text(
            """
            UPDATE entitlement_cleanup_commands
               SET state = 'cleanup_terminal', encrypted_panel_uuid = NULL,
                   terminal_at = :now, retention_until = :retention, updated_at = :now
             WHERE operation_id = :operation_id
            """
        ),
        {'operation_id': operation_id, 'now': now, 'retention': retention_until},
    )
    await session.execute(
        text(
            """
            UPDATE entitlement_identities
               SET lifecycle_state = 'cleanup_terminal', cleanup_terminal_at = :now, updated_at = :now
             WHERE id = :identity_id
            """
        ),
        {'identity_id': cleanup['identity_id'], 'now': now},
    )
    await session.execute(
        text(
            """
            UPDATE entitlement_cleanup_tombstones
               SET state = 'cleanup_terminal', terminal_at = :now, retention_until = :retention
             WHERE operation_id = :operation_id
            """
        ),
        {'operation_id': operation_id, 'now': now, 'retention': retention_until},
    )


async def mark_overdue_cleanup_alerts(session: AsyncSession, *, now: datetime) -> tuple[str, ...]:
    """Durably mark one operator alert per overdue unresolved cleanup."""

    rows = await session.execute(
        text(
            """
            UPDATE entitlement_cleanup_commands
               SET operator_alerted_at=:now,
                   last_error_code=COALESCE(last_error_code, 'cleanup_overdue'),
                   updated_at=:now
             WHERE state IN ('erasure_requested', 'quarantined')
               AND alert_after <= :now
               AND operator_alerted_at IS NULL
         RETURNING operation_id
            """
        ),
        {'now': now},
    )
    operation_ids = tuple(str(value) for value in rows.scalars().all())
    if operation_ids:
        await session.execute(
            text(
                """
                UPDATE entitlement_cleanup_tombstones t
                   SET last_error_code='cleanup_overdue'
                  FROM entitlement_cleanup_commands c
                 WHERE t.operation_id=c.operation_id
                   AND c.operator_alerted_at=:now
                """
            ),
            {'now': now},
        )
    return operation_ids


async def mark_final_erasure(session: AsyncSession, *, identity_id: int, now: datetime) -> None:
    """Complete the second erasure stage only after cleanup is terminal."""

    updated = await session.execute(
        text(
            """
            UPDATE entitlement_identities
               SET lifecycle_state='final_erasure', updated_at=:now
             WHERE id=:identity_id AND lifecycle_state='cleanup_terminal'
               AND user_id IS NULL AND panel_uuid IS NULL
            """
        ),
        {'identity_id': identity_id, 'now': now},
    )
    if updated.rowcount != 1:
        raise ValueError('final erasure requires cleanup_terminal with cleared identity links')


async def housekeep_terminal_evidence(session: AsyncSession, *, now: datetime) -> dict[str, int]:
    """Delete only expired terminal evidence; unresolved cleanup is indefinite."""

    observations = await session.execute(
        text('DELETE FROM entitlement_observations WHERE retention_until < :now RETURNING id'), {'now': now}
    )
    webhooks = await session.execute(
        text('DELETE FROM entitlement_webhook_inbox WHERE retention_until < :now RETURNING id'), {'now': now}
    )
    commands = await session.execute(
        text(
            """
            DELETE FROM entitlement_cleanup_commands
             WHERE state = 'cleanup_terminal'
               AND encrypted_panel_uuid IS NULL
               AND retention_until < :now
         RETURNING id
            """
        ),
        {'now': now},
    )
    tombstones = await session.execute(
        text(
            """
            DELETE FROM entitlement_cleanup_tombstones
             WHERE state = 'cleanup_terminal' AND retention_until < :now
         RETURNING id
            """
        ),
        {'now': now},
    )
    return {
        'observations': len(observations.scalars().all()),
        'webhooks': len(webhooks.scalars().all()),
        'cleanup_commands': len(commands.scalars().all()),
        'tombstones': len(tombstones.scalars().all()),
    }

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.entitlement_authority.persistence import (
    CommandClaim,
    PostgresEntitlementStore,
    ingest_webhook_observation,
)
from app.services.entitlement_authority.privacy import (
    housekeep_terminal_evidence,
    mark_cleanup_terminal,
    mark_final_erasure,
    mark_overdue_cleanup_alerts,
    request_erasure_cleanup,
)
from app.services.entitlement_authority.types import EntitlementSnapshot, compare_snapshots
from app.utils.security import decrypt_restricted_identifier


DATABASE_URL = os.environ.get('ENTITLEMENT_AUTHORITY_APP_DATABASE_URL')
if DATABASE_URL is None:
    pytest.skip('isolated entitlement authority PostgreSQL is not configured', allow_module_level=True)
NOW = datetime(2026, 8, 13, tzinfo=UTC)
SECRET = 'test-only-existing-application-secret'
RAW_UUID = '11111111-2222-3333-4444-555555555555'


@pytest_asyncio.fixture
async def sessions() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_size=3, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_foundation(sessions: async_sessionmaker[AsyncSession]):
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                TRUNCATE entitlement_cleanup_tombstones, entitlement_cleanup_commands,
                         entitlement_notification_intents, entitlement_webhook_inbox,
                         entitlement_observations, entitlement_projection_commands,
                         entitlement_overlays, entitlement_source_revisions,
                         entitlement_identities RESTART IDENTITY CASCADE
                """
            )
        )


async def create_identity(
    sessions: async_sessionmaker[AsyncSession], *, panel_uuid: str | None = RAW_UUID
) -> tuple[int, int]:
    async with sessions() as session, session.begin():
        user_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO users
                           (auth_type, has_had_paid_subscription, email_verified,
                            auto_promo_group_assigned, auto_promo_group_threshold_kopeks,
                            promo_offer_discount_percent, has_made_first_topup,
                            restriction_topup, restriction_subscription, partner_status)
                    VALUES ('test', false, false, false, 0, 0, false, false, false, 'none')
                    RETURNING id
                    """
                )
            )
        ).scalar_one()
        identity_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_identities
                           (operation_id, user_id, deterministic_owner_key, panel_uuid,
                            generation, lifecycle_state)
                    VALUES (:operation_id, :user_id, :owner, :panel_uuid, 1, 'ready')
                    RETURNING id
                    """
                ),
                {
                    'operation_id': str(uuid.uuid4()),
                    'user_id': user_id,
                    'owner': f'owner-{user_id}',
                    'panel_uuid': panel_uuid,
                },
            )
        ).scalar_one()
        snapshot = EntitlementSnapshot(
            owner_key=f'owner-{user_id}',
            panel_uuid=panel_uuid,
            status='ACTIVE',
            expire_at=NOW + timedelta(days=30),
            traffic_limit_bytes=0,
            traffic_limit_strategy='NO_RESET',
            hwid_device_limit=None,
            internal_squads=(),
            external_squad_uuid=None,
            provenance='test',
            generation=1,
        )
        desired_json = json.dumps(snapshot.canonical(), sort_keys=True)
        source_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_source_revisions
                           (identity_id, generation, source_type, source_key, source_fingerprint,
                            provenance, desired_snapshot, desired_hash)
                    VALUES (:identity_id, 1, 'test', :source_key, :fingerprint,
                            'test', CAST(:snapshot AS jsonb), :desired_hash)
                    RETURNING id
                    """
                ),
                {
                    'identity_id': identity_id,
                    'source_key': f'erasure-source-{identity_id}',
                    'fingerprint': hashlib.sha256(str(identity_id).encode()).hexdigest(),
                    'snapshot': desired_json,
                    'desired_hash': snapshot.desired_hash,
                },
            )
        ).scalar_one()
        await session.execute(
            text(
                """
                INSERT INTO entitlement_projection_commands
                       (operation_id, identity_id, source_revision_id, generation, command_type,
                        idempotency_key, desired_hash, stage)
                VALUES (:operation_id, :identity_id, :source_id, 1, 'project',
                        :idempotency_key, :desired_hash, 'pending')
                """
            ),
            {
                'operation_id': str(uuid.uuid4()),
                'identity_id': identity_id,
                'source_id': source_id,
                'idempotency_key': f'erasure-command-{identity_id}',
                'desired_hash': snapshot.desired_hash,
            },
        )
    return user_id, identity_id


def identity_snapshot(user_id: int, *, panel_uuid: str | None = RAW_UUID) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        owner_key=f'owner-{user_id}',
        panel_uuid=panel_uuid,
        status='ACTIVE',
        expire_at=NOW + timedelta(days=30),
        traffic_limit_bytes=0,
        traffic_limit_strategy='NO_RESET',
        hwid_device_limit=None,
        internal_squads=(),
        external_squad_uuid=None,
        provenance='test',
        generation=1,
    )


async def write_identity_evidence(
    sessions: async_sessionmaker[AsyncSession],
    *,
    kind: str,
    user_id: int,
    identity_id: int,
) -> None:
    if kind == 'webhook':
        async with sessions() as session, session.begin():
            assert await ingest_webhook_observation(
                session,
                identity_id=identity_id,
                event_id_hash='a' * 64,
                event_type='user.updated',
                normalized_hash='b' * 64,
                event_timestamp=NOW,
                now=NOW,
            )
        return
    snapshot = identity_snapshot(user_id)
    async with sessions() as session:
        command_id = (
            await session.execute(
                text('SELECT id FROM entitlement_projection_commands WHERE identity_id=:identity_id AND generation=1'),
                {'identity_id': identity_id},
            )
        ).scalar_one()
    claim = CommandClaim(
        command_id=command_id,
        identity_id=identity_id,
        generation=1,
        lease_epoch=0,
        mode='work',
        panel_uuid=RAW_UUID,
        deterministic_create_key=None,
        desired_snapshot=snapshot,
    )
    await PostgresEntitlementStore(sessions).record_observation(
        claim,
        snapshot,
        compare_snapshots(snapshot, snapshot),
        event_type='concurrent_erasure_probe',
        now=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['webhook', 'observation'])
@pytest.mark.parametrize('erasure_locks_first', [False, True])
async def test_evidence_linking_serializes_with_erasure_in_both_lock_orders(
    sessions: async_sessionmaker[AsyncSession],
    kind: str,
    erasure_locks_first: bool,
) -> None:
    user_id, identity_id = await create_identity(sessions)

    async def erase() -> None:
        async with sessions() as session, session.begin():
            await request_erasure_cleanup(
                session,
                identity_id=identity_id,
                secret=SECRET,
                now=NOW + timedelta(seconds=1),
            )

    if erasure_locks_first:
        eraser = sessions()
        transaction = await eraser.begin()
        await request_erasure_cleanup(
            eraser,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW + timedelta(seconds=1),
        )
        evidence_task = asyncio.create_task(
            write_identity_evidence(sessions, kind=kind, user_id=user_id, identity_id=identity_id)
        )
        await asyncio.sleep(0.05)
        assert not evidence_task.done()
        await transaction.commit()
        await eraser.close()
        await asyncio.wait_for(evidence_task, timeout=2)
    else:
        blocked_table = 'entitlement_webhook_inbox' if kind == 'webhook' else 'entitlement_observations'
        blocker = sessions()
        blocker_transaction = await blocker.begin()
        await blocker.execute(text(f'LOCK TABLE {blocked_table} IN ACCESS EXCLUSIVE MODE'))
        evidence_task = asyncio.create_task(
            write_identity_evidence(sessions, kind=kind, user_id=user_id, identity_id=identity_id)
        )
        await asyncio.sleep(0.05)
        assert not evidence_task.done()
        erasure_task = asyncio.create_task(erase())
        await asyncio.sleep(0.05)
        assert not erasure_task.done()
        await blocker_transaction.commit()
        await blocker.close()
        await asyncio.wait_for(evidence_task, timeout=2)
        await asyncio.wait_for(erasure_task, timeout=2)

    async with sessions() as session:
        identity = (
            await session.execute(
                text('SELECT lifecycle_state, erasure_requested_at FROM entitlement_identities WHERE id=:identity_id'),
                {'identity_id': identity_id},
            )
        ).one()
        assert identity.lifecycle_state == 'erasure_requested'
        assert identity.erasure_requested_at is not None
        assert (
            await session.execute(
                text('SELECT count(*) FROM entitlement_observations WHERE identity_id=:identity_id'),
                {'identity_id': identity_id},
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text('SELECT count(*) FROM entitlement_webhook_inbox WHERE identity_id=:identity_id'),
                {'identity_id': identity_id},
            )
        ).scalar_one() == 0
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
async def test_erasure_without_panel_binding_severs_links_and_is_terminal_without_remote_cleanup(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, identity_id = await create_identity(sessions, panel_uuid=None)
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
    async with sessions() as session:
        identity = (
            await session.execute(
                text('SELECT user_id, panel_uuid, lifecycle_state FROM entitlement_identities WHERE id=:id'),
                {'id': identity_id},
            )
        ).one()
        cleanup = (
            await session.execute(
                text(
                    'SELECT state, encrypted_panel_uuid, remote_outcome_unknown, retention_until '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()
        assert identity == (None, None, 'cleanup_terminal')
        assert cleanup.state == 'cleanup_terminal'
        assert cleanup.encrypted_panel_uuid is None
        assert not cleanup.remote_outcome_unknown
        assert cleanup.retention_until == NOW + timedelta(days=90)
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 0
        assert (await session.execute(text('SELECT count(*) FROM entitlement_observations'))).scalar_one() == 0
    async with sessions() as session, session.begin():
        await mark_final_erasure(session, identity_id=identity_id, now=NOW + timedelta(seconds=1))
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
async def test_erasure_two_stage_encryption_terminal_clear_and_ninety_day_retention(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, identity_id = await create_identity(sessions)
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
        assert (
            await request_erasure_cleanup(
                session,
                identity_id=identity_id,
                secret=SECRET,
                now=NOW + timedelta(seconds=1),
            )
            == operation_id
        )
    async with sessions() as session:
        identity = (
            await session.execute(
                text('SELECT user_id, panel_uuid, lifecycle_state FROM entitlement_identities WHERE id=:id'),
                {'id': identity_id},
            )
        ).one()
        cleanup = (
            await session.execute(
                text(
                    """
                    SELECT encrypted_panel_uuid, state, alert_after, retention_until
                      FROM entitlement_cleanup_commands WHERE operation_id=:operation_id
                    """
                ),
                {'operation_id': operation_id},
            )
        ).one()
        assert identity == (None, None, 'erasure_requested')
        assert cleanup.state == 'erasure_requested'
        assert cleanup.alert_after == NOW + timedelta(days=30)
        assert cleanup.retention_until is None
        assert RAW_UUID.encode() not in cleanup.encrypted_panel_uuid
        assert (
            decrypt_restricted_identifier(
                cleanup.encrypted_panel_uuid,
                secret=SECRET,
                purpose='entitlement-panel-cleanup-v1',
            )
            == RAW_UUID
        )
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 0
        assert (await session.execute(text('SELECT count(*) FROM entitlement_observations'))).scalar_one() == 0
        command = (
            await session.execute(
                text(
                    'SELECT source_revision_id, stage, idempotency_key '
                    'FROM entitlement_projection_commands WHERE identity_id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
        assert command.source_revision_id is None and command.stage == 'cancelled'
        assert command.idempotency_key.startswith('erased:')
    async with sessions() as session, session.begin():
        counts = await housekeep_terminal_evidence(session, now=NOW + timedelta(days=365))
        assert counts['cleanup_commands'] == 0 and counts['tombstones'] == 0
    async with sessions() as session, session.begin():
        await mark_cleanup_terminal(
            session, operation_id=operation_id, verified_absent=True, now=NOW + timedelta(days=1)
        )
        await mark_final_erasure(session, identity_id=identity_id, now=NOW + timedelta(days=1))
    async with sessions() as session:
        cleanup = (
            await session.execute(
                text('SELECT encrypted_panel_uuid, state, retention_until FROM entitlement_cleanup_commands')
            )
        ).one()
        assert cleanup.encrypted_panel_uuid is None
        assert cleanup.state == 'cleanup_terminal'
        assert cleanup.retention_until == NOW + timedelta(days=91)
        state = (
            await session.execute(
                text('SELECT lifecycle_state FROM entitlement_identities WHERE id=:id'), {'id': identity_id}
            )
        ).scalar_one()
        assert state == 'final_erasure'
        original_retention = cleanup.retention_until
    async with sessions() as session, session.begin():
        await mark_cleanup_terminal(
            session,
            operation_id=operation_id,
            verified_absent=True,
            now=NOW + timedelta(days=30),
        )
    async with sessions() as session:
        repeated = (
            await session.execute(
                text(
                    """
                    SELECT c.retention_until, i.lifecycle_state
                      FROM entitlement_cleanup_commands c
                      JOIN entitlement_identities i ON i.id=c.identity_id
                     WHERE c.operation_id=:operation_id
                    """
                ),
                {'operation_id': operation_id},
            )
        ).one()
        assert repeated.retention_until == original_retention
        assert repeated.lifecycle_state == 'final_erasure'
    async with sessions() as session, session.begin():
        before = await housekeep_terminal_evidence(session, now=NOW + timedelta(days=90))
        assert before['cleanup_commands'] == 0 and before['tombstones'] == 0
    async with sessions() as session, session.begin():
        after = await housekeep_terminal_evidence(session, now=NOW + timedelta(days=92))
        assert after['cleanup_commands'] == 1 and after['tombstones'] == 1
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['panel_ciphertext', 'create_locator', 'tombstone_state'])
async def test_repeated_terminal_cleanup_rejects_corrupt_durable_state(
    sessions: async_sessionmaker[AsyncSession],
    corruption: str,
) -> None:
    user_id, identity_id = await create_identity(sessions, panel_uuid=None)
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
        if corruption == 'panel_ciphertext':
            await session.execute(
                text(
                    'UPDATE entitlement_cleanup_commands SET encrypted_panel_uuid=:value '
                    'WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id, 'value': b'corrupt-panel'},
            )
        elif corruption == 'create_locator':
            await session.execute(
                text(
                    'UPDATE entitlement_cleanup_commands SET encrypted_create_locator=:value '
                    'WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id, 'value': b'corrupt-locator'},
            )
        else:
            await session.execute(
                text("UPDATE entitlement_cleanup_tombstones SET state='quarantined' WHERE operation_id=:operation_id"),
                {'operation_id': operation_id},
            )
    async with sessions() as session, session.begin():
        with pytest.raises(ValueError, match='cleanup terminal durable state mismatch'):
            await mark_cleanup_terminal(
                session,
                operation_id=operation_id,
                verified_absent=True,
                now=NOW + timedelta(seconds=1),
            )
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
@pytest.mark.parametrize('finalize', [False, True])
async def test_erasure_cannot_restart_after_terminal_evidence_retention(
    sessions: async_sessionmaker[AsyncSession],
    finalize: bool,
) -> None:
    user_id, identity_id = await create_identity(sessions, panel_uuid=None)
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
        if finalize:
            await mark_final_erasure(session, identity_id=identity_id, now=NOW + timedelta(seconds=1))
    async with sessions() as session, session.begin():
        counts = await housekeep_terminal_evidence(session, now=NOW + timedelta(days=91))
        assert counts['cleanup_commands'] == 1 and counts['tombstones'] == 1
    async with sessions() as session, session.begin():
        with pytest.raises(ValueError, match='marker exists without retained cleanup'):
            await request_erasure_cleanup(
                session,
                identity_id=identity_id,
                secret=SECRET,
                now=NOW + timedelta(days=92),
            )
    async with sessions() as session:
        identity = (
            await session.execute(
                text('SELECT generation, lifecycle_state FROM entitlement_identities WHERE id=:identity_id'),
                {'identity_id': identity_id},
            )
        ).one()
        assert identity == (2, 'final_erasure' if finalize else 'cleanup_terminal')
        assert (
            await session.execute(
                text('SELECT count(*) FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'),
                {'operation_id': operation_id},
            )
        ).scalar_one() == 0
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
async def test_cleanup_terminal_requires_verified_delete_or_canonical_404(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, identity_id = await create_identity(sessions)
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
    async with sessions() as session, session.begin():
        with pytest.raises(ValueError, match='verified DELETE'):
            await mark_cleanup_terminal(session, operation_id=operation_id, verified_absent=False, now=NOW)
    async with sessions() as session:
        encrypted = (
            await session.execute(
                text('SELECT encrypted_panel_uuid FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'),
                {'operation_id': operation_id},
            )
        ).scalar_one()
        assert encrypted is not None
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})


@pytest.mark.asyncio
async def test_erasure_keeps_unknown_cleanup_target_and_raises_one_overdue_alert(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, identity_id = await create_identity(sessions)
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                UPDATE entitlement_projection_commands
                   SET stage='mutating', mutation_sent_at=:now
                 WHERE identity_id=:identity_id
                """
            ),
            {'identity_id': identity_id, 'now': NOW},
        )
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW,
        )
    async with sessions() as session, session.begin():
        with pytest.raises(ValueError, match='contract-safe resolution'):
            await mark_cleanup_terminal(
                session,
                operation_id=operation_id,
                verified_absent=True,
                now=NOW + timedelta(days=31),
            )
    async with sessions() as session, session.begin():
        first = await mark_overdue_cleanup_alerts(session, now=NOW + timedelta(days=31))
        second = await mark_overdue_cleanup_alerts(session, now=NOW + timedelta(days=32))
        counts = await housekeep_terminal_evidence(session, now=NOW + timedelta(days=365))
        assert first == (operation_id,) and second == ()
        assert counts['cleanup_commands'] == 0 and counts['tombstones'] == 0
    async with sessions() as session:
        cleanup = (
            await session.execute(
                text(
                    'SELECT state, remote_outcome_unknown, encrypted_panel_uuid, operator_alerted_at '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()
        assert cleanup.state == 'quarantined' and cleanup.remote_outcome_unknown
        assert cleanup.encrypted_panel_uuid is not None
        assert cleanup.operator_alerted_at == NOW + timedelta(days=31)
    async with sessions() as session, session.begin():
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})

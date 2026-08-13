from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .state_machine import Stage, claim_transition, finalize_transition
from .types import EntitlementSnapshot, SnapshotComparison


async def _lock_identity_for_evidence(
    session: AsyncSession,
    *,
    identity_id: int,
    generation: int | None = None,
) -> bool:
    """Serialize evidence linking with erasure and reject terminal identities."""

    identity = (
        (
            await session.execute(
                text(
                    """
                    SELECT i.generation, i.lifecycle_state, i.erasure_requested_at,
                           i.cleanup_terminal_at,
                           EXISTS (
                               SELECT 1 FROM entitlement_cleanup_commands c
                                WHERE c.identity_id=i.id
                           ) AS has_cleanup_command
                      FROM entitlement_identities i
                     WHERE i.id=:identity_id
                     FOR UPDATE
                    """
                ),
                {'identity_id': identity_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return bool(
        identity is not None
        and (generation is None or identity['generation'] == generation)
        and identity['lifecycle_state'] not in {'erasure_requested', 'cleanup_terminal', 'final_erasure'}
        and identity['erasure_requested_at'] is None
        and identity['cleanup_terminal_at'] is None
        and not identity['has_cleanup_command']
    )


@dataclass(frozen=True, slots=True)
class CommandClaim:
    command_id: int
    identity_id: int
    generation: int
    lease_epoch: int
    mode: str
    panel_uuid: str | None
    deterministic_create_key: str | None
    reason: str | None = None
    desired_snapshot: EntitlementSnapshot | None = None


def _durable_desired_snapshot(
    *,
    identity: Mapping[str, object],
    command: Mapping[str, object],
    source: Mapping[str, object] | None,
) -> EntitlementSnapshot:
    """Validate and return the immutable source bound to current identity state."""

    if source is None:
        raise ValueError('durable_source_missing')
    if (
        source['identity_id'] != identity['id']
        or source['generation'] != command['generation']
        or source['authority_state'] != 'authorized'
    ):
        raise ValueError('durable_source_fence_mismatch')
    raw_snapshot = source['desired_snapshot']
    if not isinstance(raw_snapshot, Mapping):
        raise ValueError('durable_source_snapshot_invalid')
    snapshot = EntitlementSnapshot.from_mapping(raw_snapshot)
    if (
        snapshot.generation != source['generation']
        or snapshot.provenance != source['provenance']
        or snapshot.desired_hash != source['desired_hash']
        or snapshot.owner_key != identity['deterministic_owner_key']
    ):
        raise ValueError('durable_source_hash_mismatch')
    panel_uuid = identity['panel_uuid']
    if panel_uuid is None:
        if snapshot.panel_uuid is not None:
            raise ValueError('durable_source_identity_binding_mismatch')
        expected = snapshot
    else:
        panel_uuid = str(panel_uuid)
        if snapshot.panel_uuid not in {None, panel_uuid}:
            raise ValueError('durable_source_identity_binding_mismatch')
        expected = snapshot if snapshot.panel_uuid == panel_uuid else snapshot.bind(panel_uuid)
    if command['desired_hash'] != expected.desired_hash:
        raise ValueError('durable_command_hash_mismatch')
    return expected


class PostgresEntitlementStore:
    """Small transaction boundary for the dormant coordinator.

    Every method opens and commits its own short transaction.  No caller can
    carry an ordinary PostgreSQL row lock across Panel HTTP.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim_entitlement_command(
        self,
        command_id: int,
        *,
        worker: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> CommandClaim:
        async with self._sessions() as session, session.begin():
            identity_id = (
                await session.execute(
                    text('SELECT identity_id FROM entitlement_projection_commands WHERE id = :id'),
                    {'id': command_id},
                )
            ).scalar_one()
            identity = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_identities WHERE id = :id FOR UPDATE'),
                        {'id': identity_id},
                    )
                )
                .mappings()
                .one()
            )
            command = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_projection_commands WHERE id = :id FOR UPDATE'),
                        {'id': command_id},
                    )
                )
                .mappings()
                .one()
            )
            source = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_source_revisions WHERE id=:id'),
                        {'id': command['source_revision_id']},
                    )
                )
                .mappings()
                .one_or_none()
            )
            try:
                desired_snapshot = _durable_desired_snapshot(
                    identity=identity,
                    command=command,
                    source=source,
                )
            except (KeyError, TypeError, ValueError) as exc:
                reason = str(exc) or 'durable_source_invalid'
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_identities
                           SET lifecycle_state='quarantined', quarantine_code=:reason, updated_at=:now
                         WHERE id=:identity_id
                           AND lifecycle_state NOT IN
                               ('erasure_requested', 'cleanup_terminal', 'final_erasure')
                        """
                    ),
                    {'identity_id': identity_id, 'reason': reason, 'now': now},
                )
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_projection_commands
                           SET stage='quarantined', last_error_code=:reason,
                               lease_owner=NULL, lease_expires_at=NULL, updated_at=:now
                         WHERE id=:id
                        """
                    ),
                    {'id': command_id, 'reason': reason, 'now': now},
                )
                return CommandClaim(
                    command_id=command_id,
                    identity_id=identity_id,
                    generation=command['generation'],
                    lease_epoch=command['lease_epoch'],
                    mode='invalid',
                    panel_uuid=identity['panel_uuid'],
                    deterministic_create_key=command['deterministic_create_key'],
                    reason=reason,
                )
            if (
                command['lease_owner']
                and command['lease_owner'] != worker
                and command['lease_expires_at']
                and command['lease_expires_at'] > now
            ):
                return CommandClaim(
                    command_id=command_id,
                    identity_id=identity_id,
                    generation=command['generation'],
                    lease_epoch=command['lease_epoch'],
                    mode='busy',
                    panel_uuid=identity['panel_uuid'],
                    deterministic_create_key=command['deterministic_create_key'],
                    reason='lease_active',
                    desired_snapshot=desired_snapshot,
                )
            competing = (
                await session.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM entitlement_projection_commands
                             WHERE identity_id = :identity_id AND id <> :command_id
                               AND (remote_outcome_unknown OR mutation_sent_at IS NOT NULL)
                               AND stage NOT IN ('ready', 'cancelled')
                        )
                        """
                    ),
                    {'identity_id': identity_id, 'command_id': command_id},
                )
            ).scalar_one()
            mutation_possible = command['mutation_sent_at'] is not None or competing
            transition = claim_transition(
                command_stage=Stage(command['stage']),
                command_generation=command['generation'],
                current_generation=identity['generation'],
                identity_quarantined=identity['lifecycle_state'] in {'quarantined', 'remote_outcome_unknown'},
                remote_outcome_unknown=bool(command['remote_outcome_unknown']),
                mutation_was_possible=mutation_possible,
            )
            next_epoch = int(command['lease_epoch']) + 1
            mode = 'mutate' if transition.may_mutate else 'observe'
            stage = transition.stage.value
            if mode == 'observe' and stage == Stage.CANCELLED.value:
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_projection_commands
                           SET stage = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                               last_error_code = :reason, updated_at = :now
                         WHERE id = :id
                        """
                    ),
                    {'id': command_id, 'reason': transition.reason, 'now': now},
                )
            else:
                if mode == 'observe':
                    stage = Stage.QUARANTINED.value
                    await session.execute(
                        text(
                            """
                            UPDATE entitlement_identities
                               SET lifecycle_state = 'quarantined', quarantine_code = :reason,
                                   remote_outcome_unknown_generation = COALESCE(remote_outcome_unknown_generation, :generation),
                                   updated_at = :now
                             WHERE id = :identity_id
                            """
                        ),
                        {
                            'identity_id': identity_id,
                            'reason': transition.reason,
                            'generation': command['generation'],
                            'now': now,
                        },
                    )
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_projection_commands
                           SET stage = :stage, lease_owner = :worker, lease_epoch = :epoch,
                               lease_expires_at = :lease_expires, updated_at = :now,
                               last_error_code = COALESCE(:reason, last_error_code)
                         WHERE id = :id
                        """
                    ),
                    {
                        'id': command_id,
                        'stage': stage,
                        'worker': worker,
                        'epoch': next_epoch,
                        'lease_expires': now + timedelta(seconds=lease_seconds),
                        'now': now,
                        'reason': transition.reason,
                    },
                )
            return CommandClaim(
                command_id=command_id,
                identity_id=identity_id,
                generation=command['generation'],
                lease_epoch=next_epoch,
                mode=mode,
                panel_uuid=identity['panel_uuid'],
                deterministic_create_key=command['deterministic_create_key'],
                reason=transition.reason,
                desired_snapshot=desired_snapshot,
            )

    async def mark_mutation_sent(self, claim: CommandClaim, *, stage: Stage, now: datetime) -> None:
        if stage not in {Stage.CREATING_DISABLED, Stage.MUTATING}:
            raise ValueError('only mutating stages may be marked sent')
        async with self._sessions() as session, session.begin():
            identity = (
                (
                    await session.execute(
                        text(
                            'SELECT id, generation, lifecycle_state, panel_uuid, deterministic_owner_key '
                            'FROM entitlement_identities WHERE id=:id FOR UPDATE'
                        ),
                        {'id': claim.identity_id},
                    )
                )
                .mappings()
                .one()
            )
            command = (
                (
                    await session.execute(
                        text(
                            'SELECT generation, lease_epoch, remote_outcome_unknown, desired_hash, source_revision_id '
                            'FROM entitlement_projection_commands WHERE id=:id FOR UPDATE'
                        ),
                        {'id': claim.command_id},
                    )
                )
                .mappings()
                .one()
            )
            source = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_source_revisions WHERE id=:id'),
                        {'id': command['source_revision_id']},
                    )
                )
                .mappings()
                .one_or_none()
            )
            try:
                durable_desired = _durable_desired_snapshot(
                    identity=identity,
                    command=command,
                    source=source,
                )
                desired_fence_lost = (
                    claim.desired_snapshot is None
                    or claim.desired_snapshot.desired_hash != durable_desired.desired_hash
                )
            except (KeyError, TypeError, ValueError):
                desired_fence_lost = True
            if (
                identity['generation'] != claim.generation
                or identity['lifecycle_state']
                in {'quarantined', 'remote_outcome_unknown', 'erasure_requested', 'cleanup_terminal', 'final_erasure'}
                or command['generation'] != claim.generation
                or command['lease_epoch'] != claim.lease_epoch
                or command['remote_outcome_unknown']
                or desired_fence_lost
            ):
                raise RuntimeError('mutation_fence_lost')
            await session.execute(
                text(
                    """
                    UPDATE entitlement_projection_commands
                       SET stage = :stage, mutation_sent_at = :now, updated_at = :now
                     WHERE id = :id
                    """
                ),
                {
                    'id': claim.command_id,
                    'stage': stage.value,
                    'now': now,
                },
            )

    async def bind_uuid(
        self,
        claim: CommandClaim,
        panel_uuid: str,
        *,
        panel_uuid_hmac: str,
        encrypted_cleanup_panel_uuid: bytes,
        bound_desired_hash: str,
        now: datetime,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            # Serialize all bind attempts for the same remote identifier before
            # taking identity rows.  This makes the cross-owner check atomic;
            # the UNIQUE constraint remains the final DB backstop.
            await session.execute(
                text('SELECT pg_advisory_xact_lock(hashtextextended(:panel_uuid, 0))'),
                {'panel_uuid': panel_uuid},
            )
            conflict_id = (
                await session.execute(
                    text('SELECT id FROM entitlement_identities WHERE panel_uuid=:panel_uuid AND id<>:identity_id'),
                    {'panel_uuid': panel_uuid, 'identity_id': claim.identity_id},
                )
            ).scalar_one_or_none()
            identity_ids = sorted({claim.identity_id, *([int(conflict_id)] if conflict_id is not None else [])})
            identities = (
                (
                    await session.execute(
                        text(
                            'SELECT id, generation, lifecycle_state, panel_uuid, deterministic_owner_key '
                            'FROM entitlement_identities WHERE id = ANY(:identity_ids) ORDER BY id FOR UPDATE'
                        ),
                        {'identity_ids': identity_ids},
                    )
                )
                .mappings()
                .all()
            )
            identity_by_id = {int(row['id']): row for row in identities}
            identity = identity_by_id[claim.identity_id]
            command = (
                (
                    await session.execute(
                        text(
                            'SELECT generation, lease_epoch, stage, desired_hash, source_revision_id, '
                            'deterministic_create_key, mutation_sent_at, remote_outcome_unknown '
                            'FROM entitlement_projection_commands WHERE id=:id FOR UPDATE'
                        ),
                        {'id': claim.command_id},
                    )
                )
                .mappings()
                .one()
            )
            source = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_source_revisions WHERE id=:id'),
                        {'id': command['source_revision_id']},
                    )
                )
                .mappings()
                .one_or_none()
            )
            terminal_lifecycle = identity['lifecycle_state'] in {
                'erasure_requested',
                'cleanup_terminal',
                'final_erasure',
            }
            if terminal_lifecycle:
                cleanup = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT operation_id, generation, state, remote_outcome_unknown,
                                       encrypted_create_locator
                                  FROM entitlement_cleanup_commands
                                 WHERE identity_id=:identity_id
                                 ORDER BY id DESC LIMIT 1
                                 FOR UPDATE
                                """
                            ),
                            {'identity_id': claim.identity_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                safe_erasure_handoff = bool(
                    identity['lifecycle_state'] == 'erasure_requested'
                    and identity['generation'] == claim.generation + 1
                    and command['generation'] == claim.generation
                    and command['lease_epoch'] == claim.lease_epoch
                    and command['stage'] == Stage.REMOTE_OUTCOME_UNKNOWN.value
                    and command['remote_outcome_unknown']
                    and command['mutation_sent_at'] is not None
                    and command['deterministic_create_key'] == claim.deterministic_create_key
                    and cleanup is not None
                    and cleanup['generation'] == identity['generation']
                    and cleanup['state'] == 'quarantined'
                    and cleanup['remote_outcome_unknown']
                    and cleanup['encrypted_create_locator'] is not None
                )
                if not safe_erasure_handoff:
                    raise RuntimeError('uuid_bind_fence_lost')
                if conflict_id is not None:
                    await session.execute(
                        text(
                            """
                            UPDATE entitlement_cleanup_commands
                               SET last_error_code='create_receipt_uuid_conflict', updated_at=:now
                             WHERE operation_id=:operation_id
                            """
                        ),
                        {'operation_id': cleanup['operation_id'], 'now': now},
                    )
                    return False
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_cleanup_commands
                           SET encrypted_panel_uuid=:encrypted_panel_uuid,
                               panel_uuid_hmac=:panel_uuid_hmac,
                               last_error_code='create_receipt_handed_to_cleanup',
                               updated_at=:now
                         WHERE operation_id=:operation_id
                        """
                    ),
                    {
                        'operation_id': cleanup['operation_id'],
                        'encrypted_panel_uuid': encrypted_cleanup_panel_uuid,
                        'panel_uuid_hmac': panel_uuid_hmac,
                        'now': now,
                    },
                )
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_cleanup_tombstones
                           SET panel_uuid_hmac=:panel_uuid_hmac,
                               last_error_code='create_receipt_handed_to_cleanup'
                         WHERE operation_id=:operation_id
                        """
                    ),
                    {
                        'operation_id': cleanup['operation_id'],
                        'panel_uuid_hmac': panel_uuid_hmac,
                    },
                )
                return False
            if conflict_id is not None:
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_identities
                           SET lifecycle_state='quarantined', quarantine_code='cross_identity_panel_uuid',
                               updated_at=:now
                         WHERE id = ANY(:identity_ids)
                        """
                    ),
                    {'identity_ids': identity_ids, 'now': now},
                )
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_projection_commands
                           SET stage='quarantined', last_error_code='cross_identity_panel_uuid',
                               lease_owner=NULL, lease_expires_at=NULL, updated_at=:now
                         WHERE id=:id AND generation=:generation AND lease_epoch=:lease_epoch
                        """
                    ),
                    {
                        'id': claim.command_id,
                        'generation': claim.generation,
                        'lease_epoch': claim.lease_epoch,
                        'now': now,
                    },
                )
                return False
            if (
                identity['generation'] != claim.generation
                or identity['lifecycle_state']
                in {'quarantined', 'remote_outcome_unknown', 'erasure_requested', 'cleanup_terminal', 'final_erasure'}
                or command['generation'] != claim.generation
                or command['lease_epoch'] != claim.lease_epoch
                or command['stage'] != Stage.CREATING_DISABLED.value
            ):
                raise RuntimeError('uuid_bind_fence_lost')
            if identity['panel_uuid'] not in {None, panel_uuid}:
                raise RuntimeError('cross_identity_uuid_bind')
            try:
                durable_desired = _durable_desired_snapshot(
                    identity=identity,
                    command=command,
                    source=source,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError('uuid_bind_desired_fence_lost') from exc
            if (
                claim.desired_snapshot is None
                or claim.desired_snapshot.desired_hash != durable_desired.desired_hash
                or durable_desired.panel_uuid is not None
                or bound_desired_hash != durable_desired.bind(panel_uuid).desired_hash
            ):
                raise RuntimeError('uuid_bind_desired_fence_lost')
            await session.execute(
                text(
                    """
                    UPDATE entitlement_identities
                       SET panel_uuid=:panel_uuid, panel_uuid_hmac=:panel_uuid_hmac,
                           lifecycle_state='projecting', updated_at=:now
                     WHERE id=:id
                    """
                ),
                {
                    'id': claim.identity_id,
                    'panel_uuid': panel_uuid,
                    'panel_uuid_hmac': panel_uuid_hmac,
                    'now': now,
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE entitlement_projection_commands
                       SET stage='uuid_bound', mutation_sent_at=NULL,
                           desired_hash=:desired_hash, updated_at=:now
                     WHERE id=:id
                    """
                ),
                {'id': claim.command_id, 'desired_hash': bound_desired_hash, 'now': now},
            )
            return True

    async def mark_verifying(self, claim: CommandClaim, *, now: datetime) -> None:
        async with self._sessions() as session, session.begin():
            updated = await session.execute(
                text(
                    """
                    UPDATE entitlement_projection_commands
                       SET stage='verifying', mutation_sent_at=NULL, updated_at=:now
                     WHERE id=:id AND generation=:generation AND lease_epoch=:lease_epoch
                       AND stage='mutating' AND remote_outcome_unknown=false
                    """
                ),
                {
                    'id': claim.command_id,
                    'generation': claim.generation,
                    'lease_epoch': claim.lease_epoch,
                    'now': now,
                },
            )
            if updated.rowcount != 1:
                raise RuntimeError('verify_fence_lost')

    async def quarantine(self, claim: CommandClaim, *, code: str, unknown: bool, now: datetime) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE entitlement_identities
                       SET lifecycle_state='quarantined', quarantine_code=:code,
                           remote_outcome_unknown_generation=CASE WHEN :unknown THEN :generation
                                                                  ELSE remote_outcome_unknown_generation END,
                           updated_at=:now
                     WHERE id=:identity_id AND generation=:generation
                       AND lifecycle_state NOT IN ('erasure_requested', 'cleanup_terminal', 'final_erasure')
                    """
                ),
                {
                    'identity_id': claim.identity_id,
                    'code': code,
                    'unknown': unknown,
                    'generation': claim.generation,
                    'now': now,
                },
            )
            await session.execute(
                text(
                    """
                    UPDATE entitlement_projection_commands
                       SET stage=:stage, remote_outcome_unknown=:unknown, last_error_code=:code,
                           lease_owner=NULL, lease_expires_at=NULL, updated_at=:now
                     WHERE id=:id AND generation=:generation AND lease_epoch=:lease_epoch
                    """
                ),
                {
                    'id': claim.command_id,
                    'generation': claim.generation,
                    'lease_epoch': claim.lease_epoch,
                    'stage': Stage.REMOTE_OUTCOME_UNKNOWN.value if unknown else Stage.QUARANTINED.value,
                    'unknown': unknown,
                    'code': code,
                    'now': now,
                },
            )

    async def record_observation(
        self,
        claim: CommandClaim,
        observed: EntitlementSnapshot,
        comparison: SnapshotComparison,
        *,
        event_type: str,
        now: datetime,
    ) -> None:
        event_key = hashlib.sha256(f'{claim.command_id}:{event_type}:{comparison.observed_hash}'.encode()).hexdigest()
        async with self._sessions() as session, session.begin():
            if not await _lock_identity_for_evidence(
                session,
                identity_id=claim.identity_id,
                generation=claim.generation,
            ):
                return
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_observations
                           (identity_id, generation, source_kind, event_type, event_id_hash,
                            observed_snapshot, observed_hash, comparison_result, mismatch_fields,
                            observed_at, created_at, retention_until)
                    VALUES (:identity_id, :generation, 'canonical_get', :event_type, :event_key,
                           CAST(:snapshot AS jsonb), :observed_hash, :result, CAST(:mismatch AS jsonb),
                           :now, :now, :retention)
                    ON CONFLICT (identity_id, source_kind, event_id_hash) DO NOTHING
                    """
                ),
                {
                    'identity_id': claim.identity_id,
                    'generation': claim.generation,
                    'event_type': event_type,
                    'event_key': event_key,
                    'snapshot': __import__('json').dumps(observed.canonical(), sort_keys=True),
                    'observed_hash': comparison.observed_hash,
                    'result': 'exact' if comparison.exact else 'mismatch',
                    'mismatch': __import__('json').dumps(list(comparison.mismatch_fields)),
                    'now': now,
                    'retention': now + timedelta(days=90),
                },
            )

    async def finalize_entitlement_command(
        self,
        claim: CommandClaim,
        comparison: SnapshotComparison,
        *,
        now: datetime,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            identity = (
                (
                    await session.execute(
                        text(
                            'SELECT id, generation, lifecycle_state, panel_uuid, deterministic_owner_key '
                            'FROM entitlement_identities WHERE id=:id FOR UPDATE'
                        ),
                        {'id': claim.identity_id},
                    )
                )
                .mappings()
                .one()
            )
            command = (
                (
                    await session.execute(
                        text(
                            'SELECT generation, lease_epoch, stage, remote_outcome_unknown, '
                            'desired_hash, source_revision_id '
                            'FROM entitlement_projection_commands WHERE id=:id FOR UPDATE'
                        ),
                        {'id': claim.command_id},
                    )
                )
                .mappings()
                .one()
            )
            source = (
                (
                    await session.execute(
                        text('SELECT * FROM entitlement_source_revisions WHERE id=:id'),
                        {'id': command['source_revision_id']},
                    )
                )
                .mappings()
                .one_or_none()
            )
            try:
                durable_desired = _durable_desired_snapshot(
                    identity=identity,
                    command=command,
                    source=source,
                )
                desired_fence_lost = (
                    claim.desired_snapshot is None
                    or claim.desired_snapshot.desired_hash != durable_desired.desired_hash
                    or comparison.desired_hash != durable_desired.desired_hash
                )
            except (KeyError, TypeError, ValueError):
                desired_fence_lost = True
            transition = finalize_transition(
                command_generation=command['generation'],
                current_generation=identity['generation'],
                remote_outcome_unknown=bool(command['remote_outcome_unknown']),
                exact_canonical_match=comparison.exact,
            )
            if (
                not transition.may_finalize
                or command['lease_epoch'] != claim.lease_epoch
                or command['stage'] != Stage.VERIFYING.value
                or command['desired_hash'] != comparison.desired_hash
                or identity['lifecycle_state'] == 'quarantined'
                or desired_fence_lost
            ):
                fence_failure = transition.may_finalize
                reason = 'finalize_fence_lost' if fence_failure else (transition.reason or 'finalize_rejected')
                failed_stage = Stage.QUARANTINED.value if fence_failure else transition.stage.value
                if fence_failure:
                    await session.execute(
                        text(
                            """
                            UPDATE entitlement_identities
                               SET lifecycle_state='quarantined', quarantine_code=:reason, updated_at=:now
                             WHERE id=:identity_id AND generation=:generation
                               AND lifecycle_state NOT IN
                                   ('erasure_requested', 'cleanup_terminal', 'final_erasure')
                            """
                        ),
                        {
                            'identity_id': claim.identity_id,
                            'generation': claim.generation,
                            'reason': reason,
                            'now': now,
                        },
                    )
                await session.execute(
                    text(
                        """
                        UPDATE entitlement_projection_commands
                           SET stage=:stage, last_error_code=:reason,
                               lease_owner=NULL, lease_expires_at=NULL, updated_at=:now
                         WHERE id=:id
                        """
                    ),
                    {
                        'id': claim.command_id,
                        'stage': failed_stage,
                        'reason': reason,
                        'now': now,
                    },
                )
                return False
            await session.execute(
                text(
                    """
                    UPDATE entitlement_identities
                       SET verified_generation=:generation, lifecycle_state='ready',
                           quarantine_code=NULL, updated_at=:now
                     WHERE id=:identity_id
                    """
                ),
                {'identity_id': claim.identity_id, 'generation': claim.generation, 'now': now},
            )
            await session.execute(
                text(
                    """
                    UPDATE entitlement_projection_commands
                       SET stage='ready', canonical_observation_hash=:observed_hash,
                           lease_owner=NULL, lease_expires_at=NULL, completed_at=:now, updated_at=:now
                     WHERE id=:id
                    """
                ),
                {'id': claim.command_id, 'observed_hash': comparison.observed_hash, 'now': now},
            )
            await session.execute(
                text(
                    """
                    UPDATE entitlement_notification_intents
                       SET state='cancelled', cancellation_code='superseded_generation', cancelled_at=:now
                     WHERE identity_id=:identity_id AND generation < :generation AND state='pending'
                    """
                ),
                {'identity_id': claim.identity_id, 'generation': claim.generation, 'now': now},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_notification_intents
                           (operation_id, identity_id, generation, notification_type, state, created_at)
                    VALUES (:operation_id, :identity_id, :generation, 'ready', 'pending', :now)
                    ON CONFLICT (identity_id, generation, notification_type) DO NOTHING
                    """
                ),
                {
                    'operation_id': str(uuid.uuid4()),
                    'identity_id': claim.identity_id,
                    'generation': claim.generation,
                    'now': now,
                },
            )
            return True


async def append_source_and_command(
    session: AsyncSession,
    *,
    identity_id: int,
    source_type: str,
    source_key: str,
    source_fingerprint: str,
    snapshot: EntitlementSnapshot,
) -> tuple[int, int]:
    """Append source+command inside the caller's financial transaction.

    The caller owns canonical Payment→User→Subscription locks before invoking
    this function.  A rollback therefore leaves neither source nor command.
    Duplicate provider callbacks resolve to the one immutable source.
    """

    identity = (
        (
            await session.execute(
                text(
                    """
                    SELECT i.generation, i.lifecycle_state, i.erasure_requested_at,
                           i.cleanup_terminal_at, i.deterministic_owner_key, i.panel_uuid,
                           EXISTS (
                               SELECT 1 FROM entitlement_cleanup_commands c
                                WHERE c.identity_id=i.id
                           ) AS has_cleanup_command
                      FROM entitlement_identities i
                     WHERE i.id=:id
                     FOR UPDATE
                    """
                ),
                {'id': identity_id},
            )
        )
        .mappings()
        .one()
    )
    if (
        identity['lifecycle_state'] in {'erasure_requested', 'cleanup_terminal', 'final_erasure'}
        or identity['erasure_requested_at'] is not None
        or identity['cleanup_terminal_at'] is not None
        or identity['has_cleanup_command']
    ):
        raise ValueError('identity_erasure_in_progress_or_complete')
    if snapshot.owner_key != identity['deterministic_owner_key'] or snapshot.panel_uuid != identity['panel_uuid']:
        raise ValueError('snapshot_identity_binding_mismatch')
    existing = (
        (
            await session.execute(
                text(
                    """
                SELECT id, identity_id, generation, source_fingerprint, desired_hash, provenance
                  FROM entitlement_source_revisions
                 WHERE source_type=:source_type AND source_key=:source_key
                """
                ),
                {'source_type': source_type, 'source_key': source_key},
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing:
        if (
            existing['identity_id'] != identity_id
            or existing['source_fingerprint'] != source_fingerprint
            or existing['desired_hash'] != snapshot.desired_hash
            or existing['provenance'] != snapshot.provenance
        ):
            raise ValueError('source_provenance_conflict')
        command_id = (
            await session.execute(
                text(
                    'SELECT id FROM entitlement_projection_commands WHERE identity_id=:identity_id AND generation=:generation'
                ),
                {'identity_id': identity_id, 'generation': existing['generation']},
            )
        ).scalar_one()
        return existing['id'], command_id
    generation = int(identity['generation']) + 1
    if snapshot.generation != generation:
        raise ValueError('snapshot generation must be the next locked identity generation')
    source_id = (
        await session.execute(
            text(
                """
                INSERT INTO entitlement_source_revisions
                       (identity_id, generation, source_type, source_key, source_fingerprint,
                        provenance, desired_snapshot, desired_hash, authority_state)
                VALUES (:identity_id, :generation, :source_type, :source_key, :source_fingerprint,
                        :provenance, CAST(:snapshot AS jsonb), :desired_hash, 'authorized')
             RETURNING id
                """
            ),
            {
                'identity_id': identity_id,
                'generation': generation,
                'source_type': source_type,
                'source_key': source_key,
                'source_fingerprint': source_fingerprint,
                'provenance': snapshot.provenance,
                'snapshot': __import__('json').dumps(snapshot.canonical(), sort_keys=True),
                'desired_hash': snapshot.desired_hash,
            },
        )
    ).scalar_one()
    await session.execute(
        text(
            """
            UPDATE entitlement_identities
               SET generation=:generation,
                   lifecycle_state=CASE
                       WHEN lifecycle_state IN ('quarantined', 'remote_outcome_unknown') THEN lifecycle_state
                       ELSE 'pending'
                   END,
                   updated_at=now()
             WHERE id=:identity_id
            """
        ),
        {'identity_id': identity_id, 'generation': generation},
    )
    await session.execute(
        text(
            """
            UPDATE entitlement_notification_intents
               SET state='cancelled', cancellation_code='superseded_generation', cancelled_at=now()
             WHERE identity_id=:identity_id AND generation < :generation AND state='pending'
            """
        ),
        {'identity_id': identity_id, 'generation': generation},
    )
    command_id = (
        await session.execute(
            text(
                """
                INSERT INTO entitlement_projection_commands
                       (operation_id, identity_id, source_revision_id, generation, command_type,
                        idempotency_key, deterministic_create_key, desired_hash, stage)
                VALUES (:operation_id, :identity_id, :source_id, :generation, 'project',
                        :idempotency_key, :create_key, :desired_hash, 'pending')
             RETURNING id
                """
            ),
            {
                'operation_id': str(uuid.uuid4()),
                'identity_id': identity_id,
                'source_id': source_id,
                'generation': generation,
                'idempotency_key': f'{source_type}:{source_key}',
                'create_key': hashlib.sha256(f'{identity_id}:{generation}:{source_key}'.encode()).hexdigest(),
                'desired_hash': snapshot.desired_hash,
            },
        )
    ).scalar_one()
    return source_id, command_id


async def lock_entitlement_context(
    session: AsyncSession,
    *,
    payment_attempt_id: int | None,
    user_id: int,
    subscription_id: int | None,
    identity_id: int,
) -> None:
    """Canonical Payment→User→Subscription→Identity lock acquisition."""

    if payment_attempt_id is not None:
        await session.execute(
            text('SELECT id FROM checkout_payment_attempts WHERE id=:id FOR UPDATE'),
            {'id': payment_attempt_id},
        )
    await session.execute(text('SELECT id FROM users WHERE id=:id FOR UPDATE'), {'id': user_id})
    if subscription_id is not None:
        await session.execute(text('SELECT id FROM subscriptions WHERE id=:id FOR UPDATE'), {'id': subscription_id})
    await session.execute(text('SELECT id FROM entitlement_identities WHERE id=:id FOR UPDATE'), {'id': identity_id})


async def ingest_webhook_observation(
    session: AsyncSession,
    *,
    identity_id: int | None,
    event_id_hash: str,
    event_type: str,
    normalized_hash: str,
    event_timestamp: datetime,
    now: datetime,
) -> bool:
    """Append normalized webhook metadata only; commercial desired state is untouched."""

    linked_identity_id = None
    if identity_id is not None and await _lock_identity_for_evidence(session, identity_id=identity_id):
        linked_identity_id = identity_id
    inserted = await session.execute(
        text(
            """
            INSERT INTO entitlement_webhook_inbox
                   (identity_id, event_id_hash, event_type, normalized_hash,
                    event_timestamp, received_at, retention_until)
            VALUES (:linked_identity_id, :event_id_hash, :event_type, :normalized_hash,
                    :event_timestamp, :now, :retention)
            ON CONFLICT (event_id_hash) DO NOTHING
            RETURNING id
            """
        ),
        {
            'linked_identity_id': linked_identity_id,
            'event_id_hash': event_id_hash,
            'event_type': event_type,
            'normalized_hash': normalized_hash,
            'event_timestamp': event_timestamp,
            'now': now,
            'retention': now + timedelta(days=90),
        },
    )
    return inserted.scalar_one_or_none() is not None

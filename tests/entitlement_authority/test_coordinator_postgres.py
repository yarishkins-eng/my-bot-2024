from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.entitlement_authority.coordinator import ProjectionCoordinator
from app.services.entitlement_authority.persistence import (
    PostgresEntitlementStore,
    append_source_and_command,
    ingest_webhook_observation,
    lock_entitlement_context,
)
from app.services.entitlement_authority.privacy import mark_final_erasure, request_erasure_cleanup
from app.services.entitlement_authority.state_machine import Stage
from app.services.entitlement_authority.strict_panel import StrictPanelClient, panel_owner_username
from app.services.entitlement_authority.types import EntitlementSnapshot, compare_snapshots
from app.utils.security import decrypt_restricted_identifier


DATABASE_URL = os.environ.get('ENTITLEMENT_AUTHORITY_APP_DATABASE_URL')
if DATABASE_URL is None:
    pytest.skip('isolated entitlement authority PostgreSQL is not configured', allow_module_level=True)
NOW = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
SECRET = 'test-only-existing-application-secret'


def desired(
    *, generation: int = 1, panel_uuid: str | None = 'panel-existing', **changes: object
) -> EntitlementSnapshot:
    values: dict[str, object] = {
        'owner_key': 'owner-fingerprint',
        'panel_uuid': panel_uuid,
        'status': 'ACTIVE',
        'expire_at': NOW + timedelta(days=30),
        'traffic_limit_bytes': 0,
        'traffic_limit_strategy': 'NO_RESET',
        'hwid_device_limit': None,
        'internal_squads': (),
        'external_squad_uuid': None,
        'provenance': 'paid_sale',
        'generation': generation,
    }
    values.update(changes)
    return EntitlementSnapshot(**values)  # type: ignore[arg-type]


def raw(snapshot: EntitlementSnapshot) -> dict[str, Any]:
    return {
        'username': panel_owner_username(snapshot.owner_key),
        'uuid': snapshot.panel_uuid,
        'status': snapshot.status,
        'expireAt': snapshot.expire_at.isoformat().replace('+00:00', 'Z'),
        'trafficLimitBytes': snapshot.traffic_limit_bytes,
        'trafficLimitStrategy': snapshot.traffic_limit_strategy,
        'hwidDeviceLimit': snapshot.hwid_device_limit,
        'activeInternalSquads': list(snapshot.internal_squads),
        'externalSquadUuid': snapshot.external_squad_uuid,
    }


class FakePanelTransport:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.patch_mode = 'apply'
        self.create_mode = 'apply'
        self.create_receipt_override: str | None = None
        self.create_state_changes: dict[str, Any] = {}
        self.get_timeout = False
        self.lookup_override: list[dict[str, Any]] | None = None
        self.patch_received = asyncio.Event()
        self.patch_release = asyncio.Event()
        self.patch_release.set()
        self._created = 0

    def _apply_patch(self, payload: Mapping[str, Any]) -> None:
        panel_uuid = str(payload['uuid'])
        state = self.states[panel_uuid]
        state.update(
            {
                'uuid': panel_uuid,
                'status': payload['status'],
                'expireAt': payload['expireAt'],
                'trafficLimitBytes': payload['trafficLimitBytes'],
                'trafficLimitStrategy': payload['trafficLimitStrategy'],
                'hwidDeviceLimit': payload['hwidDeviceLimit'],
                'activeInternalSquads': list(payload['activeInternalSquads']),
                'externalSquadUuid': payload['externalSquadUuid'],
            }
        )

    async def request_once(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        copied = dict(payload) if payload is not None else None
        self.calls.append((method, endpoint, copied))
        if method == 'GET' and '/by-username/' in endpoint:
            if self.get_timeout:
                raise TimeoutError('fake read timeout')
            candidates = self.lookup_override
            if candidates is None:
                username = endpoint.rsplit('/', 1)[-1]
                candidates = [state for state in self.states.values() if state.get('username') == username]
            return {'response': candidates}
        if method == 'GET':
            if self.get_timeout:
                raise TimeoutError('fake read timeout')
            return {'response': self.states.get(endpoint.rsplit('/', 1)[-1])}
        if method == 'POST':
            assert endpoint == '/api/users'
            assert payload is not None and payload['status'] == 'DISABLED'
            self._created += 1
            panel_uuid = f'panel-created-{self._created}'
            state = {
                'uuid': panel_uuid,
                'username': payload['username'],
                **{key: value for key, value in payload.items() if key != 'username'},
            }
            state.update(self.create_state_changes)
            if self.create_mode in {'apply', 'apply_lost'}:
                self.states[panel_uuid] = state
            if self.create_mode == 'apply_lost':
                raise ConnectionError('response lost after apply')
            if self.create_mode == 'lost_without_apply':
                raise ConnectionError('request outcome unknown')
            return {'response': {'uuid': self.create_receipt_override or panel_uuid}}
        if method == 'PATCH':
            assert payload is not None
            self.patch_received.set()
            await self.patch_release.wait()
            if self.patch_mode in {'apply', 'apply_lost'}:
                self._apply_patch(payload)
            if self.patch_mode == 'apply_lost':
                raise ConnectionError('response lost after apply')
            return {'response': self.states[str(payload['uuid'])]}
        raise AssertionError((method, endpoint))

    def count(self, method: str) -> int:
        return sum(call_method == method for call_method, _endpoint, _payload in self.calls)


@pytest_asyncio.fixture
async def sessions() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=0)
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


async def make_command(
    sessions: async_sessionmaker[AsyncSession],
    snapshot: EntitlementSnapshot,
    *,
    source_key: str = 'source-one',
) -> tuple[int, int, PostgresEntitlementStore]:
    async with sessions() as session, session.begin():
        identity_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_identities
                           (operation_id, deterministic_owner_key, panel_uuid, generation, lifecycle_state)
                    VALUES (:operation_id, :owner_key, :panel_uuid, 0, 'dormant')
                    RETURNING id
                    """
                ),
                {
                    'operation_id': str(uuid.uuid4()),
                    'owner_key': snapshot.owner_key,
                    'panel_uuid': snapshot.panel_uuid,
                },
            )
        ).scalar_one()
        _source_id, command_id = await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='test',
            source_key=source_key,
            source_fingerprint=hashlib.sha256(source_key.encode()).hexdigest(),
            snapshot=snapshot,
        )
    return identity_id, command_id, PostgresEntitlementStore(sessions)


async def command_state(sessions: async_sessionmaker[AsyncSession], command_id: int) -> dict[str, Any]:
    async with sessions() as session:
        return dict(
            (
                await session.execute(
                    text(
                        """
                        SELECT c.*, i.lifecycle_state, i.generation AS identity_generation,
                               i.verified_generation, i.panel_uuid
                          FROM entitlement_projection_commands c
                          JOIN entitlement_identities i ON i.id=c.identity_id
                         WHERE c.id=:id
                        """
                    ),
                    {'id': command_id},
                )
            )
            .mappings()
            .one()
        )


def coordinator(store: PostgresEntitlementStore, fake: FakePanelTransport, failpoint=None) -> ProjectionCoordinator:
    return ProjectionCoordinator(
        store,
        StrictPanelClient(fake),
        fingerprint_secret=SECRET,
        failpoint=failpoint,
    )


@pytest.mark.asyncio
async def test_existing_binding_stale_fields_never_ready_before_exact(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(target, status='DISABLED', traffic_limit_bytes=99))
    fake.patch_mode = 'ack_without_apply'
    result = await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
    state = await command_state(sessions, command_id)
    assert result == 'quarantined'
    assert state['stage'] == 'quarantined'
    assert state['verified_generation'] is None
    assert fake.count('POST') == 0 and fake.count('PATCH') == 1


@pytest.mark.asyncio
async def test_caller_snapshot_cannot_override_bound_durable_source(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    authorized = desired()
    forged = replace(authorized, status='DISABLED')
    _identity, command_id, store = await make_command(sessions, authorized, source_key='forged-bound')
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(authorized)

    result = await coordinator(store, fake).project_command(command_id, forged, worker='worker-one', now=NOW)

    state = await command_state(sessions, command_id)
    assert result == 'quarantined'
    assert state['stage'] == 'quarantined' and state['last_error_code'] == 'caller_desired_mismatch'
    assert fake.states['panel-existing']['status'] == 'ACTIVE'
    assert fake.count('POST') == 0 and fake.count('PATCH') == 0


@pytest.mark.asyncio
async def test_caller_snapshot_cannot_override_unbound_durable_source_or_create(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    authorized = desired(panel_uuid=None)
    forged = replace(authorized, status='LIMITED', traffic_limit_bytes=123456)
    _identity, command_id, store = await make_command(sessions, authorized, source_key='forged-unbound')
    fake = FakePanelTransport()

    result = await coordinator(store, fake).project_command(command_id, forged, worker='worker-one', now=NOW)

    state = await command_state(sessions, command_id)
    async with sessions() as session:
        hashes = (
            await session.execute(
                text(
                    """
                    SELECT c.desired_hash, s.desired_hash
                      FROM entitlement_projection_commands c
                      JOIN entitlement_source_revisions s ON s.id=c.source_revision_id
                     WHERE c.id=:id
                    """
                ),
                {'id': command_id},
            )
        ).one()
    assert result == 'quarantined'
    assert state['stage'] == 'quarantined' and state['panel_uuid'] is None
    assert hashes[0] == hashes[1] == authorized.desired_hash
    assert fake.count('POST') == 0 and fake.count('PATCH') == 0


@pytest.mark.asyncio
async def test_finalize_hash_fence_never_writes_false_ready(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    authorized = desired()
    forged = replace(authorized, status='DISABLED')
    _identity, command_id, store = await make_command(sessions, authorized, source_key='forged-finalize')
    claim = await store.claim_entitlement_command(command_id, worker='worker-one', now=NOW)
    await store.mark_mutation_sent(claim, stage=Stage.MUTATING, now=NOW)
    await store.mark_verifying(claim, now=NOW)

    assert not await store.finalize_entitlement_command(
        claim,
        compare_snapshots(forged, forged),
        now=NOW,
    )
    state = await command_state(sessions, command_id)
    assert state['stage'] == 'quarantined'
    assert state['lifecycle_state'] == 'quarantined'
    assert state['verified_generation'] is None
    assert state['last_error_code'] == 'finalize_fence_lost'


@pytest.mark.asyncio
@pytest.mark.parametrize('bound', [True, False])
async def test_corrupt_durable_source_stops_before_any_panel_mutation(
    sessions: async_sessionmaker[AsyncSession],
    bound: bool,
) -> None:
    authorized = desired(panel_uuid='panel-existing' if bound else None)
    _identity, command_id, store = await make_command(
        sessions,
        authorized,
        source_key=f'corrupt-source-{bound}',
    )
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                """
                UPDATE entitlement_source_revisions
                   SET desired_snapshot=jsonb_set(desired_snapshot, '{status}', '"DISABLED"'::jsonb)
                 WHERE identity_id=:identity_id
                """
            ),
            {'identity_id': _identity},
        )
    fake = FakePanelTransport()
    if bound:
        fake.states['panel-existing'] = raw(authorized)

    result = await coordinator(store, fake).project_command(command_id, authorized, worker='worker-one', now=NOW)

    state = await command_state(sessions, command_id)
    assert result == 'quarantined'
    assert state['stage'] == 'quarantined' and state['last_error_code'] == 'durable_source_hash_mismatch'
    assert fake.count('POST') == 0 and fake.count('PATCH') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mismatch',
    [
        {'owner_key': 'different-owner'},
        {'panel_uuid': 'different-panel'},
    ],
)
async def test_append_snapshot_must_match_locked_identity_binding(
    sessions: async_sessionmaker[AsyncSession],
    mismatch: dict[str, object],
) -> None:
    first = desired()
    identity_id, _command_id, _store = await make_command(sessions, first, source_key='binding-first')
    second = replace(first, generation=2, **mismatch)
    with pytest.raises(ValueError, match='snapshot_identity_binding_mismatch'):
        async with sessions() as session, session.begin():
            await append_source_and_command(
                session,
                identity_id=identity_id,
                source_type='financial',
                source_key=f'binding-{next(iter(mismatch))}',
                source_fingerprint='a' * 64,
                snapshot=second,
            )
    async with sessions() as session:
        assert (
            await session.execute(
                text('SELECT count(*) FROM entitlement_source_revisions WHERE identity_id=:id'),
                {'id': identity_id},
            )
        ).scalar_one() == 1
        assert (
            await session.execute(
                text('SELECT generation FROM entitlement_identities WHERE id=:id'),
                {'id': identity_id},
            )
        ).scalar_one() == 1


@pytest.mark.asyncio
async def test_get_timeout_is_fail_closed_and_never_creates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(target)
    fake.get_timeout = True
    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    state = await command_state(sessions, command_id)
    assert state['verified_generation'] is None
    assert fake.count('POST') == 0


@pytest.mark.asyncio
async def test_patch_applied_lost_then_takeover_observes_only_never_creates(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(target, status='DISABLED'))
    fake.patch_mode = 'apply_lost'
    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    first_patch_count = fake.count('PATCH')
    fake.patch_mode = 'apply'
    assert (
        await coordinator(store, fake).project_command(
            command_id, target, worker='worker-two', now=NOW + timedelta(seconds=31)
        )
        == 'quarantined'
    )
    assert fake.count('PATCH') == first_patch_count == 1
    assert fake.count('POST') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('create_mode', ['apply_lost', 'lost_without_apply'])
async def test_create_unknown_has_at_most_one_disabled_candidate_and_no_blind_second_post(
    sessions: async_sessionmaker[AsyncSession],
    create_mode: str,
) -> None:
    target = desired(panel_uuid=None)
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.create_mode = create_mode
    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    assert fake.count('POST') == 1
    assert len(fake.states) <= 1
    if fake.states:
        assert next(iter(fake.states.values()))['status'] == 'DISABLED'
    await coordinator(store, fake).project_command(
        command_id, target, worker='worker-two', now=NOW + timedelta(seconds=31)
    )
    assert fake.count('POST') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('create_mode', ['apply_lost', 'lost_without_apply'])
async def test_erasure_after_unknown_create_retains_encrypted_deterministic_locator(
    sessions: async_sessionmaker[AsyncSession],
    create_mode: str,
) -> None:
    target = desired(panel_uuid=None)
    identity_id, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.create_mode = create_mode
    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    async with sessions() as session, session.begin():
        operation_id = await request_erasure_cleanup(
            session,
            identity_id=identity_id,
            secret=SECRET,
            now=NOW + timedelta(seconds=1),
        )
    async with sessions() as session:
        cleanup = (
            await session.execute(
                text(
                    'SELECT encrypted_panel_uuid, encrypted_create_locator, remote_outcome_unknown '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()
        identity = (
            await session.execute(
                text(
                    'SELECT panel_uuid, lifecycle_state, deterministic_owner_key '
                    'FROM entitlement_identities WHERE id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
    assert cleanup.remote_outcome_unknown
    assert cleanup.encrypted_panel_uuid is None
    assert cleanup.encrypted_create_locator is not None
    assert decrypt_restricted_identifier(
        cleanup.encrypted_create_locator,
        secret=SECRET,
        purpose='entitlement-panel-create-locator-v1',
    ) == panel_owner_username(target.owner_key)
    assert target.owner_key not in identity.deterministic_owner_key
    assert identity.panel_uuid is None and identity.lifecycle_state == 'erasure_requested'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'barrier',
    ['after_create_send_fence', 'after_create_post', 'after_create_canonical_get'],
)
async def test_erasure_during_create_hands_receipt_to_restricted_cleanup(
    sessions: async_sessionmaker[AsyncSession],
    barrier: str,
) -> None:
    target = desired(panel_uuid=None)
    identity_id, command_id, store = await make_command(sessions, target, source_key=f'erasure-create-{barrier}')
    fake = FakePanelTransport()
    operation_id: str | None = None

    async def erase(name: str) -> None:
        nonlocal operation_id
        if name != barrier or operation_id is not None:
            return
        async with sessions() as session, session.begin():
            operation_id = await request_erasure_cleanup(
                session,
                identity_id=identity_id,
                secret=SECRET,
                now=NOW + timedelta(seconds=1),
            )

    result = await coordinator(store, fake, erase).project_command(
        command_id,
        target,
        worker='worker-one',
        now=NOW,
    )
    assert result == 'quarantined'
    assert operation_id is not None and len(fake.states) == 1
    remote_uuid = next(iter(fake.states))
    async with sessions() as session:
        cleanup = (
            await session.execute(
                text(
                    'SELECT encrypted_panel_uuid, encrypted_create_locator, panel_uuid_hmac, '
                    'remote_outcome_unknown, last_error_code '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()
        identity = (
            await session.execute(
                text('SELECT panel_uuid, lifecycle_state FROM entitlement_identities WHERE id=:identity_id'),
                {'identity_id': identity_id},
            )
        ).one()
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 0
    assert cleanup.remote_outcome_unknown
    assert cleanup.encrypted_create_locator is not None
    assert cleanup.encrypted_panel_uuid is not None
    assert cleanup.last_error_code == 'create_receipt_handed_to_cleanup'
    assert (
        decrypt_restricted_identifier(
            cleanup.encrypted_panel_uuid,
            secret=SECRET,
            purpose='entitlement-panel-cleanup-v1',
        )
        == remote_uuid
    )
    assert identity.panel_uuid is None and identity.lifecycle_state == 'erasure_requested'


@pytest.mark.asyncio
async def test_terminal_receipt_handoff_rejects_cross_wired_identity_claim(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target_a = desired(panel_uuid=None, owner_key='owner-a')
    target_b = desired(panel_uuid=None, owner_key='owner-b')
    identity_a, command_a, store = await make_command(sessions, target_a, source_key='cross-wire-a')
    identity_b, command_b, _ = await make_command(sessions, target_b, source_key='cross-wire-b')
    claim_a = await store.claim_entitlement_command(command_a, worker='worker-a', now=NOW)
    claim_b = await store.claim_entitlement_command(command_b, worker='worker-b', now=NOW)
    await store.mark_mutation_sent(claim_a, stage=Stage.CREATING_DISABLED, now=NOW)
    await store.mark_mutation_sent(claim_b, stage=Stage.CREATING_DISABLED, now=NOW)
    async with sessions() as session, session.begin():
        operation_a = await request_erasure_cleanup(
            session,
            identity_id=identity_a,
            secret=SECRET,
            now=NOW + timedelta(seconds=1),
        )
    before_hmac = await _cleanup_row(sessions, operation_a)
    cross_wired = replace(
        claim_b,
        identity_id=identity_a,
        deterministic_create_key=claim_a.deterministic_create_key,
    )
    with pytest.raises(RuntimeError, match='uuid_bind_fence_lost'):
        await store.bind_uuid(
            cross_wired,
            'panel-for-b',
            panel_uuid_hmac='c' * 64,
            encrypted_cleanup_panel_uuid=b'wrong-cross-wired-ciphertext',
            bound_desired_hash=target_b.bind('panel-for-b').desired_hash,
            now=NOW + timedelta(seconds=2),
        )
    after_hmac = await _cleanup_row(sessions, operation_a)
    assert identity_a != identity_b
    assert before_hmac == after_hmac


async def _cleanup_row(
    sessions: async_sessionmaker[AsyncSession],
    operation_id: str,
) -> tuple[bytes | None, str, str | None]:
    async with sessions() as session:
        return (
            await session.execute(
                text(
                    'SELECT encrypted_panel_uuid, panel_uuid_hmac, last_error_code '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()


class SimulatedProcessKill(BaseException):
    pass


@pytest.mark.asyncio
async def test_unbound_create_rejects_foreign_receipt_before_bind_or_patch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired(panel_uuid=None)
    identity_id, command_id, store = await make_command(sessions, target, source_key='foreign-create-receipt')
    fake = FakePanelTransport()
    foreign_uuid = 'foreign-stale'
    foreign = raw(
        desired(
            panel_uuid=foreign_uuid,
            owner_key='foreign-owner',
            status='ACTIVE',
            traffic_limit_bytes=777,
        )
    )
    fake.states[foreign_uuid] = foreign
    fake.create_receipt_override = foreign_uuid

    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    state = await command_state(sessions, command_id)
    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT panel_uuid, lifecycle_state, remote_outcome_unknown_generation '
                    'FROM entitlement_identities WHERE id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
    assert fake.count('POST') == 1
    assert fake.count('GET') == 1
    assert fake.count('PATCH') == 0
    assert fake.states[foreign_uuid]['trafficLimitBytes'] == 777
    assert identity.panel_uuid is None
    assert identity.lifecycle_state == 'quarantined'
    assert identity.remote_outcome_unknown_generation == 1
    assert state['stage'] == 'remote_outcome_unknown'
    assert state['last_error_code'] == 'create_receipt_contract_invalid'
    assert state['verified_generation'] is None


@pytest.mark.asyncio
async def test_unbound_create_requires_exact_disabled_receipt_before_bind_or_patch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired(panel_uuid=None)
    identity_id, command_id, store = await make_command(sessions, target, source_key='inexact-create-receipt')
    fake = FakePanelTransport()
    fake.create_state_changes = {'status': 'ACTIVE', 'trafficLimitBytes': 777}

    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    state = await command_state(sessions, command_id)
    remote = next(iter(fake.states.values()))
    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT panel_uuid, lifecycle_state, remote_outcome_unknown_generation '
                    'FROM entitlement_identities WHERE id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
    assert fake.count('POST') == 1
    assert fake.count('GET') == 1
    assert fake.count('PATCH') == 0
    assert remote['status'] == 'ACTIVE' and remote['trafficLimitBytes'] == 777
    assert identity.panel_uuid is None
    assert identity.lifecycle_state == 'quarantined'
    assert identity.remote_outcome_unknown_generation == 1
    assert state['stage'] == 'remote_outcome_unknown'
    assert state['last_error_code'] == 'create_receipt_mismatch'
    assert state['verified_generation'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('create_mode', 'get_timeout', 'expected_error'),
    [
        ('ack_without_apply', False, 'create_receipt_missing'),
        ('apply', True, 'create_receipt_get_failed'),
    ],
)
async def test_unbound_create_receipt_read_failure_never_binds_or_patches(
    sessions: async_sessionmaker[AsyncSession],
    create_mode: str,
    get_timeout: bool,
    expected_error: str,
) -> None:
    target = desired(panel_uuid=None)
    identity_id, command_id, store = await make_command(
        sessions,
        target,
        source_key=f'create-receipt-read-{create_mode}',
    )
    fake = FakePanelTransport()
    fake.create_mode = create_mode
    fake.get_timeout = get_timeout

    assert (
        await coordinator(store, fake).project_command(command_id, target, worker='worker-one', now=NOW)
        == 'quarantined'
    )
    state = await command_state(sessions, command_id)
    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT panel_uuid, lifecycle_state, remote_outcome_unknown_generation '
                    'FROM entitlement_identities WHERE id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
    assert fake.count('POST') == 1
    assert fake.count('GET') == 1
    assert fake.count('PATCH') == 0
    assert identity.panel_uuid is None
    assert identity.lifecycle_state == 'quarantined'
    assert identity.remote_outcome_unknown_generation == 1
    assert state['stage'] == 'remote_outcome_unknown'
    assert state['last_error_code'] == expected_error
    assert state['verified_generation'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'barrier',
    [
        'after_intent',
        'after_create_send_fence',
        'after_create_post',
        'after_create_canonical_get',
        'after_uuid_bind',
        'after_active_patch',
        'after_canonical_get',
        'before_final_commit',
    ],
)
async def test_create_kill_barriers_never_duplicate_or_false_ready(
    sessions: async_sessionmaker[AsyncSession],
    barrier: str,
) -> None:
    target = desired(panel_uuid=None)
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fired = False

    async def failpoint(name: str) -> None:
        nonlocal fired
        if name == barrier and not fired:
            fired = True
            raise SimulatedProcessKill(name)

    with pytest.raises(SimulatedProcessKill):
        await coordinator(store, fake, failpoint).project_command(command_id, target, worker='worker-one', now=NOW)
    before = fake.count('POST')
    result = await coordinator(store, fake).project_command(
        command_id,
        target,
        worker='worker-two',
        now=NOW + timedelta(seconds=31),
    )
    state = await command_state(sessions, command_id)
    assert fake.count('POST') <= 1
    if barrier == 'after_intent':
        assert before == 0 and result == 'ready'
        assert state['verified_generation'] == 1
    else:
        assert result == 'quarantined'
        assert state['verified_generation'] is None


@pytest.mark.asyncio
async def test_late_generation_n_blocks_n_plus_one_and_never_becomes_ready(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = desired()
    identity_id, command_one, store = await make_command(sessions, first, source_key='generation-one')
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(first, status='DISABLED'))
    fake.patch_release.clear()
    task = asyncio.create_task(
        coordinator(store, fake).project_command(command_one, first, worker='worker-one', now=NOW)
    )
    await asyncio.wait_for(fake.patch_received.wait(), timeout=2)
    second = replace(first, generation=2, expire_at=first.expire_at + timedelta(days=30))
    async with sessions() as session, session.begin():
        _source_two, command_two = await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='test',
            source_key='generation-two',
            source_fingerprint='2' * 64,
            snapshot=second,
        )
    assert (
        await coordinator(store, fake).project_command(command_two, second, worker='worker-two', now=NOW)
        == 'quarantined'
    )
    assert fake.count('PATCH') == 1
    fake.patch_release.set()
    assert await task == 'quarantined'
    one = await command_state(sessions, command_one)
    two = await command_state(sessions, command_two)
    assert one['stage'] == 'cancelled'
    assert two['verified_generation'] is None
    assert fake.count('PATCH') == 1


@pytest.mark.asyncio
async def test_reclaiming_stale_sent_n_keeps_fence_and_blocks_n_plus_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = desired()
    identity_id, command_one, store = await make_command(sessions, first, source_key='stale-sent-one')
    claim = await store.claim_entitlement_command(command_one, worker='worker-one', now=NOW)
    await store.mark_mutation_sent(claim, stage=Stage.MUTATING, now=NOW)
    second = replace(first, generation=2, expire_at=first.expire_at + timedelta(days=30))
    async with sessions() as session, session.begin():
        _source_two, command_two = await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='test',
            source_key='stale-sent-two',
            source_fingerprint='7' * 64,
            snapshot=second,
        )

    reclaimed = await store.claim_entitlement_command(
        command_one,
        worker='worker-reclaim',
        now=NOW + timedelta(seconds=31),
    )
    assert reclaimed.mode == 'observe'
    assert (await command_state(sessions, command_one))['stage'] != 'cancelled'

    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(first, status='DISABLED'))
    assert (
        await coordinator(store, fake).project_command(
            command_two,
            second,
            worker='worker-two',
            now=NOW + timedelta(seconds=32),
        )
        == 'quarantined'
    )
    assert fake.count('PATCH') == 0
    fake.states['panel-existing'] = raw(first)  # late server-side N applies
    assert (
        await coordinator(store, fake).project_command(
            command_two,
            second,
            worker='worker-three',
            now=NOW + timedelta(seconds=64),
        )
        == 'quarantined'
    )
    assert fake.count('PATCH') == 0
    assert (await command_state(sessions, command_two))['verified_generation'] is None


@pytest.mark.asyncio
async def test_concurrent_cross_identity_uuid_bind_quarantines_both(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = desired(panel_uuid=None, owner_key='owner-one')
    second = desired(panel_uuid=None, owner_key='owner-two')
    identity_one, command_one, store = await make_command(sessions, first, source_key='bind-one')
    identity_two, command_two, _ = await make_command(sessions, second, source_key='bind-two')
    claim_one = await store.claim_entitlement_command(command_one, worker='one', now=NOW)
    claim_two = await store.claim_entitlement_command(command_two, worker='two', now=NOW)
    await store.mark_mutation_sent(claim_one, stage=Stage.CREATING_DISABLED, now=NOW)
    await store.mark_mutation_sent(claim_two, stage=Stage.CREATING_DISABLED, now=NOW)

    outcomes = await asyncio.gather(
        store.bind_uuid(
            claim_one,
            'shared-panel-uuid',
            panel_uuid_hmac='1' * 64,
            encrypted_cleanup_panel_uuid=b'encrypted-one',
            bound_desired_hash=first.bind('shared-panel-uuid').desired_hash,
            now=NOW,
        ),
        store.bind_uuid(
            claim_two,
            'shared-panel-uuid',
            panel_uuid_hmac='1' * 64,
            encrypted_cleanup_panel_uuid=b'encrypted-two',
            bound_desired_hash=second.bind('shared-panel-uuid').desired_hash,
            now=NOW,
        ),
    )
    assert sorted(outcomes) == [False, True]
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    'SELECT id, panel_uuid, lifecycle_state FROM entitlement_identities '
                    'WHERE id IN (:one, :two) ORDER BY id'
                ),
                {'one': identity_one, 'two': identity_two},
            )
        ).all()
    assert sum(row.panel_uuid == 'shared-panel-uuid' for row in rows) == 1
    assert {row.lifecycle_state for row in rows} == {'quarantined'}


@pytest.mark.asyncio
async def test_expired_lease_while_old_worker_alive_allows_observation_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(target, status='DISABLED'))
    fake.patch_release.clear()
    old = asyncio.create_task(coordinator(store, fake).project_command(command_id, target, worker='old', now=NOW))
    await asyncio.wait_for(fake.patch_received.wait(), timeout=2)
    assert (
        await coordinator(store, fake).project_command(
            command_id,
            target,
            worker='takeover',
            now=NOW + timedelta(seconds=31),
        )
        == 'quarantined'
    )
    assert fake.count('PATCH') == 1
    fake.patch_release.set()
    with pytest.raises(RuntimeError, match='verify_fence_lost'):
        await old
    assert (await command_state(sessions, command_id))['verified_generation'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('barrier_source', 'status'),
    [
        ('reset', 'DISABLED'),
        ('delete', 'DISABLED'),
        ('channel_deny', 'DISABLED'),
        ('limited', 'LIMITED'),
        ('erasure', 'DISABLED'),
        ('traffic_addon', 'ACTIVE'),
        ('device_addon', 'ACTIVE'),
        ('expiry', 'EXPIRED'),
    ],
)
async def test_new_generation_barriers_cancel_stale_finalize(
    sessions: async_sessionmaker[AsyncSession],
    barrier_source: str,
    status: str,
) -> None:
    first = desired()
    identity_id, command_one, store = await make_command(sessions, first, source_key='first')
    claim = await store.claim_entitlement_command(command_one, worker='old', now=NOW)
    await store.mark_mutation_sent(claim, stage=Stage.MUTATING, now=NOW)
    await store.mark_verifying(claim, now=NOW)
    second = replace(first, generation=2, status=status, expire_at=first.expire_at + timedelta(days=1))
    async with sessions() as session, session.begin():
        await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type=barrier_source,
            source_key=f'{barrier_source}-two',
            source_fingerprint='b' * 64,
            snapshot=second,
        )
    assert not await store.finalize_entitlement_command(claim, compare_snapshots(first, first), now=NOW)
    assert (await command_state(sessions, command_one))['stage'] == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('barrier_source', 'status'),
    [
        ('reset', 'DISABLED'),
        ('delete', 'DISABLED'),
        ('channel_deny', 'DISABLED'),
        ('limited', 'LIMITED'),
    ],
)
@pytest.mark.parametrize(
    'barrier',
    ['after_intent', 'after_patch_send_fence', 'after_active_patch', 'after_canonical_get', 'before_final_commit'],
)
async def test_deny_generation_at_every_remote_barrier_never_allows_stale_ready(
    sessions: async_sessionmaker[AsyncSession],
    barrier_source: str,
    status: str,
    barrier: str,
) -> None:
    first = desired()
    identity_id, command_one, store = await make_command(sessions, first, source_key='barrier-first')
    second = replace(first, generation=2, status=status, expire_at=first.expire_at + timedelta(days=1))
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(first, status='DISABLED'))
    injected = False

    async def inject(name: str) -> None:
        nonlocal injected
        if name != barrier or injected:
            return
        injected = True
        async with sessions() as session, session.begin():
            await append_source_and_command(
                session,
                identity_id=identity_id,
                source_type=barrier_source,
                source_key=f'{barrier_source}-{barrier}',
                source_fingerprint=hashlib.sha256(f'{barrier_source}:{barrier}'.encode()).hexdigest(),
                snapshot=second,
            )

    first_result = await coordinator(store, fake, inject).project_command(
        command_one,
        first,
        worker='generation-one',
        now=NOW,
    )
    first_state = await command_state(sessions, command_one)
    assert first_result in {'cancelled', 'quarantined'}
    assert first_state['verified_generation'] is None
    if barrier == 'after_intent':
        assert fake.count('PATCH') == 0
    async with sessions() as session:
        command_two = (
            await session.execute(
                text('SELECT id FROM entitlement_projection_commands WHERE identity_id=:identity_id AND generation=2'),
                {'identity_id': identity_id},
            )
        ).scalar_one()
    assert (
        await coordinator(store, fake).project_command(
            command_two,
            second,
            worker='generation-two',
            now=NOW + timedelta(seconds=31),
        )
        == 'ready'
    )
    assert (await command_state(sessions, command_two))['verified_generation'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'barrier',
    ['after_intent', 'after_patch_send_fence', 'after_active_patch', 'after_canonical_get', 'before_final_commit'],
)
async def test_erasure_at_every_remote_barrier_severs_pii_and_never_allows_stale_ready(
    sessions: async_sessionmaker[AsyncSession],
    barrier: str,
) -> None:
    first = desired()
    identity_id, command_id, store = await make_command(sessions, first, source_key='erasure-barrier-first')
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(first, status='DISABLED'))
    operation_id: str | None = None

    async def erase(name: str) -> None:
        nonlocal operation_id
        if name != barrier or operation_id is not None:
            return
        async with sessions() as session, session.begin():
            operation_id = await request_erasure_cleanup(
                session,
                identity_id=identity_id,
                secret=SECRET,
                now=NOW + timedelta(seconds=1),
            )

    try:
        result = await coordinator(store, fake, erase).project_command(
            command_id,
            first,
            worker='generation-one',
            now=NOW,
        )
        assert result in {'cancelled', 'quarantined'}
    except RuntimeError as exc:
        assert str(exc) == 'verify_fence_lost'
    assert operation_id is not None
    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT user_id, panel_uuid, generation, verified_generation, lifecycle_state '
                    'FROM entitlement_identities WHERE id=:identity_id'
                ),
                {'identity_id': identity_id},
            )
        ).one()
        cleanup = (
            await session.execute(
                text(
                    'SELECT remote_outcome_unknown, encrypted_panel_uuid '
                    'FROM entitlement_cleanup_commands WHERE operation_id=:operation_id'
                ),
                {'operation_id': operation_id},
            )
        ).one()
        assert identity.user_id is None and identity.panel_uuid is None
        assert identity.generation == 2 and identity.verified_generation is None
        assert identity.lifecycle_state == 'erasure_requested'
        assert cleanup.encrypted_panel_uuid is not None
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 0
        assert (await session.execute(text('SELECT count(*) FROM entitlement_observations'))).scalar_one() == 0
    if barrier in {'after_patch_send_fence', 'after_active_patch'}:
        assert cleanup.remote_outcome_unknown
    else:
        assert not cleanup.remote_outcome_unknown


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_state', ['erasure_requested', 'cleanup_terminal', 'final_erasure'])
async def test_append_after_erasure_fails_closed_without_reintroducing_pii(
    sessions: async_sessionmaker[AsyncSession],
    terminal_state: str,
) -> None:
    panel_uuid = 'panel-existing' if terminal_state == 'erasure_requested' else None
    first = desired(panel_uuid=panel_uuid)
    identity_id, _command_id, _store = await make_command(
        sessions,
        first,
        source_key=f'erasure-append-{terminal_state}',
    )
    async with sessions() as session, session.begin():
        await request_erasure_cleanup(session, identity_id=identity_id, secret=SECRET, now=NOW)
        if terminal_state == 'final_erasure':
            await mark_final_erasure(session, identity_id=identity_id, now=NOW + timedelta(seconds=1))

    stale_snapshot = replace(first, generation=3)
    with pytest.raises(ValueError, match='identity_erasure_in_progress_or_complete'):
        async with sessions() as session, session.begin():
            await append_source_and_command(
                session,
                identity_id=identity_id,
                source_type='financial',
                source_key=f'post-erasure-{terminal_state}',
                source_fingerprint=hashlib.sha256(terminal_state.encode()).hexdigest(),
                snapshot=stale_snapshot,
            )

    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT generation, lifecycle_state, user_id, panel_uuid FROM entitlement_identities WHERE id=:id'
                ),
                {'id': identity_id},
            )
        ).one()
        snapshots = list(
            (
                await session.execute(
                    text('SELECT desired_snapshot::text FROM entitlement_source_revisions WHERE identity_id=:id'),
                    {'id': identity_id},
                )
            ).scalars()
        )
        pending = (
            await session.execute(
                text("SELECT count(*) FROM entitlement_projection_commands WHERE identity_id=:id AND stage='pending'"),
                {'id': identity_id},
            )
        ).scalar_one()
        assert identity == (2, terminal_state, None, None)
        assert snapshots == [] and pending == 0


@pytest.mark.asyncio
async def test_append_waiting_on_identity_lock_stops_after_erasure_commit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = desired()
    identity_id, _command_id, _store = await make_command(sessions, first, source_key='stale-erasure-waiter')
    stale_snapshot = replace(first, generation=3)

    async with sessions() as eraser:
        erasure_transaction = await eraser.begin()
        await request_erasure_cleanup(eraser, identity_id=identity_id, secret=SECRET, now=NOW)

        async def stale_append() -> tuple[int, int]:
            async with sessions() as session, session.begin():
                return await append_source_and_command(
                    session,
                    identity_id=identity_id,
                    source_type='financial',
                    source_key='stale-waiter-after-erasure',
                    source_fingerprint='f' * 64,
                    snapshot=stale_snapshot,
                )

        waiter = asyncio.create_task(stale_append())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        await erasure_transaction.commit()
        with pytest.raises(ValueError, match='identity_erasure_in_progress_or_complete'):
            await waiter

    async with sessions() as session:
        identity = (
            await session.execute(
                text(
                    'SELECT generation, lifecycle_state, user_id, panel_uuid FROM entitlement_identities WHERE id=:id'
                ),
                {'id': identity_id},
            )
        ).one()
        assert identity == (2, 'erasure_requested', None, None)
        assert (
            await session.execute(
                text('SELECT count(*) FROM entitlement_source_revisions WHERE identity_id=:id'),
                {'id': identity_id},
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM entitlement_projection_commands WHERE identity_id=:id AND stage='pending'"),
                {'id': identity_id},
            )
        ).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('candidates', ['multiple', 'foreign'])
async def test_shared_or_cross_owner_deterministic_candidate_quarantines(
    sessions: async_sessionmaker[AsyncSession],
    candidates: str,
) -> None:
    target = desired(panel_uuid=None)
    _identity, command_id, store = await make_command(sessions, target)
    fake = FakePanelTransport()
    fake.create_mode = 'lost_without_apply'
    await coordinator(store, fake).project_command(command_id, target, worker='one', now=NOW)
    candidate = raw(target.bind('foreign'))
    if candidates == 'foreign':
        candidate['username'] = 'different-owner'
        fake.lookup_override = [candidate]
    else:
        fake.lookup_override = [candidate, {**candidate, 'uuid': 'foreign-two'}]
    await coordinator(store, fake).project_command(
        command_id,
        target,
        worker='two',
        now=NOW + timedelta(seconds=31),
    )
    assert fake.count('POST') == 1 and fake.count('PATCH') == 0
    assert (await command_state(sessions, command_id))['verified_generation'] is None


@pytest.mark.asyncio
async def test_owner_mismatch_sentinel_collision_cannot_become_ready(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired(owner_key='owner-mismatch')
    _identity_id, command_id, store = await make_command(
        sessions,
        target,
        source_key='owner-sentinel-collision',
    )
    fake = FakePanelTransport()
    fake.states['panel-existing'] = raw(replace(target, status='DISABLED'))
    fake.patch_mode = 'ack_without_apply'
    fake.states['panel-existing']['username'] = panel_owner_username('different-owner')
    assert await coordinator(store, fake).project_command(command_id, target, worker='worker', now=NOW) == 'quarantined'
    state = await command_state(sessions, command_id)
    assert state['verified_generation'] is None
    assert state['last_error_code'] == 'canonical_contract_invalid'


@pytest.mark.asyncio
async def test_duplicate_reordered_webhooks_do_not_change_desired_source(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    identity_id, _command_id, _store = await make_command(sessions, target)
    async with sessions() as session, session.begin():
        assert await ingest_webhook_observation(
            session,
            identity_id=identity_id,
            event_id_hash='a' * 64,
            event_type='user.modified',
            normalized_hash='b' * 64,
            event_timestamp=NOW + timedelta(minutes=2),
            now=NOW + timedelta(minutes=3),
        )
        assert not await ingest_webhook_observation(
            session,
            identity_id=identity_id,
            event_id_hash='a' * 64,
            event_type='user.modified',
            normalized_hash='b' * 64,
            event_timestamp=NOW + timedelta(minutes=2),
            now=NOW + timedelta(minutes=4),
        )
        assert await ingest_webhook_observation(
            session,
            identity_id=identity_id,
            event_id_hash='c' * 64,
            event_type='user.modified',
            normalized_hash='d' * 64,
            event_timestamp=NOW,
            now=NOW + timedelta(minutes=5),
        )
    async with sessions() as session:
        assert (await session.execute(text('SELECT count(*) FROM entitlement_webhook_inbox'))).scalar_one() == 2
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 1
        stored_hash = (
            await session.execute(text('SELECT desired_hash FROM entitlement_source_revisions'))
        ).scalar_one()
        assert stored_hash == target.desired_hash


@pytest.mark.asyncio
async def test_webhook_after_erasure_cannot_relink_identity(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    identity_id, _command_id, _store = await make_command(sessions, target, source_key='webhook-erasure')
    async with sessions() as session, session.begin():
        await request_erasure_cleanup(session, identity_id=identity_id, secret=SECRET, now=NOW)
        assert await ingest_webhook_observation(
            session,
            identity_id=identity_id,
            event_id_hash='e' * 64,
            event_type='user.modified',
            normalized_hash='f' * 64,
            event_timestamp=NOW,
            now=NOW + timedelta(seconds=1),
        )
    async with sessions() as session:
        linked_identity = (
            await session.execute(
                text('SELECT identity_id FROM entitlement_webhook_inbox WHERE event_id_hash=:event_id_hash'),
                {'event_id_hash': 'e' * 64},
            )
        ).scalar_one_or_none()
        assert linked_identity is None


@pytest.mark.asyncio
async def test_duplicate_provider_callback_is_one_source_and_stale_notification_is_cancelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = desired()
    identity_id, command_one, store = await make_command(sessions, first, source_key='provider-one')
    claim = await store.claim_entitlement_command(command_one, worker='one', now=NOW)
    await store.mark_mutation_sent(claim, stage=Stage.MUTATING, now=NOW)
    await store.mark_verifying(claim, now=NOW)
    assert await store.finalize_entitlement_command(claim, compare_snapshots(first, first), now=NOW)
    second = replace(first, generation=2, expire_at=first.expire_at + timedelta(days=30))
    async with sessions() as session, session.begin():
        first_ids = await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='provider_callback',
            source_key='provider-two',
            source_fingerprint='e' * 64,
            snapshot=second,
        )
    async with sessions() as session, session.begin():
        duplicate_ids = await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='provider_callback',
            source_key='provider-two',
            source_fingerprint='e' * 64,
            snapshot=second,
        )
    assert first_ids == duplicate_ids
    async with sessions() as session:
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 2
        assert (await session.execute(text('SELECT count(*) FROM entitlement_projection_commands'))).scalar_one() == 2
        old_state = (
            await session.execute(
                text('SELECT state FROM entitlement_notification_intents WHERE identity_id=:id AND generation=1'),
                {'id': identity_id},
            )
        ).scalar_one()
        assert old_state == 'cancelled'


@pytest.mark.asyncio
async def test_duplicate_source_key_with_changed_evidence_fails_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    identity_id, _command_id, _store = await make_command(sessions, target, source_key='provider-conflict')
    with pytest.raises(ValueError, match='source_provenance_conflict'):
        async with sessions() as session, session.begin():
            await append_source_and_command(
                session,
                identity_id=identity_id,
                source_type='test',
                source_key='provider-conflict',
                source_fingerprint='changed-fingerprint',
                snapshot=target,
            )
    async with sessions() as session:
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 1
        assert (await session.execute(text('SELECT generation FROM entitlement_identities'))).scalar_one() == 1


@pytest.mark.asyncio
async def test_financial_transaction_rollback_leaves_zero_source_and_command(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    target = desired()
    async with sessions() as session, session.begin():
        identity_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_identities(operation_id, deterministic_owner_key, panel_uuid)
                    VALUES (:op, :owner, :panel_uuid)
                    RETURNING id
                    """
                ),
                {'op': str(uuid.uuid4()), 'owner': target.owner_key, 'panel_uuid': target.panel_uuid},
            )
        ).scalar_one()
    async with sessions() as session:
        transaction = await session.begin()
        await append_source_and_command(
            session,
            identity_id=identity_id,
            source_type='financial',
            source_key='rolled-back',
            source_fingerprint='f' * 64,
            snapshot=target,
        )
        await transaction.rollback()
    async with sessions() as session:
        assert (await session.execute(text('SELECT count(*) FROM entitlement_source_revisions'))).scalar_one() == 0
        assert (await session.execute(text('SELECT count(*) FROM entitlement_projection_commands'))).scalar_one() == 0


@pytest.mark.asyncio
async def test_canonical_lock_order_has_no_deadlock_and_preserves_ap_term(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    grant_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    inventory_fingerprint = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
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
        subscription_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO subscriptions(user_id, end_date, is_daily_paused, remnawave_short_id)
                    VALUES (:user_id, :end_date, false, :short_id) RETURNING id
                    """
                ),
                {
                    'user_id': user_id,
                    'end_date': NOW + timedelta(days=30),
                    'short_id': uuid.uuid4().hex[:16],
                },
            )
        ).scalar_one()
        identity_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO entitlement_identities(operation_id, user_id, deterministic_owner_key)
                    VALUES (:op, :user_id, :owner) RETURNING id
                    """
                ),
                {'op': str(uuid.uuid4()), 'user_id': user_id, 'owner': f'owner-{user_id}'},
            )
        ).scalar_one()
        term_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO subscription_entitlement_terms
                           (subscription_id, term_version, starts_at, ends_at, access_point_ids,
                            technical_squad_keys, policy_revision, inventory_fingerprint,
                            provenance, grant_hash)
                    VALUES (:subscription_id, 1, :starts_at, :ends_at, '[]', '[]', 1,
                            :fingerprint, 'synthetic_ap', :grant_hash)
                    RETURNING id
                    """
                ),
                {
                    'subscription_id': subscription_id,
                    'starts_at': NOW,
                    'ends_at': NOW + timedelta(days=30),
                    'fingerprint': inventory_fingerprint,
                    'grant_hash': grant_hash,
                },
            )
        ).scalar_one()

    first_locked = asyncio.Event()

    async def workflow(delay: float) -> None:
        async with sessions() as session, session.begin():
            await lock_entitlement_context(
                session,
                payment_attempt_id=None,
                user_id=user_id,
                subscription_id=subscription_id,
                identity_id=identity_id,
            )
            first_locked.set()
            await asyncio.sleep(delay)

    one = asyncio.create_task(workflow(0.15))
    await asyncio.wait_for(first_locked.wait(), timeout=2)
    two = asyncio.create_task(workflow(0))
    await asyncio.wait_for(asyncio.gather(one, two), timeout=3)
    async with sessions() as session:
        term = (
            await session.execute(
                text('SELECT provenance, grant_hash FROM subscription_entitlement_terms WHERE id=:id'),
                {'id': term_id},
            )
        ).one()
        assert term == ('synthetic_ap', grant_hash)
    async with sessions() as session, session.begin():
        await session.execute(
            text('DELETE FROM subscription_entitlement_terms WHERE id=:id'),
            {'id': term_id},
        )
        await session.execute(text('DELETE FROM users WHERE id=:id'), {'id': user_id})

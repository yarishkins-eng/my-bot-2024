from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.utils.security import encrypt_restricted_identifier, hmac_fingerprint

from .persistence import CommandClaim, PostgresEntitlementStore
from .state_machine import Stage
from .strict_panel import (
    PanelReadError,
    RemoteOutcomeUnknown,
    StrictPanelClient,
    panel_owner_username,
    panel_username_owns,
)
from .types import EntitlementSnapshot, compare_snapshots


Failpoint = Callable[[str], Awaitable[None]]


def normalize_panel_observation(
    value: Mapping[str, Any],
    *,
    expected: EntitlementSnapshot,
) -> EntitlementSnapshot:
    squads = value.get('activeInternalSquads')
    if isinstance(squads, list):
        squads = [item.get('uuid') if isinstance(item, Mapping) else item for item in squads]
    remote_owner = value.get('username')
    if not panel_username_owns(remote_owner, expected.owner_key):
        raise ValueError('canonical owner proof mismatch')
    return EntitlementSnapshot.from_mapping(
        {
            # RemnaWave 2.8.1 canonical user DTO exposes ``username``.  Gate 1
            # reserves a deterministic non-PII username as the owner marker.
            'owner_key': expected.owner_key,
            'panel_uuid': value.get('uuid'),
            'status': value.get('status'),
            'expire_at': value.get('expireAt'),
            'traffic_limit_bytes': value.get('trafficLimitBytes'),
            'traffic_limit_strategy': value.get('trafficLimitStrategy'),
            'hwid_device_limit': value.get('hwidDeviceLimit'),
            'internal_squads': squads,
            'external_squad_uuid': value.get('externalSquadUuid'),
            # Panel cannot own these fields.  They are copied from the local
            # generation only after the explicit owner field is read back.
            'provenance': expected.provenance,
            'generation': expected.generation,
            'reset_epoch': expected.reset_epoch,
            'revoke_epoch': expected.revoke_epoch,
            'deny_overlays': expected.deny_overlays,
        }
    )


class ProjectionCoordinator:
    def __init__(
        self,
        store: PostgresEntitlementStore,
        panel: StrictPanelClient,
        *,
        fingerprint_secret: str,
        failpoint: Failpoint | None = None,
    ) -> None:
        self._store = store
        self._panel = panel
        self._secret = fingerprint_secret
        self._failpoint = failpoint

    async def _hit(self, name: str) -> None:
        if self._failpoint:
            await self._failpoint(name)

    async def _observe_only(
        self,
        claim: CommandClaim,
        desired: EntitlementSnapshot,
        *,
        now: datetime,
    ) -> str:
        try:
            if claim.panel_uuid:
                raw = await self._panel.get_canonical(claim.panel_uuid)
                candidates = [raw] if raw is not None else []
            elif claim.deterministic_create_key:
                candidates = await self._panel.find_by_deterministic_username(panel_owner_username(desired.owner_key))
            else:
                candidates = []
        except PanelReadError:
            await self._store.quarantine(claim, code='observe_read_failed', unknown=True, now=now)
            return 'quarantined'
        if len(candidates) != 1:
            await self._store.quarantine(
                claim,
                code='deterministic_lookup_not_exactly_one',
                unknown=True,
                now=now,
            )
            return 'quarantined'
        try:
            observed = normalize_panel_observation(candidates[0], expected=desired.bind(str(candidates[0]['uuid'])))
        except (KeyError, TypeError, ValueError):
            await self._store.quarantine(claim, code='observation_contract_invalid', unknown=True, now=now)
            return 'quarantined'
        comparison = compare_snapshots(desired.bind(observed.panel_uuid or ''), observed)
        await self._store.record_observation(claim, observed, comparison, event_type='takeover_observe', now=now)
        # RemnaWave has no CAS/late-write exclusion.  Even an exact observation
        # cannot clear an unknown mutation automatically.
        await self._store.quarantine(claim, code='unknown_requires_operator_resolution', unknown=True, now=now)
        return 'quarantined'

    async def project_command(
        self,
        command_id: int,
        desired: EntitlementSnapshot,
        *,
        worker: str,
        now: datetime | None = None,
    ) -> str:
        clock = now or datetime.now(UTC)
        claim = await self._store.claim_entitlement_command(command_id, worker=worker, now=clock)
        if claim.mode == 'busy':
            return 'busy'
        if claim.mode == 'invalid' or claim.desired_snapshot is None:
            return 'quarantined'
        provided = desired.bind(claim.panel_uuid) if claim.panel_uuid else desired
        if provided.desired_hash != claim.desired_snapshot.desired_hash:
            await self._store.quarantine(claim, code='caller_desired_mismatch', unknown=False, now=clock)
            return 'quarantined'
        desired = claim.desired_snapshot
        if claim.mode == 'observe':
            return await self._observe_only(claim, desired, now=clock)
        await self._hit('after_intent')

        bound = desired.bind(claim.panel_uuid) if claim.panel_uuid else desired
        if bound.panel_uuid is None:
            if not claim.deterministic_create_key:
                await self._store.quarantine(claim, code='missing_create_intent', unknown=False, now=clock)
                return 'quarantined'
            try:
                await self._store.mark_mutation_sent(claim, stage=Stage.CREATING_DISABLED, now=clock)
            except RuntimeError as exc:
                if str(exc) != 'mutation_fence_lost':
                    raise
                return 'cancelled'
            await self._hit('after_create_send_fence')
            try:
                receipt = await self._panel.create_disabled(
                    desired,
                    panel_owner_username(desired.owner_key),
                )
            except RemoteOutcomeUnknown:
                await self._store.quarantine(claim, code='create_outcome_unknown', unknown=True, now=clock)
                return 'quarantined'
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._store.quarantine(claim, code='create_cancelled_unknown', unknown=True, now=clock)
                )
                raise
            await self._hit('after_create_post')
            bound_uuid = await self._store.bind_uuid(
                claim,
                receipt.panel_uuid,
                panel_uuid_hmac=hmac_fingerprint(
                    receipt.panel_uuid,
                    secret=self._secret,
                    purpose='entitlement-panel-uuid',
                ),
                encrypted_cleanup_panel_uuid=encrypt_restricted_identifier(
                    receipt.panel_uuid,
                    secret=self._secret,
                    purpose='entitlement-panel-cleanup-v1',
                ),
                bound_desired_hash=desired.bind(receipt.panel_uuid).desired_hash,
                now=clock,
            )
            if not bound_uuid:
                return 'quarantined'
            await self._hit('after_uuid_bind')
            bound = desired.bind(receipt.panel_uuid)
            claim = replace(claim, panel_uuid=receipt.panel_uuid, desired_snapshot=bound)

        try:
            await self._store.mark_mutation_sent(claim, stage=Stage.MUTATING, now=clock)
        except RuntimeError as exc:
            if str(exc) != 'mutation_fence_lost':
                raise
            return 'cancelled'
        await self._hit('after_patch_send_fence')
        try:
            await self._panel.patch_exact(bound)
        except RemoteOutcomeUnknown:
            await self._store.quarantine(claim, code='patch_outcome_unknown', unknown=True, now=clock)
            return 'quarantined'
        except asyncio.CancelledError:
            await asyncio.shield(self._store.quarantine(claim, code='patch_cancelled_unknown', unknown=True, now=clock))
            raise
        await self._hit('after_active_patch')
        await self._store.mark_verifying(claim, now=clock)

        try:
            raw = await self._panel.get_canonical(bound.panel_uuid or '')
        except PanelReadError:
            await self._store.quarantine(claim, code='canonical_get_failed', unknown=False, now=clock)
            return 'quarantined'
        if raw is None:
            await self._store.quarantine(claim, code='canonical_get_missing', unknown=False, now=clock)
            return 'quarantined'
        try:
            observed = normalize_panel_observation(raw, expected=bound)
        except (TypeError, ValueError):
            await self._store.quarantine(claim, code='canonical_contract_invalid', unknown=False, now=clock)
            return 'quarantined'
        comparison = compare_snapshots(bound, observed)
        await self._store.record_observation(claim, observed, comparison, event_type='post_mutation_verify', now=clock)
        await self._hit('after_canonical_get')
        if not comparison.exact:
            await self._store.quarantine(claim, code='canonical_mismatch', unknown=False, now=clock)
            return 'quarantined'
        await self._hit('before_final_commit')
        return (
            'ready' if await self._store.finalize_entitlement_command(claim, comparison, now=clock) else 'quarantined'
        )

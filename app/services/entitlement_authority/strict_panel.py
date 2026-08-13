from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .types import EntitlementSnapshot


class RemoteOutcomeUnknown(RuntimeError):
    """A mutating request may have reached Panel; automatic mutation must stop."""


class PanelReadError(RuntimeError):
    """A read failed; it never authorizes READY or CREATE."""


def panel_owner_username(owner_key: str) -> str:
    """Map an internal non-PII owner key to RemnaWave's 3..36 username."""

    if not owner_key:
        raise ValueError('owner key must be non-empty')
    return f'te-{hashlib.sha256(owner_key.encode()).hexdigest()[:32]}'


def panel_username_owns(username: object, owner_key: str) -> bool:
    return isinstance(username, str) and hmac.compare_digest(username, panel_owner_username(owner_key))


class StrictPanelTransport(Protocol):
    async def request_once(
        self, method: str, endpoint: str, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """Send exactly one request.  Implementations must not retry mutations."""


@dataclass(frozen=True, slots=True)
class CreateReceipt:
    panel_uuid: str


class StrictPanelClient:
    """Narrow Gate-1 Panel contract used only by fake/test adapters for now.

    There is deliberately no production ``RemnaWaveAPI`` adapter in Gate 1.
    A future cutover must add one behind a separate owner gate and prove that
    ``request_once`` does not delegate to the legacy mutating retry loop.
    """

    def __init__(self, transport: StrictPanelTransport) -> None:
        self._transport = transport

    @staticmethod
    def _payload(snapshot: EntitlementSnapshot, *, status: str | None = None) -> dict[str, Any]:
        return {
            'uuid': snapshot.panel_uuid,
            # RemnaWave 2.8.1 exposes ``username`` on canonical user reads;
            # there is no ``ownerKey`` field.  The non-PII deterministic
            # username is therefore the exact remote owner proof.
            'username': panel_owner_username(snapshot.owner_key),
            'status': status or snapshot.status,
            'expireAt': snapshot.expire_at.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
            'trafficLimitBytes': snapshot.traffic_limit_bytes,
            'trafficLimitStrategy': snapshot.traffic_limit_strategy,
            'hwidDeviceLimit': snapshot.hwid_device_limit,
            'activeInternalSquads': list(snapshot.internal_squads),
            'externalSquadUuid': snapshot.external_squad_uuid,
        }

    async def get_canonical(self, panel_uuid: str) -> Mapping[str, Any] | None:
        try:
            response = await self._transport.request_once('GET', f'/api/users/{panel_uuid}')
        except Exception as exc:
            raise PanelReadError('canonical_get_failed') from exc
        value = response.get('response')
        return value if isinstance(value, Mapping) else None

    async def find_by_deterministic_username(self, username: str) -> list[Mapping[str, Any]]:
        try:
            response = await self._transport.request_once('GET', f'/api/users/by-username/{username}')
        except Exception as exc:
            raise PanelReadError('deterministic_lookup_failed') from exc
        value = response.get('response')
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [value]
        if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
            return value
        raise PanelReadError('deterministic_lookup_contract_invalid')

    async def create_disabled(self, snapshot: EntitlementSnapshot, deterministic_username: str) -> CreateReceipt:
        if snapshot.panel_uuid is not None:
            raise ValueError('CREATE requires an unbound snapshot')
        if deterministic_username != panel_owner_username(snapshot.owner_key):
            raise ValueError('CREATE owner proof must equal the desired deterministic username')
        payload = self._payload(snapshot, status='DISABLED')
        payload.pop('uuid')
        try:
            response = await self._transport.request_once('POST', '/api/users', payload)
        except Exception as exc:
            raise RemoteOutcomeUnknown('create_outcome_unknown') from exc
        body = response.get('response')
        panel_uuid = body.get('uuid') if isinstance(body, Mapping) else None
        if not panel_uuid:
            raise RemoteOutcomeUnknown('create_response_missing_uuid')
        return CreateReceipt(str(panel_uuid))

    async def patch_exact(self, snapshot: EntitlementSnapshot) -> None:
        if not snapshot.panel_uuid:
            raise ValueError('PATCH requires a durably bound Panel UUID')
        try:
            await self._transport.request_once('PATCH', '/api/users', self._payload(snapshot))
        except Exception as exc:
            raise RemoteOutcomeUnknown('patch_outcome_unknown') from exc

    async def delete_once(self, panel_uuid: str) -> None:
        try:
            await self._transport.request_once('DELETE', f'/api/users/{panel_uuid}')
        except Exception as exc:
            raise RemoteOutcomeUnknown('delete_outcome_unknown') from exc

"""Read-only RemnaWave adapter for subscription-visible access points.

The adapter deliberately owns no Panel write operation.  It maps the
source-verified RemnaWave 2.8 Host relation
``Host.inbound.(configProfileUuid, configProfileInboundUuid)`` to the matching
``InternalSquad.inbounds.(profileUuid, uuid)`` and supplies only opaque
server-side evidence to the access-point inventory domain.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from hashlib import sha256

from app.config import settings
from app.external.remnawave_api import RemnaWaveAccessibleNode, RemnaWaveInternalSquad
from app.services.public_access_point_inventory import InventoryHost, InventorySnapshot, InventorySquad
from app.services.remnawave_service import RemnaWaveService


class RemnaWaveAccessPointInventoryError(RuntimeError):
    """The GET-only inventory evidence was absent or internally inconsistent."""


def _inbound_key(profile_uuid: str, inbound_uuid: str) -> tuple[str, str] | None:
    profile = profile_uuid.strip()
    inbound = inbound_uuid.strip()
    return (profile, inbound) if profile and inbound else None


def _opaque_raw_evidence(raw_inbound: object) -> str:
    """Keep a stable server-side identity without retaining raw JSON values."""

    try:
        payload = json.dumps(raw_inbound, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise RemnaWaveAccessPointInventoryError('Internal Squad raw evidence is not serialisable') from exc
    return sha256(payload.encode()).hexdigest()


class RemnaWaveAccessPointInventoryClient:
    """Typed adapter backed exclusively by RemnaWave GET endpoints."""

    _MAX_CONCURRENT_READS_HARD_LIMIT = 10
    _MAX_INVENTORY_RECORDS_HARD_LIMIT = 500

    def __init__(self, service: RemnaWaveService | None = None):
        self._service = service or RemnaWaveService()

    @property
    def is_configured(self) -> bool:
        return bool(self._service.is_configured)

    @staticmethod
    def _bounded_int(value: object, *, default: int, maximum: int) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_seconds(value: object, *, default: float, maximum: float) -> float:
        try:
            return max(1.0, min(float(value), maximum))
        except (TypeError, ValueError):
            return default

    async def read_access_point_inventory(self) -> InventorySnapshot:
        if not self._service.is_configured:
            raise RemnaWaveAccessPointInventoryError(
                self._service.configuration_error or 'RemnaWave API is not configured'
            )

        try:
            async with self._service.get_api_client() as api:
                hosts = await api.get_subscription_hosts()
                squads = await api.get_access_point_inventory_internal_squads()
                current_nodes = await api.get_access_point_inventory_nodes()
                max_hosts = self._bounded_int(
                    settings.ACCESS_POINT_INVENTORY_MAX_HOSTS,
                    default=100,
                    maximum=self._MAX_INVENTORY_RECORDS_HARD_LIMIT,
                )
                max_squads = self._bounded_int(
                    settings.ACCESS_POINT_INVENTORY_MAX_SQUADS,
                    default=100,
                    maximum=self._MAX_INVENTORY_RECORDS_HARD_LIMIT,
                )
                if len(hosts) > max_hosts or len(squads) > max_squads:
                    raise RemnaWaveAccessPointInventoryError('RemnaWave inventory exceeds the read safety limit')
                concurrent_reads = self._bounded_int(
                    settings.ACCESS_POINT_INVENTORY_MAX_CONCURRENT_READS,
                    default=4,
                    maximum=self._MAX_CONCURRENT_READS_HARD_LIMIT,
                )
                read_budget_seconds = self._bounded_seconds(
                    settings.ACCESS_POINT_INVENTORY_READ_BUDGET_SECONDS,
                    default=45.0,
                    maximum=60.0,
                )
                semaphore = asyncio.Semaphore(concurrent_reads)

                async def read_accessible_nodes(squad: RemnaWaveInternalSquad) -> list[RemnaWaveAccessibleNode]:
                    async with semaphore:
                        return await api.get_internal_squad_accessible_nodes(squad.uuid)

                accessibility = await asyncio.wait_for(
                    asyncio.gather(*(read_accessible_nodes(squad) for squad in squads)),
                    timeout=read_budget_seconds,
                )
        except Exception as exc:
            raise RemnaWaveAccessPointInventoryError('RemnaWave read-only inventory request failed') from exc

        node_by_key = {node.uuid: node for node in current_nodes}
        required_active_tags_by_squad = {
            squad.uuid: frozenset(
                inbound.tag for inbound in squad.inbounds if isinstance(inbound.tag, str) and inbound.tag.strip()
            )
            for squad in squads
        }

        def required_profile(squad: RemnaWaveInternalSquad) -> str | None:
            profile_ids = {inbound.profile_uuid for inbound in squad.inbounds if inbound.profile_uuid.strip()}
            return next(iter(profile_ids)) if len(profile_ids) == 1 else None

        required_profile_by_squad = {squad.uuid: required_profile(squad) for squad in squads}

        def is_currently_healthy(
            squad: RemnaWaveInternalSquad,
            accessible_nodes: list[RemnaWaveAccessibleNode],
        ) -> bool:
            """Fail closed unless every selected node is live and serving an inbound.

            ``accessible-nodes`` proves a configured graph, not node health.
            A missing, offline, disabled, connecting, or inbound-less node is
            therefore not eligible inventory evidence.  ``activeInbounds``
            uses RemnaWave inbound tags (source-verified in the production
            preflight), so it must exactly match this Squad's verified tags;
            an unrelated active inbound is not access evidence for this Host.
            """

            required_tags = required_active_tags_by_squad.get(squad.uuid, frozenset())
            required_profile = required_profile_by_squad.get(squad.uuid)
            return (
                bool(accessible_nodes)
                and bool(required_tags)
                and required_profile is not None
                and all(
                    (node := node_by_key.get(accessible.uuid)) is not None
                    and node.is_connected
                    and not node.is_disabled
                    and not node.is_connecting
                    and accessible.config_profile_uuid == required_profile
                    and isinstance(accessible.active_inbounds, list)
                    and all(isinstance(tag, str) and tag.strip() for tag in accessible.active_inbounds)
                    and frozenset(accessible.active_inbounds) == required_tags
                    for accessible in accessible_nodes
                )
            )

        healthy_by_squad = {
            squad.uuid: is_currently_healthy(squad, nodes) for squad, nodes in zip(squads, accessibility, strict=True)
        }
        squads_by_inbound: dict[tuple[str, str], set[str]] = defaultdict(set)
        inbound_keys_by_squad: dict[str, set[tuple[str, str]]] = {}
        raw_members_by_squad: dict[str, tuple[str, ...]] = {}
        for squad in squads:
            inbound_keys: set[tuple[str, str]] = set()
            raw_evidence: list[str] = []
            raw_evidence_complete = True
            for inbound in squad.inbounds:
                key = _inbound_key(inbound.profile_uuid, inbound.uuid)
                if key is None:
                    raw_evidence_complete = False
                    continue
                inbound_keys.add(key)
                squads_by_inbound[key].add(squad.uuid)
                if inbound.raw_inbound is None:
                    raw_evidence_complete = False
                else:
                    raw_evidence.append(_opaque_raw_evidence(inbound.raw_inbound))
            inbound_keys_by_squad[squad.uuid] = inbound_keys
            raw_members_by_squad[squad.uuid] = tuple(sorted(raw_evidence)) if raw_evidence_complete else ()

        host_key_by_inbound: dict[tuple[str, str], set[str]] = defaultdict(set)
        host_inbound_by_key: dict[str, tuple[str, str] | None] = {}
        for host in hosts:
            key = _inbound_key(host.config_profile_uuid, host.config_profile_inbound_uuid)
            host_inbound_by_key[host.uuid] = key
            if key is not None and host.uuid:
                host_key_by_inbound[key].add(host.uuid)

        host_keys_by_squad: dict[str, set[str]] = defaultdict(set)
        for inbound_key, squad_keys in squads_by_inbound.items():
            for squad_key in squad_keys:
                host_keys_by_squad[squad_key].update(host_key_by_inbound.get(inbound_key, set()))

        def is_dedicated_to_one_host(squad: RemnaWaveInternalSquad) -> bool:
            host_keys = host_keys_by_squad.get(squad.uuid, set())
            inbound_keys = inbound_keys_by_squad.get(squad.uuid, set())
            if len(host_keys) != 1 or not inbound_keys:
                return False
            only_host = next(iter(host_keys))
            host_inbound = host_inbound_by_key.get(only_host)
            # Any additional or unknown inbound could widen the entitlement
            # beyond the one visible Host title, so it is never "dedicated".
            return host_inbound is not None and inbound_keys == {host_inbound}

        # A legacy, shared Squad may legitimately retain the same inbound for
        # historical subscriptions.  It must remain visible in the source
        # snapshot (so drift still fences discovery), but it must not make a
        # separately proven dedicated Squad unsafe for a *new* access-point
        # tariff.  Exactly one structural dedicated mapping is required: zero
        # or more than one stays fail-closed as a missing mapping below.
        dedicated_squad_keys_by_host: dict[str, set[str]] = defaultdict(set)
        for squad in squads:
            if not is_dedicated_to_one_host(squad):
                continue
            host_keys = host_keys_by_squad.get(squad.uuid, set())
            # `is_dedicated_to_one_host` established this invariant above.
            dedicated_squad_keys_by_host[next(iter(host_keys))].add(squad.uuid)

        def selected_dedicated_squad_keys(host_key: str) -> tuple[str, ...]:
            keys = tuple(sorted(dedicated_squad_keys_by_host.get(host_key, set())))
            return keys if len(keys) == 1 else ()

        inventory_hosts = tuple(
            InventoryHost(
                host_key=host.uuid,
                title=host.remark,
                squad_keys=selected_dedicated_squad_keys(host.uuid),
                is_hidden=host.is_hidden,
                is_disabled=host.is_disabled,
            )
            for host in hosts
        )
        inventory_squads = tuple(
            InventorySquad(
                key=squad.uuid,
                is_dedicated=is_dedicated_to_one_host(squad),
                is_healthy=healthy_by_squad.get(squad.uuid, False),
                raw_members=raw_members_by_squad.get(squad.uuid, ()),
            )
            for squad in squads
        )
        # RemnaWave Hosts do not expose an ETag/revision in this endpoint.  The
        # caller takes two complete hashes and rejects any observed drift.
        return InventorySnapshot(revision=None, hosts=inventory_hosts, squads=inventory_squads)

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.config import settings
from app.external.remnawave_api import (
    RemnaWaveAccessibleNode,
    RemnaWaveInbound,
    RemnaWaveInternalSquad,
    RemnaWaveNode,
    RemnaWaveSubscriptionHost,
)
from app.services.public_access_point_inventory import assess_inventory
from app.services.remnawave_access_point_inventory import (
    RemnaWaveAccessPointInventoryClient,
    RemnaWaveAccessPointInventoryError,
)


class _ReadOnlyApi:
    def __init__(
        self,
        *,
        hosts: list[RemnaWaveSubscriptionHost],
        squads: list[RemnaWaveInternalSquad],
        accessible: dict[str, list[RemnaWaveAccessibleNode]],
        nodes: list[RemnaWaveNode],
        accessible_read_started: asyncio.Event | None = None,
        accessible_read_release: asyncio.Event | None = None,
    ):
        self.hosts = hosts
        self.squads = squads
        self.accessible = accessible
        self.nodes = nodes
        self.accessible_read_started = accessible_read_started
        self.accessible_read_release = accessible_read_release
        self.calls: list[str] = []
        self.accessible_reads_in_flight = 0
        self.max_accessible_reads_in_flight = 0

    async def get_subscription_hosts(self) -> list[RemnaWaveSubscriptionHost]:
        self.calls.append('GET /api/hosts')
        return self.hosts

    async def get_access_point_inventory_internal_squads(self) -> list[RemnaWaveInternalSquad]:
        self.calls.append('GET /api/internal-squads')
        return self.squads

    async def get_access_point_inventory_nodes(self) -> list[RemnaWaveNode]:
        self.calls.append('GET /api/nodes')
        return self.nodes

    async def get_internal_squad_accessible_nodes(self, squad_key: str) -> list[RemnaWaveAccessibleNode]:
        self.calls.append(f'GET /api/internal-squads/{squad_key}/accessible-nodes')
        self.accessible_reads_in_flight += 1
        self.max_accessible_reads_in_flight = max(
            self.max_accessible_reads_in_flight,
            self.accessible_reads_in_flight,
        )
        if self.accessible_read_started is not None:
            self.accessible_read_started.set()
        try:
            if self.accessible_read_release is not None:
                await self.accessible_read_release.wait()
            return self.accessible[squad_key]
        finally:
            self.accessible_reads_in_flight -= 1


class _Service:
    is_configured = True
    configuration_error = None

    def __init__(self, api: _ReadOnlyApi):
        self._api = api

    @asynccontextmanager
    async def get_api_client(self):
        yield self._api


def _host(host_key: str, title: str, inbound_key: str) -> RemnaWaveSubscriptionHost:
    return RemnaWaveSubscriptionHost(
        uuid=host_key,
        remark=title,
        config_profile_uuid='profile-a',
        config_profile_inbound_uuid=inbound_key,
        is_hidden=False,
        is_disabled=False,
    )


def _squad(squad_key: str, *inbound_keys: str) -> RemnaWaveInternalSquad:
    return RemnaWaveInternalSquad(
        uuid=squad_key,
        name='server-only-name',
        members_count=1,
        inbounds_count=len(inbound_keys),
        inbounds=[
            RemnaWaveInbound(
                uuid=inbound_key,
                profile_uuid='profile-a',
                tag=f'tag-{inbound_key}',
                type='vless',
                raw_inbound={'tag': f'raw-{inbound_key}', 'protocol': 'vless'},
            )
            for inbound_key in inbound_keys
        ],
    )


def _accessible_node(
    node_key: str,
    *,
    active_inbounds: list[str] | None = None,
    profile_uuid: str = 'profile-a',
) -> RemnaWaveAccessibleNode:
    return RemnaWaveAccessibleNode(
        uuid=node_key,
        node_name='server-only-node-name',
        country_code='PL',
        config_profile_uuid=profile_uuid,
        config_profile_name='server-only-profile-name',
        active_inbounds=active_inbounds if active_inbounds is not None else ['tag-inbound-pl'],
    )


def _node(
    node_key: str,
    *,
    is_connected: bool = True,
    is_disabled: bool = False,
    is_connecting: bool = False,
) -> RemnaWaveNode:
    return RemnaWaveNode(
        uuid=node_key,
        name='server-only-node-name',
        address='server-only-address',
        country_code='PL',
        is_connected=is_connected,
        is_disabled=is_disabled,
        is_connecting=is_connecting,
        users_online=0,
        traffic_used_bytes=None,
        traffic_limit_bytes=None,
    )


@pytest.mark.asyncio
async def test_adapter_maps_verified_dedicated_host_without_exposing_raw_evidence() -> None:
    api = _ReadOnlyApi(
        hosts=[_host('host-pl', 'Польша', 'inbound-pl')],
        squads=[_squad('squad-pl', 'inbound-pl')],
        accessible={'squad-pl': [_accessible_node('node-pl')]},
        nodes=[_node('node-pl')],
    )

    snapshot = await RemnaWaveAccessPointInventoryClient(_Service(api)).read_access_point_inventory()

    assert snapshot.hosts[0].title == 'Польша'
    assert snapshot.hosts[0].squad_keys == ('squad-pl',)
    assert snapshot.squads[0].is_dedicated is True
    assert snapshot.squads[0].is_healthy is True
    assert snapshot.squads[0].raw_members
    assert 'raw-inbound-pl' not in snapshot.squads[0].raw_members[0]
    assert assess_inventory(snapshot).points[0].assignable is True
    assert api.calls == [
        'GET /api/hosts',
        'GET /api/internal-squads',
        'GET /api/nodes',
        'GET /api/internal-squads/squad-pl/accessible-nodes',
    ]


@pytest.mark.asyncio
async def test_adapter_marks_shared_host_mapping_needs_verification() -> None:
    api = _ReadOnlyApi(
        hosts=[_host('host-pl', 'Польша', 'inbound-pl'), _host('host-pl-2', 'Польша 2', 'inbound-pl-2')],
        squads=[_squad('squad-shared', 'inbound-pl', 'inbound-pl-2')],
        accessible={'squad-shared': [_accessible_node('node-pl')]},
        nodes=[_node('node-pl')],
    )

    snapshot = await RemnaWaveAccessPointInventoryClient(_Service(api)).read_access_point_inventory()
    assessment = assess_inventory(snapshot)

    assert snapshot.squads[0].is_dedicated is False
    assert all(point.state == 'needs_verification' for point in assessment.points)
    assert all('shared_squad_mapping' in (point.reason or '') for point in assessment.points)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('nodes', 'accessible'),
    [
        ([_node('node-pl', is_connected=False)], [_accessible_node('node-pl')]),
        ([_node('node-pl', is_disabled=True)], [_accessible_node('node-pl')]),
        ([_node('node-pl', is_connecting=True)], [_accessible_node('node-pl')]),
        ([], [_accessible_node('unknown-node')]),
        ([_node('node-pl')], [_accessible_node('node-pl', active_inbounds=[])]),
        ([_node('node-pl')], [_accessible_node('node-pl', active_inbounds=['unrelated-inbound'])]),
        ([_node('node-pl')], [_accessible_node('node-pl', profile_uuid='unrelated-profile')]),
    ],
)
async def test_adapter_marks_unhealthy_or_unproven_node_evidence_nonassignable(
    nodes: list[RemnaWaveNode],
    accessible: list[RemnaWaveAccessibleNode],
) -> None:
    api = _ReadOnlyApi(
        hosts=[_host('host-pl', 'Польша', 'inbound-pl')],
        squads=[_squad('squad-pl', 'inbound-pl')],
        accessible={'squad-pl': accessible},
        nodes=nodes,
    )

    snapshot = await RemnaWaveAccessPointInventoryClient(_Service(api)).read_access_point_inventory()
    assessment = assess_inventory(snapshot)

    assert snapshot.squads[0].is_healthy is False
    assert assessment.points[0].assignable is False
    assert 'unhealthy_squad' in (assessment.points[0].reason or '')


@pytest.mark.asyncio
async def test_adapter_bounds_accessible_node_read_fanout(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_MAX_CONCURRENT_READS', 2)
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_READ_BUDGET_SECONDS', 5.0)
    release = asyncio.Event()
    api = _ReadOnlyApi(
        hosts=[_host(f'host-{index}', f'Точка {index}', f'inbound-{index}') for index in range(5)],
        squads=[_squad(f'squad-{index}', f'inbound-{index}') for index in range(5)],
        accessible={
            f'squad-{index}': [_accessible_node(f'node-{index}', active_inbounds=[f'tag-inbound-{index}'])]
            for index in range(5)
        },
        nodes=[_node(f'node-{index}') for index in range(5)],
        accessible_read_started=asyncio.Event(),
        accessible_read_release=release,
    )

    read_task = asyncio.create_task(RemnaWaveAccessPointInventoryClient(_Service(api)).read_access_point_inventory())
    await api.accessible_read_started.wait()
    await asyncio.sleep(0)

    assert api.max_accessible_reads_in_flight == 2
    release.set()
    await read_task


@pytest.mark.asyncio
async def test_adapter_rejects_oversized_inventory_before_accessible_node_fanout(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_MAX_HOSTS', 2)
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_MAX_SQUADS', 2)
    api = _ReadOnlyApi(
        hosts=[_host(f'host-{index}', f'Точка {index}', f'inbound-{index}') for index in range(3)],
        squads=[_squad(f'squad-{index}', f'inbound-{index}') for index in range(3)],
        accessible={f'squad-{index}': [_accessible_node(f'node-{index}')] for index in range(3)},
        nodes=[_node(f'node-{index}') for index in range(3)],
    )

    with pytest.raises(RemnaWaveAccessPointInventoryError, match='inventory'):
        await RemnaWaveAccessPointInventoryClient(_Service(api)).read_access_point_inventory()

    assert not any('accessible-nodes' in call for call in api.calls)

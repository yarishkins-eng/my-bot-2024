from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import server_squad
from app.services import public_location_entitlement_service as entitlement_service


@pytest.mark.asyncio
async def test_native_tariff_resolves_its_explicit_internal_squads(monkeypatch):
    available = AsyncMock(return_value=['de-squad', 'nl-squad'])
    monkeypatch.setattr(entitlement_service, 'get_effective_tariff_squad_uuids', available)
    tariff = SimpleNamespace(id=3, entitlement_mode='native_squads', allowed_squads=['de-squad', 'nl-squad'])
    db = SimpleNamespace()

    resolved = await entitlement_service.resolve_tariff_entitlement(db, tariff)

    assert resolved.squad_uuids == ('de-squad', 'nl-squad')
    assert resolved.provenance == 'native_squads'
    available.assert_awaited_once_with(db, tariff.allowed_squads)


@pytest.mark.asyncio
async def test_empty_native_tariff_retains_upstream_all_available_semantics(monkeypatch):
    available = AsyncMock(
        return_value=[
            SimpleNamespace(squad_uuid='de-squad'),
            SimpleNamespace(squad_uuid='nl-squad'),
        ]
    )
    monkeypatch.setattr(server_squad, 'get_available_server_squads', available)

    resolved = await server_squad.get_effective_tariff_squad_uuids(SimpleNamespace(), [])

    assert resolved == ['de-squad', 'nl-squad']


@pytest.mark.asyncio
async def test_native_subscription_keeps_its_issued_squads_not_a_legacy_snapshot():
    subscription = SimpleNamespace(id=101, tariff_id=3, connected_squads=['de-squad', 'nl-squad'])
    tariff = SimpleNamespace(id=3, entitlement_mode='native_squads')
    db = SimpleNamespace(get=AsyncMock(side_effect=[subscription, tariff]), scalar=AsyncMock())

    resolved = await entitlement_service.get_subscription_resolved_entitlement(db, subscription.id)

    assert resolved.squad_uuids == ('de-squad', 'nl-squad')
    assert resolved.provenance == 'native_squads'
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_issue_does_not_create_an_immutable_access_point_snapshot():
    entitlement = entitlement_service.ResolvedEntitlement((), ('de-squad',), 0, 'native_squads')

    result = await entitlement_service.persist_subscription_entitlement_snapshot(
        SimpleNamespace(), subscription_id=101, tariff_id=3, entitlement=entitlement
    )

    assert result is None

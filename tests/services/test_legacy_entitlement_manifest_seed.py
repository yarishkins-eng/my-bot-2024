from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import legacy_entitlement_manifest_seed
from app.services.legacy_entitlement_manifest_seed import (
    LegacyEntitlementSeedError,
    _ApprovedSharedSquadInventory,
    _raw_inbound_membership_hash,
    _squad_reference,
    _squad_set_hash,
    build_legacy_entitlement_seed_plan,
    validate_approved_shared_squad_inventory,
    validate_legacy_shared_squad_inventory_before_panel_sync,
)


def _tariff(tariff_id: int, *, active: bool, squads: list[str]):
    return SimpleNamespace(
        id=tariff_id,
        is_active=active,
        entitlement_mode='legacy_snapshot',
        allowed_squads=squads,
        location_policy_revision=1,
    )


def _subscription(subscription_id: int, tariff_id: int, squads: list[str]):
    return SimpleNamespace(id=subscription_id, tariff_id=tariff_id, connected_squads=squads)


def _patch_approved_inventory(monkeypatch, squads: list[str]) -> None:
    monkeypatch.setattr(
        legacy_entitlement_manifest_seed,
        'OWNER_APPROVED_SHARED_SQUAD_INVENTORY',
        {
            _squad_reference(squad): _ApprovedSharedSquadInventory(
                raw_inbounds_count=2,
                raw_inbound_membership_sha256=_raw_inbound_membership_hash([f'{squad}-one', f'{squad}-two']),
            )
            for squad in squads
        },
    )


def test_seed_plan_preserves_explicit_existing_subscription_access(monkeypatch):
    squads = ['a', 'b']
    _patch_approved_inventory(monkeypatch, squads)
    monkeypatch.setattr(
        legacy_entitlement_manifest_seed,
        'OWNER_APPROVED_LEGACY_TARIFF_SQUAD_SET_HASHES',
        {3: _squad_set_hash(squads), 4: _squad_set_hash(squads), 5: _squad_set_hash(squads)},
    )
    plan = build_legacy_entitlement_seed_plan(
        [_tariff(3, active=True, squads=squads), _tariff(4, active=True, squads=squads), _tariff(5, active=False, squads=squads)],
        [_subscription(10, 3, squads), _subscription(11, 5, squads), _subscription(12, 3, [])],
    )

    assert {manifest.tariff_id for manifest in plan.manifests} == {3, 4, 5}
    assert [snapshot.subscription_id for snapshot in plan.subscription_snapshots] == [10, 11]
    assert plan.subscription_snapshots[1].entitlement.squad_uuids == ('a', 'b')
    assert plan.skipped_empty_subscriptions == 1


def test_seed_plan_rejects_unapproved_active_tariff_scope():
    with pytest.raises(LegacyEntitlementSeedError, match='scope differs'):
        build_legacy_entitlement_seed_plan(
            [_tariff(3, active=True, squads=['a']), _tariff(4, active=True, squads=['a']), _tariff(9, active=True, squads=['a'])],
            [],
        )


def test_seed_plan_rejects_subscription_access_that_does_not_equal_its_tariff(monkeypatch):
    squads = ['a', 'b']
    _patch_approved_inventory(monkeypatch, squads)
    monkeypatch.setattr(
        legacy_entitlement_manifest_seed,
        'OWNER_APPROVED_LEGACY_TARIFF_SQUAD_SET_HASHES',
        {3: _squad_set_hash(squads), 4: _squad_set_hash(squads)},
    )
    with pytest.raises(LegacyEntitlementSeedError, match='manual reconcile'):
        build_legacy_entitlement_seed_plan(
            [_tariff(3, active=True, squads=squads), _tariff(4, active=True, squads=squads)],
            [_subscription(10, 3, ['a'])],
        )


def test_shared_squad_inventory_accepts_exact_approved_raw_membership(monkeypatch):
    squads = ['shared-a', 'shared-b']
    _patch_approved_inventory(monkeypatch, squads)

    evidence = validate_approved_shared_squad_inventory(
        {
            'shared-a': ['shared-a-two', 'shared-a-one'],
            'shared-b': ['shared-b-one', 'shared-b-two'],
        }
    )

    assert evidence['shared_squads'][0]['raw_inbounds_count'] == 2
    assert {item['squad_ref_sha256'] for item in evidence['shared_squads']} == {
        _squad_reference('shared-a'),
        _squad_reference('shared-b'),
    }


def test_shared_squad_inventory_rejects_unexpected_raw_member(monkeypatch):
    squads = ['shared-a', 'shared-b']
    _patch_approved_inventory(monkeypatch, squads)

    with pytest.raises(LegacyEntitlementSeedError, match='membership differs'):
        validate_approved_shared_squad_inventory(
            {
                'shared-a': ['shared-a-one', 'unexpected-raw-inbound'],
                'shared-b': ['shared-b-one', 'shared-b-two'],
            }
        )


@pytest.mark.asyncio
async def test_legacy_shared_squad_sync_requires_injected_read_only_inventory(monkeypatch):
    squads = ['shared-a', 'shared-b']
    _patch_approved_inventory(monkeypatch, squads)
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=tuple(squads))),
    )

    with pytest.raises(LegacyEntitlementSeedError, match='requires an injected'):
        await validate_legacy_shared_squad_inventory_before_panel_sync(
            SimpleNamespace(),
            SimpleNamespace(id=10),
            inventory_reader=None,
        )

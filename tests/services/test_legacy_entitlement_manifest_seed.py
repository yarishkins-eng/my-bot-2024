from types import SimpleNamespace

import pytest

from app.services import legacy_entitlement_manifest_seed
from app.services.legacy_entitlement_manifest_seed import (
    LegacyEntitlementSeedError,
    _squad_set_hash,
    build_legacy_entitlement_seed_plan,
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


def test_seed_plan_preserves_explicit_existing_subscription_access(monkeypatch):
    squads = ['a', 'b']
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

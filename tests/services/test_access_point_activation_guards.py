from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.database.crud import subscription as subscription_crud, tariff as tariff_crud
from app.services import user_service as user_service_module
from app.services.public_access_point_service import AccessPointPolicyError, assert_no_manual_access_point_grant
from app.services.public_location_entitlement_service import EntitlementResolutionError
from app.services.subscription_service import SubscriptionService


@pytest.mark.asyncio
async def test_new_tariff_uses_native_squads_without_an_access_point_policy(monkeypatch) -> None:
    db = SimpleNamespace(add=Mock(), execute=AsyncMock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_effective_tariff_squad_uuids',
        AsyncMock(return_value=['de-squad']),
    )

    tariff = await tariff_crud.create_tariff(db, 'Native draft', is_active=True, allowed_squads=['de-squad'])

    assert tariff.entitlement_mode == 'native_squads'
    assert tariff.allowed_squads == ['de-squad']
    db.add.assert_called_once_with(tariff)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_native_tariff_rejects_an_unavailable_internal_squad(monkeypatch) -> None:
    db = SimpleNamespace(add=Mock(), execute=AsyncMock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_effective_tariff_squad_uuids',
        AsyncMock(side_effect=ValueError('tariff references unavailable Internal Squads')),
    )

    with pytest.raises(ValueError, match='no available Internal Squad selection'):
        await tariff_crud.create_tariff(db, 'Unsafe tariff', is_active=True, allowed_squads=['disabled-squad'])

    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_squad_change_is_blocked_while_a_live_checkout_can_still_be_paid() -> None:
    tariff = SimpleNamespace(id=17, entitlement_mode='legacy_snapshot', allowed_squads=['old-squad'])
    db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(return_value=999),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    with pytest.raises(ValueError, match='live checkout or Platega invoice'):
        await tariff_crud.update_tariff(db, tariff, allowed_squads=['de-squad'])

    assert tariff.entitlement_mode == 'legacy_snapshot'
    assert tariff.allowed_squads == ['old-squad']
    db.get.assert_awaited_once_with(tariff_crud.Tariff, 17, with_for_update=True, populate_existing=True)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_squad_payload_cannot_bypass_live_checkout_fence_during_native_transition() -> None:
    tariff = SimpleNamespace(id=18, entitlement_mode='access_point_managed', allowed_squads=['de-squad'])
    db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(return_value=1000),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    with pytest.raises(ValueError, match='live checkout or Platega invoice'):
        await tariff_crud.update_tariff(db, tariff, allowed_squads=['de-squad'])

    assert tariff.entitlement_mode == 'access_point_managed'
    assert tariff.allowed_squads == ['de-squad']
    db.get.assert_awaited_once_with(tariff_crud.Tariff, 18, with_for_update=True, populate_existing=True)
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_assignment_checks_entitlement_before_clearing_other_trials(monkeypatch) -> None:
    tariff = SimpleNamespace(id=7, entitlement_mode='no_locations')
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(side_effect=EntitlementResolutionError('no verified policy')),
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())

    with pytest.raises(EntitlementResolutionError, match='no verified policy'):
        await tariff_crud.set_trial_tariff(db, 7)

    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_is_checked_by_shared_crud_gate_before_tariff_mutation(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=8,
        is_active=False,
        is_trial_available=False,
        traffic_limit_gb=100,
        device_limit=1,
        device_price_kopeks=None,
        max_device_limit=None,
        device_purchase_options=[],
        period_prices={},
        allowed_squads=[],
        is_daily=False,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
    )
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(side_effect=EntitlementResolutionError('no verified policy')),
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    with pytest.raises(EntitlementResolutionError, match='no verified policy'):
        await tariff_crud.update_tariff(db, tariff, is_active=True)

    assert tariff.is_active is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_point_tariff_cannot_be_made_daily_or_trial_before_mutation() -> None:
    tariff = SimpleNamespace(entitlement_mode='access_point_managed', is_daily=False, is_trial_available=False)
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    with pytest.raises(ValueError, match='daily'):
        await tariff_crud.update_tariff(db, tariff, is_daily=True)
    with pytest.raises(ValueError, match='trials'):
        await tariff_crud.update_tariff(db, tariff, is_trial_available=True)

    assert tariff.is_daily is False
    assert tariff.is_trial_available is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_point_manual_admin_grant_is_rejected_before_mutation() -> None:
    subscription = SimpleNamespace(id=17, tariff_id=17)
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(entitlement_mode='access_point_managed')),
        scalar=AsyncMock(return_value=None),
    )

    with pytest.raises(AccessPointPolicyError, match='manual activate'):
        await assert_no_manual_access_point_grant(db, subscription, action='activate')


@pytest.mark.asyncio
async def test_access_point_trial_and_pending_issuers_fail_before_entitlement_or_db_write() -> None:
    tariff = SimpleNamespace(id=21, entitlement_mode='access_point_managed')
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=tariff),
        add=Mock(),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )

    with pytest.raises(ValueError, match='access_point_trial_unsupported'):
        await subscription_crud.create_trial_subscription(db, user_id=7, tariff_id=21)
    with pytest.raises(ValueError, match='access_point_pending_issuance_unsupported'):
        await subscription_crud.create_pending_subscription(db, user_id=7, duration_days=30, tariff_id=21)

    db.add.assert_not_called()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_user_unblock_does_not_require_access_point_reprojection(monkeypatch) -> None:
    user = SimpleNamespace(id=31, account_erasure_requested_at=None, subscriptions=[])
    monkeypatch.setattr(user_service_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(user_service_module, 'update_user', AsyncMock())
    db = SimpleNamespace(commit=AsyncMock())

    assert await user_service_module.UserService().unblock_user(db, user_id=31, admin_id=9) is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_panel_sync_rejects_access_point_before_update_or_create(monkeypatch) -> None:
    subscription = SimpleNamespace(id=44, tariff_id=19, is_active=True)
    user = SimpleNamespace(id=33, account_erasure_requested_at=None, subscriptions=[subscription])
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(entitlement_mode='access_point_managed')),
        scalar=AsyncMock(return_value=None),
    )

    with pytest.raises(HTTPException) as error:
        await admin_users.sync_user_to_panel(
            user_id=33,
            subscription_id=44,
            admin=SimpleNamespace(id=2),
            db=db,
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_generic_panel_state_push_rejects_access_point_grace_extension() -> None:
    subscription = SimpleNamespace(id=45, user_id=33, tariff_id=19)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(entitlement_mode='access_point_managed')))

    pushed = await SubscriptionService().push_panel_state(
        db,
        subscription,
        active=True,
        expire_at=SimpleNamespace(),
    )

    assert pushed is False

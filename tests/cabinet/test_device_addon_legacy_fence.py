"""Old buttons cannot bypass quote confirmation or revive an old device cart."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices
from app.config import Settings
from app.handlers.subscription import devices as bot_devices
from app.services import subscription_auto_purchase_service as carts
from app.webapi.routes import miniapp
from app.webapi.schemas.miniapp import MiniAppSubscriptionDevicesUpdateRequest


def test_device_purchase_requires_explicit_release_activation(monkeypatch):
    monkeypatch.delenv('DEVICE_ADDON_PURCHASE_ENABLED', raising=False)
    configured = Settings(_env_file=None, BOT_TOKEN='123456789:TEST_ONLY_DEVICE_DEFAULT')
    assert configured.DEVICE_ADDON_PURCHASE_ENABLED is False


@pytest.mark.asyncio
async def test_bot_device_addon_entry_hides_button_while_purchase_is_disabled(monkeypatch):
    monkeypatch.setattr(bot_devices.settings, 'DEVICE_ADDON_PURCHASE_ENABLED', False)
    monkeypatch.setattr(type(bot_devices.settings), 'get_cabinet_link', lambda self: 'https://cabinet.example.test')
    callback = SimpleNamespace(
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    await bot_devices._open_device_addon_cabinet(
        callback,
        SimpleNamespace(language='ru'),
        SimpleNamespace(id=99),
        2,
    )
    callback.answer.assert_awaited_once_with('Докупка устройств временно недоступна.', show_alert=True)
    callback.message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'handler', [devices.purchase_devices_legacy, devices.purchase_devices, devices.save_devices_cart]
)
async def test_old_device_posts_require_refresh_before_any_database_or_cart_write(handler):
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await handler(request=SimpleNamespace(devices=2), subscription_id=99, user=SimpleNamespace(id=1), db=db)
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'quote_required'
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_webapi_device_increase_requires_quote_before_database_write(monkeypatch):
    subscription = SimpleNamespace(
        id=99,
        actual_status='active',
        is_active=True,
        is_trial=False,
        device_limit=3,
    )
    user = SimpleNamespace(id=1, subscriptions=[subscription])
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
    panel_service = Mock()
    monkeypatch.setattr(miniapp, 'SubscriptionService', panel_service)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await miniapp.update_subscription_devices_endpoint(
            payload=MiniAppSubscriptionDevicesUpdateRequest(
                initData='signed',
                subscription_id=99,
                devices=4,
            ),
            db=db,
        )

    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'quote_required'
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    panel_service.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_webapi_device_decrease_keeps_existing_update_path(monkeypatch):
    subscription = SimpleNamespace(
        id=99,
        actual_status='active',
        is_active=True,
        is_trial=False,
        device_limit=3,
        tariff_id=None,
        updated_at=None,
    )
    user = SimpleNamespace(id=1, subscriptions=[subscription])
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
    monkeypatch.setattr(miniapp.settings, 'PRICE_PER_DEVICE', 5000)
    monkeypatch.setattr(miniapp.settings, 'MAX_DEVICES_LIMIT', 10)

    from app.services import public_access_point_service

    access_check = AsyncMock()
    monkeypatch.setattr(public_access_point_service, 'assert_no_manual_access_point_grant', access_check)
    update_panel = AsyncMock()
    monkeypatch.setattr(
        miniapp,
        'SubscriptionService',
        lambda: SimpleNamespace(update_remnawave_user=update_panel),
    )
    monkeypatch.setattr(miniapp, 'with_admin_notification_service', AsyncMock())
    locked_result = SimpleNamespace(scalar_one=lambda: subscription)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=locked_result),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    response = await miniapp.update_subscription_devices_endpoint(
        payload=MiniAppSubscriptionDevicesUpdateRequest(
            initData='signed',
            subscription_id=99,
            devices=2,
        ),
        db=db,
    )

    assert response.success is True
    assert subscription.device_limit == 2
    access_check.assert_awaited_once()
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()
    update_panel.assert_awaited_once_with(db, subscription)


@pytest.mark.asyncio
async def test_legacy_webapi_rechecks_device_increase_after_subscription_lock(monkeypatch):
    stale_subscription = SimpleNamespace(
        id=99,
        actual_status='active',
        is_active=True,
        is_trial=False,
        device_limit=3,
        tariff_id=None,
    )
    locked_subscription = SimpleNamespace(id=99, device_limit=2)
    user = SimpleNamespace(id=1, subscriptions=[stale_subscription])
    monkeypatch.setattr(miniapp, '_authorize_miniapp_user', AsyncMock(return_value=user))
    monkeypatch.setattr(miniapp.settings, 'PRICE_PER_DEVICE', 5000)
    monkeypatch.setattr(miniapp.settings, 'MAX_DEVICES_LIMIT', 10)

    from app.services import public_access_point_service

    access_check = AsyncMock()
    monkeypatch.setattr(public_access_point_service, 'assert_no_manual_access_point_grant', access_check)
    panel_service = Mock()
    monkeypatch.setattr(miniapp, 'SubscriptionService', panel_service)
    locked_result = SimpleNamespace(scalar_one=lambda: locked_subscription)
    db = SimpleNamespace(execute=AsyncMock(return_value=locked_result), commit=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await miniapp.update_subscription_devices_endpoint(
            payload=MiniAppSubscriptionDevicesUpdateRequest(
                initData='signed',
                subscription_id=99,
                devices=3,
            ),
            db=db,
        )

    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'quote_required'
    access_check.assert_awaited_once()
    db.execute.assert_awaited_once()
    db.commit.assert_not_awaited()
    panel_service.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_device_cart_does_not_lock_or_reactivate_subscription(monkeypatch):
    check_subscription = AsyncMock()
    monkeypatch.setattr(carts, '_is_subscription_disabled', check_subscription)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    user = SimpleNamespace(id=1, restriction_subscription=True)
    cart = {'cart_mode': 'add_devices', 'subscription_id': 99, 'devices_to_add': 2, 'total_price': 100}
    assert await carts._process_single_cart(db, user, cart) is False
    assert await carts._auto_add_devices(db, user, cart) is False
    check_subscription.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()

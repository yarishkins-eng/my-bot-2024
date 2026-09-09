"""Old buttons cannot bypass quote confirmation or revive an old device cart."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices
from app.services import subscription_auto_purchase_service as carts


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

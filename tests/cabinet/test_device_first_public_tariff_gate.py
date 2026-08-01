from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.subscription_modules import purchase


@pytest.mark.asyncio
async def test_public_device_first_rollout_rejects_the_legacy_tariff_purchase_endpoint(monkeypatch):
    monkeypatch.setattr(purchase.settings, 'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED', True)

    with pytest.raises(HTTPException) as raised:
        await purchase.purchase_tariff(
            SimpleNamespace(),
            SimpleNamespace(restriction_subscription=False),
            SimpleNamespace(),
        )

    assert raised.value.status_code == status.HTTP_409_CONFLICT
    assert raised.value.detail['code'] == 'device_first_required'


@pytest.mark.asyncio
async def test_canary_mode_does_not_block_the_legacy_tariff_purchase_endpoint(monkeypatch):
    monkeypatch.setattr(purchase.settings, 'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED', False)
    monkeypatch.setattr(purchase.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)

    with pytest.raises(HTTPException) as raised:
        await purchase.purchase_tariff(
            SimpleNamespace(),
            SimpleNamespace(restriction_subscription=True),
            SimpleNamespace(),
        )

    assert raised.value.status_code == status.HTTP_403_FORBIDDEN

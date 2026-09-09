"""Checkout availability and the legacy mutation fence must agree."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import device_first
from app.cabinet.routes.subscription_modules import purchase


@pytest.mark.asyncio
@pytest.mark.parametrize('public_rollout', [False, True])
@pytest.mark.parametrize('eligible', [False, True])
async def test_options_explicitly_report_legacy_permission_independent_of_eligibility(
    monkeypatch, public_rollout, eligible
):
    monkeypatch.setattr(device_first.settings, 'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED', public_rollout)
    payload = {'eligible': eligible, 'reason': 'eligible_tariff_count_not_one' if not eligible else 'eligible'}
    build = AsyncMock(return_value=payload)
    monkeypatch.setattr(device_first, 'build_purchase_options', build)
    db, user = AsyncMock(), SimpleNamespace(id=1)

    result = await device_first.purchase_options(user=user, db=db)

    assert result == {**payload, 'legacy_tariff_purchase_allowed': not public_rollout}
    assert 'legacy_tariff_purchase_allowed' not in payload
    build.assert_awaited_once_with(db, user)
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_options_failure_remains_a_failure_not_legacy_permission(monkeypatch):
    monkeypatch.setattr(device_first, 'build_purchase_options', AsyncMock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError, match='unavailable'):
        await device_first.purchase_options(user=SimpleNamespace(id=1), db=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('is_trial', [True, False])
async def test_retired_purchase_rejected_before_any_lookup_or_financial_side_effect(monkeypatch, is_trial):
    monkeypatch.setattr(purchase.settings, 'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED', True)
    lookup = AsyncMock()
    monkeypatch.setattr(purchase, 'get_tariff_by_id', lookup)
    db = AsyncMock()
    user = SimpleNamespace(id=1, restriction_subscription=False, subscription=SimpleNamespace(is_trial=is_trial))

    with pytest.raises(HTTPException) as raised:
        await purchase.purchase_tariff(SimpleNamespace(tariff_id=3, period_days=30), user, db)

    assert raised.value.status_code == 409
    assert raised.value.detail['code'] == 'device_first_required'
    lookup.assert_not_awaited()
    assert db.mock_calls == []
    assert user.subscription.is_trial is is_trial


@pytest.mark.asyncio
@pytest.mark.parametrize('public_rollout', [False, True])
async def test_tariff_options_preserve_catalog_but_distinguish_forbidden_legacy_tariffs(monkeypatch, public_rollout):
    from app.services import device_first_checkout_service

    monkeypatch.setattr(purchase.settings, 'DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED', public_rollout)
    monkeypatch.setattr(purchase.settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(type(purchase.settings), 'is_multi_tariff_enabled', lambda self: False)
    tariffs = [
        SimpleNamespace(id=1, entitlement_mode='legacy_snapshot'),
        SimpleNamespace(id=2, entitlement_mode='access_point_managed'),
        SimpleNamespace(id=3, entitlement_mode='native_squads'),
    ]
    monkeypatch.setattr(purchase, 'get_tariffs_for_user', AsyncMock(return_value=tariffs))
    monkeypatch.setattr(purchase, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        purchase,
        '_build_tariff_response',
        AsyncMock(
            side_effect=lambda db, tariff, *args: {'id': tariff.id, 'periods': [{'days': 30, 'price_kopeks': 24900}]}
        ),
    )
    monkeypatch.setattr(
        device_first_checkout_service, 'build_purchase_options', AsyncMock(return_value={'eligible': False})
    )
    db = AsyncMock()
    response = await purchase.get_purchase_options(
        user=SimpleNamespace(id=1, promo_group=None, balance_kopeks=0), db=db
    )
    assert [t['id'] for t in response['tariffs']] == [1, 2, 3]
    assert [t['legacy_purchase_allowed'] for t in response['tariffs']] == [
        not public_rollout,
        False,
        not public_rollout,
    ]
    assert all(t['periods'] == [{'days': 30, 'price_kopeks': 24900}] for t in response['tariffs'])
    assert db.mock_calls == []

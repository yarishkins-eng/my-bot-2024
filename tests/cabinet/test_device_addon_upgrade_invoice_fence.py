"""Этап ДУ-2б: докупка закрыта, пока висит неоплаченный счёт на продление с добавленными устройствами.

Счёт Platega на продление с ростом устройств уже содержит доплату за те же устройства на
остаток срока и исполняется по своему снимку без пересчёта (лимит подписки — терпимый ключ
слепка). Докупка с баланса в это окно = двойная оплата пересечения; окно — жизнь счёта у
провайдера (часы), а не 30 минут котировки.
"""

from __future__ import annotations

from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import device_addon_service as service


def _checkout(*, state, prorate):
    return SimpleNamespace(lifecycle_state=state, price_breakdown={'upgrade_prorate_kopeks': prorate})


def _wire(monkeypatch, *, open_checkout):
    subscription = SimpleNamespace(id=30, status='active', tariff_id=3, device_limit=2, end_date=None)
    monkeypatch.setattr(service, '_find_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.get_open_checkout_for_user',
        AsyncMock(return_value=open_checkout),
    )
    price_resolver = MagicMock(side_effect=RuntimeError('дальше этой точки тест не идёт'))
    monkeypatch.setattr('app.cabinet.routes.subscription_modules.helpers._resolve_device_addon_price', price_resolver)
    monkeypatch.setattr('app.services.account_test_reset_service.reset_is_busy', lambda _user: False)
    monkeypatch.setattr('app.services.public_access_point_service.assert_no_manual_access_point_grant', AsyncMock())
    user = SimpleNamespace(id=206, status='active', restriction_subscription=False, account_erasure_requested_at=None)
    return user, price_resolver


@pytest.mark.asyncio
async def test_pending_upgrade_invoice_closes_the_addon_door(monkeypatch):
    user, price_resolver = _wire(monkeypatch, open_checkout=_checkout(state='awaiting_funds', prorate=60_800))
    db = SimpleNamespace(get=AsyncMock(return_value=None))

    with pytest.raises(service.DeviceAddonError) as error:
        await service.calculate_device_addon(db, user=user, subscription_id=30, devices_to_add=1)

    assert error.value.code == 'renewal_invoice_pending'
    assert error.value.status_code == 409
    price_resolver.assert_not_called(), 'отказ стоит ДО расчёта цены докупки'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'open_checkout',
    [
        None,
        _checkout(state='awaiting_funds', prorate=0),  # счёт без роста устройств — пересечения нет
        _checkout(state='confirmed', prorate=60_800),  # счёт ещё не выпущен — кошелёк перепроверит сам
        SimpleNamespace(lifecycle_state='awaiting_funds', price_breakdown=None),  # заказ до этапа
    ],
)
async def test_the_addon_door_stays_open_without_a_pending_upgrade_invoice(monkeypatch, open_checkout):
    user, price_resolver = _wire(monkeypatch, open_checkout=open_checkout)
    db = SimpleNamespace(get=AsyncMock(return_value=None))

    with suppress(RuntimeError):
        await service.calculate_device_addon(db, user=user, subscription_id=30, devices_to_add=1)

    price_resolver.assert_called_once(), 'дошли до расчёта цены — забор не сработал'

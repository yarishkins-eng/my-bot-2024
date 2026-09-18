from types import SimpleNamespace

import pytest

from app.services import subscription_auto_purchase_service as service


@pytest.mark.parametrize('cart_device_limit', [None, 1])
def test_trial_conversion_preserves_one_device_for_both_cart_paths(monkeypatch, cart_device_limit):
    monkeypatch.setattr(service.settings, 'DEFAULT_DEVICE_LIMIT', 3)
    subscription = SimpleNamespace(
        is_trial=True,
        traffic_limit_gb=10,
        device_limit=1,
        connected_squads=[],
    )
    context = service.AutoExtendContext(
        subscription=subscription,
        period_days=30,
        price_kopeks=14_900,
        description='test',
        device_limit=cart_device_limit,
    )

    service._apply_extension_updates(context)

    assert subscription.device_limit == 1


# --- Этап ДУ-3 (14.09.2026): лимит из корзины не может отличаться от того, по которому
# посчитана цена. Дефект 3 внешнего ревью 13.09: корзина `extend` с 2 устройствами после
# уменьшения до 1 возвращала 2 за цену одного. ---------------------------------------


def _prepare_kwargs(monkeypatch, *, subscription, cart_device_limit):
    """Собирает всё вокруг `_prepare_auto_extend_context`, чтобы дойти до строки с лимитом."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(service.settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=subscription)
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_id_for_user', AsyncMock(return_value=subscription)
    )
    tariff = SimpleNamespace(id=3, name='Базовый', is_active=True, is_daily=False, period_prices={'30': 14900})
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(side_effect=lambda db, uid: user))
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=14_900, original_total=14_900, promo_offer_discount=0)),
    )
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=())),
    )
    monkeypatch.setattr('app.utils.promo_offer.get_user_active_promo_discount_percent', lambda _u: 0)
    user = SimpleNamespace(id=7, telegram_id=7007, balance_kopeks=100_000, subscription=subscription)
    cart = {
        'cart_mode': 'extend',
        'subscription_id': subscription.id,
        'tariff_id': 3,
        'period_days': 30,
        'device_limit': cart_device_limit,
    }
    return user, cart


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('is_trial', 'priced_limit', 'cart_limit', 'expected'),
    [
        (False, 1, 2, 1),  # платная: уменьшил 2 → 1, корзина ещё помнит 2 — выдаём 1
        (False, 2, 2, 2),
        (False, 3, 1, 3),  # докупил после корзины — не режем
        (True, 1, 2, 1),  # пробная: старый лимит в корзине — тоже не пол
        (False, None, 2, None),  # лимит не записан — корзина его не выдумывает
    ],
)
async def test_extend_cart_device_limit_follows_the_priced_subscription(
    monkeypatch, is_trial, priced_limit, cart_limit, expected
):
    subscription = SimpleNamespace(
        id=99,
        is_trial=is_trial,
        status='active',
        tariff_id=3,
        device_limit=priced_limit,
        traffic_limit_gb=0,
        connected_squads=[],
        end_date=None,
    )
    user, cart = _prepare_kwargs(monkeypatch, subscription=subscription, cart_device_limit=cart_limit)

    prepared = await service._prepare_auto_extend_context(object(), user, cart)

    assert prepared is not None, 'расхождение корзины с подпиской — не повод молча пропустить продление'
    assert prepared.device_limit == expected
    assert prepared.price_kopeks == 14_900, 'цена — по подписке, не по корзине'

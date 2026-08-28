"""Одноразовая скидка обязана сгорать ровно на той продаже, которая её применила.

🔴 Дефект был живым: 28.08.2026 пользователь 314 применил скидку 10 % в заказе 64
(заплатил 224,10 ₽ вместо 249,00 ₽ картой) и остался со скидкой на счету.

Числа в фикстурах намеренно НЕ совпадают с умолчаниями соседних тестов кассы
(10 000 / 30 050 / 36 900 / 50 000): совпадение делает сторож проверкой совпадения,
а не проверкой защиты.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as service
from app.services.public_location_entitlement_service import ResolvedEntitlement


PRICE = 17_700
DISCOUNT = 1_300
PERCENT = 13
BALANCE = 41_900


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, _traceback):
        return False


class _Result:
    """Одна подделка на все чтения: `scalar_one_or_none` и `scalars().first()`."""

    def __init__(self, one=None, first=None):
        self._one = one
        self._first = first

    def scalar_one(self):
        return self._one

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return SimpleNamespace(first=lambda: self._first, all=list)


def _user_with_offer(*, percent: int = PERCENT, balance: int = BALANCE, source: str = 'expired_discount_wave2'):
    return SimpleNamespace(
        id=7,
        balance_kopeks=balance,
        has_had_paid_subscription=False,
        promo_offer_discount_percent=percent,
        promo_offer_discount_source=source,
        promo_offer_discount_expires_at=datetime.now(UTC) + timedelta(hours=9),
    )


def _checkout(*, discount_kopeks: int):
    return SimpleNamespace(
        id=77,
        public_id='checkout-77',
        price_breakdown={'promo_offer_discount_kopeks': discount_kopeks},
    )


# --------------------------------------------------------------------------- #
# Сам механизм
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_applied_discount_is_burned_and_written_into_the_promo_log():
    user = _user_with_offer()
    offer = SimpleNamespace(id=404, effect_type='percent_discount')
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(first=offer)),
        add=added.append,
        flush=AsyncMock(),
        begin_nested=lambda: _Savepoint(),
    )

    await service._consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=_checkout(discount_kopeks=DISCOUNT),
        applied_discount_kopeks=DISCOUNT,
        expected_source='expired_discount_wave2',
    )

    assert user.promo_offer_discount_percent == 0
    assert user.promo_offer_discount_source is None
    assert user.promo_offer_discount_expires_at is None
    assert len(added) == 1
    entry = added[0]
    assert entry.action == 'consumed'
    # Процент пишем ТОТ, что был у человека до гашения: после гашения он ноль,
    # и лог «потрачено 0 %» не сказал бы владельцу ничего.
    assert entry.percent == PERCENT
    assert entry.offer_id == offer.id
    assert entry.source == 'expired_discount_wave2'
    assert entry.details['discount_kopeks'] == DISCOUNT
    assert entry.details['checkout_public_id'] == 'checkout-77'


@pytest.mark.asyncio
async def test_right_to_a_discount_without_an_applied_discount_is_never_burned():
    """Мина BC: гасить по ПРАВУ на скидку — значит сжигать предложение впустую.

    Это единственный вход, на котором «гасить по факту» и «гасить по праву»
    дают РАЗНЫЙ ответ: право есть, применённая скидка нулевая.
    """
    user = _user_with_offer()
    db = SimpleNamespace(execute=AsyncMock(), add=AsyncMock(), flush=AsyncMock(), begin_nested=lambda: _Savepoint())

    await service._consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=_checkout(discount_kopeks=0),
        applied_discount_kopeks=0,
        expected_source='expired_discount_wave2',
    )

    assert user.promo_offer_discount_percent == PERCENT
    assert user.promo_offer_discount_source == 'expired_discount_wave2'
    assert user.promo_offer_discount_expires_at is not None
    db.add.assert_not_called()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_user_without_a_discount_costs_no_lookup_and_no_log():
    user = _user_with_offer(percent=0)
    db = SimpleNamespace(execute=AsyncMock(), add=AsyncMock(), flush=AsyncMock(), begin_nested=lambda: _Savepoint())

    await service._consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=_checkout(discount_kopeks=DISCOUNT),
        applied_discount_kopeks=DISCOUNT,
        expected_source='expired_discount_wave2',
    )

    db.add.assert_not_called()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_offer_lookup_still_burns_the_discount(monkeypatch):
    """Поиск предложения нужен только для подробностей лога, продажу он не решает."""
    user = _user_with_offer()
    added = []
    db = SimpleNamespace(execute=AsyncMock(), add=added.append, flush=AsyncMock(), begin_nested=lambda: _Savepoint())
    monkeypatch.setattr(
        service,
        'get_latest_claimed_offer_for_user',
        AsyncMock(side_effect=RuntimeError('offer table unavailable')),
    )

    await service._consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=_checkout(discount_kopeks=DISCOUNT),
        applied_discount_kopeks=DISCOUNT,
        expected_source='expired_discount_wave2',
    )

    assert user.promo_offer_discount_percent == 0
    assert added[0].offer_id is None
    assert added[0].percent == PERCENT


# --------------------------------------------------------------------------- #
# Механизм ПОДКЛЮЧЁН — через настоящие точки входа, а не мимо них
# (урок 18.08, пункт 4.1: тесты на функцию не доказывают, что её зовут)
# --------------------------------------------------------------------------- #


def _paid_target():
    return SimpleNamespace(
        id=12,
        tariff_id=99,
        status='active',
        is_trial=False,
        device_limit=4,
        end_date=datetime.now(UTC) + timedelta(days=20),
        updated_at=datetime.now(UTC),
    )


def _armed_checkout(target):
    return SimpleNamespace(
        id=77,
        public_id='checkout-77',
        user_id=7,
        lifecycle_state='armed',
        fulfillment_state='not_started',
        provisioning_state='not_started',
        target_subscription_id=target.id,
        target_snapshot=service._subscription_snapshot(target),
        expect_no_subscription=False,
        tariff_id=7,
        period_days=30,
        selected_device_limit=4,
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        pricing_revision=1,
        quoted_price_kopeks=PRICE,
        settlement_mode='legacy_deposit',
        quote_state='valid',
        terminal_reason=None,
        price_breakdown={'promo_offer_discount_kopeks': 0},
    )


@pytest.mark.asyncio
async def test_wallet_fulfilment_burns_the_discount_it_actually_applied(monkeypatch):
    """Источник факта здесь — ПЕРЕСЧИТАННАЯ цена, а не замороженная разбивка заказа.

    Поэтому в фикстуре они РАСХОДЯТСЯ: разбивка говорит «скидки нет», живая цена —
    «скидка есть». Совпади они, сторож не отличал бы один источник от другого.
    Выше по коду стоит забор `charge != checkout.quoted_price_kopeks`, но он сверяет
    ИТОГ, а не его состав.
    """
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement((), ('squad-1',), 1, 'test')),
    )
    target = _paid_target()
    checkout = _armed_checkout(target)
    user = _user_with_offer()
    tariff = SimpleNamespace(id=7, pricing_revision=1, traffic_limit_gb=100, allowed_squads=[])
    extended = SimpleNamespace(id=target.id, end_date=datetime.now(UTC) + timedelta(days=50))
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(one=user),
                _Result(one=target),
                _Result(one=tariff),
                _Result(first=SimpleNamespace(id=404, effect_type='percent_discount')),
            ]
        ),
        commit=AsyncMock(),
        add=added.append,
        flush=AsyncMock(),
        refresh=AsyncMock(),
        begin_nested=lambda: _Savepoint(),
    )
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=checkout))
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda tariff, subscription: SimpleNamespace(eligible=True, period_options=(30,), device_options=(4, 5)),
    )
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=DISCOUNT)),
    )
    monkeypatch.setattr(service, 'extend_subscription', AsyncMock(return_value=extended))

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.fulfillment_state == 'fulfilled'
    assert user.balance_kopeks == BALANCE - PRICE
    assert user.promo_offer_discount_percent == 0
    assert user.promo_offer_discount_expires_at is None
    assert any(getattr(row, 'action', None) == 'consumed' for row in added)


@pytest.mark.asyncio
async def test_wallet_fulfilment_keeps_a_discount_the_price_did_not_use(monkeypatch):
    """Тот же путь, единственное отличие — применённая скидка нулевая."""
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement((), ('squad-1',), 1, 'test')),
    )
    target = _paid_target()
    checkout = _armed_checkout(target)
    # Зеркало предыдущего сторожа: замороженная разбивка утверждает скидку, живая
    # цена её не применила. Гасить нельзя — платит именно живая цена.
    checkout.price_breakdown = {'promo_offer_discount_kopeks': DISCOUNT}
    user = _user_with_offer()
    tariff = SimpleNamespace(id=7, pricing_revision=1, traffic_limit_gb=100, allowed_squads=[])
    extended = SimpleNamespace(id=target.id, end_date=datetime.now(UTC) + timedelta(days=50))
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(one=user), _Result(one=target), _Result(one=tariff)]),
        commit=AsyncMock(),
        add=added.append,
        flush=AsyncMock(),
        refresh=AsyncMock(),
        begin_nested=lambda: _Savepoint(),
    )
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=checkout))
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda tariff, subscription: SimpleNamespace(eligible=True, period_options=(30,), device_options=(4, 5)),
    )
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=0)),
    )
    monkeypatch.setattr(service, 'extend_subscription', AsyncMock(return_value=extended))

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.fulfillment_state == 'fulfilled'
    assert user.promo_offer_discount_percent == PERCENT
    assert not any(getattr(row, 'action', None) == 'consumed' for row in added)


def _card_sale_checkout(*, discount_kopeks: int, user=None, snapshot_source: str = 'expired_discount_wave2'):
    """Прямая продажа КАРТОЙ — ровно тот путь, что сработал на боевом 28.08.2026.

    Баланс здесь не трогается вовсе, поэтому гасить скидку побочным эффектом
    списания (как делает `subtract_user_balance`) тут физически негде.
    """
    entitlement = ResolvedEntitlement(('point-de',), ('squad-de',), 2, 'tariff_squads', None)
    checkout = SimpleNamespace(
        id=77,
        public_id='checkout-77',
        user_id=7,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='confirmed',
        fulfillment_state='not_started',
        period_days=30,
        selected_device_limit=2,
        tariff_id=7,
        tariff_total_kopeks=PRICE,
        wallet_applied_kopeks=0,
        external_payable_kopeks=PRICE,
        funding_mode='platega',
        expect_no_subscription=True,
        target_snapshot={},
        price_breakdown={'promo_offer_discount_kopeks': discount_kopeks},
        pricing_revision=3,
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        financial_committed_at=None,
    )
    tariff = SimpleNamespace(id=7, name='Базовый', traffic_limit_gb=100)
    checkout.sale_snapshot = service._direct_sale_snapshot(
        checkout,
        tariff,
        funding_mode='platega',
        entitlement=entitlement,
        # Личность предложения замораживается вместе с ценой — в момент выставления счёта.
        user=user if user is not None else _user_with_offer(source=snapshot_source),
    )
    return checkout


def _patch_direct_sale(monkeypatch, added):
    monkeypatch.setattr(service, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(service, '_report_entitlement_drift_without_blocking', AsyncMock())
    monkeypatch.setattr(service, 'ensure_deposit_outbox', AsyncMock())
    monkeypatch.setattr(
        service,
        'create_paid_subscription',
        AsyncMock(return_value=SimpleNamespace(id=12, end_date=datetime.now(UTC) + timedelta(days=30))),
    )
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=_Result(one=None, first=SimpleNamespace(id=404, effect_type='percent_discount'))
        ),
        add=added.append,
        flush=AsyncMock(),
        begin_nested=lambda: _Savepoint(),
    )


@pytest.mark.asyncio
async def test_card_sale_burns_the_discount_even_though_no_balance_is_touched(monkeypatch):
    user = _user_with_offer()
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    checkout = _card_sale_checkout(discount_kopeks=DISCOUNT)

    result = await service._complete_direct_sale_locked(
        db,
        checkout=checkout,
        user=user,
        target=None,
        provider_payment_id='provider-77',
    )

    assert result.fulfillment_state == 'fulfilled'
    # Баланс не тронут — иначе сторож доказывал бы кошельковый путь, а не картовый.
    assert user.balance_kopeks == BALANCE
    assert user.promo_offer_discount_percent == 0
    assert user.promo_offer_discount_source is None
    assert user.promo_offer_discount_expires_at is None
    consumed = [row for row in added if getattr(row, 'action', None) == 'consumed']
    assert len(consumed) == 1
    assert consumed[0].details['discount_kopeks'] == DISCOUNT


@pytest.mark.asyncio
async def test_card_sale_without_an_applied_discount_leaves_the_offer_alone(monkeypatch):
    user = _user_with_offer()
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    checkout = _card_sale_checkout(discount_kopeks=0)

    result = await service._complete_direct_sale_locked(
        db,
        checkout=checkout,
        user=user,
        target=None,
        provider_payment_id='provider-77',
    )

    assert result.fulfillment_state == 'fulfilled'
    assert user.promo_offer_discount_percent == PERCENT
    assert not any(getattr(row, 'action', None) == 'consumed' for row in added)


@pytest.mark.asyncio
async def test_card_sale_does_not_burn_an_offer_the_customer_claimed_later(monkeypatch):
    """Счёт у провайдера живёт часами — за это время предложение могло смениться.

    Замороженная цена посчитана по предложению A, а на человеке лежит уже B.
    Сжечь B — отобрать у клиента скидку, которой эта покупка не пользовалась.
    """
    user = _user_with_offer(source='trial_expired_discount')
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    checkout = _card_sale_checkout(discount_kopeks=DISCOUNT, snapshot_source='expired_discount_wave2')

    result = await service._complete_direct_sale_locked(
        db, checkout=checkout, user=user, target=None, provider_payment_id='provider-77'
    )

    assert result.fulfillment_state == 'fulfilled'
    assert user.promo_offer_discount_percent == PERCENT
    assert user.promo_offer_discount_source == 'trial_expired_discount'
    assert not any(getattr(row, 'action', None) == 'consumed' for row in added)


@pytest.mark.asyncio
async def test_an_order_created_before_this_change_still_burns_by_amount(monkeypatch):
    """У заказов до СК-1 личности предложения в снимке нет — правило прежнее, «по сумме»."""
    user = _user_with_offer()
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    checkout = _card_sale_checkout(discount_kopeks=DISCOUNT)
    del checkout.sale_snapshot['promo_offer_source']

    await service._complete_direct_sale_locked(
        db, checkout=checkout, user=user, target=None, provider_payment_id='provider-77'
    )

    assert user.promo_offer_discount_percent == 0
    assert any(getattr(row, 'action', None) == 'consumed' for row in added)


@pytest.mark.asyncio
async def test_a_sale_that_ends_in_operator_review_never_burns_the_discount(monkeypatch):
    """Сторож на ПОРЯДОК: гашение обязано стоять ниже всего, что умеет бросить.

    Вызывающий ловит `DeviceFirstError` и коммитит состояние разбора. Подними гашение
    выше любой такой ветки — и скидка сгорит по несостоявшейся продаже, молча.
    """
    user = _user_with_offer()
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    # Подписка появилась после оплаты — заказ обязан уйти в разбор, не выдавая ничего.
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=SimpleNamespace(id=99)))
    checkout = _card_sale_checkout(discount_kopeks=DISCOUNT)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service._complete_direct_sale_locked(
            db, checkout=checkout, user=user, target=None, provider_payment_id='provider-77'
        )

    assert raised.value.code == 'operator_review_required'
    assert checkout.lifecycle_state == 'operator_review'
    assert user.promo_offer_discount_percent == PERCENT
    assert not any(getattr(row, 'action', None) == 'consumed' for row in added)


@pytest.mark.asyncio
async def test_a_replayed_provider_webhook_burns_nothing_a_second_time(monkeypatch):
    """Повтор вебхука не должен гасить ВТОРОЕ предложение и писать второй раз в журнал."""
    user = _user_with_offer()
    added = []
    db = _patch_direct_sale(monkeypatch, added)
    checkout = _card_sale_checkout(discount_kopeks=DISCOUNT)

    await service._complete_direct_sale_locked(
        db, checkout=checkout, user=user, target=None, provider_payment_id='provider-77'
    )
    assert checkout.fulfillment_state == 'fulfilled'
    # Между попытками человек успел забрать новое предложение.
    user.promo_offer_discount_percent = PERCENT
    user.promo_offer_discount_source = 'expired_discount_wave2'

    await service._complete_direct_sale_locked(
        db, checkout=checkout, user=user, target=None, provider_payment_id='provider-77'
    )

    assert user.promo_offer_discount_percent == PERCENT
    assert len([row for row in added if getattr(row, 'action', None) == 'consumed']) == 1

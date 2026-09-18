"""Сторож К-2: карточка продажи с кассы кабинета доходит до владельца (Г1).

С 02.08.2026 касса не звала общий сборщик карточек вовсе — 28 продаж без единой карточки.
Проверяем три вещи, каждую — через настоящий код, а не по исходнику:
1. строка `sale:first`/`sale:repeat` ставится в очередь ДО того, как продажа перевернёт
   `has_had_paid_subscription` (иначе каждая первая продажа подписалась бы «Продление»);
2. воркер очереди зовёт общий сборщик с ЯВНЫМИ аргументами: тип покупки, способ оплаты по
   коду метода провайдера, скидка из замороженной разбивки — и никогда не шлёт клиенту;
3. выключенные уведомления → строка `obsolete`; отказ доставки → `failed`, без повторов.
Числа в фикстурах нарочно не совпадают с умолчаниями (период 90, скидка 1 500 коп., код 11).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service_module
from app.services.device_first_checkout_service import (
    READY_NOTIFICATION_TYPE,
    RETRYABLE_NOTIFICATION_TYPES,
    SALE_NOTIFICATION_PREFIX,
    _queue_owner_sale_row,
    process_device_first_notification_outbox,
)
from app.services.public_location_entitlement_service import ResolvedEntitlement
from tests.services.test_device_first_checkout_transitions import ScalarResult, armed_checkout, paid_target


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _user(**overrides):
    base = dict(
        id=291,
        telegram_id=555,
        username='meddoxpo',
        first_name='С',
        has_had_paid_subscription=False,
        account_erased_at=None,
        account_erasure_requested_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── 1. постановка строки ДО переворота флага ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(('paid_before', 'expected'), [(False, 'sale:first'), (True, 'sale:repeat')])
async def test_sale_row_carries_first_or_repeat_by_the_flag_before_the_flip(paid_before, expected):
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.add = MagicMock()
    checkout = SimpleNamespace(id=42, public_id='ck-42')
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        await _queue_owner_sale_row(db, checkout=checkout, user=_user(has_had_paid_subscription=paid_before))
    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert row.checkout_id == 42
    assert row.notification_type == expected


@pytest.mark.asyncio
async def test_wallet_sale_queues_the_row_before_flipping_the_paid_flag(monkeypatch):
    """Порядок доказывается спаем: в момент постановки флаг ещё False, после продажи — True."""
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=4, quoted_price=10_000)
    user = SimpleNamespace(id=7, balance_kopeks=50_000, has_had_paid_subscription=False)
    tariff = SimpleNamespace(id=7, pricing_revision=1, traffic_limit_gb=100, allowed_squads=[])
    extended = SimpleNamespace(id=target.id, end_date=datetime.now(UTC) + timedelta(days=50))
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[ScalarResult(user), ScalarResult(target), ScalarResult(tariff)]),
        commit=AsyncMock(),
        add=lambda obj: None,
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    seen_flag: list[bool] = []

    async def spy(db_, *, checkout, user):
        seen_flag.append(bool(user.has_had_paid_subscription))

    monkeypatch.setattr(service_module, 'get_owned_checkout', AsyncMock(return_value=checkout))
    monkeypatch.setattr(
        service_module,
        'tariff_eligibility',
        lambda tariff, subscription: SimpleNamespace(eligible=True, period_options=(30,), device_options=(4, 5)),
    )
    monkeypatch.setattr(
        service_module.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10_000, promo_offer_discount=0)),
    )
    monkeypatch.setattr(service_module, 'extend_subscription', AsyncMock(return_value=extended))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement((), ('squad-1',), 1, 'test')),
    )
    monkeypatch.setattr(service_module, '_queue_owner_sale_row', spy)

    await service_module.fulfill_checkout(db, checkout.public_id, user.id)

    assert seen_flag == [False]
    assert user.has_had_paid_subscription is True


@pytest.mark.asyncio
async def test_sale_row_never_breaks_the_sale_and_never_names_an_erased_user():
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=RuntimeError('db down'))
    db.add = MagicMock()
    checkout = SimpleNamespace(id=42, public_id='ck-42')
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        await _queue_owner_sale_row(db, checkout=checkout, user=_user())  # не бросает
        db.add.assert_not_called()
        db.scalar = AsyncMock(return_value=None)
        await _queue_owner_sale_row(db, checkout=checkout, user=_user(account_erased_at=datetime.now(UTC)))
        db.add.assert_not_called()
    with patch.object(service_module, '_owner_alerts_enabled', return_value=False):
        await _queue_owner_sale_row(db, checkout=checkout, user=_user())
        db.add.assert_not_called()


def test_sale_rows_are_not_revived_because_each_retry_would_write_a_second_purchase_event():
    assert not any(kind.startswith(SALE_NOTIFICATION_PREFIX) for kind in RETRYABLE_NOTIFICATION_TYPES)


# ── 2–3. воркер очереди ────────────────────────────────────────────────────────


def _outbox_row(row_id, notification_type):
    return SimpleNamespace(
        id=row_id,
        checkout_id=100 + row_id,
        notification_type=notification_type,
        status='pending',
        lease_token=None,
        lease_expires_at=None,
        sending_at=None,
        sent_at=None,
        last_error=None,
    )


def _checkout(*, funding_mode='external', target_was_trial=False):
    return SimpleNamespace(
        id=101,
        target_snapshot={'is_trial': target_was_trial},
        public_id='ck-101',
        user_id=291,
        created_subscription_id=104,
        debit_transaction_id=594,
        period_days=30,  # колонка врёт нарочно: сборщик обязан читать снимок
        funding_mode=funding_mode,
        sale_snapshot={'period_days': 90, 'price_breakdown': {'promo_offer_discount_kopeks': 1500}},
    )


def _worker_db(rows, *, checkout, user, subscription, transaction, attempt, loads_entities=True):
    """Порядок запросов воркера: захват строк → заказ → (user, subscription, transaction,
    [attempt — только при внешней оплате]; при выключенных уведомлениях ничего) → перечитывание
    строки под аренду."""
    db = MagicMock()
    results: list[_Result] = [_Result(rows)]
    for row in rows:
        results.append(_Result([checkout]))
        if loads_entities:
            results.append(_Result([user]))
            results.append(_Result([subscription]))
            results.append(_Result([transaction]))
            if checkout.funding_mode != 'wallet':
                results.append(_Result([attempt] if attempt is not None else []))
        results.append(_Result([row]))
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(side_effect=lambda model, key: next((r for r in rows if r.id == key), None))
    return db


def _admin(*, enabled=True, delivered=True, categories=None):
    admin = MagicMock()
    admin.is_enabled = enabled
    admin.category_enabled = categories or {}
    admin.send_subscription_purchase_notification = AsyncMock(return_value=delivered)
    return admin


async def _run(rows, *, checkout, admin, attempt=SimpleNamespace(provider_method_code=11), loads_entities=True):
    user = _user()
    subscription = SimpleNamespace(id=104, device_limit=3)
    transaction = SimpleNamespace(id=594, amount_kopeks=-28900, payment_method='platega')
    db = _worker_db(
        rows,
        checkout=checkout,
        user=user,
        subscription=subscription,
        transaction=transaction,
        attempt=attempt,
        loads_entities=loads_entities,
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)
    return sent, bot, user, subscription, transaction


@pytest.mark.asyncio
async def test_first_sale_row_becomes_a_first_purchase_card_with_explicit_arguments():
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}first')
    admin = _admin()
    sent, bot, user, subscription, transaction = await _run([row], checkout=_checkout(), admin=admin)

    assert sent == 1 and row.status == 'sent'
    bot.send_message.assert_not_awaited()  # покупателю — ничего, «Подписка готова» уходит своей строкой
    call = admin.send_subscription_purchase_notification.await_args
    assert call.args[1] is user and call.args[2] is subscription and call.args[3] is transaction
    assert call.args[4] == 90  # из снимка, не из колонки
    assert call.kwargs == {
        'purchase_type': 'first_purchase',
        'was_trial_conversion': False,
        'payment_label': 'картой',
        'discount_kopeks': 1500,
    }


@pytest.mark.asyncio
async def test_first_sale_over_a_trial_is_reported_as_a_trial_conversion():
    """Снимок цели снят при заведении заказа — выдача к моменту воркера уже сняла «пробный»."""
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}first')
    admin = _admin()
    sent, *_ = await _run([row], checkout=_checkout(target_was_trial=True), admin=admin)

    assert sent == 1
    assert admin.send_subscription_purchase_notification.await_args.kwargs['was_trial_conversion'] is True


@pytest.mark.asyncio
async def test_repeat_sale_over_a_trial_flag_is_still_a_renewal():
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}repeat')
    admin = _admin()
    await _run([row], checkout=_checkout(funding_mode='wallet', target_was_trial=True), admin=admin, attempt=None)

    kwargs = admin.send_subscription_purchase_notification.await_args.kwargs
    assert kwargs['purchase_type'] == 'renewal' and kwargs['was_trial_conversion'] is False


@pytest.mark.asyncio
async def test_repeat_sale_from_the_wallet_is_a_renewal_without_a_payment_label():
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}repeat')
    admin = _admin()
    sent, _, _, _, _ = await _run([row], checkout=_checkout(funding_mode='wallet'), admin=admin, attempt=None)

    assert sent == 1 and row.status == 'sent'
    kwargs = admin.send_subscription_purchase_notification.await_args.kwargs
    assert kwargs['purchase_type'] == 'renewal'
    assert kwargs['payment_label'] == ''  # списание с баланса не называем: деньги пришли своей карточкой


@pytest.mark.asyncio
async def test_disabled_notifications_make_the_sale_row_obsolete_not_failed():
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}first')
    admin = _admin(enabled=False)
    sent, _, _, _, _ = await _run([row], checkout=_checkout(), admin=admin, loads_entities=False)

    assert sent == 0 and row.status == 'obsolete'
    admin.send_subscription_purchase_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_undelivered_sale_card_is_marked_failed_and_not_sent():
    row = _outbox_row(1, f'{SALE_NOTIFICATION_PREFIX}first')
    admin = _admin(delivered=False)
    sent, _, _, _, _ = await _run([row], checkout=_checkout(), admin=admin)

    assert sent == 0 and row.status == 'failed'
    assert 'sale_card_not_delivered' in (row.last_error or '')


@pytest.mark.asyncio
async def test_ready_rows_still_reach_the_client_after_the_sale_branch_was_added():
    row = _outbox_row(1, READY_NOTIFICATION_TYPE)
    admin = _admin()
    checkout = _checkout()
    user = SimpleNamespace(id=185, telegram_id=7454290913, username='krotop999', language='ru', full_name='K')
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Result([row]), _Result([checkout]), _Result([row])])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.get = AsyncMock(side_effect=lambda model, key: user if model.__name__ == 'User' else row)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)
    assert sent == 1
    bot.send_message.assert_awaited_once()
    admin.send_subscription_purchase_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_sale_queues_the_row_before_flipping_the_paid_flag(monkeypatch):
    """Тот же порядок на пути картой/СБП (`_complete_direct_sale_locked`): спай видит флаг False."""
    from tests.services.test_device_first_durable_reconciliation import _paid_sale, _sale_db, live_trial, live_user

    target = live_trial(id=134, is_trial=False, device_limit=3, status='active')
    checkout, entitlement = _paid_sale(target, snapshot_device_limit=3)
    user = live_user(balance_kopeks=100_000)
    user.has_had_paid_subscription = False
    db = _sale_db(tariff=SimpleNamespace(id=3, entitlement_mode='native_squads'), user=user)
    db.flush = AsyncMock()
    seen_flag: list[bool] = []

    async def spy(db_, *, checkout, user):
        seen_flag.append(bool(user.has_had_paid_subscription))

    monkeypatch.setattr(service_module, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service_module, 'extend_subscription', AsyncMock(return_value=target))
    monkeypatch.setattr(service_module, '_resolve_checkout_entitlement', AsyncMock(return_value=entitlement))
    monkeypatch.setattr(service_module, '_owner_alerts_enabled', lambda: True)
    monkeypatch.setattr(service_module, '_queue_owner_sale_row', spy)
    await service_module._complete_direct_sale_locked(db, checkout=checkout, user=user, target=target)

    assert seen_flag == [False], 'строка ставится ДО переворота флага, иначе первая продажа станет «Продление»'
    assert user.has_had_paid_subscription is True

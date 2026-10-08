"""ВК-16, часть 16а-2, заявка 3б (08.10.2026): договор с экраном.

Решение владельца 05.10.2026 «Оформляется само»; план ВК, 16а-2 (ж), (з), (л), `_with_purchase_step`, карточка владельцу
по факту отправки, вечная кнопка `df:a2:`. Стенд — настоящий движок SQLite из сторожей 16а-1 и заявок 1–3а; подменены
только сами функции оформления и сеть. Цены нарочно не круглые.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import balance as balance_route
from app.handlers.subscription import device_first as handlers
from app.services import device_first_checkout_service as dfc
from tests.cabinet.test_vk16_topup_autocomplete import (  # noqa: F401 -- фикстуры стенда заявки 1
    TRIAL,
    _pay,
    buy,
    checkouts,
    webhook,
)
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401 -- фикстуры стенда 16а-1
    PRICE_30_1,
    _decide,
    _intent_payment,
    _purchase,
    _set_user,
    _user,
    db,
    env,
    session,
)


async def _by_id(db, session, payment_id: int):
    return await balance_route.get_pending_payment_details(
        method='platega', payment_id=payment_id, user=_user(session), db=db
    )


async def _latest(db, session):
    return await balance_route.get_latest_payment_by_method(method='platega', user=_user(session), db=db)


def _iso(ago: timedelta) -> str:
    return (datetime.now(UTC) - ago).isoformat()


# --- (л) крипта: подтверждение может прийти позже часа — обычное пополнение без обещания -----------------------------


async def test_crypto_is_an_ordinary_top_up_and_promises_nothing(db, session, env):
    decision = await _decide(db, session, method=13)

    assert (decision.status, decision.reason, decision.intent) == ('ordinary', 'method_not_supported', None)
    env.options.assert_not_awaited()  # ни одной проверки ниже: счёт обычный, как у не-Platega


async def test_sbp_and_card_still_get_the_intent(db, session, env):
    for method in (2, 11):
        assert (await _decide(db, session, method=method)).status == 'accepted'


# --- (з) ответ опроса: всё, что экрану нужно для честного ответа -------------------------------------------------------


async def test_refusal_with_offer_tells_the_screen_everything_and_hides_the_raw_code(db, session):
    _intent_payment(
        session,
        payment_id=90,
        period_days=90,
        devices=2,
        quote_kopeks=41_377,
        status='refused',
        reason='price_changed',
        detail='reprice_required',
        offer_kopeks=43_311,
        offer_tariff_name='Базовый',
        is_paid=True,
        transaction_id=990,
    )

    for response in (await _by_id(db, session, 90), await _latest(db, session)):
        assert (response.intent_outcome, response.intent_reason, response.intent_refusal_kind) == (
            'refused',
            'price_changed',
            'retry',
        )
        assert (response.intent_period_days, response.intent_devices, response.intent_quote_kopeks) == (90, 2, 41_377)
        assert (response.intent_offer_kopeks, response.intent_offer_tariff_name) == (43_311, 'Базовый')
        assert response.intent_payment_id == 90
        assert 'reprice_required' not in response.model_dump_json()  # сырой код — только для разбора


async def test_raw_error_code_in_the_reason_is_normalized_for_the_screen(db, session):
    # Исход, записанный до закрытого набора (или руками), не уходит экрану сырым кодом.
    _intent_payment(session, payment_id=91, status='refused', reason='wallet_insufficient', is_paid=True)

    response = await _by_id(db, session, 91)

    assert (response.intent_reason, response.intent_refusal_kind) == ('balance_short', 'retry')


@pytest.mark.parametrize(
    ('status', 'extra', 'kind'),
    [
        ('refused', {'reason': 'already_purchased'}, 'bought'),
        ('refused', {'reason': 'open_order', 'checkout_public_id': 'chk-open-4'}, 'order'),
        ('refused', {'reason': 'order_on_review'}, 'support'),
        ('refused', {'reason': 'restricted'}, 'support'),
        ('replaced', {}, 'retry'),
        ('cancelled', {}, 'retry'),
    ],
)
async def test_kind_is_the_same_as_in_the_bot_message(db, session, status, extra, kind):
    _intent_payment(session, payment_id=92, status=status, is_paid=True, transaction_id=992, **extra)

    response = await _by_id(db, session, 92)

    assert response.intent_refusal_kind == kind
    assert response.intent_refusal_kind == dfc.topup_intent_refusal_kind(response.intent_reason)


@pytest.mark.parametrize(
    ('status', 'extra'),
    [('pending', {}), ('fulfilled', {'checkout_public_id': 'chk-ok-3'})],
)
async def test_waiting_and_fulfilled_have_no_kind_and_no_offer(db, session, status, extra):
    _intent_payment(session, payment_id=93, status=status, offer_kopeks=1_234, **extra)

    response = await _by_id(db, session, 93)

    assert (response.intent_refusal_kind, response.intent_offer_kopeks, response.intent_offer_tariff_name) == (
        None,
        None,
        None,
    )
    assert (response.intent_period_days, response.intent_devices) == (30, 1)


# --- шаг «осталось оформить» — по виду кнопки, а не по подсказке корзины ----------------------------------------------


@pytest.mark.parametrize(
    ('reason', 'step'),
    [
        ('already_purchased', False),  # бот написал «второй раз не списываем»
        ('order_on_review', False),  # бот написал «напишите в поддержку»
        ('account_erasure', False),
        ('open_order', True),
        ('price_changed', True),
        ('technical_error', True),
    ],
)
async def test_purchase_step_follows_the_kind_of_the_refusal(db, session, monkeypatch, reason, step):
    hint = AsyncMock(return_value='Оформите подписку')
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', hint)
    _intent_payment(session, payment_id=94, status='refused', reason=reason, is_paid=True, provider_status='CONFIRMED')

    response = await _by_id(db, session, 94)

    assert response.purchase_step_pending is step
    hint.assert_not_awaited()


async def test_closed_intent_whose_money_came_is_a_step_and_unpaid_is_not(db, session, monkeypatch):
    hint = AsyncMock(return_value=None)  # подсказка молчит — а деньги на балансе и заказ не оформлен
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', hint)
    _intent_payment(session, payment_id=95, status='cancelled', is_paid=True, provider_status='CONFIRMED')
    _intent_payment(session, payment_id=96, status='replaced', created_ago=timedelta(minutes=1))

    assert (await _by_id(db, session, 95)).purchase_step_pending is True
    assert (await _by_id(db, session, 96)).purchase_step_pending is False
    hint.assert_not_awaited()


async def test_ordinary_top_up_still_asks_the_hint(db, session, monkeypatch):
    hint = AsyncMock(return_value='Оформите подписку')
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', hint)
    _intent_payment(session, payment_id=97, status='pending', is_paid=True, provider_status='CONFIRMED')
    session.get(dfc.PlategaPayment, 97).metadata_json = {'language': 'ru'}
    session.commit()

    assert (await _by_id(db, session, 97)).purchase_step_pending is True
    hint.assert_awaited_once()


# --- (ж) оплатили СТАРЫЙ счёт, а экран ждёт новый ----------------------------------------------------------------------


def _replaced_then_paid(session, *, status='refused', paid_ago=timedelta(seconds=30), **old):
    """Счёт 70 открыт 10 мин назад, через 4 минуты человек открыл 71 другим способом (70 — «заменён»), а заплатил по 70."""
    _intent_payment(
        session,
        payment_id=70,
        created_ago=timedelta(minutes=10),
        is_paid=True,
        transaction_id=970,
        provider_status='CONFIRMED',
        **{
            'status': status,
            'replaced_by': 71,
            'reason': 'replaced',
            'offer_kopeks': 14_911,
            'offer_tariff_name': 'Базовый',
            'decided_at': _iso(paid_ago),
            **old,
        },
    )
    _intent_payment(session, payment_id=71, method=11, created_ago=timedelta(minutes=6))


async def test_new_invoice_screen_shows_the_outcome_of_the_paid_old_invoice(db, session):
    _replaced_then_paid(session)

    for response in (await _by_id(db, session, 71), await _latest(db, session)):
        assert response.id == 71 and response.is_paid is False  # сам платёж — новый, денег по нему нет
        assert (response.intent_outcome, response.intent_reason, response.intent_payment_id) == (
            'refused',
            'replaced',
            70,
        )
        assert response.intent_offer_kopeks == 14_911


async def test_cancelled_old_invoice_without_a_link_to_the_new_one_is_found_too(db, session):
    # Отменил заказ в боте (у старого — `cancelled`, без `replaced_by`), открыл новый — а заплатил по старой ссылке.
    _replaced_then_paid(session, replaced_by=None, reason='cancelled')

    response = await _by_id(db, session, 71)

    assert (response.intent_outcome, response.intent_reason, response.intent_payment_id) == ('refused', 'cancelled', 70)


async def test_old_invoice_paid_before_the_new_one_was_opened_is_not_its_outcome(db, session):
    # Деньги по 70 пришли ДО открытия 71: это не «заплатил по старой ссылке» — у 71 своя судьба.
    _replaced_then_paid(session, paid_ago=timedelta(minutes=8))

    response = await _by_id(db, session, 71)

    assert (response.intent_outcome, response.intent_payment_id) == ('waiting', 71)


async def test_unpaid_old_invoice_is_not_an_outcome(db, session):
    _intent_payment(session, payment_id=70, status='replaced', replaced_by=71, created_ago=timedelta(minutes=10))
    _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=6))

    assert (await _by_id(db, session, 71)).intent_payment_id == 71


async def test_old_invoice_paid_and_still_being_processed_shows_processing(db, session):
    # Деньги пришли секунду назад, исход ещё пишется (`decided_at` нет) — «оформляем», а не «ждём».
    _replaced_then_paid(session, status='pending', decided_at=None)

    response = await _by_id(db, session, 71)

    assert (response.intent_outcome, response.intent_payment_id) == ('processing', 70)


async def test_newest_of_two_paid_old_invoices_wins(db, session):
    _replaced_then_paid(session, paid_ago=timedelta(seconds=90), reason='replaced')
    _intent_payment(
        session,
        payment_id=69,
        created_ago=timedelta(minutes=12),
        is_paid=True,
        transaction_id=969,
        status='refused',
        reason='cancelled',
        decided_at=_iso(timedelta(seconds=20)),
    )

    assert (await _by_id(db, session, 71)).intent_payment_id == 69


async def test_new_invoice_paid_itself_keeps_its_own_outcome(db, session):
    _replaced_then_paid(session)
    payment = session.get(dfc.PlategaPayment, 71)
    intent = dfc.topup_intent_of(payment)
    payment.metadata_json = {
        **payment.metadata_json,
        dfc.TOPUP_INTENT_KEY: {**intent, 'status': 'fulfilled', 'checkout_public_id': 'chk-71'},
    }
    payment.is_paid = True
    session.commit()

    response = await _by_id(db, session, 71)

    assert (response.intent_outcome, response.intent_checkout_public_id, response.intent_payment_id) == (
        'fulfilled',
        'chk-71',
        71,
    )


async def test_someone_elses_paid_invoice_is_never_the_outcome(db, session):
    _intent_payment(
        session,
        payment_id=70,
        user_id=2,
        created_ago=timedelta(minutes=10),
        is_paid=True,
        transaction_id=970,
        status='refused',
        reason='replaced',
        decided_at=_iso(timedelta(seconds=30)),
    )
    _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=6))

    assert (await _by_id(db, session, 71)).intent_payment_id == 71


# --- карточка владельцу — по факту отправки -------------------------------------------------------------------------


async def test_owner_card_says_the_explanation_was_sent_only_after_it_was(db, session, webhook):
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})
    seen_by_card: list[int] = []

    async def card(*args, **kwargs):
        seen_by_card.append(webhook.bot.send_message.await_count)
        webhook.admin.append(kwargs)

    with patch.object(webhook.service, 'bot', webhook.bot):
        with patch('app.services.admin_notification_service.AdminNotificationService') as admin:
            admin.return_value.send_balance_topup_notification = card
            await _pay(db, webhook)

    assert seen_by_card == [1]  # карточка ушла ПОСЛЕ сообщения клиенту
    assert webhook.admin[-1]['intent_refused'].endswith('клиенту отправлено объяснение')


async def test_owner_card_does_not_claim_an_explanation_that_failed_to_send(db, session, webhook):
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})
    webhook.bot.send_message.side_effect = RuntimeError('telegram down')

    await _pay(db, webhook)

    assert 'клиенту отправлено объяснение' not in webhook.admin[0]['intent_refused']
    assert webhook.admin[0]['intent_refused'].endswith(
        'объяснение в бот клиенту не ушло (нет Telegram или сбой отправки — смотреть журнал)'
    )


async def test_owner_card_of_a_client_without_telegram_does_not_claim_an_explanation(db, session, webhook):
    _set_user(session, telegram_id=None)
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    webhook.bot.send_message.assert_not_awaited()
    assert 'не ушло' in webhook.admin[0]['intent_refused']


# --- вечная кнопка «Оформить за N ₽» / «Оплатить с баланса» ---------------------------------------------------------


def _wallet_callback(shown_ago: timedelta | None):
    data = 'df:a2:30:1:14900'
    if shown_ago is not None:
        data += f':{int((datetime.now(UTC) - shown_ago).timestamp())}'
    return SimpleNamespace(data=data, answer=AsyncMock())


async def _press(db, session, callback):
    checkout = SimpleNamespace(public_id='chk-wallet')
    with (
        patch.object(
            handlers,
            'create_or_resume_direct_checkout',
            AsyncMock(return_value=SimpleNamespace(checkout=checkout, proceed_to_payment=True)),
        ) as create,
        patch.object(handlers, 'commit_direct_wallet_checkout', AsyncMock(return_value=checkout)) as commit,
        patch.object(handlers, '_render_checkout', AsyncMock()),
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.pay_wallet_fused(callback, _user(session), db, AsyncMock())
    return create, commit, render


async def test_button_pressed_after_a_purchase_by_another_path_charges_nothing(db, session):
    _purchase(session, minutes_ago=30)  # купил картой через полчаса после сообщения

    create, commit, render = await _press(db, session, _wallet_callback(timedelta(hours=1)))

    create.assert_not_awaited()
    commit.assert_not_awaited()
    caption = render.await_args.kwargs['caption']
    assert 'После этого сообщения на вашем аккаунте уже была покупка, поэтому мы ничего не списали.' in caption
    rows = render.await_args.kwargs['keyboard'].inline_keyboard
    assert [(b.text, b.callback_data) for row in rows for b in row] == [
        ('Открыть заказ заново', 'df:start'),
        ('В главное меню', 'back_to_menu'),
    ]


async def test_button_pressed_with_no_purchase_since_it_was_shown_charges_as_before(db, session):
    _purchase(session, minutes_ago=90)  # покупка ДО показа кнопки — не повод спрашивать

    create, commit, _ = await _press(db, session, _wallet_callback(timedelta(hours=1)))

    assert create.await_args.kwargs['expected_tariff_total_kopeks'] == 14_900
    commit.assert_awaited_once()


async def test_button_from_an_old_message_without_the_moment_works_as_before(db, session):
    _purchase(session, minutes_ago=5)

    create, commit, _ = await _press(db, session, _wallet_callback(None))

    create.assert_awaited_once()
    commit.assert_awaited_once()


@pytest.mark.parametrize('data', ['df:a2:30:1:14900:soon', 'df:a2:30:1:14900:1:2', 'df:a2:30:1:14900:99999999999999'])
async def test_broken_button_does_nothing(db, session, data):
    create, commit, render = await _press(db, session, SimpleNamespace(data=data, answer=AsyncMock()))

    create.assert_not_awaited()
    commit.assert_not_awaited()
    render.assert_not_awaited()


def test_refusal_offer_button_carries_the_moment_it_was_shown(monkeypatch):
    monkeypatch.setattr(handlers, '_shown_at', lambda: 1_791_234_567)
    intent = {
        'status': 'refused',
        'reason': 'price_changed',
        'period_days': 30,
        'devices': 1,
        'offer_kopeks': PRICE_30_1,
        'offer_tariff_name': 'Базовый',
    }
    _, keyboard = handlers.topup_intent_refusal_message(
        SimpleNamespace(language='ru'), intent, amount_kopeks=9_900, balance_kopeks=20_000
    )

    assert keyboard.inline_keyboard[0][0].callback_data == f'df:a2:30:1:{PRICE_30_1}:1791234567'
    assert len(keyboard.inline_keyboard[0][0].callback_data.encode()) <= 64


def test_moment_fits_telegram_callback_limit_for_the_longest_order():
    assert len(f'df:a2:3650:100:{2_000_000_000}:{handlers._shown_at()}'.encode()) <= 64

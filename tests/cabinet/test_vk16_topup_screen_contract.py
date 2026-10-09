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
from app.database.models import Transaction
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
    TOP_UP_30_1,
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


async def test_sbp_and_card_still_get_the_intent(db, session, env):
    for method in (2, 11):
        assert (await _decide(db, session, method=method)).status == 'accepted'


async def test_crypto_still_gets_the_same_invoice_answer_before_an_ordinary_one(db, session, env):
    # Волна 1 заявки 3б: обычный счёт криптой поверх живого счёта СБП того же заказа толкал бы ко второй оплате.
    _intent_payment(session, payment_id=60, created_ago=timedelta(minutes=3))

    assert (await _decide(db, session, method=13)).status == 'already_paying'


async def test_crypto_with_enough_balance_is_told_the_balance_covers_it(db, session, env):
    _set_user(session, balance_kopeks=PRICE_30_1)

    assert (await _decide(db, session, method=13)).status == 'balance_covers'


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
        assert (response.intent_payment_id, response.intent_paid, response.intent_amount_kopeks) == (90, True, 9_900)
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
    # Деньги пришли (`transaction_id`): у закрытого без денег вида нет — см. тест ниже.
    _intent_payment(session, payment_id=92, status=status, is_paid=True, transaction_id=992, **extra)

    response = await _by_id(db, session, 92)

    assert response.intent_refusal_kind == kind
    # Сверка с НАСТОЯЩИМИ кнопками сообщения бота про то же намерение (не с той же функцией, что зовёт код).
    _, keyboard = handlers.topup_intent_refusal_message(
        SimpleNamespace(language='ru'),
        dfc.topup_intent_of(session.get(dfc.PlategaPayment, 92)) | {'status': 'refused'},
        amount_kopeks=9_900,
        balance_kopeks=20_000,
    )
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    expected_bot_buttons = {
        'bought': ['В главное меню'],
        'order': ['К моему заказу', 'В главное меню'],
        'retry': ['Выбрать срок', 'В главное меню'],
    }
    if kind == 'support':
        assert 'Оформить' not in ' '.join(texts) and 'Выбрать срок' not in texts
    else:
        assert texts == expected_bot_buttons[kind]


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


@pytest.mark.parametrize('status', ['replaced', 'cancelled'])
async def test_closed_without_money_has_no_button_kind(db, session, status):
    # Бот без денег ничего не пишет — экран не рисует «Оформить» по закрытому неоплаченному счёту (волна 1 заявки 3б).
    _intent_payment(session, payment_id=89, status=status)

    response = await _by_id(db, session, 89)

    assert (response.intent_outcome, response.intent_refusal_kind, response.intent_paid) == ('closed', None, False)


async def test_offer_is_withdrawn_once_anything_was_bought_after_the_refusal(db, session, monkeypatch):
    # 🔴 Три линзы волны 1 заявки 3б: предложение в метаданных вечное, а экран 16в-2 спишет с баланса без банка.
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', AsyncMock(return_value='x'))
    _intent_payment(
        session,
        payment_id=88,
        created_ago=timedelta(minutes=20),
        status='refused',
        reason='price_changed',
        offer_kopeks=14_911,
        offer_tariff_name='Базовый',
        is_paid=True,
        transaction_id=988,
    )
    before = await _by_id(db, session, 88)
    _purchase(session, minutes_ago=2)  # потом купил картой
    after = await _by_id(db, session, 88)

    assert (before.intent_refusal_kind, before.intent_offer_kopeks, before.purchase_step_pending) == (
        'retry',
        14_911,
        True,
    )
    assert (after.intent_reason, after.intent_refusal_kind) == ('already_purchased', 'bought')
    assert (after.intent_offer_kopeks, after.intent_offer_tariff_name, after.purchase_step_pending) == (
        None,
        None,
        False,
    )


async def test_order_bought_by_the_offer_button_reads_as_fulfilled_not_refused(db, session):
    # Волна 2 заявки 3б: человек нажал «Оформить за N ₽» в чате — там «VPN готов»; экран не должен сказать «не оформился».
    _intent_payment(
        session,
        payment_id=86,
        created_ago=timedelta(minutes=20),
        status='refused',
        reason='price_changed',
        offer_kopeks=14_911,
        is_paid=True,
        transaction_id=986,
    )
    _purchase(session, minutes_ago=1, checkout_public_id='chk-offer-1', period_days=30, devices=1)

    response = await _by_id(db, session, 86)

    assert (response.intent_outcome, response.intent_checkout_public_id, response.intent_refusal_kind) == (
        'fulfilled',
        'chk-offer-1',
        None,
    )
    assert (response.intent_reason, response.intent_offer_kopeks, response.purchase_step_pending) == (None, None, False)


async def test_purchase_of_another_order_after_the_refusal_is_bought_not_fulfilled(db, session):
    _intent_payment(
        session,
        payment_id=85,
        created_ago=timedelta(minutes=20),
        status='refused',
        reason='price_changed',
        offer_kopeks=14_911,
        is_paid=True,
        transaction_id=985,
    )
    _purchase(session, minutes_ago=1, checkout_public_id='chk-90-3', period_days=90, devices=1)

    response = await _by_id(db, session, 85)

    assert (response.intent_outcome, response.intent_refusal_kind, response.intent_checkout_public_id) == (
        'refused',
        'bought',
        None,
    )


async def test_purchase_before_the_top_up_does_not_withdraw_the_offer(db, session):
    _purchase(session, minutes_ago=40)
    _intent_payment(
        session,
        payment_id=87,
        created_ago=timedelta(minutes=20),
        status='refused',
        reason='open_order',
        checkout_public_id='chk-open-9',
        is_paid=True,
    )

    assert (await _by_id(db, session, 87)).intent_refusal_kind == 'order'


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


def _replaced_then_paid(session, *, status='refused', paid_ago=timedelta(seconds=30), decided=True, **old):
    """Счёт 70 открыт 10 мин назад, через 4 минуты человек открыл 71 другим способом (70 — «заменён»), а заплатил по 70.
    Суммы разные (сумма 70 нарочно не та, что у 71): экран обязан взять сумму пришедших денег, а не своего счёта.
    Проводка зачисления — как на боевом: `created_at` = время ВЫСТАВЛЕНИЯ счёта (так пишет `_finalize_platega_payment`),
    время прихода денег — `completed_at` (волна 2 заявки 3б: сторож на `created_at` проверял совпадение фикстуры)."""
    now = datetime.now(UTC)
    session.add(
        Transaction(
            id=970,
            user_id=1,
            type='deposit',
            amount_kopeks=12_377,
            created_at=now - timedelta(minutes=10),
            completed_at=now - paid_ago,
        )
    )
    _intent_payment(
        session,
        payment_id=70,
        created_ago=timedelta(minutes=10),
        is_paid=True,
        transaction_id=970,
        provider_status='CONFIRMED',
        amount_kopeks=12_377,
        **{
            'status': status,
            'replaced_by': 71,
            'reason': 'replaced',
            'offer_kopeks': 14_911,
            'offer_tariff_name': 'Базовый',
            **({'decided_at': _iso(paid_ago)} if decided else {}),
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
        assert (response.intent_paid, response.intent_amount_kopeks) == (True, 12_377)
        assert response.amount_kopeks == TOP_UP_30_1  # своя сумма записи — не то, что пришло
        assert response.purchase_step_pending is True


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


async def test_old_replaced_invoice_paid_before_its_outcome_is_written_says_money_came(db, session):
    # Живой путь (волна 1 заявки 3б): старый счёт уже `replaced`, деньги зачислены, исход ещё пишется (или не записался —
    # мина OU). Момент прихода — по проводке зачисления; экран видит «закрыто, но деньги пришли», а не «денег не было».
    _replaced_then_paid(session, status='replaced', decided=False)

    response = await _by_id(db, session, 71)

    assert (response.intent_outcome, response.intent_payment_id, response.intent_paid) == ('closed', 70, True)
    assert (response.intent_refusal_kind, response.purchase_step_pending) == ('retry', True)


async def test_old_invoice_paid_before_the_new_one_without_outcome_is_not_moved_by_a_late_row_write(db, session):
    # Без `decided_at` момент прихода — проводка зачисления, а не `updated_at` (его сдвигает поздний статус, мина OH).
    _replaced_then_paid(session, status='replaced', decided=False, paid_ago=timedelta(minutes=8))
    session.get(dfc.PlategaPayment, 70).updated_at = datetime.now(UTC)
    session.commit()

    assert (await _by_id(db, session, 71)).intent_payment_id == 71


async def test_newest_of_two_paid_old_invoices_wins(db, session):
    _replaced_then_paid(session, paid_ago=timedelta(seconds=90), reason='replaced')
    session.add(Transaction(id=969, user_id=1, type='deposit', completed_at=datetime.now(UTC) - timedelta(seconds=20)))
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
        'сообщение клиенту не дошло — напишите ему сами: деньги на балансе, заказ не оформлен'
    )


async def test_owner_card_says_the_client_got_the_plain_top_up_message_when_the_refusal_did_not_build(
    db, session, webhook, monkeypatch
):
    monkeypatch.setattr(handlers, 'topup_intent_refusal_message', lambda *a, **k: 1 / 0)
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    assert 'Пополнение успешно' in webhook.bot.send_message.await_args.args[1]
    assert webhook.admin[0]['intent_refused'].endswith('клиенту ушло обычное «Пополнение успешно» без объяснения')


async def test_owner_card_does_not_claim_the_plain_message_when_it_failed_too(db, session, webhook, monkeypatch):
    monkeypatch.setattr(handlers, 'topup_intent_refusal_message', lambda *a, **k: 1 / 0)
    webhook.bot.send_message.side_effect = RuntimeError('telegram down')
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    assert webhook.admin[0]['intent_refused'].endswith(
        'сообщение клиенту не дошло — напишите ему сами: деньги на балансе, заказ не оформлен'
    )


async def test_owner_card_of_a_client_without_telegram_does_not_claim_an_explanation(db, session, webhook):
    _set_user(session, telegram_id=None)
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    webhook.bot.send_message.assert_not_awaited()
    assert webhook.admin[0]['intent_refused'].endswith(
        'у клиента нет Telegram — объяснения в боте нет, при желании напишите ему'
    )


# --- вечная кнопка «Оформить за N ₽» / «Оплатить с баланса» ---------------------------------------------------------


def _wallet_callback(shown_ago: timedelta | None):
    data = 'df:a2:30:1:14900'
    if shown_ago is not None:
        data += f':{int((datetime.now(UTC) - shown_ago).timestamp())}'
    return SimpleNamespace(data=data, answer=AsyncMock())


async def _press(db, session, callback):
    checkout = SimpleNamespace(public_id='chk-wallet')
    with (
        patch.object(handlers, '_render_checkout', AsyncMock()) as shown,
        patch.object(
            handlers,
            'create_or_resume_direct_checkout',
            AsyncMock(return_value=SimpleNamespace(checkout=checkout, proceed_to_payment=True)),
        ) as create,
        patch.object(handlers, 'commit_direct_wallet_checkout', AsyncMock(return_value=checkout)) as commit,
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.pay_wallet_fused(callback, _user(session), db, AsyncMock())
    render.shown = shown
    return create, commit, render


async def test_second_tap_of_the_same_button_shows_the_paid_order_not_charged_nothing(db, session):
    # Двойной тап идёт параллельно: второе нажатие видит проводку первого — это СВОЙ заказ (тот же срок и устройства),
    # показываем его, а не «ничего не списали» поверх списания (волна 1 заявки 3б).
    _purchase(session, minutes_ago=0.05, checkout_public_id='chk-own-1', period_days=30, devices=1)

    create, commit, render = await _press(db, session, _wallet_callback(timedelta(minutes=2)))

    create.assert_not_awaited()
    commit.assert_not_awaited()
    render.assert_not_awaited()
    assert render.shown.await_args.args[3].public_id == 'chk-own-1'


@pytest.mark.parametrize(
    ('minutes_ago', 'funding_mode'),
    [(30, 'wallet'), (0.05, 'platega')],  # давняя покупка того же размера; свежая, но картой в кабинете
)
async def test_same_size_purchase_that_is_not_this_tap_is_not_shown_as_paid_by_it(
    db, session, minutes_ago, funding_mode
):
    # Волна 2 заявки 3б: «VPN готов» на нажатие «Оплатить с баланса» читался бы как «продлил», а списания не было.
    _purchase(
        session,
        minutes_ago=minutes_ago,
        checkout_public_id='chk-same-1',
        period_days=30,
        devices=1,
        funding_mode=funding_mode,
    )

    create, commit, render = await _press(db, session, _wallet_callback(timedelta(hours=1)))

    create.assert_not_awaited()
    render.shown.assert_not_awaited()
    assert 'с баланса ничего не списано' in render.await_args.kwargs['caption']


async def test_purchase_of_another_order_after_the_button_is_not_shown_as_this_one(db, session):
    _purchase(session, minutes_ago=1, checkout_public_id='chk-90-2', period_days=90, devices=2)

    create, commit, render = await _press(db, session, _wallet_callback(timedelta(minutes=5)))

    create.assert_not_awaited()
    render.shown.assert_not_awaited()
    assert 'уже прошла оплата' in render.await_args.kwargs['caption']


async def test_button_pressed_after_a_purchase_by_another_path_charges_nothing(db, session):
    _purchase(session, minutes_ago=30)  # купил картой через полчаса после сообщения

    create, commit, render = await _press(db, session, _wallet_callback(timedelta(hours=1)))

    create.assert_not_awaited()
    commit.assert_not_awaited()
    caption = render.await_args.kwargs['caption']
    assert 'С тех пор как бот показал эту кнопку, на вашем аккаунте уже прошла оплата.' in caption
    assert 'с баланса ничего не списано — на балансе 50,37 ₽.' in caption  # где деньги — называем
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


def test_refusal_offer_button_carries_the_moment_the_top_up_began(monkeypatch):
    # Не момент сборки сообщения: покупка между решением и сообщением прошла бы мимо обеих проверок (волна 1, 3б).
    monkeypatch.setattr(handlers, '_shown_at', lambda: 1_999_999_999)
    intent = {
        'created_at': datetime.fromtimestamp(1_791_234_567, UTC).isoformat(),
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


# --- добор по мутациям волны 2 (заявка 3б) ---------------------------------------------------------------------------


async def test_late_cancel_that_cleared_is_paid_still_counts_as_money_came(db, session):
    # Мина OH: поздний «отменён» гасит `is_paid`, а зачисление (`transaction_id`) осталось — деньги пришли.
    _intent_payment(session, payment_id=84, status='cancelled', is_paid=False, transaction_id=984)

    response = await _by_id(db, session, 84)

    assert (response.intent_paid, response.intent_refusal_kind) == (True, 'retry')


@pytest.mark.parametrize(('status', 'extra'), [('pending', {}), ('fulfilled', {'checkout_public_id': 'chk-ok-3'})])
async def test_offer_tariff_name_is_only_for_a_refusal(db, session, status, extra):
    _intent_payment(session, payment_id=83, status=status, offer_tariff_name='Базовый', **extra)

    assert (await _by_id(db, session, 83)).intent_offer_tariff_name is None


async def test_payment_without_intent_has_no_intent_fields_at_all(db, session):
    _intent_payment(session, payment_id=82, is_paid=True, transaction_id=982)
    payment = session.get(dfc.PlategaPayment, 82)
    payment.metadata_json = {'language': 'ru'}
    session.commit()

    response = await _by_id(db, session, 82)

    assert (response.intent_payment_id, response.intent_paid, response.intent_amount_kopeks) == (None, None, None)


@pytest.mark.parametrize(
    ('reason', 'kind_after'),
    [('open_order', 'bought'), ('order_on_review', 'support')],  # «к заказу» гасится покупкой, «в поддержку» — нет
)
async def test_purchase_after_the_refusal_withdraws_only_buying_buttons(db, session, reason, kind_after):
    _intent_payment(
        session, payment_id=81, created_ago=timedelta(minutes=20), status='refused', reason=reason, is_paid=True
    )
    _purchase(session, minutes_ago=1)

    assert (await _by_id(db, session, 81)).intent_refusal_kind == kind_after


@pytest.mark.parametrize('paid_by', ['is_paid', 'transaction_id'])
async def test_old_invoice_counts_as_paid_by_either_sign(db, session, paid_by):
    _replaced_then_paid(session)
    payment = session.get(dfc.PlategaPayment, 70)
    if paid_by == 'is_paid':
        payment.transaction_id = None  # зачисление ещё не связано, но провайдер подтвердил
    else:
        payment.is_paid = False  # мина OH
    session.commit()

    response = await _by_id(db, session, 71)
    assert response.intent_payment_id == (71 if paid_by == 'is_paid' else 70)
    if paid_by == 'is_paid':
        assert response.intent_paid_at is None  # без проводки время прихода неизвестно


@pytest.mark.parametrize(('ago_68', 'ago_69', 'winner'), [(10, 50, 68), (50, 10, 69)])
async def test_the_old_invoice_whose_money_came_last_wins_regardless_of_its_number(db, session, ago_68, ago_69, winner):
    # Номер строки и порядок прихода денег — разные вещи: побеждает пришедший позже, в обоих порядках номеров.
    now = datetime.now(UTC)
    session.add(Transaction(id=968, user_id=1, type='deposit', completed_at=now - timedelta(seconds=ago_68)))
    session.add(Transaction(id=969, user_id=1, type='deposit', completed_at=now - timedelta(seconds=ago_69)))
    _intent_payment(
        session,
        payment_id=68,
        created_ago=timedelta(minutes=12),
        is_paid=True,
        transaction_id=968,
        status='refused',
        reason='cancelled',
        decided_at=_iso(timedelta(seconds=ago_68)),
    )
    _intent_payment(
        session,
        payment_id=69,
        created_ago=timedelta(minutes=11),
        is_paid=True,
        transaction_id=969,
        status='refused',
        reason='replaced',
        decided_at=_iso(timedelta(seconds=ago_69)),
    )
    _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=6))

    assert (await _by_id(db, session, 71)).intent_payment_id == winner


async def test_an_older_invoice_is_not_given_the_outcome_of_a_newer_one(db, session):
    # Опрашивают НЕ самый новый счёт: оплаченный более новый — не его предшественник.
    _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=10))
    _intent_payment(
        session,
        payment_id=72,
        created_ago=timedelta(minutes=6),
        is_paid=True,
        transaction_id=972,
        status='refused',
        reason='price_changed',
        decided_at=_iso(timedelta(seconds=10)),
    )

    assert (await _by_id(db, session, 71)).intent_payment_id == 71


async def test_check_button_answers_with_the_outcome_of_the_paid_old_invoice(db, session, monkeypatch):
    # «Проверить ещё раз» (16в-2) — тот же договор, что `/{id}` и `/latest`.
    _replaced_then_paid(session)

    class _Bot:
        session = SimpleNamespace(close=AsyncMock())

    monkeypatch.setattr(balance_route, 'create_bot', lambda: _Bot())
    monkeypatch.setattr(balance_route, '_is_checkable', lambda record: True)

    async def manual_check(db_, method, payment_id, service):
        from app.services.payment_verification_service import get_payment_record

        return await get_payment_record(db_, method, payment_id)

    monkeypatch.setattr(balance_route, 'run_manual_check', manual_check)
    checked = await balance_route.check_payment_status(method='platega', payment_id=71, user=_user(session), db=db)
    monkeypatch.setattr(balance_route, '_is_checkable', lambda record: False)
    unavailable = await balance_route.check_payment_status(method='platega', payment_id=71, user=_user(session), db=db)
    monkeypatch.setattr(balance_route, '_is_checkable', lambda record: True)
    monkeypatch.setattr(balance_route, 'run_manual_check', AsyncMock(return_value=None))
    failed = await balance_route.check_payment_status(method='platega', payment_id=71, user=_user(session), db=db)

    for answer in (checked, unavailable, failed):
        assert (answer.payment.intent_payment_id, answer.payment.intent_outcome) == (70, 'refused')


def test_moment_in_the_wallet_button_is_now(monkeypatch):
    now = datetime.now(UTC).timestamp()

    assert abs(handlers._shown_at() - now) <= 2


def test_moment_of_a_top_up_without_time_zone_is_read_as_utc():
    assert handlers._intent_moment({'created_at': '2026-10-08T10:00:00'}) == int(
        datetime(2026, 10, 8, 10, 0, tzinfo=UTC).timestamp()
    )


@pytest.mark.parametrize(('period_days', 'devices'), [(30, 2), (90, 1)])  # тот же срок или те же устройства — не тот же
async def test_own_tap_needs_both_the_same_period_and_devices(db, session, period_days, devices):
    _purchase(session, minutes_ago=0.05, checkout_public_id='chk-near-1', period_days=period_days, devices=devices)

    _, _, render = await _press(db, session, _wallet_callback(timedelta(minutes=2)))

    render.shown.assert_not_awaited()
    assert 'с баланса ничего не списано' in render.await_args.kwargs['caption']


async def test_english_guard_names_the_balance(db, session):
    _set_user(session, language='en')
    session.expire_all()
    _purchase(session, minutes_ago=30)

    _, _, render = await _press(db, session, _wallet_callback(timedelta(hours=1)))

    assert 'nothing was taken from your balance — your balance is ₽50.37.' in render.await_args.kwargs['caption']


async def test_moment_too_large_for_a_date_does_nothing(db, session):
    create, _, render = await _press(
        db, session, SimpleNamespace(data='df:a2:30:1:14900:99999999999999999999', answer=AsyncMock())
    )

    create.assert_not_awaited()
    render.assert_not_awaited()

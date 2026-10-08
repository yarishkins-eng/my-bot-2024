"""ВК-16, часть 16а-2, заявка 2 (08.10.2026): отказ — одно сообщение с кнопкой по причине, отмена гасит доплату,
писатели меток не откатывают метку и исход.

Решение владельца 05.10.2026 «Оформляется само»; замысел v2 (`РЕВЬЮ-ВК-16-замысел-20261006.md`), правила 5 и 7; план ВК,
16а-2 (в) и блок «Заявке 2 добавить». Стенд — настоящий движок SQLite из сторожей 16а-1 и заявки 1; подменены только
сами функции оформления и сеть. Цены нарочно не круглые.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database.models import PlategaPayment, SubscriptionCheckout, Transaction, User
from app.handlers.subscription import device_first as handlers
from app.services import device_first_checkout_service as dfc
from app.services.payment.platega import PlategaPaymentMixin
from tests.cabinet.test_vk16_topup_autocomplete import (  # noqa: F401 -- фикстуры стенда заявки 1
    TRIAL,
    _complete,
    _paid,
    _pay,
    buy,
    checkouts,
    webhook,
)
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401 -- фикстуры стенда 16а-1
    BALANCE,
    PRICE_30_1,
    TOP_UP_30_1,
    _intent_payment,
    _options,
    _set_user,
    db,
    env,
    session,
)


def _intent(session, payment_id: int = 97) -> dict:
    session.expire_all()
    return dfc.topup_intent_of(session.get(PlategaPayment, payment_id))


def _write_intent(session, payment_id: int = 97, **changes) -> None:
    """Запись ДРУГОГО писателя прямо в базу (кнопка бота, оформление) — снимок в карте объектов остаётся старым."""
    row = session.execute(text('SELECT metadata_json FROM platega_payments WHERE id = :id'), {'id': payment_id}).one()
    metadata = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    metadata[dfc.TOPUP_INTENT_KEY] = {**metadata[dfc.TOPUP_INTENT_KEY], **changes}
    session.execute(
        text('UPDATE platega_payments SET metadata_json = :m WHERE id = :id'),
        {'m': json.dumps(metadata), 'id': payment_id},
    )
    session.commit()


def _buttons(keyboard) -> list[tuple[str, str | None, str | None]]:
    return [(button.text, button.callback_data, button.url) for row in keyboard.inline_keyboard for button in row]


# --- причины — закрытый набор ------------------------------------------------------------------------------------


def test_refusal_reasons_are_a_closed_set_named_literally():
    assert {
        'expired',
        'replaced',
        'cancelled',
        'disabled',
        'account_erasure',
        'restricted',
        'order_on_review',
        'open_order',
        'already_purchased',
        'subscription_changed',
        'unavailable',
        'price_changed',
        'balance_short',
        'technical_error',
    } == dfc.TOPUP_INTENT_REFUSAL_REASONS


@pytest.mark.parametrize(
    ('raw', 'reason'),
    [
        ('operator_review_required', 'order_on_review'),
        ('legacy_trial_reconciliation_required', 'order_on_review'),
        ('open_checkout_exists', 'open_order'),
        ('external_invoice_active', 'open_order'),
        ('funding_mode_locked', 'open_order'),
        ('reprice_required', 'price_changed'),
        ('wallet_insufficient', 'balance_short'),
        ('subscription_restricted', 'restricted'),
        ('account_closing', 'account_erasure'),
        ('feature_disabled', 'unavailable'),
        ('legacy_only', 'unavailable'),
        ('location_policy_not_sellable', 'unavailable'),
        ('invalid_state', 'technical_error'),
        ('совсем новый код', 'technical_error'),
        (None, 'technical_error'),
        ('expired', 'expired'),
        ('already_purchased', 'already_purchased'),
    ],
)
def test_raw_codes_map_into_the_closed_set(raw, reason):
    assert dfc.topup_intent_refusal_reason(raw) == reason


@pytest.mark.parametrize(
    ('reason', 'kind'),
    [
        ('already_purchased', 'bought'),
        ('open_order', 'order'),
        ('order_on_review', 'support'),
        ('restricted', 'support'),
        ('account_erasure', 'support'),
        ('expired', 'retry'),
        ('replaced', 'retry'),
        ('cancelled', 'retry'),
        ('disabled', 'retry'),
        ('subscription_changed', 'retry'),
        ('unavailable', 'retry'),
        ('price_changed', 'retry'),
        ('balance_short', 'retry'),
        ('technical_error', 'retry'),
        ('operator_review_required', 'support'),  # сырой код судится по его месту в наборе
    ],
)
def test_each_reason_gets_its_kind_of_button(reason, kind):
    assert dfc.topup_intent_refusal_kind(reason) == kind


async def test_unknown_code_from_the_purchase_is_stored_closed_with_the_raw_code_aside(session, buy):
    async def crashed(checkout):
        raise dfc.DeviceFirstError('invalid_state', 'something new')

    buy.commit_effect = crashed
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason'], stored['detail']) == ('refused', 'technical_error', 'invalid_state')


async def test_closed_reason_carries_no_detail(session, buy):
    _paid(session, subscription_id=40)

    _, stored = await _complete(session)

    assert (stored['reason'], 'detail' in stored) == ('subscription_changed', False)


# --- перепроверка под замком прямо перед списанием ----------------------------------------------------------------


def _between_create_and_debit(monkeypatch, effect) -> None:
    inner = dfc.create_or_resume_direct_checkout

    async def create(db_, **kwargs):
        resolved = await inner(db_, **kwargs)
        effect()
        return resolved

    monkeypatch.setattr(dfc, 'create_or_resume_direct_checkout', create)


async def test_cancel_pressed_after_the_check_but_before_the_debit_wins(session, buy, monkeypatch):
    _paid(session)
    _between_create_and_debit(monkeypatch, lambda: _write_intent(session, status='cancelled'))

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'cancelled')
    assert buy.commit == []  # не списано
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1
    own = session.get(SubscriptionCheckout, 501)
    assert (own.lifecycle_state, own.terminal_reason) == ('cancelled', 'topup_intent_refused')


async def test_purchase_by_another_path_after_the_check_but_before_the_debit_wins(session, buy, monkeypatch):
    _paid(session)

    def bought():
        session.add(Transaction(user_id=1, type='subscription_payment', amount_kopeks=1, created_at=datetime.now(UTC)))
        session.commit()

    _between_create_and_debit(monkeypatch, bought)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'already_purchased')
    assert buy.commit == []
    assert 'offer_kopeks' not in stored  # купившему кнопку покупки не даём


async def test_subscription_swapped_after_the_check_but_before_the_debit_wins(session, buy, env, monkeypatch):
    _paid(session)

    def swapped():
        env.options.return_value = {**_options(), 'current_subscription': {'id': 88, 'tariff_id': 5, 'is_trial': True}}

    _between_create_and_debit(monkeypatch, swapped)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'subscription_changed')
    assert buy.commit == []


def _subscription(session, *, sub_id: int, tariff_id: int, is_trial: int, status: str, end: str) -> None:
    session.execute(
        text(
            'INSERT INTO subscriptions (id, user_id, tariff_id, is_trial, status, end_date, created_at) '
            "VALUES (:id, 1, :t, :trial, :status, :end, '2026-09-01 00:00:00.000000')"
        ),
        {'id': sub_id, 't': tariff_id, 'trial': is_trial, 'status': status, 'end': end},
    )
    session.commit()


async def test_subscription_under_the_lock_is_read_fresh_and_without_a_commit(db, session, monkeypatch):
    _subscription(session, sub_id=41, tariff_id=5, is_trial=1, status='active', end='2026-10-10 00:00:00.000000')
    _subscription(session, sub_id=42, tariff_id=6, is_trial=0, status='expired', end='2027-01-01 00:00:00.000000')
    _subscription(session, sub_id=43, tariff_id=7, is_trial=0, status='trial', end='2027-02-01 00:00:00.000000')
    stale = session.get(dfc.Subscription, 41)  # сверка загрузила раньше — объект в карте сессии
    session.execute(text('UPDATE subscriptions SET tariff_id = 9, is_trial = 0 WHERE id = 41'))
    session.commit()
    assert stale.tariff_id == 5
    monkeypatch.setattr(db, 'commit', AsyncMock(side_effect=AssertionError('коммит снял бы замок перепроверки')))

    assert await dfc._subscription_identity(db, 1) == {'id': 41, 'tariff_id': 9, 'is_trial': False}


async def test_without_any_subscription_the_identity_is_none(db, session):
    assert await dfc._subscription_identity(db, 1) is None


async def test_trial_is_preferred_over_an_expired_one_with_a_later_end(db, session):
    _subscription(session, sub_id=42, tariff_id=6, is_trial=0, status='expired', end='2027-01-01 00:00:00.000000')
    _subscription(session, sub_id=43, tariff_id=7, is_trial=1, status='trial', end='2026-11-01 00:00:00.000000')

    assert (await dfc._subscription_identity(db, 1))['id'] == 43


# --- кнопка «Оформить» по свежей цене ---------------------------------------------------------------------------


async def test_retry_refusal_offers_the_fresh_price_and_tariff(session, buy, env):
    cheaper = 13_377
    options = _options()
    options['price_matrix'][0]['prices'][0]['price_kopeks'] = cheaper
    env.options.return_value = options
    _paid(session, status='cancelled')

    _, stored = await _complete(session)

    assert (stored['reason'], stored['offer_kopeks'], stored['offer_tariff_name']) == ('cancelled', cheaper, 'Базовый')


async def test_no_offer_when_the_balance_does_not_cover_the_fresh_price(session, buy, env):
    options = _options()
    options['price_matrix'][0]['prices'][0]['price_kopeks'] = BALANCE + TOP_UP_30_1 + 1
    env.options.return_value = options
    _paid(session)

    _, stored = await _complete(session)

    assert stored['reason'] == 'price_changed'
    assert 'offer_kopeks' not in stored


async def test_balance_exactly_covering_the_fresh_price_still_gets_the_offer(session, buy, env):
    options = _options()
    options['price_matrix'][0]['prices'][0]['price_kopeks'] = BALANCE + TOP_UP_30_1
    env.options.return_value = options
    _paid(session, status='replaced')

    _, stored = await _complete(session)

    assert stored['offer_kopeks'] == BALANCE + TOP_UP_30_1


@pytest.mark.parametrize('lifecycle_state', ['awaiting_funds', 'operator_review'])
async def test_no_offer_for_an_open_or_reviewed_order(session, buy, env, lifecycle_state):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state=lifecycle_state, public_id='chk-open')
    _paid(session)

    _, stored = await _complete(session)

    assert 'offer_kopeks' not in stored


async def test_no_offer_when_the_tariff_is_no_longer_sold(session, buy, env):
    env.options.return_value = {**_options(), 'eligible': False}
    _paid(session)

    _, stored = await _complete(session)

    assert stored['reason'] == 'unavailable'
    assert 'offer_kopeks' not in stored


# --- сообщение об отказе — одно, кнопка по причине ------------------------------------------------------------


def _user(language: str = 'ru') -> SimpleNamespace:
    return SimpleNamespace(id=1, language=language)


def _message(reason: str, language: str = 'ru', **intent):
    return handlers.topup_intent_refusal_message(
        _user(language),
        {'status': 'refused', 'reason': reason, 'period_days': 30, 'devices': 2, **intent},
        amount_kopeks=9_900,
        balance_kopeks=14_937,
    )


def test_already_bought_has_no_button_to_buy_again():
    text_, keyboard = _message('already_purchased')

    assert 'Оплата получена: 99 ₽' in text_ and 'дважды мы не списываем' in text_
    assert 'На балансе: 149,37 ₽' in text_.split('\n')
    assert _buttons(keyboard) == [('В главное меню', 'back_to_menu', None)]


def test_open_order_leads_to_that_order():
    text_, keyboard = _message('open_order')

    assert 'у вас уже есть открытый заказ' in text_ and 'откройте заказ' in text_
    assert [b[1] for b in _buttons(keyboard)] == ['df:start', 'back_to_menu']


@pytest.mark.parametrize('reason', ['order_on_review', 'restricted', 'account_erasure', 'operator_review_required'])
def test_review_and_blocked_go_to_support_without_a_buy_button(monkeypatch, reason):
    monkeypatch.setattr(settings, 'SUPPORT_USERNAME', '@teplo_help', raising=False)
    text_, keyboard = _message(reason, offer_kopeks=14_900)

    assert 'напишите в поддержку' in text_
    buttons = _buttons(keyboard)
    assert buttons[0][0] == 'Написать в поддержку' and buttons[0][2]
    assert not any(str(b[1]).startswith(('df:a2:', 'df:e2', 'df:start')) for b in buttons)


def test_order_on_review_says_review_and_blocked_says_cannot_order():
    assert 'предыдущий заказ на проверке' in _message('order_on_review')[0]
    assert 'оформить подписку сейчас нельзя' in _message('restricted')[0]


def test_retry_offers_one_tap_at_the_fresh_price_and_another_period():
    text_, keyboard = _message('price_changed', offer_kopeks=13_377, offer_tariff_name='Базовый')

    assert 'Заказ сам не оформился: цена изменилась. Деньги на балансе.' in text_
    assert _buttons(keyboard) == [
        ('Оформить: Базовый · 1 месяц · 133,77 ₽', 'df:a2:30:2:13377', None),
        ('‹ Выбрать другой срок', 'df:e2', None),
        ('В главное меню', 'back_to_menu', None),
    ]


def test_retry_without_an_offer_only_lets_choose_a_period():
    text_, keyboard = _message('technical_error')

    assert 'Заказ сам не оформился. Деньги на балансе.' in text_
    assert [b[:2] for b in _buttons(keyboard)] == [('Выбрать срок', 'df:e2'), ('В главное меню', 'back_to_menu')]


@pytest.mark.parametrize(
    ('reason', 'why'),
    [
        ('expired', 'с выбора заказа прошло больше часа'),
        ('replaced', 'вы открыли другой счёт на этот заказ'),
        ('cancelled', 'вы отменили заказ'),
        ('subscription_changed', 'ваша подписка изменилась'),
        ('balance_short', 'на балансе не хватило денег'),
    ],
)
def test_retry_names_why(reason, why):
    assert f'Заказ сам не оформился: {why}.' in _message(reason)[0]


def test_english_refusal():
    text_, keyboard = _message('price_changed', language='en', offer_kopeks=13_377, offer_tariff_name='Basic')

    assert 'Payment received: ₽99' in text_ and 'the price has changed' in text_ and 'Balance: ₽149.37' in text_
    assert _buttons(keyboard)[0] == ('Order: Basic · 1 month · ₽133.77', 'df:a2:30:2:13377', None)


# --- зачисление: отказ уходит одним сообщением, исход не откатывается ------------------------------------------


async def test_webhook_refusal_is_one_message_with_the_button_and_the_owner_hears_why(db, session, webhook):
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    webhook.bot.send_message.assert_awaited_once()
    text_ = webhook.bot.send_message.await_args.args[1]
    assert 'Пополнение успешно' not in text_ and 'ваша подписка изменилась' in text_
    buttons = _buttons(webhook.bot.send_message.await_args.kwargs['reply_markup'])
    assert buttons[0][1] == f'df:a2:30:1:{PRICE_30_1}'
    assert webhook.admin[0]['intent_refused'] == (
        'Заказ по доплате бот сам не оформил (subscription_changed) — деньги остались на балансе, '
        'клиенту ушло сообщение с кнопкой'
    )


async def test_refusal_message_failure_is_logged_and_does_not_stop_the_webhook(db, session, webhook, monkeypatch):
    monkeypatch.setattr(handlers, 'topup_intent_refusal_message', lambda *a, **k: 1 / 0)
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    assert await _pay(db, webhook) is True
    assert _intent(session)['status'] == 'refused'


async def test_final_write_keeps_the_label_written_after_the_webhook_snapshot(db, session, webhook, monkeypatch):
    # Исход записать не удалось, а кнопка бота успела поставить метку: финальная запись вебхука присваивает
    # метаданные ЦЕЛИКОМ — и вернула бы `pending` из своего снимка (план ВК, 16а-2 (в)).
    async def label_then_fail(**kwargs):
        _write_intent(session, status='cancelled')

    monkeypatch.setattr(dfc, 'complete_topup_intent', label_then_fail)
    _intent_payment(session, payment_id=97, **TRIAL)

    await _pay(db, webhook)

    session.expire_all()
    stored = session.get(PlategaPayment, 97)
    assert dfc.topup_intent_of(stored)['status'] == 'cancelled'
    assert stored.metadata_json['balance_credited'] is True


# --- мина OQ: проверка статуса не откатывает метку -----------------------------------------------------------


async def test_status_check_keeps_a_label_written_while_it_was_asking_the_provider(db, session):
    _intent_payment(session, payment_id=97, provider_status='PENDING')

    async def get_transaction(transaction_id):
        _write_intent(session, status='replaced', replaced_by=98)
        return {'status': 'CANCELED', 'id': transaction_id}

    service = PlategaPaymentMixin()
    service.platega_service = SimpleNamespace(get_transaction=AsyncMock(side_effect=get_transaction))

    result = await service.get_platega_payment_status(db, 97)

    session.expire_all()
    stored = session.get(PlategaPayment, 97)
    assert result['status'] == 'CANCELED' and stored.status == 'CANCELED'
    assert dfc.topup_intent_of(stored)['status'] == 'replaced'
    assert stored.metadata_json['remote_status'] == {'status': 'CANCELED', 'id': 'tx-97'}


# --- отмена гасит доплату ------------------------------------------------------------------------------------


async def test_cancel_marks_only_unpaid_pending_intents_of_this_person(db, session):
    _intent_payment(session, payment_id=91)
    _intent_payment(session, payment_id=92, user_id=2)
    _intent_payment(session, payment_id=93, status='replaced')

    assert await dfc.cancel_topup_intents(db, user_id=1) is None

    assert _intent(session, 91)['status'] == 'cancelled' and _intent(session, 91)['cancelled_at']
    assert _intent(session, 92)['status'] == 'pending'
    assert _intent(session, 93)['status'] == 'replaced'


async def test_cancel_never_touches_an_intent_whose_money_arrived(db, session):
    _intent_payment(session, payment_id=91, is_paid=True)
    _intent_payment(session, payment_id=92, transaction_id=5)

    assert await dfc.cancel_topup_intents(db, user_id=1) == 'paid'

    assert (_intent(session, 91)['status'], _intent(session, 92)['status']) == ('pending', 'pending')


async def test_cancel_says_fulfilled_when_the_top_up_already_bought_the_order(db, session):
    decided = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _intent_payment(session, payment_id=91, status='fulfilled', is_paid=True, decided_at=decided)
    _intent_payment(session, payment_id=92, status='refused', is_paid=True, decided_at=decided)

    assert await dfc.cancel_topup_intents(db, user_id=1) == 'fulfilled'


async def test_cancel_says_paid_for_a_refused_top_up_within_the_hour(db, session):
    decided = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _intent_payment(session, payment_id=91, status='refused', is_paid=True, decided_at=decided)

    assert await dfc.cancel_topup_intents(db, user_id=1) == 'paid'


async def test_money_older_than_an_hour_does_not_change_the_cancel_words(db, session):
    decided = (datetime.now(UTC) - timedelta(minutes=70)).isoformat()
    _intent_payment(
        session,
        payment_id=91,
        status='fulfilled',
        is_paid=True,
        decided_at=decided,
        created_ago=timedelta(minutes=80),
    )

    assert await dfc.cancel_topup_intents(db, user_id=1) is None


async def test_cancelled_then_paid_is_refused_with_the_one_tap_offer(db, session, webhook):
    _intent_payment(session, payment_id=97, **TRIAL)
    await dfc.cancel_topup_intents(db, user_id=1)

    await _pay(db, webhook)

    assert (_intent(session)['status'], _intent(session)['reason']) == ('refused', 'cancelled')
    assert 'вы отменили заказ' in webhook.bot.send_message.await_args.args[1]
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1  # не списано


# --- кнопки бота ----------------------------------------------------------------------------------------------


def _callback(data: str) -> SimpleNamespace:
    return SimpleNamespace(data=data, answer=AsyncMock())


@pytest.mark.parametrize(
    ('note', 'expected', 'absent'),
    [
        (None, 'Заказ отменён. Деньги не списаны.', 'Доплата'),
        ('paid', 'Заказ отменён. Доплата уже пришла — деньги на балансе.', 'Деньги не списаны'),
        ('fulfilled', 'заказ по ней оформлен с баланса', 'Деньги не списаны'),
    ],
)
async def test_cancel_fused_words_follow_the_money(note, expected, absent):
    user = SimpleNamespace(id=17, language='ru')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(return_value=note)) as gas,
        patch.object(handlers, '_has_order_in_flight', AsyncMock(return_value=False)),
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.cancel_fused(_callback('df:x2'), user, AsyncMock(), SimpleNamespace(clear=AsyncMock()))

    gas.assert_awaited_once()
    assert gas.await_args.kwargs == {'user_id': 17}
    caption = render.await_args.kwargs['caption']
    assert expected in caption and absent not in caption


async def test_cancel_that_could_not_gas_the_top_up_does_nothing_and_asks_again():
    user = SimpleNamespace(id=17, language='ru')
    db = AsyncMock()
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(side_effect=SQLAlchemyError('down'))),
        patch.object(handlers, '_has_order_in_flight', AsyncMock(return_value=False)) as in_flight,
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.cancel_fused(_callback('df:x2'), user, db, SimpleNamespace(clear=AsyncMock()))

    db.rollback.assert_awaited_once()
    in_flight.assert_not_awaited()
    assert 'Заказ отменён' not in render.await_args.kwargs['caption']
    assert 'нажмите ещё раз' in render.await_args.kwargs['caption']


async def test_cancel_of_an_order_names_the_money_too():
    user = SimpleNamespace(id=17, language='ru')
    checkout = SimpleNamespace(public_id='chk-1')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(return_value='paid')) as gas,
        patch.object(handlers, 'get_owned_checkout', AsyncMock(return_value=checkout)),
        patch.object(handlers, 'settlement_mode', lambda c: 'legacy'),
        patch.object(handlers, 'cancel_checkout', AsyncMock(return_value=checkout)),
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.cancel(_callback('df:x:chk-1'), user, AsyncMock(), SimpleNamespace(clear=AsyncMock()))

    gas.assert_awaited_once()
    assert render.await_args.kwargs['caption'] == 'Заказ отменён. Доплата уже пришла — деньги на балансе.'


async def test_abandon_gases_the_top_up_and_keeps_the_old_link_warning():
    user = SimpleNamespace(id=17, language='ru')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(return_value=None)) as gas,
        patch.object(
            handlers,
            'abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=SimpleNamespace(lifecycle_state='cancelled')),
        ),
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.abandon(_callback('df:xa:chk-1'), user, AsyncMock(), SimpleNamespace(clear=AsyncMock()))

    gas.assert_awaited_once()
    caption = render.await_args.kwargs['caption']
    assert caption.startswith('Заказ отменён. Деньги не списаны.\n\nЕсли старая ссылка будет оплачена позднее')


@pytest.mark.parametrize(('handler', 'data'), [('change_selection', 'df:e:chk-1'), ('change_selection_fused', 'df:e2')])
async def test_change_buttons_gas_the_top_up_first(handler, data):
    user = SimpleNamespace(id=17, language='ru')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(return_value=None)) as gas,
        patch.object(handlers, 'get_owned_checkout', AsyncMock(return_value=SimpleNamespace(public_id='chk-1'))),
        patch.object(handlers, 'build_purchase_options', AsyncMock(return_value={'eligible': True})),
        patch.object(handlers, '_period_page', AsyncMock()) as period_page,
    ):
        await getattr(handlers, handler)(
            _callback(data),
            user,
            AsyncMock(),
            SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock()),
        )

    gas.assert_awaited_once()
    period_page.assert_awaited_once()


@pytest.mark.parametrize(('handler', 'data'), [('change_selection', 'df:e:chk-1'), ('change_selection_fused', 'df:e2')])
async def test_change_buttons_stop_when_the_top_up_could_not_be_gassed(handler, data):
    user = SimpleNamespace(id=17, language='ru')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(side_effect=SQLAlchemyError('down'))),
        patch.object(handlers, '_period_page', AsyncMock()) as period_page,
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()),
    ):
        await getattr(handlers, handler)(_callback(data), user, AsyncMock(), SimpleNamespace(get_data=AsyncMock()))

    period_page.assert_not_awaited()


# --- карточка владельцу ---------------------------------------------------------------------------------------


async def test_owner_card_prints_the_refusal_as_is_and_never_asks_the_cart(monkeypatch):
    from app.services import admin_notification_service as module

    service = module.AdminNotificationService(bot=None)
    captured = {}
    monkeypatch.setattr(service, '_is_enabled', lambda: True)
    monkeypatch.setattr(service, '_owner_cart_hint', AsyncMock(side_effect=AssertionError('корзину не спрашиваем')))

    def build(*args, cart_hint=None, **kwargs):
        captured['hint'] = cart_hint
        raise ValueError('stop here')

    monkeypatch.setattr(service, '_build_balance_topup_message', build)
    await service.send_balance_topup_notification(
        SimpleNamespace(id=1),
        SimpleNamespace(amount_kopeks=1, completed_at=None, created_at=None),
        0,
        topup_status='t',
        referrer_info='',
        subscription=None,
        promo_group=None,
        intent_refused='не оформил',
    )

    assert captured['hint'] == module.OwnerCartHint('не оформил', False, final=True)


def test_owner_card_line_with_the_refusal():
    from app.services import admin_notification_service as module

    service = module.AdminNotificationService(bot=None)
    user = SimpleNamespace(id=1, balance_kopeks=14_937, has_had_paid_subscription=False)
    transaction = SimpleNamespace(amount_kopeks=9_900, description='', payment_method='platega')
    with patch.object(service, '_owner_card', lambda *lines: '\n'.join(line for line in lines if line)):
        with (
            patch.object(service, '_owner_title', lambda *a: 'T'),
            patch.object(service, '_owner_who', lambda *a, **k: 'W'),
        ):
            message = service._build_balance_topup_message(
                user,
                transaction,
                5_037,
                topup_status='🔄 Пополнение',
                referrer_info='Нет',
                subscription=None,
                promo_group=None,
                cart_hint=module.OwnerCartHint('Заказ по доплате бот сам не оформил (x)', False, final=True),
            )

    assert f'стало {settings.format_price(14_937)}. Заказ по доплате бот сам не оформил (x)' in message
    assert 'Корзины нет' not in message and 'карточка придёт' not in message

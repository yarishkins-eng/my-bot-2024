"""ВК-16, часть 16а-2, заявка 1 (07.10.2026): деньги доплаты пришли — заказ оформляется сам.

Решение владельца 05.10.2026 «Оформляется само» разворачивает решение 02.09 «одно нажатие». Сверку исполняет настоящий
движок SQLite (стенд 16а-1 плюс таблица заказов), зачисление — настоящий `_finalize_platega_payment` через вебхук;
подменены только сами функции оформления (`create_or_resume_direct_checkout`, `commit_direct_wallet_checkout` — у них
свои сторожа) и сеть. «Своя сессия» оформления в тестах — та же база SQLite: проверяем решения и записи, а не изоляцию.
Цены нарочно не круглые: свежая цена отличается от котировки, чтобы видеть, какую из них отдают в оформление.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.config import settings
from app.database.models import PlategaPayment, SubscriptionCheckout, Transaction, User
from app.services import device_first_checkout_service as dfc, device_first_payment_service
from app.services.payment.platega import PlategaPaymentMixin
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401 -- фикстуры стенда 16а-1
    BALANCE,
    PRICE_30_1,
    TOP_UP_30_1,
    _AsyncOverSync,
    _intent_payment,
    _options,
    _set_user,
    db,
    env,
    session,
)


TRIAL = {'had_subscription': True, 'subscription_id': 41, 'subscription_tariff_id': 5, 'subscription_is_trial': True}


@pytest.fixture
def checkouts(session):
    columns = ', '.join(
        'id INTEGER PRIMARY KEY' if column.name == 'id' else column.name
        for column in SubscriptionCheckout.__table__.columns
    )
    session.execute(text(f'CREATE TABLE subscription_checkouts ({columns})'))
    session.commit()


def _add_checkout(session, *, checkout_id: int, source: str, lifecycle_state: str = 'confirmed', **values):
    session.execute(
        text(
            'INSERT INTO subscription_checkouts (id, public_id, user_id, source, lifecycle_state, period_days, '
            'selected_device_limit) VALUES (:id, :pid, 1, :source, :state, 30, 1)'
        ),
        {'id': checkout_id, 'pid': f'chk-{checkout_id}', 'source': source, 'state': lifecycle_state},
    )
    for name, value in values.items():
        session.execute(
            text(f'UPDATE subscription_checkouts SET {name} = :v WHERE id = :id'), {'v': value, 'id': checkout_id}
        )
    session.commit()
    return session.get(SubscriptionCheckout, checkout_id)


@pytest.fixture
def buy(monkeypatch, session, db, env, checkouts):
    """Оформление: создаёт свой заказ и списывает с баланса — как «Списать … и оформить», без пробного и панели."""
    calls = SimpleNamespace(create=[], commit=[], commit_effect=None, resume=None)

    @asynccontextmanager
    async def own_session():
        yield db

    async def create(db_, **kwargs):
        calls.create.append(kwargs)
        if calls.resume is not None:
            return dfc.FusedDirectCheckout(checkout=calls.resume, proceed_to_payment=True)
        checkout = _add_checkout(session, checkout_id=500 + len(calls.create), source=kwargs['source'])
        return dfc.FusedDirectCheckout(checkout=checkout, proceed_to_payment=True)

    async def commit(db_, *, public_id, user_id):
        calls.commit.append(public_id)
        checkout = session.query(SubscriptionCheckout).filter_by(public_id=public_id).one()
        if calls.commit_effect is not None:
            return await calls.commit_effect(checkout)
        price = calls.create[-1]['expected_tariff_total_kopeks']
        checkout.lifecycle_state = 'fulfilling'
        checkout.funding_mode = 'wallet'
        checkout.financial_committed_at = datetime.now(UTC)
        user = session.get(User, user_id)
        user.balance_kopeks -= price
        session.add(Transaction(user_id=user_id, type='subscription_payment', amount_kopeks=price, is_completed=True))
        session.commit()
        return checkout

    monkeypatch.setattr(dfc, '_topup_intent_session', own_session)
    monkeypatch.setattr(dfc, 'create_or_resume_direct_checkout', create)
    monkeypatch.setattr(dfc, 'commit_direct_wallet_checkout', commit)
    return calls


def _paid(session, payment_id: int = 97, **intent):
    """Счёт с намерением, уже зачисленный (так он выглядит в момент крючка)."""
    payment = _intent_payment(session, payment_id=payment_id, **{**TRIAL, **intent})
    _set_user(session, balance_kopeks=BALANCE + TOP_UP_30_1)
    return payment


async def _complete(session, payment_id: int = 97):
    final = await dfc.complete_topup_intent(payment_id=payment_id)
    session.expire_all()
    return final, dfc.topup_intent_of(session.get(PlategaPayment, payment_id))


# --- успех ------------------------------------------------------------------------------------------------------


async def test_trial_holder_order_is_fulfilled_from_the_balance_at_the_fresh_price(session, buy):
    _paid(session)

    final, stored = await _complete(session)

    assert final == stored
    assert (stored['status'], stored['checkout_public_id']) == ('fulfilled', 'chk-501')
    assert stored['decided_at'].endswith('+00:00')  # с поясом (план ВК 16а-2 (а))
    request = buy.create[0]
    assert (request['funding_mode'], request['source'], request['method_key']) == ('wallet', 'topup_intent', None)
    assert (request['period_days'], request['selected_device_limit']) == (30, 1)
    assert request['expected_tariff_total_kopeks'] == PRICE_30_1
    assert buy.commit == ['chk-501']
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1 - PRICE_30_1


async def test_newcomer_without_subscription_is_fulfilled(session, buy, env):
    env.options.return_value = {**_options(), 'current_subscription': None}
    _paid(
        session, had_subscription=False, subscription_id=None, subscription_tariff_id=None, subscription_is_trial=None
    )

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'


async def test_renewal_of_the_same_paid_subscription_is_fulfilled(session, buy, env):
    env.options.return_value = {**_options(), 'current_subscription': {'id': 77, 'tariff_id': 3, 'is_trial': False}}
    _paid(session, subscription_id=77, subscription_tariff_id=3, subscription_is_trial=False)

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'


async def test_cheaper_now_is_fulfilled_at_the_cheaper_price_not_the_quote(session, buy, env):
    cheaper = PRICE_30_1 - 1_037
    options = _options()
    options['price_matrix'][0]['prices'][0]['price_kopeks'] = cheaper
    env.options.return_value = options
    _paid(session)

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'
    assert buy.create[0]['expected_tariff_total_kopeks'] == cheaper


@pytest.mark.parametrize(('column', 'value'), [('restriction_topup', True)])
async def test_topup_restriction_and_switched_off_platega_do_not_refuse_money_already_here(
    session, buy, monkeypatch, column, value
):
    # План ВК 16а-2 (д): деньги уже пришли — запрет ПОПОЛНЕНИЯ и выключенная Platega отказом не становятся.
    _set_user(session, **{column: value})
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', False, raising=False)
    _paid(session)

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'


async def test_open_quote_without_invoice_does_not_block(session, buy, env):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state='confirmed', public_id='chk-quote')
    _paid(session)

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'


async def test_purchase_made_before_the_intent_does_not_block(session, buy):
    payment = _paid(session)
    before = datetime.fromisoformat(dfc.topup_intent_of(payment)['created_at']) - timedelta(seconds=1)
    session.add(Transaction(user_id=1, type='subscription_payment', amount_kopeks=1, created_at=before))
    session.commit()

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'


# --- отказ: покупки нет, свой заказ не остаётся ---------------------------------------------------------------


@pytest.mark.parametrize(
    ('intent', 'reason'),
    [
        ({'created_ago': timedelta(minutes=61)}, 'expired'),
        ({'status': 'replaced', 'replaced_by': 98}, 'replaced'),
        ({'status': 'cancelled'}, 'cancelled'),
        ({'subscription_id': 40}, 'subscription_changed'),
        ({'subscription_tariff_id': 3}, 'subscription_changed'),
        ({'subscription_is_trial': False}, 'subscription_changed'),
        ({'quote_kopeks': PRICE_30_1 - 1}, 'price_changed'),
        ({'period_days': 45}, 'unavailable'),
    ],
)
async def test_intent_that_no_longer_matches_is_refused_without_buying(session, buy, intent, reason):
    _paid(session, **intent)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', reason)
    assert buy.create == []
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1


async def test_newcomer_whose_subscription_appeared_is_refused(session, buy):
    _paid(
        session, had_subscription=False, subscription_id=None, subscription_tariff_id=None, subscription_is_trial=None
    )

    _, stored = await _complete(session)

    assert stored['reason'] == 'subscription_changed'


async def test_purchase_by_another_path_after_the_intent_is_named_before_the_changed_subscription(session, buy):
    payment = _paid(session, subscription_id=40)
    after = datetime.fromisoformat(dfc.topup_intent_of(payment)['created_at']) + timedelta(seconds=1)
    session.add(Transaction(user_id=1, type='subscription_payment', amount_kopeks=1, created_at=after))
    session.commit()

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'already_purchased')
    assert buy.create == []


@pytest.mark.parametrize(
    ('lifecycle_state', 'reason'),
    [('awaiting_funds', 'open_order'), ('fulfilling', 'open_order'), ('operator_review', 'order_on_review')],
)
async def test_open_order_is_refused_and_named_before_anything_is_created(session, buy, env, lifecycle_state, reason):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state=lifecycle_state, public_id='chk-open')
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason'], stored['checkout_public_id']) == ('refused', reason, 'chk-open')
    assert buy.create == []


@pytest.mark.parametrize(
    ('user_id_or_values', 'reason'),
    [
        ({'account_erasure_requested_at': '2026-10-07 10:00:00'}, 'account_erasure'),
        ({'restriction_subscription': True}, 'restricted'),
    ],
)
async def test_account_that_cannot_buy_is_refused(session, buy, user_id_or_values, reason):
    _set_user(session, **user_id_or_values)
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', reason)
    assert buy.create == []


async def test_not_rolled_out_to_this_person_is_refused(session, buy):
    _intent_payment(session, payment_id=97, user_id=2, **TRIAL)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'disabled')


async def test_balance_spent_meanwhile_is_refused(session, buy):
    _paid(session)
    _set_user(session, balance_kopeks=PRICE_30_1 - 1)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'balance_short')


async def test_refused_commit_closes_its_own_order_so_it_locks_nothing(session, buy):
    async def drifted(checkout):
        checkout.lifecycle_state = 'reprice_required'  # так `_validate_direct_pre_commit` коммитит сам
        session.commit()
        raise dfc.DeviceFirstError('reprice_required', 'The quote changed')

    buy.commit_effect = drifted
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'reprice_required')
    own = session.get(SubscriptionCheckout, 501)
    assert (own.lifecycle_state, own.terminal_reason) == ('cancelled', 'topup_intent_refused')
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1


async def test_resumed_foreign_order_is_never_closed_by_the_refusal(session, buy):
    foreign = _add_checkout(session, checkout_id=400, source='cabinet')
    buy.resume = foreign

    async def drifted(checkout):
        checkout.lifecycle_state = 'conflict'
        session.commit()
        raise dfc.DeviceFirstError('reprice_required', 'The quote changed')

    buy.commit_effect = drifted
    _paid(session)

    _, stored = await _complete(session)

    assert stored['status'] == 'refused'
    assert session.get(SubscriptionCheckout, 400).lifecycle_state == 'conflict'


async def test_order_that_won_the_race_is_named_and_not_paid_again(session, buy, monkeypatch):
    # Между сверкой и оформлением другой путь успел создать оплаченный заказ: его состояние — ответ, второго нет.
    winner = _add_checkout(session, checkout_id=402, source='cabinet', lifecycle_state='fulfilling')

    async def create(db_, **kwargs):
        buy.create.append(kwargs)
        return dfc.FusedDirectCheckout(checkout=winner, proceed_to_payment=False)

    monkeypatch.setattr(dfc, 'create_or_resume_direct_checkout', create)
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason'], stored['checkout_public_id']) == ('refused', 'open_order', 'chk-402')
    assert buy.commit == []


async def test_tariff_that_is_no_longer_sold_is_refused_even_if_a_price_is_left(session, buy, env):
    env.options.return_value = {**_options(), 'eligible': False}
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'unavailable')
    assert buy.create == []


async def test_crash_inside_the_purchase_is_a_refusal_never_silence(session, buy):
    async def crash(checkout):
        raise RuntimeError('panel exploded')

    buy.commit_effect = crash
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['reason']) == ('refused', 'technical_error')
    assert session.get(SubscriptionCheckout, 501).lifecycle_state == 'cancelled'


async def test_crash_after_the_money_was_taken_is_reported_as_fulfilled(session, buy):
    # Списано, а потом упало — «оформите сами» толкнуло бы ко второй покупке.
    async def crash_after_debit(checkout):
        checkout.financial_committed_at = datetime.now(UTC)
        checkout.lifecycle_state = 'fulfilling'
        session.commit()
        raise RuntimeError('refresh failed')

    buy.commit_effect = crash_after_debit
    _paid(session)

    _, stored = await _complete(session)

    assert (stored['status'], stored['checkout_public_id']) == ('fulfilled', 'chk-501')


async def test_outcome_already_written_is_returned_without_a_second_purchase(session, buy):
    _paid(session, status='fulfilled', checkout_public_id='chk-old')

    final, stored = await _complete(session)

    assert final['checkout_public_id'] == 'chk-old' == stored['checkout_public_id']
    assert buy.create == []


async def test_payment_without_intent_has_nothing_to_complete(session, buy):
    _paid(session)
    session.execute(text("UPDATE platega_payments SET metadata_json = '{}' WHERE id = 97"))
    session.commit()

    assert await dfc.complete_topup_intent(payment_id=97) is None
    assert buy.create == []


# --- крючок в настоящем зачислении --------------------------------------------------------------------------------


@pytest.fixture
def webhook(monkeypatch, buy):
    order: list[str] = []
    calls = SimpleNamespace(order=order, admin=[], messages=[])
    real_complete = dfc.complete_topup_intent

    async def complete(**kwargs):
        order.append('hook')
        return await real_complete(**kwargs)

    async def emit(*args, **kwargs):
        order.append('side_effects')

    async def cart(*args, **kwargs):
        order.append('cart_chain')

    class _Admin:
        def __init__(self, bot):
            pass

        async def send_balance_topup_notification(self, *args, **kwargs):
            calls.admin.append(kwargs)

    monkeypatch.setattr(dfc, 'complete_topup_intent', complete)
    monkeypatch.setattr('app.database.crud.transaction.emit_transaction_side_effects', emit)
    monkeypatch.setattr('app.services.referral_service.process_referral_topup', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.payment.common.send_cart_notification_after_topup', cart)
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', _Admin)
    erasure = AsyncMock(return_value=False)
    monkeypatch.setattr('app.services.account_erasure_service.mark_late_legacy_payment_for_manual_review', erasure)
    carts = SimpleNamespace(delete_user_cart=AsyncMock(), clear_topup_intent=AsyncMock())
    monkeypatch.setattr('app.services.user_cart_service.user_cart_service', carts)
    monkeypatch.setattr(
        device_first_payment_service,
        'settle_device_first_platega_payment',
        AsyncMock(side_effect=AssertionError('намерение не прямая продажа')),
    )

    bot = SimpleNamespace(send_message=AsyncMock(), delete_message=AsyncMock())

    class _Service(PlategaPaymentMixin):
        def __init__(self):
            self.bot = bot

        async def build_topup_success_keyboard(self, user):
            return None

    calls.service, calls.bot, calls.erasure, calls.carts = _Service(), bot, erasure, carts
    return calls


async def _pay(db, webhook, payment_id: int = 97):
    return await webhook.service.process_platega_webhook(
        db, {'id': f'tx-{payment_id}', 'status': 'CONFIRMED', 'payload': f'platega:corr-{payment_id}'}
    )


async def test_webhook_credits_then_completes_then_runs_side_effects_and_skips_the_old_chain(db, session, webhook):
    _intent_payment(session, payment_id=97, **TRIAL)

    assert await _pay(db, webhook) is True

    assert webhook.order == ['hook', 'side_effects']  # ловушка 11: крючок до побочных эффектов; цепочки корзины нет
    session.expire_all()
    stored = session.get(PlategaPayment, 97)
    # Финальная запись вебхука переписывает метаданные целиком — исход обязан пережить её (план ВК 16а-2 (г)).
    assert dfc.topup_intent_outcome(stored) == ('fulfilled', 'chk-501', None)
    assert stored.metadata_json['balance_credited'] is True
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1 - PRICE_30_1
    webhook.bot.send_message.assert_not_awaited()  # «Пополнение успешно… сама не оплатится» оформленному — ложь
    assert webhook.admin[0]['auto_next_step'] == 'заказ chk-501 оформлен сам с баланса'
    webhook.erasure.assert_awaited_once()
    webhook.carts.delete_user_cart.assert_awaited_once_with(1)
    webhook.carts.clear_topup_intent.assert_awaited_once_with(1)


async def test_deposit_is_committed_before_the_hook_opens_its_own_session(db, session, webhook, monkeypatch):
    seen = {}
    real = dfc._topup_intent_session

    def watching_session():
        # В момент открытия своей сессии деньги уже должны лежать в базе, а не в памяти сессии вебхука.
        seen['balance'] = session.execute(text('SELECT balance_kopeks FROM users WHERE id = 1')).scalar_one()
        seen['committed'] = not session.dirty and not session.new
        return real()

    monkeypatch.setattr(dfc, '_topup_intent_session', watching_session)
    _intent_payment(session, payment_id=97, **TRIAL)

    await _pay(db, webhook)

    assert seen == {'balance': BALANCE + TOP_UP_30_1, 'committed': True}


async def test_refused_intent_still_tells_about_the_money_but_not_through_the_old_chain(db, session, webhook):
    _intent_payment(session, payment_id=97, **{**TRIAL, 'subscription_id': 40})

    await _pay(db, webhook)

    session.expire_all()
    assert dfc.topup_intent_outcome(session.get(PlategaPayment, 97)) == ('refused', None, 'subscription_changed')
    webhook.bot.send_message.assert_awaited_once()
    assert 'Пополнение успешно' in webhook.bot.send_message.await_args.args[1]
    assert 'cart_chain' not in webhook.order
    assert webhook.admin[0]['auto_next_step'] is None
    webhook.erasure.assert_awaited_once()


async def test_outcome_write_failure_still_keeps_the_old_chain_away(db, session, webhook, monkeypatch):
    async def nothing(**kwargs):
        return None

    monkeypatch.setattr(dfc, 'complete_topup_intent', nothing)
    _intent_payment(session, payment_id=97, **TRIAL)

    await _pay(db, webhook)

    assert 'cart_chain' not in webhook.order  # без исхода неизвестно, купили ли, — автопокупка корзины не нужна
    webhook.bot.send_message.assert_awaited_once()


async def test_repeated_webhook_completes_the_order_once(db, session, webhook):
    _intent_payment(session, payment_id=97, **TRIAL)

    await _pay(db, webhook)
    await _pay(db, webhook)

    assert webhook.order.count('hook') == 1
    assert len(webhook.service.bot.send_message.await_args_list) == 0


async def test_top_up_without_intent_runs_the_old_chain_and_no_hook(db, session, webhook):
    _intent_payment(session, payment_id=97, **TRIAL)
    session.execute(text("UPDATE platega_payments SET metadata_json = '{}' WHERE id = 97"))
    session.commit()

    await _pay(db, webhook)

    assert webhook.order == ['side_effects', 'cart_chain']
    webhook.erasure.assert_not_awaited()

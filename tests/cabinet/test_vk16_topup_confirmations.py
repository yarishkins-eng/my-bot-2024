"""ВК-16, часть 16а-2, заявка 3а (08.10.2026): подтверждения и сторожа.

Решение владельца 05.10.2026 «Оформляется само»; план ВК, 16а-2 (а), (б), (к), мины OP, OR, OV, OW. Стенд — настоящий
движок SQLite из сторожей 16а-1 и заявок 1–2; подменены только сами функции оформления и сеть. Цены нарочно не круглые.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.database.models import PlategaPayment, SubscriptionCheckout, Transaction, User
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
    BALANCE,
    PRICE_30_1,
    TOP_UP_30_1,
    _decide,
    _intent_payment,
    _options,
    _purchase,
    _request,
    _user,
    db,
    env,
    provider,
    session,
)


END = '2026-11-07T12:34:56+00:00'


@pytest.fixture
def ends(env):
    options = _options()
    options['current_subscription'] = {**options['current_subscription'], 'end_date': END}
    env.options.return_value = options
    return env


# --- (а), мина OP: «уже оформлено» — вопрос по ЛЮБОЙ покупке, а не запрет по строке намерения ----------------------


async def test_purchase_by_card_within_an_hour_is_asked_about_and_no_invoice_is_issued(db, session, ends):
    # Покупка мимо доплаты (карта, касса): строки намерения нет — прежний код выставил бы счёт второго срока молча.
    _purchase(session, minutes_ago=20)

    decision = await _decide(db, session)

    assert (decision.status, decision.checkout_public_id, decision.subscription_end_date) == (
        'already_fulfilled',
        None,
        END,
    )
    assert decision.intent is None and decision.amount_kopeks == 0


async def test_purchase_through_the_new_cashier_names_its_own_order_and_the_end_date(db, session, ends):
    _purchase(session, minutes_ago=12, checkout_public_id='chk-card-7', period_days=90, devices=2)

    decision = await _decide(db, session, period_days=30, devices=1)

    assert (decision.status, decision.checkout_public_id) == ('already_fulfilled', 'chk-card-7')
    assert (decision.period_days, decision.devices, decision.subscription_end_date) == (90, 2, END)


async def test_newest_purchase_within_the_hour_is_the_one_named(db, session, ends):
    _purchase(session, minutes_ago=40, checkout_public_id='chk-old-1', period_days=90)
    _purchase(session, minutes_ago=5, checkout_public_id='chk-new-2', period_days=180)

    decision = await _decide(db, session)

    assert (decision.checkout_public_id, decision.period_days) == ('chk-new-2', 180)


async def test_order_with_money_but_without_a_purchase_record_still_asks(db, session, ends):
    # Заказ новой кассы со списанием — покупка сам по себе, даже если проводки покупки рядом нет.
    _purchase(session, minutes_ago=8, checkout_public_id='chk-direct-4', transaction=False)

    assert (await _decide(db, session)).checkout_public_id == 'chk-direct-4'


async def test_top_up_or_bonus_within_the_hour_is_not_a_purchase(db, session, ends):
    # Пополнение баланса и бонус — не покупка: иначе любая доплата спрашивала бы «уже оформлено» у того, кто только
    # что пополнил баланс.
    at = datetime.now(UTC) - timedelta(minutes=5)
    session.add(Transaction(user_id=1, type='deposit', amount_kopeks=TOP_UP_30_1, created_at=at))
    session.add(Transaction(user_id=1, type='referral_reward', amount_kopeks=5_000, created_at=at))
    session.commit()

    assert (await _decide(db, session)).status == 'accepted'


async def test_invoice_issued_between_two_purchases_is_not_reused_for_once_more(db, session, ends):
    # Считаем от ПОСЛЕДНЕЙ покупки: счёт между ними при зачислении откажет «уже была оплата».
    _purchase(session, minutes_ago=40, checkout_public_id='chk-first-1', transaction=False)
    _intent_payment(session, payment_id=73, created_ago=timedelta(minutes=20))
    _purchase(session, minutes_ago=10)

    assert (await _decide(db, session, repeat=True)).status == 'accepted'


async def test_yes_once_more_bills_another_period(db, session, ends):
    _purchase(session, minutes_ago=20, checkout_public_id='chk-card-7')

    decision = await _decide(db, session, repeat=True)

    assert (decision.status, decision.amount_kopeks, decision.price_kopeks) == ('accepted', TOP_UP_30_1, PRICE_30_1)


@pytest.mark.parametrize('minutes_ago', [61, 90])
async def test_purchase_older_than_an_hour_does_not_ask(db, session, ends, minutes_ago):
    _purchase(session, minutes_ago=minutes_ago, checkout_public_id='chk-old-3')

    assert (await _decide(db, session)).status == 'accepted'


async def test_someone_elses_purchase_does_not_ask(db, session, ends):
    _purchase(session, minutes_ago=5, checkout_public_id='chk-other-1', user_id=2)

    assert (await _decide(db, session)).status == 'accepted'


async def test_yes_once_more_does_not_reuse_an_invoice_issued_before_the_purchase(db, session, ends):
    # Такой счёт при зачислении откажет «уже была оплата» — «ещё период» им не оплатить, нужен новый.
    _intent_payment(session, payment_id=70, created_ago=timedelta(minutes=30))
    _purchase(session, minutes_ago=20)

    assert (await _decide(db, session, repeat=True)).status == 'accepted'


async def test_yes_once_more_reuses_an_invoice_issued_after_the_purchase(db, session, ends):
    _purchase(session, minutes_ago=20)
    live = _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=5))

    decision = await _decide(db, session, repeat=True)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)


async def test_money_already_on_its_way_is_named_before_the_question(db, session, ends):
    # Деньги по доплате пришли и оформляются — второй счёт поверх был бы вторыми деньгами при любом ответе.
    _intent_payment(session, payment_id=72, provider_status='CONFIRMED', is_paid=True, transaction_id=990)
    _purchase(session, minutes_ago=3)

    assert (await _decide(db, session, repeat=True)).status == 'already_paid'


async def test_route_carries_the_question_and_the_yes(db, session, provider, ends):
    _purchase(session, minutes_ago=15, checkout_public_id='chk-card-7')

    asked = await balance_route_topup(db, session)
    billed = await balance_route_topup(db, session, repeat=True)

    assert (asked.intent_status, asked.payment_url, asked.checkout_public_id) == (
        'already_fulfilled',
        None,
        'chk-card-7',
    )
    assert asked.subscription_end_date == datetime(2026, 11, 7, 12, 34, 56, tzinfo=UTC)
    assert provider.calls and len(provider.calls) == 1  # счёт выставлен только на «да»
    assert (billed.intent_status, billed.amount_kopeks) == ('accepted', TOP_UP_30_1)


async def balance_route_topup(db, session, **kw):
    from app.cabinet.routes import balance as balance_route

    return await balance_route.create_topup(request=_request(**kw), user=_user(session), db=db)


# --- (б), мина OR: тот же счёт любым способом, новый — только явной сменой способа ------------------------------------


async def test_reopening_without_a_method_gets_the_live_invoice_of_another_method(db, session, env):
    # Кнопка бота способа не несёт — сервер берёт первый активный (11 в этом стенде нет: берём 11 явно).
    live = _intent_payment(session, payment_id=80, method=2)

    decision = await _decide(db, session, method=11)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)
    assert session.get(PlategaPayment, 80).metadata_json[dfc.TOPUP_INTENT_KEY]['status'] == 'pending'


async def test_explicit_method_change_still_reuses_a_live_invoice_of_that_same_method(db, session, env):
    _intent_payment(session, payment_id=81, method=2)
    same = _intent_payment(session, payment_id=82, method=11, created_ago=timedelta(minutes=2))

    decision = await _decide(db, session, method=11, change_method=True)

    assert (decision.status, decision.payment.id) == ('already_paying', same.id)


async def test_route_reopening_names_the_method_of_the_invoice_it_returns(db, session, provider):
    first = await balance_route_topup(db, session, option='2')
    again = await balance_route_topup(db, session, option='11')

    assert (again.intent_status, again.payment_id, again.payment_option) == ('already_paying', first.payment_id, '2')
    assert again.payment_url == first.payment_url and len(provider.calls) == 1
    old = dfc.topup_intent_of(session.get(PlategaPayment, int(first.payment_id)))
    assert old['status'] == 'pending'  # не заменён — его ещё можно оплатить


async def test_route_new_invoice_has_no_method_hint(db, session, provider):
    response = await balance_route_topup(db, session)

    assert response.intent_status == 'accepted' and response.payment_option is None


# --- (к): исход не перезаписывается, оформление одно на любой набор входов --------------------------------------


@pytest.mark.parametrize(
    ('stored', 'attempt'),
    [
        ({'status': 'fulfilled', 'checkout_public_id': 'chk-9'}, {'status': 'refused', 'reason': 'already_purchased'}),
        ({'status': 'refused', 'reason': 'price_changed'}, {'status': 'fulfilled', 'checkout_public_id': 'chk-9'}),
    ],
)
async def test_decided_outcome_is_not_overwritten(db, session, stored, attempt):
    decided = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    _intent_payment(session, payment_id=97, is_paid=True, decided_at=decided, **{**TRIAL, **stored})

    final = await dfc._record_topup_intent_outcome(db, payment_id=97, **attempt)

    session.expire_all()
    kept = dfc.topup_intent_of(session.get(PlategaPayment, 97))
    assert final == kept
    assert (kept['status'], kept['decided_at']) == (stored['status'], decided)


@pytest.mark.parametrize('label', ['pending', 'cancelled', 'replaced'])
async def test_outcome_is_written_over_an_undecided_label(db, session, label):
    _intent_payment(session, payment_id=97, is_paid=True, status=label)

    final = await dfc._record_topup_intent_outcome(db, payment_id=97, status='refused', reason=label)

    assert (final['status'], final['reason']) == ('refused', label) and final['decided_at']


def _status_check(webhook):
    webhook.service.platega_service = SimpleNamespace(
        get_transaction=AsyncMock(return_value={'status': 'CONFIRMED', 'id': 'tx-97'})
    )

    async def check(db_):
        return await webhook.service.get_platega_payment_status(db_, 97)

    return check


@pytest.mark.parametrize(
    'entries',
    [
        ('webhook', 'status'),
        ('status', 'webhook'),
        ('status', 'status'),
        ('webhook', 'webhook', 'status'),
    ],
)
async def test_every_entry_completes_the_order_once(db, session, webhook, buy, entries):
    # Все пять входов проверки (автопроверка, «Проверить» клиента и админа, две кнопки чат-админки) идут через
    # `get_platega_payment_status`; вебхук — через `process_platega_webhook`. Оформление — одно, сообщение — одно.
    _intent_payment(session, payment_id=97, **TRIAL)
    check = _status_check(webhook)

    for entry in entries:
        await (_pay(db, webhook) if entry == 'webhook' else check(db))

    assert webhook.order.count('hook') == 1 and len(buy.commit) == 1
    assert len(webhook.service.bot.send_message.await_args_list) == 1
    session.expire_all()
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1 - PRICE_30_1
    stored = dfc.topup_intent_of(session.get(PlategaPayment, 97))
    assert stored['status'] == 'fulfilled'


# --- мина OV: ответ кнопок отмены — только про свою доплату -------------------------------------------------------


async def test_cancel_with_only_an_unpaid_top_up_says_no_money(db, session):
    _intent_payment(session, payment_id=91)

    assert await dfc.cancel_topup_intents(db, user_id=1) is None


async def test_cancel_charged_just_now_is_still_named_fulfilled(db, session):
    # Своя доплата, застанная в работе, со списанием после выбора — «отменить уже нельзя» остаётся правдой.
    payment = _intent_payment(session, payment_id=91, is_paid=True)
    after = datetime.fromisoformat(dfc.topup_intent_of(payment)['created_at']) + timedelta(seconds=1)
    session.add(Transaction(user_id=1, type='subscription_payment', amount_kopeks=PRICE_30_1, created_at=after))
    session.commit()
    decided = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _intent_payment(session, payment_id=92, status='refused', is_paid=True, decided_at=decided)

    assert await dfc.cancel_topup_intents(db, user_id=1) == 'fulfilled'


async def test_someone_elses_decided_top_up_changes_nothing(db, session):
    decided = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _intent_payment(session, payment_id=91, user_id=2, status='fulfilled', is_paid=True, decided_at=decided)

    assert await dfc.cancel_topup_intents(db, user_id=1) is None


def test_earlier_words_neither_promise_a_message_nor_deny_the_money():
    user = SimpleNamespace(language='ru')

    caption = handlers._cancelled_text(user, 'earlier')

    assert caption == 'Заказ отменён. О доплате, которая пришла раньше, бот уже написал отдельным сообщением.'
    english = handlers._cancelled_text(SimpleNamespace(language='en'), 'earlier')
    assert 'no money' not in english.lower() and 'will follow' not in english


async def test_cancel_fused_after_an_earlier_top_up_uses_the_earlier_words():
    user = SimpleNamespace(id=17, language='ru')
    with (
        patch.object(handlers, 'cancel_topup_intents', AsyncMock(return_value='earlier')),
        patch.object(handlers, '_has_order_in_flight', AsyncMock(return_value=False)),
        patch.object(handlers, 'edit_or_answer_photo', AsyncMock()) as render,
    ):
        await handlers.cancel_fused(
            SimpleNamespace(data='df:x2', answer=AsyncMock()), user, AsyncMock(), SimpleNamespace(clear=AsyncMock())
        )

    caption = render.await_args.kwargs['caption']
    assert 'бот уже написал отдельным сообщением' in caption
    assert 'Деньги не списаны' not in caption and 'Отменить уже нельзя' not in caption


# --- мина OW: списание с баланса читает баланс под замком ---------------------------------------------------------


async def test_wallet_debit_lock_rereads_a_balance_credited_meanwhile(db, session, checkouts):
    user = session.get(User, 1)  # объект уже в карте сессии — как у маршрута и кнопки бота
    session.execute(text('UPDATE users SET balance_kopeks = balance_kopeks + 9900 WHERE id = 1'))
    session.commit()  # зачисление «другой транзакцией»: карта объектов его не видит
    user.balance_kopeks = BALANCE  # снимок, прочитанный до замка

    with pytest.raises(dfc.DeviceFirstError):  # заказа нет — дальше замка не идём, но баланс уже перечитан
        await dfc._lock_direct_context(db, public_id='chk-none', user_id=1)

    assert user.balance_kopeks == BALANCE + 9_900


async def test_wallet_debit_lock_on_a_real_async_session_keeps_the_promo_group(tmp_path, monkeypatch):
    # Урок заявки 2: освежение пользователя целиком сбрасывало связи (MissingGreenlet в цене). Здесь — только баланс.
    import importlib
    import sys

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database.crud.user import get_user_by_id
    from app.database.models import (
        PromoGroup,
        ServerSquad,
        Subscription,
        Tariff,
        UserPromoGroup,
        server_squad_promo_groups,
    )

    stub = sys.modules.pop('aiosqlite', None)
    try:
        real_driver = importlib.import_module('aiosqlite')
    finally:
        if stub is not None:
            sys.modules['aiosqlite'] = stub
    monkeypatch.setitem(sys.modules, 'aiosqlite', real_driver)

    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "lock.db"}')
    async with engine.begin() as connection:
        models = (User, SubscriptionCheckout, Subscription, Tariff, PromoGroup, UserPromoGroup, ServerSquad)
        for table in [model.__table__ for model in models] + [server_squad_promo_groups]:
            columns = ', '.join(
                'id INTEGER PRIMARY KEY' if column.name == 'id' else column.name for column in table.columns
            )
            await connection.execute(text(f'CREATE TABLE {table.name} ({columns})'))
        await connection.execute(text("INSERT INTO promo_groups (id, name, priority) VALUES (7, 'Базовая', 0)"))
        await connection.execute(
            text(
                'INSERT INTO users (id, telegram_id, balance_kopeks, status, language, promo_group_id) '
                f"VALUES (1, 777001, {BALANCE}, 'active', 'ru', 7)"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as db_:
        user = await get_user_by_id(db_, 1)
        async with factory() as other:
            await other.execute(text('UPDATE users SET balance_kopeks = balance_kopeks + 9900 WHERE id = 1'))
            await other.commit()

        with pytest.raises(dfc.DeviceFirstError):
            await dfc._lock_direct_context(db_, public_id='chk-none', user_id=1)

        assert user.balance_kopeks == BALANCE + 9_900
        assert user.get_primary_promo_group().id == 7
    await engine.dispose()

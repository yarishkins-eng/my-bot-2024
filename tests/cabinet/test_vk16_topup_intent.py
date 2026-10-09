"""ВК-16, часть 16а-1 (07.10.2026): пополнение помнит заказ, сервер говорит исход.

Решение владельца 05.10.2026 «Оформляется само» разворачивает решение 02.09 «одно нажатие»: доплата под заказ
оформит его без нажатия (это 16а-2). Здесь сторожим память о заказе в счёте и честный ответ ДО счёта.

Выборки намерений, замена старых и фильтр `/latest` исполняет НАСТОЯЩИЙ движок SQLite (таблицы из моделей без
типов Postgres — образец `test_recent_payments_live_only.py`); счёт создаёт настоящий код Platega, подменён только
сетевой вызов провайдера; зачисление — настоящий `_finalize_platega_payment`. Цена и суммы в фикстурах нарочно не
круглые: 50,37 ₽ на балансе, клиент просит 123,45 ₽. Счета по умолчанию без срока от провайдера — как на боевом
(мина V: у СБП-счетов `expiresIn` пуст, Platega закрывает их сама за 30–41 минуту).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.cabinet.routes import balance as balance_route, device_first as device_first_route
from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpIntent, TopUpRequest
from app.config import settings
from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceAddonTopupAttempt,
    PlategaPayment,
    PromoGroup,
    Subscription,
    SubscriptionCheckout,
    Tariff,
    Transaction,
    User,
    UserPromoGroup,
)
from app.services import device_first_checkout_service as dfc, device_first_payment_service
from app.services.payment.platega import PlategaPaymentMixin


STAND_TG = 777_001
CLIENT_TG = 777_002
BALANCE = 5_037  # 50,37 ₽
PRICE_30_1 = 14_900
TOP_UP_30_1 = 9_900  # недостача 98,63 ₽ → вверх до целого рубля
CLIENT_AMOUNT = 12_345  # что прислал экран — при намерении не используется


class _AsyncOverSync:
    """Асинхронная обёртка над настоящей сессией SQLite: запросы исполняет движок, а не заглушка."""

    def __init__(self, session: Session) -> None:
        self.sync = session

    async def execute(self, statement, *args, **kwargs):
        return self.sync.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        return self.sync.scalar(statement, *args, **kwargs)

    async def get(self, entity, ident, **kwargs):
        return self.sync.get(entity, ident, **kwargs)

    async def refresh(self, instance, attribute_names=None):
        self.sync.refresh(instance, attribute_names=attribute_names)

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    async def flush(self) -> None:
        self.sync.flush()

    def add(self, instance) -> None:
        self.sync.add(instance)


def _new_session() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        for table in (
            User.__table__,
            PlategaPayment.__table__,
            CheckoutPaymentAttempt.__table__,
            DeviceAddonTopupAttempt.__table__,
            Transaction.__table__,
            Subscription.__table__,
            SubscriptionCheckout.__table__,
            Tariff.__table__,
            PromoGroup.__table__,
            UserPromoGroup.__table__,
        ):
            # Только имена колонок: полная схема тянет типы и внешние ключи Postgres. Номер строки — сам движок.
            columns = ', '.join(
                'id INTEGER PRIMARY KEY' if column.name == 'id' else column.name for column in table.columns
            )
            connection.execute(text(f'CREATE TABLE {table.name} ({columns})'))
        connection.execute(
            text(
                'INSERT INTO users (id, telegram_id, balance_kopeks, status, language) VALUES '
                f"(1, {STAND_TG}, {BALANCE}, 'active', 'ru'), (2, {CLIENT_TG}, {BALANCE}, 'active', 'ru')"
            )
        )
    return Session(engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
def session() -> Session:
    return _new_session()


@pytest.fixture
def db(session) -> _AsyncOverSync:
    return _AsyncOverSync(session)


def _options(*, eligible: bool = True) -> dict:
    if not eligible:
        return {'eligible': False, 'reason': 'eligible_tariff_count_not_one'}
    return {
        'eligible': True,
        'tariff': {'id': 3, 'name': 'Базовый'},
        'current_subscription': {'id': 41, 'tariff_id': 5, 'is_trial': True, 'status': 'active'},
        'balance_kopeks': BALANCE,
        'price_matrix': [
            {
                'period_days': 30,
                'prices': [
                    {'device_limit': 1, 'price_kopeks': PRICE_30_1},
                    {'device_limit': 2, 'price_kopeks': 19_900},
                ],
            },
            {'period_days': 90, 'prices': [{'device_limit': 1, 'price_kopeks': 39_900}]},
        ],
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TG))
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 100_000_000, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', 'test-merchant', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', 'test-secret', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_ACTIVE_METHODS', '2,11', raising=False)
    options = AsyncMock(return_value=_options())
    open_checkout = AsyncMock(return_value=None)
    monkeypatch.setattr(dfc, 'build_purchase_options', options)
    monkeypatch.setattr(dfc, 'get_open_checkout_for_user', open_checkout)
    return SimpleNamespace(options=options, open_checkout=open_checkout)


def _user(session: Session, user_id: int = 1) -> User:
    return session.get(User, user_id)


def _set_user(session: Session, user_id: int = 1, **values) -> None:
    assignments = ', '.join(f'{name} = :{name}' for name in values)
    session.execute(text(f'UPDATE users SET {assignments} WHERE id = :uid'), {**values, 'uid': user_id})
    session.commit()


def _intent_payment(
    session: Session,
    *,
    payment_id: int,
    user_id: int = 1,
    period_days: int = 30,
    devices: int = 1,
    method: int = 2,
    status: str = 'pending',
    created_ago: timedelta = timedelta(minutes=5),
    provider_status: str = 'PENDING',
    is_paid: bool = False,
    transaction_id: int | None = None,
    expires_in: timedelta | None = None,
    redirect: bool = True,
    amount_kopeks: int = TOP_UP_30_1,
    quote_kopeks: int = PRICE_30_1,
    **intent_extra,
) -> PlategaPayment:
    now = datetime.now(UTC)
    intent = {
        'period_days': period_days,
        'devices': devices,
        'quote_kopeks': quote_kopeks,
        'method': method,
        'created_at': (now - created_ago).isoformat(),
        'status': status,
        **intent_extra,
    }
    payment = PlategaPayment(
        id=payment_id,
        user_id=user_id,
        amount_kopeks=amount_kopeks,
        currency='RUB',
        status=provider_status,
        is_paid=is_paid,
        payment_method_code=method,
        correlation_id=f'corr-{payment_id}',
        platega_transaction_id=f'tx-{payment_id}',
        redirect_url=f'https://pay.test/{payment_id}' if redirect else None,
        payload=f'platega:corr-{payment_id}',
        metadata_json={dfc.TOPUP_INTENT_KEY: intent, 'language': 'ru', 'selected_method': method},
        transaction_id=transaction_id,
        expires_at=(now + expires_in) if expires_in is not None else None,
        created_at=now - created_ago,
    )
    session.add(payment)
    session.commit()
    return payment


async def _decide(db, session, *, user_id: int = 1, period_days: int = 30, devices: int = 1, method: int = 2, **kw):
    return await dfc.prepare_topup_intent(
        db,
        user=_user(session, user_id),
        period_days=period_days,
        devices=devices,
        method_code=method,
        min_kopeks=kw.get('min_kopeks', 100),
        max_kopeks=kw.get('max_kopeks', 100_000_000),
        confirmed_purchase_at=kw.get('confirmed_purchase_at'),
        change_method=kw.get('change_method', False),
    )


def _purchase(
    session: Session,
    *,
    minutes_ago: float,
    checkout_public_id: str | None = None,
    period_days: int = 30,
    devices: int = 1,
    user_id: int = 1,
    transaction: bool = True,
    lifecycle_state: str = 'ready',
    source: str = 'cabinet',
    funding_mode: str = 'wallet',
) -> None:
    """Покупка ЛЮБЫМ путём (заявка 3а, мина OP): проводка покупки подписки и, если назван номер, заказ новой кассы,
    к которому она привязана (`device_first_checkout_id`, как у `direct-sale:`). `transaction=False` с заказом — счёт
    картой, выставленный и брошенный: `financial_committed_at` у него есть, а денег не было."""
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    checkout = None
    if checkout_public_id is not None:
        checkout = SubscriptionCheckout(
            public_id=checkout_public_id,
            user_id=user_id,
            source=source,
            tariff_id=3,
            period_days=period_days,
            selected_device_limit=devices,
            lifecycle_state=lifecycle_state,
            financial_committed_at=at,
            funding_mode=funding_mode,
        )
        session.add(checkout)
        session.flush()
    if transaction:
        session.add(
            Transaction(
                user_id=user_id,
                type='subscription_payment',
                amount_kopeks=PRICE_30_1,
                created_at=at,
                device_first_checkout_id=checkout.id if checkout is not None else None,
            )
        )
    session.commit()


# --- кому включено ----------------------------------------------------------------------------------------------


async def test_stands_mode_gives_ordinary_customer_only_an_ordinary_top_up(db, session, env, monkeypatch):
    # 09.10 перевод на всех: прежнее ожидание сохраняется как проверка режима отката `stands`.
    monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', 'stands')
    decision = await _decide(db, session, user_id=2)

    assert (decision.status, decision.reason) == ('ordinary', 'disabled')
    env.options.assert_not_awaited()
    env.open_checkout.assert_not_awaited()


async def test_who_gets_it_is_a_code_constant_not_an_admin_switch(session, env, monkeypatch):
    # Ответ владельца 07.10.2026 17:10: «не городить переключатели в админке» — флаг автопокупки не влияет.
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False, raising=False)
    stand, client = _user(session, 1), _user(session, 2)
    # 09.10: владелец закрыл живой проход и поручил перевод на всех, прежнее `stands` больше не умолчание.
    assert (dfc.topup_intent_enabled_for(stand), dfc.topup_intent_enabled_for(client)) == (True, True)
    # Всех включает только точное 'all'; 'off' и опечатка — выключают, а не раздают всем.
    for rollout, expected in (
        ('all', (True, True)),
        ('stands', (True, False)),
        ('off', (False, False)),
        ('stand', (False, False)),
    ):
        monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', rollout)
        assert (dfc.topup_intent_enabled_for(stand), dfc.topup_intent_enabled_for(client)) == expected, rollout


@pytest.mark.parametrize(
    ('column', 'value', 'reason'),
    [
        ('account_erasure_requested_at', '2026-10-07 10:00:00', 'account_erasure'),
        ('account_erased_at', '2026-10-07 10:00:00', 'account_erasure'),
        ('restriction_subscription', 1, 'restricted'),
        ('restriction_topup', 1, 'restricted'),
    ],
)
async def test_stand_that_cannot_be_served_gets_ordinary_top_up_and_no_promise(db, session, env, column, value, reason):
    # Запрет подписки `create_checkout` проверяет уже после оплаты — обещать «оформится само» нельзя.
    _set_user(session, **{column: value})

    decision = await _decide(db, session)

    assert (decision.status, decision.reason) == ('ordinary', reason)
    assert dfc.topup_intent_enabled_for(_user(session)) is False
    env.options.assert_not_awaited()


async def test_platega_switched_off_promises_nothing(session, env, monkeypatch):
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', False, raising=False)

    assert dfc.topup_intent_unavailable_reason(_user(session)) == 'disabled'


# --- исходы до счёта ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(('eligible', 'period_days'), [(False, 30), (True, 180)])
async def test_unpriceable_order_gets_ordinary_top_up(db, session, env, eligible, period_days):
    env.options.return_value = _options(eligible=eligible)

    decision = await _decide(db, session, period_days=period_days)

    assert (decision.status, decision.reason) == ('ordinary', 'unavailable')


@pytest.mark.parametrize(
    ('lifecycle_state', 'expected'),
    [
        ('awaiting_funds', 'open_order'),
        ('armed', 'open_order'),
        ('fulfilling', 'open_order'),
        ('operator_review', 'order_on_review'),
    ],
)
async def test_live_order_blocks_a_new_invoice_and_names_its_own_terms(db, session, env, lifecycle_state, expected):
    env.open_checkout.return_value = SimpleNamespace(
        lifecycle_state=lifecycle_state, public_id='chk-live-7', period_days=90, selected_device_limit=2
    )

    decision = await _decide(db, session)

    assert (decision.status, decision.checkout_public_id) == (expected, 'chk-live-7')
    assert (decision.period_days, decision.devices) == (90, 2)


@pytest.mark.parametrize('lifecycle_state', ['draft', 'confirmed'])
async def test_stale_quote_does_not_block(db, session, env, lifecycle_state):
    env.open_checkout.return_value = SimpleNamespace(
        lifecycle_state=lifecycle_state, public_id='chk-quote', period_days=30, selected_device_limit=1
    )

    assert (await _decide(db, session)).status == 'accepted'


async def test_corrupted_order_is_review_not_a_new_invoice(db, session, env):
    env.open_checkout.side_effect = dfc.DeviceFirstError('operator_review_required', 'corrupted')

    assert (await _decide(db, session)).status == 'order_on_review'


async def test_order_fulfilled_within_an_hour_asks_before_a_second_term_and_names_it(db, session, env):
    # Заявка 3а (мина OP): переписано — раньше это был ЗАПРЕТ по строке намерения; теперь вопрос «уже оформлено до …»
    # по самой покупке (заказ со списанием), и срок/устройства — купленного заказа, а не запрошенного.
    decided = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    _intent_payment(
        session,
        payment_id=50,
        status='fulfilled',
        is_paid=True,
        transaction_id=900,
        checkout_public_id='chk-done-5',
        decided_at=decided,
    )
    _purchase(session, minutes_ago=10, checkout_public_id='chk-done-5')

    decision = await _decide(db, session, period_days=90)

    assert (decision.status, decision.checkout_public_id) == ('already_fulfilled', 'chk-done-5')
    assert (decision.period_days, decision.devices) == (30, 1)


async def test_intent_made_70_minutes_ago_and_fulfilled_50_minutes_ago_still_asks(db, session, env):
    # Час считается от покупки, а не от выбора заказа (заявка 3а: от списания, а не от строки намерения).
    _intent_payment(
        session,
        payment_id=52,
        status='fulfilled',
        is_paid=True,
        transaction_id=902,
        created_ago=timedelta(minutes=70),
        checkout_public_id='chk-late',
        decided_at=(datetime.now(UTC) - timedelta(minutes=50)).isoformat(),
    )
    _purchase(session, minutes_ago=50, checkout_public_id='chk-late')

    decision = await _decide(db, session)

    assert (decision.status, decision.checkout_public_id) == ('already_fulfilled', 'chk-late')


async def test_order_fulfilled_long_ago_does_not_block(db, session, env):
    _intent_payment(
        session,
        payment_id=51,
        status='fulfilled',
        is_paid=True,
        transaction_id=901,
        created_ago=timedelta(minutes=80),
        checkout_public_id='chk-old',
        decided_at=(datetime.now(UTC) - timedelta(minutes=70)).isoformat(),
    )

    assert (await _decide(db, session)).status == 'accepted'


async def test_same_order_same_method_live_invoice_is_reused(db, session, env):
    live = _intent_payment(session, payment_id=60)

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)
    assert (decision.amount_kopeks, decision.price_kopeks, decision.period_days) == (TOP_UP_30_1, PRICE_30_1, 30)


async def test_two_live_invoices_of_the_same_order_reuse_the_latest_decision(db, session, env):
    # Замена могла проглотить сбой (`_replace_older_topup_intents_quietly`): оба счёта живы. Отдаём тот, что решён
    # позже, — его человек открыл последним (и когда у него больший номер строки, и когда меньший — гонка выше).
    _intent_payment(session, payment_id=10, created_ago=timedelta(minutes=4))
    _intent_payment(session, payment_id=11, created_ago=timedelta(minutes=1))

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', 11)


async def test_invoice_without_provider_deadline_at_minute_35_is_still_reused(db, session, env):
    # Мина V: СБП-счёт без срока Platega закрывает сама за 30–41 минуту и до того принимает деньги.
    live = _intent_payment(session, payment_id=61, created_ago=timedelta(minutes=35))

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)


async def test_same_order_other_method_is_the_same_invoice_unless_the_method_is_changed_explicitly(db, session, env):
    # Заявка 3а (мина OR): переписано — раньше другой способ давал новый счёт и молча гасил старый, а кнопка бота
    # способа не несёт. Теперь новый счёт другим способом — только явной сменой способа.
    live = _intent_payment(session, payment_id=62, method=2)

    assert ((await _decide(db, session, method=11)).status, live.id) == ('already_paying', 62)
    assert (await _decide(db, session, method=11, change_method=True)).status == 'accepted'


async def test_other_order_same_method_is_a_new_invoice(db, session, env):
    _intent_payment(session, payment_id=63)

    assert (await _decide(db, session, period_days=90)).status == 'accepted'


@pytest.mark.parametrize(
    'overrides',
    [
        {'expires_in': timedelta(minutes=-1)},
        {'provider_status': 'CANCELED'},
        {'created_ago': timedelta(minutes=61)},
        {'status': 'replaced'},
        {'status': 'cancelled'},
        {'user_id': 2},
        {'redirect': False},
        {'amount_kopeks': TOP_UP_30_1 - 100},
        {'quote_kopeks': PRICE_30_1 + 100},
    ],
)
async def test_dead_foreign_or_stale_invoice_is_not_reused(db, session, env, overrides):
    # Без ссылки повтор вёл бы в тупик; со старой суммой или ценой счёт уже не покрывает заказ.
    _intent_payment(session, payment_id=65, **overrides)

    assert (await _decide(db, session)).status == 'accepted'


async def test_balance_that_now_covers_the_price_beats_a_live_invoice(db, session, env):
    _intent_payment(session, payment_id=66)
    _set_user(session, balance_kopeks=PRICE_30_1)

    decision = await _decide(db, session)

    assert (decision.status, decision.price_kopeks, decision.period_days) == ('balance_covers', PRICE_30_1, 30)


@pytest.mark.parametrize(
    'paid',
    [
        {'provider_status': 'CONFIRMED', 'is_paid': True, 'transaction_id': 903},
        # Мина OH: зачислено (`transaction_id`), а поздний «отменён» сироты перевернул `is_paid`.
        {'provider_status': 'CANCELED', 'is_paid': False, 'transaction_id': 904},
    ],
)
async def test_paid_intent_awaiting_outcome_blocks_any_new_invoice_and_names_its_order(db, session, env, paid):
    found = _intent_payment(session, payment_id=67, **paid)

    decision = await _decide(db, session, period_days=90, method=11)

    assert (decision.status, decision.payment.id) == ('already_paid', found.id)
    assert (decision.period_days, decision.devices, decision.price_kopeks) == (30, 1, PRICE_30_1)


async def test_balance_that_covers_the_price_needs_no_invoice(db, session, env):
    _set_user(session, balance_kopeks=PRICE_30_1)

    decision = await _decide(db, session)

    assert (decision.status, decision.price_kopeks) == ('balance_covers', PRICE_30_1)


async def test_accepted_intent_amount_is_the_cashier_formula_and_remembers_the_order(db, session, env):
    decision = await _decide(db, session)

    assert decision.status == 'accepted'
    assert decision.amount_kopeks == TOP_UP_30_1
    assert decision.amount_kopeks == dfc.device_first_top_up_kopeks(price_kopeks=PRICE_30_1, balance_kopeks=BALANCE)
    assert (decision.price_kopeks, decision.period_days, decision.devices) == (PRICE_30_1, 30, 1)
    intent = decision.intent
    assert {key: intent[key] for key in intent if key != 'created_at'} == {
        'period_days': 30,
        'devices': 1,
        'quote_kopeks': PRICE_30_1,
        'tariff_id': 3,
        'method': 2,
        'had_subscription': True,
        'subscription_id': 41,
        'subscription_tariff_id': 5,
        'subscription_is_trial': True,
        'status': 'pending',
    }
    assert abs(datetime.fromisoformat(intent['created_at']) - datetime.now(UTC)) < timedelta(seconds=5)


async def test_newcomer_intent_remembers_there_was_no_subscription(db, session, env):
    env.options.return_value = {**_options(), 'current_subscription': None}

    intent = (await _decide(db, session)).intent

    assert (intent['had_subscription'], intent['subscription_id'], intent['subscription_is_trial']) == (
        False,
        None,
        None,
    )


async def test_amount_outside_the_narrowed_method_range_is_not_substituted(db, session, env):
    decision = await _decide(db, session, min_kopeks=10_000, max_kopeks=100_000)

    assert (decision.status, decision.reason) == ('ordinary', 'unavailable')


# --- замена своих неоплаченных ------------------------------------------------------------------------------


async def test_newer_decision_replaces_own_unpaid_earlier_ones_only(db, session, env):
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70)
    _intent_payment(session, payment_id=71, is_paid=True, provider_status='CONFIRMED', transaction_id=971)
    _intent_payment(session, payment_id=72, status='fulfilled', is_paid=True, transaction_id=972)
    _intent_payment(session, payment_id=73, user_id=2)
    # Мина OH: зачислен, а `is_paid` перевернул поздний «отменён» сироты — не трогать.
    _intent_payment(session, payment_id=76, provider_status='CANCELED', is_paid=False, transaction_id=976)
    # Решение принято ПОЗЖЕ, хотя строка с меньшим номером (запрос дольше ждал Platega) — не трогать.
    _intent_payment(session, payment_id=69, created_ago=timedelta(seconds=10))

    replaced = await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id)

    assert replaced == 1
    statuses = {row.id: dfc.topup_intent_of(row)['status'] for row in session.query(PlategaPayment).all()}
    assert statuses == {
        69: 'pending',
        70: 'replaced',
        71: 'pending',
        72: 'fulfilled',
        73: 'pending',
        74: 'pending',
        76: 'pending',
    }
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['replaced_by'] == 74


async def test_row_paid_between_read_and_lock_is_not_replaced(db, session, env):
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    held = _intent_payment(session, payment_id=70)  # сессия держит строку «неоплаченной»
    # Вебхук оплачивает её в другой транзакции — перечитывание под замком обязано это увидеть.
    session.execute(text('UPDATE platega_payments SET is_paid = 1, transaction_id = 970 WHERE id = 70'))
    session.commit()

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id) == 0
    assert dfc.topup_intent_of(held)['status'] == 'pending'


# --- маршрут /topup целиком ------------------------------------------------------------------------------------


class _FakeProvider:
    is_configured = True

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def create_payment(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            return None
        number = len(self.calls)
        return {'transactionId': f'prov-{number}', 'redirect': f'https://pay.test/new-{number}', 'status': 'PENDING'}


@pytest.fixture
def provider(monkeypatch, env):
    fake = _FakeProvider()

    class _Service(PlategaPaymentMixin):
        def __init__(self) -> None:
            self.platega_service = fake

    async def methods(*_, **__):
        return [
            PaymentMethodResponse(id='platega', name='Platega', min_amount_kopeks=100, max_amount_kopeks=100_000_000),
            PaymentMethodResponse(id='yookassa', name='ЮKassa', min_amount_kopeks=100, max_amount_kopeks=100_000_000),
        ]

    monkeypatch.setattr(balance_route, 'PaymentService', _Service)
    monkeypatch.setattr(balance_route, 'get_payment_methods', methods)
    return fake


def _request(
    *,
    method: str = 'platega',
    option: str | None = '2',
    intent: bool = True,
    period_days: int = 30,
    amount_kopeks: int = CLIENT_AMOUNT,
    confirmed_purchase_at: datetime | None = None,
    change_method: bool = False,
) -> TopUpRequest:
    return TopUpRequest(
        amount_kopeks=amount_kopeks,
        payment_method=method,
        payment_option=option,
        intent=TopUpIntent(
            period_days=period_days,
            devices=1,
            confirmed_purchase_at=confirmed_purchase_at,
            change_method=change_method,
        )
        if intent
        else None,
    )


async def test_route_accepts_intent_and_bills_the_server_amount(db, session, provider):
    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.amount_kopeks, response.price_kopeks) == ('accepted', TOP_UP_30_1, 14_900)
    assert (response.period_days, response.devices) == (30, 1)
    assert provider.calls[0]['amount'] == TOP_UP_30_1 / 100
    stored = session.get(PlategaPayment, int(response.payment_id))
    assert stored.amount_kopeks == TOP_UP_30_1
    assert dfc.topup_intent_of(stored)['status'] == 'pending'
    assert {'raw_response', 'language', 'selected_method'} <= set(stored.metadata_json)
    # Ловушка 5 стартера: ключи device-first увели бы вебхук в ветку прямой продажи без зачисления.
    assert not {'settlement_mode', 'device_first_attempt_id', 'purpose', 'purchase_token'} & set(stored.metadata_json)


async def test_route_with_intent_ignores_a_bogus_client_amount(db, session, provider):
    # Мина ON: сумму считает сервер — заглушка клиента ниже минимума способа не валит запрос до решения.
    response = await balance_route.create_topup(request=_request(amount_kopeks=1), user=_user(session), db=db)

    assert (response.intent_status, response.amount_kopeks) == ('accepted', TOP_UP_30_1)


async def test_route_ordinary_top_up_still_checks_the_client_amount(db, session, provider, monkeypatch):
    # 09.10: обычное пополнение при намерении осталось у клиента в режиме отката `stands`.
    monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', 'stands')
    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(request=_request(amount_kopeks=1), user=_user(session, 2), db=db)

    assert (error.value.status_code, error.value.detail) == (400, 'Minimum amount is 1.00 RUB')
    assert provider.calls == []


async def test_route_switching_method_replaces_the_old_invoice(db, session, provider):
    # Заявка 3а (мина OR): замена — только явной сменой способа (`intent.change_method`).
    first = await balance_route.create_topup(request=_request(option='2'), user=_user(session), db=db)
    second = await balance_route.create_topup(
        request=_request(option='11', change_method=True), user=_user(session), db=db
    )

    assert (first.intent_status, second.intent_status) == ('accepted', 'accepted')
    old = dfc.topup_intent_of(session.get(PlategaPayment, int(first.payment_id)))
    assert (old['status'], old['replaced_by']) == ('replaced', int(second.payment_id))
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(second.payment_id)))['status'] == 'pending'


async def test_route_slow_first_request_does_not_replace_the_invoice_the_client_holds(db, session, provider):
    # Первый POST завис в повторах Platega, кабинет сдался через 30 с, человек нажал ещё раз и открыл ВТОРОЙ счёт.
    # Первый запрос дожимается позже и получает номер строки больше — но решён раньше, поэтому не заменяет второй.
    gate = asyncio.Event()
    original = provider.create_payment
    hung = []

    async def slow_first(**kwargs):
        if not hung:
            hung.append(kwargs)
            await gate.wait()
        return await original(**kwargs)

    provider.create_payment = slow_first
    first = asyncio.create_task(balance_route.create_topup(request=_request(), user=_user(session), db=db))
    await asyncio.sleep(0)
    held = await balance_route.create_topup(request=_request(), user=_user(session), db=db)
    gate.set()
    lost = await first

    assert (held.intent_status, lost.intent_status) == ('accepted', 'accepted')
    assert int(lost.payment_id) > int(held.payment_id)
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(held.payment_id)))['status'] == 'pending'
    # Третье нажатие отдаёт тот счёт, что у клиента (самое позднее решение), а не потерянный с большим номером.
    again = await balance_route.create_topup(request=_request(), user=_user(session), db=db)
    assert (again.intent_status, again.payment_id) == ('already_paying', held.payment_id)


async def test_route_reopening_the_same_order_returns_the_same_invoice(db, session, provider):
    first = await balance_route.create_topup(request=_request(), user=_user(session), db=db)
    again = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (again.intent_status, again.status) == ('already_paying', 'pending')
    assert (again.payment_id, again.payment_url) == (first.payment_id, first.payment_url)
    assert (again.amount_kopeks, again.price_kopeks, again.period_days) == (TOP_UP_30_1, PRICE_30_1, 30)
    assert len(provider.calls) == 1


async def test_route_already_paid_gives_no_link_to_pay_again(db, session, provider):
    paid = _intent_payment(session, payment_id=68, provider_status='CANCELED', is_paid=False, transaction_id=968)

    response = await balance_route.create_topup(request=_request(period_days=90), user=_user(session), db=db)

    assert (response.intent_status, response.payment_id, response.status) == ('already_paid', str(paid.id), 'paid')
    assert (response.payment_url, response.period_days, response.price_kopeks) == (None, 30, PRICE_30_1)
    assert provider.calls == []


async def test_route_outcome_without_invoice_never_calls_the_provider(db, session, provider, env):
    env.open_checkout.return_value = SimpleNamespace(
        lifecycle_state='awaiting_funds', public_id='chk-live-9', period_days=90, selected_device_limit=1
    )

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.checkout_public_id, response.period_days) == (
        'open_order',
        'chk-live-9',
        90,
    )
    assert (response.payment_id, response.payment_url, response.amount_kopeks) == (None, None, 0)
    assert provider.calls == []


async def test_route_disabled_intent_is_an_ordinary_top_up_for_the_client_amount(db, session, provider, monkeypatch):
    # Снятый default `stands` проверяем явно: при откате клиент получает прежнее пополнение.
    monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', 'stands')
    response = await balance_route.create_topup(request=_request(), user=_user(session, 2), db=db)

    assert (response.intent_status, response.intent_reason) == ('ordinary', 'disabled')
    assert response.amount_kopeks == CLIENT_AMOUNT
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(response.payment_id))) is None


async def test_route_without_intent_answers_as_before(db, session, provider):
    response = await balance_route.create_topup(request=_request(intent=False), user=_user(session), db=db)

    assert (response.amount_kopeks, response.status, response.payment_url) == (
        CLIENT_AMOUNT,
        'pending',
        'https://pay.test/new-1',
    )
    new_fields = ('intent_status', 'intent_reason', 'checkout_public_id', 'period_days', 'devices', 'price_kopeks')
    assert {name: getattr(response, name) for name in new_fields} == dict.fromkeys(new_fields)
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(response.payment_id))) is None


async def test_route_invoice_refused_with_intent_is_an_outcome_not_a_500(db, session, provider):
    provider.fail = True

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.payment_url) == ('invoice_not_created', None)


async def test_route_invoice_refused_without_intent_stays_500(db, session, provider):
    provider.fail = True

    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(request=_request(intent=False), user=_user(session), db=db)

    assert error.value.status_code == 500


async def test_route_other_method_with_intent_is_ordinary(db, session, provider, monkeypatch):
    class _YooService:
        async def create_yookassa_payment(self, **_):
            return {'confirmation_url': 'https://yoo.test/pay', 'local_payment_id': 5}

    monkeypatch.setattr(balance_route, 'PaymentService', _YooService)

    response = await balance_route.create_topup(
        request=_request(method='yookassa', option=None), user=_user(session), db=db
    )

    assert (response.intent_status, response.intent_reason) == ('ordinary', 'method_not_supported')
    assert response.amount_kopeks == CLIENT_AMOUNT


# --- исход для экрана: /pending-payments/{id} и /latest --------------------------------------------------------


@pytest.mark.parametrize(
    ('intent_extra', 'expected'),
    [
        ({'status': 'pending'}, ('waiting', None, None)),
        # Зачислено, исхода ещё нет: «оплата получена, оформляем» — даже если поздний «отменён» сироты (мина OH)
        # перевернул `is_paid`: экран не сочтёт счёт мёртвым.
        ({'status': 'pending', 'is_paid': True, 'transaction_id': 980}, ('processing', None, None)),
        (
            {'status': 'pending', 'provider_status': 'CANCELED', 'transaction_id': 980},
            ('processing', None, None),
        ),
        ({'status': 'fulfilled', 'checkout_public_id': 'chk-ok-3'}, ('fulfilled', 'chk-ok-3', None)),
        ({'status': 'refused', 'reason': 'price_changed'}, ('refused', None, 'price_changed')),
        (
            {'status': 'refused', 'reason': 'open_order', 'checkout_public_id': 'chk-open-4'},
            ('refused', 'chk-open-4', 'open_order'),
        ),
        ({'status': 'replaced'}, ('closed', None, 'replaced')),
        ({'status': 'cancelled'}, ('closed', None, 'cancelled')),
    ],
)
async def test_outcome_is_served_by_id_and_by_latest(db, session, monkeypatch, intent_extra, expected):
    _intent_payment(session, payment_id=80, **intent_extra)
    user = _user(session)

    by_id = await balance_route.get_pending_payment_details(method='platega', payment_id=80, user=user, db=db)
    latest = await balance_route.get_latest_payment_by_method(method='platega', user=user, db=db)

    assert (by_id.intent_outcome, by_id.intent_checkout_public_id, by_id.intent_reason) == expected
    assert (latest.id, latest.intent_outcome, latest.intent_checkout_public_id, latest.intent_reason) == (80, *expected)


@pytest.mark.parametrize(
    ('intent_status', 'step_pending'), [('pending', False), ('fulfilled', False), ('refused', True)]
)
async def test_server_does_not_say_buy_it_yourself_over_its_own_auto_purchase(
    db, session, monkeypatch, intent_status, step_pending
):
    monkeypatch.setattr(
        'app.services.payment.common.topup_pending_purchase_hint', AsyncMock(return_value='Оформите подписку')
    )
    _intent_payment(session, payment_id=82, status=intent_status, provider_status='CONFIRMED', is_paid=True)

    response = await balance_route.get_pending_payment_details(
        method='platega', payment_id=82, user=_user(session), db=db
    )

    assert response.purchase_step_pending is step_pending


async def test_payment_without_intent_has_no_outcome(db, session):
    session.add(
        PlategaPayment(
            id=81,
            user_id=1,
            amount_kopeks=10_000,
            currency='RUB',
            status='PENDING',
            is_paid=False,
            payment_method_code=2,
            correlation_id='corr-81',
            metadata_json={'language': 'ru'},
            created_at=datetime.now(UTC),
        )
    )
    session.commit()

    response = await balance_route.get_pending_payment_details(
        method='platega', payment_id=81, user=_user(session), db=db
    )

    assert (response.intent_outcome, response.intent_checkout_public_id, response.intent_reason) == (None, None, None)


async def test_latest_skips_direct_sale_and_device_addon_rows(db, session):
    # Мина OI: строка прямой оплаты заказа (с ВК-15 — и FAILED при сбое счёта) и докупки — не пополнение.
    now = datetime.now(UTC)
    _intent_payment(session, payment_id=90, created_ago=timedelta(minutes=20))
    for payment_id, minutes, metadata in (
        (91, 10, {'settlement_mode': 'direct_purchase_v2', 'device_first_attempt_id': 7}),
        (92, 5, {'settlement_mode': 'device_addon_topup_v1', 'device_addon_attempt_id': 8}),
    ):
        session.add(
            PlategaPayment(
                id=payment_id,
                user_id=1,
                amount_kopeks=14_900,
                currency='RUB',
                status='FAILED',
                is_paid=False,
                payment_method_code=2,
                correlation_id=f'corr-{payment_id}',
                metadata_json=metadata,
                created_at=now - timedelta(minutes=minutes),
            )
        )
    session.commit()
    session.execute(text('INSERT INTO checkout_payment_attempts (id, platega_payment_id) VALUES (7, 91)'))
    session.execute(text('INSERT INTO device_addon_topup_attempts (id, platega_payment_id) VALUES (8, 92)'))
    session.commit()

    latest = await balance_route.get_latest_payment_by_method(method='platega', user=_user(session), db=db)

    assert latest.id == 90


# --- вебхук: намерение зачисляется ОБЩЕЙ веткой пополнения -------------------------------------------------


async def test_intent_payment_is_credited_by_the_real_generic_top_up(db, session, monkeypatch):
    # Настоящий `_finalize_platega_payment`: внутри него вторая развилка device-first и гостевая покупка.
    settle = AsyncMock(side_effect=AssertionError('намерение не прямая продажа'))
    monkeypatch.setattr(device_first_payment_service, 'settle_device_first_platega_payment', settle)
    for target in (
        'app.database.crud.transaction.emit_transaction_side_effects',
        'app.services.referral_service.process_referral_topup',
        'app.services.payment.common.send_cart_notification_after_topup',
    ):
        monkeypatch.setattr(target, AsyncMock(return_value=None))
    # 16а-2 переписал: при зачислении намерение оформляется (его сторожа — `test_vk16_topup_autocomplete.py`); здесь
    # оформление подменено, а проверяется, что деньги пришли ОБЩЕЙ веткой и исход оформления дошёл до записи.
    completed = {'status': 'fulfilled', 'checkout_public_id': 'chk-97'}

    async def complete_and_record(*, payment_id):
        # Как настоящее оформление: исход пишется в строку платежа его сессией (финальная запись вебхука с заявки 2
        # берёт намерение со строки под замком, а не из своего снимка).
        row = session.get(PlategaPayment, payment_id)
        row.metadata_json = {**row.metadata_json, dfc.TOPUP_INTENT_KEY: completed}
        session.commit()
        return completed

    complete = AsyncMock(side_effect=complete_and_record)
    monkeypatch.setattr(dfc, 'complete_topup_intent', complete)
    _intent_payment(session, payment_id=97)

    handled = await PlategaPaymentMixin().process_platega_webhook(
        db, {'id': 'tx-97', 'status': 'CONFIRMED', 'payload': 'platega:corr-97'}
    )

    assert handled is True
    settle.assert_not_awaited()
    assert session.get(User, 1).balance_kopeks == BALANCE + TOP_UP_30_1
    deposit = session.execute(text('SELECT type, amount_kopeks, payment_method FROM transactions')).all()
    assert [tuple(row) for row in deposit] == [('deposit', TOP_UP_30_1, 'platega')]
    stored = session.get(PlategaPayment, 97)
    assert (stored.is_paid, stored.transaction_id is not None) == (True, True)
    complete.assert_awaited_once_with(payment_id=97)
    assert dfc.topup_intent_of(stored) == completed


async def test_late_orphan_cancel_keeps_the_intent_outcome(db, session):
    # Мина OH: повторный POST мог завести счёт-сироту с тем же `payload`; его поздний «отменён» находит строку по
    # `correlation_id` и пишет `is_paid=false`. Исход заказа живёт в намерении и переживает это.
    _intent_payment(
        session,
        payment_id=96,
        status='fulfilled',
        checkout_public_id='chk-ok-96',
        provider_status='CONFIRMED',
        is_paid=True,
        transaction_id=996,
    )

    await PlategaPaymentMixin().process_platega_webhook(
        db, {'id': 'orphan-invoice', 'status': 'CANCELED', 'payload': 'platega:corr-96'}
    )

    stored = session.get(PlategaPayment, 96)
    assert stored.is_paid is False
    assert dfc.topup_intent_outcome(stored) == ('fulfilled', 'chk-ok-96', None)


# --- создание счёта и признак для экранов ------------------------------------------------------------------


async def test_own_metadata_cannot_override_the_base_keys(db, session, env):
    fake = _FakeProvider()

    class _Service(PlategaPaymentMixin):
        platega_service = fake

    result = await _Service().create_platega_payment(
        db,
        user_id=1,
        amount_kopeks=TOP_UP_30_1,
        description='Пополнение',
        language='ru',
        payment_method_code=2,
        extra_metadata={dfc.TOPUP_INTENT_KEY: {'status': 'pending'}, 'language': 'xx', 'selected_method': 99},
    )

    stored = session.get(PlategaPayment, result['local_payment_id'])
    assert (stored.metadata_json['language'], stored.metadata_json['selected_method']) == ('ru', 2)
    assert stored.metadata_json[dfc.TOPUP_INTENT_KEY] == {'status': 'pending'}


@pytest.mark.parametrize(('user_id', 'enabled'), [(1, True), (2, True)])
async def test_purchase_options_tell_the_screens_whom_it_is_enabled_for(
    session, db, env, monkeypatch, user_id, enabled
):
    monkeypatch.setattr(device_first_route, 'build_purchase_options', AsyncMock(return_value=_options()))

    result = await device_first_route.purchase_options(user=_user(session, user_id), db=db)

    assert result['topup_intent_enabled'] is enabled


# --- сторожа скептика волны 2: края замены, порядок проверок, признак экранам ---------------------------------


async def test_paid_intent_beats_balance_that_now_covers_the_price(db, session, env):
    # После зачисления баланс ВСЕГДА покрывает цену (доплата округлена вверх): при обратном порядке «оплата получена»
    # была бы недостижима, и человек увидел бы «оплатите с баланса» поверх идущего автооформления.
    _intent_payment(session, payment_id=10, provider_status='CONFIRMED', is_paid=True, transaction_id=910)
    _set_user(session, balance_kopeks=BALANCE + TOP_UP_30_1)

    assert (await _decide(db, session)).status == 'already_paid'


async def test_fulfilled_intent_beats_balance_that_covers_the_price(db, session, env):
    _intent_payment(
        session,
        payment_id=10,
        status='fulfilled',
        is_paid=True,
        transaction_id=910,
        checkout_public_id='chk-1',
        decided_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )
    _purchase(session, minutes_ago=5, checkout_public_id='chk-1')
    _set_user(session, balance_kopeks=PRICE_30_1 + 100)

    assert (await _decide(db, session)).status == 'already_fulfilled'


def _rewrite_intent(session: Session, payment: PlategaPayment, **changes) -> None:
    intent = {**payment.metadata_json[dfc.TOPUP_INTENT_KEY], **changes}
    payment.metadata_json = {
        **payment.metadata_json,
        dfc.TOPUP_INTENT_KEY: {key: value for key, value in intent.items() if value is not None},
    }
    session.commit()


async def test_equal_decision_time_replaces_nobody(db, session, env):
    stamp = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    for payment_id in (10, 11):
        _rewrite_intent(session, _intent_payment(session, payment_id=payment_id), created_at=stamp)

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=11) == 0
    assert [dfc.topup_intent_of(session.get(PlategaPayment, pid))['status'] for pid in (10, 11)] == ['pending'] * 2


async def test_newer_without_decision_time_replaces_nobody(db, session, env):
    _intent_payment(session, payment_id=10)
    _rewrite_intent(session, _intent_payment(session, payment_id=11), created_at=None)

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=11) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 10))['status'] == 'pending'


async def test_older_without_decision_time_is_left_alone(db, session, env):
    _rewrite_intent(session, _intent_payment(session, payment_id=10), created_at=None)
    _intent_payment(session, payment_id=11, created_ago=timedelta(minutes=1))

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=11) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 10))['status'] == 'pending'


async def test_purchase_options_flag_follows_the_shared_rule_not_just_the_stand_list(session, db, env, monkeypatch):
    monkeypatch.setattr(device_first_route, 'build_purchase_options', AsyncMock(return_value=_options()))
    _set_user(session, restriction_subscription=1)

    result = await device_first_route.purchase_options(user=_user(session, 1), db=db)

    assert result['topup_intent_enabled'] is False


async def test_other_method_with_intent_still_checks_the_client_amount(db, session, provider, monkeypatch):
    class _YooService:
        async def create_yookassa_payment(self, **_):
            return {'confirmation_url': 'https://yoo.test/pay', 'local_payment_id': 5}

    monkeypatch.setattr(balance_route, 'PaymentService', _YooService)

    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(
            request=_request(method='yookassa', option=None, amount_kopeks=1), user=_user(session), db=db
        )

    assert error.value.status_code == 400


# =====================================================================================================================
# Сторожа мутационного прогона ВК-16, 16а-1 (07.10.2026).
# Дописываются В КОНЕЦ `tests/cabinet/test_vk16_topup_intent.py`: используют его импорты, фикстуры (`db`, `session`, `env`,
# `provider`) и помощники (`_intent_payment`, `_decide`, `_request`, `_user`, `_set_user`, `_options`, константы).
# В скобках после имени — поломка, которую сторож ловит (идентификатор мутации из отчёта). Каждый сторож проверен: на
# исходном коде проходит, на своей мутации падает.
# =====================================================================================================================


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _bare_payment(session, *, payment_id: int, metadata) -> None:
    session.add(
        PlategaPayment(
            id=payment_id,
            user_id=1,
            amount_kopeks=10_000,
            currency='RUB',
            status='PENDING',
            is_paid=False,
            payment_method_code=2,
            correlation_id=f'corr-{payment_id}',
            metadata_json=metadata,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()


# --- кому включено и в каком порядке названа причина -------------------------------------------------------------


async def test_erasure_is_named_before_restriction_when_both_apply(session, env):
    # (S09, мелочь) порядок проверок в `topup_intent_unavailable_reason` — контракт: экран по причине решает, что показать.
    _set_user(session, account_erasure_requested_at='2026-10-07 10:00:00', restriction_topup=1)

    assert dfc.topup_intent_unavailable_reason(_user(session)) == 'account_erasure'


# --- память о заказе в счёте: ключ, время, чужие и испорченные строки --------------------------------------------


async def test_the_key_under_which_the_intent_is_remembered_is_a_stable_literal(db, session, provider):
    # (S21) ключ лежит в базе: переименование осиротит живые счета и разойдётся с оформлением при зачислении (16а-2).
    assert dfc.TOPUP_INTENT_KEY == 'topup_intent'

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    stored = session.get(PlategaPayment, int(response.payment_id))
    assert stored.metadata_json['topup_intent']['status'] == 'pending'


async def test_naive_intent_time_is_read_as_utc_not_a_crash(db, session, env):
    # (S11) время без пояса не должно ронять сравнение с «сейчас» — иначе пополнение человека падает на 2 часа.
    naive = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    live = _intent_payment(session, payment_id=60, created_at=naive)

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)


@pytest.mark.parametrize('garbage', ['not-a-time', None, ''])
async def test_garbage_intent_time_is_neither_live_nor_a_crash(db, session, env, garbage):
    # (S12, S28) мусор вместо времени: счёт не считается «живым» (время рождения неизвестно), решение не падает.
    _intent_payment(session, payment_id=60, created_at=garbage)

    assert (await _decide(db, session)).status == 'accepted'


@pytest.mark.parametrize('metadata', [None, {'topup_intent': 'garbage'}, {'topup_intent': ['x']}])
async def test_other_payments_with_empty_or_broken_metadata_do_not_break_the_decision(db, session, env, metadata):
    # (S13, S14) выборка читает ВСЕ платежи человека за 2 часа: пустые метаданные (NULL) и чужой мусор не должны её ронять.
    _bare_payment(session, payment_id=95, metadata=metadata)

    assert (await _decide(db, session)).status == 'accepted'


# --- что считается живым счётом ------------------------------------------------------------------------------------


@pytest.mark.parametrize('provider_status', ['INPROGRESS', 'inprogress', 'pending'])
async def test_invoice_in_progress_or_with_a_lowercase_status_is_still_live(db, session, env, provider_status):
    # (S24, S31) те же статусы «ждёт оплаты», что у `_is_live_direct_provider_invoice`, и без учёта регистра.
    live = _intent_payment(session, payment_id=61, provider_status=provider_status)

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)


async def test_invoice_with_a_provider_deadline_in_the_future_is_reused(db, session, env):
    # (S32) срок провайдера, если назван и не вышел, не делает счёт мёртвым.
    live = _intent_payment(session, payment_id=61, expires_in=timedelta(minutes=10))

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)


async def test_newest_live_invoice_of_the_same_order_is_the_one_reused(db, session, env):
    # (S38, P29) два живых счёта одного заказа и способа (гонка двух запросов): отдаём самый новый.
    _intent_payment(session, payment_id=60, created_ago=timedelta(minutes=10))
    newest = _intent_payment(session, payment_id=61, created_ago=timedelta(minutes=5))

    decision = await _decide(db, session)

    assert (decision.status, decision.payment.id) == ('already_paying', newest.id)


async def test_intent_row_older_than_two_hours_is_not_looked_at(db, session, env):
    # (S37, необязательный: граничный случай) окно выборки — ровно вдвое шире срока намерения; всё, что старше, в решение
    # не входит. Закрепляет документированное окно: расширять его без причины незачем.
    _intent_payment(
        session,
        payment_id=53,
        status='fulfilled',
        is_paid=True,
        transaction_id=903,
        created_ago=timedelta(hours=3),
        checkout_public_id='chk-ancient',
        decided_at=_ago(10),
    )

    assert (await _decide(db, session)).status == 'accepted'


async def test_paid_flag_alone_counts_as_money_that_has_come(db, session, env):
    # (S29) вебхук успел поставить `is_paid`, а `transaction_id` ещё нет (или не поставит: ветка «пользователь не найден»).
    found = _intent_payment(session, payment_id=67, provider_status='CONFIRMED', is_paid=True)

    decision = await _decide(db, session, period_days=90, method=11)

    assert (decision.status, decision.payment.id) == ('already_paid', found.id)


# --- цена, баланс, сумма, порядок проверок ---------------------------------------------------------------------------


async def test_price_and_amount_follow_the_requested_device_count(db, session, env):
    # (P04) цену берём по ЗАПРОШЕННОМУ числу устройств; во всех прежних тестах устройство одно.
    decision = await _decide(db, session, devices=2)

    assert (decision.status, decision.price_kopeks, decision.amount_kopeks, decision.devices) == (
        'accepted',
        19_900,
        14_900,
        2,
    )
    assert (decision.intent['devices'], decision.intent['quote_kopeks']) == (2, 19_900)


async def test_options_and_open_order_are_looked_up_for_this_very_user(db, session, env):
    # (P07, X18) заглушки `build_purchase_options` и `get_open_checkout_for_user` принимают что угодно — закрепляем, ЧЬИ
    # цены и ЧЕЙ заказ ищутся.
    await _decide(db, session)

    env.options.assert_awaited_once_with(db, _user(session))
    env.open_checkout.assert_awaited_once_with(db, user_id=1)


async def test_unpriceable_selection_is_answered_before_the_open_order_is_looked_up(db, session, env):
    # (P15) порядок проверок — контракт (docstring `prepare_topup_intent`): цена → открытый заказ.
    env.options.return_value = _options(eligible=False)
    env.open_checkout.return_value = SimpleNamespace(
        lifecycle_state='awaiting_funds', public_id='chk-live-7', period_days=90, selected_device_limit=2
    )

    decision = await _decide(db, session)

    assert (decision.status, decision.reason) == ('ordinary', 'unavailable')
    env.open_checkout.assert_not_awaited()


async def test_refused_intent_with_a_fresh_decision_time_does_not_block_a_new_invoice(db, session, env):
    # (P17) запирает только ОФОРМЛЕННЫЙ заказ: отказ (деньги остались на балансе) не должен держать человека час.
    _intent_payment(
        session,
        payment_id=53,
        status='refused',
        reason='price_changed',
        provider_status='CONFIRMED',
        is_paid=True,
        transaction_id=903,
        decided_at=_ago(10),
    )

    assert (await _decide(db, session)).status == 'accepted'


async def test_fulfilled_intent_without_a_decision_time_does_not_crash(db, session, env):
    # (P18) защита от строки «оформлено» без времени решения: не падаем и не запираем.
    _intent_payment(
        session,
        payment_id=54,
        status='fulfilled',
        provider_status='CONFIRMED',
        is_paid=True,
        transaction_id=904,
        checkout_public_id='chk-x',
    )

    assert (await _decide(db, session)).status == 'accepted'


@pytest.mark.parametrize('overrides', [{'devices': 2}, {'period_days': 90}])
async def test_live_invoice_of_another_order_is_not_reused_even_with_the_same_amount_and_price(
    db, session, env, overrides
):
    # (P27, P28) заказ различается сроком и устройствами САМ по себе: сумма и цена в счёте их не заменяют (в прежних
    # тестах они всегда расходились вместе с заказом, и проверка суммы прикрывала дыру в сравнении заказа).
    _intent_payment(session, payment_id=63, **overrides)

    assert (await _decide(db, session)).status == 'accepted'


async def test_one_kopek_short_of_the_price_still_needs_an_invoice(db, session, env):
    # (P33) баланс покрывает цену только при `>=`; недостача в копейку — это счёт на целый рубль.
    _set_user(session, balance_kopeks=PRICE_30_1 - 1)

    decision = await _decide(db, session)

    assert (decision.status, decision.amount_kopeks) == ('accepted', 100)


@pytest.mark.parametrize(
    ('intent', 'expected'),
    [
        ({'provider_status': 'CONFIRMED', 'is_paid': True, 'transaction_id': 903}, 'already_paid'),
        (
            {
                'status': 'fulfilled',
                'is_paid': True,
                'transaction_id': 904,
                'checkout_public_id': 'chk-done-5',
                'decided_at': _ago(10),
            },
            'already_fulfilled',
        ),
    ],
)
async def test_credited_money_is_named_before_the_balance_that_now_covers_the_price(db, session, env, intent, expected):
    # (P35) деньги по намерению уже на балансе, поэтому он «покрывает» цену, — но это исход намерения, а не повод
    # купить с баланса руками поверх оформления, которое идёт само.
    _intent_payment(session, payment_id=67, **intent)
    if expected == 'already_fulfilled':
        _purchase(session, minutes_ago=10, checkout_public_id='chk-done-5')  # оформление — это и есть покупка
    _set_user(session, balance_kopeks=BALANCE + TOP_UP_30_1)

    assert (await _decide(db, session)).status == expected


@pytest.mark.parametrize(
    ('min_kopeks', 'max_kopeks', 'expected'),
    [
        (TOP_UP_30_1, 100_000_000, ('accepted', None)),  # сумма ровно на нижней границе способа
        (100, TOP_UP_30_1, ('accepted', None)),  # ровно на верхней
        (100, TOP_UP_30_1 - 100, ('ordinary', 'unavailable')),  # выше верхней границы способа
    ],
)
async def test_amount_on_the_method_bounds_is_accepted_and_above_the_max_is_not(
    db, session, env, min_kopeks, max_kopeks, expected
):
    # (P41, P42, P43) границы включительно. Нижняя — не экзотика: при недостаче меньше минимума кассы сумма счёта
    # равна именно минимуму, то есть всегда лежит на границе.
    decision = await _decide(db, session, min_kopeks=min_kopeks, max_kopeks=max_kopeks)

    assert (decision.status, decision.reason) == expected


async def test_reused_invoice_names_the_requested_devices_and_price(db, session, env):
    # (P55) у исхода без нового счёта экран показывает срок, устройства и цену заказа, о котором исход.
    live = _intent_payment(session, payment_id=60, devices=2, quote_kopeks=19_900, amount_kopeks=14_900)

    decision = await _decide(db, session, devices=2)

    assert (decision.status, decision.payment.id) == ('already_paying', live.id)
    assert (decision.devices, decision.price_kopeks, decision.period_days) == (2, 19_900, 30)


async def test_balance_covers_names_the_requested_devices(db, session, env):
    # (P53)
    _set_user(session, balance_kopeks=19_900)

    decision = await _decide(db, session, devices=2)

    assert (decision.status, decision.devices, decision.period_days) == ('balance_covers', 2, 30)


# --- замена своих неоплаченных ---------------------------------------------------------------------------------------


async def test_replace_leaves_an_already_closed_older_intent_as_it_was(db, session, env):
    # (R08) уже закрытое («cancelled») намерение не перезаписывается меткой «replaced» и не теряет свою причину.
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70, status='cancelled')

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['status'] == 'cancelled'


async def test_replace_never_touches_a_row_the_webhook_has_flagged_paid(db, session, env):
    # (R09) `is_paid` без `transaction_id` — тоже оплачено (вебхук ставит их не одним движением).
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70, provider_status='CONFIRMED', is_paid=True)

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['status'] == 'pending'


async def test_replace_keeps_the_rest_of_the_intent_and_of_the_payment_metadata(db, session, env):
    # (R12, R13) замена меняет метку, а не стирает память о заказе и прочие метаданные счёта (язык, способ).
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70)

    await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id)

    stored = session.get(PlategaPayment, 70)
    intent = dfc.topup_intent_of(stored)
    assert (intent['status'], intent['replaced_by']) == ('replaced', 74)
    assert (intent['period_days'], intent['devices'], intent['quote_kopeks'], intent['method']) == (
        30,
        1,
        PRICE_30_1,
        2,
    )
    assert 'created_at' in intent
    assert (stored.metadata_json['language'], stored.metadata_json['selected_method']) == ('ru', 2)


async def test_replace_is_committed(db, session, env):
    # (R16) route не коммитит сам: без коммита внутри замена пропала бы вместе с сессией запроса.
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70)

    await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id)
    session.rollback()  # всё, что не закоммичено, пропадает

    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['status'] == 'replaced'


async def test_replace_with_a_newer_payment_that_has_no_intent_touches_nothing(db, session, env):
    # (R19) страховка: если у «нового» платежа намерения нет (метаданные не легли), старые не трогаем и не падаем.
    _bare_payment(session, payment_id=74, metadata={'language': 'ru'})
    _intent_payment(session, payment_id=70)

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=74) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['status'] == 'pending'


async def test_replace_skips_an_older_intent_whose_time_is_garbage(db, session, env):
    # (R04) испорченное время у старого намерения: пропускаем, а не роняем выдачу счёта.
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70, created_at='not-a-time')

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id) == 0
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['status'] == 'pending'


async def test_row_that_lost_its_intent_between_read_and_lock_is_left_alone(db, session, env):
    # (R11) как и «оплачено между чтением и замком»: перечитанная под замком строка без намерения пропускается.
    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    held = _intent_payment(session, payment_id=70)  # сессия держит строку с намерением (ссылка — чтобы не потерялась)
    session.execute(text('UPDATE platega_payments SET metadata_json = \'{"language": "ru"}\' WHERE id = 70'))
    session.commit()

    assert await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id) == 0
    assert dfc.topup_intent_of(held) is None


async def test_replace_locks_the_older_rows_one_by_one_in_ascending_order(db, session, env, monkeypatch):
    # (R05, R06) FOR UPDATE и порядок захвата по возрастанию номера: два замещающих запроса не должны брать строки
    # навстречу друг другу. SQLite замок не исполняет, поэтому смотрим на сами запросы, как их увидит Postgres.
    from sqlalchemy.dialects import postgresql

    newer = _intent_payment(session, payment_id=74, created_ago=timedelta(minutes=1))
    _intent_payment(session, payment_id=70, created_ago=timedelta(minutes=9))
    _intent_payment(session, payment_id=71, created_ago=timedelta(minutes=8))
    locked, original = [], db.execute

    async def spy(statement, *args, **kwargs):
        compiled = statement.compile(dialect=postgresql.dialect())
        if 'FOR UPDATE' in str(compiled):
            locked.append(next(iter(compiled.params.values())))
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(db, 'execute', spy)

    await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=newer.id)

    assert locked == [70, 71]


# --- маршрут /topup ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('amount', 'detail'),
    [(99, 'Minimum amount is 1.00 RUB'), (100, None), (1_000, None), (1_001, 'Maximum amount is 10.00 RUB')],
)
def test_top_up_amount_bounds_are_inclusive_and_name_the_right_limit(amount, detail):
    # (B02, B03, B04, B05) сторожа предела суммы: максимум в прежних тестах не проверялся вовсе.
    method = PaymentMethodResponse(id='platega', name='Platega', min_amount_kopeks=100, max_amount_kopeks=1_000)

    if detail is None:
        balance_route._check_top_up_amount(amount, method)
        return
    with pytest.raises(HTTPException) as error:
        balance_route._check_top_up_amount(amount, method)
    assert (error.value.status_code, error.value.detail) == (400, detail)


async def test_route_other_method_with_intent_still_checks_the_client_amount(db, session, provider):
    # (B09) отказ от проверки суммы при намерении действует только для Platega; у остальных способов она обязательна.
    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(
            request=_request(method='yookassa', option=None, amount_kopeks=1), user=_user(session), db=db
        )

    assert (error.value.status_code, error.value.detail) == (400, 'Minimum amount is 1.00 RUB')


@pytest.mark.parametrize('outcome', ['balance_covers', 'already_fulfilled', 'order_on_review'])
async def test_route_every_outcome_without_an_invoice_has_no_payment_and_never_calls_the_provider(
    db, session, provider, env, outcome
):
    # (B12, B13, B14, B39) прежние тесты проверяли это для open_order, already_paid и already_paying — не для этих.
    if outcome == 'balance_covers':
        _set_user(session, balance_kopeks=PRICE_30_1)
    elif outcome == 'already_fulfilled':
        _intent_payment(
            session,
            payment_id=50,
            status='fulfilled',
            is_paid=True,
            transaction_id=900,
            checkout_public_id='chk-done-5',
            decided_at=_ago(10),
        )
        _purchase(session, minutes_ago=10, checkout_public_id='chk-done-5')
    else:
        env.open_checkout.return_value = SimpleNamespace(
            lifecycle_state='operator_review', public_id='chk-rev-1', period_days=30, selected_device_limit=1
        )

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert response.intent_status == outcome
    assert (response.payment_id, response.payment_url, response.amount_kopeks, response.status) == (
        None,
        None,
        0,
        'none',
    )
    assert provider.calls == []


async def test_route_open_order_names_its_own_term_and_devices(db, session, provider, env):
    # (B45) срок и устройства в ответе — найденного заказа, а не запрошенного.
    env.open_checkout.return_value = SimpleNamespace(
        lifecycle_state='awaiting_funds', public_id='chk-live-9', period_days=90, selected_device_limit=2
    )

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.checkout_public_id, response.period_days, response.devices) == (
        'open_order',
        'chk-live-9',
        90,
        2,
    )


async def test_route_invoice_description_and_rubles_carry_the_server_amount(db, session, provider):
    # (B20, B31) человек видит в платёжной форме и в ответе ту сумму, которую реально платит, а не заглушку экрана.
    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert response.amount_rubles == TOP_UP_30_1 / 100
    description = provider.calls[0]['description']
    assert settings.format_price(TOP_UP_30_1) in description
    assert settings.format_price(CLIENT_AMOUNT) not in description


async def test_route_invoice_refused_with_intent_says_what_it_would_have_billed(db, session, provider):
    # (B27, B29, B30) «счёт не выдан»: статус, сумма сервера, срок и устройства — для экрана заказа (текст ВК-15).
    provider.fail = True

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.status, response.intent_status, response.payment_id, response.payment_url) == (
        'failed',
        'invoice_not_created',
        None,
        None,
    )
    assert (response.amount_kopeks, response.amount_rubles, response.period_days, response.devices) == (
        TOP_UP_30_1,
        TOP_UP_30_1 / 100,
        30,
        1,
    )


async def test_route_reopened_invoice_carries_the_provider_deadline(db, session, provider):
    # (B41) срок счёта, если провайдер его назвал, нужен экрану для обратного отсчёта.
    live = _intent_payment(session, payment_id=60, expires_in=timedelta(minutes=10))

    again = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (again.intent_status, again.expires_at) == ('already_paying', live.expires_at)


async def test_route_failed_replace_does_not_take_the_invoice_away(db, session, provider, monkeypatch):
    # (B25, B26) счёт уже выдан: сбой замены старых намерений глотается, а сессия откатывается.
    async def boom(*_, **__):
        raise RuntimeError('db hiccup')

    monkeypatch.setattr(balance_route, 'replace_older_topup_intents', boom)
    rollback = AsyncMock()
    monkeypatch.setattr(db, 'rollback', rollback)

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.payment_url) == ('accepted', 'https://pay.test/new-1')
    rollback.assert_awaited_once()


@pytest.mark.parametrize(
    ('min_kopeks', 'max_kopeks', 'client_amount'),
    [(10_000, 100_000_000, CLIENT_AMOUNT), (100, 9_000, 5_000)],
)
async def test_route_hands_the_narrowed_method_range_to_the_decision(
    db, session, provider, monkeypatch, min_kopeks, max_kopeks, client_amount
):
    # (B07, B08) диапазон способа сужен в админке: сервер не подменяет сумму (мина ON), счёт выставляется на сумму клиента.
    async def narrowed(*_, **__):
        return [
            PaymentMethodResponse(
                id='platega', name='Platega', min_amount_kopeks=min_kopeks, max_amount_kopeks=max_kopeks
            )
        ]

    monkeypatch.setattr(balance_route, 'get_payment_methods', narrowed)

    response = await balance_route.create_topup(
        request=_request(amount_kopeks=client_amount), user=_user(session), db=db
    )

    assert (response.intent_status, response.intent_reason, response.amount_kopeks) == (
        'ordinary',
        'unavailable',
        client_amount,
    )


# --- исход для экрана --------------------------------------------------------------------------------------------------


async def test_replaced_or_cancelled_intent_that_got_paid_still_gets_the_purchase_hint(db, session, monkeypatch):
    # (B54) закрытое до оплаты намерение сервер уже не оформит: подсказка «оформите подписку» обязана остаться.
    hint = AsyncMock(return_value='Оформите подписку')
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', hint)
    _intent_payment(session, payment_id=82, status='replaced', provider_status='CONFIRMED', is_paid=True)

    response = await balance_route.get_pending_payment_details(
        method='platega', payment_id=82, user=_user(session), db=db
    )

    assert (response.intent_outcome, response.purchase_step_pending) == ('closed', True)


async def test_unpaid_payment_is_never_asked_for_the_purchase_hint(db, session, monkeypatch):
    # (B56) маршрут опрашивают раз в три секунды: подсказку спрашиваем только у оплаченного.
    hint = AsyncMock(return_value='Оформите подписку')
    monkeypatch.setattr('app.services.payment.common.topup_pending_purchase_hint', hint)
    _intent_payment(session, payment_id=83, status='pending')  # PENDING у провайдера, не оплачен, намерение «ждём»
    _bare_payment(session, payment_id=84, metadata={'language': 'ru'})  # обычное пополнение, не оплачено

    for payment_id in (83, 84):
        response = await balance_route.get_pending_payment_details(
            method='platega', payment_id=payment_id, user=_user(session), db=db
        )
        assert response.purchase_step_pending is False
    hint.assert_not_awaited()

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
    )


# --- кому включено ----------------------------------------------------------------------------------------------


async def test_not_a_stand_gets_ordinary_top_up_without_a_single_check_below(db, session, env):
    decision = await _decide(db, session, user_id=2)

    assert (decision.status, decision.reason) == ('ordinary', 'disabled')
    env.options.assert_not_awaited()
    env.open_checkout.assert_not_awaited()


async def test_who_gets_it_is_a_code_constant_not_an_admin_switch(session, env, monkeypatch):
    # Ответ владельца 07.10.2026 17:10: «не городить переключатели в админке» — флаг автопокупки не влияет.
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False, raising=False)
    stand, client = _user(session, 1), _user(session, 2)
    assert (dfc.topup_intent_enabled_for(stand), dfc.topup_intent_enabled_for(client)) == (True, False)
    # Всех включает только точное 'all'; 'off' и опечатка — выключают, а не раздают всем.
    for rollout, expected in (('all', (True, True)), ('off', (False, False)), ('stand', (False, False))):
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


async def test_order_fulfilled_within_an_hour_blocks_a_second_term_and_names_it(db, session, env):
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

    decision = await _decide(db, session, period_days=90)

    assert (decision.status, decision.checkout_public_id) == ('already_fulfilled', 'chk-done-5')
    assert (decision.period_days, decision.devices) == (30, 1)


async def test_intent_made_70_minutes_ago_and_fulfilled_50_minutes_ago_still_blocks(db, session, env):
    # Окно поиска вдвое шире срока намерения: оформлено в последний час, хотя намерение старше часа.
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


async def test_same_order_other_method_is_a_new_invoice(db, session, env):
    _intent_payment(session, payment_id=62, method=2)

    assert (await _decide(db, session, method=11)).status == 'accepted'


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
) -> TopUpRequest:
    return TopUpRequest(
        amount_kopeks=amount_kopeks,
        payment_method=method,
        payment_option=option,
        intent=TopUpIntent(period_days=period_days, devices=1) if intent else None,
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


async def test_route_ordinary_top_up_still_checks_the_client_amount(db, session, provider):
    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(request=_request(amount_kopeks=1), user=_user(session, 2), db=db)

    assert (error.value.status_code, error.value.detail) == (400, 'Minimum amount is 1.00 RUB')
    assert provider.calls == []


async def test_route_switching_method_replaces_the_old_invoice(db, session, provider):
    first = await balance_route.create_topup(request=_request(option='2'), user=_user(session), db=db)
    second = await balance_route.create_topup(request=_request(option='11'), user=_user(session), db=db)

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


async def test_route_disabled_intent_is_an_ordinary_top_up_for_the_client_amount(db, session, provider):
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
    # Оформление — 16а-2; 16а-1 намерение при зачислении не трогает.
    assert dfc.topup_intent_of(stored)['status'] == 'pending'


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


@pytest.mark.parametrize(('user_id', 'enabled'), [(1, True), (2, False)])
async def test_purchase_options_tell_the_screens_whom_it_is_enabled_for(session, env, monkeypatch, user_id, enabled):
    monkeypatch.setattr(device_first_route, 'build_purchase_options', AsyncMock(return_value=_options()))

    result = await device_first_route.purchase_options(user=_user(session, user_id), db=AsyncMock())

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


async def test_purchase_options_flag_follows_the_shared_rule_not_just_the_stand_list(session, env, monkeypatch):
    monkeypatch.setattr(device_first_route, 'build_purchase_options', AsyncMock(return_value=_options()))
    _set_user(session, restriction_subscription=1)

    result = await device_first_route.purchase_options(user=_user(session, 1), db=AsyncMock())

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

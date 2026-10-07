"""ВК-16, часть 16а-1 (07.10.2026): пополнение помнит заказ, сервер говорит исход.

Решение владельца 05.10.2026 «Оформляется само» разворачивает решение 02.09 «одно нажатие»: доплата под заказ
оформит его без нажатия (это 16а-2). Здесь сторожим память о заказе в счёте и честный ответ ДО счёта.

Выборки намерений, замена старых и фильтр `/latest` исполняет НАСТОЯЩИЙ движок SQLite (таблицы из моделей без
типов Postgres — образец `test_recent_payments_live_only.py`); счёт создаёт настоящий код Platega, подменён только
сетевой вызов провайдера. Цена и суммы в фикстурах нарочно не круглые: 50,37 ₽ на балансе, клиент просит 123,45 ₽.
"""

from __future__ import annotations

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
from app.database.models import CheckoutPaymentAttempt, DeviceAddonTopupAttempt, PlategaPayment, User
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
    expires_in: timedelta | None = timedelta(minutes=25),
    **intent_extra,
) -> PlategaPayment:
    now = datetime.now(UTC)
    intent = {
        'period_days': period_days,
        'devices': devices,
        'quote_kopeks': PRICE_30_1,
        'method': method,
        'created_at': (now - created_ago).isoformat(),
        'status': status,
        **intent_extra,
    }
    payment = PlategaPayment(
        id=payment_id,
        user_id=user_id,
        amount_kopeks=TOP_UP_30_1,
        currency='RUB',
        status=provider_status,
        is_paid=is_paid,
        payment_method_code=method,
        correlation_id=f'corr-{payment_id}',
        platega_transaction_id=f'tx-{payment_id}',
        redirect_url=f'https://pay.test/{payment_id}',
        payload=f'platega:corr-{payment_id}',
        metadata_json={dfc.TOPUP_INTENT_KEY: intent, 'language': 'ru', 'selected_method': method},
        transaction_id=transaction_id,
        expires_at=(now + expires_in) if expires_in is not None else None,
        created_at=now - created_ago,
    )
    session.add(payment)
    session.commit()
    return payment


async def _decide(db, session, *, user_id: int = 1, period_days: int = 30, devices: int = 1, method: int = 2):
    return await dfc.prepare_topup_intent(
        db,
        user=_user(session, user_id),
        period_days=period_days,
        devices=devices,
        method_code=method,
        min_kopeks=100,
        max_kopeks=100_000_000,
    )


# --- исходы до счёта ----------------------------------------------------------------------------------------


async def test_not_a_stand_gets_ordinary_top_up_without_a_single_check_below(db, session, env):
    decision = await _decide(db, session, user_id=2)

    assert (decision.status, decision.reason) == ('ordinary', 'disabled')
    env.options.assert_not_awaited()
    env.open_checkout.assert_not_awaited()


async def test_stands_only_is_a_code_constant_not_an_admin_switch(session, env, monkeypatch):
    # Ответ владельца 07.10.2026 17:10: «не городить переключатели в админке» — флаг автопокупки не влияет.
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False, raising=False)
    assert dfc.topup_intent_enabled_for(_user(session, 1)) is True
    assert dfc.topup_intent_enabled_for(_user(session, 2)) is False
    monkeypatch.setattr(dfc, 'TOPUP_INTENT_STANDS_ONLY', False)
    assert dfc.topup_intent_enabled_for(_user(session, 2)) is True


async def test_account_being_erased_gets_ordinary_top_up(db, session, env):
    session.execute(text("UPDATE users SET account_erasure_requested_at = '2026-10-07 10:00:00' WHERE id = 1"))
    session.commit()

    decision = await _decide(db, session)

    assert (decision.status, decision.reason) == ('ordinary', 'account_erasure')
    env.options.assert_not_awaited()


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
async def test_live_order_blocks_a_new_invoice(db, session, env, lifecycle_state, expected):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state=lifecycle_state, public_id='chk-live-7')

    decision = await _decide(db, session)

    assert (decision.status, decision.checkout_public_id) == (expected, 'chk-live-7')


@pytest.mark.parametrize('lifecycle_state', ['draft', 'confirmed'])
async def test_stale_quote_does_not_block(db, session, env, lifecycle_state):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state=lifecycle_state, public_id='chk-quote')

    decision = await _decide(db, session)

    assert decision.status == 'accepted'


async def test_corrupted_order_is_review_not_a_new_invoice(db, session, env):
    env.open_checkout.side_effect = dfc.DeviceFirstError('operator_review_required', 'corrupted')

    decision = await _decide(db, session)

    assert decision.status == 'order_on_review'


async def test_order_fulfilled_within_an_hour_blocks_a_second_term(db, session, env):
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


async def test_order_fulfilled_long_ago_does_not_block(db, session, env):
    decided = (datetime.now(UTC) - timedelta(minutes=70)).isoformat()
    _intent_payment(
        session,
        payment_id=51,
        status='fulfilled',
        is_paid=True,
        transaction_id=901,
        created_ago=timedelta(minutes=80),
        checkout_public_id='chk-old',
        decided_at=decided,
    )

    assert (await _decide(db, session)).status == 'accepted'


async def test_same_order_same_method_live_invoice_is_reused(db, session, env):
    live = _intent_payment(session, payment_id=60)

    decision = await _decide(db, session)

    assert decision.status == 'already_paying'
    assert decision.payment.id == live.id


async def test_same_order_other_method_is_a_new_invoice(db, session, env):
    _intent_payment(session, payment_id=61, method=2)

    assert (await _decide(db, session, method=11)).status == 'accepted'


async def test_other_order_same_method_is_a_new_invoice(db, session, env):
    _intent_payment(session, payment_id=62)

    assert (await _decide(db, session, period_days=90)).status == 'accepted'


async def test_paid_intent_awaiting_outcome_blocks_any_new_invoice(db, session, env):
    paid = _intent_payment(session, payment_id=63, provider_status='CONFIRMED', is_paid=True, transaction_id=903)

    decision = await _decide(db, session, period_days=90, method=11)

    assert (decision.status, decision.payment.id) == ('already_paying', paid.id)


async def test_credited_intent_with_flipped_is_paid_still_counts_as_paid(db, session, env):
    # Мина OH: поздний «отменён» по счёту-сироте пишет `is_paid=false`; зачисление выдаёт `transaction_id`.
    _intent_payment(session, payment_id=64, provider_status='CANCELED', is_paid=False, transaction_id=904)

    assert (await _decide(db, session, period_days=90)).status == 'already_paying'


@pytest.mark.parametrize(
    'overrides',
    [
        {'expires_in': timedelta(minutes=-1)},
        {'provider_status': 'CANCELED'},
        {'created_ago': timedelta(minutes=61), 'expires_in': timedelta(minutes=5)},
        {'status': 'replaced'},
        {'status': 'cancelled'},
        {'user_id': 2},
    ],
)
async def test_dead_or_foreign_invoice_is_not_reused(db, session, env, overrides):
    _intent_payment(session, payment_id=65, **overrides)

    assert (await _decide(db, session)).status == 'accepted'


async def test_balance_that_covers_the_price_needs_no_invoice(db, session, env):
    session.execute(text(f'UPDATE users SET balance_kopeks = {PRICE_30_1} WHERE id = 1'))
    session.commit()

    decision = await _decide(db, session)

    assert (decision.status, decision.price_kopeks) == ('balance_covers', PRICE_30_1)


async def test_accepted_intent_amount_is_the_cashier_formula_and_remembers_the_order(db, session, env):
    decision = await _decide(db, session)

    assert decision.status == 'accepted'
    assert decision.amount_kopeks == TOP_UP_30_1
    assert decision.amount_kopeks == dfc.device_first_top_up_kopeks(price_kopeks=PRICE_30_1, balance_kopeks=BALANCE)
    assert decision.price_kopeks == PRICE_30_1
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
    decision = await dfc.prepare_topup_intent(
        db, user=_user(session), period_days=30, devices=1, method_code=2, min_kopeks=10_000, max_kopeks=100_000
    )

    assert (decision.status, decision.reason) == ('ordinary', 'unavailable')


# --- замена старых намерений ----------------------------------------------------------------------------------


async def test_newer_intent_replaces_own_unpaid_older_ones_only(db, session, env):
    _intent_payment(session, payment_id=70)
    _intent_payment(session, payment_id=71, is_paid=True, provider_status='CONFIRMED', transaction_id=971)
    _intent_payment(session, payment_id=72, status='fulfilled', is_paid=True, transaction_id=972)
    _intent_payment(session, payment_id=73, user_id=2)
    _intent_payment(session, payment_id=75)

    replaced = await dfc.replace_older_topup_intents(db, user_id=1, newer_payment_id=74)

    assert replaced == 1
    statuses = {row.id: dfc.topup_intent_of(row)['status'] for row in session.query(PlategaPayment).all()}
    assert statuses == {70: 'replaced', 71: 'pending', 72: 'fulfilled', 73: 'pending', 75: 'pending'}
    assert dfc.topup_intent_of(session.get(PlategaPayment, 70))['replaced_by'] == 74


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


def _request(*, method: str = 'platega', option: str = '2', intent: bool = True, period_days: int = 30) -> TopUpRequest:
    return TopUpRequest(
        amount_kopeks=CLIENT_AMOUNT,
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


async def test_route_switching_method_replaces_the_old_invoice(db, session, provider):
    first = await balance_route.create_topup(request=_request(option='2'), user=_user(session), db=db)
    second = await balance_route.create_topup(request=_request(option='11'), user=_user(session), db=db)

    assert (first.intent_status, second.intent_status) == ('accepted', 'accepted')
    old = dfc.topup_intent_of(session.get(PlategaPayment, int(first.payment_id)))
    assert (old['status'], old['replaced_by']) == ('replaced', int(second.payment_id))
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(second.payment_id)))['status'] == 'pending'


async def test_route_reopening_the_same_order_returns_the_same_invoice(db, session, provider):
    first = await balance_route.create_topup(request=_request(), user=_user(session), db=db)
    again = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert again.intent_status == 'already_paying'
    assert (again.payment_id, again.payment_url) == (first.payment_id, first.payment_url)
    assert len(provider.calls) == 1


async def test_route_outcome_without_invoice_never_calls_the_provider(db, session, provider, env):
    env.open_checkout.return_value = SimpleNamespace(lifecycle_state='awaiting_funds', public_id='chk-live-9')

    response = await balance_route.create_topup(request=_request(), user=_user(session), db=db)

    assert (response.intent_status, response.checkout_public_id) == ('open_order', 'chk-live-9')
    assert (response.payment_id, response.payment_url, response.amount_kopeks) == (None, None, 0)
    assert provider.calls == []


async def test_route_disabled_intent_is_an_ordinary_top_up_for_the_client_amount(db, session, provider):
    response = await balance_route.create_topup(request=_request(), user=_user(session, 2), db=db)

    assert (response.intent_status, response.intent_reason) == ('ordinary', 'disabled')
    assert response.amount_kopeks == CLIENT_AMOUNT
    assert dfc.topup_intent_of(session.get(PlategaPayment, int(response.payment_id))) is None


async def test_route_without_intent_answers_as_before(db, session, provider):
    response = await balance_route.create_topup(request=_request(intent=False), user=_user(session), db=db)

    assert response.amount_kopeks == CLIENT_AMOUNT
    assert (response.intent_status, response.intent_reason, response.price_kopeks) == (None, None, None)
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
        ({'status': 'fulfilled', 'checkout_public_id': 'chk-ok-3'}, ('fulfilled', 'chk-ok-3', None)),
        ({'status': 'refused', 'reason': 'price_changed'}, ('refused', None, 'price_changed')),
        ({'status': 'replaced'}, ('refused', None, 'replaced')),
        ({'status': 'cancelled'}, ('refused', None, 'cancelled')),
    ],
)
async def test_outcome_is_served_by_id_and_by_latest(db, session, monkeypatch, intent_extra, expected):
    monkeypatch.setattr(
        'app.services.payment.common.topup_pending_purchase_hint', AsyncMock(return_value=None), raising=False
    )
    _intent_payment(session, payment_id=80, **intent_extra)
    user = _user(session)

    by_id = await balance_route.get_pending_payment_details(method='platega', payment_id=80, user=user, db=db)
    latest = await balance_route.get_latest_payment_by_method(method='platega', user=user, db=db)

    assert (by_id.intent_outcome, by_id.intent_checkout_public_id, by_id.intent_reason) == expected
    assert (latest.id, latest.intent_outcome, latest.intent_checkout_public_id, latest.intent_reason) == (80, *expected)


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


class _WebhookService(PlategaPaymentMixin):
    def __init__(self) -> None:
        self.finalized: list[int] = []

    async def _finalize_platega_payment(self, db, payment, payload):
        self.finalized.append(payment.id)
        return payment


async def test_intent_payment_is_credited_by_the_generic_top_up_branch(db, session, monkeypatch):
    settle = AsyncMock(side_effect=AssertionError('намерение не прямая продажа'))
    monkeypatch.setattr(device_first_payment_service, 'settle_device_first_platega_payment', settle)
    _intent_payment(session, payment_id=95)
    service = _WebhookService()

    handled = await service.process_platega_webhook(
        db, {'id': 'tx-95', 'status': 'CONFIRMED', 'payload': 'platega:corr-95'}
    )

    assert handled is True
    assert service.finalized == [95]
    settle.assert_not_awaited()


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

    await _WebhookService().process_platega_webhook(
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

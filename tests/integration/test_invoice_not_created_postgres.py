"""ВК-15 на НАСТОЯЩЕМ PostgreSQL: сбой Platega без номера счёта отпускает заказ.

Подделка сессии не исполняет ни блокировок, ни отбора воркера сверки, ни захвата его
аренды, ни частичных уникальных индексов — а именно они решают, сможет ли человек купить
снова. Здесь всё настоящее: схема из моделей, `FOR UPDATE`, отбор и аренда сверки.

    INVOICE_NOT_CREATED_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \\
        tests/integration/test_invoice_not_created_postgres.py -q

⚠️ Тест пересоздаёт схему `public` — давать ему только отдельную пустую базу.
"""

from __future__ import annotations

import importlib
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio


try:  # pragma: no cover - глобальная фикстура ставит заглушку asyncpg
    import asyncpg

    if not hasattr(asyncpg, 'connect'):
        sys.modules.pop('asyncpg', None)
        asyncpg = importlib.import_module('asyncpg')
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import (
    Base,
    CheckoutPaymentAttempt,
    PlategaPayment,
    SubscriptionCheckout,
    Tariff,
    User,
)
from app.services import device_first_payment_service as service
from app.services.device_first_checkout_service import checkout_money_state, get_open_checkout_for_user


DATABASE_URL = os.getenv('INVOICE_NOT_CREATED_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DATABASE_URL, reason='INVOICE_NOT_CREATED_TEST_DATABASE_URL is required'),
]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    async with engine.begin() as connection:
        await connection.execute(text('DROP SCHEMA public CASCADE'))
        await connection.execute(text('CREATE SCHEMA public'))
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


class _NoNetworkPlatega:
    """Сверке у счёта без номера спрашивать Platega нечего: любой запрос — ошибка теста."""

    def __init__(self):
        self._max_retries = 1

    async def get_transaction(self, _transaction_id):
        raise AssertionError('у счёта без номера сверка не ходит в Platega')

    async def create_payment(self, **_kwargs):
        raise AssertionError('повторного счёта в той же попытке быть не может')


async def _seed(db, *, attempt_status: str = 'creating', paid: bool = False) -> tuple[int, int, int]:
    """Заказ «Базовый», 2 устройства, 199 ₽ — как у заказа 112, — и попытка без номера."""
    now = datetime.now(UTC)
    user = User(telegram_id=7_000_112, username='buyer112')
    tariff = Tariff(name='Базовый')
    db.add_all([user, tariff])
    await db.flush()
    checkout = SubscriptionCheckout(
        public_id='co-112',
        user_id=user.id,
        tariff_id=tariff.id,
        period_days=30,
        selected_device_limit=2,
        quoted_price_kopeks=19_900,
        max_price_kopeks=19_900,
        tariff_total_kopeks=19_900,
        external_payable_kopeks=19_900,
        pricing_revision=1,
        quote_expires_at=now + timedelta(minutes=30),
        expires_at=now + timedelta(minutes=30),
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        funding_mode='platega',
        financial_committed_at=now,
        settlement_mode=service.DIRECT_SETTLEMENT_MODE,
    )
    db.add(checkout)
    await db.flush()
    attempt = CheckoutPaymentAttempt(
        checkout_id=checkout.id,
        merchant_order_key='dfv2-co-112-orphan',
        method_key='sbp',
        provider_method_code=2,
        currency='RUB',
        requested_amount_kopeks=19_900,
        settlement_mode=service.DIRECT_SETTLEMENT_MODE,
        status=attempt_status,
        # Срок первой сверки уже наступил: прошло больше 5 минут.
        next_reconcile_at=now - timedelta(minutes=1),
    )
    db.add(attempt)
    await db.flush()
    payment = PlategaPayment(
        user_id=user.id,
        correlation_id='corr-112',
        amount_kopeks=19_900,
        currency='RUB',
        payment_method_code=2,
        status='CREATING',
        is_paid=paid,
        payload='platega:corr-112',
        metadata_json={'device_first_attempt_id': attempt.id, 'settlement_mode': service.DIRECT_SETTLEMENT_MODE},
    )
    db.add(payment)
    await db.flush()
    attempt.platega_payment_id = payment.id
    await db.commit()
    return user.id, checkout.id, attempt.id


async def _fresh(db, model, row_id):
    return (
        await db.execute(select(model).where(model.id == row_id).execution_options(populate_existing=True))
    ).scalar_one()


async def test_the_worker_releases_an_invoice_that_never_got_a_number(session, monkeypatch) -> None:
    monkeypatch.setattr(service, 'PlategaService', _NoNetworkPlatega)
    user_id, checkout_id, attempt_id = await _seed(session)

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    attempt = await _fresh(session, CheckoutPaymentAttempt, attempt_id)
    checkout = await _fresh(session, SubscriptionCheckout, checkout_id)
    payment = await _fresh(session, PlategaPayment, attempt.platega_payment_id)
    assert (attempt.status, attempt.reconciliation_reason) == (
        'failed',
        'provider_invoice_not_created:creation_interrupted',
    )
    assert attempt.lease_token is None, 'аренда сверки обязана освободиться'
    assert payment.status == 'FAILED'
    assert (checkout.lifecycle_state, checkout.funding_state, checkout.terminal_reason) == (
        'cancelled',
        'invoice_not_created',
        'provider_invoice_not_created',
    )
    # Ничего не держит человека: ни открытого заказа, ни заказа на разборе, ни живой попытки.
    assert await get_open_checkout_for_user(session, user_id=user_id) is None
    assert await service.get_pending_platega_attempt(session, checkout_id=checkout_id) is None
    on_review = await session.scalar(
        select(SubscriptionCheckout.id).where(
            SubscriptionCheckout.user_id == user_id,
            SubscriptionCheckout.lifecycle_state == 'operator_review',
        )
    )
    assert on_review is None
    assert await checkout_money_state(session, checkout) == 'no_money'

    # Следующее нажатие рождает новый заказ: частичный уникальный индекс «один открытый
    # заказ на человека» его пропускает, потому что прежний закрыт. Неотпущенный
    # `awaiting_funds` дал бы здесь отказ самой базы. Проверяем настоящей вставкой.
    now = datetime.now(UTC)
    session.add(
        SubscriptionCheckout(
            public_id='co-113',
            user_id=user_id,
            tariff_id=checkout.tariff_id,
            period_days=30,
            selected_device_limit=2,
            quoted_price_kopeks=19_900,
            max_price_kopeks=19_900,
            pricing_revision=1,
            quote_expires_at=now + timedelta(minutes=30),
            expires_at=now + timedelta(minutes=30),
            lifecycle_state='confirmed',
            settlement_mode=service.DIRECT_SETTLEMENT_MODE,
        )
    )
    await session.commit()
    assert (await get_open_checkout_for_user(session, user_id=user_id)).public_id == 'co-113'


async def test_a_paid_payment_without_a_number_stays_with_the_operator(session, monkeypatch) -> None:
    """Деньги без номера счёта не отпускаются никогда: прежний разбор, как до ВК-15."""
    monkeypatch.setattr(service, 'PlategaService', _NoNetworkPlatega)
    _user_id, checkout_id, attempt_id = await _seed(session, attempt_status='reconciliation', paid=True)

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    attempt = await _fresh(session, CheckoutPaymentAttempt, attempt_id)
    checkout = await _fresh(session, SubscriptionCheckout, checkout_id)
    assert (attempt.status, attempt.reconciliation_reason) == (
        'operator_review',
        'provider_invoice_creation_incomplete',
    )
    assert (checkout.lifecycle_state, checkout.terminal_reason) == (
        'operator_review',
        'provider_invoice_creation_incomplete',
    )


async def test_the_request_path_releases_without_waiting_for_the_worker(session) -> None:
    """Тот же вызов, что делает `_create_direct_platega_attempt` сразу после ответа без номера."""
    _user_id, checkout_id, attempt_id = await _seed(session, attempt_status='creating')
    attempt = await _fresh(session, CheckoutPaymentAttempt, attempt_id)

    released = await service._hold_direct_invoice_for_review(
        session,
        attempt_id=attempt_id,
        payment_id=attempt.platega_payment_id,
        reason='provider_invoice_creation_incomplete',
        release_failure='provider_response_missing_identity',
    )

    assert released is True
    assert service._invoice_creation_failed(attempt).code == 'provider_invoice_not_created'
    checkout = await _fresh(session, SubscriptionCheckout, checkout_id)
    assert (checkout.lifecycle_state, checkout.terminal_reason) == ('cancelled', 'provider_invoice_not_created')

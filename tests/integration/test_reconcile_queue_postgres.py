"""Забитая очередь сверки — воспроизведение на НАСТОЯЩЕМ PostgreSQL.

Читающий сторож на исходник в этом проекте почти ничего не ловит. Здесь
поломка воспроизводится по-настоящему: строка, за которую воркер взялся и не
смог, обязана отодвинуть свой срок — иначе она навсегда остаётся в голове
очереди и не пропускает за собой живые платежи.

    RECONCILE_QUEUE_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \
        tests/integration/test_reconcile_queue_postgres.py -q
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


DATABASE_URL = os.getenv('RECONCILE_QUEUE_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DATABASE_URL, reason='RECONCILE_QUEUE_TEST_DATABASE_URL is required'),
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


async def _seed_delivered_sale(db, index: int) -> int:
    """Успешная выданная продажа: её попытка остаётся `paid_processing` навсегда."""
    now = datetime.now(UTC)
    user = User(telegram_id=900000 + index, username=f'buyer{index}')
    tariff = Tariff(name=f'tariff{index}')
    db.add_all([user, tariff])
    await db.flush()
    checkout = SubscriptionCheckout(
        public_id=f'co-{index}',
        user_id=user.id,
        tariff_id=tariff.id,
        period_days=30,
        selected_device_limit=1,
        quoted_price_kopeks=19900,
        max_price_kopeks=19900,
        pricing_revision=1,
        quote_expires_at=now,
        expires_at=now,
        lifecycle_state='ready',
        settlement_mode=service.DIRECT_SETTLEMENT_MODE,
    )
    payment = PlategaPayment(
        user_id=user.id,
        correlation_id=f'corr-{index}',
        amount_kopeks=19900,
        payment_method_code=1,
        status='CONFIRMED',
        is_paid=True,
    )
    db.add_all([checkout, payment])
    await db.flush()
    attempt = CheckoutPaymentAttempt(
        checkout_id=checkout.id,
        merchant_order_key=f'key-{index}',
        method_key='sbp',
        provider_method_code=1,
        requested_amount_kopeks=19900,
        status='paid_processing',
        settlement_mode=service.DIRECT_SETTLEMENT_MODE,
        provider_payment_id=f'prov-{index}',
        platega_payment_id=payment.id,
        # Срок из прошлого — ровно как на боевом.
        next_reconcile_at=now - timedelta(days=3),
    )
    db.add(attempt)
    await db.commit()
    return attempt.id


async def test_a_delivered_sale_stops_hogging_the_queue(session, monkeypatch) -> None:
    async def _refuses(*args, **kwargs):
        raise service.DeviceFirstError('invalid_state', 'Checkout is already being fulfilled')

    monkeypatch.setattr(service, 'fulfill_direct_external_checkout', _refuses)
    attempt_id = await _seed_delivered_sale(session, 1)
    before = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    after = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )
    assert after > before, 'строка снова не отодвинула свой срок и осталась в голове очереди'
    assert after > datetime.now(UTC), 'срок обязан уехать в будущее, а не остаться в прошлом'
    tries = await session.scalar(
        select(CheckoutPaymentAttempt.reconcile_attempts).where(CheckoutPaymentAttempt.id == attempt_id)
    )
    assert tries == 1


async def test_a_waiting_payment_gets_its_turn_after_one_pass(session, monkeypatch) -> None:
    """Главное, ради чего этап: живой платёж перестаёт быть двадцать первым.

    Двадцать выданных продаж занимают всю выборку. До починки строка, ждущая
    досверки, не попадала в неё никогда — ровно это и было на боевом три дня.
    """

    async def _refuses(*args, **kwargs):
        raise service.DeviceFirstError('invalid_state', 'Checkout is already being fulfilled')

    monkeypatch.setattr(service, 'fulfill_direct_external_checkout', _refuses)
    for index in range(1, 21):
        await _seed_delivered_sale(session, index)
    waiting_id = await _seed_delivered_sale(session, 21)
    # Двадцать первый ждёт дольше всех остальных по НОМЕРУ, но позже по сроку —
    # именно так он и оказывался за пробкой.
    await session.execute(
        text(
            "UPDATE checkout_payment_attempts SET status = :st, next_reconcile_at = now() - interval '1 day' WHERE id = :i"
        ),
        {'st': 'reconciliation', 'i': waiting_id},
    )
    await session.commit()

    picked = await service.reconcile_device_first_payments(session, limit=20, direct_only=True)
    assert picked >= 0

    # После одного прохода все двадцать пробок отодвинуты в будущее,
    # значит следующий проход возьмёт ждущего.
    stuck_ahead = await session.scalar(
        text(
            'SELECT count(*) FROM checkout_payment_attempts WHERE status = :st AND next_reconcile_at <= now()'
        ).bindparams(st='paid_processing')
    )
    assert stuck_ahead == 0, f'{stuck_ahead} выданных продаж всё ещё держат очередь'

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

from sqlalchemy import func, select, text
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


async def _seed_delivered_sale(db, index: int, *, delivered: bool = True) -> int:
    """Настоящая выданная продажа — та самая форма, что стоит на боевом.

    🔴 `fulfillment_state='fulfilled'` + `lifecycle_state='ready'` обязательны:
    именно на этой форме `fulfill_direct_external_checkout` возвращается ШТАТНО,
    не бросая исключения. Первая редакция починки стояла в `except` и на этой
    форме не срабатывала вовсе — а на боевом такими были все двадцать строк.
    """
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
        lifecycle_state='ready' if delivered else 'fulfilling',
        funding_state='funded' if delivered else 'paid',
        fulfillment_state='fulfilled' if delivered else 'not_started',
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


async def test_a_delivered_sale_stops_hogging_the_queue(session) -> None:
    """Без подмены: настоящая выданная продажа проходит настоящий код.

    Подменять `fulfill_direct_external_checkout` здесь нельзя — это ровно та
    функция, чьё поведение (вернуть или бросить) решает, сработает починка или
    нет. Тест с подменой прошёл бы и на коде, который беду не лечит.
    """
    attempt_id = await _seed_delivered_sale(session, 1)
    before = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    after = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )
    assert after > before, 'выданная продажа снова не отодвинула срок и держит голову очереди'
    assert after > datetime.now(UTC), 'срок обязан уехать в будущее'
    assert (
        await session.scalar(
            select(CheckoutPaymentAttempt.reconcile_attempts).where(CheckoutPaymentAttempt.id == attempt_id)
        )
        == 1
    )


async def test_a_paid_but_undelivered_order_keeps_its_fast_retry(session) -> None:
    """Тот, кто заплатил и ждёт подписку, замедляться не должен.

    Отличать «выдано, сверять нечего» от «деньги взяты, подписки нет» —
    единственное, что здесь важно: час ожидания на пустом месте платит клиент.
    """
    attempt_id = await _seed_delivered_sale(session, 2, delivered=False)
    before = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    after = await session.scalar(
        select(CheckoutPaymentAttempt.next_reconcile_at).where(CheckoutPaymentAttempt.id == attempt_id)
    )
    assert after == before, 'невыданный оплаченный заказ отодвинули — оплативший ждёт на пустом месте'


async def test_a_waiting_payment_gets_its_turn_after_one_pass(session) -> None:
    """Главное, ради чего этап: живой платёж перестаёт быть двадцать первым.

    Двадцать выданных продаж занимают всю выборку. До починки строка, ждущая
    досверки, не попадала в неё никогда — ровно это и было на боевом три дня.
    """
    for index in range(1, 21):
        await _seed_delivered_sale(session, index)

    await service.reconcile_device_first_payments(session, limit=20, direct_only=True)

    still_due = await session.scalar(
        select(func.count())
        .select_from(CheckoutPaymentAttempt)
        .where(
            CheckoutPaymentAttempt.status == 'paid_processing',
            CheckoutPaymentAttempt.next_reconcile_at <= datetime.now(UTC),
        )
    )
    assert still_due == 0, f'{still_due} выданных продаж всё ещё держат очередь после прохода'

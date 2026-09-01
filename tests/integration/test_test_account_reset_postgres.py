"""Обнуление тестового аккаунта на НАСТОЯЩЕМ PostgreSQL.

Здесь проверяется то, чего подделанная сессия проверить не может: переживёт ли
удаление настоящие внешние ключи. Именно на них молча падает очистка при
`/start` — база запрещает удалить подписку, пока на ней висит снимок прав.

Запускается по требованию:
    TEST_ACCOUNT_RESET_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \
        tests/integration/test_test_account_reset_postgres.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio


try:  # pragma: no cover - зависит от окружения
    import asyncpg

    if not hasattr(asyncpg, 'connect'):
        sys.modules.pop('asyncpg', None)
        asyncpg = importlib.import_module('asyncpg')
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    Base,
    CheckoutPaymentAttempt,
    PlategaPayment,
    ReferralEarning,
    ServerSquad,
    Subscription,
    SubscriptionCheckout,
    SubscriptionEntitlementSnapshot,
    SubscriptionServer,
    Tariff,
    Transaction,
    User,
    UserStatus,
)
from app.services import user_service


DATABASE_URL = os.getenv('TEST_ACCOUNT_RESET_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason='TEST_ACCOUNT_RESET_TEST_DATABASE_URL is required for the PostgreSQL reset tests',
    ),
]

STAND_TELEGRAM_ID = 7749231125
OUTSIDER_TELEGRAM_ID = 999000111


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    async with engine.begin() as connection:
        # `drop_all` не умеет разложить цикл subscription_checkouts <-> transactions;
        # для одноразовой базы проще снести схему целиком.
        await connection.execute(text('DROP SCHEMA public CASCADE'))
        await connection.execute(text('CREATE SCHEMA public'))
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _seed_person(db, telegram_id: int, *, balance_kopeks: int, checkout_state: str = 'ready') -> User:
    """Человек со всем, что накапливает стенд: подписка, снимок прав, заказ, деньги."""
    now = datetime.now(UTC)
    user = User(
        telegram_id=telegram_id,
        username=f'person{telegram_id}',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=balance_kopeks,
        has_had_paid_subscription=True,
        remnawave_uuid=f'panel-{telegram_id}',
    )
    db.add(user)
    await db.flush()

    squad = ServerSquad(squad_uuid=str(uuid.uuid4()), display_name=f'squad {telegram_id}', current_users=5)
    tariff = Tariff(name=f'tariff {telegram_id}')
    db.add_all([squad, tariff])
    await db.flush()

    subscription = Subscription(
        user_id=user.id,
        end_date=now + timedelta(days=30),
        is_trial=False,
        remnawave_short_id=f'sid{telegram_id}'[:16],
    )
    db.add(subscription)
    await db.flush()

    db.add_all(
        [
            # Ровно эта строка сегодня валит удаление подписки во всём коде.
            SubscriptionEntitlementSnapshot(
                subscription_id=subscription.id,
                location_ids=[1],
                technical_squad_uuids=[squad.squad_uuid],
                policy_revision=1,
                provenance='test',
                snapshot_hash=f'hash-{telegram_id}',
            ),
            SubscriptionServer(subscription_id=subscription.id, server_squad_id=squad.id),
        ]
    )

    checkout = SubscriptionCheckout(
        public_id=f'co-{telegram_id}',
        user_id=user.id,
        tariff_id=tariff.id,
        period_days=30,
        selected_device_limit=1,
        quoted_price_kopeks=19900,
        max_price_kopeks=19900,
        pricing_revision=1,
        quote_expires_at=now + timedelta(minutes=30),
        expires_at=now + timedelta(minutes=30),
        lifecycle_state=checkout_state,
    )
    payment = PlategaPayment(
        user_id=user.id,
        correlation_id=f'corr-{telegram_id}',
        amount_kopeks=19900,
        payment_method_code=1,
        status='CONFIRMED',
        is_paid=True,
    )
    transaction = Transaction(user_id=user.id, type='deposit', amount_kopeks=19900)
    db.add_all([checkout, payment, transaction])
    await db.flush()

    db.add(
        CheckoutPaymentAttempt(
            checkout_id=checkout.id,
            merchant_order_key=f'key-{telegram_id}',
            method_key='sbp',
            provider_method_code=1,
            requested_amount_kopeks=19900,
            status='failed',
            platega_payment_id=payment.id,
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _counts(db, user_id: int) -> dict[str, int]:
    async def one(model, column):
        return int(await db.scalar(select(func.count()).select_from(model).where(column == user_id)) or 0)

    return {
        'subscriptions': await one(Subscription, Subscription.user_id),
        'checkouts': await one(SubscriptionCheckout, SubscriptionCheckout.user_id),
        'payments': await one(PlategaPayment, PlategaPayment.user_id),
        'transactions': await one(Transaction, Transaction.user_id),
        'snapshots': int(
            await db.scalar(
                select(func.count())
                .select_from(SubscriptionEntitlementSnapshot)
                .join(Subscription, Subscription.id == SubscriptionEntitlementSnapshot.subscription_id)
                .where(Subscription.user_id == user_id)
            )
            or 0
        ),
    }


async def test_reset_removes_the_stand_and_leaves_everyone_else_alone(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)
    outsider = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=50000)

    # Живой клиент, которого стенд когда-то пригласил: его строку не трогаем.
    invited = User(telegram_id=555444333, username='invited', referred_by_id=stand.id)
    session.add(invited)
    await session.flush()
    session.add(ReferralEarning(user_id=stand.id, referral_id=invited.id, amount_kopeks=5000, reason='signup'))
    await session.commit()

    deleted_panel_uuids: list[str] = []

    async def _fake_panel_delete(user, panel_uuids):
        deleted_panel_uuids.extend(panel_uuids)
        return True

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _fake_panel_delete)

    before_outsider = await _counts(session, outsider.id)
    squad_users_before = await session.scalar(select(func.sum(ServerSquad.current_users)))

    # Первое нажатие: только показать.
    preview = await user_service.reset_test_account(session, stand, admin_id=1, confirm=False)
    assert preview.allowed is True
    assert preview.blocked_reason is None
    assert preview.done is False
    assert preview.balance_kopeks == 22450
    assert preview.orders == 1
    assert preview.payments == 1
    assert preview.invited_users == 1
    assert preview.panel_linked is True
    assert deleted_panel_uuids == []
    assert (await _counts(session, stand.id))['subscriptions'] == 1

    # Второе: снести.
    done = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)
    assert done.done is True
    assert done.panel_deleted is True
    assert deleted_panel_uuids == [f'panel-{STAND_TELEGRAM_ID}']

    after_stand = await _counts(session, stand.id)
    assert after_stand == {
        'subscriptions': 0,
        'checkouts': 0,
        'payments': 0,
        'transactions': 0,
        'snapshots': 0,
    }
    await session.refresh(stand)
    assert stand.balance_kopeks == 0
    assert stand.remnawave_uuid is None
    assert stand.has_had_paid_subscription is False
    assert stand.status == UserStatus.DELETED.value
    assert stand.account_erasure_requested_at is None
    # Тот же Телеграм, та же строка: `/start` заведёт его заново.
    assert stand.telegram_id == STAND_TELEGRAM_ID

    # Никто другой не задет — ни его данные, ни счётчик общего сервера.
    assert await _counts(session, outsider.id) == before_outsider
    await session.refresh(outsider)
    assert outsider.balance_kopeks == 50000
    assert outsider.status == UserStatus.ACTIVE.value
    await session.refresh(invited)
    assert invited.referred_by_id == stand.id
    squad_users_after = await session.scalar(select(func.sum(ServerSquad.current_users)))
    assert squad_users_after == squad_users_before - 1


async def test_reset_refuses_while_an_order_is_still_in_flight(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000, checkout_state='awaiting_funds')

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert plan.done is False
    assert 'в работе' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1
    await session.refresh(stand)
    assert stand.balance_kopeks == 1000
    assert stand.status == UserStatus.ACTIVE.value


async def test_reset_refuses_when_the_provider_has_not_answered_yet(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text("UPDATE platega_payments SET status = 'VERIFYING', is_paid = false WHERE user_id = :uid"),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert 'не досверен' in (plan.blocked_reason or '')
    assert '199.00' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_reset_refuses_a_staff_account_even_if_it_is_on_the_list(session, monkeypatch) -> None:
    """Забор №2. Ровно тот случай, где список сам себя отменить не может."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text(
            'INSERT INTO admin_roles (id, name, level, permissions, is_system, is_active) '
            'VALUES (1, :name, 50, :perms, false, true)'
        ),
        {'name': 'Модератор', 'perms': '{}'},
    )
    await session.execute(
        text('INSERT INTO user_roles (user_id, role_id, is_active) VALUES (:uid, 1, true)'),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert 'служебная роль' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_nothing_changes_when_the_panel_refuses_to_delete(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)

    async def _panel_says_no(user, panel_uuids):
        return False

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_says_no)
    squad_users_before = await session.scalar(select(func.sum(ServerSquad.current_users)))
    stand_id = stand.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is False
    assert plan.allowed is False
    assert 'Ничего не тронуто' in (plan.blocked_reason or '')
    assert (await _counts(session, stand_id))['subscriptions'] == 1
    stand = await session.get(User, stand_id)
    assert stand.balance_kopeks == 22450
    assert stand.status == UserStatus.ACTIVE.value
    assert await session.scalar(select(func.sum(ServerSquad.current_users))) == squad_users_before

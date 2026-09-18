"""Обнуление тестового аккаунта на НАСТОЯЩЕМ PostgreSQL.

Здесь проверяется то, чего подделанная сессия проверить не может: переживёт ли
удаление настоящие внешние ключи. Именно на них молча падает очистка при
`/start` — база запрещает удалить подписку, пока на ней висит снимок прав.

Запускается по требованию:
    TEST_ACCOUNT_RESET_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \
        tests/integration/test_test_account_reset_postgres.py -q
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    Base,
    CheckoutPaymentAttempt,
    GuestPurchase,
    LavaPayment,
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


async def _panel_ok(user, panel_uuids, **kwargs):
    """Панель в этих тестах не участвует: её путь проверяется отдельно."""
    return True


async def _confirmed_reset(db, user, admin_id=1, **kwargs):
    preview = await user_service.reset_test_account(db, user, admin_id, confirm=False)
    result = await user_service.reset_test_account(
        db, user, admin_id, confirm=True, preview_token=preview.preview_token
    )
    await db.refresh(user)
    return result


def _install_guards(connection):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = importlib.import_module('migrations.alembic.versions.0105_test_account_reset_fence')
    with Operations.context(MigrationContext.configure(connection)):
        migration.install_guards()


STAND_TELEGRAM_ID = 7749231125
OUTSIDER_TELEGRAM_ID = 999000111


@pytest_asyncio.fixture
async def session():
    url = make_url(DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or url.database != 'teplo_reset_test':
        raise RuntimeError('Destructive tests require the isolated local teplo_reset_test database')
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    async with engine.begin() as connection:
        # `drop_all` не умеет разложить цикл subscription_checkouts <-> transactions;
        # для одноразовой базы проще снести схему целиком.
        await connection.execute(text('DROP SCHEMA public CASCADE'))
        await connection.execute(text('CREATE SCHEMA public'))
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_install_guards)
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
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)
    outsider = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=50000)

    # Живой клиент, которого стенд когда-то пригласил: его строку не трогаем.
    invited = User(telegram_id=555444333, username='invited', referred_by_id=stand.id)
    session.add(invited)
    await session.flush()
    session.add(ReferralEarning(user_id=stand.id, referral_id=invited.id, amount_kopeks=5000, reason='signup'))
    await session.commit()

    deleted_panel_uuids: list[str] = []

    async def _fake_panel_delete(user, panel_uuids, **kwargs):
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
    done = await _confirmed_reset(session, stand)
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
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000, checkout_state='awaiting_funds')

    plan = await _confirmed_reset(session, stand)

    assert plan.allowed is False
    assert plan.done is False
    assert 'не закончен' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1
    await session.refresh(stand)
    assert stand.balance_kopeks == 1000
    assert stand.status == UserStatus.ACTIVE.value


async def test_reset_refuses_when_the_provider_has_not_answered_yet(session, monkeypatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text("UPDATE platega_payments SET status = 'VERIFYING', is_paid = false WHERE user_id = :uid"),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await _confirmed_reset(session, stand)

    assert plan.allowed is False
    assert 'не досверен' in (plan.blocked_reason or '')
    assert '199.00' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_reset_refuses_a_staff_account_even_if_it_is_on_the_list(session, monkeypatch) -> None:
    """Забор №2. Ровно тот случай, где список сам себя отменить не может."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
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

    plan = await _confirmed_reset(session, stand)

    assert plan.allowed is False
    assert 'служебная роль' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_nothing_changes_when_the_panel_refuses_to_delete(session, monkeypatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)

    async def _panel_says_no(user, panel_uuids, **kwargs):
        return False

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_says_no)
    squad_users_before = await session.scalar(select(func.sum(ServerSquad.current_users)))
    stand_id = stand.id

    plan = await _confirmed_reset(session, stand)

    assert plan.done is False
    assert plan.allowed is False
    assert 'ничего не тронуто' in (plan.blocked_reason or '').lower()
    assert (await _counts(session, stand_id))['subscriptions'] == 1
    stand = await session.get(User, stand_id)
    assert stand.balance_kopeks == 22450
    assert stand.status == UserStatus.ACTIVE.value
    assert await session.scalar(select(func.sum(ServerSquad.current_users))) == squad_users_before


async def test_a_successful_purchase_does_not_lock_the_button(session, monkeypatch) -> None:
    """Главная ловушка: `paid_processing` остаётся у успешной продажи навсегда.

    Если считать его «деньгами в пути», кнопка ломается ровно на том стенде,
    где владелец впервые довёл покупку до конца.
    """
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    await session.execute(
        text('UPDATE checkout_payment_attempts SET status = :st'),
        {'st': 'paid_processing'},
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await _confirmed_reset(session, stand)

    assert plan.blocked_reason is None
    assert plan.done is True
    assert (await _counts(session, stand.id))['subscriptions'] == 0


async def test_an_abandoned_invoice_does_not_lock_the_button(session, monkeypatch) -> None:
    """Открыл оплату и закрыл вкладку — счёт протух. Это не деньги в пути."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    await session.execute(text("UPDATE platega_payments SET status = 'EXPIRED', is_paid = false"))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await _confirmed_reset(session, stand)

    assert plan.blocked_reason is None
    assert plan.done is True


async def test_a_finished_closure_does_not_lock_the_button_forever(session, monkeypatch) -> None:
    """Строка заявки на закрытие не удаляется никогда — запирать по её
    наличию значило бы убить кнопку первым же нажатием «удалить»."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    session.add(AccountErasureRequest(user_id=stand.id, state='completed'))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await _confirmed_reset(session, stand)

    assert plan.done is True

    # А незакрытая — запирает.
    other = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=0)
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(OUTSIDER_TELEGRAM_ID))
    session.add(AccountErasureRequest(user_id=other.id, state='awaiting_manual_resolution'))
    await session.commit()

    blocked = await _confirmed_reset(session, other)
    assert blocked.done is False
    assert 'закрывается' in (blocked.blocked_reason or '')


async def test_a_paid_gift_of_a_real_buyer_survives(session, monkeypatch) -> None:
    """`SET NULL` на ссылке — указание схемы «строка переживает человека»."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    buyer = User(telegram_id=444333222, username='buyer')
    session.add(buyer)
    await session.flush()
    # Подарок купил ЖИВОЙ человек, активировал — стенд.
    session.add(
        GuestPurchase(
            buyer_user_id=buyer.id,
            user_id=stand.id,
            amount_kopeks=19900,
            status='paid',
            token=f'gift-{STAND_TELEGRAM_ID}',
            contact_type='telegram',
            contact_value=str(STAND_TELEGRAM_ID),
            period_days=30,
        )
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand_id = stand.id

    plan = await _confirmed_reset(session, stand)

    # Оплаченный, но ещё не доставленный подарок — деньги в пути: отказ.
    assert plan.done is False
    assert 'подарок' in (plan.blocked_reason or '')
    assert await session.scalar(select(func.count()).select_from(GuestPurchase)) == 1
    assert (await _counts(session, stand_id))['subscriptions'] == 1


async def test_confirmation_rechecks_the_preview_without_touching_panel(session, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    panel = AsyncMock(return_value=True)
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', panel)
    preview = await user_service.reset_test_account(session, stand, 1, confirm=False)
    stand.balance_kopeks += 1
    await session.commit()
    result = await user_service.reset_test_account(session, stand, 1, confirm=True, preview_token=preview.preview_token)
    assert not result.done and not result.allowed
    panel.assert_not_awaited()
    await session.refresh(stand)
    assert stand.balance_kopeks == 1001 and stand.test_reset_started_at is None


@pytest.mark.parametrize(
    'statement',
    [
        'UPDATE users SET balance_kopeks = 999 WHERE id = :id',
        "UPDATE subscriptions SET status = 'active' WHERE user_id = :id",
        'DELETE FROM subscriptions WHERE user_id = :id',
        "INSERT INTO transactions (user_id, type, amount_kopeks) VALUES (:id, 'deposit', 50)",
        "UPDATE checkout_payment_attempts SET status = 'pending' WHERE checkout_id IN (SELECT id FROM subscription_checkouts WHERE user_id = :id)",
        "UPDATE subscription_entitlement_snapshots SET provenance = 'late' WHERE subscription_id IN (SELECT id FROM subscriptions WHERE user_id = :id)",
    ],
)
async def test_failed_reset_fences_stale_writers_and_can_resume(session, monkeypatch, statement):
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import DBAPIError

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    outsider = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=5000)
    stand_id, outsider_id = stand.id, outsider.id
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', AsyncMock(return_value=False))
    result = await _confirmed_reset(session, stand)
    assert not result.done and result.reset_state == 'failed'
    assert stand.balance_kopeks == 1000
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as writer:
        with pytest.raises(DBAPIError, match='test_account_reset_in_progress'):
            await writer.execute(text(statement), {'id': stand_id})
            await writer.commit()
        await writer.rollback()
        await writer.execute(
            text('UPDATE users SET balance_kopeks = balance_kopeks + 1 WHERE id = :id'), {'id': outsider_id}
        )
        await writer.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    result = await _confirmed_reset(session, stand)
    assert result.done and stand.test_reset_state == 'ready'
    assert stand.test_reset_panel_uuids == [f'panel-{STAND_TELEGRAM_ID}']
    await session.refresh(outsider)
    assert outsider.balance_kopeks == 5001


async def test_two_reset_requests_have_one_remote_owner(session, monkeypatch):
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    stand_id = stand.id
    preview = await user_service.reset_test_account(session, stand, 1, confirm=False)
    await session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed_panel(user, panel_uuids, **kwargs):
        calls.append(user.id)
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return True

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', delayed_panel)
    maker = async_sessionmaker(session.bind, expire_on_commit=False)

    async def first_request():
        async with maker() as db:
            user = await db.get(User, stand_id)
            return await user_service.reset_test_account(db, user, 1, confirm=True, preview_token=preview.preview_token)

    first = asyncio.create_task(first_request())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with maker() as db:
            user = await db.get(User, stand_id)
            second = await asyncio.wait_for(
                user_service.reset_test_account(db, user, 1, confirm=True, preview_token=preview.preview_token), 2
            )
            assert not second.done and 'уже выполняется' in second.blocked_reason
    finally:
        release.set()
    assert (await first).done
    assert calls == [stand_id]


@pytest.mark.parametrize('mode', ['valid_multi', 'missing', 'foreign_owner', 'not_deleted', 'lost_response'])
async def test_panel_verified_delete_and_retry_contract(session, monkeypatch, mode):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.services.remnawave_service import RemnaWaveService

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    expected = f'panel-{STAND_TELEGRAM_ID}'
    records = (
        {}
        if mode == 'missing'
        else {
            expected: SimpleNamespace(
                uuid=expected, telegram_id=OUTSIDER_TELEGRAM_ID if mode == 'foreign_owner' else STAND_TELEGRAM_ID
            ),
            'second-test-identity': SimpleNamespace(uuid='second-test-identity', telegram_id=STAND_TELEGRAM_ID),
        }
    )
    deletes = []
    lost = False

    class Panel:
        async def get_user_by_telegram_id(self, telegram_id):
            return [item for item in records.values() if item.telegram_id == telegram_id]

        async def get_user_by_uuid(self, panel_uuid):
            return records.get(panel_uuid)

        async def delete_user(self, panel_uuid):
            nonlocal lost
            deletes.append(panel_uuid)
            if mode != 'not_deleted':
                records.pop(panel_uuid, None)
            if mode == 'lost_response' and not lost:
                lost = True
                raise TimeoutError('simulated accepted DELETE with lost response')
            return True

    @asynccontextmanager
    async def get_api(_self):
        yield Panel()

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', get_api)
    result = await _confirmed_reset(session, stand)
    if mode == 'foreign_owner':
        assert not result.done and deletes == []
        assert (await _counts(session, stand.id))['subscriptions'] == 1
    elif mode == 'not_deleted':
        assert not result.done and stand.test_reset_state == 'failed'
        assert (await _counts(session, stand.id))['subscriptions'] == 1
    else:
        if mode == 'lost_response':
            assert not result.done and stand.test_reset_state == 'failed'
            assert expected in stand.test_reset_panel_uuids
            result = await _confirmed_reset(session, stand)
        assert result.done and not records


@pytest.mark.parametrize(
    'event_name', ['user.disabled', 'user.modified', 'user.revoked', 'user.traffic_reset', 'user.deleted']
)
async def test_retired_webhook_cannot_touch_new_trial_even_after_unregister(session, monkeypatch, event_name):
    from unittest.mock import AsyncMock

    from app.services.remnawave_webhook_service import RemnaWaveWebhookService

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    assert (await _confirmed_reset(session, stand)).done
    stand.test_account_enabled = False
    stand.remnawave_uuid = 'new-identity'
    stand.status = 'active'
    subscription = Subscription(
        user_id=stand.id, is_trial=True, status='active', end_date=datetime.now(UTC) + timedelta(days=3)
    )
    session.add(subscription)
    await session.commit()
    service = RemnaWaveWebhookService(bot=AsyncMock())
    handler = AsyncMock()
    assert await service._process_user_event(
        session, event_name, {'telegramId': STAND_TELEGRAM_ID, 'uuid': f'panel-{STAND_TELEGRAM_ID}'}, handler
    )
    handler.assert_not_awaited()
    assert await service._process_user_event(
        session, event_name, {'telegramId': STAND_TELEGRAM_ID, 'uuid': 'new-identity'}, handler
    )
    handler.assert_awaited_once()


async def test_old_subscription_cannot_provision_after_new_generation(session, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.subscription_service import SubscriptionService

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    old_id = await session.scalar(select(Subscription.id).where(Subscription.user_id == stand.id))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    assert (await _confirmed_reset(session, stand)).done
    stand.remnawave_uuid = 'new-identity'
    session.add(Subscription(user_id=stand.id, is_trial=True, end_date=datetime.now(UTC) + timedelta(days=3)))
    await session.commit()
    operation = AsyncMock()
    result = await SubscriptionService().run_guarded_panel_write(
        session, user_id=stand.id, subscription_id=old_id, api=AsyncMock(), operation=operation
    )
    assert result is None
    operation.assert_not_awaited()


async def test_membership_is_explicit_reversible_and_non_destructive(session, monkeypatch):
    from fastapi import HTTPException

    from app.cabinet.routes import admin_users
    from app.cabinet.schemas.users import TestAccountMembershipRequest, TestAccountResetRequest

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    owner = User(telegram_id=111000222, username='owner')
    session.add(owner)
    stand = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=12345)
    owner_id, stand_id = owner.id, stand.id
    before = await _counts(session, stand_id)
    monkeypatch.setattr(admin_users, '_can_manage_test_accounts', lambda user: False)
    with pytest.raises(HTTPException) as denied:
        await admin_users.set_test_membership(
            stand_id,
            TestAccountMembershipRequest(enabled=True, telegram_id=OUTSIDER_TELEGRAM_ID),
            admin=owner,
            db=session,
        )
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as denied_reset:
        await admin_users.reset_test_account_route(stand_id, TestAccountResetRequest(), admin=owner, db=session)
    assert denied_reset.value.status_code == 403
    monkeypatch.setattr(admin_users, '_can_manage_test_accounts', lambda user: user.id == owner_id)
    with pytest.raises(HTTPException) as mismatch:
        await admin_users.set_test_membership(
            stand_id, TestAccountMembershipRequest(enabled=True, telegram_id=STAND_TELEGRAM_ID), admin=owner, db=session
        )
    assert mismatch.value.status_code == 409
    result = await admin_users.set_test_membership(
        stand_id, TestAccountMembershipRequest(enabled=True, telegram_id=OUTSIDER_TELEGRAM_ID), admin=owner, db=session
    )
    assert result['is_test_account'] and user_service.is_test_account(stand)
    assert stand.test_reset_state == 'idle' and stand.test_reset_started_at is None
    assert await _counts(session, stand_id) == before and stand.balance_kopeks == 12345
    # Explicit removal wins even if the legacy environment later contains it.
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(OUTSIDER_TELEGRAM_ID))
    await admin_users.set_test_membership(
        stand_id, TestAccountMembershipRequest(enabled=False, telegram_id=OUTSIDER_TELEGRAM_ID), admin=owner, db=session
    )
    assert not user_service.is_test_account(stand)
    assert await _counts(session, stand_id) == before and stand.balance_kopeks == 12345


async def test_migration_upgrade_is_bounded_and_downgrade_preserves_history(session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    migration = importlib.import_module('migrations.alembic.versions.0105_test_account_reset_fence')

    def invoke(connection, action):
        with Operations.context(MigrationContext.configure(connection)):
            action()

    connection = await session.connection()
    await connection.run_sync(invoke, migration.downgrade)
    await session.commit()
    async with session.bind.connect() as writer:
        await writer.execute(text('LOCK TABLE subscriptions IN ROW EXCLUSIVE MODE'))
        connection = await session.connection()
        with pytest.raises(DBAPIError, match='lock timeout'):
            await asyncio.wait_for(connection.run_sync(invoke, migration.upgrade), 5)
        await session.rollback()
        await writer.rollback()
    connection = await session.connection()
    await connection.run_sync(invoke, migration.upgrade)
    await session.commit()
    stand = User(telegram_id=STAND_TELEGRAM_ID, test_account_enabled=False)
    session.add(stand)
    await session.commit()
    connection = await session.connection()
    with pytest.raises(RuntimeError, match='retain additive schema'):
        await connection.run_sync(invoke, migration.downgrade)
    await session.rollback()


async def test_three_reset_and_fresh_trial_cycles(session, monkeypatch):
    from app.database.crud.subscription import create_trial_subscription
    from app.services.user_revival_service import revive_deleted_user

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    seen = set()
    for cycle in range(3):
        assert (await _confirmed_reset(session, stand)).done
        await session.refresh(stand, ['subscriptions'])
        assert not stand.is_trial_already_used()
        await revive_deleted_user(session, stand, source='test_reset_test')
        await session.commit()
        trial = await create_trial_subscription(session, stand.id, duration_days=3, traffic_limit_gb=5, device_limit=1)
        assert trial.id not in seen
        seen.add(trial.id)
        await session.refresh(stand, ['subscriptions'])
        assert stand.is_trial_already_used()
        assert trial.is_trial and trial.device_limit == 1 and trial.traffic_limit_gb == 5


async def test_referrers_earning_does_not_deadlock_the_reset(session, monkeypatch) -> None:
    """Строку заработка реферера мы храним — а она ссылается на транзакцию стенда.

    База запрещает удалить транзакцию, пока ссылка жива (`NO ACTION`). Без
    обнуления ссылки обнуление падало бы КАЖДЫЙ раз, уже после удаления
    пользователя из панели, — то есть стенд оставался бы заперт навсегда.
    """
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    referrer = User(telegram_id=111222333, username='referrer')
    session.add(referrer)
    await session.flush()
    stand_transaction = await session.scalar(select(Transaction.id).where(Transaction.user_id == stand.id))
    session.add(
        ReferralEarning(
            user_id=referrer.id,
            referral_id=stand.id,
            amount_kopeks=15000,
            reason='referral_first_topup',
            referral_transaction_id=stand_transaction,
        )
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    referrer_id = referrer.id

    plan = await _confirmed_reset(session, stand)

    assert plan.done is True, plan.blocked_reason
    # Заработок реферера цел, только указатель на снесённую транзакцию снят.
    earning = (
        await session.execute(select(ReferralEarning).where(ReferralEarning.user_id == referrer_id))
    ).scalar_one()
    assert earning.amount_kopeks == 15000
    assert earning.referral_transaction_id is None


async def test_a_sleeping_payment_gateway_row_locks_the_button(session, monkeypatch) -> None:
    """Кассы с `SET NULL` не сносятся, но держат транзакции ссылкой `NO ACTION`.

    Значит про них нельзя ни судить, ни молчать: строка есть — отказ.
    """
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    session.add(LavaPayment(user_id=stand.id, order_id='lava-1', amount_kopeks=19900, status='pending'))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await _confirmed_reset(session, stand)

    assert plan.done is False
    assert 'Lava' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_an_admin_in_the_list_is_still_refused(session, monkeypatch) -> None:
    """Забор №2 существует ровно для ошибки, которую список отменить не может.

    Мутационный прогон показал, что эта строка не была защищена ничем: убери
    её — и админский аккаунт, случайно вписанный в список, обнулился бы молча.
    """
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)

    plan = await _confirmed_reset(session, stand)

    assert plan.done is False
    assert 'админский' in (plan.blocked_reason or '').lower()
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_a_support_moderator_is_still_refused(session, monkeypatch) -> None:
    """Третий реестр служебных людей живёт в файле, а не в базе."""
    from app.services.support_settings_service import SupportSettingsService

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(SupportSettingsService, 'is_moderator', classmethod(lambda cls, tid: True))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)

    plan = await _confirmed_reset(session, stand)

    assert plan.done is False
    assert 'модератор' in (plan.blocked_reason or '').lower()


async def test_a_staff_role_is_refused_for_the_right_reason(session, monkeypatch) -> None:
    """Панель подменена намеренно: иначе тест краснел бы из-за неё, а не из-за роли."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
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

    plan = await _confirmed_reset(session, stand)

    assert 'служебная роль' in (plan.blocked_reason or '')


async def test_a_used_promocode_gets_its_slot_back(session, monkeypatch) -> None:
    """Иначе у кода с лимитом 1 после первой проверки не осталось бы мест."""
    from app.database.models import PromoCode, PromoCodeUse

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    promocode = PromoCode(code='TEST1', type='balance', max_uses=1, current_uses=1)
    session.add(promocode)
    await session.flush()
    session.add(PromoCodeUse(promocode_id=promocode.id, user_id=stand.id))
    await session.commit()
    promocode_id = promocode.id

    plan = await _confirmed_reset(session, stand)

    assert plan.done is True, plan.blocked_reason
    assert await session.scalar(select(PromoCode.current_uses).where(PromoCode.id == promocode_id)) == 0


async def test_prices_look_like_a_newcomers_again(session, monkeypatch) -> None:
    """Стенд со скидочной промо-группой показал бы не те цены, что новичок.

    Ровно это расхождение и сорвало приёмку, из-за которой инструмент затеян.
    """
    from app.database.models import PromoGroup

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    default_group = PromoGroup(name='Базовая', is_default=True)
    discount_group = PromoGroup(name='Скидка 50', is_default=False)
    session.add_all([default_group, discount_group])
    await session.flush()
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    stand.promo_group_id = discount_group.id
    stand.has_made_first_topup = True
    stand.auto_promo_group_assigned = True
    await session.commit()
    default_id, stand_id = default_group.id, stand.id

    plan = await _confirmed_reset(session, stand)

    assert plan.done is True, plan.blocked_reason
    refreshed = await session.get(User, stand_id)
    assert refreshed.promo_group_id == default_id
    assert refreshed.has_made_first_topup is False
    assert refreshed.auto_promo_group_assigned is False


async def test_a_restricted_stand_comes_back_able_to_buy(session, monkeypatch) -> None:
    """Проверять «что видит ограниченный клиент» на стенде — обычное дело.

    Запреты покупки и пополнения живут на строке пользователя и переживают
    удаление всего остального. Не сняв их, мы вернули бы «нового клиента»,
    который не умеет покупать.
    """
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    stand.restriction_topup = True
    stand.restriction_subscription = True
    stand.restriction_reason = 'проверяли экран ограничений'
    stand.promo_offer_discount_percent = 30
    await session.commit()
    stand_id = stand.id

    plan = await _confirmed_reset(session, stand)

    assert plan.done is True, plan.blocked_reason
    refreshed = await session.get(User, stand_id)
    assert refreshed.restriction_topup is False
    assert refreshed.restriction_subscription is False
    assert refreshed.restriction_reason is None
    assert refreshed.promo_offer_discount_percent == 0


async def test_the_plan_names_the_support_conversation_before_it_disappears(session, monkeypatch) -> None:
    """Обращение сносится вместе с перепиской ВНУТРИ него — включая ответы
    менеджера. Это исчезало незаметно: в плане обращений не было вовсе."""
    from app.database.models import Ticket, TicketMessage

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    manager = User(telegram_id=101010101, username='manager')
    session.add(manager)
    await session.flush()
    ticket = Ticket(user_id=stand.id, title='вопрос', status='open')
    session.add(ticket)
    await session.flush()
    # Ответ менеджера лежит ВНУТРИ обращения стенда и уедет по каскаду.
    session.add_all(
        [
            TicketMessage(ticket_id=ticket.id, user_id=stand.id, message_text='не работает'),
            TicketMessage(ticket_id=ticket.id, user_id=manager.id, message_text='смотрим', is_from_admin=True),
        ]
    )
    await session.commit()

    preview = await user_service.reset_test_account(session, stand, admin_id=1, confirm=False)
    assert preview.tickets == 1, 'обращения обязаны быть видны ДО нажатия'

    done = await _confirmed_reset(session, stand)
    assert done.done is True, done.blocked_reason
    assert await session.scalar(select(func.count()).select_from(TicketMessage)) == 0


async def test_an_unfinished_gift_locks_but_a_finished_one_does_not(session, monkeypatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    gift = GuestPurchase(
        user_id=stand.id,
        amount_kopeks=19900,
        status='pending',
        token='gift-pending',
        contact_type='telegram',
        contact_value=str(STAND_TELEGRAM_ID),
        period_days=30,
    )
    session.add(gift)
    await session.commit()

    blocked = await _confirmed_reset(session, stand)
    assert blocked.done is False
    assert 'подарок' in (blocked.blocked_reason or '')

    # Завершённый — просто история, схема велит строку сохранить.
    gift.status = 'delivered'
    await session.commit()

    done = await _confirmed_reset(session, stand)
    assert done.done is True, done.blocked_reason
    assert await session.scalar(select(func.count()).select_from(GuestPurchase)) == 1

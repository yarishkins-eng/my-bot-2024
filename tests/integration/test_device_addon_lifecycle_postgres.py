"""Real FK/lifecycle regression on a disposable local PostgreSQL schema."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import (
    Base,
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PlategaPayment,
    Subscription,
    Transaction,
    User,
)
from app.services.account_erasure_service import ERASURE_AWAITING_MANUAL, ERASURE_READY, _lock_context, _target_state
from app.services.account_merge_service import _guard_device_addon_merge
from app.services.device_addon_service import (
    DeviceAddonError,
    calculate_device_addon,
    create_intent,
    purchase_intent,
    quote_for_calculation,
)
from app.services.user_service import UserService, _test_reset_blocked_reason, _test_reset_delete_plan


DATABASE_URL = os.getenv('DEVICE_ADDON_TEST_DATABASE_URL')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE_URL, reason='Requires isolated addon PostgreSQL')]


@pytest_asyncio.fixture
async def sessions():
    if not DATABASE_URL:
        pytest.skip('Requires isolated addon PostgreSQL')
    url = make_url(DATABASE_URL)
    if url.host not in {'localhost', '127.0.0.1'} or not url.database.startswith('teplo_device_test_'):
        raise RuntimeError('Only a disposable local teplo_device_test_* database is allowed')
    schema = f'addon_lifecycle_{uuid.uuid4().hex}'
    bootstrap = create_async_engine(DATABASE_URL)
    async with bootstrap.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(DATABASE_URL, connect_args={'server_settings': {'search_path': schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await bootstrap.dispose()


async def seed(db, *, with_attempt=True, status='terminal', purchased=False):
    user = User(
        telegram_id=7788012249,
        balance_kopeks=0,
        status='active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        remnawave_uuid=str(uuid.uuid4()),
        test_account_enabled=True,
    )
    db.add(user)
    await db.flush()
    sub = Subscription(
        user_id=user.id,
        end_date=datetime.now(UTC) - timedelta(days=1),
        status='expired',
        is_trial=False,
        device_limit=2,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(sub)
    await db.flush()
    intent = DeviceAddonIntent(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        subscription_id=sub.id,
        target_subscription_id=sub.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash='a' * 64,
        devices_to_add=1,
        original_device_limit=2,
        end_date=sub.end_date,
        device_addon_generation=0,
        days_left=1,
        monthly_price_kopeks=5000,
        base_price_kopeks=166,
        quoted_price_kopeks=166,
        purchase_state='purchased' if purchased else 'draft',
        receipt_json={'devices_added': 1} if purchased else None,
    )
    db.add(intent)
    await db.flush()
    payment = attempt = None
    if with_attempt:
        correlation = str(uuid.uuid4())
        payment = PlategaPayment(
            user_id=user.id,
            correlation_id=correlation,
            amount_kopeks=166,
            payment_method_code=2,
            status='CANCELED' if status == 'terminal' else 'CONFIRMED',
            is_paid=status == 'paid',
            platega_transaction_id=str(uuid.uuid4()),
        )
        db.add(payment)
        await db.flush()
        attempt = DeviceAddonTopupAttempt(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            intent_id=intent.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='b' * 64,
            method_key='platega_sbp',
            provider_method_code=2,
            expected_amount_kopeks=166,
            requested_amount_kopeks=166,
            status=status,
            platega_payment_id=payment.id,
            correlation_id=correlation,
            provider_payment_id=payment.platega_transaction_id,
        )
        db.add(attempt)
    await db.commit()
    return user, sub, intent, payment, attempt


async def seed_active_addon_target(db, *, balance_kopeks: int = 0):
    user = User(
        telegram_id=7788020000 + balance_kopeks,
        balance_kopeks=balance_kopeks,
        status='active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        remnawave_uuid=str(uuid.uuid4()),
    )
    db.add(user)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        status='active',
        is_trial=False,
        device_limit=1,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(subscription)
    await db.commit()
    return user, subscription


@pytest.mark.parametrize('target', ['payment', 'subscription', 'user'])
async def test_money_graph_cannot_be_physically_deleted(sessions, target):
    async with sessions() as db:
        user, sub, intent, payment, attempt = await seed(db)
        entity = {'payment': payment, 'subscription': sub, 'user': user}[target]
        target_id, model = entity.id, type(entity)
        attempt_id = attempt.id
        if target == 'subscription':
            await db.execute(delete(model).where(model.id == target_id))
            await db.commit()
            retained = (
                await db.execute(
                    select(DeviceAddonIntent)
                    .where(DeviceAddonIntent.id == intent.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            assert retained is not None
            assert retained.subscription_id is None
            assert retained.target_subscription_id == target_id
            assert await db.get(DeviceAddonTopupAttempt, attempt_id) is not None
            return
        with pytest.raises(IntegrityError):
            await db.execute(delete(model).where(model.id == target_id))
            await db.commit()
        await db.rollback()
        assert await db.get(DeviceAddonTopupAttempt, attempt_id) is not None


async def test_terminal_attempt_blocks_reset_and_merge_but_remains_canonical_history(sessions):
    async with sessions() as db:
        user, _, intent, _, attempt = await seed(db)
        assert 'счёт докупки' in await _test_reset_blocked_reason(db, user)
        with pytest.raises(ValueError, match='финансовая сверка'):
            await _guard_device_addon_merge(db, [user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None
        assert await db.get(DeviceAddonTopupAttempt, attempt.id) is not None
        assert await UserService._get_financial_history_kind(db, user.id) == (True, False)


async def test_paid_free_purchase_also_preserves_its_pending_entitlement(sessions):
    async with sessions() as db:
        user, _, _, _, _ = await seed(db, with_attempt=False, purchased=True)
        assert 'выполненная докупка' in await _test_reset_blocked_reason(db, user)
        with pytest.raises(ValueError, match='проверка её выдачи'):
            await _guard_device_addon_merge(db, [user.id])


async def test_clean_draft_can_be_revoked_before_merge(sessions):
    async with sessions() as db:
        user, sub, intent, _, _ = await seed(db, with_attempt=False)
        intent_id = intent.id
        await _guard_device_addon_merge(db, [user.id])
        await db.commit()
        assert await db.get(DeviceAddonIntent, intent_id) is None
        assert await db.get(Subscription, sub.id) is not None


async def test_clean_draft_is_in_reset_child_first_delete_plan(sessions):
    async with sessions() as db:
        user, sub, intent, _, _ = await seed(db, with_attempt=False)
        user_id, sub_id = user.id, sub.id
        plan = _test_reset_delete_plan({'users.id': [user_id], 'subscriptions.id': [sub_id]})
        names = [table.name for table, _ in plan]
        assert names.index('device_addon_intents') < names.index('subscriptions')
        for table, whereclause in plan:
            await db.execute(delete(table).where(whereclause))
        await db.commit()
        assert await db.scalar(select(DeviceAddonIntent.id)) is None
        assert await db.get(User, user_id) is not None


@pytest.mark.parametrize('status, expected', [('terminal', ERASURE_READY), ('paid', ERASURE_AWAITING_MANUAL)])
async def test_erasure_locks_and_classifies_addon_graph(sessions, status, expected):
    async with sessions() as db:
        user, _, intent, _, attempt = await seed(db, status=status)
        context = await _lock_context(db, user_id=user.id)
        assert [row.id for row in context.addon_intents] == [intent.id]
        assert [row.id for row in context.addon_attempts] == [attempt.id]
        assert _target_state(context)[0] == expected


async def test_expired_subscription_cannot_be_quoted(sessions):
    async with sessions() as db:
        user, subscription, _, _, _ = await seed(db, with_attempt=False)
        # A stale writer can leave an apparently-active subscription with a
        # past end timestamp.  The calculator must reject that real time
        # boundary instead of pricing it as one remaining day.
        subscription.status = 'active'
        await db.commit()
        with pytest.raises(DeviceAddonError, match='Срок подписки закончился') as error:
            await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=1)
        assert error.value.code == 'subscription_expired'


async def test_missing_panel_identity_cannot_be_quoted_or_debited(sessions):
    async with sessions() as db:
        user, subscription = await seed_active_addon_target(db)
        user.remnawave_uuid = None
        await db.commit()
        with pytest.raises(DeviceAddonError) as error:
            await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=1)
        assert error.value.code == 'panel_identity_unavailable'


async def test_intent_replay_survives_quote_expiry_and_later_subscription_change(sessions):
    async with sessions() as db:
        user, subscription = await seed_active_addon_target(db)
        calculation = await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=1)
        quote = quote_for_calculation(calculation, user_id=user.id)
        intent = await create_intent(
            db,
            user=user,
            quote_token=quote['quote_token'],
            idempotency_key='retry-after-later-change',
        )
        subscription.end_date = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        replay = await create_intent(
            db,
            user=user,
            quote_token=quote['quote_token'],
            idempotency_key='retry-after-later-change',
        )
        assert replay.id == intent.id


async def test_purchase_is_atomic_and_concurrent_replays_create_one_debit(sessions):
    async with sessions() as db:
        user, subscription = await seed_active_addon_target(db, balance_kopeks=10_000)
        calculation = await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=1)
        assert calculation.price_kopeks > 0
        quote = quote_for_calculation(calculation, user_id=user.id)
        intent = await create_intent(
            db,
            user=user,
            quote_token=quote['quote_token'],
            idempotency_key='purchase-concurrency-intent',
        )
        public_id, user_id, intent_id, quote_token = intent.public_id, user.id, intent.id, quote['quote_token']

    async def purchase_once():
        async with sessions() as worker_db:
            worker_user = await worker_db.get(User, user_id)
            result = await purchase_intent(
                worker_db,
                user=worker_user,
                public_id=public_id,
                quote_token=quote_token,
            )
            return result.id

    first, second = await asyncio.gather(purchase_once(), purchase_once())
    assert first == second == intent_id

    async with sessions() as verify_db:
        stored_intent = await verify_db.get(DeviceAddonIntent, intent_id)
        stored_user = await verify_db.get(User, user_id)
        stored_subscription = await verify_db.get(Subscription, subscription.id)
        assert stored_intent.purchase_state == 'purchased'
        assert stored_intent.transaction_id is not None
        assert stored_subscription.device_limit == 2
        assert stored_user.balance_kopeks == 10_000 - calculation.price_kopeks
        debit_count = await verify_db.scalar(
            select(func.count(Transaction.id)).where(Transaction.id == stored_intent.transaction_id)
        )
        assert debit_count == 1

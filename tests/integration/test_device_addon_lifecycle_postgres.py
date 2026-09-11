"""Real FK/lifecycle regression on a disposable local PostgreSQL schema."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    Base,
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PlategaPayment,
    Subscription,
    Transaction,
    User,
)
from app.services import account_merge_service, account_test_reset_service, user_service
from app.services.account_erasure_service import (
    ERASURE_AWAITING_MANUAL,
    ERASURE_AWAITING_RECONCILIATION,
    ERASURE_READY,
    _lock_context,
    _target_state,
    _target_state_after_financial_resolution,
    resolve_financial_account_erasure,
)
from app.services.account_merge_service import _guard_device_addon_merge, execute_merge
from app.services.device_addon_payment_service import (
    handle_device_addon_platega_callback,
    reconcile_device_addon_payment,
)
from app.services.device_addon_service import (
    DeviceAddonError,
    calculate_device_addon,
    create_intent,
    purchase_intent,
    quote_for_calculation,
)
from app.services.user_service import UserService, _test_reset_blocked_reason, _test_reset_delete_plan
from tests.integration.test_device_addon_migration_postgres import run_migration


DATABASE_URL = os.getenv('DEVICE_ADDON_TEST_DATABASE_URL')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE_URL, reason='Requires isolated addon PostgreSQL')]


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    if not DATABASE_URL:
        pytest.skip('Requires isolated addon PostgreSQL')
    monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', True)
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
            # Install actual production safety fences, then execute the new
            # migration. Base.metadata alone cannot prove trigger behavior.
            await connection.execute(text('DROP TABLE device_addon_topup_attempts'))
            await connection.execute(text('DROP TABLE device_addon_intents'))
            await connection.execute(text('ALTER TABLE users DROP COLUMN device_addon_generation'))
            await connection.run_sync(run_migration, '0098_account_erasure_financial_tombstone', 'upgrade')
            await connection.run_sync(run_migration, '0105_test_account_reset_fence', 'install_guards')
            await connection.run_sync(run_migration, '0106_device_addon_intents', 'upgrade')
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await bootstrap.dispose()


async def seed(
    db,
    *,
    with_attempt=True,
    status='terminal',
    purchased=False,
    fulfillment_state='pending',
    provider_payment_id=True,
    quoted_price_kopeks=166,
    referral_status='done',
    event_status='done',
):
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
        quoted_price_kopeks=quoted_price_kopeks,
        purchase_state='purchased' if purchased else 'draft',
        receipt_json={'devices_added': 1} if purchased else None,
        fulfillment_state=fulfillment_state,
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
            provider_payment_id=payment.platega_transaction_id if provider_payment_id else None,
            referral_status=referral_status,
            event_status=event_status,
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


@pytest.mark.parametrize('attempt_status', ['prepared', 'dispatching', 'creation_unknown', 'pending', 'reconciling'])
async def test_in_flight_attempt_blocks_reset_and_merge(sessions, attempt_status):
    async with sessions() as db:
        user, _, intent, _, attempt = await seed(db, status=attempt_status)
        assert 'Счёт докупки устройств ещё в работе' in await _test_reset_blocked_reason(db, user)
        with pytest.raises(ValueError, match='Счёт докупки устройств ещё в работе'):
            await _guard_device_addon_merge(db, [user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None
        assert await db.get(DeviceAddonTopupAttempt, attempt.id) is not None


async def test_operator_review_blocks_only_after_provider_invoice_is_known(sessions):
    async with sessions() as db:
        blocked_user, _, intent, payment, attempt = await seed(db, status='operator_review')
        assert 'Счёт докупки устройств ещё в работе' in await _test_reset_blocked_reason(db, blocked_user)
        with pytest.raises(ValueError, match='Счёт докупки устройств ещё в работе'):
            await _guard_device_addon_merge(db, [blocked_user.id])

        attempt.provider_payment_id = None
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()
        assert await _test_reset_blocked_reason(db, blocked_user) is None
        await _guard_device_addon_merge(db, [blocked_user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None
        assert await db.get(DeviceAddonTopupAttempt, attempt.id) is not None


@pytest.mark.parametrize('attempt_status', ['terminal', 'paid'])
async def test_settled_attempt_allows_account_change_and_retains_history(sessions, attempt_status):
    async with sessions() as db:
        user, _, intent, payment, attempt = await seed(db, status=attempt_status)
        if attempt_status == 'paid':
            payment.status = 'OPERATOR_REVIEW'
            await db.commit()
        assert await _test_reset_blocked_reason(db, user) is None
        await _guard_device_addon_merge(db, [user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None
        assert await db.get(DeviceAddonTopupAttempt, attempt.id) is not None
        assert await UserService._get_financial_history_kind(db, user.id) == (True, False)


@pytest.mark.parametrize(
    'referral_status,event_status',
    [
        ('pending', 'done'),
        ('processing', 'done'),
        ('operator_review', 'done'),
        ('unknown_future_state', 'done'),
        ('done', 'pending'),
        ('done', 'processing'),
        ('done', 'unknown_future_state'),
    ],
)
async def test_paid_attempt_blocks_account_change_until_durable_effects_finish(
    sessions,
    referral_status,
    event_status,
):
    async with sessions() as db:
        user, _, intent, _, attempt = await seed(
            db,
            status='paid',
            referral_status=referral_status,
            event_status=event_status,
        )
        assert 'обязательные действия' in await _test_reset_blocked_reason(db, user)
        with pytest.raises(ValueError, match='обязательные действия'):
            await _guard_device_addon_merge(db, [user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None
        assert await db.get(DeviceAddonTopupAttempt, attempt.id) is not None


async def test_manually_resolved_paid_referral_allows_account_change(sessions):
    async with sessions() as db:
        user, _, _, _, _ = await seed(
            db,
            status='paid',
            referral_status='resolved_manually',
            event_status='done',
        )
        assert await _test_reset_blocked_reason(db, user) is None
        await _guard_device_addon_merge(db, [user.id])


async def test_paid_effect_guard_breaks_transaction_user_lock_cycle(sessions, monkeypatch):
    async with sessions() as seed_db:
        primary = User(
            telegram_id=7788012260,
            balance_kopeks=0,
            status='active',
            language='ru',
            referral_code=uuid.uuid4().hex[:12],
        )
        seed_db.add(primary)
        await seed_db.commit()
        secondary, _, _, payment, attempt = await seed(
            seed_db,
            status='paid',
            referral_status='pending',
            event_status='done',
        )
        deposit = Transaction(
            user_id=secondary.id,
            type='deposit',
            amount_kopeks=166,
            is_completed=True,
        )
        seed_db.add(deposit)
        await seed_db.flush()
        payment.transaction_id = deposit.id
        attempt.deposit_transaction_id = deposit.id
        await seed_db.commit()
        primary_id = primary.id
        secondary_id = secondary.id
        deposit_id = deposit.id

    merge_reached_guard = asyncio.Event()
    effects_started_user_lock = asyncio.Event()
    real_guard = account_merge_service._guard_device_addon_merge

    async def coordinated_guard(db, user_ids):
        merge_reached_guard.set()
        await effects_started_user_lock.wait()
        await real_guard(db, user_ids)

    monkeypatch.setattr(account_merge_service, '_guard_device_addon_merge', coordinated_guard)

    async with sessions() as effects_db, sessions() as merge_db:
        await effects_db.execute(select(Transaction).where(Transaction.id == deposit_id).with_for_update())

        async def continue_paid_effect_lock_order():
            await merge_reached_guard.wait()
            effects_started_user_lock.set()
            await effects_db.execute(select(User).where(User.id == secondary_id).with_for_update())

        async def attempt_merge():
            with pytest.raises(ValueError, match='обязательные действия'):
                await execute_merge(
                    merge_db,
                    primary_id,
                    secondary_id,
                    deferred_remnawave_deletions=[],
                )
            await merge_db.rollback()

        await asyncio.wait_for(
            asyncio.gather(attempt_merge(), continue_paid_effect_lock_order()),
            timeout=5,
        )
        await effects_db.rollback()


async def test_reset_enters_platega_graph_before_user_lock(sessions, monkeypatch):
    async with sessions() as seed_db:
        user, _, _, payment, attempt = await seed(seed_db, status='pending')
        user_id = user.id
        payment_id = payment.id
        provider_payment_id = str(attempt.provider_payment_id)
        correlation_id = attempt.correlation_id

    reset_reached_payment_lock = asyncio.Event()
    allow_reset_payment_lock = asyncio.Event()
    real_payment_lock = account_test_reset_service.lock_reset_platega_rows

    async def coordinated_payment_lock(db, locked_user_id):
        reset_reached_payment_lock.set()
        await allow_reset_payment_lock.wait()
        await real_payment_lock(db, locked_user_id)

    monkeypatch.setattr(account_test_reset_service, 'lock_reset_platega_rows', coordinated_payment_lock)

    async with sessions() as callback_db, sessions() as reset_db:
        locked_payment = (
            await callback_db.execute(select(PlategaPayment).where(PlategaPayment.id == payment_id).with_for_update())
        ).scalar_one()

        async def attempt_reset():
            current = await reset_db.get(User, user_id)
            result = await user_service.reset_test_account(
                reset_db,
                current,
                admin_id=1,
                confirm=True,
                preview_token='blocked-before-preview-check',
            )
            assert 'Счёт докупки устройств ещё в работе' in (result.blocked_reason or '')

        reset_task = asyncio.create_task(attempt_reset())
        await asyncio.wait_for(reset_reached_payment_lock.wait(), 5)
        try:
            assert await asyncio.wait_for(
                handle_device_addon_platega_callback(
                    callback_db,
                    payment=locked_payment,
                    payload={
                        'id': provider_payment_id,
                        'payload': f'platega:{correlation_id}',
                        'status': 'PENDING',
                        'amount': '1.66',
                        'currency': 'RUB',
                        'paymentMethod': 2,
                    },
                ),
                5,
            )
        finally:
            allow_reset_payment_lock.set()
        await asyncio.wait_for(reset_task, 5)


async def test_reset_checks_paid_effects_before_transaction_locks(sessions, monkeypatch):
    async with sessions() as seed_db:
        user, _, _, payment, attempt = await seed(
            seed_db,
            status='paid',
            referral_status='processing',
            event_status='done',
        )
        deposit = Transaction(
            user_id=user.id,
            type='deposit',
            amount_kopeks=166,
            is_completed=True,
        )
        seed_db.add(deposit)
        await seed_db.flush()
        payment.transaction_id = deposit.id
        attempt.deposit_transaction_id = deposit.id
        await seed_db.commit()
        user_id = user.id
        deposit_id = deposit.id

    reset_reached_effect_guard = asyncio.Event()
    effect_started_user_lock = asyncio.Event()
    allow_effect_guard = asyncio.Event()
    real_blocked_reason = user_service._test_reset_blocked_reason

    async def coordinated_blocked_reason(db, current):
        reset_reached_effect_guard.set()
        await allow_effect_guard.wait()
        return await real_blocked_reason(db, current)

    monkeypatch.setattr(user_service, '_test_reset_blocked_reason', coordinated_blocked_reason)

    async with sessions() as effects_db, sessions() as reset_db:
        await effects_db.execute(select(Transaction).where(Transaction.id == deposit_id).with_for_update())

        async def continue_effect_lock_order():
            effect_started_user_lock.set()
            await effects_db.execute(select(User).where(User.id == user_id).with_for_update())
            await effects_db.rollback()

        async def attempt_reset():
            current = await reset_db.get(User, user_id)
            result = await user_service.reset_test_account(
                reset_db,
                current,
                admin_id=1,
                confirm=True,
                preview_token='blocked-before-preview-check',
            )
            assert 'обязательные действия' in (result.blocked_reason or '')

        reset_task = asyncio.create_task(attempt_reset())
        await asyncio.wait_for(reset_reached_effect_guard.wait(), 5)
        effect_task = asyncio.create_task(continue_effect_lock_order())
        await asyncio.wait_for(effect_started_user_lock.wait(), 5)
        allow_effect_guard.set()
        await asyncio.wait_for(asyncio.gather(reset_task, effect_task), 5)


async def test_purchased_pending_entitlement_blocks_account_change(sessions):
    async with sessions() as db:
        user, _, _, _, _ = await seed(db, with_attempt=False, purchased=True)
        assert 'Выдача докупленных устройств ещё в работе' in await _test_reset_blocked_reason(db, user)
        with pytest.raises(ValueError, match='Выдача докупленных устройств ещё в работе'):
            await _guard_device_addon_merge(db, [user.id])


@pytest.mark.parametrize('fulfillment_state', ['ready', 'needs_attention'])
async def test_completed_or_held_entitlement_allows_account_change(sessions, fulfillment_state):
    async with sessions() as db:
        user, _, intent, _, _ = await seed(
            db,
            with_attempt=False,
            purchased=True,
            fulfillment_state=fulfillment_state,
            quoted_price_kopeks=0,
        )
        assert await _test_reset_blocked_reason(db, user) is None
        await _guard_device_addon_merge(db, [user.id])
        assert await db.get(DeviceAddonIntent, intent.id) is not None


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


async def test_paid_completed_graph_is_in_reset_child_first_delete_plan(sessions):
    async with sessions() as db:
        user, sub, intent, payment, attempt = await seed(
            db,
            status='paid',
            purchased=True,
            fulfillment_state='ready',
        )
        deposit = Transaction(user_id=user.id, type='deposit', amount_kopeks=166, is_completed=True)
        debit = Transaction(user_id=user.id, type='subscription_payment', amount_kopeks=166, is_completed=True)
        db.add_all([deposit, debit])
        await db.flush()
        payment.transaction_id = deposit.id
        attempt.deposit_transaction_id = deposit.id
        intent.transaction_id = debit.id
        await db.commit()

        assert await _test_reset_blocked_reason(db, user) is None
        plan = _test_reset_delete_plan({'users.id': [user.id], 'subscriptions.id': [sub.id]})
        names = [table.name for table, _ in plan]
        assert names.index('device_addon_topup_attempts') < names.index('device_addon_intents')
        assert names.index('device_addon_intents') < names.index('platega_payments')
        assert names.index('device_addon_intents') < names.index('transactions')

        for table, whereclause in plan:
            await db.execute(delete(table).where(whereclause))
        await db.commit()
        assert (
            await db.scalar(select(DeviceAddonTopupAttempt.id).where(DeviceAddonTopupAttempt.id == attempt.id)) is None
        )
        assert await db.scalar(select(DeviceAddonIntent.id).where(DeviceAddonIntent.id == intent.id)) is None
        assert await db.scalar(select(PlategaPayment.id).where(PlategaPayment.id == payment.id)) is None
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 0


async def test_merge_transfers_terminal_graph_and_late_confirmation_credits_primary(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    async with sessions() as db:
        primary = User(
            telegram_id=7788012250,
            balance_kopeks=0,
            status='active',
            language='ru',
            referral_code=uuid.uuid4().hex[:12],
        )
        db.add(primary)
        await db.commit()
        secondary, _, intent, payment, attempt = await seed(db, status='terminal')
        primary_id, secondary_id = primary.id, secondary.id
        intent_id, payment_id, attempt_id = intent.id, payment.id, attempt.id

        async with sessions() as callback_db:
            # Model the callback's initial non-locking lookup happening before
            # merge: its identity map still remembers the secondary owner.
            stale_attempt = await callback_db.get(DeviceAddonTopupAttempt, attempt_id)
            assert stale_attempt.user_id == secondary_id
            await callback_db.commit()

            await execute_merge(db, primary_id, secondary_id, deferred_remnawave_deletions=[])
            await db.commit()
            stored_intent = await db.get(DeviceAddonIntent, intent_id, populate_existing=True)
            stored_payment = await db.get(PlategaPayment, payment_id, populate_existing=True)
            stored_attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
            assert stored_intent.user_id == stored_payment.user_id == stored_attempt.user_id == primary_id

            confirmed = {
                'id': stored_attempt.provider_payment_id,
                'status': 'CONFIRMED',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '1.66', 'currency': 'RUB'},
                'payload': f'platega:{stored_attempt.correlation_id}',
            }
            await reconcile_device_addon_payment(callback_db, attempt_id=attempt_id, payload=confirmed)

        async with sessions() as verify_db:
            merged_primary = await verify_db.get(User, primary_id)
            merged_secondary = await verify_db.get(User, secondary_id)
            stored_attempt = await verify_db.get(DeviceAddonTopupAttempt, attempt_id)
            assert merged_primary.balance_kopeks == 166
            assert merged_secondary.balance_kopeks == 0
            assert stored_attempt.user_id == primary_id
            assert stored_attempt.status == 'paid'
            assert (
                await verify_db.scalar(
                    select(func.count(Transaction.id)).where(
                        Transaction.user_id == primary_id,
                        Transaction.type == 'deposit',
                    )
                )
                == 1
            )


async def test_merge_rekeys_colliding_per_user_intent_history(sessions):
    collision_key = 'same-client-key-on-two-accounts'
    async with sessions() as db:
        primary = User(
            telegram_id=7788012251,
            balance_kopeks=0,
            status='active',
            language='ru',
            referral_code=uuid.uuid4().hex[:12],
        )
        db.add(primary)
        await db.flush()
        primary_sub = Subscription(
            user_id=primary.id,
            end_date=datetime.now(UTC) - timedelta(days=1),
            status='expired',
            is_trial=False,
            device_limit=2,
            remnawave_short_id=uuid.uuid4().hex[:16],
        )
        db.add(primary_sub)
        await db.flush()
        primary_intent = DeviceAddonIntent(
            public_id=str(uuid.uuid4()),
            user_id=primary.id,
            subscription_id=primary_sub.id,
            target_subscription_id=primary_sub.id,
            idempotency_key=collision_key,
            request_hash='c' * 64,
            devices_to_add=1,
            original_device_limit=2,
            end_date=primary_sub.end_date,
            device_addon_generation=0,
            days_left=1,
            monthly_price_kopeks=5000,
            base_price_kopeks=0,
            quoted_price_kopeks=0,
            purchase_state='purchased',
            receipt_json={'devices_added': 1},
            fulfillment_state='ready',
            price_snapshot={'source': 'primary'},
        )
        db.add(primary_intent)
        await db.commit()
        secondary, _, secondary_intent, _, _ = await seed(db, status='terminal')
        secondary_intent.idempotency_key = collision_key
        secondary_intent.price_snapshot = {'source': 'secondary'}
        await db.commit()
        primary_id, secondary_id = primary.id, secondary.id
        primary_intent_id, secondary_intent_id = primary_intent.id, secondary_intent.id

        await execute_merge(db, primary_id, secondary_id, deferred_remnawave_deletions=[])
        await db.commit()

    async with sessions() as verify_db:
        rows = list(
            await verify_db.scalars(
                select(DeviceAddonIntent)
                .where(DeviceAddonIntent.id.in_([primary_intent_id, secondary_intent_id]))
                .order_by(DeviceAddonIntent.id)
            )
        )
        assert len(rows) == 2
        assert {row.user_id for row in rows} == {primary_id}
        assert len({row.idempotency_key for row in rows}) == 2
        stored_primary = next(row for row in rows if row.id == primary_intent_id)
        stored_secondary = next(row for row in rows if row.id == secondary_intent_id)
        assert stored_primary.idempotency_key == collision_key
        assert stored_secondary.idempotency_key.startswith(f'merged:{secondary_id}:')
        assert stored_secondary.price_snapshot['source'] == 'secondary'
        assert stored_secondary.price_snapshot['merge_idempotency_history'] == [
            {'user_id': secondary_id, 'idempotency_key': collision_key}
        ]


@pytest.mark.parametrize('status, expected', [('terminal', ERASURE_READY), ('paid', ERASURE_AWAITING_MANUAL)])
async def test_erasure_locks_and_classifies_addon_graph(sessions, status, expected):
    async with sessions() as db:
        user, _, intent, _, attempt = await seed(db, status=status)
        context = await _lock_context(db, user_id=user.id)
        assert [row.id for row in context.addon_intents] == [intent.id]
        assert [row.id for row in context.addon_attempts] == [attempt.id]
        assert _target_state(context)[0] == expected


async def test_manual_erasure_approval_cannot_discard_pending_referral_money(sessions):
    async with sessions() as db:
        user, _, _, payment, attempt = await seed(db, status='paid')
        deposit = Transaction(user_id=user.id, type='deposit', amount_kopeks=166, is_completed=True)
        db.add(deposit)
        await db.flush()
        attempt.deposit_transaction_id = deposit.id
        attempt.referral_status = 'pending'
        payment.transaction_id = deposit.id
        request = AccountErasureRequest(
            user_id=user.id,
            state='awaiting_manual_resolution',
            financial_resolution_at=datetime.now(UTC),
            financial_resolution_code='owner_approved',
        )
        db.add(request)
        await db.commit()
        context = await _lock_context(db, user_id=user.id)
        assert _target_state_after_financial_resolution(context, context.request) == (
            ERASURE_AWAITING_RECONCILIATION,
            'device_addon_referral_pending',
        )
        # The outbox's single money transaction marks this only after all
        # rewards are durable. Existing approval may then finish redaction.
        context.addon_attempts[0].referral_status = 'done'
        await db.commit()
        context = await _lock_context(db, user_id=user.id)
        assert _target_state_after_financial_resolution(context, context.request) == (ERASURE_READY, None)


@pytest.mark.parametrize('referral_status', ['pending', 'processing', 'operator_review'])
async def test_held_referral_requires_explicit_settlement_before_erasure(sessions, monkeypatch, referral_status):
    panel = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.account_erasure_service._remove_panel_identity', panel)
    async with sessions() as db:
        user, _, _, payment, attempt = await seed(db, status='paid')
        operator = User(telegram_id=7788800333)
        deposit = Transaction(user_id=user.id, type='deposit', amount_kopeks=166, is_completed=True)
        db.add_all([operator, deposit])
        await db.flush()
        attempt.deposit_transaction_id = deposit.id
        attempt.referral_status = referral_status
        payment.transaction_id = deposit.id
        user.account_erasure_requested_at = datetime.now(UTC)
        request = AccountErasureRequest(
            user_id=user.id,
            state=ERASURE_AWAITING_MANUAL,
            financial_resolution_at=datetime.now(UTC),
            financial_resolution_code='balance_writeoff_approved',
        )
        db.add(request)
        await db.commit()
        user_id, operator_id, attempt_id = user.id, operator.id, attempt.id
        result = await resolve_financial_account_erasure(
            db,
            user_id=user_id,
            resolved_by_user_id=operator_id,
            resolution_code='provider_terminal_verified',
            resolution_note='Provider status verified during reconciliation.',
        )
        assert result.state == (
            ERASURE_AWAITING_RECONCILIATION if referral_status != 'operator_review' else 'settlement_required'
        )
        panel.assert_not_awaited()
        await db.refresh(user)
        assert user.account_erased_at is None
        if referral_status == 'operator_review':
            context = await _lock_context(db, user_id=user_id)
            assert _target_state_after_financial_resolution(context, context.request) == (
                ERASURE_AWAITING_MANUAL,
                'device_addon_referral_review',
            )
            result = await resolve_financial_account_erasure(
                db,
                user_id=user_id,
                resolved_by_user_id=operator_id,
                resolution_code='balance_writeoff_approved',
                resolution_note='Held referral liability settled with the owner.',
            )
            assert result.completed
            panel.assert_awaited_once()
            await db.refresh(user)
            stored_attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
            assert user.account_erased_at is not None
            assert stored_attempt.referral_status == 'resolved_manually'
            assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.type == 'referral_reward')) == 0


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

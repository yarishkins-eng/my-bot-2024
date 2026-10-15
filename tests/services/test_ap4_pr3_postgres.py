"""Real PR-3 transaction/lock proofs on the explicitly provisioned local PostgreSQL.

Run with AP4_PR3_TEST_DATABASE_URL and ENTITLEMENT_AUTHORITY_TEST_DATABASE_URL
pointing to the same disposable database. The default suite skips this file.
Only a fresh per-test schema is created/dropped; no public-schema cleanup.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.database.models import Base, DiscountOffer, SentNotification, Subscription, Tariff, User, UserPromoGroup
from app.services import (
    device_first_checkout_service as checkout,
    monitoring_service as monitoring,
    trial_ending_discount_service as issuer,
)
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


DATABASE_URL = os.getenv('AP4_PR3_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DATABASE_URL, reason='AP4_PR3_TEST_DATABASE_URL requires disposable local PostgreSQL'),
]
LOCK_NAMESPACE = 202610024


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    if not DATABASE_URL:
        pytest.skip('AP4_PR3_TEST_DATABASE_URL requires disposable local PostgreSQL')
    url = make_url(DATABASE_URL)
    if (
        url.drivername != 'postgresql+asyncpg'
        or url.host not in {'127.0.0.1', 'localhost'}
        or url.port != 65439
        or url.database != 'ap4_pr3'
        or os.getenv('ENTITLEMENT_AUTHORITY_TEST_DATABASE_URL') != DATABASE_URL
    ):
        raise RuntimeError('PR-3 tests require the provisioned local ap4_pr3 database on port 65439')
    schema = f'ap4_pr3_{uuid.uuid4().hex}'
    bootstrap = create_async_engine(DATABASE_URL)
    engine = create_async_engine(
        DATABASE_URL,
        pool_size=5,
        max_overflow=0,
        connect_args={'server_settings': {'search_path': schema, 'statement_timeout': '5000', 'lock_timeout': '3000'}},
    )
    created = False
    try:
        async with bootstrap.begin() as connection:
            assert (await connection.scalar(text('SELECT version()'))).startswith('PostgreSQL ')
            assert await connection.scalar(text('SELECT current_database()')) == 'ap4_pr3'
            assert await connection.scalar(text('SELECT inet_server_port()')) == 65439
            assert await connection.scalar(text('SELECT host(inet_server_addr())')) == '127.0.0.1'
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(issuer, 'AsyncSessionLocal', maker)
        config = {'enabled': True, 'discount_percent': 20, 'carrier': 24, 'audience': 1, 'post_trial_hours': 6}
        monkeypatch.setattr(
            issuer.NotificationSettingsService, 'get_trial_ending_discount_settings', lambda: config.copy()
        )
        monkeypatch.setattr(issuer.NotificationSettingsService, 'is_enabled', lambda _key: True)
        monkeypatch.setattr(issuer.NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
        monkeypatch.setattr(issuer.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
        monkeypatch.setattr(issuer, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'stands')
        monkeypatch.setattr(issuer.settings, 'WEBHOOK_NOTIFY_USER_ENABLED', True)
        monkeypatch.setattr(issuer.settings, 'WEBHOOK_NOTIFY_SUB_EXPIRING', True)
        monkeypatch.setattr(issuer.settings, 'SALES_MODE', 'tariffs')
        monkeypatch.setattr(issuer.settings, 'MULTI_TARIFF_ENABLED', False)
        monkeypatch.setattr(
            issuer,
            'build_purchase_options',
            AsyncMock(return_value={'eligible': True, 'price_matrix': [{'prices': [{'price_kopeks': 11920}]}]}),
        )
        yield maker
    finally:
        await engine.dispose()
        if created:
            async with bootstrap.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await bootstrap.dispose()


async def _seed(maker, *, reset_ready=False, expired_slot=False):
    now = datetime.now(UTC)
    async with maker() as db:
        trial = Tariff(name='PR3 trial', is_active=False, is_trial_available=True, device_limit=2)
        paid = Tariff(
            name='PR3 paid',
            is_active=True,
            device_limit=2,
            device_purchase_options=[2],
            period_prices={'30': 14900, '90': 39900},
        )
        user = User(
            telegram_id=9_001_000_001,
            status='active',
            language='ru',
            first_name='before',
            remnawave_uuid=str(uuid.uuid4()),
            test_account_enabled=True,
            test_reset_state='ready' if reset_ready else None,
            test_reset_started_at=now - timedelta(hours=2) if reset_ready else None,
            promo_offer_discount_percent=10 if expired_slot else 0,
            promo_offer_discount_source='old-source' if expired_slot else None,
            promo_offer_discount_expires_at=now - timedelta(hours=1) if expired_slot else None,
        )
        db.add_all([trial, paid, user])
        await db.flush()
        subscription = Subscription(
            user_id=user.id,
            tariff_id=trial.id,
            status='active',
            is_trial=True,
            device_limit=2,
            remnawave_uuid=user.remnawave_uuid,
            end_date=now + timedelta(hours=23, minutes=59),
        )
        db.add(subscription)
        await db.commit()
        return {
            'user_id': user.id,
            'subscription_id': subscription.id,
            'carrier': 24,
            'panel_uuid': subscription.remnawave_uuid,
            'panel_end': subscription.end_date,
            'lifetime_bytes': 1_000_000,
            'reset_started_at': user.test_reset_started_at,
        }


async def _read_user(db, user_id):
    return await db.scalar(
        select(User)
        .where(User.id == user_id)
        .options(
            selectinload(User.promo_group),
            selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
        )
    )


@pytest.mark.parametrize('skip', [False, True])
async def test_polling_releases_user_row_after_failed_send_or_fresh_skip(sessions, monkeypatch, skip):
    args = await _seed(sessions)
    async with sessions() as db:
        owner = await db.get(User, args['user_id'])
        owner.promo_offer_discount_source = 'trial_ending_discount'
        owner.promo_offer_discount_percent = 20
        owner.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=2)
        owner.restriction_subscription = skip
        sub = await db.get(Subscription, args['subscription_id'])
        sub.end_date = datetime.now(UTC) + timedelta(hours=3)
        await db.commit()
    monitor = monitoring.MonitoringService.__new__(monitoring.MonitoringService)
    monitor.bot = object()
    monitor.prepare_trial_discount = AsyncMock(return_value=None)
    monitor._trial_price_lines = AsyncMock(return_value=monitoring.TrialDiscountPriceLines('own offer'))
    monitor._send_trial_ending_notification = AsyncMock(side_effect=TimeoutError('telegram'))
    async with sessions() as db:
        await monitor._check_trial_expiring_soon(db)
        assert not db.in_transaction()
        async with sessions() as other:
            assert (
                await other.scalar(select(User.id).where(User.id == args['user_id']).with_for_update(nowait=True))
                == args['user_id']
            )
    assert monitor._send_trial_ending_notification.await_count == int(not skip)


async def _assert_grant(maker, args, prepared):
    async with maker() as db:
        offers = (await db.scalars(select(DiscountOffer).where(DiscountOffer.user_id == args['user_id']))).all()
        assert len(offers) == 1
        offer = offers[0]
        user = await db.get(User, args['user_id'])
        assert offer.id == prepared.offer_id
        assert offer.discount_percent == user.promo_offer_discount_percent == prepared.discount_percent == 20
        assert offer.expires_at == user.promo_offer_discount_expires_at == prepared.expires_at
        assert offer.notification_type == user.promo_offer_discount_source == issuer.TRIAL_ENDING_DISCOUNT_SOURCE
        assert offer.subscription_id == prepared.subscription_id == args['subscription_id']
        assert offer.claimed_at is not None and offer.is_active is False
        assert offer.extra_data['panel_uuid'] == prepared.panel_uuid == args['panel_uuid']
        assert offer.extra_data['reset_started_at'] == (
            args['reset_started_at'].isoformat() if args['reset_started_at'] else None
        )


async def _wait_for_actual_postgres_block(maker):
    async def observe():
        async with maker() as db:
            while not await db.scalar(
                text(
                    'SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() '
                    "AND wait_event_type='Lock' AND cardinality(pg_blocking_pids(pid)) > 0 "
                    "AND query LIKE 'SELECT users.%')"
                )
            ):
                await db.rollback()
                await asyncio.sleep(0.01)

    await asyncio.wait_for(observe(), timeout=2)


async def _try_outer_lock(maker, subscription_id):
    async with maker() as db:
        return await db.scalar(select(func.pg_try_advisory_xact_lock(LOCK_NAMESPACE, subscription_id)))


async def test_parallel_prepare_has_one_real_row_lock_winner_and_one_unchanged_grant(sessions, monkeypatch):
    args = await _seed(sessions)
    reached, resume = asyncio.Event(), asyncio.Event()
    calls = 0

    async def options(_db, _user, *, subscription):
        nonlocal calls
        calls += 1
        if calls == 1:
            reached.set()
            await resume.wait()
        return {'eligible': True, 'price_matrix': [{'prices': [{'price_kopeks': 11920}]}]}

    monkeypatch.setattr(issuer, 'build_purchase_options', options)
    first = asyncio.create_task(issuer.prepare_trial_ending_discount(**args))
    second = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=2)
        second = asyncio.create_task(issuer.prepare_trial_ending_discount(**args))
        await _wait_for_actual_postgres_block(sessions)
        assert not second.done()
    finally:
        resume.set()
        results = await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=5)
    assert len(results) == 2 and results[0] is not None and results[1] is None
    assert calls == 2  # Only the winner reaches before/after pricing; the loser sees committed state.
    await _assert_grant(sessions, args, results[0])


async def test_reset_ready_webhook_commits_issue_before_its_outer_user_lock(sessions):
    args = await _seed(sessions, reset_ready=True)
    webhook = RemnaWaveWebhookService(bot=None)
    called = []

    async def handler(db, user, subscription, data):
        prepared = data['_trial_discount_prepared']
        assert prepared is not None and issuer.validate_prepared(prepared, user, subscription)
        await _assert_grant(sessions, args, prepared)
        async with sessions() as rival:
            with pytest.raises(DBAPIError) as blocked:
                await rival.scalar(select(User).where(User.id == user.id).with_for_update(nowait=True))
            assert blocked.value.orig.sqlstate == '55P03'
        called.append(prepared)

    payload = {
        'uuid': args['panel_uuid'],
        'expireAt': args['panel_end'].isoformat(),
        'userTraffic': {'lifetimeUsedTrafficBytes': 1_000_000},
    }
    async with sessions() as outer:
        assert (
            await asyncio.wait_for(
                webhook._process_user_event(outer, 'user.expires_in_24_hours', payload, handler), timeout=5
            )
            is True
        )
        assert len(called) == 1
        await outer.rollback()
    await _assert_grant(sessions, args, called[0])


async def test_independent_issue_real_price_builder_and_failed_delivery_preserve_outer_advisory(sessions, monkeypatch):
    args = await _seed(sessions)
    monkeypatch.setattr(checkout, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(checkout, 'is_device_first_canary_user', lambda _user: True)
    sent = []

    async def delivery(**kwargs):
        assert await _try_outer_lock(sessions, args['subscription_id']) is False
        sent.append(kwargs)
        return False  # Do not reach the normal sent-mark commit, which intentionally releases the lock.

    monkeypatch.setattr(monitoring.notification_delivery_service, 'send_notification', delivery)
    async with sessions() as outer:
        await outer.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, args['subscription_id'])))
        transaction_id = await outer.scalar(text('SELECT txid_current()'))
        user = await _read_user(outer, args['user_id'])
        subscription = await outer.get(Subscription, args['subscription_id'])
        prepared = await issuer.prepare_trial_ending_discount(**args)
        assert prepared is not None
        assert await _try_outer_lock(sessions, subscription.id) is False
        await outer.refresh(
            user,
            attribute_names=[
                'promo_offer_discount_percent',
                'promo_offer_discount_source',
                'promo_offer_discount_expires_at',
            ],
        )
        options = await checkout.build_purchase_options(outer, user, subscription=subscription)
        assert options['eligible'] is True
        month = next(row for row in options['price_matrix'] if row['period_days'] == 30)['prices'][0]
        assert month['price_kopeks'] > 0 and month['breakdown']['promo_offer_discount_kopeks'] > 0
        assert month['breakdown']['promo_offer']['source'] == issuer.TRIAL_ENDING_DISCOUNT_SOURCE
        assert await outer.scalar(text('SELECT txid_current()')) == transaction_id
        assert await _try_outer_lock(sessions, subscription.id) is False
        assert (
            await monitoring.monitoring_service.handle_trial_expiring(
                outer,
                user,
                subscription,
                hours=24,
                bot=None,
                panel_expire_at=args['panel_end'].isoformat(),
                prepared_discount=prepared,
            )
            is True
        )
        assert len(sent) == 1 and sent[0]['telegram_message']
        assert await outer.scalar(text('SELECT txid_current()')) == transaction_id
        assert await _try_outer_lock(sessions, subscription.id) is False
        assert await outer.scalar(select(func.count(SentNotification.id))) == 0
        await outer.rollback()
    assert await _try_outer_lock(sessions, args['subscription_id']) is True
    await _assert_grant(sessions, args, prepared)


class _PausedCleanupSession(AsyncSession):
    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        names = [column['name'] for column in getattr(statement, 'column_descriptions', ())]
        if names == [
            'id',
            'promo_offer_discount_percent',
            'promo_offer_discount_source',
            'promo_offer_discount_expires_at',
        ]:
            self.info['candidates_read'].set()
            await self.info['resume_cleanup'].wait()
        return result


async def test_stale_cleanup_candidates_cannot_clear_a_new_independently_committed_grant(sessions):
    args = await _seed(sessions, expired_slot=True)
    read, resume = asyncio.Event(), asyncio.Event()
    cleanup_maker = async_sessionmaker(
        sessions.kw['bind'],
        class_=_PausedCleanupSession,
        expire_on_commit=False,
        info={'candidates_read': read, 'resume_cleanup': resume},
    )

    async def cleanup():
        async with cleanup_maker() as db:
            return await issuer.cleanup_expired_discounts_safely(db)

    task = asyncio.create_task(cleanup())
    try:
        await asyncio.wait_for(read.wait(), timeout=2)
        prepared = await issuer.prepare_trial_ending_discount(**args)
        assert prepared is not None
        await _assert_grant(sessions, args, prepared)
    finally:
        resume.set()
        cleared = await asyncio.wait_for(task, timeout=5)
    assert cleared == 0
    await _assert_grant(sessions, args, prepared)


async def test_outer_rollback_preserves_committed_issue_and_repeat_never_extends_it(sessions):
    args = await _seed(sessions)
    async with sessions() as outer:
        await outer.execute(select(func.pg_advisory_xact_lock(LOCK_NAMESPACE, args['subscription_id'])))
        prepared = await issuer.prepare_trial_ending_discount(**args)
        assert prepared is not None
        user = await outer.scalar(select(User).where(User.id == args['user_id']).with_for_update())
        user.first_name = 'rolled-back'
        await outer.flush()
        await outer.rollback()
    assert await issuer.prepare_trial_ending_discount(**args) is None
    await _assert_grant(sessions, args, prepared)
    async with sessions() as db:
        user = await db.get(User, args['user_id'])
        assert user.first_name == 'before'
        # Even after the live slot expires, its existing issuance ledger prevents a second grant.
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(promo_offer_discount_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert await issuer.prepare_trial_ending_discount(**args) is None
    async with sessions() as db:
        assert await db.scalar(select(func.count(DiscountOffer.id))) == 1
        offer = await db.get(DiscountOffer, prepared.offer_id)
        assert offer.expires_at == prepared.expires_at

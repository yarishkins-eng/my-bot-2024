"""Real PostgreSQL regression for atomic campaign-tariff entitlement issuance.

Run only against a disposable local database:
    CAMPAIGN_TARIFF_BONUS_TEST_DATABASE_URL=postgresql+asyncpg://... \
    ENTITLEMENT_AUTHORITY_TEST_DATABASE_URL=postgresql+asyncpg://... \
    DATABASE_URL=postgresql+asyncpg://... pytest -q tests/integration/test_campaign_tariff_bonus_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Base,
    ServerSquad,
    Subscription,
    Tariff,
    User,
)
from app.services.campaign_service import AdvertisingCampaignService
from tests.integration.campaign_tariff_pg_helpers import ProbedSession, UserLockProbe, wait_for_backend_lock


DATABASE_URL = os.getenv('CAMPAIGN_TARIFF_BONUS_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DATABASE_URL, reason='CAMPAIGN_TARIFF_BONUS_TEST_DATABASE_URL is required'),
]


def _assert_isolated_url() -> None:
    url = make_url(DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or not (url.database or '').startswith('teplo_campaign_test_'):
        raise RuntimeError('campaign tariff integration tests require an isolated local teplo_campaign_test_* database')


@pytest_asyncio.fixture
async def sessions():
    _assert_isolated_url()
    schema = f'campaign_tariff_{uuid.uuid4().hex}'
    bootstrap = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await bootstrap.dispose()

    engine = create_async_engine(
        DATABASE_URL,
        pool_size=4,
        max_overflow=0,
        connect_args={'server_settings': {'search_path': schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        try:
            async with cleanup.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await cleanup.dispose()


async def _seed(maker, *, with_subscription: bool) -> tuple[int, int, datetime | None]:
    async with maker() as db:
        user = User(telegram_id=900_001, username='campaign-race', language='ru')
        squad = ServerSquad(squad_uuid='campaign-test-squad', display_name='Campaign test squad', is_available=True)
        tariff = Tariff(
            name='Campaign test tariff',
            is_active=True,
            traffic_limit_gb=10,
            device_limit=2,
            allowed_squads=[squad.squad_uuid],
            entitlement_mode='native_squads',
        )
        db.add_all((user, squad, tariff))
        await db.flush()
        campaign = AdvertisingCampaign(
            name='Campaign tariff race',
            start_parameter='campaign-tariff-race',
            bonus_type='tariff',
            tariff_id=tariff.id,
            tariff_duration_days=30,
            is_active=True,
        )
        prior_end = None
        if with_subscription:
            prior_end = datetime.now(UTC) + timedelta(days=7)
            db.add(
                Subscription(
                    user_id=user.id,
                    tariff_id=tariff.id,
                    status='active',
                    is_trial=False,
                    start_date=datetime.now(UTC) - timedelta(days=2),
                    end_date=prior_end,
                    traffic_limit_gb=10,
                    device_limit=2,
                    connected_squads=[squad.squad_uuid],
                    remnawave_short_id='camp-existing-01',
                )
            )
        db.add(campaign)
        await db.commit()
        return user.id, campaign.id, prior_end


async def _apply(maker, user_id: int, campaign_id: int, *, create_panel=None, update_panel=None, on_session=None):
    async with maker() as db:
        if on_session:
            await on_session(db)
        user = await db.get(User, user_id)
        campaign = await db.get(AdvertisingCampaign, campaign_id)
        service = AdvertisingCampaignService()
        service.subscription_service.create_remnawave_user = create_panel or AsyncMock(return_value=SimpleNamespace())
        service.subscription_service.update_remnawave_user = update_panel or AsyncMock(return_value=SimpleNamespace())
        return await service.apply_campaign_bonus(db, user, campaign)


async def _counts(maker, user_id: int, campaign_id: int) -> tuple[list[Subscription], int]:
    async with maker() as db:
        subscriptions = list((await db.scalars(select(Subscription).where(Subscription.user_id == user_id))).all())
        markers = await db.scalar(
            select(func.count())
            .select_from(AdvertisingCampaignRegistration)
            .where(
                AdvertisingCampaignRegistration.user_id == user_id,
                AdvertisingCampaignRegistration.campaign_id == campaign_id,
            )
        )
        return subscriptions, int(markers or 0)


async def _squad_count(maker) -> int:
    async with maker() as db:
        return int(await db.scalar(select(ServerSquad.current_users)) or 0)


async def _marker(maker, user_id: int, campaign_id: int) -> AdvertisingCampaignRegistration:
    async with maker() as db:
        marker = await db.scalar(
            select(AdvertisingCampaignRegistration).where(
                AdvertisingCampaignRegistration.user_id == user_id,
                AdvertisingCampaignRegistration.campaign_id == campaign_id,
            )
        )
        assert marker is not None
        return marker


@pytest.mark.parametrize('with_subscription', [False, True])
async def test_concurrent_bot_and_cabinet_tariff_bonus_commit_once(
    sessions, monkeypatch: pytest.MonkeyPatch, with_subscription: bool
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: with_subscription)
    user_id, campaign_id, prior_end = await _seed(sessions, with_subscription=with_subscription)
    first_panel, second_panel = AsyncMock(return_value=SimpleNamespace()), AsyncMock(return_value=SimpleNamespace())
    probe = UserLockProbe()
    probed_sessions = async_sessionmaker(
        sessions.kw['bind'],
        class_=ProbedSession,
        expire_on_commit=False,
        info={'campaign_user_lock_probe': probe},
    )
    try:
        bot = asyncio.create_task(
            _apply(probed_sessions, user_id, campaign_id, create_panel=first_panel, update_panel=first_panel)
        )
        await asyncio.wait_for(probe.first_locked.wait(), timeout=3)
        second_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def remember_second_pid(db):
            second_pid.set_result(await db.scalar(text('SELECT pg_backend_pid()')))

        cabinet = asyncio.create_task(
            _apply(
                probed_sessions,
                user_id,
                campaign_id,
                create_panel=second_panel,
                update_panel=second_panel,
                on_session=remember_second_pid,
            )
        )
        await asyncio.wait_for(second_pid, timeout=3)
        await asyncio.wait_for(probe.second_attempted.wait(), timeout=3)
        await wait_for_backend_lock(sessions, second_pid.result())
        assert not cabinet.done(), 'second entry must wait on the winner’s real PostgreSQL user lock'
        probe.release_first.set()
        results = await asyncio.wait_for(asyncio.gather(bot, cabinet), timeout=5)
    finally:
        probe.release_first.set()

    subscriptions, markers = await _counts(sessions, user_id, campaign_id)
    assert markers == 1
    assert sorted(result.is_new_registration for result in results) == [False, True]
    assert all(result.success for result in results)
    assert len(subscriptions) == 1
    assert first_panel.await_count + second_panel.await_count == 1
    marker = await _marker(sessions, user_id, campaign_id)
    assert (
        marker.bonus_type,
        marker.balance_bonus_kopeks,
        marker.subscription_duration_days,
        marker.tariff_id,
        marker.tariff_duration_days,
    ) == ('tariff', 0, None, subscriptions[0].tariff_id, 30)
    if prior_end is not None:
        extension = subscriptions[0].end_date - prior_end
        assert extension == timedelta(days=30)


async def test_marker_failure_rolls_back_grant_then_retry_commits_once(
    sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    user_id, campaign_id, _ = await _seed(sessions, with_subscription=False)
    panel = AsyncMock(return_value=SimpleNamespace())
    fail_once = True

    def reject_first_marker(sync_session: Session, _flush_context, _instances) -> None:
        nonlocal fail_once
        if fail_once and any(isinstance(item, AdvertisingCampaignRegistration) for item in sync_session.new):
            fail_once = False
            raise RuntimeError('injected marker write failure')

    event.listen(Session, 'before_flush', reject_first_marker)
    try:
        with pytest.raises(RuntimeError, match='injected marker write failure'):
            await _apply(sessions, user_id, campaign_id, create_panel=panel)
    finally:
        event.remove(Session, 'before_flush', reject_first_marker)

    subscriptions, markers = await _counts(sessions, user_id, campaign_id)
    assert subscriptions == []
    assert markers == 0
    assert await _squad_count(sessions) == 0
    panel.assert_not_awaited()
    retry = await _apply(sessions, user_id, campaign_id)
    subscriptions, markers = await _counts(sessions, user_id, campaign_id)
    assert retry.is_new_registration is True
    assert len(subscriptions) == 1
    assert markers == 1
    assert await _squad_count(sessions) == 1


@pytest.mark.parametrize(
    'panel_failure',
    [None, RuntimeError('panel unavailable')],
    ids=['panel-returned-none', 'panel-raised'],
)
async def test_panel_failure_after_commit_queues_recovery_without_second_grant(
    sessions, monkeypatch: pytest.MonkeyPatch, panel_failure: Exception | None
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    user_id, campaign_id, _ = await _seed(sessions, with_subscription=False)
    from app.services.remnawave_retry_queue import remnawave_retry_queue

    enqueue = MagicMock()
    monkeypatch.setattr(remnawave_retry_queue, 'enqueue', enqueue)
    failed_panel = AsyncMock(return_value=None) if panel_failure is None else AsyncMock(side_effect=panel_failure)
    winner = await _apply(sessions, user_id, campaign_id, create_panel=failed_panel)

    subscriptions, markers = await _counts(sessions, user_id, campaign_id)
    assert winner.success is True
    assert winner.is_new_registration is True
    assert len(subscriptions) == 1
    assert markers == 1
    enqueue.assert_called_once_with(subscription_id=subscriptions[0].id, user_id=user_id, action='create')

    later_panel = AsyncMock(return_value=SimpleNamespace())
    loser = await _apply(sessions, user_id, campaign_id, create_panel=later_panel)
    subscriptions_after, markers_after = await _counts(sessions, user_id, campaign_id)
    assert loser.success is True
    assert loser.is_new_registration is False
    later_panel.assert_not_awaited()
    enqueue.assert_called_once()
    assert len(subscriptions_after) == 1
    assert markers_after == 1


@pytest.mark.parametrize('helper_rolls_back', [False, True], ids=['helper-returns-none', 'helper-rolls-back'])
async def test_panel_db_rollback_after_commit_leaves_caller_session_usable(
    sessions, monkeypatch: pytest.MonkeyPatch, helper_rolls_back: bool
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    user_id, campaign_id, _ = await _seed(sessions, with_subscription=False)
    from app.services.remnawave_retry_queue import remnawave_retry_queue

    enqueue = MagicMock()
    monkeypatch.setattr(remnawave_retry_queue, 'enqueue', enqueue)
    panel_failed = False
    async with sessions() as db:
        user = await db.get(User, user_id)
        campaign = await db.get(AdvertisingCampaign, campaign_id)
        service = AdvertisingCampaignService()

        async def panel_with_its_own_failed_db_operation(_db, _subscription):
            nonlocal panel_failed
            with pytest.raises(SQLAlchemyError):
                await db.execute(text('SELECT * FROM absent_campaign_panel_table'))
            panel_failed = True
            if helper_rolls_back:
                await db.rollback()

        service.subscription_service.create_remnawave_user = panel_with_its_own_failed_db_operation
        result = await service.apply_campaign_bonus(db, user, campaign)
        assert panel_failed is True
        assert result.success is True
        assert result.is_new_registration is True
        assert result.tariff_id is not None
        assert user.telegram_id == 900_001
        assert await db.scalar(text('SELECT 1')) == 1

    subscriptions, markers = await _counts(sessions, user_id, campaign_id)
    assert len(subscriptions) == 1
    assert markers == 1
    enqueue.assert_called_once_with(subscription_id=subscriptions[0].id, user_id=user_id, action='create')

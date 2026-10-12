"""Two concurrent webhook sessions on a disposable local PostgreSQL database."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import SentNotification
from app.services import monitoring_service as module


URL = os.getenv('AP2B_TEST_DATABASE_URL')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not URL, reason='Requires isolated AP2b PostgreSQL')]


@pytest.mark.parametrize('delivered', [True, False])
async def test_parallel_trial_warning_is_serialized_and_failure_can_retry(monkeypatch, delivered):
    url = make_url(URL)
    if url.host not in {'localhost', '127.0.0.1'} or not url.database.startswith('teplo_ap2b_test_'):
        raise RuntimeError('Only a disposable local teplo_ap2b_test_* database is allowed')
    schema = f'ap2b_{uuid.uuid4().hex}'
    bootstrap = create_async_engine(URL)
    async with bootstrap.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(URL, connect_args={'server_settings': {'search_path': schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    started, resume = asyncio.Event(), asyncio.Event()
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        started.set()
        await resume.wait()
        return delivered

    monkeypatch.setattr(module, 'is_canonical_trial', AsyncMock(return_value=True))
    monkeypatch.setattr(module.monitoring_service, '_trial_price_lines', AsyncMock(return_value='1 месяц — 149 ₽'))
    monkeypatch.setattr(module.NotificationSettingsService, 'is_enabled', lambda key: True)
    monkeypatch.setattr(module.notification_delivery_service, 'send_notification', send)
    monkeypatch.setattr(settings, 'WEBHOOK_NOTIFY_USER_ENABLED', True)
    monkeypatch.setattr(settings, 'WEBHOOK_NOTIFY_SUB_EXPIRING', True)
    user = SimpleNamespace(id=907, telegram_id=5207068834, status='active', language='ru')
    sub = SimpleNamespace(id=233, status='active', end_date=datetime.now(UTC) + timedelta(hours=48))

    async def invoke():
        async with sessions() as db:
            return await module.monitoring_service.handle_trial_expiring(db, user, sub, hours=48, bot=None)

    try:
        async with engine.begin() as connection:
            await connection.execute(text('CREATE TABLE users (id integer PRIMARY KEY)'))
            await connection.execute(text('CREATE TABLE subscriptions (id integer PRIMARY KEY)'))
            await connection.run_sync(SentNotification.__table__.create)
            await connection.execute(text('INSERT INTO users VALUES (907)'))
            await connection.execute(text('INSERT INTO subscriptions VALUES (233)'))
        first = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert await asyncio.wait_for(invoke(), 2) is True
            assert len(calls) == 1
        finally:
            resume.set()
            await first
        assert await invoke() is True
        assert len(calls) == (1 if delivered else 2)
        async with sessions() as db:
            assert await db.scalar(select(func.count(SentNotification.id))) == int(delivered)
            # The advisory transaction lock is released on success and session rollback alike.
            assert await db.scalar(select(func.pg_try_advisory_xact_lock(202610048, sub.id))) is True
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await bootstrap.dispose()

"""Regression for the 0105 ``json`` column in the ordinary low-balance query.

The production incident was not a reset operation: PostgreSQL tried to compare
every ``users`` column for ``SELECT DISTINCT users.*`` and ``json`` has no
equality operator.  Keep this on the isolated real PostgreSQL database; SQLite
cannot reproduce the failure.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from app.database.models import Subscription, User
from app.services import monitoring_service as monitoring
from app.services.monitoring_service import MonitoringService

# Reuse the destructive, schema-per-test fixture and its strict
# localhost/``teplo_reset_test`` guard.  Its current model intentionally has
# ``test_reset_panel_uuids = JSON`` — exactly the production 0105 shape.
from tests.integration.test_test_account_reset_postgres import session  # noqa: F401 -- pytest fixture registration


DATABASE_URL = os.getenv('TEST_ACCOUNT_RESET_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason='TEST_ACCOUNT_RESET_TEST_DATABASE_URL is required for the PostgreSQL reset tests',
    ),
]


class _Daytime(datetime):
    """Freeze only the monitoring module, never Python's global datetime class."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 8, 12, tzinfo=tz or UTC)


async def test_low_balance_query_handles_0105_json_and_keeps_session_usable(session, monkeypatch):
    """Two matching subscriptions still yield one notification and a live session."""
    # Keep exercising the already-deployed 0105 representation even after the
    # model eventually changes to JSONB in a separate forward migration.
    await session.execute(
        text('ALTER TABLE users ALTER COLUMN test_reset_panel_uuids TYPE json USING test_reset_panel_uuids::json')
    )
    await session.commit()
    assert (
        await session.scalar(
            text(
                'SELECT data_type FROM information_schema.columns '
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "AND column_name = 'test_reset_panel_uuids'"
            )
        )
    ) == 'json'

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    user = User(
        telegram_id=7749231136,
        balance_kopeks=50,
        notification_settings={'balance_low_enabled': True, 'balance_low_threshold': 100},
        # This non-NULL JSON value makes sure the selected full User row has
        # the same non-comparable type which crashed production.
        test_reset_panel_uuids=['retired-panel-uuid'],
    )
    session.add(user)
    await session.flush()
    session.add_all(
        [
            Subscription(
                user_id=user.id,
                status='active',
                autopay_enabled=True,
                end_date=now + timedelta(hours=1),
                remnawave_short_id='lb-json-1',
            ),
            Subscription(
                user_id=user.id,
                status='trial',
                autopay_enabled=True,
                end_date=now + timedelta(hours=2),
                remnawave_short_id='lb-json-2',
            ),
        ]
    )
    await session.commit()

    monkeypatch.setattr(monitoring, 'datetime', _Daytime)
    monkeypatch.setattr(
        monitoring.NotificationSettingsService,
        'is_enabled',
        classmethod(lambda cls, key: key == 'low_balance'),
    )
    monkeypatch.setattr(type(monitoring.settings), 'get_main_menu_miniapp_url', lambda self: None)
    monkeypatch.setattr(monitoring.cache, 'get', AsyncMock(return_value=None))
    monkeypatch.setattr(monitoring.cache, 'set', AsyncMock(return_value=True))

    bot = AsyncMock()
    await MonitoringService(bot=bot)._check_low_balance_alerts(session)

    bot.send_message.assert_awaited_once()
    assert (await session.execute(text('SELECT 1'))).scalar_one() == 1


async def test_low_balance_query_error_rolls_back_only_its_savepoint(session, monkeypatch):
    """A bad notification query cannot discard an earlier pending cycle write."""
    pending_outer_write = User(telegram_id=7749231137, username='outer-write-survives')
    session.add(pending_outer_write)

    monkeypatch.setattr(monitoring, 'datetime', _Daytime)
    monkeypatch.setattr(
        monitoring.NotificationSettingsService,
        'is_enabled',
        classmethod(lambda cls, key: key == 'low_balance'),
    )

    original_execute = session.execute
    failed_once = False

    async def fail_only_the_low_balance_statement(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            # Execute a real PostgreSQL error inside the service's nested
            # transaction.  The outer pending INSERT is flushed before the
            # savepoint, and must remain usable after the inner rollback.
            return await original_execute(text('SELECT 1 / 0'))
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(session, 'execute', fail_only_the_low_balance_statement)

    await MonitoringService(bot=object())._check_low_balance_alerts(session)

    assert failed_once
    assert (await session.scalar(select(User.id).where(User.telegram_id == 7749231137))) == pending_outer_write.id
    assert (await session.execute(text('SELECT 1'))).scalar_one() == 1

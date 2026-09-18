"""A failed low-balance SQL query must not poison the monitoring session."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import monitoring_service as monitoring
from app.services.monitoring_service import MonitoringService


class _Daytime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 8, 12, tzinfo=tz or UTC)


@pytest.mark.asyncio
async def test_low_balance_database_error_is_contained_by_nested_transaction(monkeypatch):
    """The handler must protect the shared monitoring transaction with a savepoint."""
    db = AsyncMock()
    db.execute.side_effect = RuntimeError('simulated PostgreSQL query failure')

    @asynccontextmanager
    async def nested_transaction():
        yield

    db.begin_nested = Mock(side_effect=nested_transaction)

    monkeypatch.setattr(monitoring, 'datetime', _Daytime)
    monkeypatch.setattr(
        monitoring.NotificationSettingsService,
        'is_enabled',
        classmethod(lambda cls, key: key == 'low_balance'),
    )

    await MonitoringService(bot=object())._check_low_balance_alerts(db)

    db.begin_nested.assert_called_once_with()
    db.rollback.assert_not_awaited()

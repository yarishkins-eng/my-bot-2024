"""Regression tests for the regular monitoring autopay grace guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import monitoring_service as monitoring


class _Result:
    def __init__(self, subscriptions):
        self._subscriptions = subscriptions

    def scalars(self):
        return SimpleNamespace(all=lambda: self._subscriptions)

    def scalar_one_or_none(self):
        if isinstance(self._subscriptions, list):
            return self._subscriptions[0] if self._subscriptions else None
        return self._subscriptions


def _subscription(*, grace_until, in_grace=True):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=42,
        user_id=7,
        tariff_id=1,
        status='expired',
        end_date=now - timedelta(hours=1),
        autopay_days_before=3,
        in_grace=in_grace,
        grace_until=grace_until,
        tariff=SimpleNamespace(name='Базовый', is_daily=False),
    )


@pytest.mark.asyncio
async def test_regular_autopay_skips_active_grace_before_pricing_or_debit(monkeypatch):
    """The monitoring recovery query may include recently EXPIRED rows, but it
    must not charge one while its promised grace access is still live."""
    subscription = _subscription(grace_until=datetime.now(UTC) + timedelta(days=2))
    db = SimpleNamespace(execute=AsyncMock(return_value=_Result([subscription])))
    service = monitoring.MonitoringService(bot=None)
    debit = AsyncMock()
    monkeypatch.setattr(monitoring, 'subtract_user_balance', debit)

    await service._process_autopayments(db)

    # The candidate was rejected before the per-user refetch, pricing and debit.
    db.execute.assert_awaited_once()
    debit.assert_not_awaited()


@pytest.mark.asyncio
async def test_regular_autopay_does_not_block_a_past_grace_window(monkeypatch):
    """A stale in_grace flag is not a permanent block after grace_until passes."""
    subscription = _subscription(grace_until=datetime.now(UTC) - timedelta(minutes=1))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_Result([subscription]), _Result(subscription)]))
    service = monitoring.MonitoringService(bot=None)

    # This guard is reached only after the candidate and its refetched row both
    # passed the active-grace checks. It prevents unrelated pricing work here.
    webhook_guard = MagicMock(return_value=True)
    monkeypatch.setattr(
        'app.database.crud.subscription.is_recently_updated_by_webhook',
        webhook_guard,
    )

    await service._process_autopayments(db)

    assert db.execute.await_count == 2
    webhook_guard.assert_called_once_with(subscription)

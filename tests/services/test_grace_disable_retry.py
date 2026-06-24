"""Финализация grace («бонус 2 дня» закончился) должна НАДЁЖНО гасить VPN.

Ревью после Чата 5: `_process_grace_ended` звал `push_panel_state(active=False)`, но
ИГНОРИРОВАЛ результат и не клал сбой в очередь ретраев. «Массовый синк подберёт» —
неверно (он не на расписании). Теперь при неудаче disable панель ставится в
`remnawave_retry_queue` (action='update' перечитает grace-aware состояние и догасит).
БД при этом всё равно финализируется (in_grace=False) — повторно в grace не войдём.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus
from app.services.monitoring_service import monitoring_service


def _grace_ended_sub():
    user = SimpleNamespace(id=11, telegram_id=111)
    sub = SimpleNamespace(
        id=1,
        user_id=11,
        user=user,
        status=SubscriptionStatus.EXPIRED.value,
        in_grace=True,
        grace_until=datetime.now(UTC) - timedelta(hours=1),
        tariff=None,
    )
    return sub, user


@pytest.mark.asyncio
async def test_grace_disable_failure_enqueues_retry(monkeypatch):
    sub, user = _grace_ended_sub()
    monkeypatch.setattr(
        'app.services.monitoring_service.get_subscriptions_grace_ended', AsyncMock(return_value=[sub])
    )
    monkeypatch.setattr('app.services.monitoring_service.get_user_by_id', AsyncMock(return_value=user))
    # Панель НЕ приняла disable.
    monkeypatch.setattr(monitoring_service.subscription_service, 'push_panel_state', AsyncMock(return_value=False))
    monkeypatch.setattr(monitoring_service, '_send_subscription_expired_notification', AsyncMock(return_value=True))
    enqueue_spy = MagicMock()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue', enqueue_spy)
    monitoring_service.bot = MagicMock()

    await monitoring_service._process_grace_ended(AsyncMock())

    enqueue_spy.assert_called_once()
    kwargs = enqueue_spy.call_args.kwargs
    assert kwargs.get('subscription_id') == 1
    assert kwargs.get('action') == 'update'
    # БД всё равно финализирована — повторно в grace не зайдёт.
    assert sub.in_grace is False


@pytest.mark.asyncio
async def test_grace_disable_success_no_retry(monkeypatch):
    sub, user = _grace_ended_sub()
    monkeypatch.setattr(
        'app.services.monitoring_service.get_subscriptions_grace_ended', AsyncMock(return_value=[sub])
    )
    monkeypatch.setattr('app.services.monitoring_service.get_user_by_id', AsyncMock(return_value=user))
    # Панель приняла disable.
    monkeypatch.setattr(monitoring_service.subscription_service, 'push_panel_state', AsyncMock(return_value=True))
    monkeypatch.setattr(monitoring_service, '_send_subscription_expired_notification', AsyncMock(return_value=True))
    enqueue_spy = MagicMock()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue', enqueue_spy)
    monitoring_service.bot = MagicMock()

    await monitoring_service._process_grace_ended(AsyncMock())

    enqueue_spy.assert_not_called()
    assert sub.in_grace is False

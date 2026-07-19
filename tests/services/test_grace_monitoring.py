"""Central expiry loop must branch into grace for eligible subscribers and stay
silent in the follow-up waves while the VPN is still alive.

Pins:
  • _check_expired_subscriptions sends an eligible paid subscriber into grace
    (no expire, no «истекла» push) and expires everyone else normally;
  • _check_expired_subscription_followups skips in_grace rows so the day-1
    «Доступ заблокирован» message never fires while grace keeps the VPN on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus
from app.services.monitoring_service import monitoring_service


@pytest.mark.asyncio
async def test_eligible_subscriber_enters_grace_instead_of_expiring(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'GRACE_ENABLED', True, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    now = datetime.now(UTC)
    eligible = SimpleNamespace(
        id=1,
        user_id=11,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=now - timedelta(hours=1),
        grace_eligible_period_days=30,
        in_grace=False,
        grace_until=None,
        tariff=SimpleNamespace(name='Премиум'),
    )
    not_eligible = SimpleNamespace(
        id=2,
        user_id=22,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=True,  # trial → never grace
        end_date=now - timedelta(hours=1),
        grace_eligible_period_days=None,
        in_grace=False,
        grace_until=None,
        tariff=SimpleNamespace(name='Триал'),
    )

    paid_user = SimpleNamespace(id=11, telegram_id=111, has_had_paid_subscription=True)
    trial_user = SimpleNamespace(id=22, telegram_id=222, has_had_paid_subscription=False)
    users = {11: paid_user, 22: trial_user}

    monkeypatch.setattr(
        'app.services.monitoring_service.get_expired_subscriptions',
        AsyncMock(return_value=[eligible, not_eligible]),
    )
    monkeypatch.setattr(
        'app.services.monitoring_service.get_user_by_id',
        AsyncMock(side_effect=lambda _db, uid: users.get(uid)),
    )
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda _s: False)
    expire_spy = AsyncMock(side_effect=lambda _db, s: s)
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_spy)

    enter_grace_spy = AsyncMock(return_value=True)
    expired_notify_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, '_enter_subscription_grace', enter_grace_spy)
    monkeypatch.setattr(monitoring_service, '_send_subscription_expired_notification', expired_notify_spy)
    monkeypatch.setattr(monitoring_service, '_process_grace_ended', AsyncMock())
    monkeypatch.setattr(monitoring_service, '_log_monitoring_event', AsyncMock())
    monitoring_service.bot = MagicMock()

    db = AsyncMock()
    await monitoring_service._check_expired_subscriptions(db)

    # Eligible paid subscriber → grace, NOT expired, NO «истекла» push.
    enter_grace_spy.assert_awaited_once()
    assert enter_grace_spy.await_args.args[1] is eligible
    expired_ids = [call.args[1].id for call in expire_spy.await_args_list]
    assert 1 not in expired_ids, 'eligible subscriber must NOT be expired'
    notified_ids = [call.args[1].id for call in expired_notify_spy.await_args_list]
    assert 1 not in notified_ids, 'eligible subscriber must NOT get the «истекла» push'

    # The trial → normal expiry.
    assert 2 in expired_ids


@pytest.mark.asyncio
async def test_grace_disabled_flag_falls_back_to_normal_expiry(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'GRACE_ENABLED', False, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    now = datetime.now(UTC)
    eligible = SimpleNamespace(
        id=1,
        user_id=11,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        end_date=now - timedelta(hours=1),
        grace_eligible_period_days=30,
        in_grace=False,
        grace_until=None,
        tariff=SimpleNamespace(name='Премиум'),
    )
    paid_user = SimpleNamespace(id=11, telegram_id=111, has_had_paid_subscription=True)

    monkeypatch.setattr('app.services.monitoring_service.get_expired_subscriptions', AsyncMock(return_value=[eligible]))
    monkeypatch.setattr('app.services.monitoring_service.get_user_by_id', AsyncMock(return_value=paid_user))
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda _s: False)
    expire_spy = AsyncMock(side_effect=lambda _db, s: s)
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_spy)
    enter_grace_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, '_enter_subscription_grace', enter_grace_spy)
    monkeypatch.setattr(monitoring_service, '_send_subscription_expired_notification', AsyncMock())
    monkeypatch.setattr(monitoring_service, '_process_grace_ended', AsyncMock())
    monkeypatch.setattr(monitoring_service, '_log_monitoring_event', AsyncMock())
    monitoring_service.bot = MagicMock()

    await monitoring_service._check_expired_subscriptions(AsyncMock())

    enter_grace_spy.assert_not_awaited()  # kill-switch: grace fully off
    assert [c.args[1].id for c in expire_spy.await_args_list] == [1]


@pytest.mark.asyncio
async def test_followups_skip_subscription_in_grace(monkeypatch):
    monkeypatch.setattr(
        'app.services.notification_settings_service.NotificationSettingsService.are_notifications_globally_enabled',
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        'app.services.notification_settings_service.NotificationSettingsService.is_expired_1d_enabled',
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        'app.services.notification_settings_service.NotificationSettingsService.is_second_wave_enabled',
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(
        'app.services.notification_settings_service.NotificationSettingsService.is_third_wave_enabled',
        staticmethod(lambda: False),
    )
    from app.config import settings

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)

    now = datetime.now(UTC)
    grace_user = SimpleNamespace(id=11, telegram_id=111)
    grace_sub = SimpleNamespace(
        id=1,
        user_id=11,
        user=grace_user,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        end_date=now - timedelta(days=1, hours=12),  # ~1.5 days ago → day-1 window
        in_grace=True,  # but VPN still alive → must stay silent
        grace_until=now + timedelta(hours=12),
        tariff=None,
    )

    result = MagicMock()
    result.scalars.return_value.all.return_value = [grace_sub]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    day1_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, '_send_expired_day1_notification', day1_spy)
    monkeypatch.setattr('app.services.monitoring_service.notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr('app.services.monitoring_service.record_notification', AsyncMock())
    monkeypatch.setattr(monitoring_service, '_log_monitoring_event', AsyncMock())
    monitoring_service.bot = MagicMock()

    await monitoring_service._check_expired_subscription_followups(db)

    day1_spy.assert_not_called()

"""A failed or skipped N-hour letter commits its user transaction and cannot stop the next one."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_service as m
from tests.services.test_ap4_pr3_delivery_guards import common, letter_user
from tests.services.test_ap7_trial_letters_by_tariff import TRIAL_ID, _SelectionDB


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['not_delivered', 'send_exception', 'prepare_exception', 'changed', 'already_sent'])
async def test_each_user_commits_and_next_letter_survives(monkeypatch, first):
    _marked, recorded = common(monkeypatch)
    owner1, sub1 = letter_user()
    owner2, sub2 = letter_user()
    owner2.id = sub2.id = sub2.user_id = 82
    monkeypatch.setattr(m.NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)

    async def was_sent(_db, uid, *_args):
        return first == 'already_sent' and uid == owner1.id

    async def prepare(_db, user, *_args):
        if user is owner1 and first == 'prepare_exception':
            raise TimeoutError('preparation')

    async def refresh(obj, **_kwargs):
        if obj is owner1 and first == 'changed':
            owner1.has_had_paid_subscription = True

    async def send(user, *_args, **_kwargs):
        if user is owner1:
            if first == 'send_exception':
                raise TimeoutError('telegram')
            return False
        return True

    async def identity(_db, uid):
        return {'id': uid}

    monkeypatch.setattr(m, 'notification_sent', was_sent)
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID)))
    monkeypatch.setattr('app.services.device_first_checkout_service._subscription_identity', identity)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = object()
    monitor.prepare_trial_discount = prepare
    lines = m.TrialDiscountPriceLines('own offer')
    lines.offer_id = 91
    monitor._trial_price_lines = AsyncMock(return_value=lines)
    monitor._send_trial_ending_notification = AsyncMock(side_effect=send)
    monitor._log_monitoring_event = AsyncMock()
    db = _SelectionDB([sub1, sub2])
    db.scalar, db.refresh, db.commit, db.rollback = AsyncMock(), refresh, AsyncMock(), AsyncMock()
    try:
        await monitor._check_trial_expiring_soon(db)
        assert db.commit.await_count == 2
        db.rollback.assert_not_awaited()
        recorded.assert_awaited_once_with(db, owner2.id, sub2.id, 'trial_2h')
        assert monitor._send_trial_ending_notification.await_args.args[0] is owner2
    finally:
        db.close()

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.database.models import Subscription, Tariff
from app.services import monitoring_service as monitoring, trial_ending_discount_service as issuer
from app.services.device_first_checkout_service import _subscription_identity
from tests.services.test_ap4_pr3_postgres import (
    _seed,
    sessions as sessions,  # noqa: PLC0414 -- shared isolated PG fixture
)


@pytest.mark.asyncio
async def test_existing_offer_never_sends_trial_letter_after_primary_became_other_free_tariff(sessions, monkeypatch):
    args = await _seed(sessions)
    grant = await issuer.prepare_trial_ending_discount(**args)
    assert grant is not None
    async with sessions() as db:
        another = Tariff(name='free admin secondary', is_active=False, device_limit=2)
        db.add(another)
        await db.flush()
        primary = Subscription(
            user_id=args['user_id'],
            tariff_id=another.id,
            status='active',
            is_trial=False,
            device_limit=2,
            remnawave_uuid='another-panel',
            remnawave_short_id='free-secondary',
            end_date=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(primary)
        await db.commit()
        expected_primary = primary.id
    monkeypatch.setattr(monitoring.NotificationSettingsService, 'get_trial_warn_hours', lambda: 48)
    monitor = monitoring.MonitoringService.__new__(monitoring.MonitoringService)
    monitor.bot = object()
    monitor._trial_price_lines = AsyncMock(return_value=monitoring.TrialDiscountPriceLines('20%'))
    monitor._send_trial_ending_notification = AsyncMock(return_value=False)
    async with sessions() as db:
        actual_primary = await _subscription_identity(db, args['user_id'])
        assert actual_primary['id'] == expected_primary
        await monitor._check_trial_expiring_soon(db)
        monitor._send_trial_ending_notification.assert_not_awaited()

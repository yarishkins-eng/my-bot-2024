"""Подготовка выдачи предшествует внешнему reset-lock; старый результат не разрешает отправку."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import (
    monitoring_service as module,
    remnawave_webhook_service as webhook,
    trial_ending_discount_service as issuer,
)
from app.services.notification_settings_service import NotificationSettingsService


@pytest.mark.parametrize(
    'event,hours,data',
    [
        ('user.expires_in_24_hours', 24, {}),
        ('user.expires_in_48_hours', 48, {}),
        ('user.expiration', 24, {'_meta': {'expiration': -24}}),
        ('user.expiration', 48, {'_meta': {'expiration': -48}}),
    ],
)
@pytest.mark.parametrize('valid', [True, False])
async def test_issue_before_reset_lock_and_revalidate(monkeypatch, event, hours, data, valid):
    trace = []
    user = SimpleNamespace(
        id=7,
        test_reset_state='ready',
        test_reset_started_at=datetime.now(UTC),
        test_reset_panel_uuids=[],
        remnawave_uuid='fresh',
    )
    sub = SimpleNamespace(id=8, remnawave_uuid='fresh')
    prepared = object()

    async def prepare(*args, **kwargs):
        trace.append('issue-commit')
        return prepared

    async def lock(*args, **kwargs):
        trace.append('outer-user-lock')
        return user

    async def handler(*args):
        trace.append('send')
        assert args[3]['_trial_discount_prepared'] is (prepared if valid else None)

    service = webhook.RemnaWaveWebhookService(bot=None)
    service._resolve_user_and_subscription = AsyncMock(return_value=(user, sub))
    monkeypatch.setattr(module.monitoring_service, 'prepare_trial_discount', prepare)
    monkeypatch.setattr(issuer, 'validate_prepared', lambda *args: valid)
    assert await service._process_user_event(SimpleNamespace(scalar=lock), event, {'uuid': 'fresh', **data}, handler)
    # An invalid discount proof falls back to the ordinary letter; reset/retired fences remain above it.
    assert trace == ['issue-commit', 'outer-user-lock', 'send']


@pytest.mark.parametrize('raw', [None, True, -1, '1000000', 1.0, 1_000_000])
async def test_payload_reader_is_strict_and_bound_before_network(monkeypatch, raw):
    from tests.services.test_ap4_pr3_delivery_guards import letter_user

    user, sub = letter_user()
    monitor = module.MonitoringService.__new__(module.MonitoringService)
    api = SimpleNamespace(get_user_lifetime_traffic_once=AsyncMock(return_value=2_000_000))

    class Client:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *args):
            pass

    monitor.subscription_service = SimpleNamespace(get_api_client=lambda: Client())
    monkeypatch.setattr(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=sub.tariff_id)))
    monkeypatch.setattr(
        NotificationSettingsService,
        'get_trial_ending_discount_settings',
        lambda: {'enabled': True, 'carrier': 24, 'audience': 1},
    )
    prepare = AsyncMock(return_value=None)
    monkeypatch.setattr(issuer, 'prepare_trial_ending_discount', prepare)
    db = SimpleNamespace(dirty=set())
    await monitor.prepare_trial_discount(
        db, user, sub, 24, data={'uuid': 'current', 'userTraffic': {'lifetimeUsedTrafficBytes': raw}}
    )
    assert prepare.await_args.kwargs['lifetime_bytes'] == (raw if type(raw) is int and raw >= 0 else 2_000_000)
    assert api.get_user_lifetime_traffic_once.await_count == int(not (type(raw) is int and raw >= 0))
    prepare.reset_mock()
    api.get_user_lifetime_traffic_once.reset_mock()
    await monitor.prepare_trial_discount(
        db, user, sub, 24, data={'uuid': 'retired', 'userTraffic': {'lifetimeUsedTrafficBytes': raw}}
    )
    prepare.assert_not_awaited()
    api.get_user_lifetime_traffic_once.assert_not_awaited()


@pytest.mark.parametrize('change', ['none', 'reset', 'sale', 'term', 'uuid', 'primary'])
async def test_previous_issue_does_not_bypass_fresh_polling_fence(monkeypatch, change):
    from tests.services.test_ap7_trial_letters_by_tariff import TRIAL_ID, _SelectionDB, _subscription

    sub = _subscription(81, TRIAL_ID, True, 'soon')
    sub.remnawave_uuid = 'current'
    sub.status = 'active'
    user = sub.user
    user.status = 'active'
    user.test_reset_started_at = None
    user.test_reset_state = 'ready'
    user.restriction_subscription = False
    user.account_erasure_requested_at = None
    user.account_erased_at = None
    user.promo_offer_discount_source = 'trial_ending_discount'
    user.promo_offer_discount_percent = 20
    user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID)))
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda key: True)
    monkeypatch.setattr(NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=False))
    monitor = module.MonitoringService.__new__(module.MonitoringService)
    monitor.bot = object()
    monitor.prepare_trial_discount = AsyncMock(return_value=None)
    monitor._trial_price_lines = AsyncMock(return_value=module.TrialDiscountPriceLines('STALE 20%'))
    monitor._send_trial_ending_notification = AsyncMock(return_value=False)
    monkeypatch.setattr(
        'app.services.device_first_checkout_service._subscription_identity',
        AsyncMock(return_value={'id': sub.id + int(change == 'primary')}),
    )

    async def refresh(obj, **kwargs):
        if obj is user:
            if change == 'reset':
                user.test_reset_started_at = datetime.now(UTC)
            if change == 'sale':
                user.has_had_paid_subscription = True
        else:
            if change == 'term':
                sub.end_date += timedelta(hours=1)
            if change == 'uuid':
                sub.remnawave_uuid = 'replacement'

    db = _SelectionDB([sub])
    db.scalar = AsyncMock(return_value=user.id)
    db.refresh = refresh
    try:
        await monitor._check_trial_expiring_soon(db)
        assert monitor._send_trial_ending_notification.await_count == int(change == 'none')
        db.scalar.assert_awaited_once()
    finally:
        db.close()

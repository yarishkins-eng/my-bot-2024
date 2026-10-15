"""Cheap rejection precedes private locks and panel I/O; traffic GET is redacted and one-shot."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.external.remnawave_api import RemnaWaveAPI
from app.services import monitoring_service as m, trial_ending_discount_service as issuer
from tests.services.test_ap4_pr3_delivery_guards import letter_user


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['stands', 'paid', 'telegram', 'tariff', 'not_trial'])
async def test_cheap_gates_do_not_call_panel_or_private_issuer(monkeypatch, gate):
    user, sub = letter_user()
    if gate == 'paid':
        user.has_had_paid_subscription = True
    if gate == 'telegram':
        user.telegram_id = None
    if gate == 'not_trial':
        sub.is_trial = False
    monkeypatch.setattr('app.services.user_service.is_test_account', lambda _user: gate != 'stands')
    monkeypatch.setattr(issuer, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'stands')
    monkeypatch.setattr(
        m.NotificationSettingsService,
        'get_trial_ending_discount_settings',
        lambda: {'enabled': True, 'carrier': 24, 'audience': 1},
    )
    monkeypatch.setattr(
        m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=sub.tariff_id + int(gate == 'tariff')))
    )
    issue = AsyncMock()
    monkeypatch.setattr(issuer, 'prepare_trial_ending_discount', issue)
    client = Mock(side_effect=AssertionError('must reject before panel'))
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.subscription_service = SimpleNamespace(get_api_client=client)
    assert await monitor.prepare_trial_discount(SimpleNamespace(dirty=set()), user, sub, 24) is None
    client.assert_not_called()
    issue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'uuid,traffic,expected',
    [
        ('same', 1000000, 1000000),
        ('other', 1000000, None),
        ('same', True, None),
        ('same', -1, None),
        ('same', '1000000', None),
    ],
)
async def test_traffic_transport_is_one_get_without_enrichment(uuid, traffic, expected):
    api = RemnaWaveAPI.__new__(RemnaWaveAPI)
    api._make_request = AsyncMock(
        return_value={
            'response': {
                'uuid': uuid,
                'userTraffic': {'lifetimeUsedTrafficBytes': traffic},
                'subscriptionUrl': 'discard',
            }
        }
    )
    api.enrich_user_with_happ_link = AsyncMock(side_effect=AssertionError('no enrichment'))
    assert await api.get_user_lifetime_traffic_once('same') == expected
    api._make_request.assert_awaited_once_with(
        'GET',
        '/api/users/same',
        log_endpoint='/api/users/{redacted-traffic-uuid}',
        redact_error_details=True,
        log_http_errors_as_warning=True,
        max_retries=0,
    )
    api.enrich_user_with_happ_link.assert_not_awaited()

"""Opt-in lifetime traffic never changes the device-list contract or query budget."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.cabinet.routes.subscription_modules import devices
from app.external import remnawave_api
from tests.cabinet.test_devices_panel_ok import _install_service


@pytest.fixture
def setup(monkeypatch):
    sub = SimpleNamespace(device_limit=3, is_active=True, remnawave_uuid='selected-profile')
    api = SimpleNamespace(
        get_user_devices_all=AsyncMock(return_value={'devices': [{'hwid': 'phone', 'platform': 'iOS'}], 'total': 1}),
        get_user_lifetime_traffic_once=AsyncMock(return_value=1_000_000),
    )
    monkeypatch.setattr(devices, 'resolve_subscription', AsyncMock(return_value=sub))
    monkeypatch.setattr(devices, 'settings', SimpleNamespace(is_multi_tariff_enabled=lambda: True))
    monkeypatch.setattr(devices, 'get_aliases_for_user', AsyncMock(return_value={'phone': 'My phone'}))
    monkeypatch.setattr(devices, 'logger', Mock())
    _install_service(monkeypatch, api)

    async def call(**kwargs):
        return await devices.get_devices(
            subscription_id=12, user=SimpleNamespace(id=8, remnawave_uuid='other'), db=object(), **kwargs
        )

    return sub, api, call


@pytest.mark.asyncio
@pytest.mark.parametrize('usage,expected', [(999_999, False), (1_000_000, True), (None, None)])
async def test_exact_threshold_and_unknown(setup, usage, expected):
    _, api, call = setup
    api.get_user_lifetime_traffic_once.return_value = usage
    result = await call(traffic=True)
    assert result['vpn_working'] is expected
    assert result['total'] == 1 and result['panel_ok'] is True
    assert result['devices'][0]['local_name'] == 'My phone'
    api.get_user_lifetime_traffic_once.assert_awaited_once_with('selected-profile')


@pytest.mark.asyncio
async def test_threshold_is_a_constant_not_a_second_literal(setup, monkeypatch):
    assert remnawave_api.VPN_USED_MIN_BYTES == 1_000_000
    monkeypatch.setattr(devices, 'VPN_USED_MIN_BYTES', 9)
    _, api, call = setup
    api.get_user_lifetime_traffic_once.return_value = 9
    assert (await call(traffic=True))['vpn_working'] is True
    api.get_user_lifetime_traffic_once.return_value = 8
    assert (await call(traffic=True))['vpn_working'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['timeout', 'exception'])
async def test_traffic_failure_keeps_devices_total_and_panel_ok(setup, monkeypatch, fault):
    _, api, call = setup
    original = await call()
    real_timeout = asyncio.timeout
    budgets, cancelled = [], []

    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.001)

    async def read(_uuid):
        if fault == 'exception':
            raise RuntimeError('secret-selected-profile')
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(devices.asyncio, 'timeout', short_timeout)
    api.get_user_lifetime_traffic_once.side_effect = read
    result = await call(traffic=True)
    assert result.pop('vpn_working') is None
    assert result == original
    assert budgets == [3]
    assert cancelled == ([True] if fault == 'timeout' else [])
    devices.logger.warning.assert_called_once()
    assert 'selected-profile' not in str(devices.logger.warning.call_args)
    devices.logger.error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['no_uuid', 'inactive', 'panel_failure'])
async def test_unknown_branches_and_no_traffic_io(setup, gate):
    sub, api, call = setup
    if gate == 'no_uuid':
        sub.remnawave_uuid = None
    elif gate == 'inactive':
        sub.is_active = False
    else:
        api.get_user_devices_all.side_effect = RuntimeError('devices unavailable')
    result = await call(traffic=True)
    assert result['vpn_working'] is None
    assert result['panel_ok'] is (gate != 'panel_failure')
    api.get_user_lifetime_traffic_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_reads_keep_old_response_and_do_not_read_traffic(setup):
    _, api, call = setup
    assert 'vpn_working' not in await call()
    assert 'vpn_working' not in await call(traffic=False)
    api.get_user_lifetime_traffic_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_read_only_get_without_retry_or_enrichment(setup):
    _, api, call = setup
    real = remnawave_api.RemnaWaveAPI.__new__(remnawave_api.RemnaWaveAPI)
    real._make_request = AsyncMock(
        return_value={'response': {'uuid': 'selected-profile', 'userTraffic': {'lifetimeUsedTrafficBytes': 1_000_000}}}
    )
    real.enrich_user_with_happ_link = AsyncMock(side_effect=AssertionError('no enrichment'))
    api.get_user_lifetime_traffic_once = real.get_user_lifetime_traffic_once
    assert (await call(traffic=True))['vpn_working'] is True
    real._make_request.assert_awaited_once_with(
        'GET',
        '/api/users/selected-profile',
        log_endpoint='/api/users/{redacted-traffic-uuid}',
        redact_error_details=True,
        log_http_errors_as_warning=True,
        max_retries=0,
    )
    real.enrich_user_with_happ_link.assert_not_awaited()

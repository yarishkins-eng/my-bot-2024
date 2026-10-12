"""Remnawave 2.8.0 merged the 4 per-interval expiration webhooks into a single
``user.expiration`` event carrying ``meta.expiration`` (signed hours). The bot
must handle the new event (or expiration notifications silently stop on 2.8.0),
while still accepting the old events from 2.7.x panels.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.remnawave_webhook_service import RemnaWaveWebhookService


def _service() -> RemnaWaveWebhookService:
    svc = RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    return svc


def _user() -> MagicMock:
    u = MagicMock()
    u.id = 1
    return u


def _sub(*, flagged_friend=False) -> MagicMock:
    s = MagicMock()
    s.id = 42
    s.is_trial = flagged_friend
    s.tariff_id = 31
    return s


def _sent_key(svc: RemnaWaveWebhookService) -> str:
    # _notify_user(user, text_key, *, ...)
    return svc._notify_user.await_args.args[1]


def _receiver_data(expiration) -> dict:
    """Build the handler ``data`` exactly as the webhook receiver does.

    The receiver injects the envelope ``meta`` under ``data['_meta']`` (see
    app/webserver/remnawave_webhook.py: "Inject meta into data so handlers can
    access it via data.get('_meta')"). Handlers read ``_meta`` — NOT ``meta``.
    Tests MUST go through this contract, otherwise they silently pass against
    the wrong key and mask a production break (every notification lost).
    """
    payload = {'event': 'user.expiration', 'meta': {'expiration': expiration}}
    data: dict = {}
    meta = payload.get('meta')
    if isinstance(meta, dict):
        data['_meta'] = meta
    return data


async def test_new_and_old_events_both_registered():
    svc = _service()
    # 2.8.0 event handled...
    assert svc._user_handlers.get('user.expiration') is not None
    # ...and 2.7.x events kept for backward compatibility.
    for old in (
        'user.expires_in_72_hours',
        'user.expires_in_48_hours',
        'user.expires_in_24_hours',
        'user.expired_24_hours_ago',
    ):
        assert old in svc._user_handlers


async def test_canonical_hours_map_to_legacy_messages_for_flagged_friends(monkeypatch):
    """Решение12.10: маппинг сохраняется друзьям с флагом на обычном тарифе, платным пересылка снята."""
    monkeypatch.setattr('app.services.monitoring_service.is_canonical_trial', AsyncMock(return_value=False))
    cases = {
        -72: 'WEBHOOK_SUB_EXPIRES_72H',
        -48: 'WEBHOOK_SUB_EXPIRES_48H',
        -24: 'WEBHOOK_SUB_EXPIRES_24H',
        24: 'WEBHOOK_SUB_EXPIRED_24H_AGO',
    }
    for hours, expected in cases.items():
        svc = _service()
        await svc._handle_user_expiration(None, _user(), _sub(flagged_friend=True), _receiver_data(hours))
        svc._notify_user.assert_awaited_once()
        assert _sent_key(svc) == expected


async def test_reads_receiver_meta_key_not_raw_meta(monkeypatch):
    """Regression: the handler reads data['_meta'] (receiver contract). A payload
    carrying the RAW envelope key 'meta' (i.e. receiver injection skipped) must
    NOT produce a notification — this is exactly the bug that silently dropped
    every user.expiration notification on 2.8.0."""
    # АП-7: выбираем оставшуюся пересылаемую когорту, чтобы новый paid-gate не маскировал ошибку разбора meta.
    monkeypatch.setattr('app.services.monitoring_service.is_canonical_trial', AsyncMock(return_value=False))
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(flagged_friend=True), {'meta': {'expiration': -24}})
    svc._notify_user.assert_not_awaited()


async def test_non_canonical_negative_picks_nearest_before_message_for_flagged_friends(monkeypatch):
    """Решение12.10: нестандартный интервал продолжает nearest-пересылку только флаговым непробным."""
    monkeypatch.setattr('app.services.monitoring_service.is_canonical_trial', AsyncMock(return_value=False))
    svc = _service()
    # -30 is closest to -24 → "expires in <24h" message.
    await svc._handle_user_expiration(None, _user(), _sub(flagged_friend=True), _receiver_data(-30))
    assert _sent_key(svc) == 'WEBHOOK_SUB_EXPIRES_24H'


async def test_non_canonical_positive_uses_expired_message_for_flagged_friends(monkeypatch):
    """Решение12.10: общее «после» сохранено друзьям; платным его больше не пересылаем."""
    monkeypatch.setattr('app.services.monitoring_service.is_canonical_trial', AsyncMock(return_value=False))
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(flagged_friend=True), _receiver_data(48))
    assert _sent_key(svc) == 'WEBHOOK_SUB_EXPIRED_24H_AGO'


async def test_missing_or_invalid_meta_sends_nothing(monkeypatch):
    """АП-7: ошибка meta должна молчать даже у друзей, которым пересылка по решению владельца сохранена."""
    monkeypatch.setattr('app.services.monitoring_service.is_canonical_trial', AsyncMock(return_value=False))
    for data in ({'_meta': {}}, {'_meta': {'expiration': 'oops'}}, {}, {'_meta': None}):
        svc = _service()
        await svc._handle_user_expiration(None, _user(), _sub(flagged_friend=True), data)
        svc._notify_user.assert_not_awaited()


async def test_no_subscription_sends_nothing():
    svc = _service()
    await svc._handle_user_expiration(None, _user(), None, _receiver_data(-24))
    svc._notify_user.assert_not_awaited()


@pytest.mark.parametrize('hours', [-72, -48, -24, 24, -30, -55, -1, 0, 48, 100])
async def test_ap7_paid_panel_expiration_is_not_forwarded(hours):
    """Владелец12.10 выбрал только письма бота платным, включая нестандартные часы панели."""
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(), _receiver_data(hours))
    svc._notify_user.assert_not_awaited()


@pytest.mark.parametrize('hours', [-72, -48, -24, 24, -30, 48])
async def test_ap7_every_suppressed_expiration_has_hours_and_subscription_in_log(monkeypatch, hours):
    """Журнал позволяет принять отсутствие пересылки на настоящем событии, не по отсутствию доставки."""
    logger = MagicMock()
    monkeypatch.setattr('app.services.remnawave_webhook_service.logger.info', logger)
    svc = _service()
    await svc._handle_user_expiration(None, _user(), _sub(), _receiver_data(hours))
    logger.assert_any_call('письмо панели о конце не пересылаем', hours=hours, subscription_id=42)


async def test_new_2_8_0_api_token_admin_events_registered():
    """2.8.0 added service.api_token_created/deleted — surfaced as admin notifications."""
    svc = _service()
    assert 'service.api_token_created' in svc._admin_handlers
    assert 'service.api_token_deleted' in svc._admin_handlers


async def test_user_modified_syncs_used_traffic_from_nested_user_traffic():
    """usedTrafficBytes lives nested in userTraffic (ExtendedUsersSchema); the
    user.modified handler must read it there, else used-traffic never syncs."""
    svc = _service()
    sub = MagicMock()
    sub.status = 'active'
    sub.traffic_used_gb = 0.0
    await svc._handle_user_modified(AsyncMock(), _user(), sub, {'userTraffic': {'usedTrafficBytes': 5 * 1024**3}})
    assert sub.traffic_used_gb == 5.0


async def test_user_modified_used_traffic_falls_back_to_flat_key():
    """Old panels send a flat usedTrafficBytes — keep the fallback working."""
    svc = _service()
    sub = MagicMock()
    sub.status = 'active'
    sub.traffic_used_gb = 0.0
    await svc._handle_user_modified(AsyncMock(), _user(), sub, {'usedTrafficBytes': 2 * 1024**3})
    assert sub.traffic_used_gb == 2.0

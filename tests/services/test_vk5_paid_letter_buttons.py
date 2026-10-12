"""VK-5: all paid renewal letters open checkout without changing unrelated buttons."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.admin_auto_messages import AUTO_MESSAGE_CATALOG
from app.config import settings
from app.services.monitoring_service import MonitoringService
from app.services.notification_settings_service import NotificationSettingsService
from app.services.pricing_engine import pricing_engine
from app.utils.miniapp_buttons import CALLBACK_TO_CABINET_PATH


SCENARIOS = ['3d', '1d', 'expired', 'day1', 'wave2', 'wave3', 'grace']


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', SCENARIOS)
@pytest.mark.parametrize('multi', [False, True])
async def test_actual_paid_sender_opens_checkout_or_keeps_multi_target(monkeypatch, scenario, multi):
    monkeypatch.setattr(type(settings), 'is_cabinet_mode', lambda self: True)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: multi)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://example.test')
    monkeypatch.setattr(NotificationSettingsService, 'text_for', staticmethod(lambda key, default: default))
    monkeypatch.setattr(
        pricing_engine, 'calculate_renewal_price', AsyncMock(return_value=SimpleNamespace(final_total=14900))
    )
    user = SimpleNamespace(id=7, telegram_id=77, language='ru', balance_kopeks=5000)
    end = datetime(2026, 10, 12, tzinfo=UTC)
    sub = SimpleNamespace(id=42, tariff=None, tariff_id=3, end_date=end, is_trial=False, autopay_enabled=False)
    monitor = MonitoringService.__new__(MonitoringService)
    monitor._send_message_with_logo = AsyncMock()
    if scenario in {'3d', '1d'}:
        ok = await monitor._send_subscription_expiring_notification(user, sub, 3 if scenario == '3d' else 1)
    elif scenario == 'expired':
        ok = await monitor._send_subscription_expired_notification(user, sub)
    elif scenario == 'day1':
        ok = await monitor._send_expired_day1_notification(AsyncMock(), user, sub)
    elif scenario in {'wave2', 'wave3'}:
        ok = await monitor._send_expired_discount_notification(
            user,
            sub,
            10 if scenario == 'wave2' else 20,
            end,
            12,
            'second' if scenario == 'wave2' else 'third',
            trigger_days=3,
        )
    else:
        ok = await monitor._send_grace_started_notification(user, sub, end)
    assert ok is True
    buttons = [
        button
        for row in monitor._send_message_with_logo.await_args.kwargs['reply_markup'].inline_keyboard
        for button in row
    ]
    renew = next(button for button in buttons if 'Продлить' in button.text)
    if multi:
        assert renew.callback_data == 'se:42'
        assert renew.web_app is None
    else:
        assert renew.web_app.url == 'https://example.test/subscription/purchase'
        assert renew.callback_data is None
    if scenario in {'3d', '1d'}:
        topup = next(button for button in buttons if 'Пополнить' in button.text)
        assert topup.web_app.url == 'https://example.test/balance/top-up'
    assert CALLBACK_TO_CABINET_PATH['subscription_extend'] == '/subscription'


@pytest.mark.parametrize(
    'card_id', ['paid-3d', 'paid-1d', 'paid-expired', 'return-day1', 'return-wave2', 'return-wave3', 'grace-2d']
)
def test_catalog_describes_the_new_renewal_destination(card_id):
    card = next(card for card in AUTO_MESSAGE_CATALOG if card['id'] == card_id)
    renew = next(button for button in card['buttons'] if 'Продлить' in button['label'])
    assert renew['target'] == 'Кабинет, касса продления'
    assert renew['tracked'] is False

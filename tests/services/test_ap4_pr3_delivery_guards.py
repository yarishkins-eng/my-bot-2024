"""Permanent review-B guards: real senders, delivery facts and switched-off carriers."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.localization.texts import get_texts
from app.services import monitoring_service as m, trial_ending_discount_service as issuer
from app.services.notification_settings_service import NotificationSettingsService
from tests.services.test_ap4_pr3_issuer import setup as setup  # noqa: PLC0414
from tests.services.test_ap4_pr3_letter import options
from tests.services.test_ap7_trial_letters_by_tariff import TRIAL_ID, _SelectionDB, _subscription


pytestmark = pytest.mark.asyncio
OWNER_TEXT = 'Пробный закончится через {hours_text}.\nОформите подписку.\n\n'


class LetterDB:
    def __init__(self):
        self.scalar = AsyncMock(side_effect=[True, None, 91])

    @asynccontextmanager
    async def begin_nested(self):
        yield


class Bot:
    def __init__(self):
        self.calls = []

    async def send_photo(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(photo=[SimpleNamespace(file_id='test-logo')])

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace()


def letter_user():
    sub = _subscription(81, TRIAL_ID, True, 'soon')
    sub.user_id, sub.remnawave_uuid = sub.user.id, 'current'
    owner = sub.user
    for field, value in {
        'balance_kopeks': 5000,
        'test_account_enabled': True,
        'status': 'active',
        'test_reset_started_at': None,
        'test_reset_state': 'ready',
        'restriction_subscription': False,
        'account_erasure_requested_at': None,
        'account_erased_at': None,
        'promo_offer_discount_source': 'trial_ending_discount',
        'promo_offer_discount_percent': 20,
        'promo_offer_discount_expires_at': datetime.now(UTC) + timedelta(days=2),
    }.items():
        setattr(owner, field, value)
    return owner, sub


def common(monkeypatch):
    data = options(percent=20, end=(datetime.now(UTC) + timedelta(days=2)).isoformat())
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.build_purchase_options', AsyncMock(return_value=data)
    )
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID)))
    monkeypatch.setattr(m, 'clear_notification_by_type', AsyncMock())
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda _key: True)
    monkeypatch.setattr(m.settings, 'WEBHOOK_NOTIFY_USER_ENABLED', True)
    monkeypatch.setattr(m.settings, 'WEBHOOK_NOTIFY_SUB_EXPIRING', True)
    marked, recorded = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(issuer, 'mark_trial_discount_delivered', marked)
    monkeypatch.setattr(m, 'record_notification', recorded)
    return marked, recorded


@pytest.mark.parametrize('delivered', [True, False])
@pytest.mark.parametrize('hours', [24, 48])
async def test_48_24_delivery_fact_follows_sender(monkeypatch, delivered, hours):
    marked, recorded = common(monkeypatch)
    sender = AsyncMock(return_value=delivered)
    monkeypatch.setattr(m.notification_delivery_service, 'send_notification', sender)
    owner, sub = letter_user()
    sub.end_date = datetime.now(UTC) + timedelta(hours=hours, minutes=-1)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    assert await monitor.handle_trial_expiring(LetterDB(), owner, sub, hours=hours, bot=object())
    assert marked.await_count == recorded.await_count == int(delivered)
    sent = sender.await_args.kwargs
    assert isinstance(sent['context']['price_lines'], m.TrialDiscountPriceLines)
    assert sent['telegram_markup'].inline_keyboard[0][0].text == get_texts('ru').t('TRIAL_DISCOUNT_CTA')


async def test_2h_discount_block_after_owner_text_with_discount_cta(monkeypatch):
    common(monkeypatch)
    monkeypatch.setattr(
        NotificationSettingsService,
        'get_text_override',
        lambda name: OWNER_TEXT if name == 'TRIAL_ENDING_TEXT' else None,
    )
    monkeypatch.setattr(m.settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr('app.utils.message_patch.get_logo_media', lambda: 'logo')
    monkeypatch.setattr('app.utils.message_patch._cache_logo_file_id', lambda _r: None)
    owner, sub = letter_user()
    sub.end_date = datetime.now(UTC) + timedelta(hours=5, minutes=59)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = Bot()
    db = LetterDB()
    db.scalar = AsyncMock(return_value=91)
    lines = await monitor._trial_price_lines(db, owner, subscription=sub)
    assert isinstance(lines, m.TrialDiscountPriceLines) and lines.offer_id == 91
    assert await monitor._send_trial_ending_notification(owner, sub, 6, price_lines=lines)
    sent = monitor.bot.calls[-1]
    assert (sent.get('caption') or sent.get('text')) == OWNER_TEXT.format(hours_text='6 часов').rstrip(
        '\n'
    ) + '\n\n' + str(lines)
    assert sent['reply_markup'].inline_keyboard[0][0].text == get_texts('ru').t('TRIAL_DISCOUNT_CTA')


async def test_trial_expired_letter_with_discount_uses_discount_cta(monkeypatch):
    common(monkeypatch)
    owner, sub = letter_user()
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = Bot()
    db = LetterDB()
    db.scalar = AsyncMock(return_value=91)
    lines = await monitor._trial_price_lines(db, owner, subscription=sub)
    assert await monitor._send_trial_expired_notification(owner, price_lines=lines)
    sent = monitor.bot.calls[-1]
    assert str(lines) in sent['text']
    assert sent['reply_markup'].inline_keyboard[0][0].text == get_texts('ru').t('TRIAL_DISCOUNT_CTA')


@pytest.mark.parametrize('gate', ['carrier_letter_off', 'notifications_off', 'webhook_switch_off'])
async def test_switched_off_letter_never_gets_silent_discount(setup, monkeypatch, gate):
    _db, _config, args, _seen = setup
    if gate == 'carrier_letter_off':
        monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda key: key != 'trial_expiring_24h')
    elif gate == 'notifications_off':
        monkeypatch.setattr(NotificationSettingsService, 'are_notifications_globally_enabled', lambda: False)
    else:
        monkeypatch.setattr(m.settings, 'WEBHOOK_NOTIFY_SUB_EXPIRING', False)
    assert await issuer.prepare_trial_ending_discount(**args) is None


@pytest.mark.parametrize('delivered', [False, True])
async def test_m09_2h_delivery_fact_requires_sender_success(monkeypatch, delivered):
    """M09 must fail on sender=False, without rolling away an incorrectly recorded fact."""
    marked, recorded = common(monkeypatch)
    owner, sub = letter_user()
    monkeypatch.setattr(NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
    monkeypatch.setattr(m, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(
        'app.services.device_first_checkout_service._subscription_identity', AsyncMock(return_value={'id': sub.id})
    )
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = object()
    monitor.prepare_trial_discount = AsyncMock(return_value=None)
    lines = m.TrialDiscountPriceLines('own offer')
    lines.offer_id = 91
    monitor._trial_price_lines = AsyncMock(return_value=lines)
    monitor._send_trial_ending_notification = AsyncMock(return_value=delivered)
    monitor._log_monitoring_event = AsyncMock()
    db = _SelectionDB([sub])
    db.scalar, db.refresh, db.commit = AsyncMock(return_value=owner.id), AsyncMock(), AsyncMock()
    try:
        await monitor._check_trial_expiring_soon(db)
        monitor._send_trial_ending_notification.assert_awaited_once()
        assert marked.await_count == recorded.await_count == int(delivered)
        assert monitor._log_monitoring_event.await_count == int(delivered)
    finally:
        db.close()

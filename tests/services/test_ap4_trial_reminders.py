"""АП-4 PR-1: реальный отбор, отправка и отметка, ночь Москвы, RU/EN и шаблон владельца."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage

from app.cabinet.routes.admin_auto_messages import CATALOG_BY_ID, _text_facts
from app.services import monitoring_service as module
from app.services.notification_settings_service import NotificationSettingsService
from app.utils import funnel_notify
from tests.services.test_ap7_trial_letters_by_tariff import TRIAL_ID, _SelectionDB, _subscription


@pytest.fixture(autouse=True)
def _defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(NotificationSettingsService, '_loaded', True)
    monkeypatch.setattr(NotificationSettingsService, '_data', {})
    monkeypatch.setattr(NotificationSettingsService, '_storage_path', tmp_path / 'settings.json')
    monkeypatch.setattr(module.settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(module.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(module.settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')


def _objects(language='ru'):
    user = SimpleNamespace(id=19, telegram_id=119, language=language, status='active', balance_kopeks=5000)
    subscription = SimpleNamespace(
        id=29, tariff_id=TRIAL_ID, tariff=None, end_date=datetime.now(UTC) + timedelta(hours=3), autopay_enabled=False
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=8)),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monitor = module.MonitoringService.__new__(module.MonitoringService)
    monitor.bot = bot
    monitor._handle_unreachable_user = AsyncMock(return_value=True)
    return monitor, user, subscription


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh', 'ru-RU', 'en-US', 'EN'])
async def test_owner_template_and_language_do_not_mix(monkeypatch, language):
    monitor, user, subscription = _objects(language)
    NotificationSettingsService._data['message_texts'] = {'TRIAL_ENDING_TEXT': 'OWNER {hours_text}.'}
    assert await monitor._send_trial_ending_notification(user, subscription, 6, price_lines='PRICE TAIL') is True
    sent = monitor.bot.send_message.await_args.kwargs
    body = sent['text']
    assert body.endswith('\n\nPRICE TAIL') and '{' not in body
    base_language = language.lower().split('-')[0]
    if base_language == 'ru':
        assert body.startswith('OWNER 3 часа.')
    elif base_language == 'en':
        assert 'Your trial ends in 3 hours.' in body and 'OWNER' not in body
    else:
        assert '3 часа' in body and 'OWNER' not in body
    buttons = [button for row in sent['reply_markup'].inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].web_app.url == 'https://cabinet.example/subscription/purchase'
    if base_language in ('ru', 'en'):
        assert buttons[0].text == ('💎 Get a subscription' if base_language == 'en' else '💎 Оформить подписку')


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
def test_locale_markers_and_catalog_match_the_sender(language):
    data = json.loads((Path(__file__).parents[2] / 'app/localization/locales' / f'{language}.json').read_text())
    assert NotificationSettingsService.marker_names(data['TRIAL_ENDING_TEXT']) == {'tariff_label', 'hours_text'}
    card = CATALOG_BY_ID['trial-2h']
    assert 'пробном тарифе' in card['when'] and 'исчерпавших трафик' in card['when']
    assert [button['label'] for button in card['buttons']] == ['💎 Оформить подписку']
    facts = _text_facts('trial-2h')
    assert facts['text_has_english'] is True
    assert '{price_lines}' not in facts['text']
    price_insert = next(insert for insert in facts['text_inserts'] if insert.name == 'price_lines')
    assert len(price_insert.variants) == 3
    assert {marker.name for marker in facts['text_markers']} >= {'month_price', 'quarter_price', 'balance', 'top_up'}


@pytest.mark.parametrize(('hour', 'minute', 'silent'), [(20, 59, False), (21, 0, True), (4, 59, True), (5, 0, False)])
@pytest.mark.parametrize('kind', ['ending', 'not_connected', 'started'])
async def test_three_letters_keep_moscow_night_boundaries(monkeypatch, hour, minute, silent, kind):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, hour, minute, tzinfo=UTC)

    monkeypatch.setattr(module, 'datetime', Clock)
    monitor, user, subscription = _objects()
    if kind == 'started':
        subscription.is_trial, subscription.traffic_limit_gb = True, 5
        user.subscription = subscription
        monkeypatch.setattr(funnel_notify, '_funnel_enabled', lambda: True)
        monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda _key: True)
        monkeypatch.setattr('app.bot_factory.create_bot', lambda: monitor.bot)
        deleted, remembered = AsyncMock(), AsyncMock()
        monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', deleted)
        monkeypatch.setattr(funnel_notify, '_remember_menu_message_id', remembered)
        await funnel_notify.send_funnel_trial_menu(
            user, trial_tariff=SimpleNamespace(id=TRIAL_ID, traffic_reset_mode='DAY')
        )
        deleted.assert_awaited_once_with(monitor.bot, user.telegram_id)
        remembered.assert_awaited_once_with(user.telegram_id, 7)
        monitor.bot.session.close.assert_awaited_once()
    elif kind == 'ending':
        assert await monitor._send_trial_ending_notification(user, subscription, 6)
    else:
        assert await monitor._send_trial_not_connected_notification(user, subscription)
    assert monitor.bot.send_message.await_args.kwargs['disable_notification'] is silent
    monitor.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize('kind', ['ending', 'not_connected', 'paid', 'day1'])
@pytest.mark.parametrize('outcome', ['success', 'none', 'forbidden', 'unreachable', 'network', 'timeout'])
async def test_sender_result_is_actual_delivery(monkeypatch, kind, outcome):
    monitor, user, subscription = _objects()
    if outcome == 'none':
        monitor.bot.send_message.return_value = None
    elif outcome != 'success':
        method = SendMessage(chat_id=user.telegram_id, text='probe')
        error = {
            'forbidden': TelegramForbiddenError(method=method, message='bot was blocked by the user'),
            'unreachable': TelegramBadRequest(method=method, message='chat not found'),
            'network': TelegramNetworkError(method=method, message='timeout'),
            'timeout': TimeoutError(),
        }[outcome]
        monitor.bot.send_message.side_effect = error
    if kind == 'ending':
        delivered = await monitor._send_trial_ending_notification(user, subscription, 6)
    elif kind == 'not_connected':
        delivered = await monitor._send_trial_not_connected_notification(user, subscription)
    elif kind == 'paid':
        delivered = await monitor._send_subscription_expiring_notification(user, subscription, 3)
    else:
        monkeypatch.setattr(
            'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
            AsyncMock(return_value=SimpleNamespace(final_total=14900)),
        )
        delivered = await monitor._send_expired_day1_notification(SimpleNamespace(), user, subscription)
    assert delivered is (outcome == 'success')
    if kind in ('paid', 'day1') and outcome == 'success':
        # Общий media-helper не меняет прежний звук платных писем.
        assert monitor.bot.send_message.await_args.kwargs['disable_notification'] is False
    if outcome in ('forbidden', 'unreachable'):
        monitor._handle_unreachable_user.assert_awaited_once()


@pytest.mark.parametrize('status', ['active', 'limited', 'expired', 'disabled'])
@pytest.mark.parametrize('outcome', ['success', 'none', 'forbidden'])
async def test_real_trial_selection_only_records_delivered(monkeypatch, status, outcome):
    monitor, _, _ = _objects()
    subscription = _subscription(81, TRIAL_ID, True, 'soon')
    subscription.status = status
    subscription.user.status = 'active'
    if outcome == 'none':
        monitor.bot.send_message.return_value = None
    elif outcome == 'forbidden':
        monitor.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=subscription.user.telegram_id, text='probe'), message='blocked'
        )
    monitor._trial_price_lines = AsyncMock(return_value='REAL PRICE TAIL')
    monitor._log_monitoring_event = AsyncMock()
    monkeypatch.setattr(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID)))
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda _key: True)
    monkeypatch.setattr(NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(module, 'record_notification', record)
    db = _SelectionDB([subscription])
    try:
        await monitor._check_trial_expiring_soon(db)
        candidate = status in ('active', 'limited')
        delivered = candidate and outcome == 'success'
        assert monitor.bot.send_message.await_count == int(candidate)
        assert record.await_count == int(delivered)
        assert monitor._log_monitoring_event.await_count == int(delivered)
        if delivered:
            assert monitor.bot.send_message.await_args.kwargs['text'].endswith('\n\nREAL PRICE TAIL')
            assert monitor._log_monitoring_event.await_args.args[-1] == {'count': 1}
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['photo', 'oversize', 'fallback'])
async def test_photo_and_text_fallback_keep_silence(monkeypatch, kind):
    monitor, user, subscription = _objects()
    monkeypatch.setattr(module.settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr(module, 'LOGO_PATH', SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr('app.utils.message_patch.get_logo_media', lambda: 'photo-id')
    monkeypatch.setattr('app.utils.message_patch._cache_logo_file_id', lambda _sent: None)
    monkeypatch.setattr(module, 'is_quiet_night', lambda: True)
    if kind == 'fallback':
        monitor.bot.send_photo.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=user.telegram_id, text='probe'), message='invalid photo'
        )
    tail = 'PRICE ' + ('x' * 1100 if kind == 'oversize' else '149')
    assert await monitor._send_trial_ending_notification(user, subscription, 6, price_lines=tail)
    if kind == 'photo':
        monitor.bot.send_photo.assert_awaited_once()
        monitor.bot.send_message.assert_not_awaited()
        kwargs = monitor.bot.send_photo.await_args.kwargs
        assert kwargs['caption'].endswith(tail)
    else:
        assert monitor.bot.send_photo.await_count == int(kind == 'fallback')
        kwargs = monitor.bot.send_message.await_args.kwargs
        assert kwargs['text'].endswith(tail)
    assert kwargs['disable_notification'] is True


@pytest.mark.parametrize('failed', ['none', 'forbidden'])
async def test_monitoring_event_counts_only_success_in_a_mixed_cohort(monkeypatch, failed):
    monitor, _, _ = _objects()
    subscriptions = [_subscription(sid, TRIAL_ID, True, 'soon') for sid in (81, 82)]
    for sub in subscriptions:
        sub.user.status = 'active'
    failure = (
        None if failed == 'none' else TelegramForbiddenError(method=SendMessage(chat_id=1, text='p'), message='blocked')
    )
    monitor.bot.send_message.side_effect = [SimpleNamespace(message_id=7), failure]
    monitor._trial_price_lines = AsyncMock(return_value='PRICE')
    monitor._log_monitoring_event = AsyncMock()
    monkeypatch.setattr(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID)))
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda _key: True)
    monkeypatch.setattr(NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(module, 'record_notification', record)
    db = _SelectionDB(subscriptions)
    try:
        await monitor._check_trial_expiring_soon(db)
        assert monitor.bot.send_message.await_count == 2
        record.assert_awaited_once_with(db, 81, 81, 'trial_2h')
        assert monitor._log_monitoring_event.await_args.args[-1] == {'count': 1}
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['paid3', 'paid1', 'day1'])
@pytest.mark.parametrize('outcome', ['success', 'none', 'forbidden'])
async def test_paid_checks_do_not_record_an_unreachable_letter(monkeypatch, kind, outcome):
    monitor, user, subscription = _objects()
    subscription.user_id, subscription.user, subscription.is_trial, subscription.in_grace = user.id, user, False, False
    subscription.end_date = datetime.now(UTC) - timedelta(hours=25)
    user.notification_settings = {}
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [subscription])))
    )
    monitor._log_monitoring_event = AsyncMock()
    monitor._get_expiring_paid_subscriptions = AsyncMock(return_value=[subscription])
    monkeypatch.setattr(module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=False))
    record = AsyncMock()
    monkeypatch.setattr(module, 'record_notification', record)
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', lambda _key: True)
    monkeypatch.setattr(NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(NotificationSettingsService, 'is_expired_1d_enabled', lambda: True)
    monkeypatch.setattr('app.utils.notification_prefs.is_subscription_expiry_enabled', lambda _user: True)
    monkeypatch.setattr('app.utils.notification_prefs.get_subscription_expiry_days', lambda _user: 3)
    if outcome == 'none':
        monitor.bot.send_message.return_value = None
    elif outcome == 'forbidden':
        monitor.bot.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=1, text='p'), message='blocked'
        )
    if kind == 'day1':
        monkeypatch.setattr(
            'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
            AsyncMock(return_value=SimpleNamespace(final_total=14900)),
        )
        await monitor._check_expired_subscription_followups(db)
        if outcome == 'success':
            record.assert_awaited_once_with(db, user.id, subscription.id, 'expired_1d')
    else:
        days = 3 if kind == 'paid3' else 1
        monkeypatch.setattr(type(module.settings), 'get_autopay_warning_days', lambda _self: [days])
        await monitor._check_expiring_subscriptions(db)
        if outcome == 'success':
            record.assert_awaited_once_with(db, user.id, subscription.id, 'expiring', days)
    assert record.await_count == int(outcome == 'success')

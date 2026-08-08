from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.manager_alerts import is_manager_alert_setup_command
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.manager_alert_service import (
    ManagerAlertSettingsService,
    ManagerAlertTopic,
    manager_alert_service,
)


@pytest.fixture(autouse=True)
def isolated_manager_alert_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ManagerAlertSettingsService, '_storage_path', tmp_path / 'manager_alert_settings.json')
    ManagerAlertSettingsService._data = {}
    ManagerAlertSettingsService._loaded = False
    yield
    ManagerAlertSettingsService._data = {}
    ManagerAlertSettingsService._loaded = False


def test_bind_topics_requires_one_group() -> None:
    assert ManagerAlertSettingsService.bind_topic(
        chat_id=-100123456, topic=ManagerAlertTopic.TICKETS, thread_id=10
    )
    assert not ManagerAlertSettingsService.bind_topic(
        chat_id=-100654321, topic=ManagerAlertTopic.PAYMENTS, thread_id=11
    )
    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.TICKETS) == (-100123456, 10)
    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.PAYMENTS) is None


@pytest.mark.asyncio
async def test_mirror_admin_category_uses_allow_list_without_keyboard() -> None:
    assert ManagerAlertSettingsService.bind_topic(
        chat_id=-100123456, topic=ManagerAlertTopic.PAYMENTS, thread_id=20
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert await manager_alert_service.mirror_admin_category(bot, 'balance', '<b>Пополнение</b>')
    bot.send_message.assert_awaited_once_with(
        chat_id=-100123456,
        message_thread_id=20,
        text='<b>Пополнение</b>',
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('category', ['infrastructure', 'errors', 'partners', 'promo', None])
async def test_mirror_never_sends_excluded_admin_categories(category: str | None) -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert not await manager_alert_service.mirror_admin_category(bot, category, 'private data')
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_status_does_not_include_node_identity() -> None:
    assert ManagerAlertSettingsService.bind_topic(
        chat_id=-100123456, topic=ManagerAlertTopic.SERVICE_STATUS, thread_id=30
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert await manager_alert_service.send_service_status(bot, restored=False, event_count=2)
    sent_text = bot.send_message.await_args.kwargs['text']
    assert 'VPN-локацией' in sent_text
    assert '10.0.0.1' not in sent_text
    assert 'node-' not in sent_text


@pytest.mark.asyncio
async def test_admin_notification_mirrors_only_safe_copy_without_admin_keyboard() -> None:
    assert ManagerAlertSettingsService.bind_topic(
        chat_id=-100123456, topic=ManagerAlertTopic.PAYMENTS, thread_id=20
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = AdminNotificationService(bot)
    service.chat_id = -100999999
    service.enabled = True

    assert await service._send_message('<b>Пополнение</b>', category=NotificationCategory.BALANCE)
    assert bot.send_message.await_count == 2
    admin_call, manager_call = bot.send_message.await_args_list
    assert admin_call.kwargs['chat_id'] == -100999999
    assert manager_call.kwargs['chat_id'] == -100123456
    assert manager_call.kwargs['message_thread_id'] == 20
    assert 'reply_markup' not in manager_call.kwargs


def test_only_exact_manager_setup_commands_pass_group_filter() -> None:
    assert is_manager_alert_setup_command('/manager_alert_bind payments')
    assert is_manager_alert_setup_command('/manager_alert_status@teplo_VPN_bot')
    assert not is_manager_alert_setup_command('/admin_help')
    assert not is_manager_alert_setup_command('/manager_alert_bindings')

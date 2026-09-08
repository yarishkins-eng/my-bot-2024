from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import start as start_module


TELEGRAM_ID = 780005


@pytest.mark.asyncio
async def test_balance_notice_bypasses_photo_capable_message_answer() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    source_message = SimpleNamespace(answer=AsyncMock())
    notification = start_module.CampaignBonusNotification(
        bonus_type='balance',
        text='На вашем балансе уже 50 ₽',
    )

    await start_module._send_campaign_bonus_notification(
        bot=bot,
        source_message=source_message,
        chat_id=TELEGRAM_ID,
        notification=notification,
    )

    bot.send_message.assert_awaited_once_with(
        chat_id=TELEGRAM_ID,
        text=notification.text,
        disable_web_page_preview=True,
    )
    source_message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('bonus_type', ['subscription', 'tariff'])
async def test_non_balance_notice_keeps_photo_capable_message_answer(bonus_type: str) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    source_message = SimpleNamespace(answer=AsyncMock())
    notification = start_module.CampaignBonusNotification(
        bonus_type=bonus_type,
        text=f'{bonus_type} reward',
    )

    await start_module._send_campaign_bonus_notification(
        bot=bot,
        source_message=source_message,
        chat_id=TELEGRAM_ID,
        notification=notification,
    )

    source_message.answer.assert_awaited_once_with(notification.text)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery_fails', [False, True])
async def test_legacy_callback_sends_balance_as_text_then_keeps_menu_photo_path(
    monkeypatch: pytest.MonkeyPatch,
    delivery_fails: bool,
) -> None:
    user = SimpleNamespace(
        id=505,
        telegram_id=TELEGRAM_ID,
        username='legacy_campaign_user',
        first_name='Legacy',
        last_name=None,
        full_name='Legacy',
        language='ru',
        status='active',
        balance_kopeks=5000,
        referred_by_id=None,
        has_had_paid_subscription=False,
        subscriptions=[],
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=RuntimeError('telegram unavailable') if delivery_fails else None)
    )
    source_message = SimpleNamespace(answer=AsyncMock())
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=TELEGRAM_ID,
            username='legacy_campaign_user',
            first_name='Legacy',
            last_name=None,
        ),
        bot=bot,
        message=source_message,
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={'language': 'ru', 'campaign_id': 9}),
        clear=AsyncMock(),
    )
    db = SimpleNamespace(refresh=AsyncMock())
    notification = start_module.CampaignBonusNotification(
        bonus_type='balance',
        text='balance bonus',
    )

    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'generate_unique_referral_code', AsyncMock(return_value='refLegacy1'))
    monkeypatch.setattr(start_module, 'create_user', AsyncMock(return_value=user))
    monkeypatch.setattr(start_module, '_apply_campaign_bonus_if_needed', AsyncMock(return_value=notification))
    monkeypatch.setattr(start_module, 'delete_pending_payload_from_redis', AsyncMock())
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', AsyncMock())
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', AsyncMock())
    monkeypatch.setattr('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='next onboarding screen'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    monkeypatch.setattr('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock())

    await start_module.complete_registration_from_callback(callback, state, db)

    bot.send_message.assert_awaited_once_with(
        chat_id=TELEGRAM_ID,
        text=notification.text,
        disable_web_page_preview=True,
    )
    assert not any(call.args and call.args[0] == notification.text for call in source_message.answer.await_args_list)
    assert any(call.args and call.args[0] == 'next onboarding screen' for call in source_message.answer.await_args_list)

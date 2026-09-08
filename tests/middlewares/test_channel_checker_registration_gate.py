from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.handlers import start as start_module
from app.middlewares import channel_checker
from app.states import RegistrationStates


TELEGRAM_ID = 780002


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=TELEGRAM_ID, user_id=TELEGRAM_ID),
    )


def _callback(data: str, language_code: str | None = 'en-US') -> types.CallbackQuery:
    user = types.User(
        id=TELEGRAM_ID,
        is_bot=False,
        first_name='New',
        language_code=language_code,
    )
    message = types.Message(
        message_id=1,
        date=1,
        chat=types.Chat(id=TELEGRAM_ID, type='private'),
        from_user=types.User(id=999, is_bot=True, first_name='Teplo Bot'),
        text='old registration screen',
    )
    return types.CallbackQuery(
        id='callback-id',
        from_user=user,
        chat_instance='chat-instance',
        message=message,
        data=data,
    )


def _patch_required_channel(monkeypatch: pytest.MonkeyPatch, middleware) -> AsyncMock:
    monkeypatch.setattr(channel_checker.settings, 'CHANNEL_IS_REQUIRED_SUB', True)
    monkeypatch.setattr(channel_checker.settings, 'ADMIN_IDS', '', raising=False)
    monkeypatch.setattr(
        'app.services.support_settings_service.SupportSettingsService.is_moderator',
        lambda _telegram_id: False,
    )
    monkeypatch.setattr(
        channel_checker.channel_subscription_service,
        'get_channels_with_status',
        AsyncMock(return_value=[{'channel_link': '@teplo', 'is_subscribed': False}]),
    )
    monkeypatch.setattr(middleware, '_deactivate_subscription_on_unsubscribe', AsyncMock())
    monkeypatch.setattr(middleware, '_capture_start_payload', AsyncMock())
    deny = AsyncMock()
    monkeypatch.setattr(middleware, '_deny_message', deny)
    return deny


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('registration_state', 'callback_data'),
    [
        (RegistrationStates.waiting_for_language, 'language_select:en'),
        (RegistrationStates.waiting_for_rules_accept, 'rules_accept'),
        (RegistrationStates.waiting_for_privacy_policy_accept, 'privacy_policy_accept'),
        (RegistrationStates.waiting_for_referral_code, 'referral_skip'),
    ],
)
async def test_unsubscribed_registration_state_does_not_bypass_channel_gate(
    monkeypatch: pytest.MonkeyPatch,
    registration_state,
    callback_data: str,
) -> None:
    middleware = channel_checker.ChannelCheckerMiddleware()
    deny = _patch_required_channel(monkeypatch, middleware)
    handler = AsyncMock()
    state = _state()
    await state.set_state(registration_state)
    await state.update_data(campaign_id=9, pending_subid='click-1')

    await middleware(handler, _callback(callback_data), {'state': state, 'bot': MagicMock()})

    handler.assert_not_awaited()
    deny.assert_awaited_once()
    saved = await state.get_data()
    assert saved['campaign_id'] == 9
    assert saved['pending_subid'] == 'click-1'


@pytest.mark.asyncio
async def test_legacy_picker_choice_survives_channel_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    middleware = channel_checker.ChannelCheckerMiddleware()
    _patch_required_channel(monkeypatch, middleware)
    monkeypatch.setattr(type(channel_checker.settings), 'is_language_selection_enabled', lambda _self: True)
    monkeypatch.setattr(channel_checker, 'get_telegram_language', lambda value: value.strip().lower())
    state = _state()
    await state.set_state(RegistrationStates.waiting_for_language)
    await state.update_data(referral_code='friend')

    await middleware(AsyncMock(), _callback('language_select:en', 'ru'), {'state': state, 'bot': MagicMock()})

    assert (await state.get_data()) == {'referral_code': 'friend', 'language': 'en'}


@pytest.mark.asyncio
async def test_legacy_picker_keeps_explicit_choice_when_manual_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = channel_checker.ChannelCheckerMiddleware()
    _patch_required_channel(monkeypatch, middleware)
    monkeypatch.setattr(type(channel_checker.settings), 'is_language_selection_enabled', lambda _self: False)
    resolver = MagicMock(return_value='ru')
    monkeypatch.setattr(channel_checker, 'get_telegram_language', resolver)
    state = _state()
    await state.set_state(RegistrationStates.waiting_for_language)

    await middleware(AsyncMock(), _callback('language_select:ru'), {'state': state, 'bot': MagicMock()})

    assert (await state.get_data())['language'] == 'ru'
    resolver.assert_called_with('ru')


@pytest.mark.asyncio
async def test_channel_prompt_prefers_existing_saved_language(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()

    class _SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(channel_checker, 'AsyncSessionLocal', lambda: _SessionContext())
    monkeypatch.setattr(
        channel_checker,
        'get_user_by_telegram_id',
        AsyncMock(return_value=SimpleNamespace(language='en')),
    )
    monkeypatch.setattr(channel_checker, 'get_telegram_language', lambda value: 'ru')

    assert await channel_checker._get_channel_prompt_language(_callback('noop').from_user) == 'en'


@pytest.mark.asyncio
async def test_channel_prompt_uses_validated_fsm_choice_for_new_user(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    await state.update_data(language='en', campaign_id=9)
    monkeypatch.setattr(channel_checker, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(channel_checker, 'get_telegram_language', lambda value: 'en' if value == 'en' else 'ru')

    assert await channel_checker._get_channel_prompt_language(_callback('noop', 'ru').from_user, state) == 'en'


@pytest.mark.asyncio
async def test_channel_check_creates_new_user_once_with_telegram_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    query = SimpleNamespace(
        from_user=_callback('noop', 'en-US').from_user,
        message=SimpleNamespace(delete=AsyncMock()),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), execute=AsyncMock(return_value=MagicMock()))
    created = SimpleNamespace(
        id=601,
        telegram_id=TELEGRAM_ID,
        language='en',
        subscription=None,
        subscriptions=[],
        has_had_paid_subscription=False,
        balance_kopeks=0,
    )
    create = AsyncMock(return_value=created)
    resolver = MagicMock(return_value='en')

    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module.channel_subscription_service, 'invalidate_user_cache', AsyncMock())
    monkeypatch.setattr(
        start_module.channel_subscription_service, 'is_user_subscribed_to_all', AsyncMock(return_value=True)
    )
    monkeypatch.setattr(start_module, 'get_telegram_language', resolver)
    monkeypatch.setattr(start_module.settings, 'SKIP_RULES_ACCEPT', True)
    monkeypatch.setattr(start_module.settings, 'SKIP_REFERRAL_CODE', True)
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'generate_unique_referral_code', AsyncMock(return_value='refChannel1'))
    monkeypatch.setattr(start_module, 'create_user', create)
    monkeypatch.setattr(start_module, '_apply_campaign_bonus_if_needed', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='menu'))
    monkeypatch.setattr(start_module.MainMenuButtonService, 'get_buttons_for_user', AsyncMock(return_value=[]))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module.settings, 'ENABLE_LOGO_MODE', False)
    monkeypatch.setattr(start_module.SupportSettingsService, 'is_moderator', lambda _telegram_id: False)

    await start_module.required_sub_channel_check(query, bot, state, db)

    create.assert_awaited_once()
    assert create.await_args.kwargs['language'] == 'en'
    assert (await state.get_data())['language'] == 'en'
    resolver.assert_called_once_with('en-US')
    bot.send_message.assert_awaited_once()

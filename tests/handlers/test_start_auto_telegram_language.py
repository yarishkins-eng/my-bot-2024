from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.database.models import UserStatus
from app.handlers import start as start_module


TELEGRAM_ID = 780001


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=TELEGRAM_ID, user_id=TELEGRAM_ID),
    )


def _tg_user(language_code: str | None) -> types.User:
    return types.User(
        id=TELEGRAM_ID,
        is_bot=False,
        first_name='New',
        username='new_user',
        language_code=language_code,
    )


def _message(language_code: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        text='/start',
        from_user=_tg_user(language_code),
        bot=MagicMock(),
        answer=AsyncMock(),
    )


def _patch_start_boundary(monkeypatch: pytest.MonkeyPatch, user=None) -> AsyncMock:
    continuation = AsyncMock()
    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    return continuation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('telegram_language', 'resolved'),
    [('ru-RU', 'ru'), ('en-US', 'en'), ('de-DE', 'ru'), (None, 'ru')],
)
@pytest.mark.parametrize('manual_selection_enabled', [True, False])
async def test_new_start_resolves_language_without_picker(
    monkeypatch: pytest.MonkeyPatch,
    telegram_language: str | None,
    resolved: str,
    manual_selection_enabled: bool,
) -> None:
    continuation = _patch_start_boundary(monkeypatch)
    monkeypatch.setattr(
        type(start_module.settings),
        'is_language_selection_enabled',
        lambda _self: manual_selection_enabled,
    )
    state = _state()
    await state.update_data(campaign_id=9, pending_subid='click-1')
    message = _message(telegram_language)

    await start_module.cmd_start(message, state, MagicMock())

    data = await state.get_data()
    assert data['language'] == resolved
    assert data['campaign_id'] == 9
    assert data['pending_subid'] == 'click-1'
    message.answer.assert_not_awaited()
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleted_start_preserves_saved_language(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted = SimpleNamespace(status=UserStatus.DELETED.value, language='en')
    continuation = _patch_start_boundary(monkeypatch, deleted)
    monkeypatch.setattr('app.services.account_test_reset_service.has_reset_history', lambda _user: True)
    state = _state()
    await state.update_data(language='ru', referral_code='friend')

    await start_module.cmd_start(_message('ru'), state, MagicMock())

    assert (await state.get_data()) == {'language': 'en', 'referral_code': 'friend'}
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_new_start_replaces_stale_fsm_language(monkeypatch: pytest.MonkeyPatch) -> None:
    continuation = _patch_start_boundary(monkeypatch)
    state = _state()
    await state.update_data(language='de', campaign_id=9)

    await start_module.cmd_start(_message('en-US'), state, MagicMock())

    assert (await state.get_data()) == {'language': 'en', 'campaign_id': 9}
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_start_preserves_language_of_cabinet_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(
        id=502,
        telegram_id=TELEGRAM_ID,
        username='new_user',
        first_name='New',
        last_name=None,
        full_name='New',
        language='en',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        referred_by_id=None,
        has_had_paid_subscription=False,
        subscriptions=[],
        last_activity=None,
        updated_at=None,
    )
    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', AsyncMock())
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', AsyncMock())
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='menu'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    monkeypatch.setattr('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock())
    resolver = MagicMock(side_effect=AssertionError('existing language must not be resolved again'))
    monkeypatch.setattr(start_module, 'get_telegram_language', resolver)
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock())

    await start_module.cmd_start(_message('ru'), _state(), db)

    assert user.language == 'en'
    resolver.assert_not_called()


def _callback(language_code: str | None, data: str) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        from_user=_tg_user(language_code),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
        bot=MagicMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_selection_enabled', [True, False])
async def test_legacy_picker_keeps_explicit_choice_for_both_manual_flag_values(
    monkeypatch: pytest.MonkeyPatch,
    manual_selection_enabled: bool,
) -> None:
    continuation = AsyncMock()
    monkeypatch.setattr(
        type(start_module.settings),
        'is_language_selection_enabled',
        lambda _self: manual_selection_enabled,
    )
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    state = _state()
    await state.update_data(campaign_id=9)

    await start_module.process_language_selection(_callback('en-US', 'language_select:ru'), state, MagicMock())

    assert (await state.get_data()) == {'campaign_id': 9, 'language': 'ru'}
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_picker_keeps_explicit_choice_when_manual_change_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuation = AsyncMock()
    monkeypatch.setattr(type(start_module.settings), 'is_language_selection_enabled', lambda _self: True)
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_telegram_language', lambda value: value.strip().lower())
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    state = _state()
    await state.update_data(pending_gift_token='gift-token')

    await start_module.process_language_selection(_callback('ru', 'language_select:en'), state, MagicMock())

    assert (await state.get_data()) == {'pending_gift_token': 'gift-token', 'language': 'en'}
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_legacy_picker_keeps_state_and_asks_for_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    continuation = AsyncMock()
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    state = _state()
    await state.update_data(campaign_id=9)
    callback = _callback('ru', 'language_select:de')

    await start_module.process_language_selection(callback, state, MagicMock())

    assert (await state.get_data()) == {'campaign_id': 9}
    continuation.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
async def test_legacy_picker_disabled_preserves_deleted_language(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted = SimpleNamespace(status=UserStatus.DELETED.value, language='en')
    continuation = AsyncMock()
    monkeypatch.setattr(type(start_module.settings), 'is_language_selection_enabled', lambda _self: False)
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=deleted))
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    state = _state()

    await start_module.process_language_selection(_callback('ru', 'language_select:ru'), state, MagicMock())

    assert (await state.get_data())['language'] == 'en'
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_picker_preserves_active_race_winner_language(monkeypatch: pytest.MonkeyPatch) -> None:
    active = SimpleNamespace(status=UserStatus.ACTIVE.value, language='en')
    continuation = AsyncMock()
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=active))
    monkeypatch.setattr(start_module, '_continue_registration_after_language', continuation)
    state = _state()
    await state.update_data(pending_gift_token='gift-token')

    await start_module.process_language_selection(_callback('ru', 'language_select:ru'), state, MagicMock())

    assert (await state.get_data()) == {'pending_gift_token': 'gift-token', 'language': 'en'}
    continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_callback_race_drains_gift_and_subid_before_state_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = SimpleNamespace(
        id=503,
        telegram_id=TELEGRAM_ID,
        username='new_user',
        first_name='New',
        last_name=None,
        full_name='New',
        language='en',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=0,
        referred_by_id=None,
        has_had_paid_subscription=False,
        subscriptions=[],
    )
    gift = AsyncMock()
    subid = AsyncMock()
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=active))
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', gift)
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', subid)
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='menu'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    callback = _callback('ru', 'language_select:ru')
    state = _state()
    await state.update_data(pending_gift_token='gift-token', pending_subid='click-1')
    db = SimpleNamespace(refresh=AsyncMock())

    await start_module.complete_registration_from_callback(callback, state, db)

    gift.assert_awaited_once_with(db, state, active, callback.message.answer)
    subid.assert_awaited_once_with(db, state, active)
    assert await state.get_data() == {}

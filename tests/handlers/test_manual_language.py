"""Manual language controls remain available independently of onboarding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers import menu
from app.utils.bot_commands import get_command_menus


def _texts() -> MagicMock:
    texts = MagicMock()
    texts.t.side_effect = lambda _, fallback: fallback
    return texts


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_language_command_preserves_manual_control_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    user = SimpleNamespace(language='en')
    texts = _texts()
    keyboard = MagicMock()
    monkeypatch.setattr(type(menu.settings), 'is_language_selection_enabled', lambda _: enabled)
    monkeypatch.setattr(menu, 'get_texts', lambda _: texts)
    monkeypatch.setattr(menu, 'get_language_selection_keyboard', keyboard)

    await menu.cmd_language(message, user, state)

    state.clear.assert_awaited_once()
    if enabled:
        keyboard.assert_called_once_with(current_language='en', include_back=True, language='en')
        assert message.answer.await_args.kwargs['reply_markup'] is keyboard.return_value
    else:
        keyboard.assert_not_called()


@pytest.mark.asyncio
async def test_manual_language_routes_report_stale_callback_user(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = SimpleNamespace(answer=AsyncMock(), data='language_select:en')
    texts = _texts()
    monkeypatch.setattr(menu, 'get_texts', lambda _: texts)

    await menu.show_language_menu(callback, None, AsyncMock())
    await menu.process_language_change(callback, None, AsyncMock())

    assert callback.answer.await_count == 2
    assert all(call.kwargs['show_alert'] is True for call in callback.answer.await_args_list)


def test_command_menus_keep_language_in_russian_and_english() -> None:
    russian, english = get_command_menus()

    assert any(command.command == 'language' for command in russian)
    assert any(command.command == 'language' for command in english)


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_language_menu_for_existing_user_obeys_manual_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language='ru')
    texts = _texts()
    keyboard = MagicMock()
    render = AsyncMock()
    monkeypatch.setattr(type(menu.settings), 'is_language_selection_enabled', lambda _: enabled)
    monkeypatch.setattr(menu, 'get_texts', lambda _: texts)
    monkeypatch.setattr(menu, 'get_language_selection_keyboard', keyboard)
    monkeypatch.setattr(menu, 'edit_or_answer_photo', render)

    await menu.show_language_menu(callback, user, MagicMock())

    if enabled:
        keyboard.assert_called_once_with(current_language='ru', include_back=True, language='ru')
        render.assert_awaited_once()
        callback.answer.assert_awaited_once_with()
    else:
        keyboard.assert_not_called()
        render.assert_not_awaited()
        callback.answer.assert_awaited_once()
        assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_language_change_for_existing_user_writes_only_when_manual_enabled(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    callback = SimpleNamespace(answer=AsyncMock(), data='language_select:en')
    user = SimpleNamespace(language='ru')
    updated_user = SimpleNamespace(language='en')
    texts = _texts()
    update = AsyncMock(return_value=updated_user)
    show_menu = AsyncMock()
    monkeypatch.setattr(type(menu.settings), 'is_language_selection_enabled', lambda _: enabled)
    monkeypatch.setattr(type(menu.settings), 'get_available_languages', lambda _: ['ru', 'en'])
    monkeypatch.setattr(menu, 'get_texts', lambda _: texts)
    monkeypatch.setattr(menu, 'update_user', update)
    monkeypatch.setattr(menu, 'show_main_menu', show_menu)
    db = MagicMock()

    await menu.process_language_change(callback, user, db)

    if enabled:
        update.assert_awaited_once_with(db, user, language='en')
        show_menu.assert_awaited_once_with(callback, updated_user, db, skip_callback_answer=True)
        callback.answer.assert_awaited_once()
        assert callback.answer.await_args.kwargs == {}
    else:
        update.assert_not_awaited()
        show_menu.assert_not_awaited()
        callback.answer.assert_awaited_once()
        assert callback.answer.await_args.kwargs['show_alert'] is True

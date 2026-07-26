from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers import menu
from app.utils.bot_commands import get_command_menus


def _make_user(language: str = 'ru'):
    user = MagicMock()
    user.language = language
    return user


@pytest.mark.anyio('asyncio')
async def test_cabinet_command_opens_the_cabinet_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com/', raising=False)
    message = MagicMock()
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()

    await menu.cmd_cabinet(message, _make_user(), state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
    button = message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.text == '👤 Личный кабинет'
    assert button.web_app.url == 'https://cabinet.example.com'


@pytest.mark.anyio('asyncio')
async def test_cabinet_command_fails_closed_without_a_cabinet_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '', raising=False)
    message = MagicMock()
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()

    await menu.cmd_cabinet(message, _make_user(), state)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs.get('reply_markup') is None


def test_command_menu_keeps_existing_commands_and_adds_cabinet() -> None:
    commands_ru, commands_en = get_command_menus()

    assert [(command.command, command.description) for command in commands_ru] == [
        ('start', '🔄 Перезагрузить бота'),
        ('cabinet', '👤 Личный кабинет'),
        ('language', '🌐 Язык'),
        ('support', '🛠️ Техподдержка'),
    ]
    assert [(command.command, command.description) for command in commands_en] == [
        ('start', '🔄 Restart bot'),
        ('cabinet', '👤 Personal account'),
        ('language', '🌐 Language'),
        ('support', '🛠️ Support'),
    ]

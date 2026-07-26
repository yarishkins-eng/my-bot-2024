"""Telegram command menus shared by bot setup and focused tests."""

from aiogram.types import BotCommand


def get_command_menus() -> tuple[list[BotCommand], list[BotCommand]]:
    """Return localized command menus for Telegram's private-chat command list."""
    commands_ru = [
        BotCommand(command='start', description='🔄 Перезагрузить бота'),
        BotCommand(command='cabinet', description='👤 Личный кабинет'),
        BotCommand(command='language', description='🌐 Язык'),
        BotCommand(command='support', description='🛠️ Техподдержка'),
    ]
    commands_en = [
        BotCommand(command='start', description='🔄 Restart bot'),
        BotCommand(command='cabinet', description='👤 Personal account'),
        BotCommand(command='language', description='🌐 Language'),
        BotCommand(command='support', description='🛠️ Support'),
    ]
    return commands_ru, commands_en

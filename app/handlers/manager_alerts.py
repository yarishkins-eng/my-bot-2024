"""Narrow group-only setup commands for the manager alert forum."""

from aiogram import Dispatcher, types
from aiogram.enums import ChatType
from aiogram.filters import Command

from app.config import settings
from app.services.manager_alert_service import ManagerAlertSettingsService, ManagerAlertTopic


_TOPIC_BY_ARGUMENT: dict[str, ManagerAlertTopic] = {
    'tickets': ManagerAlertTopic.TICKETS,
    'subscriptions': ManagerAlertTopic.SUBSCRIPTIONS,
    'payments': ManagerAlertTopic.PAYMENTS,
    'reports': ManagerAlertTopic.REPORTS,
    'service': ManagerAlertTopic.SERVICE_STATUS,
    'marketing': ManagerAlertTopic.MARKETING,
}


def is_manager_alert_setup_command(text: str | None) -> bool:
    # `split()` на пустой строке отдаёт пустой список, поэтому `[0]` нельзя брать
    # вслепую: в группу приходят и сообщения без текста (фото, стикер, служебное
    # событие форума — например «создана тема»). До 22.08.2026 каждое такое
    # сообщение роняло трейсбек в лог боевого бота.
    words = (text or '').strip().split(maxsplit=1)
    if not words:
        return False
    command = words[0].lower().split('@', maxsplit=1)[0]
    return command in {'/manager_alert_bind', '/manager_alert_status'}


def _is_owner_in_forum_topic(message: types.Message) -> bool:
    return bool(
        message.from_user
        and settings.is_admin(message.from_user.id)
        and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and message.is_topic_message
        and message.message_thread_id
    )


async def bind_manager_alert_topic(message: types.Message) -> None:
    """Bind the current forum topic without exposing raw chat/topic IDs."""
    if not _is_owner_in_forum_topic(message):
        return

    parts = (message.text or '').split(maxsplit=1)
    topic = _TOPIC_BY_ARGUMENT.get(parts[1].strip().lower() if len(parts) == 2 else '')
    if not topic:
        # Список собирается из самого маршрута: подсказка не может разойтись с
        # тем, что команда реально принимает (owner уже упёрся в это однажды).
        known = ', '.join(_TOPIC_BY_ARGUMENT)
        await message.answer(f'Укажи категорию: {known}.')
        return

    saved = ManagerAlertSettingsService.bind_topic(
        chat_id=message.chat.id,
        topic=topic,
        thread_id=message.message_thread_id,
    )
    if not saved:
        await message.answer('Не удалось сохранить: все темы должны быть в одной группе менеджера.')
        return

    await message.answer(f'✅ Тема «{topic.value}» подключена. Этот ответ подтверждает права бота на отправку.')


async def show_manager_alert_status(message: types.Message) -> None:
    if not _is_owner_in_forum_topic(message):
        return

    configured = ManagerAlertSettingsService.configured_topics()
    lines = ['<b>Маршрутизация уведомлений менеджера</b>', '']
    for argument, topic in _TOPIC_BY_ARGUMENT.items():
        mark = '✅' if topic in configured else '⚪️'
        lines.append(f'{mark} {argument}')
    await message.answer('\n'.join(lines), parse_mode='HTML')


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(bind_manager_alert_topic, Command('manager_alert_bind'))
    dp.message.register(show_manager_alert_status, Command('manager_alert_status'))

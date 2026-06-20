"""Funnel-меню: уведомления и «память» сообщения-меню для аккуратного обновления.

- send_funnel_trial_menu: после активации триала шлёт пользователю меню активного
  триала и удаляет предыдущее (ставшее мусором) сообщение-меню новичка.
- remember_funnel_menu_message: вызывается на местах показа меню новичка — сохраняет
  message_id в Redis, чтобы потом это сообщение можно было удалить.

Всё best-effort: под флагом FUNNEL_MENU_ENABLED, только cabinet-режим и telegram-юзеры,
любая ошибка подавляется и не ломает основной поток (активацию триала / показ меню).
"""

import redis.asyncio as aioredis
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Ключ Redis: id последнего показанного меню новичка (для удаления при апгрейде до триала).
_MENU_MSG_KEY = 'funnel:newbie_menu_msg:{}'
_MENU_MSG_TTL = 7200  # 2 часа — окно, в которое разумно ждать активации триала

_redis_client: aioredis.Redis | None = None
_redis_initialized: bool = False


def _get_redis() -> aioredis.Redis | None:
    """Возвращает общий Redis-клиент (тот же инстанс, что у остальных сервисов)."""
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    try:
        _redis_client = aioredis.from_url(settings.REDIS_URL)
    except Exception as exc:
        logger.warning('Не удалось создать Redis-клиент для funnel-меню', error=exc)
        _redis_client = None
    _redis_initialized = True
    return _redis_client


def _funnel_enabled() -> bool:
    return bool(getattr(settings, 'FUNNEL_MENU_ENABLED', False)) and settings.is_cabinet_mode()


async def remember_funnel_menu_message(user, message) -> None:
    """Запоминает message_id показанного меню новичка (если это funnel-новичок).

    Вызывать на местах отправки главного меню. ``message`` — отправленное сообщение
    (для answer/send_photo — результат отправки; для edit-на-месте — callback.message,
    у которого id не меняется в logo-режиме). Гейтится на состояние NEWBIE, чтобы не
    запомнить чужое сообщение.
    """
    telegram_id = getattr(user, 'telegram_id', None)
    message_id = getattr(message, 'message_id', None)
    if not (_funnel_enabled() and telegram_id and message_id):
        return
    try:
        from app.utils.funnel_state import FunnelState, classify_funnel_state

        if classify_funnel_state(user) != FunnelState.NEWBIE:
            return
        client = _get_redis()
        if client is None:
            return
        await client.set(_MENU_MSG_KEY.format(telegram_id), int(message_id), ex=_MENU_MSG_TTL)
    except Exception as exc:  # best-effort — не мешаем показу меню
        logger.debug('remember_funnel_menu_message failed', error=exc)


async def _delete_remembered_menu(bot, telegram_id: int) -> None:
    """Удаляет ранее запомненное меню новичка (best-effort) и чистит ключ."""
    try:
        client = _get_redis()
        if client is None:
            return
        key = _MENU_MSG_KEY.format(telegram_id)
        raw = await client.get(key)
        await client.delete(key)
        if not raw:
            return
        message_id = int(raw)
        try:
            await bot.delete_message(chat_id=telegram_id, message_id=message_id)
        except Exception as exc:  # сообщение могло быть удалено/старше 48ч — это норма
            logger.debug('Не удалось удалить старое меню новичка', error=exc, message_id=message_id)
    except Exception as exc:
        logger.debug('_delete_remembered_menu failed', error=exc)


async def send_funnel_trial_menu(user) -> None:
    """Шлёт пользователю меню активного триала (3 кнопки) после активации.

    Дополнительно удаляет предыдущее меню новичка (2 кнопки), если оно было запомнено.
    Ничего не делает, если funnel-меню выключено, бот не в cabinet-режиме или у
    пользователя нет telegram_id (email-only). Ошибки логируются, но не пробрасываются.
    """
    if not (_funnel_enabled() and getattr(user, 'telegram_id', None)):
        return

    try:
        from app.bot_factory import create_bot
        from app.keyboards.inline import build_funnel_menu_keyboard
        from app.localization.texts import get_texts
        from app.utils.funnel_state import FunnelState

        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        keyboard = build_funnel_menu_keyboard(FunnelState.TRIAL_ACTIVE, language, texts)
        if keyboard is None:
            return

        text = texts.t('FUNNEL_TRIAL_ACTIVATED', '🎉 Готово! Пробный период активирован.')
        bot = create_bot()
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            # Старое меню новичка теперь мусор — удаляем (best-effort).
            await _delete_remembered_menu(bot, user.telegram_id)
        finally:
            await bot.session.close()
    except Exception as exc:  # авто-обновление не критично — логируем и идём дальше
        logger.warning('Не удалось отправить funnel-меню после активации триала', error=exc)

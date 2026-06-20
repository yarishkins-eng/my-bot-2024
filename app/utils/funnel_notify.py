"""Отправка пользователю обновлённого funnel-меню после события (активация триала).

Единая точка: вызывается из всех путей активации триала (cabinet-роут мини-аппа,
webapi/miniapp, при необходимости — бот-хендлер), чтобы меню в Telegram обновилось
сразу, не дожидаясь /start. Безопасно: под флагом, только cabinet-режим и telegram-юзеры,
любая ошибка подавляется и не ломает активацию триала.
"""

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


async def send_funnel_trial_menu(user) -> None:
    """Шлёт пользователю меню активного триала (3 кнопки) после активации.

    Ничего не делает, если funnel-меню выключено, бот не в cabinet-режиме или у
    пользователя нет telegram_id (email-only). Ошибки логируются, но не пробрасываются.
    """
    if not (
        getattr(settings, 'FUNNEL_MENU_ENABLED', False)
        and settings.is_cabinet_mode()
        and getattr(user, 'telegram_id', None)
    ):
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
        finally:
            await bot.session.close()
    except Exception as exc:  # авто-обновление не критично — логируем и идём дальше
        logger.warning('Не удалось отправить funnel-меню после активации триала', error=exc)

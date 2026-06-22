"""Funnel-меню: уведомления и «память» сообщения-меню для аккуратного обновления.

- send_funnel_trial_menu: после активации триала шлёт меню активного триала.
- send_funnel_subscriber_menu: после активации ПЛАТНОЙ подписки (оплата/админ-выдача/
  автоплатёж) шлёт меню платного подписчика — чтобы оно обновилось не дожидаясь /start.
- remember_funnel_menu_message: на местах показа funnel-меню сохраняет message_id, чтобы
  при следующем переходе состояния старое (ставшее мусором) меню можно было удалить.

Всё best-effort: под флагом FUNNEL_MENU_ENABLED, только cabinet-режим и telegram-юзеры,
любая ошибка подавляется и НЕ ломает основной поток (активацию/оплату/показ меню).

ВАЖНО (для вызывающих send_funnel_subscriber_menu): объект ``user`` должен иметь СВЕЖУЮ
подписку на момент вызова (после коммита сделать ``await db.refresh(user, ['subscriptions'])``),
иначе get_subscriber_state прочитает старую/пустую подписку и пришлёт неверное меню.
"""

import redis.asyncio as aioredis
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Ключ Redis: id последнего показанного funnel-меню (для удаления при переходе состояния).
# Общий для всех funnel-состояний (новичок/триал/платный), чтобы корректно убирать старое
# меню при ЛЮБОМ переходе (новичок→триал, триал→платный, истёк→активен и т.д.).
_MENU_MSG_KEY = 'funnel:menu_msg:{}'
_MENU_MSG_TTL = 7200  # 2 часа — окно, в которое разумно ждать перехода состояния

_redis_client: aioredis.Redis | None = None
_redis_initialized: bool = False


def _get_redis() -> aioredis.Redis | None:
    """Возвращает кэшированный Redis-клиент (свой инстанс этого модуля, с таймаутами).

    Подключается к тому же settings.REDIS_URL / БД, что и остальные сервисы, поэтому
    запись (из бот-хендлера) и чтение (из webapi) видят одни и те же ключи.
    """
    global _redis_client, _redis_initialized
    if _redis_initialized:
        return _redis_client
    try:
        # Таймауты, чтобы зависший Redis не тормозил горячий путь /start.
        _redis_client = aioredis.from_url(
            settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2
        )
    except Exception as exc:
        logger.warning('Не удалось создать Redis-клиент для funnel-меню', error=exc)
        _redis_client = None
    _redis_initialized = True
    return _redis_client


def _funnel_enabled() -> bool:
    return bool(getattr(settings, 'FUNNEL_MENU_ENABLED', False)) and settings.is_cabinet_mode()


async def _remember_menu_message_id(telegram_id: int, message_id: int) -> None:
    """Сохраняет id показанного funnel-меню в Redis (best-effort)."""
    try:
        client = _get_redis()
        if client is None:
            return
        await client.set(_MENU_MSG_KEY.format(telegram_id), int(message_id), ex=_MENU_MSG_TTL)
    except Exception as exc:  # best-effort
        logger.debug('_remember_menu_message_id failed', error=exc)


async def remember_funnel_menu_message(user, message) -> None:
    """Запоминает message_id показанного funnel-меню (любое funnel-состояние).

    Вызывать на местах отправки главного меню. ``message`` — отправленное сообщение
    (для answer/send_photo — результат отправки; для edit-на-месте — callback.message,
    у которого id не меняется в logo-режиме). НЕ запоминает обычное (не-funnel) меню
    (состояние OTHER) — чтобы случайно не удалить чужое сообщение.
    """
    telegram_id = getattr(user, 'telegram_id', None)
    message_id = getattr(message, 'message_id', None)
    if not (_funnel_enabled() and telegram_id and message_id):
        return
    try:
        from app.utils.funnel_state import FunnelState, classify_funnel_state

        # Запоминаем только funnel-меню (новичок/триал/платный). Для OTHER показывается
        # обычное меню — его трогать не нужно.
        if classify_funnel_state(user) == FunnelState.OTHER:
            return
        await _remember_menu_message_id(telegram_id, message_id)
    except Exception as exc:  # best-effort — не мешаем показу меню
        logger.debug('remember_funnel_menu_message failed', error=exc)


async def _delete_remembered_menu(bot, telegram_id: int) -> None:
    """Удаляет ранее запомненное funnel-меню (best-effort) и чистит ключ."""
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
            logger.debug('Не удалось удалить старое funnel-меню', error=exc, message_id=message_id)
    except Exception as exc:
        logger.debug('_delete_remembered_menu failed', error=exc)


async def send_funnel_trial_menu(user) -> None:
    """Шлёт пользователю меню активного триала (3 кнопки) после активации.

    Дополнительно удаляет предыдущее меню (новичка), если оно было запомнено, и
    запоминает новое — чтобы его, в свою очередь, убрать при следующем переходе.
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
            sent = await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            # Старое меню новичка теперь мусор — удаляем; новое запоминаем.
            await _delete_remembered_menu(bot, user.telegram_id)
            if sent is not None:
                await _remember_menu_message_id(user.telegram_id, sent.message_id)
        finally:
            await bot.session.close()
    except Exception as exc:  # авто-обновление не критично — логируем и идём дальше
        logger.warning('Не удалось отправить funnel-меню после активации триала', error=exc)


async def notify_subscriber_menu(db, user) -> None:
    """Безопасная обёртка для точек активации платной подписки: освежает подписку и
    шлёт меню подписчика. Полностью best-effort — НЕ бросает в платёжный/активационный поток.

    Использовать ПОСЛЕ коммита подписки: ``await notify_subscriber_menu(db, user)``.
    """
    try:
        # Свежая подписка обязательна — иначе get_subscriber_state прочитает старую.
        await db.refresh(user, ['subscriptions'])
    except Exception as exc:
        logger.debug('notify_subscriber_menu refresh failed', error=exc)
    await send_funnel_subscriber_menu(user)  # сам best-effort


async def send_funnel_subscriber_menu(user) -> None:
    """Шлёт меню платного подписчика после активации платной подписки (без /start).

    Состояние и клавиатуру берёт из get_subscriber_state(user) по СВЕЖЕЙ user.subscription:
    - вернёт None (и ничего не отправит), если это триал (is_trial=True), флаги воронки/
      подписчик-меню выключены, мультитариф или нет активной платной — это нужное поведение
      (бесплатный «премиум» через «Сменить тариф» меню не получает);
    - иначе пришлёт PAID_ACTIVE/EXPIRING/EXPIRED-меню и уберёт старое.

    🔴 Вызывающий ОБЯЗАН передать user со свежей подпиской (после коммита —
    ``await db.refresh(user, ['subscriptions'])``). Best-effort: ошибки не пробрасываются.
    """
    if not (_funnel_enabled() and getattr(user, 'telegram_id', None)):
        return

    try:
        from app.bot_factory import create_bot
        from app.keyboards.inline import build_funnel_menu_keyboard
        from app.localization.texts import get_texts
        from app.utils.funnel_state import get_subscriber_state

        state, _sub = get_subscriber_state(user)
        if state is None:
            return  # не платный подписчик (триал/флаги выкл/мультитариф) — меню не шлём

        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        keyboard = build_funnel_menu_keyboard(state, language, texts)
        if keyboard is None:
            return

        text = texts.t('FUNNEL_SUBSCRIPTION_ACTIVE', '✅ Подписка активна! Вот твоё меню:')
        bot = create_bot()
        try:
            sent = await bot.send_message(
                chat_id=user.telegram_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML',
            )
            await _delete_remembered_menu(bot, user.telegram_id)
            if sent is not None:
                await _remember_menu_message_id(user.telegram_id, sent.message_id)
        finally:
            await bot.session.close()
    except Exception as exc:  # авто-обновление не критично — логируем и идём дальше
        logger.warning('Не удалось отправить меню подписчика после активации', error=exc)

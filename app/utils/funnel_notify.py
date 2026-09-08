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

import time

import redis.asyncio as aioredis
import structlog

from app.config import settings
from app.utils.scenario_media import cache_scenario_media_file_id, get_scenario_media


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
        _redis_client = aioredis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
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


async def clear_funnel_menu(user) -> None:
    """Убирает устаревшее funnel-меню из чата при ПОТЕРЕ доступа (обнуление подписки).

    Зеркало к send_funnel_*: там при ПОЛУЧЕНИИ доступа шлём новое меню и удаляем старое.
    Здесь пользователь доступ потерял — старое меню подписчика/триала стало мусором
    (неверные «Моя ссылка»/«Продлить»). Навязчиво слать меню новичка не нужно — корректное
    соберётся при следующем /start; просто удаляем устаревшее сообщение. Best-effort:
    под флагом FUNNEL_MENU_ENABLED, только telegram-юзеры, ошибки подавляются.
    """
    telegram_id = getattr(user, 'telegram_id', None)
    if not (_funnel_enabled() and telegram_id):
        return
    try:
        from app.bot_factory import create_bot

        bot = create_bot()
        try:
            await _delete_remembered_menu(bot, telegram_id)
        finally:
            await bot.session.close()
    except Exception as exc:  # best-effort — не мешаем сбросу подписки
        logger.debug('clear_funnel_menu failed', error=exc)


async def notify_trial_menu(db, user) -> None:
    """Обновляет свежую подписку и best-effort шлёт меню активного триала."""
    try:
        await db.refresh(user, ['subscriptions'])
    except Exception as exc:
        logger.debug('notify_trial_menu refresh failed', error=exc)
    await send_funnel_trial_menu(user)


async def send_funnel_trial_menu(user) -> None:
    """Шлёт пользователю меню активного триала после активации.

    Дополнительно удаляет предыдущее меню (новичка), если оно было запомнено, и
    запоминает новое — чтобы его, в свою очередь, убрать при следующем переходе.
    Ничего не делает, если funnel-меню выключено, бот не в cabinet-режиме или у
    пользователя нет telegram_id (email-only). Ошибки логируются, но не пробрасываются.
    Вызывающий, которому нужен актуальный ряд ссылки сразу после выдачи, должен
    использовать ``notify_trial_menu(db, user)``.

    Вызывается из ВСЕХ мест активации триала одинаково (React-кабинет, legacy
    Telegram mini-app, админ-выдача). Раньше тут был параметр ``source`` с ранним
    выходом при ``source='cabinet'`` — глушил пуш «чтобы не дублировать» экран
    кабинета. Это была ошибка: webview кабинета и сообщение-меню в чате Telegram —
    РАЗНЫЕ поверхности, и у пользователя в чате оставалась устаревшая кнопка
    «Попробовать бесплатно». Глушилку убрали — меню обновляется отовсюду.
    """
    if not (_funnel_enabled() and getattr(user, 'telegram_id', None)):
        return

    try:
        from app.bot_factory import create_bot
        from app.keyboards.inline import build_funnel_menu_keyboard
        from app.localization.texts import get_texts
        from app.utils.funnel_state import FunnelState
        from app.utils.subscription_link_access import get_user_subscription_with_available_link

        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        keyboard = build_funnel_menu_keyboard(
            FunnelState.TRIAL_ACTIVE,
            language,
            texts,
            show_connection_link=get_user_subscription_with_available_link(user) is not None,
        )
        if keyboard is None:
            return

        text = texts.t('FUNNEL_TRIAL_ACTIVATED', '🎉 Готово! Пробный период активирован.')
        bot = create_bot()
        try:
            media = get_scenario_media('trial_active', language) if settings.ENABLE_LOGO_MODE else None
            if media is None:
                sent = await bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
            else:
                sent = await bot.send_photo(
                    chat_id=user.telegram_id,
                    photo=media,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode='HTML',
                )
                cache_scenario_media_file_id('trial_active', language, sent)
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
        from app.utils.subscription_link_access import has_available_subscription_link

        state, _sub = get_subscriber_state(user)
        if state is None:
            return  # не платный подписчик (триал/флаги выкл/мультитариф) — меню не шлём

        language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(language)
        keyboard = build_funnel_menu_keyboard(
            state,
            language,
            texts,
            show_connection_link=has_available_subscription_link(_sub),
        )
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


# ---------------------------------------------------------------------------
# Добор следующего шага онбординга для пришедших по реферальной ссылке
# ---------------------------------------------------------------------------
# 🔴 Зачем это есть. Приветствие реферала несёт кнопку «Дальше», и следующий шаг —
# экран с бесплатным пробным — по нажатию. Кто не нажал, не видел его НИКОГДА:
# автосообщений для человека без подписки у бота нет ни одного, все фоновые выборки
# идут от таблицы подписок. Добор через N минут снимает эту цену: кнопка становится
# «пропустить ожидание», а не единственной дверью.
#
# Список ожидающих — в Redis, а не в памяти процесса: деплой бота идёт 7–10 минут и
# перезапускает процесс, а Redis живёт в отдельном контейнере и переживает это.
_ONBOARDING_DUE_KEY = 'referral_onboarding_due'
_ONBOARDING_SHOWN_PREFIX = 'referral_onboarding_shown:'
_ONBOARDING_SHOWN_TTL = 30 * 24 * 3600
# Насколько запись может опоздать и всё ещё быть отправленной. Дольше — человек уже забыл,
# что он вообще куда-то переходил: экран онбординга будет выглядеть письмом из ниоткуда.
# Деплой бота идёт 7-10 минут и в это окно укладывается с запасом.
_ONBOARDING_MAX_LATENESS = 3600


async def schedule_referral_onboarding_followup(telegram_id: int) -> bool:
    """Поставить человека в очередь на добор следующего шага. True — поставлен."""
    delay = settings.get_referral_onboarding_followup_seconds()
    if not delay or not telegram_id:
        return False
    client = _get_redis()
    if client is None:
        return False
    try:
        await client.zadd(_ONBOARDING_DUE_KEY, {str(telegram_id): time.time() + delay})
        return True
    except Exception as exc:
        logger.warning('Не удалось поставить добор онбординга в очередь', telegram_id=telegram_id, error=str(exc))
        return False


async def claim_referral_onboarding(telegram_id: int) -> bool:
    """Занять право показать следующий шаг. True — показывать нам.

    🔴 Захват АТОМАРНЫЙ (`SET NX`), и это несущая деталь. Кнопка и таймер живут в одном
    процессе и в одном событийном цикле: пока таймер ждёт ответа Telegram, нажатие успевает
    пройти мимо обычной проверки «уже показано» — и человек получает экран дважды. Проверка
    и установка одной командой этого не допускают.

    Redis недоступен — отвечаем True: лучше рискнуть копией, чем оставить человека без экрана.
    """
    if not telegram_id:
        return False
    client = _get_redis()
    if client is None:
        return True
    try:
        won = await client.set(f'{_ONBOARDING_SHOWN_PREFIX}{telegram_id}', '1', ex=_ONBOARDING_SHOWN_TTL, nx=True)
        if won:
            await client.zrem(_ONBOARDING_DUE_KEY, str(telegram_id))
        return bool(won)
    except Exception as exc:
        logger.warning('Не удалось занять показ онбординга', telegram_id=telegram_id, error=str(exc))
        return True


async def release_referral_onboarding(telegram_id: int) -> None:
    """Вернуть право показа: отправка не удалась, пусть попробует следующий."""
    client = _get_redis()
    if client is None or not telegram_id:
        return
    try:
        await client.delete(f'{_ONBOARDING_SHOWN_PREFIX}{telegram_id}')
    except Exception as exc:
        logger.debug('Не удалось освободить показ онбординга', telegram_id=telegram_id, error=str(exc))


def _has_live_subscription(user) -> bool:
    """Есть ли у человека ЖИВАЯ подписка.

    ⛔ Не «есть ли строка в таблице»: в этом проекте мусорные строки — задокументированный
    факт (мина FT). По строке-призраку человек не получил бы следующий шаг ни по таймеру,
    ни по кнопке. Набор состояний взят тот же, что в главном меню.
    """
    subs = getattr(user, 'subscriptions', None) or []
    return any(getattr(s, 'is_active', False) or getattr(s, 'actual_status', None) == 'limited' for s in subs)


async def process_due_referral_onboarding_followups(bot, limit: int = 20) -> int:
    """Прислать следующий шаг тем, у кого истекло ожидание. Возвращает число отправленных."""
    if not settings.get_referral_onboarding_followup_seconds():
        return 0
    client = _get_redis()
    if client is None:
        return 0

    now = time.time()
    try:
        due = await client.zrangebyscore(_ONBOARDING_DUE_KEY, '-inf', now, start=0, num=limit, withscores=True)
    except Exception as exc:
        logger.warning('Не удалось прочитать очередь добора онбординга', error=str(exc))
        return 0
    if not due:
        return 0

    from app.database.crud.user import get_user_by_telegram_id
    from app.database.database import AsyncSessionLocal
    from app.handlers.start import send_onboarding_menu

    sent = 0
    for raw, due_at in due:
        member = raw.decode() if isinstance(raw, bytes) else raw
        try:
            telegram_id = int(member)
        except (TypeError, ValueError):
            # Мусор в очереди: без снятия он лежит первым по сроку и глушит добор всем.
            logger.warning('Мусорная запись в очереди добора онбординга, снимаю', member=str(member))
            try:
                await client.zrem(_ONBOARDING_DUE_KEY, member)
            except Exception as exc:
                logger.error('Не удалось снять мусорную запись очереди добора', error=str(exc))
            continue

        if now - float(due_at) > _ONBOARDING_MAX_LATENESS:
            logger.info('Добор онбординга просрочен, не отправляю', telegram_id=telegram_id)
            try:
                await client.zrem(_ONBOARDING_DUE_KEY, member)
            except Exception as exc:
                logger.debug('Не удалось снять просроченную запись', error=str(exc))
            continue

        if not await claim_referral_onboarding(telegram_id):
            continue

        try:
            async with AsyncSessionLocal() as db:
                user = await get_user_by_telegram_id(db, telegram_id)
                if user is None:
                    continue
                if _has_live_subscription(user):
                    # Своё меню он получил при активации. Право показа возвращаем: если он
                    # всё-таки нажмёт кнопку под приветствием, экран обязан прийти.
                    await release_referral_onboarding(telegram_id)
                    continue
                await send_onboarding_menu(bot, telegram_id, db, user)
            sent += 1
            logger.info('Онбординг реферала: следующий шаг дослан по таймеру', telegram_id=telegram_id)
        except Exception as exc:
            await release_referral_onboarding(telegram_id)
            logger.error('Не удалось дослать следующий шаг онбординга', telegram_id=telegram_id, error=str(exc))

    return sent

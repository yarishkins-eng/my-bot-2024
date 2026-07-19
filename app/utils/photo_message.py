import asyncio
from contextlib import suppress

import structlog
from aiogram import types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import InaccessibleMessage, InputMediaPhoto

from app.config import settings

from .message_patch import (
    LOGO_PATH,
    _cache_logo_file_id,
    append_privacy_hint,
    caption_exceeds_telegram_limit,
    get_logo_media,
    is_privacy_restricted_error,
    is_qr_message,
    prepare_privacy_safe_kwargs,
)


logger = structlog.get_logger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 0.5


def _resolve_media(message: types.Message):
    if isinstance(message, InaccessibleMessage):
        return get_logo_media()
    if settings.ENABLE_LOGO_MODE and not is_qr_message(message):
        return get_logo_media()
    if message.photo and not is_qr_message(message):
        return message.photo[-1].file_id
    return get_logo_media()


def _get_language(callback: types.CallbackQuery) -> str | None:
    try:
        user = callback.from_user
        if user and getattr(user, 'language_code', None):
            return user.language_code
    except AttributeError:
        pass
    return None


def _build_base_kwargs(keyboard: types.InlineKeyboardMarkup | None, parse_mode: str | None):
    kwargs: dict[str, object] = {}
    if parse_mode is not None:
        kwargs['parse_mode'] = parse_mode
    if keyboard is not None:
        kwargs['reply_markup'] = keyboard
    return kwargs


async def safe_edit_or_resend(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    """Безопасно отредактировать текст сообщения или отправить новое при ошибке.

    Если edit_text() не работает (например, для фото-уведомлений или старых сообщений),
    удаляет исходное и отправляет новое сообщение.

    Args:
        message: Целевое сообщение.
        text: Текст для отправки/редактирования.
        reply_markup: Клавиатура (опционально).
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        # Контент не изменился (повторное нажатие кнопки) — ничего не делаем,
        # иначе будем без нужды пересоздавать сообщение и спамить чат.
        if 'message is not modified' in str(error).lower():
            return
        # Уведомление-фото или недоступное сообщение: edit_text не работает
        # Удаляем исходное и отправляем новое
        with suppress(TelegramAPIError):
            await message.delete()
        await message.answer(text, reply_markup=reply_markup)


async def _answer_text(
    callback: types.CallbackQuery,
    caption: str,
    keyboard: types.InlineKeyboardMarkup | None,
    parse_mode: str | None,
    error: TelegramBadRequest | None = None,
) -> types.Message | None:
    language = _get_language(callback)
    kwargs = _build_base_kwargs(keyboard, parse_mode)

    if error and is_privacy_restricted_error(error):
        caption = append_privacy_hint(caption, language)
        kwargs = prepare_privacy_safe_kwargs(kwargs)

    kwargs.setdefault('parse_mode', parse_mode or 'HTML')

    return await callback.message.answer(
        caption,
        **kwargs,
    )


async def edit_or_answer_photo(
    callback: types.CallbackQuery,
    caption: str,
    keyboard: types.InlineKeyboardMarkup,
    parse_mode: str | None = 'HTML',
    *,
    force_text: bool = False,
) -> types.Message | None:
    """Возвращает фактически показанное сообщение (отправленное/отредактированное)
    или None, если показать не удалось. Возврат нужен, чтобы вызывающий код мог
    запомнить message_id реального меню (см. funnel-удаление старого меню)."""
    resolved_parse_mode = parse_mode or 'HTML'

    # Если сообщение недоступно, отправляем новое сообщение
    if isinstance(callback.message, InaccessibleMessage):
        try:
            if settings.ENABLE_LOGO_MODE and LOGO_PATH.exists():
                result = await callback.message.answer_photo(
                    photo=get_logo_media(),
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
                _cache_logo_file_id(result)
                return result
            return await callback.message.answer(
                caption,
                reply_markup=keyboard,
                parse_mode=resolved_parse_mode,
            )
        except Exception as e:
            logger.warning('Не удалось отправить новое сообщение для InaccessibleMessage', e=e)
            try:
                return await callback.message.answer(
                    caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
            except Exception:
                return None

    # Если режим логотипа выключен или требуется текстовое сообщение — работаем текстом
    if force_text or not settings.ENABLE_LOGO_MODE:
        try:
            if callback.message.photo:
                await callback.message.delete()
                return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
            await callback.message.edit_text(
                caption,
                reply_markup=keyboard,
                parse_mode=resolved_parse_mode,
            )
            return callback.message
        except TelegramForbiddenError:
            logger.debug('Пользователь заблокировал бота, пропускаем')
            return None
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                return callback.message
            try:
                await callback.message.delete()
            except Exception:
                pass
            return await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)

    # Если текст слишком длинный для caption — отправим как текст
    if caption_exceeds_telegram_limit(caption):
        try:
            if callback.message.photo:
                await callback.message.delete()
            return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
        except TelegramForbiddenError:
            logger.debug('Пользователь заблокировал бота, пропускаем')
            return None
        except TelegramBadRequest as error:
            return await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)

    media = _resolve_media(callback.message)

    # Logo file unavailable (missing / directory bind-mount) — fall back to text.
    # See #586617: this used to surface as IsADirectoryError on every callback.
    if media is None:
        try:
            await callback.message.delete()
        except Exception:
            pass
        return await _answer_text(callback, caption, keyboard, resolved_parse_mode)

    # Retry logic для сетевых ошибок
    for attempt in range(MAX_RETRIES):
        try:
            await callback.message.edit_media(
                InputMediaPhoto(media=media, caption=caption, parse_mode=(parse_mode or 'HTML')),
                reply_markup=keyboard,
            )
            return callback.message  # Успешно — отредактировано на месте, id не изменился
        except TelegramNetworkError as net_error:
            if attempt < MAX_RETRIES - 1:
                logger.warning(
                    'Сетевая ошибка edit_media, повторная попытка',
                    attempt=attempt + 1,
                    MAX_RETRIES=MAX_RETRIES,
                    net_error=net_error,
                )
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            logger.error('Сетевая ошибка edit_media после попыток', MAX_RETRIES=MAX_RETRIES, net_error=net_error)
            # После всех попыток — фоллбек на текст
            try:
                await callback.message.delete()
            except Exception:
                pass
            return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
        except OSError as os_error:
            # Logo file became unreadable mid-flight (deleted/replaced by directory).
            # No point retrying — fall back to text. See #586617.
            logger.error(
                'Не удалось прочитать логотип для edit_media — фоллбек на текст',
                os_error=str(os_error),
            )
            try:
                await callback.message.delete()
            except Exception:
                pass
            return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — молча игнорируем
            logger.debug('Пользователь заблокировал бота, пропускаем edit_media')
            return None
        except TelegramBadRequest as error:
            if 'message is not modified' in str(error).lower():
                # Контент тот же — это не ошибка; не пересоздаём сообщение (иначе «прыгает» вниз чата).
                return callback.message
            if is_privacy_restricted_error(error):
                try:
                    await callback.message.delete()
                except Exception:
                    pass
                return await _answer_text(callback, caption, keyboard, resolved_parse_mode, error)
            # Фоллбек: если не удалось обновить фото — отправим текст
            try:
                await callback.message.delete()
            except Exception:
                pass
            logo_media = get_logo_media()
            if logo_media is None:
                return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
            try:
                # Отправим как фото с логотипом
                result = await callback.message.answer_photo(
                    photo=logo_media,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode=resolved_parse_mode,
                )
                _cache_logo_file_id(result)
                return result
            except (TelegramBadRequest, TelegramForbiddenError) as photo_error:
                return await _answer_text(callback, caption, keyboard, resolved_parse_mode, photo_error)
            except Exception:
                # Последний фоллбек — обычный текст
                return await _answer_text(callback, caption, keyboard, resolved_parse_mode)
    return None

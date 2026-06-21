import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command

from app.database.models import User
from app.keyboards.inline import get_support_keyboard
from app.localization.texts import get_texts
from app.services.support_settings_service import SupportSettingsService
from app.utils.photo_message import edit_or_answer_photo


logger = structlog.get_logger(__name__)


async def show_support_info(callback: types.CallbackQuery, db_user: User):
    get_texts(db_user.language)
    support_info = SupportSettingsService.get_support_info_text(db_user.language)
    await edit_or_answer_photo(
        callback=callback,
        caption=support_info,
        keyboard=get_support_keyboard(db_user.language),
        parse_mode='HTML',
    )
    await callback.answer()


async def cmd_support(message: types.Message, db_user: User):
    """Команда /support (из меню команд ☰) — тот же экран поддержки, что и кнопка menu_support.

    Переиспользует текст SupportSettingsService и клавиатуру get_support_keyboard (тикеты + связаться).
    Команда присылает НОВОЕ сообщение, поэтому message.answer (downstream-обработчики фото-безопасны).
    """
    texts = get_texts(db_user.language)
    try:
        support_enabled = SupportSettingsService.is_support_menu_enabled()
    except Exception:
        support_enabled = True
    if not support_enabled:
        await message.answer(texts.t('SUPPORT_UNAVAILABLE', '⚙️ Поддержка временно недоступна.'))
        return
    support_info = SupportSettingsService.get_support_info_text(db_user.language)
    await message.answer(
        support_info,
        reply_markup=get_support_keyboard(db_user.language),
        parse_mode='HTML',
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_support_info, F.data == 'menu_support')
    dp.message.register(cmd_support, Command('support'))

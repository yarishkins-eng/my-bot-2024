import hashlib
import json
from html import escape as html_escape
from pathlib import Path

import qrcode
import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_referral_keyboard
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.states import ReferralWithdrawalStates
from app.utils.photo_message import edit_or_answer_photo
from app.utils.user_utils import (
    get_detailed_referral_list,
    get_effective_referral_commission_percent,
    get_referral_analytics,
    get_user_referral_summary,
)


logger = structlog.get_logger(__name__)


async def show_referral_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, включена ли реферальная программа
    if not settings.is_referral_program_enabled():
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('REFERRAL_PROGRAM_DISABLED', 'Реферальная программа отключена'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    summary = await get_user_referral_summary(db, db_user.id)

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)
    referral_text = (
        texts.t('REFERRAL_PROGRAM_TITLE', '🎁 <b>Приглашай друзей — бонус получите оба</b>')
        + '\n\n'
        + texts.t(
            'REFERRAL_REWARDS_HEADER',
            'Дай другу свою ссылку. Когда он подключится и впервые пополнит баланс:',
        )
    )

    if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
        referral_text += '\n' + texts.t(
            'REFERRAL_REWARD_NEW_USER',
            ' • другу — <b>{bonus}</b> на старт (при пополнении от {minimum})',
        ).format(
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
        )

    if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0:
        referral_text += '\n' + texts.t(
            'REFERRAL_REWARD_INVITER',
            ' • тебе — <b>{bonus}</b> на баланс',
        ).format(bonus=texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS))

    if settings.REFERRAL_MAX_COMMISSION_PAYMENTS > 0:
        commission_line = texts.t(
            'REFERRAL_REWARD_COMMISSION_LIMITED',
            ' • и дальше <b>{percent}%</b> с первых {max_payments} пополнений',
        ).format(
            percent=get_effective_referral_commission_percent(db_user),
            max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        )
    else:
        commission_line = texts.t(
            'REFERRAL_REWARD_COMMISSION',
            ' • и дальше <b>{percent}%</b> с каждого его пополнения',
        ).format(percent=get_effective_referral_commission_percent(db_user))

    referral_text += '\n' + commission_line + '\n\n'

    # Ссылка (нажми — копируется), в <code> (не экранируем всю строку — #634720)
    referral_text += (
        texts.t('REFERRAL_BOT_LINK_TITLE', '🔗 <b>Твоя ссылка для друга</b> (нажми — скопируется):')
        + f'\n<code>{html_escape(bot_referral_link)}</code>\n\n'
    )

    # Подсказка про QR (он за кнопкой ниже)
    referral_text += texts.t(
        'REFERRAL_INVITE_FOOTER',
        '📱 Не можешь прислать ссылку? Покажи другу QR — кнопка ниже.',
    )

    # Короткая статистика — только если уже есть приглашённые (D3)
    if summary['invited_count'] > 0:
        referral_text += '\n\n' + texts.t(
            'REFERRAL_SHORT_STATS',
            '📊 Приглашено: {invited} · из них пополнили: {paid} · заработано: {total}',
        ).format(
            invited=summary['invited_count'],
            paid=summary['paid_referrals_count'],
            total=texts.format_price(summary['total_earned_kopeks']),
        )

    await edit_or_answer_photo(
        callback,
        referral_text,
        get_referral_keyboard(db_user.language),
    )
    await callback.answer()


async def show_referral_qr(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    await callback.answer()

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)

    qr_dir = Path('data') / 'referral_qr'
    qr_dir.mkdir(parents=True, exist_ok=True)

    link_hash = hashlib.md5(bot_referral_link.encode()).hexdigest()[:8]
    file_path = qr_dir / f'{db_user.id}_{link_hash}.png'
    if not file_path.exists():
        img = qrcode.make(bot_referral_link)
        img.save(file_path)

    photo = FSInputFile(file_path)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    caption = texts.t(
        'REFERRAL_QR_BOT_LINK',
        '📱 Покажи другу этот QR — он наведёт камеру и попадёт к тебе по ссылке.\nУдобно, когда переслать ссылку не получается.\n\n🔗 {link}',
    ).format(link=bot_referral_link)

    try:
        await callback.message.edit_media(
            types.InputMediaPhoto(media=photo, caption=caption),
            reply_markup=keyboard,
        )
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error).lower():
            await callback.message.delete()
            await callback.message.answer_photo(
                photo,
                caption=caption,
                reply_markup=keyboard,
            )


async def show_detailed_referral_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)

    referrals_data = await get_detailed_referral_list(db, db_user.id, limit=10, offset=(page - 1) * 10)

    # Устаревшая кнопка «стр.N»: если список сократился и страница пуста — вернуться на первую.
    if not referrals_data['referrals'] and page > 1:
        await show_detailed_referral_list(callback, db_user, db, 1)
        return

    if not referrals_data['referrals']:
        await edit_or_answer_photo(
            callback,
            texts.t(
                'REFERRAL_LIST_EMPTY',
                '📋 Пока никого.\n\nКинь другу ссылку — как он пополнит баланс, тебе упадёт бонус 💎',
            ),
            types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
            ),
            parse_mode=None,
        )
        await callback.answer()
        return

    summary = await get_user_referral_summary(db, db_user.id)
    text = (
        texts.t(
            'REFERRAL_LIST_HEADER',
            '👥 <b>Твои приглашённые</b> (стр. {current}/{total})',
        ).format(
            current=referrals_data['current_page'],
            total=referrals_data['total_pages'],
        )
        + '\n\n'
        + texts.t(
            'REFERRAL_LIST_STATS_HEADER',
            '💎 Заработано: <b>{total}</b> (за месяц {month})',
        ).format(
            total=texts.format_price(summary['total_earned_kopeks']),
            month=texts.format_price(summary['month_earned_kopeks']),
        )
        + '\n\n'
        + texts.t('REFERRAL_LIST_LEGEND', '💰 — пополнял · ⏳ — ещё нет')
        + '\n\n'
    )

    for i, referral in enumerate(referrals_data['referrals'], 1):
        topup_emoji = '💰' if referral['has_made_first_topup'] else '⏳'

        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_HEADER',
                '{index}. {status} <b>{name}</b>',
            ).format(index=i, status=topup_emoji, name=html_escape(str(referral['full_name'] or '')))
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_TOPUPS',
                '   {emoji} Пополнений: {count}',
            ).format(emoji='🔄', count=referral['topups_count'])
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_EARNED',
                '   💎 С него заработал: {amount}',
            ).format(amount=texts.format_price(referral['total_earned_kopeks']))
            + '\n'
        )
        days_reg = referral['days_since_registration']
        if days_reg <= 0:
            joined_line = texts.t('REFERRAL_LIST_ITEM_JOINED_TODAY', '   📅 Пришёл сегодня')
        elif days_reg == 1:
            joined_line = texts.t('REFERRAL_LIST_ITEM_JOINED_YESTERDAY', '   📅 Пришёл вчера')
        else:
            joined_line = texts.t(
                'REFERRAL_LIST_ITEM_REGISTERED',
                '   📅 Пришёл {days} дн. назад',
            ).format(days=days_reg)
        text += joined_line + '\n'

        text += '\n'

    keyboard = []
    nav_buttons = []

    if referrals_data['has_prev']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_PREV_PAGE', '⬅️ Назад'), callback_data=f'referral_list_page_{page - 1}'
            )
        )

    if referrals_data['has_next']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_NEXT_PAGE', 'Вперед ➡️'), callback_data=f'referral_list_page_{page + 1}'
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def show_referral_analytics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    analytics = await get_referral_analytics(db, db_user.id)

    text = texts.t('REFERRAL_ANALYTICS_TITLE', '📊 <b>Аналитика рефералов</b>') + '\n\n'

    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_HEADER',
            '💰 <b>Доходы по периодам:</b>',
        )
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_TODAY',
            '• Сегодня: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['today']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_WEEK',
            '• За неделю: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['week']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_MONTH',
            '• За месяц: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['month']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_QUARTER',
            '• За квартал: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['quarter']))
        + '\n\n'
    )

    if analytics['top_referrals']:
        text += (
            texts.t(
                'REFERRAL_ANALYTICS_TOP_TITLE',
                '🏆 <b>Топ-{count} рефералов:</b>',
            ).format(count=len(analytics['top_referrals']))
            + '\n'
        )
        for i, ref in enumerate(analytics['top_referrals'], 1):
            text += (
                texts.t(
                    'REFERRAL_ANALYTICS_TOP_ITEM',
                    '{index}. {name}: {amount} ({count} начислений)',
                ).format(
                    index=i,
                    name=html_escape(str(ref['referral_name'] or '')),
                    amount=texts.format_price(ref['total_earned_kopeks']),
                    count=ref['earnings_count'],
                )
                + '\n'
            )
        text += '\n'

    text += texts.t(
        'REFERRAL_ANALYTICS_FOOTER',
        '📈 Продолжайте развивать свою реферальную сеть!',
    )

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
        ),
    )
    await callback.answer()


async def create_invite_message(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)

    bonus_block = ''
    if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
        bonus_block = '\n\n' + texts.t(
            'REFERRAL_INVITE_BONUS',
            '💸 При пополнении баланса от {minimum} тебе ещё {bonus} бонусом сверху!',
        ).format(
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
        )

    # Ссылку оборачиваем в <code>: при тапе по <blockquote> для копирования Telegram
    # сохраняет содержимое <code> в буфере, но ВЫБРАСЫВАЕТ авто-линкнутые сырые URL —
    # поэтому из скопированного приглашения ссылка выпадала (#634720). Экранируем прозу
    # сами и подставляем ссылку уже в <code>, а не html_escape-им всю собранную строку.
    #
    # cabinet_block оставлен ПУСТЫМ намеренно: с редизайна «Привести друга» (22.06.2026,
    # fb38cbb4) бот-приглашение несёт только телеграм-ссылку; кабинет-ссылка живёт в
    # реф-экране кабинета. Плейсхолдер {cabinet_block} всё ещё есть во всех локалях
    # REFERRAL_INVITE_TEXT — поэтому переменную сохраняем (иначе .format() упадёт).
    cabinet_block = ''

    invite_template = texts.t(
        'REFERRAL_INVITE_TEXT',
        '🎉 Подключайся к ВПН Тёпло по моей ссылке!{bonus_block}\n\n👇 Жми:\n{link}{cabinet_block}',
    )
    invite_html = html_escape(invite_template).format(
        bonus_block=html_escape(bonus_block),
        link=f'<code>{html_escape(bot_referral_link)}</code>',
        cabinet_block=cabinet_block,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')],
        ]
    )

    await edit_or_answer_photo(
        callback,
        (
            texts.t('REFERRAL_INVITE_CREATED_TITLE', '📝 <b>Приглашение создано!</b>')
            + '\n\n'
            + texts.t(
                'REFERRAL_INVITE_CREATED_INSTRUCTION',
                'Нажми на текст ниже — скопируется:',
            )
            + '\n\n'
            f'<blockquote>{invite_html}</blockquote>'
        ),
        keyboard,
    )
    await callback.answer()


async def show_withdrawal_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает информацию о выводе реферального баланса."""
    texts = get_texts(db_user.language)

    if not settings.is_referral_withdrawal_enabled():
        await callback.answer(texts.t('REFERRAL_WITHDRAWAL_DISABLED', 'Функция вывода отключена'), show_alert=True)
        return

    # Получаем детальную статистику баланса
    stats = await referral_withdrawal_service.get_referral_balance_stats(db, db_user.id)
    min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
    cooldown_days = settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS

    # Проверяем возможность вывода
    can_request, reason, _stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)

    text = texts.t('REFERRAL_WITHDRAWAL_TITLE', '💸 <b>Вывод реферального баланса</b>') + '\n\n'

    # Показываем детальную статистику
    text += referral_withdrawal_service.format_balance_stats_for_user(stats, texts)
    text += '\n'

    text += (
        texts.t('REFERRAL_WITHDRAWAL_MIN_AMOUNT', '📊 Минимальная сумма: <b>{amount}</b>').format(
            amount=texts.format_price(min_amount)
        )
        + '\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_COOLDOWN', '⏱ Частота вывода: раз в <b>{days}</b> дней').format(days=cooldown_days)
        + '\n\n'
    )

    keyboard = []

    if can_request:
        text += texts.t('REFERRAL_WITHDRAWAL_READY', '✅ Вы можете запросить вывод средств') + '\n'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_REQUEST_BUTTON', '📝 Оформить заявку'),
                    callback_data='referral_withdrawal_start',
                )
            ]
        )
    else:
        text += f'❌ {html_escape(str(reason))}\n'

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(callback, text, types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


async def start_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начинает процесс оформления заявки на вывод."""
    texts = get_texts(db_user.language)

    # Повторная проверка
    can_request, reason, wd_stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)
    if not can_request:
        await callback.answer(reason, show_alert=True)
        return

    available = wd_stats.get('available_total', 0) if wd_stats else 0

    # Сохраняем доступный баланс в состоянии
    await state.update_data(available_balance=available)
    await state.set_state(ReferralWithdrawalStates.waiting_for_amount)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_AMOUNT', '💸 Введите сумму для вывода в рублях\n\nДоступно: <b>{amount}</b>'
    ).format(amount=texts.format_price(available))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_ALL', f'Вывести всё ({available / 100:.0f}₽)'),
                    callback_data=f'referral_withdrawal_amount_{available}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_withdrawal_amount(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод суммы для вывода."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    available = data.get('available_balance', 0)

    try:
        # Парсим сумму (в рублях)
        amount_text = message.text.strip().replace(',', '.').replace('₽', '').replace(' ', '')
        amount_rubles = float(amount_text)
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer(texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT', '❌ Введите положительную сумму'))
            return

        min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
        if amount_kopeks < min_amount:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_MIN_ERROR', '❌ Минимальная сумма: {amount}').format(
                    amount=texts.format_price(min_amount)
                )
            )
            return

        if amount_kopeks > available:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_INSUFFICIENT', '❌ Недостаточно средств. Доступно: {amount}').format(
                    amount=texts.format_price(available)
                )
            )
            return

        # Сохраняем сумму и переходим к вводу реквизитов
        await state.update_data(withdrawal_amount=amount_kopeks)
        await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

        text = texts.t(
            'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
            '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                    )
                ]
            ]
        )

        await message.answer(text, reply_markup=keyboard)

    except ValueError:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT', '❌ Введите корректную сумму'))


async def process_withdrawal_amount_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Обрабатывает выбор суммы для вывода через кнопку."""
    texts = get_texts(db_user.language)

    # Получаем сумму из callback_data
    amount_kopeks = int(callback.data.split('_')[-1])

    # Сохраняем сумму и переходим к вводу реквизитов
    await state.update_data(withdrawal_amount=amount_kopeks)
    await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
        '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ]
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_payment_details(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод реквизитов и показывает подтверждение."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = message.text.strip()

    if len(payment_details) < 10:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_DETAILS_TOO_SHORT', '❌ Реквизиты слишком короткие'))
        return

    # Сохраняем реквизиты
    await state.update_data(payment_details=payment_details)
    await state.set_state(ReferralWithdrawalStates.confirming)

    text = texts.t('REFERRAL_WITHDRAWAL_CONFIRM_TITLE', '📋 <b>Подтверждение заявки</b>') + '\n\n'
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_AMOUNT', '💰 Сумма: <b>{amount}</b>').format(
            amount=texts.format_price(amount_kopeks)
        )
        + '\n\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_DETAILS', '💳 Реквизиты:\n<code>{details}</code>').format(
            details=html_escape(payment_details)
        )
        + '\n\n'
    )
    text += texts.t('REFERRAL_WITHDRAWAL_CONFIRM_WARNING', '⚠️ После отправки заявка будет рассмотрена администрацией')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_CONFIRM_BUTTON', '✅ Подтвердить'),
                    callback_data='referral_withdrawal_confirm',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await message.answer(text, reply_markup=keyboard)


async def confirm_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Подтверждает и создаёт заявку на вывод."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = data.get('payment_details', '')

    await state.clear()

    # Создаём заявку
    request, error = await referral_withdrawal_service.create_withdrawal_request(
        db, db_user.id, amount_kopeks, payment_details
    )

    if error:
        await callback.answer(f'❌ {error}', show_alert=True)
        return

    # Отправляем уведомление админам
    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    user_id_display = html_escape(str(db_user.telegram_id or db_user.email or f'#{db_user.id}'))
    safe_name = html_escape(db_user.full_name or 'Без имени')
    safe_details = html_escape(payment_details)
    admin_text = f"""
🔔 <b>Новая заявка на вывод #{request.id}</b>

👤 Пользователь: {safe_name}
🆔 ID: <code>{user_id_display}</code>
💰 Сумма: <b>{amount_kopeks / 100:.0f}₽</b>

💳 Реквизиты:
<code>{safe_details}</code>

{referral_withdrawal_service.format_analysis_for_admin(analysis)}
"""

    # Формируем клавиатуру - кнопка профиля только для Telegram-пользователей
    keyboard_rows = [
        [
            types.InlineKeyboardButton(text='✅ Одобрить', callback_data=f'admin_withdrawal_approve_{request.id}'),
            types.InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_withdrawal_reject_{request.id}'),
        ]
    ]
    if db_user.telegram_id:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text='👤 Профиль пользователя', callback_data=f'admin_user_{db_user.telegram_id}'
                )
            ]
        )
    admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_admin_notification(
            admin_text, reply_markup=admin_keyboard, category=NotificationCategory.PARTNERS
        )
    except Exception as e:
        logger.error('Ошибка отправки уведомления админам о заявке на вывод', error=e)

    # Уведомление в топик, если настроено
    topic_id = settings.REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID
    if topic_id and settings.ADMIN_NOTIFICATIONS_CHAT_ID:
        try:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_NOTIFICATIONS_CHAT_ID,
                message_thread_id=topic_id,
                text=admin_text,
                reply_markup=admin_keyboard,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления в топик о заявке на вывод', error=e)

    # Отвечаем пользователю
    text = texts.t(
        'REFERRAL_WITHDRAWAL_SUCCESS',
        '✅ <b>Заявка #{id} создана!</b>\n\n'
        'Сумма: <b>{amount}</b>\n\n'
        'Ваша заявка будет рассмотрена администрацией. '
        'Мы уведомим вас о результате.',
    ).format(id=request.id, amount=texts.format_price(amount_kopeks))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def cancel_withdrawal_request(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Отменяет процесс создания заявки на вывод."""
    await state.clear()
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('CANCELLED', 'Отменено'))

    # Возвращаем в меню партнёрки
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )
    await edit_or_answer_photo(callback, texts.t('REFERRAL_WITHDRAWAL_CANCELLED', '❌ Заявка отменена'), keyboard)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_info, F.data == 'menu_referrals')

    dp.callback_query.register(create_invite_message, F.data == 'referral_create_invite')

    dp.callback_query.register(show_referral_qr, F.data == 'referral_show_qr')

    dp.callback_query.register(show_detailed_referral_list, F.data == 'referral_list')

    dp.callback_query.register(show_referral_analytics, F.data == 'referral_analytics')

    async def handle_referral_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
        page = int(callback.data.split('_')[-1])
        await show_detailed_referral_list(callback, db_user, db, page)

    dp.callback_query.register(handle_referral_list_page, F.data.startswith('referral_list_page_'))

    # Хендлеры вывода реферального баланса
    dp.callback_query.register(show_withdrawal_info, F.data == 'referral_withdrawal')

    dp.callback_query.register(start_withdrawal_request, F.data == 'referral_withdrawal_start')

    dp.callback_query.register(process_withdrawal_amount_callback, F.data.startswith('referral_withdrawal_amount_'))

    dp.callback_query.register(confirm_withdrawal_request, F.data == 'referral_withdrawal_confirm')

    dp.callback_query.register(cancel_withdrawal_request, F.data == 'referral_withdrawal_cancel')

    # Обработка текстового ввода суммы
    dp.message.register(process_withdrawal_amount, ReferralWithdrawalStates.waiting_for_amount)

    # Обработка текстового ввода реквизитов
    dp.message.register(process_payment_details, ReferralWithdrawalStates.waiting_for_payment_details)

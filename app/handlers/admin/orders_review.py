"""Пункт 4.4. Разбор заказов, застрявших на `operator_review`.

До этого модуля разобрать такой заказ было НЕЧЕМ: списка не существовало ни в
чат-админке, ни в веб-админке, ни в кабинете. Заказ при этом держит клиента
(`operator_hold` отбивает новую покупку) и запирает смену серверов тарифа.

Здесь ровно три действия: показать список, вернуть деньги на баланс, закрыть заказ.
🔴 Выдачи подписки тут НЕТ и она сюда не просится (решение владельца 18.08.2026):
после возврата денег клиент покупает сам, штатным путём этапа 4.0, который
синхронизируется с панелью. Второй путь выдачи — это ровно тот «новый слой»,
которым проект уже ломали.
"""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import SubscriptionCheckout, User
from app.services.device_first_checkout_service import (
    close_operator_review_checkout,
    count_operator_review_checkouts,
    list_operator_review_checkouts,
    operator_review_card,
    refund_operator_review_checkout,
    refundable_amount_kopeks,
)
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

MENU_CALLBACK = 'admin_orders_review'
CARD_PREFIX = 'adm_or_card:'
ASK_PREFIX = 'adm_or_ask:'
GO_PREFIX = 'adm_or_go:'

# Сколько заказов показываем списком. Больше в одно сообщение Telegram не влезет
# кнопками, а листалка на разборе аварий — лишний механизм: если заказов больше
# двадцати, это уже не разбор, а инцидент.
LIST_LIMIT = 20

# Два действия, и вопрос перед каждым. Тексты держим здесь, а не в ветках обработчика:
# ветка «спросить» и ветка «сделать» обязаны говорить об одном и том же действии.
ACTIONS = {
    'refund': {
        'button': '💸 Вернуть деньги на баланс',
        'confirm': '💸 Да, вернуть',
        'question': (
            '💸 <b>Вернуть {amount} по заказу {order}?</b>\n\n'
            'Это сумма, которую клиент реально заплатил, — не цена по прайсу.\n\n'
            '🔴 <b>Сначала проверьте, не вернули ли вы эти деньги руками в Platega.</b>\n'
            'Такой возврат база не видит, и тогда клиент получит их дважды.\n\n'
            'Деньги лягут ему на баланс в боте — он сможет купить подписку сам.'
        ),
        'after': '\n\nТеперь заказ можно закрыть — это снимет замок с клиента.',
    },
    'close': {
        'button': '✅ Закрыть заказ',
        'confirm': '✅ Да, закрыть',
        # 🔴 Оба обещания прежней версии были неверны, и обе неточности нашёл прогон
        # сценария: разблокировка тарифа зависит от ДРУГИХ заказов на нём, а «найти будет
        # труднее» — на самом деле «этой кнопкой уже никогда»: список берёт только
        # `operator_review`, а поиска заказа по номеру в проекте нет нигде.
        'question': (
            '✅ <b>Закрыть заказ {order}?</b>\n\n'
            'Этот заказ перестанет мешать клиенту оформить новую покупку.\n\n'
            '🔴 <b>Сначала верните деньги, если они были.</b> После закрытия заказ уйдёт из '
            'списка навсегда, и вернуть по нему деньги этой кнопкой будет уже нельзя — '
            'останется только Platega и правка баланса вручную.'
        ),
        'after': '',
    },
}


def _parse(data: str, prefix: str) -> tuple[str, int] | None:
    """`adm_or_go:refund:101` → `('refund', 101)`."""
    try:
        action, raw_id = data[len(prefix) :].split(':', 1)
        return (action, int(raw_id)) if action in ACTIONS else None
    except (AttributeError, TypeError, ValueError):
        return None


async def _load(db: AsyncSession, checkout_id: int) -> SubscriptionCheckout | None:
    # 🔴 `populate_existing`: в общей сессии `db.get` отдаёт объект из кэша, а решение
    # про деньги по протухшей копии — это возврат за уже выданный заказ (урок 4.1).
    return await db.get(SubscriptionCheckout, checkout_id, populate_existing=True)


def _rows(*buttons: tuple[str, str]) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=text, callback_data=data)] for text, data in buttons]
    )


async def _edit(callback: types.CallbackQuery, text: str, markup: types.InlineKeyboardMarkup) -> None:
    await callback.message.edit_text(
        text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    await callback.answer()


@admin_required
@error_handler
async def show_orders_review_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Список заказов, ждущих человека."""
    checkouts = await list_operator_review_checkouts(db, limit=LIST_LIMIT)
    total = await count_operator_review_checkouts(db)
    if not checkouts:
        await _edit(
            callback,
            '🧾 <b>Заказы на разборе</b>\n\nСейчас разбирать нечего — ни одного застрявшего заказа.',
            _rows(('⬅️ Назад', 'admin_panel')),
        )
        return

    lines = [f'🧾 <b>Заказы на разборе: {total}</b>', '']
    if total > len(checkouts):
        # Листалки нет намеренно: если заказов больше двадцати, это инцидент, а не разбор.
        # Но и молчать нельзя — иначе экран выглядит полным списком, которым не является.
        lines += [f'Показаны {len(checkouts)} самых свежих. Остальные появятся, когда разберёте эти.', '']
    lines.append('Пока такой заказ висит, клиент чаще всего не может оформить новый.')
    buttons = []
    for item in checkouts:
        snapshot = item.sale_snapshot if isinstance(item.sale_snapshot, dict) else {}
        tariff_name = str(snapshot.get('tariff_name') or f'тариф {item.tariff_id}')
        # 🔴 Первые 8 знаков публичного номера — то же, что напечатано в тревоге строкой
        # «Заказ: ...». Без них по тревоге свой заказ в списке не найти: внутренний id
        # заказа и внутренний id клиента в тревоге не встречаются ни разу.
        public_head = str(item.public_id or '')[:8]
        buttons.append((f'{public_head} · {tariff_name} · клиент {item.user_id}', f'{CARD_PREFIX}{item.id}'))
    buttons.append(('⬅️ Назад', 'admin_panel'))
    await _edit(callback, '\n'.join(lines), _rows(*buttons))


@admin_required
@error_handler
async def show_order_card(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Карточка заказа: кто, за что, что с деньгами, и две кнопки."""
    try:
        checkout_id = int(callback.data[len(CARD_PREFIX) :])
    except (TypeError, ValueError):
        await callback.answer('Заказ не найден', show_alert=True)
        return
    checkout = await _load(db, checkout_id)
    if checkout is None:
        await callback.answer('Заказ не найден', show_alert=True)
        return
    # 🔴 P0, найден прогоном сценария. Инлайн-кнопка живёт в переписке вечно, и по старой
    # карточке владелец открывал УЖЕ закрытый заказ. Текст тревоги для не-`operator_review`
    # уходит в ветку «застряла выдача» и печатает «ЗАКАЗ ЗАВИС… бот продолжает пробовать
    # сам… разбирать руками не нужно и нечем» — про заказ, который никто не пробует и
    # деньги по которому не возвращены. Плюс рисовались обе кнопки, и обе отказывали
    # только после подтверждения. Такой заказ карточкой не показываем вовсе.
    if checkout.lifecycle_state != 'operator_review':
        await _edit(
            callback,
            f'✅ <b>Этот заказ уже разобран</b>\n\nСостояние: «{html.escape(str(checkout.lifecycle_state))}». '
            'Разбирать его больше не нужно, и кнопки по нему не работают.\n\n'
            '🔴 Если деньги по нему так и не вернулись клиенту, этой кнопкой их уже не вернуть: '
            'разберитесь в кабинете Platega и при необходимости пополните баланс клиента вручную '
            '(админ-панель → «Пользователи» → клиент → изменить баланс).',
            _rows(('⬅️ К списку', MENU_CALLBACK)),
        )
        return
    await _edit(
        callback,
        await operator_review_card(db, checkout),
        _rows(
            (ACTIONS['refund']['button'], f'{ASK_PREFIX}refund:{checkout.id}'),
            (ACTIONS['close']['button'], f'{ASK_PREFIX}close:{checkout.id}'),
            ('⬅️ К списку', MENU_CALLBACK),
        ),
    )


@admin_required
@error_handler
async def ask_confirmation(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Вопрос перед необратимым действием. Общий для обеих кнопок."""
    parsed = _parse(callback.data, ASK_PREFIX)
    checkout = await _load(db, parsed[1]) if parsed else None
    if parsed is None or checkout is None:
        await callback.answer('Заказ не найден', show_alert=True)
        return
    action = ACTIONS[parsed[0]]
    # 🔴 Сумму показываем ДО нажатия. Прежняя версия называла её только в ответе, то есть
    # владелец подтверждал выплату вслепую: цифра на карточке — цена по прайсу, а уйдёт
    # та, что доказана базой, и это разные поля.
    amount = ''
    if parsed[0] == 'refund':
        kopeks, refusal = await refundable_amount_kopeks(db, checkout)
        if kopeks <= 0:
            await _edit(
                callback,
                f'⚠️ <b>Возвращать нечего</b>\n\n{html.escape(refusal.capitalize())}.',
                _rows(('⬅️ К заказу', f'{CARD_PREFIX}{checkout.id}')),
            )
            return
        amount = settings.format_price(kopeks)
    await _edit(
        callback,
        action['question'].format(order=html.escape(str(checkout.public_id)), amount=amount),
        _rows(
            (action['confirm'], f'{GO_PREFIX}{parsed[0]}:{checkout.id}'),
            ('⬅️ Отмена', f'{CARD_PREFIX}{checkout.id}'),
        ),
    )


@admin_required
@error_handler
async def do_action(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Выполнить подтверждённое действие."""
    parsed = _parse(callback.data, GO_PREFIX)
    checkout = await _load(db, parsed[1]) if parsed else None
    if parsed is None or checkout is None:
        await callback.answer('Заказ не найден', show_alert=True)
        return
    name = parsed[0]
    if name == 'refund':
        done, message = await refund_operator_review_checkout(db, checkout=checkout, admin_user_id=db_user.id)
        if done:
            await _tell_client_about_refund(callback, db, checkout=checkout, verdict=message)
    else:
        done, message = await close_operator_review_checkout(db, checkout=checkout, admin_user_id=db_user.id)
    tail = ACTIONS[name]['after'] if done else ''
    # После закрытия карточка заказа уже неинтересна — он ушёл из списка.
    back = ('⬅️ К списку', MENU_CALLBACK) if name == 'close' and done else ('⬅️ К заказу', f'{CARD_PREFIX}{checkout.id}')
    await _edit(callback, f'{"✅" if done else "⚠️"} {html.escape(message)}{tail}', _rows(back))


async def _tell_client_about_refund(
    callback: types.CallbackQuery,
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    verdict: str,
) -> None:
    """Сказать клиенту, что деньги вернулись.

    🔴 Без этого он не узнаёт о возврате НИКАК: аутбокс пополнений шлёт только событие для
    кабинета и внешних вебхуков, а подписчиков на него в проекте ноль. Текст «Пополнение
    успешно» живёт в обработчиках платёжных систем, куда ручной возврат не заходит.
    Молчание здесь — это клиент с деньгами на балансе, который об этом не знает.
    """
    client = await db.get(User, checkout.user_id)
    # `telegram_id` намеренно nullable (email-only клиенты); отправка в None упадёт.
    if client is None or not client.telegram_id:
        return
    try:
        await callback.bot.send_message(
            client.telegram_id,
            f'💰 <b>Деньги вернулись на баланс</b>\n\n{html.escape(verdict)}\n\n'
            'Заказ, по которому мы их держали, оформлен не будет. Оформите новый — '
            'он оплатится с баланса.',
            parse_mode=ParseMode.HTML,
        )
    except Exception as error:  # клиент мог заблокировать бота — возврат от этого не отменяется
        logger.warning(
            'device_first_operator_refund_client_notice_failed',
            checkout_id=str(checkout.public_id),
            error=str(error),
        )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_orders_review_list, F.data == MENU_CALLBACK)
    dp.callback_query.register(show_order_card, F.data.startswith(CARD_PREFIX))
    dp.callback_query.register(ask_confirmation, F.data.startswith(ASK_PREFIX))
    dp.callback_query.register(do_action, F.data.startswith(GO_PREFIX))

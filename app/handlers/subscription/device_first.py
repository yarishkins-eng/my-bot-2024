"""Native Telegram device-first checkout (short, replay-safe ``df:`` callbacks)."""

from __future__ import annotations

import uuid
from datetime import datetime
from itertools import islice

from aiogram import F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.device_first_checkout_service import (
    DeviceFirstError,
    arm_checkout,
    build_purchase_options,
    cancel_checkout,
    confirm_checkout,
    create_checkout,
    get_open_checkout_for_user,
    get_owned_checkout,
    serialize_checkout,
)
from app.services.device_first_payment_service import (
    available_platega_methods_for_db,
    create_platega_attempt,
    get_pending_platega_attempt,
    platega_method_label,
)
from app.utils.photo_message import edit_or_answer_photo


def _en(user: User) -> bool:
    return user.language == 'en'


def _text(user: User, ru: str, en: str) -> str:
    return en if _en(user) else ru


def _money(user: User, kopeks: int) -> str:
    """Show whole-ruble amounts without a misleading `.00`; preserve real kopeks."""
    rubles, remainder = divmod(abs(kopeks), 100)
    value = f'{rubles:,}'.replace(',', ' ')
    if remainder:
        separator = '.' if _en(user) else ','
        value = f'{value}{separator}{remainder:02d}'
    return f'-{value}' if kopeks < 0 else value


def _device_label(user: User, limit: int) -> str:
    if _en(user):
        return f'{limit} device' if limit == 1 else f'{limit} devices'
    remainder_100 = limit % 100
    remainder_10 = limit % 10
    if 11 <= remainder_100 <= 14:
        word = 'устройств'
    elif remainder_10 == 1:
        word = 'устройство'
    elif 2 <= remainder_10 <= 4:
        word = 'устройства'
    else:
        word = 'устройств'
    return f'{limit} {word}'


def _days_label(user: User, days: int) -> str:
    if _en(user):
        return f'{days} day' if days == 1 else f'{days} days'
    remainder_100 = days % 100
    remainder_10 = days % 10
    if 11 <= remainder_100 <= 14:
        word = 'дней'
    elif remainder_10 == 1:
        word = 'день'
    elif 2 <= remainder_10 <= 4:
        word = 'дня'
    else:
        word = 'дней'
    return f'{days} {word}'


def _period_short_label(user: User, days: int) -> str:
    labels = {
        30: ('1 месяц', '1 month'),
        90: ('3 месяца', '3 months'),
        180: ('6 месяцев', '6 months'),
        365: ('1 год', '1 year'),
    }
    if days in labels:
        return _text(user, *labels[days])
    return _days_label(user, days)


def _period_summary_label(user: User, days: int) -> str:
    if days not in {30, 90, 180, 365}:
        return _days_label(user, days)
    return _text(
        user,
        f'{_period_short_label(user, days)} ({_days_label(user, days)})',
        f'{_period_short_label(user, days)} ({_days_label(user, days)})',
    )


def _price_for(options: dict, *, days: int, devices: int) -> int | None:
    for row in options.get('price_matrix', []):
        if row.get('period_days') != days:
            continue
        for item in row.get('prices', []):
            if item.get('device_limit') == devices:
                return item.get('price_kopeks')
    return None


def _chunks(values: list[int], size: int):
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _back(user: User, callback_data: str = 'back_to_menu') -> InlineKeyboardButton:
    return InlineKeyboardButton(text=_text(user, '‹ Назад', '‹ Back'), callback_data=callback_data)


def _main_menu(user: User) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_text(user, 'В главное меню', 'Main menu'),
        callback_data='back_to_menu',
    )


def _change_selection(user: User, checkout_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_text(user, '‹ Изменить выбор', '‹ Change selection'),
        callback_data=f'df:e:{checkout_id}',
    )


def _cancel_order(user: User, checkout_id: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_text(user, 'Отменить заказ', 'Cancel order'),
        callback_data=f'df:x:{checkout_id}',
    )


async def _answer_stale(callback: types.CallbackQuery, user: User) -> None:
    await callback.answer(
        _text(
            user,
            'Экран устарел. Я обновил варианты — выберите ещё раз.',
            'This screen is out of date. I refreshed the options; please choose again.',
        ),
        show_alert=True,
    )


async def _device_page(
    callback: types.CallbackQuery,
    user: User,
    state: FSMContext,
    options: dict,
    *,
    view_id: str,
    days: int,
    origin_callback: str,
) -> None:
    values = list(options['device_options'])
    rows = []
    # The ordinary 1–10-device tariff contract fits into two columns, so every
    # choice is visible on one Telegram screen and no faux pagination is used.
    for chunk in _chunks(values, 2):
        row = []
        for limit in chunk:
            price = _price_for(options, days=days, devices=limit)
            label = _device_label(user, limit)
            if price is not None:
                amount = _money(user, price)
                label = f'{label} · {"₽" if _en(user) else ""}{amount}{"" if _en(user) else " ₽"}'
            row.append(InlineKeyboardButton(text=label, callback_data=f'df:d:{view_id}:{limit}'))
        rows.append(row)
    rows.append([_back(user, 'df:start')])
    await state.update_data(
        df_options=options,
        df_view_id=view_id,
        df_days=days,
        df_origin_callback=origin_callback,
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=(
            f'📱 <b>{options["tariff"]["name"]}</b>\n\n'
            + _text(
                user,
                f'Срок: <b>{_period_summary_label(user, days)}</b>\n\nНа скольких устройствах будете пользоваться VPN?',
                f'Period: <b>{_period_summary_label(user, days)}</b>\n\nHow many devices will use the VPN?',
            )
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )


async def _period_page(
    callback: types.CallbackQuery,
    user: User,
    state: FSMContext,
    options: dict,
    *,
    view_id: str,
    origin_callback: str,
) -> None:
    rows = []
    for chunk in _chunks(list(options['period_options']), 2):
        row = []
        for days in chunk:
            label = _period_short_label(user, days)
            row.append(InlineKeyboardButton(text=label, callback_data=f'df:t:{view_id}:{days}'))
        rows.append(row)
    rows.append([_back(user, origin_callback)])
    await state.update_data(
        df_options=options,
        df_view_id=view_id,
        df_origin_callback=origin_callback,
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=(
            f'📱 <b>{options["tariff"]["name"]}</b>\n\n'
            + _text(user, '📅 Сначала выберите срок VPN.', '📅 First choose the VPN period.')
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )


async def show_device_first_entry(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    options: dict | None = None,
    origin_callback: str | None = None,
) -> bool:
    await callback.answer()
    options = options or await build_purchase_options(db, db_user)
    if not options.get('eligible'):
        return False
    view_id = uuid.uuid4().hex[:8]
    # ``df:start`` can arrive from a keyboard rendered before this deployment.
    # Do not preserve its stale funnel callback: both a fresh entry and device
    # Back must ultimately return to the main menu, never re-open this funnel.
    origin = origin_callback or 'back_to_menu'
    existing = await get_open_checkout_for_user(db, user_id=db_user.id)
    if existing is not None:
        await state.update_data(df_checkout_id=existing.public_id)
        if existing.lifecycle_state == 'draft':
            await _render_confirmation(
                callback,
                db_user,
                existing,
                tariff_name=options['tariff']['name'],
            )
        else:
            await _render_checkout(callback, db_user, db, existing)
        return True
    await _period_page(
        callback,
        db_user,
        state,
        options,
        view_id=view_id,
        origin_callback=origin,
    )
    return True


async def legacy_device_page(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    parts = (callback.data or '').split(':')
    data = await state.get_data()
    if len(parts) != 4 or parts[2] != data.get('df_view_id'):
        await _answer_stale(callback, db_user)
        options = await build_purchase_options(db, db_user)
        if options.get('eligible'):
            await _period_page(
                callback,
                db_user,
                state,
                options,
                view_id=uuid.uuid4().hex[:8],
                origin_callback=data.get('df_origin_callback') or 'back_to_menu',
            )
        return
    await callback.answer()
    if not data.get('df_days'):
        options = await build_purchase_options(db, db_user)
        if options.get('eligible'):
            await _period_page(
                callback,
                db_user,
                state,
                options,
                view_id=uuid.uuid4().hex[:8],
                origin_callback=data.get('df_origin_callback') or 'back_to_menu',
            )
        return
    await _device_page(
        callback,
        db_user,
        state,
        data['df_options'],
        view_id=parts[2],
        days=int(data['df_days']),
        origin_callback=data.get('df_origin_callback') or 'back_to_menu',
    )


async def choose_devices(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    parts = (callback.data or '').split(':')
    data = await state.get_data()
    options = data.get('df_options') or await build_purchase_options(db, db_user)
    if len(parts) != 4 or parts[2] != data.get('df_view_id'):
        await _answer_stale(callback, db_user)
        if options.get('eligible'):
            await _period_page(
                callback,
                db_user,
                state,
                options,
                view_id=uuid.uuid4().hex[:8],
                origin_callback=data.get('df_origin_callback') or 'back_to_menu',
            )
        return
    try:
        devices = int(parts[3])
    except ValueError:
        await _answer_stale(callback, db_user)
        return
    if devices not in options.get('device_options', []):
        await _answer_stale(callback, db_user)
        await _device_page(
            callback,
            db_user,
            state,
            options,
            view_id=uuid.uuid4().hex[:8],
            days=int(data['df_days']),
            origin_callback=data.get('df_origin_callback') or 'back_to_menu',
        )
        return
    await callback.answer()
    days = int(data['df_days'])
    try:
        checkout = await create_checkout(
            db,
            user=db_user,
            period_days=days,
            selected_device_limit=devices,
            source='telegram',
        )
    except DeviceFirstError as error:
        if error.code == 'open_checkout_exists':
            existing = await get_open_checkout_for_user(db, user_id=db_user.id)
            if existing is not None:
                await state.update_data(df_checkout_id=existing.public_id)
                if existing.lifecycle_state == 'draft':
                    await _render_confirmation(
                        callback,
                        db_user,
                        existing,
                        tariff_name=options['tariff']['name'],
                    )
                else:
                    await _render_checkout(callback, db_user, db, existing)
                return
        await _render_error(callback, db_user, error)
        return
    await state.update_data(df_checkout_id=checkout.public_id)
    await _render_confirmation(
        callback,
        db_user,
        checkout,
        tariff_name=options['tariff']['name'],
    )


async def choose_period(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    parts = (callback.data or '').split(':')
    data = await state.get_data()
    options = data.get('df_options') or await build_purchase_options(db, db_user)
    if len(parts) != 4 or parts[2] != data.get('df_view_id'):
        await _answer_stale(callback, db_user)
        if options.get('eligible'):
            await _period_page(
                callback,
                db_user,
                state,
                options,
                view_id=uuid.uuid4().hex[:8],
                origin_callback=data.get('df_origin_callback') or 'back_to_menu',
            )
        return
    try:
        days = int(parts[3])
    except ValueError:
        await _answer_stale(callback, db_user)
        return
    if days not in options.get('period_options', []):
        await _answer_stale(callback, db_user)
        await _period_page(
            callback,
            db_user,
            state,
            options,
            view_id=uuid.uuid4().hex[:8],
            origin_callback=data.get('df_origin_callback') or 'back_to_menu',
        )
        return
    await callback.answer()
    await _device_page(
        callback,
        db_user,
        state,
        options,
        view_id=uuid.uuid4().hex[:8],
        days=days,
        origin_callback=data.get('df_origin_callback') or 'back_to_menu',
    )


async def _render_confirmation(
    callback: types.CallbackQuery,
    user: User,
    checkout,
    *,
    tariff_name: str,
) -> None:
    checkout_id = checkout.public_id
    snapshot = serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)
    current_devices = snapshot['current_device_limit']
    if current_devices is not None and current_devices != checkout.selected_device_limit:
        device_change = _text(
            user,
            f'было {_device_label(user, current_devices)} → станет {_device_label(user, checkout.selected_device_limit)}',
            f'{_device_label(user, current_devices)} → {_device_label(user, checkout.selected_device_limit)}',
        )
    else:
        device_change = _device_label(user, checkout.selected_device_limit)
    end_date = datetime.fromisoformat(snapshot['estimated_end_at']).strftime('%d.%m.%Y')
    shortage = snapshot['shortage_kopeks'] or 0
    rows = [
        [
            InlineKeyboardButton(
                text=_text(user, '✅ Подтвердить конфигурацию', '✅ Confirm configuration'),
                callback_data=f'df:c:{checkout_id}',
            )
        ],
        [
            _change_selection(user, checkout_id),
            _cancel_order(user, checkout_id),
        ],
    ]
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            user,
            (
                '✅ <b>Проверьте заказ</b>\n\n'
                f'💎 Тариф: <b>{tariff_name}</b>\n'
                f'📱 Устройства: <b>{device_change}</b>\n'
                f'📅 Срок: {_period_summary_label(user, checkout.period_days)}\n'
                f'🏁 До: {end_date}\n'
                f'💳 Баланс: {_money(user, user.balance_kopeks)} ₽\n'
                f'💰 Итого: <b>{_money(user, checkout.quoted_price_kopeks)} ₽</b>\n'
                + (f'⚠️ Не хватает: {_money(user, shortage)} ₽\n' if shortage else '')
                + '\nДеньги ещё не списаны. Следующее подтверждение разрешит списать эту сумму.'
            ),
            (
                '✅ <b>Review your order</b>\n\n'
                f'💎 Tariff: <b>{tariff_name}</b>\n'
                f'📱 Devices: <b>{device_change}</b>\n'
                f'📅 Period: {_period_summary_label(user, checkout.period_days)}\n'
                f'🏁 Ends: {end_date}\n'
                f'💳 Balance: ₽{_money(user, user.balance_kopeks)}\n'
                f'💰 Total: <b>₽{_money(user, checkout.quoted_price_kopeks)}</b>\n'
                + (f'⚠️ Shortage: ₽{_money(user, shortage)}\n' if shortage else '')
                + '\nNothing has been charged. The next confirmation authorizes this amount.'
            ),
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )


def _checkout_id(callback: types.CallbackQuery) -> str | None:
    parts = (callback.data or '').split(':', 3)
    # Payment callbacks carry both the method and the checkout id
    # (``df:y:<method>:<checkout>``). All checkout-state callbacks carry
    # only the id (``df:s:<checkout>`` etc.). Never turn the former into a
    # malformed status callback such as ``df:s:sbp:<checkout>``.
    if len(parts) == 4 and parts[:2] == ['df', 'y']:
        return parts[3]
    return parts[2] if len(parts) == 3 else None


async def confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    del state
    await callback.answer()
    public_id = _checkout_id(callback)
    try:
        checkout = await get_owned_checkout(db, public_id=public_id or '', user_id=db_user.id, for_update=True)
        if checkout.lifecycle_state == 'draft':
            checkout = await confirm_checkout(db, checkout)
    except DeviceFirstError as error:
        await _render_error(callback, db_user, error)
        return

    if checkout.lifecycle_state != 'confirmed':
        await _render_checkout(callback, db_user, db, checkout)
        return

    await _render_arm_confirmation(callback, db_user, checkout)


async def _render_arm_confirmation(
    callback: types.CallbackQuery,
    user: User,
    checkout,
) -> None:
    """Render the deliberate second confirmation for a confirmed checkout."""
    shortage = max(0, checkout.max_price_kopeks - user.balance_kopeks)
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            user,
            (
                '🔐 <b>Заказ подтверждён</b>\n\n'
                f'К списанию: {_money(user, checkout.quoted_price_kopeks)} ₽.\n'
                'Списание произойдёт только после следующего нажатия.'
            ),
            (
                '🔐 <b>Order confirmed</b>\n\n'
                f'To charge: ₽{_money(user, checkout.quoted_price_kopeks)}.\n'
                'The charge happens only after the next tap.'
            ),
        ),
        keyboard=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            _text(
                                user,
                                f'Пополнить {_money(user, shortage)} ₽ и оформить',
                                f'Top up ₽{_money(user, shortage)} and subscribe',
                            )
                            if shortage
                            else _text(
                                user,
                                f'Списать {_money(user, checkout.quoted_price_kopeks)} ₽ и оформить',
                                f'Charge ₽{_money(user, checkout.quoted_price_kopeks)} and subscribe',
                            )
                        ),
                        callback_data=f'df:a:{checkout.public_id}',
                    )
                ],
                [
                    _change_selection(user, checkout.public_id),
                    _cancel_order(user, checkout.public_id),
                ],
            ]
        ),
        parse_mode='HTML',
    )


async def _render_checkout(callback: types.CallbackQuery, user: User, db: AsyncSession, checkout) -> None:
    result = serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)
    if result['ui_state'] == 'confirmation':
        await _render_arm_confirmation(callback, user, checkout)
        return
    if result['ui_state'] == 'awaiting_payment':
        shortage = result['shortage_kopeks'] or 0
        pending_attempt = None
        if getattr(checkout, 'id', None) is not None:
            pending_attempt = await get_pending_platega_attempt(db, checkout_id=checkout.id)
        if pending_attempt is not None:
            method = platega_method_label(pending_attempt.method_key, language=user.language)
            amount = _money(user, pending_attempt.requested_amount_kopeks)
            status_button = InlineKeyboardButton(
                text=_text(user, 'Проверить статус', 'Check status'),
                callback_data=f'df:s:{checkout.public_id}',
            )
            if pending_attempt.status == 'pending' and pending_attempt.redirect_url:
                caption = _text(
                    user,
                    (
                        '💳 <b>Счёт ожидает оплаты</b>\n\n'
                        f'Способ: <b>{method}</b>\n'
                        f'К оплате: <b>{amount} ₽</b>\n\n'
                        'Оплатите этот счёт или проверьте его статус. Новый счёт не создаётся, '
                        'чтобы не было двойной оплаты.'
                    ),
                    (
                        '💳 <b>Invoice awaiting payment</b>\n\n'
                        f'Method: <b>{method}</b>\n'
                        f'To pay: <b>₽{amount}</b>\n\n'
                        'Pay this invoice or check its status. A new invoice is blocked to prevent a duplicate payment.'
                    ),
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=_text(user, 'Перейти к оплате', 'Open payment'),
                                url=pending_attempt.redirect_url,
                            )
                        ],
                        [status_button],
                        [_cancel_order(user, checkout.public_id)],
                    ]
                )
            elif pending_attempt.status in {'creating', 'reconciliation'} or (
                pending_attempt.status == 'pending' and not pending_attempt.redirect_url
            ):
                caption = _text(
                    user,
                    (
                        '⏳ <b>Проверяем счёт</b>\n\n'
                        f'Способ: <b>{method}</b>\nК оплате: <b>{amount} ₽</b>\n\n'
                        'Не оплачивайте и не создавайте новый счёт, пока идёт проверка.'
                    ),
                    (
                        '⏳ <b>Checking the invoice</b>\n\n'
                        f'Method: <b>{method}</b>\nTo pay: <b>₽{amount}</b>\n\n'
                        'Do not pay or create another invoice while we check this one.'
                    ),
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [status_button],
                        [
                            InlineKeyboardButton(
                                text=_text(user, 'Связаться с поддержкой', 'Contact support'),
                                callback_data='menu_support',
                            )
                        ],
                        [_cancel_order(user, checkout.public_id)],
                    ]
                )
            else:
                caption = _text(
                    user,
                    '⏳ <b>Платёж обрабатывается</b>\n\nНе создавайте новый счёт. Обновите статус через несколько секунд.',
                    '⏳ <b>Payment is being processed</b>\n\nDo not create another invoice. Refresh the status in a few seconds.',
                )
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[status_button], [_main_menu(user)]])
        elif shortage:
            methods = await available_platega_methods_for_db(db, user)
            if methods:
                rows = [
                    [
                        InlineKeyboardButton(
                            text=_text(
                                user,
                                f'Пополнить через {platega_method_label(item["key"], language=user.language)}',
                                f'Top up with {platega_method_label(item["key"], language=user.language)}',
                            ),
                            callback_data=f'df:y:{item["key"]}:{checkout.public_id}',
                        )
                    ]
                    for item in methods
                ]
                rows.extend(
                    [
                        [
                            InlineKeyboardButton(
                                text=_text(user, 'Обновить баланс', 'Refresh balance'),
                                callback_data=f'df:s:{checkout.public_id}',
                            )
                        ],
                        [_change_selection(user, checkout.public_id), _cancel_order(user, checkout.public_id)],
                    ]
                )
                caption = _text(
                    user,
                    (
                        '💳 <b>Недостаточно средств</b>\n\n'
                        f'📱 {_device_label(user, checkout.selected_device_limit)} · '
                        f'📅 {_period_summary_label(user, checkout.period_days)}\n'
                        f'💰 Итого: {_money(user, checkout.quoted_price_kopeks)} ₽\n'
                        f'⚠️ Нужно доплатить: <b>{_money(user, shortage)} ₽</b>\n\n'
                        'Заказ сохранён. Выберите способ доплаты.'
                    ),
                    (
                        '💳 <b>Insufficient balance</b>\n\n'
                        f'📱 {_device_label(user, checkout.selected_device_limit)} · '
                        f'📅 {_period_summary_label(user, checkout.period_days)}\n'
                        f'💰 Total: ₽{_money(user, checkout.quoted_price_kopeks)}\n'
                        f'⚠️ Top up: <b>₽{_money(user, shortage)}</b>\n\n'
                        'Your order is saved. Choose a top-up method.'
                    ),
                )
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
            else:
                caption = _text(
                    user,
                    (
                        '💳 <b>Недостаточно средств</b>\n\n'
                        f'Нужно доплатить: <b>{_money(user, shortage)} ₽</b>.\n\n'
                        'Пополнение сейчас недоступно. Обновите баланс, если его пополнили другим способом, '
                        'или обратитесь в поддержку.'
                    ),
                    (
                        '💳 <b>Insufficient balance</b>\n\n'
                        f'Top up needed: <b>₽{_money(user, shortage)}</b>.\n\n'
                        'Top-up is currently unavailable. Refresh your balance if you used another method, '
                        'or contact support.'
                    ),
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=_text(user, 'Обновить баланс', 'Refresh balance'),
                                callback_data=f'df:s:{checkout.public_id}',
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text=_text(user, 'Связаться с поддержкой', 'Contact support'),
                                callback_data='menu_support',
                            )
                        ],
                        [_change_selection(user, checkout.public_id), _cancel_order(user, checkout.public_id)],
                    ]
                )
        else:
            caption = _text(
                user,
                (
                    '✅ <b>Баланс пополнен</b>\n\n'
                    f'📱 {_device_label(user, checkout.selected_device_limit)} · '
                    f'📅 {_period_summary_label(user, checkout.period_days)}\n'
                    f'💰 К списанию: {_money(user, checkout.quoted_price_kopeks)} ₽\n\n'
                    'Нажмите «Продолжить и оформить».'
                ),
                (
                    '✅ <b>Balance topped up</b>\n\n'
                    f'📱 {_device_label(user, checkout.selected_device_limit)} · '
                    f'📅 {_period_summary_label(user, checkout.period_days)}\n'
                    f'💰 To charge: ₽{_money(user, checkout.quoted_price_kopeks)}\n\n'
                    'Tap “Continue and subscribe”.'
                ),
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=_text(user, 'Продолжить и оформить', 'Continue and subscribe'),
                            callback_data=f'df:a:{checkout.public_id}',
                        )
                    ],
                    [_change_selection(user, checkout.public_id), _cancel_order(user, checkout.public_id)],
                ]
            )
    elif result['ui_state'] == 'processing':
        caption = _text(
            user,
            '⏳ <b>Оформляем VPN</b>\n\nПлатёж подтверждён. Заказ уже выполняется и отменить его нельзя.',
            '⏳ <b>Completing your VPN</b>\n\nPayment confirmed. The order is in progress and can no longer be cancelled.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Обновить статус', 'Refresh status'),
                        callback_data=f'df:s:{checkout.public_id}',
                    )
                ],
                [_main_menu(user)],
            ]
        )
    elif result['ui_state'] == 'provisioning':
        caption = _text(
            user,
            '⏳ <b>Настраиваем VPN</b>\n\nПлатёж учтён. Подключение появится после синхронизации.',
            '⏳ <b>Setting up VPN</b>\n\nPayment received. Connect will appear after synchronization.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Обновить статус', 'Refresh status'),
                        callback_data=f'df:s:{checkout.public_id}',
                    )
                ],
                [_main_menu(user)],
            ]
        )
    elif result['ui_state'] == 'ready':
        from app.utils.miniapp_buttons import build_cabinet_url

        connection_url = build_cabinet_url('/connection')
        button = (
            InlineKeyboardButton(
                text=_text(user, '🔗 Подключить VPN', '🔗 Connect VPN'),
                web_app=types.WebAppInfo(url=connection_url),
            )
            if connection_url
            else InlineKeyboardButton(
                text=_text(user, '🔗 Подключить VPN', '🔗 Connect VPN'),
                callback_data='open_subscription_link',
            )
        )
        caption = _text(user, '✅ <b>VPN готов</b>', '✅ <b>VPN is ready</b>')
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[button], [_main_menu(user)]])
    elif result['ui_state'] == 'cancelled':
        caption = _text(
            user,
            '🛑 <b>Заказ отменён</b>\n\nЭтот заказ больше не будет оформлен.',
            '🛑 <b>Order cancelled</b>\n\nThis order will not be completed.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=_text(user, 'Новый расчёт', 'New quote'), callback_data='df:start')],
                [_main_menu(user)],
            ]
        )
    elif result['ui_state'] in {'expired', 'reprice_required'}:
        caption = _text(
            user,
            '⌛ <b>Нужен новый расчёт</b>\n\nСрок или условия заказа изменились. Создайте новый расчёт.',
            '⌛ <b>A new quote is needed</b>\n\nThe order period or terms changed. Create a new quote.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=_text(user, 'Новый расчёт', 'New quote'), callback_data='df:start')],
                [_main_menu(user)],
            ]
        )
    elif result['ui_state'] == 'conflict':
        caption = _text(
            user,
            '⚠️ <b>Заказ нельзя продолжить</b>\n\nДанные подписки изменились. Создайте новый расчёт.',
            '⚠️ <b>This order cannot continue</b>\n\nSubscription data changed. Create a new quote.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=_text(user, 'Новый расчёт', 'New quote'), callback_data='df:start')],
                [_main_menu(user)],
            ]
        )
    elif result['ui_state'] == 'failed':
        caption = _text(
            user,
            '⚠️ <b>Не удалось завершить заказ</b>\n\nОбратитесь в поддержку, чтобы мы проверили его статус.',
            '⚠️ <b>We could not complete the order</b>\n\nContact support so we can check its status.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Связаться с поддержкой', 'Contact support'),
                        callback_data='menu_support',
                    )
                ],
                [_main_menu(user)],
            ]
        )
    else:
        caption = _text(
            user,
            '⚠️ Расчёт изменился. Создайте новый заказ — лишнего списания не было.',
            '⚠️ The quote changed. Create a new order; no extra charge was made.',
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Начать заново', 'Start again'),
                        callback_data='df:start',
                    )
                ]
            ]
        )
    await edit_or_answer_photo(
        callback=callback,
        caption=caption,
        keyboard=keyboard,
        parse_mode='HTML',
    )


async def arm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    del state
    await callback.answer()
    try:
        checkout = await get_owned_checkout(
            db,
            public_id=_checkout_id(callback) or '',
            user_id=db_user.id,
            for_update=True,
        )
        checkout = await arm_checkout(db, checkout)
    except DeviceFirstError as error:
        await _render_error(callback, db_user, error)
        return
    await _render_checkout(callback, db_user, db, checkout)


async def refresh_status(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    del state
    await callback.answer()
    try:
        checkout = await get_owned_checkout(
            db,
            public_id=_checkout_id(callback) or '',
            user_id=db_user.id,
        )
    except DeviceFirstError as error:
        await _render_error(callback, db_user, error)
        return
    await _render_checkout(callback, db_user, db, checkout)


async def change_selection(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Cancel an uncharged checkout before returning to the device choices."""
    await callback.answer()
    public_id = _checkout_id(callback)
    if not public_id:
        await _render_error(callback, db_user, _text(db_user, 'Заказ не найден.', 'Order not found.'))
        return
    try:
        checkout = await get_owned_checkout(db, public_id=public_id, user_id=db_user.id, for_update=True)
        pending_attempt = None
        if getattr(checkout, 'id', None) is not None:
            pending_attempt = await get_pending_platega_attempt(db, checkout_id=checkout.id)
        if pending_attempt is not None:
            await _render_checkout(callback, db_user, db, checkout)
            return
        checkout = await cancel_checkout(db, checkout)
    except DeviceFirstError as error:
        if error.code == 'invalid_state' and 'checkout' in locals():
            await _render_checkout(callback, db_user, db, checkout)
        else:
            await _render_error(callback, db_user, error)
        return

    data = await state.get_data()
    options = await build_purchase_options(db, db_user)
    if not options.get('eligible'):
        await _render_error(
            callback,
            db_user,
            _text(
                db_user,
                'Варианты тарифа изменились. Вернитесь в главное меню и начните новый заказ.',
                'Tariff options changed. Return to the main menu and start a new order.',
            ),
        )
        return
    origin = data.get('df_origin_callback') or 'back_to_menu'
    await state.update_data(df_checkout_id=None)
    if checkout.period_days not in options['period_options']:
        await _period_page(
            callback,
            db_user,
            state,
            options,
            view_id=uuid.uuid4().hex[:8],
            origin_callback=origin,
        )
        return
    await _device_page(
        callback,
        db_user,
        state,
        options,
        view_id=uuid.uuid4().hex[:8],
        days=checkout.period_days,
        origin_callback=origin,
    )


async def pay(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    del state
    await callback.answer()
    parts = (callback.data or '').split(':', 3)
    if len(parts) != 4:
        return
    try:
        await create_platega_attempt(
            db,
            checkout_public_id=parts[3],
            user_id=db_user.id,
            method_key=parts[2],
        )
    except DeviceFirstError as error:
        await _render_error(callback, db_user, error)
        return
    try:
        checkout = await get_owned_checkout(db, public_id=parts[3], user_id=db_user.id)
    except DeviceFirstError as error:
        await _render_error(callback, db_user, error)
        return
    await _render_checkout(callback, db_user, db, checkout)


async def cancel(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    await callback.answer()
    public_id = _checkout_id(callback)
    if not public_id:
        await _render_error(
            callback,
            db_user,
            _text(db_user, 'Заказ не найден.', 'Order not found.'),
        )
        return
    try:
        checkout = await get_owned_checkout(db, public_id=public_id, user_id=db_user.id, for_update=True)
        pending_attempt = None
        if getattr(checkout, 'id', None) is not None:
            pending_attempt = await get_pending_platega_attempt(db, checkout_id=checkout.id)
        checkout = await cancel_checkout(db, checkout)
    except DeviceFirstError as error:
        await state.clear()
        if error.code == 'invalid_state' and 'checkout' in locals():
            await _render_checkout(callback, db_user, db, checkout)
        else:
            await _render_error(callback, db_user, error)
        return
    await state.clear()
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            db_user,
            (
                'Заказ отменён. Деньги не списаны.\n\n'
                'Если вы позже оплатите уже созданный счёт, деньги попадут только на баланс: '
                'VPN автоматически не оформится.'
                if pending_attempt is not None
                else 'Заказ отменён. Деньги не списаны.'
            ),
            (
                'Order cancelled. No money was charged.\n\n'
                'If you pay an already issued invoice later, the money goes to your balance only; '
                'VPN will not be activated automatically.'
                if pending_attempt is not None
                else 'Order cancelled. No money was charged.'
            ),
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[_main_menu(db_user)]]),
        parse_mode='HTML',
    )


def _safe_error_detail(user: User, error: DeviceFirstError | str) -> str:
    if not isinstance(error, DeviceFirstError):
        return str(error)
    messages = {
        'device_limit_decrease_not_allowed': _text(
            user,
            'Нельзя уменьшить количество устройств при продлении. Выберите текущее число или больше.',
            'A renewal cannot reduce the device limit. Choose the current number or more.',
        ),
        'open_checkout_exists': _text(
            user,
            'У вас уже есть незавершённый заказ. Откройте его и продолжите.',
            'You already have an unfinished order. Open it and continue.',
        ),
        'subscription_restricted': _text(
            user,
            'Покупка подписки сейчас недоступна для этого аккаунта. Обратитесь в поддержку.',
            'Subscription purchases are unavailable for this account. Contact support.',
        ),
        'invalid_selection': _text(
            user,
            'Этот вариант больше недоступен. Я обновил список — выберите ещё раз.',
            'That option is no longer available. I refreshed the list; choose again.',
        ),
        'rate_limited': _text(
            user,
            'Слишком много попыток подряд. Подождите минуту и повторите.',
            'Too many attempts. Wait a minute and try again.',
        ),
        'reconciliation_required': _text(
            user,
            'Мы проверяем созданный счёт. Не оплачивайте повторно: откройте проверку статуса или обратитесь в поддержку.',
            'We are checking the created invoice. Do not pay again: check its status or contact support.',
        ),
    }
    return messages.get(
        error.code,
        _text(
            user,
            'Не удалось продолжить заказ. Попробуйте ещё раз или начните новый расчёт.',
            'Could not continue the order. Try again or start a new quote.',
        ),
    )


async def _render_error(callback: types.CallbackQuery, user: User, error: DeviceFirstError | str) -> None:
    rows = [[_main_menu(user)]]
    if isinstance(error, DeviceFirstError) and error.code == 'reconciliation_required':
        public_id = _checkout_id(callback)
        if public_id:
            rows = [
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Проверить статус', 'Check status'),
                        callback_data=f'df:s:{public_id}',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Связаться с поддержкой', 'Contact support'),
                        callback_data='menu_support',
                    )
                ],
                [_cancel_order(user, public_id)],
            ]
    await edit_or_answer_photo(
        callback=callback,
        caption=f'⚠️ {_safe_error_detail(user, error)}',
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )


def register_device_first_handlers(dp) -> None:
    dp.callback_query.register(show_device_first_entry, F.data == 'df:start')
    # Keep old pagination callbacks replay-safe after deploy; new keyboards do
    # not emit `df:p:` at all.
    dp.callback_query.register(legacy_device_page, F.data.startswith('df:p:'))
    dp.callback_query.register(choose_devices, F.data.startswith('df:d:'))
    dp.callback_query.register(choose_period, F.data.startswith('df:t:'))
    dp.callback_query.register(confirm, F.data.startswith('df:c:'))
    dp.callback_query.register(arm, F.data.startswith('df:a:'))
    dp.callback_query.register(change_selection, F.data.startswith('df:e:'))
    dp.callback_query.register(pay, F.data.startswith('df:y:'))
    dp.callback_query.register(refresh_status, F.data.startswith('df:s:'))
    dp.callback_query.register(cancel, F.data.startswith('df:x:'))

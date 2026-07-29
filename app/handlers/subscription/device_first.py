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
    get_owned_checkout,
    serialize_checkout,
)
from app.services.device_first_payment_service import (
    available_platega_methods_for_db,
    create_platega_attempt,
)
from app.utils.photo_message import edit_or_answer_photo


def _en(user: User) -> bool:
    return user.language == 'en'


def _text(user: User, ru: str, en: str) -> str:
    return en if _en(user) else ru


def _money(kopeks: int) -> str:
    return f'{kopeks // 100:,}.{kopeks % 100:02d}'.replace(',', ' ')


def _chunks(values: list[int], size: int):
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _back(user: User, callback_data: str = 'back_to_menu') -> InlineKeyboardButton:
    return InlineKeyboardButton(text=_text(user, '‹ Назад', '‹ Back'), callback_data=callback_data)


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
    page: int,
    days: int,
    origin_callback: str,
) -> None:
    values = list(options['device_options'])
    page_count = max(1, (len(values) + 5) // 6)
    page = max(0, min(page, page_count - 1))
    visible = values[page * 6 : page * 6 + 6]
    rows = [
        [
            InlineKeyboardButton(
                text=_text(user, f'{limit} устройств', f'{limit} devices'),
                callback_data=f'df:d:{view_id}:{limit}',
            )
        ]
        for limit in visible
    ]
    if page_count > 1:
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text='‹', callback_data=f'df:p:{view_id}:{page - 1}'))
        nav.append(
            InlineKeyboardButton(
                text=f'{page + 1}/{page_count}',
                callback_data=f'df:p:{view_id}:{page}',
            )
        )
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton(text='›', callback_data=f'df:p:{view_id}:{page + 1}'))
        rows.append(nav)
    rows.append([_back(user, 'df:start')])
    await state.update_data(
        df_options=options,
        df_view_id=view_id,
        df_page=page,
        df_days=days,
        df_origin_callback=origin_callback,
    )
    await edit_or_answer_photo(
        callback=callback,
        caption=(
            f'📱 <b>{options["tariff"]["name"]}</b>\n\n'
            + _text(
                user,
                f'Срок: <b>{days} дней</b>\n\nСколько устройств будут пользоваться VPN?',
                f'Period: <b>{days} days</b>\n\nHow many devices will use the VPN?',
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
    rows = [
        [
            InlineKeyboardButton(
                text=_text(user, f'{days} дней', f'{days} days'),
                callback_data=f'df:t:{view_id}:{days}',
            )
            for days in chunk
        ]
        for chunk in _chunks(list(options['period_options']), 2)
    ]
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
    data = await state.get_data()
    origin = origin_callback or data.get('df_origin_callback') or 'back_to_menu'
    await _period_page(
        callback,
        db_user,
        state,
        options,
        view_id=view_id,
        origin_callback=origin,
    )
    return True


async def device_page(
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
    try:
        page = int(parts[3])
    except ValueError:
        await _answer_stale(callback, db_user)
        return
    await callback.answer()
    await _device_page(
        callback,
        db_user,
        state,
        data['df_options'],
        view_id=parts[2],
        page=page,
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
            page=0,
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
        await _render_error(callback, db_user, str(error))
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
        page=0,
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
    device_change = (
        f'{current_devices} → {checkout.selected_device_limit}'
        if current_devices is not None
        else str(checkout.selected_device_limit)
    )
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
            InlineKeyboardButton(
                text=_text(user, 'Отменить', 'Cancel'),
                callback_data=f'df:x:{checkout_id}',
            )
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
                f'📅 Срок: {checkout.period_days} дней\n'
                f'🏁 До: {end_date}\n'
                f'💳 Баланс: {_money(user.balance_kopeks)} ₽\n'
                f'💰 Итого: <b>{_money(checkout.quoted_price_kopeks)} ₽</b>\n'
                + (f'⚠️ Не хватает: {_money(shortage)} ₽\n' if shortage else '')
                + '\nДеньги ещё не списаны. Следующее подтверждение разрешит списать эту сумму.'
            ),
            (
                '✅ <b>Review your order</b>\n\n'
                f'💎 Tariff: <b>{tariff_name}</b>\n'
                f'📱 Devices: <b>{device_change}</b>\n'
                f'📅 Period: {checkout.period_days} days\n'
                f'🏁 Ends: {end_date}\n'
                f'💳 Balance: ₽{_money(user.balance_kopeks)}\n'
                f'💰 Total: <b>₽{_money(checkout.quoted_price_kopeks)}</b>\n'
                + (f'⚠️ Shortage: ₽{_money(shortage)}\n' if shortage else '')
                + '\nNothing has been charged. The next confirmation authorizes this amount.'
            ),
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode='HTML',
    )


def _checkout_id(callback: types.CallbackQuery) -> str | None:
    parts = (callback.data or '').split(':', 2)
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
        checkout = await confirm_checkout(db, checkout)
    except DeviceFirstError as error:
        await _render_error(callback, db_user, str(error))
        return
    shortage = max(0, checkout.max_price_kopeks - db_user.balance_kopeks)
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            db_user,
            (
                '🔐 <b>Заказ подтверждён</b>\n\n'
                f'К списанию: {_money(checkout.quoted_price_kopeks)} ₽.\n'
                'Списание произойдёт только после следующего нажатия.'
            ),
            (
                '🔐 <b>Order confirmed</b>\n\n'
                f'To charge: ₽{_money(checkout.quoted_price_kopeks)}.\n'
                'The charge happens only after the next tap.'
            ),
        ),
        keyboard=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            _text(
                                db_user,
                                f'Пополнить {_money(shortage)} ₽ и оформить',
                                f'Top up ₽{_money(shortage)} and subscribe',
                            )
                            if shortage
                            else _text(
                                db_user,
                                f'Списать {_money(checkout.quoted_price_kopeks)} ₽ и оформить',
                                f'Charge ₽{_money(checkout.quoted_price_kopeks)} and subscribe',
                            )
                        ),
                        callback_data=f'df:a:{checkout.public_id}',
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_text(db_user, 'Отменить', 'Cancel'),
                        callback_data=f'df:x:{checkout.public_id}',
                    )
                ],
            ]
        ),
        parse_mode='HTML',
    )


async def _render_checkout(callback: types.CallbackQuery, user: User, db: AsyncSession, checkout) -> None:
    result = serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)
    if result['ui_state'] == 'awaiting_payment':
        shortage = result['shortage_kopeks'] or 0
        methods = await available_platega_methods_for_db(db, user)
        rows = [
            [
                InlineKeyboardButton(
                    text=_text(user, f'Пополнить · {item["key"]}', f'Top up · {item["key"]}'),
                    callback_data=f'df:y:{item["key"]}:{checkout.public_id}',
                )
            ]
            for item in methods
        ]
        if shortage > 0:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Обновить баланс', 'Refresh balance'),
                        callback_data=f'df:s:{checkout.public_id}',
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=_text(user, 'Продолжить и оформить', 'Continue and subscribe'),
                        callback_data=f'df:a:{checkout.public_id}',
                    )
                ]
            )
        rows.append([_back(user)])
        if shortage:
            caption = _text(
                user,
                (
                    '💳 <b>Недостаточно средств</b>\n\n'
                    f'📱 {checkout.selected_device_limit} устройств · 📅 {checkout.period_days} дней\n'
                    f'💰 Итого: {_money(checkout.quoted_price_kopeks)} ₽\n'
                    f'⚠️ Нужно доплатить: <b>{_money(shortage)} ₽</b>\n\n'
                    'Заказ сохранён. Выберите способ доплаты.'
                ),
                (
                    '💳 <b>Insufficient balance</b>\n\n'
                    f'📱 {checkout.selected_device_limit} devices · 📅 {checkout.period_days} days\n'
                    f'💰 Total: ₽{_money(checkout.quoted_price_kopeks)}\n'
                    f'⚠️ Top up: <b>₽{_money(shortage)}</b>\n\n'
                    'Your order is saved. Choose a top-up method.'
                ),
            )
        else:
            caption = _text(
                user,
                (
                    '✅ <b>Баланс пополнен</b>\n\n'
                    f'📱 {checkout.selected_device_limit} устройств · 📅 {checkout.period_days} дней\n'
                    f'💰 К списанию: {_money(checkout.quoted_price_kopeks)} ₽\n\n'
                    'Нажмите «Продолжить и оформить».'
                ),
                (
                    '✅ <b>Balance topped up</b>\n\n'
                    f'📱 {checkout.selected_device_limit} devices · 📅 {checkout.period_days} days\n'
                    f'💰 To charge: ₽{_money(checkout.quoted_price_kopeks)}\n\n'
                    'Tap “Continue and subscribe”.'
                ),
            )
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
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
                [_back(user)],
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
                [_back(user)],
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
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[button], [_back(user)]])
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
        await _render_error(callback, db_user, str(error))
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
        await _render_error(callback, db_user, str(error))
        return
    await _render_checkout(callback, db_user, db, checkout)


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
        attempt = await create_platega_attempt(
            db,
            checkout_public_id=parts[3],
            user_id=db_user.id,
            method_key=parts[2],
        )
    except DeviceFirstError as error:
        await _render_error(callback, db_user, str(error))
        return
    if not attempt.redirect_url:
        await _render_error(
            callback,
            db_user,
            _text(db_user, 'Платёж требует сверки.', 'Payment requires reconciliation.'),
        )
        return
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            db_user,
            '💳 Счёт создан. Сумма и заказ зафиксированы.',
            '💳 Invoice created. Amount and order are locked.',
        ),
        keyboard=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(db_user, 'Перейти к оплате', 'Open payment'),
                        url=attempt.redirect_url,
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=_text(db_user, 'Проверить заказ', 'Check order'),
                        callback_data=f'df:s:{parts[3]}',
                    )
                ],
            ]
        ),
        parse_mode='HTML',
    )


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
        checkout = await cancel_checkout(db, checkout)
    except DeviceFirstError as error:
        await state.clear()
        if error.code == 'invalid_state' and 'checkout' in locals():
            await _render_checkout(callback, db_user, db, checkout)
        else:
            await _render_error(callback, db_user, str(error))
        return
    await state.clear()
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(
            db_user,
            'Заказ отменён. Деньги не списаны.',
            'Order cancelled. No money was charged.',
        ),
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[_back(db_user)]]),
        parse_mode='HTML',
    )


async def _render_error(callback: types.CallbackQuery, user: User, detail: str) -> None:
    await edit_or_answer_photo(
        callback=callback,
        caption=_text(user, f'⚠️ Не удалось продолжить: {detail}', f'⚠️ Could not continue: {detail}'),
        keyboard=InlineKeyboardMarkup(inline_keyboard=[[_back(user, 'df:start')]]),
        parse_mode='HTML',
    )


def register_device_first_handlers(dp) -> None:
    dp.callback_query.register(show_device_first_entry, F.data == 'df:start')
    dp.callback_query.register(device_page, F.data.startswith('df:p:'))
    dp.callback_query.register(choose_devices, F.data.startswith('df:d:'))
    dp.callback_query.register(choose_period, F.data.startswith('df:t:'))
    dp.callback_query.register(confirm, F.data.startswith('df:c:'))
    dp.callback_query.register(arm, F.data.startswith('df:a:'))
    dp.callback_query.register(pay, F.data.startswith('df:y:'))
    dp.callback_query.register(refresh_status, F.data.startswith('df:s:'))
    dp.callback_query.register(cancel, F.data.startswith('df:x:'))

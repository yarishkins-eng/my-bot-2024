from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.handlers.subscription.device_first import (
    _answer_stale,
    _days_label,
    _device_label,
    _device_page,
    _period_page,
    _render_arm_confirmation,
    _render_checkout,
    _render_confirmation,
    _render_direct_payment_methods,
    _render_error,
    _render_fused_confirmation,
    _render_new_checkout,
    abandon,
    arm,
    cancel,
    cancel_fused,
    change_selection,
    change_selection_fused,
    choose_devices,
    choose_period,
    confirm,
    pay,
    pay_fused,
    pay_wallet_fused,
    refresh_status,
    restart_device_first_or_show_legacy_tariffs,
    show_device_first_entry,
)
from app.services.device_first_checkout_service import DeviceFirstError


# Адрес двери доплаты, которую этап БК ставит на экран заказа.
TOP_UP_PREFIX = '/balance/top-up/'
TOP_UP_LABEL = 'Доплатить'


def _db(*, open_order: bool = False):
    """База для экрана заказа: он спрашивает про открытые заказы ЧИСТЫМ чтением.

    Служебный `get_open_checkout_for_user` здесь не годится и не используется кодом: он
    гасит просроченный заказ и делает `db.commit()`, а экран отрисовки достижим сразу
    после неудачного списания — коммит там лёг бы на чужую единицу работы.
    """
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=77 if open_order else None)
    return db


def _user(language: str = 'ru'):
    return SimpleNamespace(language=language)


@pytest.mark.asyncio
@pytest.mark.parametrize(('ui_state', 'message'), [('awaiting_payment', 'Статус не изменился.'), ('processing', 'Оплата подтверждена. Текущий статус показан ниже.'), ('cancelled', 'Счёт больше не действует.'), ('operator_review', 'Текущий статус показан ниже.'), (DeviceFirstError('invalid_state', 'closed'), None)])  # fmt: skip
async def test_refresh_status_answers_with_visible_result(ui_state: str | DeviceFirstError, message: str | None) -> None:  # fmt: skip
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    error = ui_state if isinstance(ui_state, DeviceFirstError) else None
    render, error_render = AsyncMock(), AsyncMock()
    with patch('app.handlers.subscription.device_first.get_owned_checkout', AsyncMock(side_effect=error, return_value=SimpleNamespace())), patch('app.handlers.subscription.device_first.serialize_checkout', return_value={'ui_state': ui_state}), patch('app.handlers.subscription.device_first._render_checkout', render), patch('app.handlers.subscription.device_first._render_error', error_render):  # fmt: skip
        await refresh_status(callback, user, AsyncMock(), AsyncMock())
    callback.answer.assert_awaited_once_with(*(() if error else (message,)))
    assert (render.await_count, error_render.await_count) == ((0, 1) if error else (1, 0))


@pytest.mark.asyncio
async def test_period_page_is_first_and_keeps_origin_callback() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30, 90, 180],
        'device_options': [1, 2, 3],
    }

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _period_page(
            callback,
            _user(),
            state,
            options,
            view_id='view1234',
            origin_callback='funnel_tariffs',
        )

    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert [len(row) for row in keyboard] == [2, 1, 1]
    assert keyboard[-1][0].callback_data == 'funnel_tariffs'
    assert keyboard[0][0].callback_data == 'df:t:view1234:30'
    state.update_data.assert_awaited_once_with(
        df_options=options,
        df_view_id='view1234',
        df_origin_callback='funnel_tariffs',
    )


@pytest.mark.asyncio
async def test_period_labels_are_compact_and_do_not_promise_a_price_before_device_selection() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30, 365],
        'device_options': [1, 3],
        'price_matrix': [
            {
                'period_days': 30,
                'prices': [
                    {'device_limit': 1, 'price_kopeks': 89_000},
                    {'device_limit': 3, 'price_kopeks': 109_000},
                ],
            },
            {
                'period_days': 365,
                'prices': [
                    {'device_limit': 1, 'price_kopeks': 849_000},
                    {'device_limit': 3, 'price_kopeks': 1_089_000},
                ],
            },
        ],
    }

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _period_page(
            callback,
            _user(),
            state,
            options,
            view_id='view1234',
            origin_callback='funnel_tariffs',
        )

    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].text == '1 месяц'
    assert keyboard[0][1].text == '1 год'


@pytest.mark.asyncio
async def test_period_and_device_pages_render_only_the_escaped_tariff_description() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    description = 'В каждый тариф входит:\n<b>✔️ Безлимитный трафик</b>'
    options = {
        'tariff': {'name': 'Базовый', 'description': description},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }

    with patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render:
        await _period_page(
            callback,
            _user(),
            state,
            options,
            view_id='view1234',
            origin_callback='funnel_tariffs',
        )
        period_caption = render.await_args.kwargs['caption']

        await _device_page(
            callback,
            _user(),
            state,
            options,
            view_id='view1234',
            days=30,
            origin_callback='funnel_tariffs',
        )
        device_caption = render.await_args.kwargs['caption']

    for caption in (period_caption, device_caption):
        assert 'В каждый тариф входит:\n&lt;b&gt;✔️ Безлимитный трафик&lt;/b&gt;' in caption
        assert '<b>✔️ Безлимитный трафик</b>' not in caption
        assert 'traffic_limit_gb' not in caption


@pytest.mark.asyncio
async def test_device_page_shows_all_ten_choices_on_one_screen_without_pagination() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30],
        'device_options': list(range(1, 11)),
    }

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _device_page(
            callback,
            _user('en'),
            state,
            options,
            view_id='view1234',
            days=30,
            origin_callback='funnel_tariffs',
        )

    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    # One button per row: two columns clip the price on narrow phones.
    assert [len(row) for row in keyboard] == [1] * 10 + [1]
    assert keyboard[0][0].callback_data == 'df:d:view1234:1'
    assert keyboard[9][0].callback_data == 'df:d:view1234:10'
    assert keyboard[0][0].text == '1 device'
    assert keyboard[1][0].text == '2 devices'
    assert all(not button.callback_data.startswith('df:p:') for row in keyboard for button in row)
    assert keyboard[-1][0].callback_data == 'df:start'


@pytest.mark.asyncio
async def test_device_page_places_english_ruble_amount_before_the_number() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30],
        'device_options': [1],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 1, 'price_kopeks': 30_000}]}],
    }

    with patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render:
        await _device_page(
            callback,
            _user('en'),
            state,
            options,
            view_id='view1234',
            days=30,
            origin_callback='back_to_menu',
        )

    assert render.await_args.kwargs['keyboard'].inline_keyboard[0][0].text == '1 device · ₽300'


@pytest.mark.parametrize(
    ('limit', 'label'),
    [(1, '1 устройство'), (2, '2 устройства'), (5, '5 устройств'), (11, '11 устройств')],
)
def test_device_labels_use_correct_russian_plural_forms(limit: int, label: str) -> None:
    assert _device_label(_user(), limit) == label


@pytest.mark.parametrize(
    ('language', 'days', 'label'),
    [
        ('ru', 1, '1 день'),
        ('ru', 2, '2 дня'),
        ('ru', 5, '5 дней'),
        ('ru', 21, '21 день'),
        ('en', 1, '1 day'),
        ('en', 2, '2 days'),
    ],
)
def test_day_labels_use_correct_russian_and_english_plural_forms(language: str, days: int, label: str) -> None:
    assert _days_label(_user(language), days) == label


@pytest.mark.asyncio
async def test_stale_callback_uses_localized_alert() -> None:
    callback = SimpleNamespace(answer=AsyncMock())

    await _answer_stale(callback, _user('en'))

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    assert 'out of date' in callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_choose_period_stale_token_alerts_and_renders_fresh_options() -> None:
    callback = SimpleNamespace(data='df:t:old-view:30', answer=AsyncMock())
    state = AsyncMock()
    options = {
        'eligible': True,
        'tariff': {'name': 'Premium'},
        'period_options': [30],
        'device_options': [1],
    }
    state.get_data.return_value = {
        'df_view_id': 'current-view',
        'df_options': options,
        'df_origin_callback': 'funnel_tariffs',
    }

    with patch(
        'app.handlers.subscription.device_first._period_page',
        AsyncMock(),
    ) as render:
        await choose_period(callback, _user(), AsyncMock(), state)

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    render.assert_awaited_once()
    assert render.await_args.kwargs['origin_callback'] == 'funnel_tariffs'


@pytest.mark.asyncio
async def test_confirmation_contains_server_snapshot_contract() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(language='ru', balance_kopeks=150_000)
    checkout = SimpleNamespace(
        public_id='checkout-id',
        selected_device_limit=3,
        period_days=30,
        quoted_price_kopeks=109_000,
    )
    snapshot = {
        'current_device_limit': 1,
        'current_subscription_is_trial': False,
        'estimated_end_at': '2026-08-29T12:00:00+00:00',
        'shortage_kopeks': 0,
    }

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value=snapshot,
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render,
    ):
        await _render_confirmation(
            callback,
            user,
            checkout,
            tariff_name='Premium',
        )

    caption = render.await_args.kwargs['caption']
    assert 'Premium' in caption
    assert 'было 1 устройство → станет 3 устройства' in caption
    assert '29.08.2026' in caption
    assert '1 500 ₽' in caption
    assert '1 090 ₽' in caption


@pytest.mark.asyncio
async def test_trial_confirmation_does_not_present_a_temporary_limit_as_a_paid_change() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(language='ru', balance_kopeks=150_000)
    checkout = SimpleNamespace(
        public_id='checkout-id',
        selected_device_limit=3,
        period_days=30,
        quoted_price_kopeks=109_000,
    )
    snapshot = {
        'current_device_limit': 1,
        'current_subscription_is_trial': True,
        'estimated_end_at': '2026-08-29T12:00:00+00:00',
        'shortage_kopeks': 0,
    }

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value=snapshot,
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render,
    ):
        await _render_confirmation(callback, user, checkout, tariff_name='Premium')

    caption = render.await_args.kwargs['caption']
    assert '📱 Устройства: <b>3 устройства</b>' in caption
    assert 'было 1 устройство → станет' not in caption


@pytest.mark.asyncio
async def test_annual_confirmation_keeps_the_exact_365_day_term() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(language='ru', balance_kopeks=150_000)
    checkout = SimpleNamespace(
        public_id='checkout-id',
        selected_device_limit=3,
        period_days=365,
        quoted_price_kopeks=109_000,
    )
    snapshot = {
        'current_device_limit': 1,
        'current_subscription_is_trial': False,
        'estimated_end_at': '2027-07-30T12:00:00+00:00',
        'shortage_kopeks': 0,
    }

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value=snapshot,
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render,
    ):
        await _render_confirmation(callback, user, checkout, tariff_name='Premium')

    assert '1 год (365 дней)' in render.await_args.kwargs['caption']


@pytest.mark.asyncio
async def test_choose_devices_renders_a_checkout_free_pay_confirmation() -> None:
    """Device selection never persists a draft: the order is born at «Pay»."""
    callback = SimpleNamespace(data='df:d:view1234:5', answer=AsyncMock())
    state = AsyncMock()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30, 90],
        'device_options': [2, 5],
        'price_matrix': [
            {
                'period_days': 90,
                'prices': [
                    {'device_limit': 2, 'price_kopeks': 89_000},
                    {'device_limit': 5, 'price_kopeks': 109_000},
                ],
            }
        ],
    }
    state.get_data.return_value = {
        'df_view_id': 'view1234',
        'df_days': 90,
        'df_options': options,
    }
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50000)
    db = _db()

    with (
        patch('app.handlers.subscription.device_first.create_or_resume_direct_checkout', AsyncMock()) as create,
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/subscription/purchase?safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await choose_devices(callback, user, db, state)

    # Инвариант этого сторожа — «выбор устройств не заводит заказ», и его держит ИМЕННО эта
    # строка. Прежде рядом стояла ещё проверка «мы в базу даже не смотрим»: она закрепляла
    # не инвариант, а побочное свойство. Этап БК смотрит намеренно — живой счёт закрепляет
    # способ оплаты, и предлагать доплату поверх него значит вести человека в отказ.
    # Чтение осталось чтением: заказ по-прежнему рождается только на кнопке оплаты.
    create.assert_not_awaited()
    db.scalar.assert_awaited_once()
    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Ваш заказ' in caption
    assert 'Premium' in caption
    assert '5 устройств · 3 месяца' in caption
    assert '1 090 ₽' in caption
    assert keyboard[0][0].web_app.url == 'https://cabinet.example/subscription/purchase?safe'
    assert keyboard[1][0].web_app.url == 'https://cabinet.example/subscription/purchase?safe'
    # Адреса проверяются оба и по порядку: первой стоит дверь доплаты, за ней — прежняя
    # кнопка способа оплаты, у которой метка `autostart=1` не тронута ни на знак.
    assert build_cabinet_url.call_args_list[0].args[0] == (
        '/balance/top-up/platega?returnTo=%2Fsubscription%2Fpurchase'
        '%3Ffrom%3Dcheckout%26period%3D90%26devices%3D5&amount=590'
    )
    assert build_cabinet_url.call_args_list[1].args[0] == (
        '/subscription/purchase?period=90&devices=5&method=sbp&autostart=1'
    )
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert callbacks == ['df:e2', 'df:x2']


@pytest.mark.asyncio
async def test_new_direct_checkout_skips_duplicate_confirmations_and_opens_only_the_trusted_miniapp() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    draft = SimpleNamespace(
        public_id='owned-checkout',
        lifecycle_state='draft',
        settlement_mode='direct_purchase_v2',
        tariff_id=7,
        tariff_total_kopeks=36_900,
        selected_device_limit=4,
        period_days=30,
    )
    confirmed = SimpleNamespace(**{**draft.__dict__, 'lifecycle_state': 'confirmed'})
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(name='Базовый')))

    with (
        patch(
            'app.handlers.subscription.device_first.confirm_checkout', AsyncMock(return_value=confirmed)
        ) as confirm_quote,
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}, {'key': 'cards_ru', 'provider_code': 11}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/subscription/purchase?safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_new_checkout(callback, user, db, draft, tariff_name='Базовый')

    confirm_quote.assert_awaited_once_with(db, draft)
    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Ваш заказ' in caption
    assert 'Базовый' in caption
    assert '4 устройства · 1 месяц' in caption
    assert '369 ₽' in caption
    assert 'Подтвердить конфигурацию' not in caption
    assert keyboard[0][0].text == 'СБП (QR-код) · 369 ₽'
    assert keyboard[0][0].web_app.url == 'https://cabinet.example/subscription/purchase?safe'
    assert build_cabinet_url.call_args_list[0].args[0] == (
        '/subscription/purchase?checkout=owned-checkout&method=sbp&autostart=1'
    )
    assert keyboard[1][0].text == 'Карта российского банка · 369 ₽'
    assert keyboard[-1][0].callback_data == 'df:e:owned-checkout'
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert all(not callback.startswith(('df:c:', 'df:a:', 'df:x:')) for callback in callbacks)


@pytest.mark.asyncio
async def test_confirm_reloads_owned_checkout_before_state_transition() -> None:
    callback = SimpleNamespace(data='df:c:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10000)
    draft = SimpleNamespace(public_id='owned-checkout', lifecycle_state='draft')
    confirmed = SimpleNamespace(
        public_id='owned-checkout',
        lifecycle_state='confirmed',
        quoted_price_kopeks=45000,
        max_price_kopeks=45000,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=draft),
        ) as get_owned,
        patch(
            'app.handlers.subscription.device_first.confirm_checkout',
            AsyncMock(return_value=confirmed),
        ) as transition,
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render,
    ):
        await confirm(callback, user, AsyncMock(), AsyncMock())

    get_owned.assert_awaited_once()
    assert get_owned.await_args.kwargs == {
        'public_id': 'owned-checkout',
        'user_id': 17,
        'for_update': True,
    }
    transition.assert_awaited_once()
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'df:a:owned-checkout'


@pytest.mark.asyncio
async def test_confirm_stale_callback_renders_the_canonical_cancelled_state() -> None:
    callback = SimpleNamespace(data='df:c:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10_000)
    cancelled = SimpleNamespace(public_id='owned-checkout', lifecycle_state='cancelled')

    with (
        patch('app.handlers.subscription.device_first.get_owned_checkout', AsyncMock(return_value=cancelled)),
        patch('app.handlers.subscription.device_first.confirm_checkout', AsyncMock()) as transition,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render_checkout,
        patch('app.handlers.subscription.device_first._render_arm_confirmation', AsyncMock()) as render_arm,
    ):
        await confirm(callback, user, AsyncMock(), AsyncMock())

    transition.assert_not_awaited()
    render_checkout.assert_awaited_once()
    render_arm.assert_not_awaited()


@pytest.mark.asyncio
async def test_resuming_confirmed_checkout_renders_second_confirmation_actions() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10_000)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        quoted_price_kopeks=45_000,
        max_price_kopeks=45_000,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'confirmation'},
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Заказ подтверждён' in caption
    assert keyboard[0][0].callback_data == 'df:a:owned-checkout'
    assert keyboard[1][0].callback_data == 'df:e:owned-checkout'
    assert keyboard[1][1].callback_data == 'df:x:owned-checkout'


@pytest.mark.asyncio
async def test_device_back_after_a_predeploy_state_returns_period_screen_to_the_main_menu() -> None:
    callback = SimpleNamespace(data='df:start', answer=AsyncMock())
    state = AsyncMock()
    state.get_data.return_value = {'df_origin_callback': 'funnel_tariffs'}
    user = SimpleNamespace(id=17, language='ru')
    options = {'eligible': True, 'tariff': {'name': 'Premium'}, 'period_options': [30], 'device_options': [1]}

    with (
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch('app.handlers.subscription.device_first.get_open_checkout_for_user', AsyncMock(return_value=None)),
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        assert await show_device_first_entry(callback, user, AsyncMock(), state) is True

    assert period_page.await_args.kwargs['origin_callback'] == 'back_to_menu'


@pytest.mark.asyncio
async def test_safe_open_quote_is_discarded_before_falling_back_when_new_device_first_orders_are_disabled() -> None:
    callback = SimpleNamespace(data='df:start', answer=AsyncMock())
    state = AsyncMock()
    user = SimpleNamespace(id=17, language='ru')
    checkout = SimpleNamespace(public_id='draft-17', lifecycle_state='draft', settlement_mode='legacy_deposit')
    cancelled_checkout = SimpleNamespace(public_id='draft-17', lifecycle_state='cancelled')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.build_purchase_options',
            AsyncMock(return_value={'eligible': False, 'reason': 'feature_disabled'}),
        ) as build_options,
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.cancel_checkout_for_new_calculation',
            AsyncMock(return_value=cancelled_checkout),
        ) as cancel_for_restart,
    ):
        assert await show_device_first_entry(callback, user, db, state) is False

    build_options.assert_awaited_once_with(db, user)
    cancel_for_restart.assert_awaited_once_with(db, checkout)
    state.clear.assert_awaited_once()
    callback.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_state', ['draft', 'confirmed', 'awaiting_funds'])
async def test_tariffs_starts_a_new_calculation_after_discarding_a_safe_quote(lifecycle_state: str) -> None:
    callback = SimpleNamespace(data='tariff_list', answer=AsyncMock())
    state = AsyncMock()
    user = SimpleNamespace(id=17, language='ru')
    checkout = SimpleNamespace(public_id='quote-17', lifecycle_state=lifecycle_state, settlement_mode='legacy_deposit')
    cancelled_checkout = SimpleNamespace(public_id='quote-17', lifecycle_state='cancelled')
    options = {'eligible': True, 'tariff': {'name': 'Базовый'}, 'period_options': [30], 'device_options': [2]}
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=checkout),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ) as get_owned,
        patch(
            'app.handlers.subscription.device_first.cancel_checkout_for_new_calculation',
            AsyncMock(return_value=cancelled_checkout),
        ) as cancel_for_restart,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        assert await show_device_first_entry(callback, user, db, state) is True

    get_owned.assert_awaited_once_with(db, public_id='quote-17', user_id=17, for_update=True)
    cancel_for_restart.assert_awaited_once_with(db, checkout)
    state.clear.assert_awaited_once()
    period_page.assert_awaited_once()
    assert period_page.await_args.args[3] == options


@pytest.mark.asyncio
async def test_tariffs_resumes_a_direct_checkout_without_silently_abandoning_it() -> None:
    """Opening Tariffs must not invalidate a provider-owned payment link."""
    callback = SimpleNamespace(data='tariff_list', answer=AsyncMock())
    state = AsyncMock()
    user = SimpleNamespace(id=17, language='ru')
    quote = SimpleNamespace(
        public_id='direct-quote-17', lifecycle_state='confirmed', settlement_mode='direct_purchase_v2'
    )
    db = AsyncMock()

    with (
        patch('app.handlers.subscription.device_first.get_open_checkout_for_user', AsyncMock(return_value=quote)),
        patch(
            'app.handlers.subscription.device_first.abandon_direct_checkout_for_new_calculation', AsyncMock()
        ) as abandon,
        patch('app.handlers.subscription.device_first.get_owned_checkout', AsyncMock()) as locked,
        patch(
            'app.handlers.subscription.device_first.cancel_checkout_for_new_calculation', AsyncMock()
        ) as cancel_quote,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        assert await show_device_first_entry(callback, user, db, state) is True

    abandon.assert_not_awaited()
    locked.assert_not_awaited()
    cancel_quote.assert_not_awaited()
    render.assert_awaited_once_with(callback, user, db, quote)
    period_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariffs_keeps_recovery_when_cancellation_did_not_take_effect() -> None:
    callback = SimpleNamespace(data='tariff_list', answer=AsyncMock())
    state = AsyncMock()
    user = SimpleNamespace(id=17, language='ru')
    checkout = SimpleNamespace(public_id='quote-17', lifecycle_state='confirmed', settlement_mode='legacy_deposit')
    settled_checkout = SimpleNamespace(public_id='quote-17', lifecycle_state='ready')
    options = {'eligible': True, 'tariff': {'name': 'Базовый'}, 'period_options': [30], 'device_options': [2]}
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=checkout),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.cancel_checkout_for_new_calculation',
            AsyncMock(return_value=settled_checkout),
        ),
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        assert await show_device_first_entry(callback, user, db, state) is True

    render.assert_awaited_once_with(callback, user, db, settled_checkout)
    period_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_tariffs_keeps_recovery_when_a_quote_becomes_an_external_invoice() -> None:
    callback = SimpleNamespace(data='tariff_list', answer=AsyncMock())
    state = AsyncMock()
    user = SimpleNamespace(id=17, language='ru')
    checkout = SimpleNamespace(public_id='quote-17', lifecycle_state='confirmed', settlement_mode='legacy_deposit')
    options = {'eligible': True, 'tariff': {'name': 'Базовый'}, 'period_options': [30], 'device_options': [2]}
    invoice_exists = DeviceFirstError('external_invoice_active', 'External invoice exists')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=checkout),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(side_effect=[checkout, checkout]),
        ),
        patch(
            'app.handlers.subscription.device_first.cancel_checkout_for_new_calculation',
            AsyncMock(side_effect=invoice_exists),
        ),
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        assert await show_device_first_entry(callback, user, db, state) is True

    render.assert_awaited_once_with(callback, user, db, checkout)
    period_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_start_button_falls_back_to_the_legacy_tariff_picker() -> None:
    callback = SimpleNamespace(data='df:start')
    user = SimpleNamespace(id=17, language='ru')
    db = AsyncMock()
    state = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.show_device_first_entry',
            AsyncMock(return_value=False),
        ) as show_entry,
        patch(
            'app.handlers.subscription.tariff_purchase.show_funnel_tariffs',
            AsyncMock(),
        ) as show_legacy,
    ):
        await restart_device_first_or_show_legacy_tariffs(callback, user, db, state)

    show_entry.assert_awaited_once_with(callback, user, db, state)
    show_legacy.assert_awaited_once_with(callback, user, db, state)


@pytest.mark.asyncio
async def test_arm_renders_exact_server_shortage_and_configuration() -> None:
    callback = SimpleNamespace(data='df:a:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10000)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45000,
        # Пункт 4.5 поставил в `arm` сторож терминального состояния (мина AT), поэтому
        # у живого заказа состояние обязано быть настоящим, а не подразумеваться.
        lifecycle_state='confirmed',
    )

    with (
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.arm_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first._render_checkout',
            AsyncMock(),
        ) as render,
    ):
        await arm(callback, user, AsyncMock(), AsyncMock())

    render.assert_awaited_once()

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 35000},
        ) as serialize,
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    serialize.assert_called_once_with(checkout, balance_kopeks=10000)
    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    assert '5 устройств' in caption
    assert '90 дней' in caption
    assert '450 ₽' in caption
    assert '350 ₽' in caption
    assert keyboard[-2][0].callback_data == 'df:s:owned-checkout'


@pytest.mark.asyncio
async def test_arm_confirmation_shows_the_same_provider_valid_minimum_top_up_as_the_invoice() -> None:
    callback = SimpleNamespace()
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=30_050)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        quoted_price_kopeks=30_100,
        max_price_kopeks=30_100,
    )

    with patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output:
        await _render_arm_confirmation(callback, user, checkout)

    button = output.await_args.kwargs['keyboard'].inline_keyboard[0][0]
    assert button.text == 'Пополнить 100 ₽ и оформить'
    assert 'останется на балансе' in output.await_args.kwargs['caption']


@pytest.mark.asyncio
async def test_awaiting_checkout_with_funded_balance_requires_explicit_continue() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=45000)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45000,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 0},
        ),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Баланс пополнен' in caption
    assert keyboard[-2][0].callback_data == 'df:a:owned-checkout'


@pytest.mark.asyncio
async def test_awaiting_checkout_without_payment_methods_explains_the_safe_recovery_actions() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10_000)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45_000,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 35_000},
        ),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[]),
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert 'Пополнение сейчас недоступно' in caption
    assert 'Выберите способ доплаты' not in caption
    assert 'df:s:owned-checkout' in callbacks
    assert 'menu_support' in callbacks
    assert 'df:e:owned-checkout' in callbacks
    assert 'df:x:owned-checkout' in callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('language', 'expected_method'),
    [('ru', 'Карта российского банка'), ('en', 'Russian bank card')],
)
async def test_pending_invoice_uses_a_localized_method_name_and_blocks_second_method(
    language: str, expected_method: str
) -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language=language, balance_kopeks=0)
    checkout = SimpleNamespace(
        id=101,
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45000,
    )
    attempt = SimpleNamespace(
        status='pending',
        method_key='cards_ru',
        requested_amount_kopeks=35000,
        redirect_url='https://pay.example/invoice',
    )

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 35000},
        ),
        patch(
            'app.handlers.subscription.device_first.get_pending_platega_attempt',
            AsyncMock(return_value=attempt),
        ),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(),
        ) as methods,
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    assert expected_method in caption
    assert 'cards_ru' not in caption
    assert keyboard[0][0].url == 'https://pay.example/invoice'
    assert keyboard[1][0].callback_data == 'df:s:owned-checkout'
    assert keyboard[2][0].callback_data == 'df:e:owned-checkout'
    assert keyboard[2][1].callback_data == 'df:x:owned-checkout'
    assert keyboard[3][0].callback_data == 'back_to_menu'
    methods.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_invoice_without_a_payment_url_offers_only_safe_recovery_actions() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='en', balance_kopeks=0)
    checkout = SimpleNamespace(
        id=101,
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45_000,
    )
    attempt = SimpleNamespace(
        status='pending',
        method_key='sbp',
        requested_amount_kopeks=35_000,
        redirect_url=None,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 35_000},
        ),
        patch('app.handlers.subscription.device_first.get_pending_platega_attempt', AsyncMock(return_value=attempt)),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert 'Checking the invoice' in caption
    assert 'df:s:owned-checkout' in callbacks
    assert 'menu_support' in callbacks
    assert 'back_to_menu' in callbacks
    assert all(not callback.startswith('df:x:') for callback in callbacks)


@pytest.mark.asyncio
async def test_non_live_known_direct_attempt_never_renders_payment_methods_again() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(
        id=101,
        public_id='owned-checkout',
        settlement_mode='direct_purchase_v2',
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=201))

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'awaiting_payment', 'shortage_kopeks': 35_000},
        ),
        patch('app.handlers.subscription.device_first.get_pending_platega_attempt', AsyncMock(return_value=None)),
        patch('app.handlers.subscription.device_first._render_direct_payment_methods', AsyncMock()) as methods,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, db, checkout)

    caption = output.await_args.kwargs['caption']
    callbacks = [
        button.callback_data
        for row in output.await_args.kwargs['keyboard'].inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert 'Проверяем счёт' in caption
    assert 'menu_support' in callbacks
    assert 'back_to_menu' in callbacks
    methods.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('ui_state', 'caption_fragment', 'expected_callback'),
    [
        ('cancelled', 'Заказ отменён', 'df:start'),
        ('expired', 'Нужен новый расчёт', 'df:start'),
        ('reprice_required', 'Нужен новый расчёт', 'df:start'),
        ('conflict', 'Заказ нельзя продолжить', 'df:start'),
        ('failed', 'Не удалось завершить заказ', 'menu_support'),
    ],
)
async def test_terminal_checkout_states_have_honest_recovery_messages(
    ui_state: str, caption_fragment: str, expected_callback: str
) -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout')

    with (
        patch('app.handlers.subscription.device_first.serialize_checkout', return_value={'ui_state': ui_state}),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    callbacks = [
        button.callback_data
        for row in output.await_args.kwargs['keyboard'].inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert caption_fragment in caption
    assert expected_callback in callbacks
    assert 'Расчёт изменился' not in caption


@pytest.mark.asyncio
async def test_payment_amount_mismatch_tells_the_user_that_money_is_on_balance() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=40_000)
    checkout = SimpleNamespace(public_id='owned-checkout')

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'conflict', 'terminal_reason': 'payment_amount_mismatch'},
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    assert 'Деньги зачислены на баланс' in caption
    assert 'Новый расчёт' in str(output.await_args.kwargs['keyboard'])


@pytest.mark.asyncio
async def test_change_selection_navigates_without_cancelling_a_pending_invoice() -> None:
    callback = SimpleNamespace(data='df:e:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(id=101, public_id='owned-checkout')

    state = AsyncMock()
    options = {'eligible': True, 'tariff': {'name': 'Базовый'}, 'period_options': [30], 'device_options': [2]}
    with (
        patch('app.handlers.subscription.device_first.get_owned_checkout', AsyncMock(return_value=checkout)),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.abandon_direct_checkout_for_new_calculation', AsyncMock()
        ) as abandon,
        patch('app.handlers.subscription.device_first.cancel_checkout', AsyncMock()) as cancel_checkout,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        await change_selection(callback, user, AsyncMock(), state)

    abandon.assert_not_awaited()
    cancel_checkout.assert_not_awaited()
    period_page.assert_awaited_once()
    assert state.update_data.await_args.kwargs['df_checkout_id'] == 'owned-checkout'


@pytest.mark.asyncio
async def test_choose_devices_never_touches_an_existing_checkout() -> None:
    """The showcase renders from FSM even with a live invoice: resume/supersede
    of that invoice moved into the fused pay handlers (``df:y2:``/``df:a2:``)."""
    callback = SimpleNamespace(data='df:d:view1234:4', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    options = {
        'eligible': True,
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2, 4],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 4, 'price_kopeks': 36_900}]}],
    }
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'df_options': options,
                'df_view_id': 'view1234',
                'df_days': 30,
                'df_origin_callback': 'back_to_menu',
            }
        ),
        update_data=AsyncMock(),
    )

    with (
        patch('app.handlers.subscription.device_first.get_open_checkout_for_user', AsyncMock()) as get_open,
        patch(
            'app.handlers.subscription.device_first.abandon_direct_checkout_for_new_calculation', AsyncMock()
        ) as abandon_service,
        patch('app.handlers.subscription.device_first.create_or_resume_direct_checkout', AsyncMock()) as create,
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.handlers.subscription.device_first._render_fused_confirmation', AsyncMock()) as render,
    ):
        await choose_devices(callback, user, SimpleNamespace(), state)

    get_open.assert_not_awaited()
    abandon_service.assert_not_awaited()
    create.assert_not_awaited()
    state.update_data.assert_not_awaited()
    render.assert_awaited_once()
    assert render.await_args.kwargs['days'] == 30
    assert render.await_args.kwargs['devices'] == 4


@pytest.mark.asyncio
async def test_processing_checkout_shows_paid_message_without_cancel_button() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout', id=101)

    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            return_value={'ui_state': 'processing'},
        ),
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), checkout)

    caption = output.await_args.kwargs['caption']
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert 'Платёж подтверждён' in caption
    assert 'отменить его нельзя' in caption
    assert f'df:s:{checkout.public_id}' in callbacks
    assert all(not callback_data.startswith('df:x:') for callback_data in callbacks)


@pytest.mark.asyncio
async def test_explicit_abandon_after_credit_rerenders_processing_instead_of_claiming_cancelled() -> None:
    callback = SimpleNamespace(data='df:xa:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout', lifecycle_state='processing')
    state = SimpleNamespace(clear=AsyncMock())
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first._render_checkout',
            AsyncMock(),
        ) as render_checkout,
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render_cancelled,
    ):
        await abandon(callback, user, db, state)

    state.clear.assert_not_awaited()
    render_checkout.assert_awaited_once_with(callback, user, db, checkout)
    render_cancelled.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_invoice_cancel_first_requires_explicit_customer_confirmation() -> None:
    callback = SimpleNamespace(data='df:x:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout', settlement_mode='direct_purchase_v2')

    with (
        patch('app.handlers.subscription.device_first.get_owned_checkout', AsyncMock(return_value=checkout)),
        patch(
            'app.handlers.subscription.device_first.abandon_direct_checkout_for_new_calculation', AsyncMock()
        ) as abandon_service,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await cancel(callback, user, AsyncMock(), AsyncMock())

    abandon_service.assert_not_awaited()
    keyboard = output.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'df:xa:owned-checkout'
    assert keyboard[1][0].callback_data == 'df:s:owned-checkout'


@pytest.mark.asyncio
async def test_pay_binds_method_checkout_and_owner_then_renders_the_canonical_invoice_state() -> None:
    callback = SimpleNamespace(data='df:y:sbp:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    attempt = SimpleNamespace(redirect_url='https://pay.example/invoice')
    checkout = SimpleNamespace(public_id='owned-checkout')

    with (
        patch(
            'app.handlers.subscription.device_first.create_platega_attempt',
            AsyncMock(return_value=attempt),
        ) as create,
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(),
        ) as get_owned,
        patch(
            'app.handlers.subscription.device_first._render_checkout',
            AsyncMock(),
        ) as render,
    ):
        get_owned.return_value = checkout
        await pay(callback, user, AsyncMock(), AsyncMock())

    create.assert_awaited_once()
    assert create.await_args.kwargs == {
        'checkout_public_id': 'owned-checkout',
        'user_id': 17,
        'method_key': 'sbp',
    }
    get_owned.assert_awaited_once_with(ANY, public_id='owned-checkout', user_id=17)
    render.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_error_tells_telegram_user_not_to_pay_again_and_offers_safe_recovery() -> None:
    callback = SimpleNamespace(data='df:y:sbp:owned-checkout')
    error = DeviceFirstError('reconciliation_required', 'Payment requires reconciliation')

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _render_error(callback, _user(), error)

    assert 'Не оплачивайте повторно' in render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'df:s:owned-checkout'
    assert keyboard[1][0].callback_data == 'menu_support'
    assert keyboard[2][0].callback_data == 'back_to_menu'
    assert all(
        not button.callback_data.startswith('df:x:') for row in keyboard for button in row if button.callback_data
    )


@pytest.mark.asyncio
async def test_legacy_trial_reconciliation_error_explains_the_hold_and_offers_support() -> None:
    callback = SimpleNamespace(data='df:d:2')
    error = DeviceFirstError('legacy_trial_reconciliation_required', 'Legacy trial payment requires review')

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _render_error(callback, _user(), error)

    assert 'незавершённая предыдущая оплата' in render.await_args.kwargs['caption']
    assert 'Не оплачивайте повторно' in render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'menu_support'
    assert keyboard[1][0].callback_data == 'back_to_menu'


@pytest.mark.asyncio
async def test_fused_confirmation_offers_the_wallet_button_when_balance_covers() -> None:
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50_000)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900}]}],
    }

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ) as methods,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, AsyncMock(), options, days=30, devices=2)

    methods.assert_awaited_once()
    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Оплатите с баланса.' in caption
    assert keyboard[0][0].callback_data == 'df:a2:30:2:36900'
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert callbacks == ['df:a2:30:2:36900', 'df:e2', 'df:x2']


@pytest.mark.asyncio
async def test_fused_confirmation_says_nothing_new_to_a_customer_without_money() -> None:
    """Этап БК обещал: у кого на балансе ноль — экран не меняется ни на знак.

    Сторож стоит ПЕРВЫМ среди сторожей этапа намеренно: это единственный его критерий
    приёмки, который до сих пор не проверял никто, и ломается он тише всех — ветка
    «иначе» одна обслуживала и человека с нулём, и человека с частью суммы, поэтому
    любая строка про деньги, написанная не в свою ветку, уходила бы 141 человеку,
    у которого этих денег нет.
    """
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    options = {
        'tariff': {'name': 'Базовый'},
        'device_options': [2],
        'period_options': [30],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900}]}],
    }
    db = _db()

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=None),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, db, options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert caption == (
        '💳 <b>Ваш заказ</b>\n\n'
        '<b>Базовый</b>\n'
        '2 устройства · 1 месяц\n'
        'К оплате: <b>369 ₽</b>\n\n'
        'Выберите способ оплаты.'
    )
    # Ни одного слова про деньги, которых у него нет, и ни одного лишнего запроса к базе.
    assert 'Баланс' not in caption
    assert 'Не хватает' not in caption
    db.scalar.assert_not_awaited()
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert callbacks == ['df:y2:sbp:30:2:36900', 'df:e2', 'df:x2']


@pytest.mark.asyncio
async def test_fused_confirmation_shows_the_wallet_and_a_top_up_door_on_a_partial_balance() -> None:
    """Сердце этапа: 114 человек держат по 50 ₽, и касса их не показывала."""
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=5_000)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/top-up?safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, _db(), options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert '💳 Баланс: 50 ₽' in caption
    assert '⚠️ Не хватает: 199 ₽' in caption
    # Обратная дорога названа ДО того, как человек ушёл платить: доплата заказ не оформляет.
    assert 'Ваш баланс уже учтён в строке «Не хватает». Доплатите и продолжите покупку в том же окне.' in caption
    # 🔴 РЕК-16.3 переписал это ожидание, и это ЗАЯВЛЕНИЕ, а не подкрутка под новый текст.
    # Здесь требовалась строка «этот экран не обновится: его кнопки возьмут полную цену».
    # С этапа РЕК-8а она неверна: кнопки способов — `web_app` в кабинет с `autostart=1`, а
    # кабинет по ним счёт не выставляет, пока на счету есть деньги, а округлённая недостача
    # меньше цены. Это ровно состояние, в котором печатается абзац. Предупреждение о вреде,
    # которого больше нет, — такая же ложь, как молчание о настоящем.
    # ⚠️ Граница честно: забор стережёт отсутствие СНЯТОГО обещания по его словам. Новую
    # неправду другими словами он не поймает — это цена любого текстового сторожа.
    assert 'не обновится' not in caption
    assert 'полную цену' not in caption
    # Остаток мины DE: строка про баланс без этой оговорки читается как «зачтётся».
    assert 'Или оплатите полной суммой: деньги с баланса при этом не спишутся.' in caption
    # Провайдерский минимум здесь меньше недостачи, значит второго числа на экране нет.
    assert 'останется на балансе' not in caption
    assert keyboard[0][0].text == '💰 Доплатить 199 ₽'
    assert build_cabinet_url.call_args_list[0].args[0] == (
        '/balance/top-up/platega?returnTo=%2Fsubscription%2Fpurchase'
        '%3Ffrom%3Dcheckout%26period%3D30%26devices%3D2&amount=199'
    )
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert callbacks == ['df:e2', 'df:x2']


@pytest.mark.asyncio
async def test_fused_confirmation_hides_the_top_up_door_while_an_order_is_open() -> None:
    """Живой счёт закрепляет способ оплаты: доплата поверх него кончится отказом.

    Человек доплатил бы, вернулся, нажал «Списать с баланса» — и получил
    ``funding_mode_locked`` вместе с экраном старого счёта, где главная кнопка
    предлагает заплатить второй раз. Строки про его деньги при этом остаются:
    там они верны ровно так же.

    ⚠️ Прежняя редакция сторожа проверяла ещё и испорченную строку заказа: служебный
    поиск бросал на ней исключение. Этого случая больше нет ПО ПОСТРОЕНИЮ — экран
    спрашивает базу чистым чтением и про признак строки не спрашивает вовсе.
    """
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=5_000)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/safe',
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, _db(open_order=True), options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    assert '💳 Баланс: 50 ₽' in caption
    assert '⚠️ Не хватает: 199 ₽' in caption
    assert 'уже учтён в строке' not in caption
    # Проверяем то, что видит ЧЕЛОВЕК: на экране нет кнопки с надписью «Доплатить».
    # По ссылке дверь от кнопки оплаты не отличить — подменённый `build_cabinet_url`
    # отдаёт одно и то же всем. А адрес доплаты код собирает и выбрасывает намеренно:
    # дешёвые признаки считаются раньше запроса к базе.
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert not any(TOP_UP_LABEL in button.text for row in keyboard for button in row)
    # ⚠️ Адресной проверки здесь быть НЕ МОЖЕТ, и это следствие порядка вычислений:
    # забор «есть заказ в работе» стоит ПОСЛЕ дешёвых признаков, поэтому адрес доплаты
    # успевает собраться и выбрасывается. Проверять нечего — кнопки на экране нет.


@pytest.mark.asyncio
async def test_fused_confirmation_hides_the_top_up_door_when_it_would_cost_the_full_price() -> None:
    """У кого на балансе одни копейки, минимум провайдера поднимает доплату до полной цены.

    Тогда это те же деньги дорогой в пять кадров, и предлагать её нечестно. Тот же
    выбор кабинет делает у себя тем же сравнением.
    """
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }
    db = _db()

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, db, options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    assert '💳 Баланс: 0,50 ₽' in caption
    assert '⚠️ Не хватает: 248,50 ₽' in caption
    assert 'уже учтён в строке' not in caption
    # Отказ наступает ДО обращения к базе: сравнение сумм дешевле запроса.
    db.scalar.assert_not_awaited()
    # Проверяем то, что видит ЧЕЛОВЕК: на экране нет кнопки с надписью «Доплатить».
    # По ссылке дверь от кнопки оплаты не отличить — подменённый `build_cabinet_url`
    # отдаёт одно и то же всем. А адрес доплаты код собирает и выбрасывает намеренно:
    # дешёвые признаки считаются раньше запроса к базе.
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert not any(TOP_UP_LABEL in button.text for row in keyboard for button in row)
    # Слова на кнопке мало: та же дверь под другой надписью прошла бы мимо. Здесь забор
    # срабатывает ДО сборки адреса, поэтому адрес можно проверить — и он не запрашивался.
    assert not any(call.args[0].startswith(TOP_UP_PREFIX) for call in build_cabinet_url.call_args_list)


@pytest.mark.asyncio
async def test_direct_payment_methods_screen_shows_the_wallet_and_the_top_up_door() -> None:
    """Этап БК-2: второй экран «Ваш заказ» перестал прятать деньги клиента.

    Он показывается, когда заказ уже заведён, — то есть тому, кто нажал способ оплаты,
    испугался и вышел. На боевом таких выходов 29 против 14 доведённых до оплаты.
    До этого этапа он знал два состояния кошелька вместо трёх, и человек, видевший свой
    баланс минуту назад, переставал его видеть.
    """
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=5_000)
    # 🔴 Числа НАРОЧНО не совпадают с умолчаниями первого экрана (30 дней / 2 устройства):
    # мутация «подставить 30 и 2 вместо полей заказа» пережила сторож, написанный на совпадающих
    # значениях. Сторож обязан различать источник данных, а не подтверждать совпадение.
    checkout = SimpleNamespace(
        public_id='co-1',
        tariff_id=3,
        tariff_total_kopeks=118_900,
        selected_device_limit=5,
        period_days=90,
        funding_mode=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(name='Базовый'))

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_direct_payment_methods(callback, user, db, checkout)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert '5 устройств · 3 месяца' in caption
    assert '💳 Баланс: 50 ₽' in caption
    assert '⚠️ Не хватает: 1 139 ₽' in caption
    assert 'Ваш баланс уже учтён в строке «Не хватает». Доплатите и продолжите покупку в том же окне.' in caption
    # РЕК-16.3: см. объяснение у первого такого забора выше.
    assert 'не обновится' not in caption
    assert 'Или оплатите полной суммой: деньги с баланса при этом не спишутся.' in caption
    assert keyboard[0][0].text == '💰 Доплатить 1 139 ₽'
    # Адрес возврата несёт СРОК И УСТРОЙСТВА ЭТОГО ЗАКАЗА, а не выбор из состояния диалога:
    # на этом экране состояния может уже не быть, а строка заказа есть всегда.
    assert build_cabinet_url.call_args_list[0].args[0] == (
        '/balance/top-up/platega?returnTo=%2Fsubscription%2Fpurchase'
        '%3Ffrom%3Dcheckout%26period%3D90%26devices%3D5&amount=1139'
    )


@pytest.mark.asyncio
async def test_direct_payment_methods_screen_says_nothing_new_to_a_customer_without_money() -> None:
    """Тот же критерий, что и на первом экране: у кого ноль — ни одного нового знака."""
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(
        public_id='co-1',
        tariff_id=3,
        tariff_total_kopeks=24_900,
        selected_device_limit=2,
        period_days=30,
        funding_mode=None,
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(name='Базовый'))

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=None),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_direct_payment_methods(callback, user, db, checkout)

    caption = render.await_args.kwargs['caption']
    assert caption == (
        '💳 <b>Ваш заказ</b>\n\n'
        '<b>Базовый</b>\n'
        '2 устройства · 1 месяц\n'
        'К оплате: <b>249 ₽</b>\n\n'
        'Выберите способ оплаты.'
    )
    assert 'Баланс' not in caption
    assert 'Не хватает' not in caption


@pytest.mark.asyncio
async def test_direct_payment_methods_screen_hides_the_door_when_the_funding_mode_is_fixed() -> None:
    """Способ оплаты за заказом уже закреплён — доплата кончилась бы отказом.

    Человек доплатил бы, вернулся, нажал «Оплатить с баланса» и получил
    ``funding_mode_locked`` вместе с экраном старого счёта, где главная кнопка предлагает
    заплатить второй раз. Строки про его деньги остаются: там они верны ровно так же.
    """
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=5_000)
    checkout = SimpleNamespace(
        public_id='co-1',
        tariff_id=3,
        tariff_total_kopeks=24_900,
        selected_device_limit=2,
        period_days=30,
        funding_mode='platega',
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(name='Базовый'))

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url', return_value='https://cabinet.example/safe'
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_direct_payment_methods(callback, user, db, checkout)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert '💳 Баланс: 50 ₽' in caption
    assert '⚠️ Не хватает: 199 ₽' in caption
    assert 'уже учтён в строке' not in caption
    assert not any(TOP_UP_LABEL in button.text for row in keyboard for button in row)
    # Слова на кнопке мало: та же дверь под другой надписью прошла бы мимо. Здесь забор
    # срабатывает ДО сборки адреса, поэтому адрес можно проверить — и он не запрашивался.
    assert not any(call.args[0].startswith(TOP_UP_PREFIX) for call in build_cabinet_url.call_args_list)


@pytest.mark.asyncio
async def test_fused_confirmation_explains_the_change_when_the_provider_minimum_exceeds_the_shortfall() -> None:
    """Второе число на экране появляется, только когда минимум провайдера больше недостачи.

    Тогда в сводке одна сумма, а на кнопке другая, и без объяснения это читается как ошибка.
    Ветка была единственным новым вычислением этапа, не закрытым ничем: мутация её переживала.
    """
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=20_000)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/safe',
        ) as build_cabinet_url,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, _db(), options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    # Недостача честная (49 ₽), а счёт выставят на минимум провайдера (100 ₽) — и разницу
    # экран называет сам, теми же словами, что и кабинет.
    assert '⚠️ Не хватает: 49 ₽' in caption
    assert 'После оформления 51 ₽ останется на балансе.' in caption
    assert keyboard[0][0].text == '💰 Доплатить 100 ₽'
    assert build_cabinet_url.call_args_list[0].args[0].endswith('&amount=100')


@pytest.mark.asyncio
async def test_fused_confirmation_speaks_english_to_an_english_customer() -> None:
    """Весь английский экран этапа не сторожил никто: любую строку можно было заменить молча."""
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='en', balance_kopeks=5_000)
    options = {
        'tariff': {'name': 'Basic'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            return_value='https://cabinet.example/safe',
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, _db(), options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert '💳 Balance: ₽50' in caption
    assert '⚠️ Shortage: ₽199' in caption
    assert (
        'Your balance is already counted in the «Shortage» line. Top up and finish the purchase in the same window.'
        in caption
    )
    # РЕК-16.3: обе половины снятого предупреждения — и по-английски тоже.
    assert 'will not refresh' not in caption
    assert 'charge the full price' not in caption
    assert 'Or pay the full amount: your balance stays untouched.' in caption
    assert keyboard[0][0].text == '💰 Top up ₽199'


@pytest.mark.asyncio
async def test_fused_confirmation_does_not_argue_with_itself_when_payment_is_unavailable() -> None:
    """Единственное состояние, где деньги на балансе видны, а потратить их нечем.

    Экран не должен звать выбрать способ оплаты и обсуждать списание, которого не может
    быть, а строкой ниже сообщать, что оплата недоступна. Строки про деньги остаются:
    они верны и здесь.
    """
    callback = SimpleNamespace(data='df:d:view1234:2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=5_000)
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 24_900}]}],
    }
    db = _db()

    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value='https://cabinet.example/safe'),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await _render_fused_confirmation(callback, user, db, options, days=30, devices=2)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert caption == (
        '💳 <b>Ваш заказ</b>\n\n'
        '<b>Базовый</b>\n'
        '2 устройства · 1 месяц\n'
        'К оплате: <b>249 ₽</b>\n\n'
        '💳 Баланс: 50 ₽\n'
        '⚠️ Не хватает: 199 ₽\n\n'
        'Оплата временно недоступна. Обратитесь в поддержку.'
    )
    # Дверь доплаты сюда не ставится: она ведёт к тому же провайдеру, которого сейчас нет.
    db.scalar.assert_not_awaited()
    callbacks = [button.callback_data for row in keyboard for button in row if button.callback_data]
    assert callbacks == ['menu_support', 'df:e2', 'df:x2']


def test_fused_pay_callbacks_fit_the_telegram_byte_budget() -> None:
    from app.services.device_first_payment_service import PLATEGA_METHODS

    # Telegram allows at most 64 bytes of callback_data; the fused callbacks
    # carry the whole order plus the optimistic price, so check the worst case
    # with every real method key and maximal realistic values.
    for method_key in PLATEGA_METHODS:
        data = f'df:y2:{method_key}:365:10:9999999'
        assert len(data.encode()) <= 64, data
    assert len(b'df:a2:365:10:9999999') <= 64
    # The fused prefixes must never collide with the legacy startswith filters.
    assert not 'df:y2:sbp:30:2:36900'.startswith('df:y:')
    assert not 'df:a2:30:2:36900'.startswith('df:a:')


@pytest.mark.asyncio
async def test_pay_fused_births_the_checkout_and_its_invoice_only_at_pay_time() -> None:
    callback = SimpleNamespace(data='df:y2:sbp:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='fused-checkout')
    resolved = SimpleNamespace(checkout=checkout, proceed_to_payment=True)
    attempt = SimpleNamespace(redirect_url='https://pay.example/invoice')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ) as fused,
        patch(
            'app.handlers.subscription.device_first.create_platega_attempt',
            AsyncMock(return_value=attempt),
        ) as create_attempt,
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
    ):
        await pay_fused(callback, user, db, AsyncMock())

    assert fused.await_args.kwargs == {
        'user': user,
        'period_days': 30,
        'selected_device_limit': 2,
        'expected_tariff_total_kopeks': 36_900,
        'funding_mode': 'platega',
        'method_key': 'sbp',
        'source': 'telegram',
    }
    create_attempt.assert_awaited_once_with(db, checkout_public_id='fused-checkout', user_id=17, method_key='sbp')
    render.assert_awaited_once_with(callback, user, db, checkout)


@pytest.mark.asyncio
async def test_pay_wallet_fused_debits_the_balance_only_at_pay_time() -> None:
    callback = SimpleNamespace(data='df:a2:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50_000)
    checkout = SimpleNamespace(public_id='fused-checkout')
    resolved = SimpleNamespace(checkout=checkout, proceed_to_payment=True)
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ) as fused,
        patch(
            'app.handlers.subscription.device_first.commit_direct_wallet_checkout',
            AsyncMock(return_value=checkout),
        ) as commit,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
    ):
        await pay_wallet_fused(callback, user, db, AsyncMock())

    assert fused.await_args.kwargs == {
        'user': user,
        'period_days': 30,
        'selected_device_limit': 2,
        'expected_tariff_total_kopeks': 36_900,
        'funding_mode': 'wallet',
        'method_key': None,
        'source': 'telegram',
    }
    commit.assert_awaited_once_with(db, public_id='fused-checkout', user_id=17)
    render.assert_awaited_once_with(callback, user, db, checkout)


@pytest.mark.asyncio
async def test_pay_fused_reprice_rerenders_a_fresh_confirmation_without_a_row_or_a_post() -> None:
    callback = SimpleNamespace(data='df:y2:sbp:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    options = {
        'eligible': True,
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 39_900}]}],
    }
    state = AsyncMock()
    db = AsyncMock()
    reprice = DeviceFirstError('reprice_required', 'The price changed')

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=reprice),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=None),
        patch('app.handlers.subscription.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await pay_fused(callback, user, db, state)

    create_attempt.assert_not_awaited()
    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Цена обновилась' in caption
    assert '399 ₽' in caption
    assert keyboard[0][0].callback_data == 'df:y2:sbp:30:2:39900'


@pytest.mark.asyncio
async def test_pay_wallet_fused_with_a_live_invoice_requires_the_explicit_abandon_screen() -> None:
    callback = SimpleNamespace(data='df:a2:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50_000)
    existing = SimpleNamespace(public_id='live-invoice')
    locked = DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=locked),
        ),
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=existing),
        ),
        patch('app.handlers.subscription.device_first.commit_direct_wallet_checkout', AsyncMock()) as commit,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._render_error', AsyncMock()) as render_error,
    ):
        await pay_wallet_fused(callback, user, db, AsyncMock())

    commit.assert_not_awaited()
    render.assert_awaited_once_with(callback, user, db, existing)
    render_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_pay_fused_resume_of_a_paid_race_renders_the_canonical_checkout_state() -> None:
    callback = SimpleNamespace(data='df:y2:sbp:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    settled = SimpleNamespace(public_id='settled-checkout')
    resolved = SimpleNamespace(checkout=settled, proceed_to_payment=False)
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ),
        patch('app.handlers.subscription.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
    ):
        await pay_fused(callback, user, db, AsyncMock())

    create_attempt.assert_not_awaited()
    render.assert_awaited_once_with(callback, user, db, settled)


@pytest.mark.asyncio
async def test_pay_wallet_fused_insufficient_balance_rerenders_the_confirmation() -> None:
    callback = SimpleNamespace(data='df:a2:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10_000)
    checkout = SimpleNamespace(public_id='fused-checkout')
    resolved = SimpleNamespace(checkout=checkout, proceed_to_payment=True)
    options = {
        'eligible': True,
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900}]}],
    }
    insufficient = DeviceFirstError('wallet_insufficient', 'The balance does not cover this checkout', status_code=422)
    state = AsyncMock()
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ),
        patch(
            'app.handlers.subscription.device_first.commit_direct_wallet_checkout',
            AsyncMock(side_effect=insufficient),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=None),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await pay_wallet_fused(callback, user, db, state)

    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    # Точное равенство, а не вхождение: этап БК убрал из предупреждения вторую фразу
    # («Выберите способ оплаты»), потому что тело экрана под ним теперь говорит другое.
    # Проверка на вхождение зеленела бы при обеих редакциях и пункт не сторожила бы вовсе.
    assert caption.startswith('⚠️ Баланс больше не покрывает заказ.\n\n')
    assert keyboard[0][0].callback_data == 'df:y2:sbp:30:2:36900'


@pytest.mark.asyncio
async def test_change_selection_fused_restarts_the_period_page_without_a_checkout() -> None:
    callback = SimpleNamespace(data='df:e2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    options = {'eligible': True, 'tariff': {'name': 'Базовый'}, 'period_options': [30], 'device_options': [2]}
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={'df_origin_callback': 'funnel_tariffs'}),
        update_data=AsyncMock(),
    )

    with (
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch('app.handlers.subscription.device_first.get_open_checkout_for_user', AsyncMock()) as get_open,
        patch('app.handlers.subscription.device_first._period_page', AsyncMock()) as period_page,
    ):
        await change_selection_fused(callback, user, AsyncMock(), state)

    get_open.assert_not_awaited()
    period_page.assert_awaited_once()
    assert period_page.await_args.kwargs['origin_callback'] == 'funnel_tariffs'


@pytest.mark.asyncio
async def test_cancel_fused_clears_state_without_touching_any_checkout() -> None:
    callback = SimpleNamespace(data='df:x2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    state = SimpleNamespace(clear=AsyncMock())

    with (
        patch('app.handlers.subscription.device_first.get_open_checkout_for_user', AsyncMock()) as get_open,
        patch('app.handlers.subscription.device_first.cancel_checkout', AsyncMock()) as cancel_checkout,
        # 🔴 РЕК-8в: заказа НЕТ — это и есть случай, ради которого прежняя формулировка писалась.
        patch(
            'app.handlers.subscription.device_first._has_order_in_flight',
            AsyncMock(return_value=False),
        ) as in_flight,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await cancel_fused(callback, user, AsyncMock(), state)

    get_open.assert_not_awaited()
    cancel_checkout.assert_not_awaited()
    # 🔴 Заглядывать в базу теперь ОБЯЗАНО: без этого утверждение про деньги — догадка.
    in_flight.assert_awaited_once()
    state.clear.assert_awaited_once()
    assert 'Деньги не списаны' in render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'back_to_menu'


@pytest.mark.asyncio
async def test_cancel_fused_does_not_claim_a_cancellation_when_an_order_is_alive() -> None:
    """🔴 РЕК-8в. Кнопка стоит на экране, соседние кнопки которого открывают кабинет меткой
    ``autostart=1`` — заказ рождается там, а сообщение в чате остаётся прежним. Прежде эта
    кнопка отвечала «Заказ отменён. Деньги не списаны», не заглянув в базу, и человек уходил
    в уверенности, что закрыл живой счёт."""
    callback = SimpleNamespace(data='df:x2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    state = SimpleNamespace(clear=AsyncMock())

    with (
        patch('app.handlers.subscription.device_first.cancel_checkout', AsyncMock()) as cancel_checkout,
        patch(
            'app.handlers.subscription.device_first._has_order_in_flight',
            AsyncMock(return_value=True),
        ) as in_flight,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await cancel_fused(callback, user, AsyncMock(), state)

    # 🔴 Сторож на САМУЮ дорогую половину находки: вопрос «врём ли мы про деньги» обязан
    # видеть и остановившиеся заказы, где деньги уже взяты. Состояния перечислены
    # ЛИТЕРАЛАМИ: сторож, повторяющий выражение кода, доказывает только сам себя.
    # 🔴 Ровно шесть состояний, перечислены ЛИТЕРАЛАМИ. `operator_review` — единственное
    # «остановившееся», где деньги взяты И которое умеет показать кнопка ниже. Три соседних
    # (`conflict`, `failed`, `reprice_required`) сюда НЕ входят намеренно: они доплатёжные,
    # `get_open_checkout_for_user` их не выбирает, а `reprice_required` ставится обычным
    # истечением котировки и живёт вечно — спрашивая его, мы объявляли бы «у вас открыт заказ»
    # каждому, кто когда-либо бросил расчёт.
    assert set(in_flight.await_args.kwargs['states']) == {
        'draft',
        'confirmed',
        'awaiting_funds',
        'armed',
        'fulfilling',
        'operator_review',
    }

    caption = render.await_args.kwargs['caption']
    # Ни слова про отмену и ни слова про деньги: заказ бывает и уже оплаченным
    # (``armed``/``fulfilling`` входят в тот же набор состояний).
    assert 'Заказ отменён' not in caption
    assert 'не списаны' not in caption
    assert 'Заказ не отменён' in caption
    assert 'остаётся открытым' in caption
    # ⛔ И сам заказ отсюда не отменяется: у настоящей отмены своё предупреждение про живую
    # платёжную ссылку и своё второе подтверждение.
    cancel_checkout.assert_not_awaited()
    # Дверь к настоящему заказу, а не тупик.
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].callback_data == 'df:start'
    assert keyboard[1][0].callback_data == 'back_to_menu'


@pytest.mark.asyncio
async def test_has_order_in_flight_asks_the_database_for_exactly_the_states_it_was_given() -> None:
    """🔴 Сторож на САМ ЗАПРОС, а не на его мок. Оба соседних теста `cancel_fused` подменяют
    `_has_order_in_flight` целиком — значит расширенный набор состояний до базы не доезжает ни
    разу, и сужение или расширение фильтра прошло бы мимо всех проверок. Здесь исполняется
    настоящее выражение и читается СКОМПИЛИРОВАННЫЙ SQL: приём уже есть в проекте."""
    from app.handlers.subscription.device_first import _has_order_in_flight

    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)

    assert await _has_order_in_flight(db, user_id=17, states=('draft', 'operator_review')) is False

    statement = db.scalar.await_args[0][0]
    compiled = statement.compile(compile_kwargs={'literal_binds': True})
    sql = str(compiled)
    assert "'draft'" in sql
    assert "'operator_review'" in sql
    # Состояния, которых в наборе НЕ было, в запрос попасть не могут.
    assert "'reprice_required'" not in sql
    assert 'subscription_checkouts' in sql


@pytest.mark.asyncio
async def test_cancel_fused_says_nothing_about_money_when_the_read_fails() -> None:
    """🔴 РЕК-8в, названная граница. Раньше функция не могла отказать вовсе (`del db`), теперь
    ходит в базу. При осечке чтения молчать про деньги честнее, чем успокаивать: сказать
    «не списаны», не сумев проверить, — это то же враньё, ради снятия которого правка и делалась."""
    callback = SimpleNamespace(data='df:x2', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    state = SimpleNamespace(clear=AsyncMock())

    with (
        patch(
            'app.handlers.subscription.device_first._has_order_in_flight',
            AsyncMock(side_effect=SQLAlchemyError('boom')),
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await cancel_fused(callback, user, AsyncMock(), state)

    caption = render.await_args.kwargs['caption']
    assert 'не списаны' not in caption
    assert 'Заказ не отменён' in caption


@pytest.mark.asyncio
async def test_legacy_pay_button_on_a_cancelled_draft_renders_an_error_not_a_hang() -> None:
    """After migration 0099 cancels a stale draft, its old ``df:y:`` button must
    land on the honest recovery screen instead of hanging or charging."""
    callback = SimpleNamespace(data='df:y:sbp:cancelled-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    invalid = DeviceFirstError('invalid_state', 'Checkout is not ready for a payment attempt')

    with (
        patch(
            'app.handlers.subscription.device_first.create_platega_attempt',
            AsyncMock(side_effect=invalid),
        ),
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render_checkout,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await pay(callback, user, AsyncMock(), AsyncMock())

    render_checkout.assert_not_awaited()
    caption = render.await_args.kwargs['caption']
    assert '⚠️' in caption
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[-1][0].callback_data == 'back_to_menu'


@pytest.mark.asyncio
async def test_pay_fused_open_checkout_conflict_renders_the_live_order_not_an_error() -> None:
    """A unique-index race loser sees the winner's canonical checkout screen."""
    callback = SimpleNamespace(data='df:y2:sbp:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    existing = SimpleNamespace(public_id='live-checkout')
    conflict = DeviceFirstError('open_checkout_exists', 'An active checkout already exists')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=conflict),
        ),
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=existing),
        ),
        patch('app.handlers.subscription.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._render_error', AsyncMock()) as render_error,
    ):
        await pay_fused(callback, user, db, AsyncMock())

    create_attempt.assert_not_awaited()
    render.assert_awaited_once_with(callback, user, db, existing)
    render_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_pay_fused_invalid_state_with_a_live_order_renders_its_canonical_screen() -> None:
    callback = SimpleNamespace(data='df:a2:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50_000)
    existing = SimpleNamespace(public_id='live-checkout')
    invalid = DeviceFirstError('invalid_state', 'Checkout is not ready for final confirmation')
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=invalid),
        ),
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=existing),
        ),
        patch('app.handlers.subscription.device_first._render_checkout', AsyncMock()) as render,
        patch('app.handlers.subscription.device_first._render_fused_refresh', AsyncMock()) as refresh,
        patch('app.handlers.subscription.device_first._render_error', AsyncMock()) as render_error,
    ):
        await pay_wallet_fused(callback, user, db, AsyncMock())

    render.assert_awaited_once_with(callback, user, db, existing)
    refresh.assert_not_awaited()
    render_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_pay_fused_invalid_state_without_a_live_order_rerenders_a_fresh_confirmation() -> None:
    """A race loser whose fresh row was swept gets a fresh quote, not a dead end."""
    callback = SimpleNamespace(data='df:y2:sbp:30:2:36900', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    options = {
        'eligible': True,
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [2],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900}]}],
    }
    invalid = DeviceFirstError('invalid_state', 'Checkout is not ready for final confirmation')
    state = AsyncMock()
    db = AsyncMock()

    with (
        patch(
            'app.handlers.subscription.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=invalid),
        ),
        patch(
            'app.handlers.subscription.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=None),
        ),
        patch('app.handlers.subscription.device_first.build_purchase_options', AsyncMock(return_value=options)),
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=None),
        patch('app.handlers.subscription.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        await pay_fused(callback, user, db, state)

    create_attempt.assert_not_awaited()
    caption = render.await_args.kwargs['caption']
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert 'Условия заказа изменились' in caption
    assert 'Ваш заказ' in caption
    assert keyboard[0][0].callback_data == 'df:y2:sbp:30:2:36900'


@pytest.mark.asyncio
async def test_provider_amount_out_of_range_offers_balance_top_up_or_support() -> None:
    callback = SimpleNamespace(data='df:y2:sbp:30:2:5000')
    error = DeviceFirstError('provider_amount_out_of_range', 'Required amount is outside Platega limits')

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _render_error(callback, _user(), error)

    caption = render.await_args.kwargs['caption']
    assert 'платёжную систему' in caption
    assert 'Пополните баланс' in caption
    assert 'поддержку' in caption


@pytest.mark.asyncio
async def test_provider_amount_out_of_range_offers_balance_top_up_or_support_in_english() -> None:
    callback = SimpleNamespace(data='df:y2:sbp:30:2:5000')
    error = DeviceFirstError('provider_amount_out_of_range', 'Required amount is outside Platega limits')

    with patch(
        'app.handlers.subscription.device_first.edit_or_answer_photo',
        AsyncMock(),
    ) as render:
        await _render_error(callback, _user('en'), error)

    caption = render.await_args.kwargs['caption']
    assert 'payment provider' in caption
    assert 'Top up your balance' in caption
    assert 'contact support' in caption


# ---------------------------------------------------------------------------
# Мина AT (пункт 4.5). Бот перерисовывал ЗАКРЫТЫЙ заказ как живой экран оплаты.
#
# Инлайн-кнопка живёт в переписке Telegram вечно, а `get_owned_checkout` терминальные
# строки отдаёт спокойно. Ветка «баланса не хватает» звала экран способов оплаты вообще
# без вопроса о состоянии заказа. Деньги при этом не двигались (все переходы под
# `FOR UPDATE` отвергают не-`confirmed`), но человек видел «💳 Выберите способ оплаты»
# у заказа, который сам же отменил минуту назад.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'state',
    ['cancelled', 'expired', 'operator_review', 'conflict', 'failed', 'reprice_required'],
)
async def test_arm_never_offers_payment_for_an_order_that_is_already_closed(state) -> None:
    """Состояния литералами: сторож, читающий ту же константу, что и код, пуст."""
    callback = SimpleNamespace(data='df:a:owned-checkout', answer=AsyncMock())
    # Баланса заведомо не хватает — то есть код пошёл бы ровно в дырявую ветку.
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45000,
        tariff_total_kopeks=45000,
        settlement_mode='direct_purchase_v2',
        lifecycle_state=state,
    )

    with (
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first._render_direct_payment_methods',
            AsyncMock(),
        ) as methods,
        patch(
            'app.handlers.subscription.device_first.commit_direct_wallet_checkout',
            AsyncMock(),
        ) as commit,
        patch(
            'app.handlers.subscription.device_first.arm_checkout',
            AsyncMock(),
        ) as arm_it,
        patch(
            'app.handlers.subscription.device_first._render_checkout',
            AsyncMock(),
        ) as render,
    ):
        await arm(callback, user, AsyncMock(), AsyncMock())

    methods.assert_not_awaited()
    commit.assert_not_awaited()
    arm_it.assert_not_awaited()
    # Человек видит настоящее состояние своего заказа — тем же экраном, что и везде.
    render.assert_awaited_once()

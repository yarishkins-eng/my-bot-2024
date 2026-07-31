from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.handlers.subscription.device_first import (
    _answer_stale,
    _days_label,
    _device_label,
    _device_page,
    _period_page,
    _render_arm_confirmation,
    _render_checkout,
    _render_confirmation,
    _render_error,
    arm,
    cancel,
    change_selection,
    choose_devices,
    choose_period,
    confirm,
    pay,
    show_device_first_entry,
)
from app.services.device_first_checkout_service import DeviceFirstError


def _user(language: str = 'ru'):
    return SimpleNamespace(language=language)


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
    assert [len(row) for row in keyboard] == [2, 2, 2, 2, 2, 1]
    assert keyboard[0][0].callback_data == 'df:d:view1234:1'
    assert keyboard[4][1].callback_data == 'df:d:view1234:10'
    assert keyboard[0][0].text == '1 device'
    assert keyboard[0][1].text == '2 devices'
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
async def test_choose_devices_creates_owned_telegram_checkout_and_renders_confirmation() -> None:
    callback = SimpleNamespace(data='df:d:view1234:5', answer=AsyncMock())
    state = AsyncMock()
    state.get_data.return_value = {
        'df_view_id': 'view1234',
        'df_days': 90,
        'df_options': {
            'tariff': {'name': 'Premium'},
            'period_options': [30, 90],
            'device_options': [2, 5],
        },
    }
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=50000)
    checkout = SimpleNamespace(public_id='owned-checkout')

    with (
        patch(
            'app.handlers.subscription.device_first.create_checkout',
            AsyncMock(return_value=checkout),
        ) as create,
        patch(
            'app.handlers.subscription.device_first._render_confirmation',
            AsyncMock(),
        ) as render,
    ):
        await choose_devices(callback, user, AsyncMock(), state)

    create.assert_awaited_once()
    assert create.await_args.kwargs == {
        'user': user,
        'period_days': 90,
        'selected_device_limit': 5,
        'source': 'telegram',
    }
    state.update_data.assert_awaited_once_with(df_checkout_id='owned-checkout')
    render.assert_awaited_once_with(callback, user, checkout, tariff_name='Premium')


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
async def test_arm_renders_exact_server_shortage_and_configuration() -> None:
    callback = SimpleNamespace(data='df:a:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=10000)
    checkout = SimpleNamespace(
        public_id='owned-checkout',
        selected_device_limit=5,
        period_days=90,
        quoted_price_kopeks=45000,
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
    assert keyboard[2][0].callback_data == 'df:x:owned-checkout'
    methods.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_invoice_without_a_payment_url_offers_status_support_and_cancel() -> None:
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
    assert 'df:x:owned-checkout' in callbacks


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
async def test_change_selection_does_not_cancel_an_order_with_a_pending_invoice() -> None:
    callback = SimpleNamespace(data='df:e:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(id=101, public_id='owned-checkout')

    with (
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.get_pending_platega_attempt',
            AsyncMock(return_value=SimpleNamespace(status='pending')),
        ),
        patch(
            'app.handlers.subscription.device_first.cancel_checkout',
            AsyncMock(),
        ) as cancel_checkout,
        patch(
            'app.handlers.subscription.device_first._render_checkout',
            AsyncMock(),
        ) as render_checkout,
    ):
        await change_selection(callback, user, AsyncMock(), AsyncMock())

    cancel_checkout.assert_not_awaited()
    render_checkout.assert_awaited_once()


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
async def test_cancel_after_credit_rerenders_processing_instead_of_claiming_cancelled() -> None:
    callback = SimpleNamespace(data='df:x:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout')
    state = SimpleNamespace(clear=AsyncMock())
    db = AsyncMock()
    invalid_state = DeviceFirstError('invalid_state', 'A fulfilled checkout cannot be cancelled')

    with (
        patch(
            'app.handlers.subscription.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.handlers.subscription.device_first.cancel_checkout',
            AsyncMock(side_effect=invalid_state),
        ),
        patch(
            'app.handlers.subscription.device_first.get_pending_platega_attempt',
            AsyncMock(return_value=None),
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
        await cancel(callback, user, db, state)

    state.clear.assert_awaited_once()
    render_checkout.assert_awaited_once_with(callback, user, db, checkout)
    render_cancelled.assert_not_awaited()


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

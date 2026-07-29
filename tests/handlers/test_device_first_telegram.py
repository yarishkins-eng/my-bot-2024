from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.subscription.device_first import (
    _answer_stale,
    _device_page,
    _period_page,
    _render_checkout,
    _render_confirmation,
    arm,
    cancel,
    choose_devices,
    choose_period,
    confirm,
    pay,
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
async def test_device_page_uses_one_wide_choice_per_row_and_paginates() -> None:
    state = AsyncMock()
    callback = SimpleNamespace()
    options = {
        'tariff': {'name': 'Premium'},
        'period_options': [30],
        'device_options': list(range(1, 9)),
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
            page=0,
            days=30,
            origin_callback='funnel_tariffs',
        )

    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert all(len(row) == 1 for row in keyboard[:6])
    assert keyboard[0][0].callback_data == 'df:d:view1234:1'
    assert len(keyboard[6]) == 2
    assert keyboard[6][-1].callback_data == 'df:p:view1234:1'
    assert keyboard[-1][0].callback_data == 'df:start'


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
    assert '1 → 3' in caption
    assert '29.08.2026' in caption
    assert '1 500.00 ₽' in caption
    assert '1 090.00 ₽' in caption


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
    draft = SimpleNamespace(public_id='owned-checkout')
    confirmed = SimpleNamespace(
        public_id='owned-checkout',
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
    assert '450.00 ₽' in caption
    assert '350.00 ₽' in caption
    assert keyboard[-2][0].callback_data == 'df:s:owned-checkout'


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
async def test_processing_checkout_shows_paid_message_without_cancel_button() -> None:
    callback = SimpleNamespace(data='df:s:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru', balance_kopeks=0)
    checkout = SimpleNamespace(public_id='owned-checkout')

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
async def test_pay_binds_method_checkout_and_owner_then_renders_provider_url() -> None:
    callback = SimpleNamespace(data='df:y:sbp:owned-checkout', answer=AsyncMock())
    user = SimpleNamespace(id=17, language='ru')
    attempt = SimpleNamespace(redirect_url='https://pay.example/invoice')

    with (
        patch(
            'app.handlers.subscription.device_first.create_platega_attempt',
            AsyncMock(return_value=attempt),
        ) as create,
        patch(
            'app.handlers.subscription.device_first.edit_or_answer_photo',
            AsyncMock(),
        ) as render,
    ):
        await pay(callback, user, AsyncMock(), AsyncMock())

    create.assert_awaited_once()
    assert create.await_args.kwargs == {
        'checkout_public_id': 'owned-checkout',
        'user_id': 17,
        'method_key': 'sbp',
    }
    keyboard = render.await_args.kwargs['keyboard'].inline_keyboard
    assert keyboard[0][0].url == 'https://pay.example/invoice'
    assert keyboard[1][0].callback_data == 'df:s:owned-checkout'

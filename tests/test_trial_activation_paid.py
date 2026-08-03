from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.handlers.subscription.purchase import (
    _activate_trial_with_coordinator_from_telegram,
    _show_trial_checkout_resolution,
    activate_trial,
    handle_trial_payment_method,
)
from app.services.trial_activation_service import (
    TrialActivationResult,
    TrialCheckoutContext,
    TrialCheckoutSummary,
    TrialPaymentInsufficientFunds,
)


@pytest.fixture
def trial_callback_query():
    callback = AsyncMock(spec=CallbackQuery)
    callback.message = AsyncMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture
def trial_user():
    user = MagicMock(spec=User)
    user.subscription = None
    user.has_had_paid_subscription = False
    user.language = 'ru'
    return user


@pytest.fixture
def trial_db():
    return AsyncMock(spec=AsyncSession)


@pytest.mark.asyncio
async def test_activate_trial_paid_shows_payment_screen_with_trial_price(
    trial_callback_query,
    trial_user,
    trial_db,
):
    # Paid-trial entrypoint: when the activation charge is positive and the
    # balance cannot cover it, activation must require a normal balance top-up,
    # not create a separate external pending-trial payment flow.
    trial_price_kopeks = 15900
    balance_kopeks = 100

    trial_user.balance_kopeks = balance_kopeks
    trial_user.restriction_subscription = False
    trial_user.auth_type = 'telegram'
    trial_user.is_trial_already_used.return_value = False

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    with (
        # get_trial_activation_charge_amount is imported locally inside
        # activate_trial via `from app.services.trial_activation_service import
        # ...`, so the name resolves from the source module at call time ->
        # patch it there.
        patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=trial_price_kopeks,
        ),
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
            ),
        ),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch(
            'app.handlers.subscription.purchase.get_trial_checkout_context',
            new=AsyncMock(return_value=TrialCheckoutContext('ready')),
        ),
        patch(
            'app.handlers.subscription.purchase.get_insufficient_balance_keyboard',
            return_value=mock_keyboard,
        ) as topup_keyboard,
    ):
        await activate_trial(trial_callback_query, trial_user, trial_db)

    topup_keyboard.assert_called_once_with(
        trial_user.language,
        resume_callback='trial_activate',
        amount_kopeks=trial_price_kopeks - balance_kopeks,
    )

    trial_callback_query.message.edit_text.assert_called_once()
    _args, kwargs = trial_callback_query.message.edit_text.call_args
    body = trial_callback_query.message.edit_text.call_args[0][0]

    # The top-up screen must surface the exact trial price and balance.
    assert kwargs['reply_markup'] is mock_keyboard
    assert settings.format_price(trial_price_kopeks) in body
    assert settings.format_price(balance_kopeks) in body

    trial_callback_query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_activate_trial_coordinator_insufficient_funds_redirects_to_topup(
    trial_callback_query,
    trial_user,
    trial_db,
):
    # The Telegram fallback must not construct a subscription itself.  The
    # common coordinator rejects an unaffordable paid trial before it changes
    # checkout, balance or subscription state, then the UI points to top-up.
    error = TrialPaymentInsufficientFunds(required_amount=15900, balance_amount=100)

    mock_keyboard = InlineKeyboardMarkup(inline_keyboard=[])

    trial_user.restriction_subscription = False
    trial_user.auth_type = 'telegram'
    trial_user.is_trial_already_used.return_value = False
    trial_user.id = 42

    with (
        # Paid branch skipped so activation calls the shared coordinator.
        patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=0,
        ),
        patch(
            'app.handlers.subscription.purchase.get_texts',
            return_value=MagicMock(
                t=lambda key, default, **kwargs: default,
            ),
        ),
        patch('app.config.Settings.is_trial_disabled_for_user', return_value=False),
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch('app.config.Settings.is_devices_selection_enabled', return_value=True),
        patch(
            'app.handlers.subscription.purchase.get_trial_checkout_context',
            new=AsyncMock(return_value=TrialCheckoutContext('ready')),
        ),
        patch(
            'app.handlers.subscription.purchase.activate_trial_with_checkout_resolution',
            new=AsyncMock(side_effect=error),
        ),
        patch(
            'app.handlers.subscription.purchase.get_insufficient_balance_keyboard',
            return_value=mock_keyboard,
        ) as insufficient_keyboard,
    ):
        await activate_trial(trial_callback_query, trial_user, trial_db)

    # Top-up redirect must target the exact required amount, not the balance.
    insufficient_keyboard.assert_called_once_with(
        trial_user.language,
        amount_kopeks=error.required_amount,
    )
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_paid_balance_trial_emits_side_effects_after_committed_coordinator_result(
    trial_callback_query,
    trial_user,
    trial_db,
):
    """A paid Telegram trial must not crash after the common commit.

    This covers the real post-commit branch that loads ``Transaction`` before
    emitting its durable ledger side effects.  It used to raise ``NameError``
    after money and entitlement were already committed.
    """

    trial_user.id = 42
    subscription = MagicMock()
    subscription.id = 77
    transaction = MagicMock()
    trial_db.get = AsyncMock(return_value=transaction)
    result = TrialActivationResult(
        subscription=subscription,
        charged_amount_kopeks=15_900,
        abandoned_checkout=None,
        charge_transaction_id=123,
    )

    texts = MagicMock()
    texts.TRIAL_ACTIVATED = 'Активировано'
    service = MagicMock()
    service.is_configured = False

    with (
        patch(
            'app.handlers.subscription.purchase.activate_trial_with_checkout_resolution',
            new=AsyncMock(return_value=result),
        ),
        patch('app.handlers.subscription.purchase.get_texts', return_value=texts),
        patch('app.handlers.subscription.purchase.SubscriptionService', return_value=service),
        patch('app.handlers.subscription.purchase.get_display_subscription_link', return_value=None),
        patch('app.handlers.subscription.purchase.emit_transaction_side_effects', new=AsyncMock()) as emit,
        patch(
            'app.handlers.subscription.purchase.AdminNotificationService.send_trial_activation_notification',
            new=AsyncMock(),
        ),
        patch('app.utils.funnel_notify.notify_trial_menu', new=AsyncMock()),
    ):
        await _activate_trial_with_coordinator_from_telegram(trial_callback_query, trial_user, trial_db)

    trial_db.get.assert_awaited_once()
    emit.assert_awaited_once()
    assert emit.await_args.args[1] is transaction
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_stale_external_trial_button_never_creates_pending_trial_subscription(
    trial_callback_query,
    trial_user,
    trial_db,
):
    """Old rendered external buttons must become a safe balance-topup route.

    Creating a new ``PENDING`` trial here would reintroduce the competing
    paid-trial / Device-First ownership race.
    """

    trial_user.id = 42
    trial_user.balance_kopeks = 0
    trial_user.is_trial_already_used.return_value = False
    trial_callback_query.data = 'trial_payment_platega'
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    texts = MagicMock()
    texts.t.side_effect = lambda _key, default, **_kwargs: default

    with (
        patch('app.handlers.subscription.purchase.get_texts', return_value=texts),
        patch(
            'app.handlers.subscription.purchase.get_trial_checkout_context',
            new=AsyncMock(return_value=TrialCheckoutContext('ready')),
        ),
        patch(
            'app.services.trial_activation_service.get_trial_activation_charge_amount',
            return_value=15_900,
        ),
        patch('app.handlers.subscription.purchase.get_insufficient_balance_keyboard', return_value=keyboard),
        patch(
            'app.handlers.subscription.purchase.create_pending_trial_subscription',
            new=AsyncMock(),
        ) as create_pending,
    ):
        await handle_trial_payment_method(trial_callback_query, trial_user, trial_db)

    create_pending.assert_not_awaited()
    trial_callback_query.message.edit_text.assert_called_once()
    trial_callback_query.answer.assert_called_once()


@pytest.mark.asyncio
async def test_trial_fallback_without_cabinet_url_never_builds_invalid_webapp_button(
    trial_callback_query,
    trial_user,
    trial_db,
):
    """A missing Mini App URL must degrade to support, not BUTTON_URL_INVALID."""

    context = TrialCheckoutContext(
        'pending_invoice',
        TrialCheckoutSummary(
            public_id='checkout-9',
            tariff_name='Базовый',
            period_days=30,
            device_limit=2,
            amount_kopeks=24_900,
        ),
    )
    texts = MagicMock()
    texts.BACK = 'Назад'

    with (
        patch('app.handlers.subscription.purchase.get_texts', return_value=texts),
        patch(
            'app.handlers.subscription.purchase.get_trial_checkout_context',
            new=AsyncMock(return_value=context),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value=''),
    ):
        shown = await _show_trial_checkout_resolution(trial_callback_query, trial_user, trial_db)

    assert shown is True
    keyboard = trial_callback_query.message.edit_text.call_args.kwargs['reply_markup']
    assert all(button.web_app is None for row in keyboard.inline_keyboard for button in row)

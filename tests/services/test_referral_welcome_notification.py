from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.utils.miniapp_buttons import build_miniapp_or_callback_button


def _settings(
    *,
    enabled: bool = True,
    minimum: int = 25_100,
    bonus: int = 7_300,
    first_payment_percent: int | None = None,
    recurring_tiers: str = '',
    max_payments: int = 0,
    inviter_bonus: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        REFERRAL_MINIMUM_TOPUP_KOPEKS=minimum,
        REFERRAL_FIRST_TOPUP_BONUS_KOPEKS=bonus,
        REFERRAL_INVITER_BONUS_KOPEKS=inviter_bonus,
        REFERRAL_COMMISSION_PERCENT=25,
        REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT=first_payment_percent,
        REFERRAL_RECURRING_COMMISSION_TIERS=recurring_tiers,
        REFERRAL_MAX_COMMISSION_PAYMENTS=max_payments,
        format_price=lambda kopeks: f'{kopeks / 100:g} ₽',
        is_referral_program_enabled=lambda: enabled,
    )


async def _run_registration(
    *,
    live_settings: SimpleNamespace,
    commission_values: list[int],
    new_user_telegram_id: int | None = 1010,
):
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()
    empty_row = AsyncMock()
    empty_row.scalar_one_or_none = lambda: None
    db.execute.return_value = empty_row

    new_user = SimpleNamespace(
        id=10,
        telegram_id=new_user_telegram_id,
        referred_by_id=20,
        language='ru',
        full_name='New User',
    )
    referrer = SimpleNamespace(
        id=20,
        telegram_id=2020,
        language='ru',
        full_name='Secret Referrer Name',
    )
    button = InlineKeyboardButton(text='💰 Открыть «Заработок»', callback_data='menu_referrals')

    with (
        patch('app.services.referral_service.settings', live_settings),
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.get_user_campaign_id', AsyncMock(return_value=None)),
        patch('app.services.referral_service.create_referral_earning', AsyncMock()),
        patch(
            'app.services.referral_contest_service.referral_contest_service.on_referral_registration',
            AsyncMock(),
        ),
        patch(
            'app.services.referral_service.get_effective_referral_commission_percent',
            side_effect=commission_values,
        ),
        patch('app.services.referral_service.build_miniapp_or_callback_button', return_value=button) as build_button,
        patch('app.services.referral_service.send_referral_notification', AsyncMock()) as send_notification,
    ):
        result = await process_referral_registration(db, new_user_id=10, referrer_id=20, bot=AsyncMock())

    return result, send_notification, build_button, button


@pytest.mark.asyncio
async def test_welcome_uses_live_reward_values_and_new_users_own_percent() -> None:
    result, send_notification, build_button, button = await _run_registration(
        live_settings=_settings(),
        commission_values=[82, 37],
    )

    assert result is True
    welcome_call = send_notification.await_args_list[0]
    assert welcome_call.args[2] == (
        '🎁 <b>Вы пришли по ссылке друга.</b>\n\n'
        'После первой оплаты от <b>251 ₽</b> мы начислим вам <b>73 ₽</b> на баланс.\n\n'
        'Приглашайте своих друзей и получайте <b>37%</b> с каждой их оплаты.\n\n'
        '<i>Условия программы применяются на момент оплаты.</i>'
    )
    assert '82%' not in welcome_call.args[2]
    assert 'Secret Referrer Name' not in welcome_call.args[2]

    markup = welcome_call.kwargs['reply_markup']
    assert markup.inline_keyboard == [[button]]
    build_button.assert_called_once_with(
        text='💰 Открыть «Заработок»',
        callback_data='menu_referrals',
        cabinet_path='/referral',
    )


@pytest.mark.asyncio
async def test_disabled_program_does_not_promise_rewards_or_show_earnings_button() -> None:
    result, send_notification, build_button, _ = await _run_registration(
        live_settings=_settings(enabled=False),
        commission_values=[25, 25],
    )

    assert result is True
    welcome_call = send_notification.await_args_list[0]
    assert welcome_call.args[2] == '🎁 <b>Вы пришли по ссылке друга.</b>'
    assert welcome_call.kwargs['reply_markup'] is None
    build_button.assert_not_called()


@pytest.mark.asyncio
async def test_zero_rewards_are_omitted_instead_of_promising_zero() -> None:
    _, send_notification, build_button, _ = await _run_registration(
        live_settings=_settings(bonus=0),
        commission_values=[25, 0],
    )

    welcome_call = send_notification.await_args_list[0]
    assert welcome_call.args[2] == '🎁 <b>Вы пришли по ссылке друга.</b>'
    assert welcome_call.kwargs['reply_markup'] is None
    build_button.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'live_settings',
    [
        _settings(first_payment_percent=12),
        _settings(recurring_tiers='0:10,10:20'),
        _settings(max_payments=3),
    ],
)
async def test_variable_or_limited_commission_uses_truthful_non_numeric_copy(
    live_settings: SimpleNamespace,
) -> None:
    _, send_notification, _, _ = await _run_registration(
        live_settings=live_settings,
        commission_values=[25, 37],
    )

    welcome_text = send_notification.await_args_list[0].args[2]
    assert '37%' not in welcome_text
    assert 'по действующим условиям реферальной программы' in welcome_text


@pytest.mark.asyncio
async def test_unreachable_base_percent_does_not_promise_earnings_or_show_cta() -> None:
    _, send_notification, build_button, _ = await _run_registration(
        live_settings=_settings(first_payment_percent=0, recurring_tiers='0:0'),
        commission_values=[25, 37],
    )

    welcome_call = send_notification.await_args_list[0]
    assert 'получайте' not in welcome_call.args[2]
    assert welcome_call.kwargs['reply_markup'] is None
    build_button.assert_not_called()


@pytest.mark.asyncio
async def test_fixed_inviter_bonus_keeps_generic_earnings_cta_when_commission_is_zero() -> None:
    _, send_notification, build_button, _ = await _run_registration(
        live_settings=_settings(
            bonus=0,
            first_payment_percent=0,
            recurring_tiers='0:0',
            inviter_bonus=5_000,
        ),
        commission_values=[25, 37],
    )

    welcome_call = send_notification.await_args_list[0]
    assert 'по действующим условиям реферальной программы' in welcome_call.args[2]
    assert welcome_call.kwargs['reply_markup'] is not None
    build_button.assert_called_once()


@pytest.mark.asyncio
async def test_email_only_registration_does_not_send_fake_zero_bonus_email() -> None:
    result, send_notification, build_button, _ = await _run_registration(
        live_settings=_settings(),
        commission_values=[25],
        new_user_telegram_id=None,
    )

    assert result is True
    assert send_notification.await_count == 1
    assert send_notification.await_args.args[1] == 2020
    build_button.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_delivery_forwards_optional_keyboard() -> None:
    from app.services.referral_service import send_referral_notification

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Заработок', callback_data='menu_referrals')]]
    )

    await send_referral_notification(bot, 1010, 'Текст', reply_markup=markup)

    bot.send_message.assert_awaited_once_with(1010, 'Текст', parse_mode='HTML', reply_markup=markup)


@pytest.mark.asyncio
async def test_telegram_delivery_keeps_old_call_shape_without_keyboard() -> None:
    from app.services.referral_service import send_referral_notification

    bot = AsyncMock()

    await send_referral_notification(bot, 1010, 'Текст')

    bot.send_message.assert_awaited_once_with(1010, 'Текст', parse_mode='HTML')


def test_earnings_button_opens_exact_cabinet_referral_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com/', raising=False)

    button = build_miniapp_or_callback_button(
        text='💰 Открыть «Заработок»',
        callback_data='menu_referrals',
        cabinet_path='/referral',
    )

    assert button.web_app is not None
    assert button.web_app.url == 'https://cabinet.example.com/referral'
    assert button.callback_data is None


def test_earnings_button_falls_back_to_existing_bot_referral_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet', raising=False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', '', raising=False)

    button = build_miniapp_or_callback_button(
        text='💰 Открыть «Заработок»',
        callback_data='menu_referrals',
        cabinet_path='/referral',
    )

    assert button.web_app is None
    assert button.callback_data == 'menu_referrals'

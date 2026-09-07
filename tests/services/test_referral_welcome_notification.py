import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.referral_service import REFERRAL_WELCOME_NEXT_CALLBACK


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
    referrer_first_name: str | None = None,
    referrer_username: str | None = None,
    break_welcome: bool = False,
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
        first_name=referrer_first_name,
        last_name=None,
        username=referrer_username,
    )

    texts_patch = (
        patch('app.services.referral_service.get_texts', side_effect=KeyError('REFERRAL_WELCOME_BONUS'))
        if break_welcome
        else contextlib.nullcontext()
    )

    with (
        patch('app.services.referral_service.settings', live_settings),
        texts_patch,
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
        patch(
            'app.services.referral_service.send_referral_notification',
            AsyncMock(return_value=True),
        ) as send_notification,
    ):
        result = await process_referral_registration(
            db, new_user_id=10, referrer_id=20, bot=AsyncMock(), report_welcome_delivery=True
        )

    return result, send_notification


def _welcome(send_notification):
    return send_notification.await_args_list[0]


def _inviter(send_notification):
    return send_notification.await_args_list[1]


@pytest.mark.asyncio
async def test_welcome_uses_live_reward_values_and_new_users_own_percent() -> None:
    result, send_notification = await _run_registration(
        live_settings=_settings(),
        commission_values=[82, 37],
    )

    assert result is True
    welcome_call = _welcome(send_notification)
    assert welcome_call.args[2] == (
        '🎁 <b>Вы пришли по ссылке друга.</b>\n\n'
        'После первой оплаты от <b>251 ₽</b> мы начислим вам <b>73 ₽</b> на баланс.\n\n'
        'Приглашайте своих друзей и получайте <b>37%</b> с каждой их оплаты.\n\n'
        '<i>Условия программы применяются на момент оплаты.</i>'
    )
    # Процент берётся у НОВИЧКА (37), а не у пригласившего (82): обещание про его будущих друзей.
    assert '82%' not in welcome_call.args[2]


@pytest.mark.asyncio
async def test_welcome_names_the_referrer_when_the_name_is_known() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(),
        commission_values=[25, 25],
        referrer_first_name='Сергей',
    )

    assert _welcome(send_notification).args[2].startswith('🎁 <b>Вы пришли по ссылке друга: Сергей</b>')


@pytest.mark.asyncio
async def test_welcome_falls_back_to_username_and_then_to_generic_wording() -> None:
    _, by_username = await _run_registration(
        live_settings=_settings(),
        commission_values=[25, 25],
        referrer_username='seryoga',
    )
    assert _welcome(by_username).args[2].startswith('🎁 <b>Вы пришли по ссылке друга: @seryoga</b>')

    _, nameless = await _run_registration(live_settings=_settings(), commission_values=[25, 25])
    assert _welcome(nameless).args[2].startswith('🎁 <b>Вы пришли по ссылке друга.</b>')


@pytest.mark.asyncio
async def test_referrer_name_cannot_break_the_message_markup() -> None:
    """Имя приходит из Telegram и не проверяется никем: экранирование обязательно.

    Без него имя со скобкой роняет отправку письма целиком — а вместе с ним, до правки
    06.09.2026, молча и навсегда терялось письмо ПРИГЛАСИВШЕМУ.
    """
    _, send_notification = await _run_registration(
        live_settings=_settings(),
        commission_values=[25, 25],
        referrer_first_name='<b>злой</b>',
    )

    welcome_text = _welcome(send_notification).args[2]
    assert '&lt;b&gt;злой&lt;/b&gt;' in welcome_text
    assert '<b>злой</b>' not in welcome_text


@pytest.mark.asyncio
async def test_next_button_appears_when_the_next_step_is_deferred_and_the_letter_has_substance() -> None:
    _, send_notification = await _run_registration(live_settings=_settings(), commission_values=[25, 25])

    markup = _welcome(send_notification).kwargs['reply_markup']
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [REFERRAL_WELCOME_NEXT_CALLBACK]
    assert markup.inline_keyboard[0][0].text == 'Дальше →'


@pytest.mark.asyncio
async def test_no_button_when_the_letter_says_nothing_but_that_a_friend_invited_you() -> None:
    """🔴 За одной строкой нельзя прятать единственный вход в пробный период.

    При выключенной программе и при нулевых наградах письмо схлопывается до факта перехода —
    в этом случае следующий шаг обязан прийти сам, как раньше.
    """
    for live_settings in (_settings(enabled=False), _settings(bonus=0)):
        result, send_notification = await _run_registration(
            live_settings=live_settings,
            commission_values=[25, 0],
        )

        assert _welcome(send_notification).kwargs['reply_markup'] is None
        assert result is False, 'без кнопки шаг не отложен — бот обязан показать его сам'


@pytest.mark.asyncio
async def test_disabled_program_promises_nothing_to_the_invited_user() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(enabled=False),
        commission_values=[25, 25],
    )

    assert _welcome(send_notification).args[2] == '🎁 <b>Вы пришли по ссылке друга.</b>'


@pytest.mark.asyncio
async def test_disabled_program_promises_no_money_to_the_inviter_either() -> None:
    """🔴 До 06.09.2026 выключатель гасил обещания только приглашённому.

    Пригласившему уходило «вы получите 100 ₽ + 25 %», которых `process_referral_topup`
    в этом режиме не платит: он выходит до начисления.
    """
    _, send_notification = await _run_registration(
        live_settings=_settings(enabled=False, inviter_bonus=10_000),
        commission_values=[25, 25],
    )

    inviter_text = _inviter(send_notification).args[2]
    assert inviter_text == (
        '👥 <b>Новый реферал!</b>\n\nПо вашей ссылке зарегистрировался пользователь <b>New User</b>!'
    )
    assert '₽' not in inviter_text
    assert '%' not in inviter_text


@pytest.mark.asyncio
async def test_enabled_program_still_tells_the_inviter_what_he_gets() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(inviter_bonus=10_000),
        commission_values=[25, 25],
    )

    inviter_text = _inviter(send_notification).args[2]
    assert 'Когда он оплатит от 251 ₽' in inviter_text
    assert 'вы получите 100 ₽ + 25% от суммы оплаты.' in inviter_text
    assert 'С каждой следующей его оплаты вы будете получать 25%.' in inviter_text


@pytest.mark.asyncio
async def test_broken_welcome_text_does_not_swallow_the_inviter_notification() -> None:
    """Письмо пригласившему стоит ПОСЛЕ приветствия в одном общем `try`.

    Без локальной страховки любая ошибка приветствия отменяла его молча и навсегда:
    запись о регистрации уже закоммичена, и повторный вызов выходит на первом SELECT.
    """
    result, send_notification = await _run_registration(
        live_settings=_settings(),
        commission_values=[25],
        break_welcome=True,
    )

    # 🔴 И возвращаем False: приветствия не было, значит следующий шаг онбординга обязан
    # прийти сам. Соврать True здесь — оставить человека вообще без сообщений.
    assert result is False
    assert send_notification.await_count == 1
    assert send_notification.await_args.args[1] == 2020
    assert 'Новый реферал!' in send_notification.await_args.args[2]


@pytest.mark.asyncio
async def test_zero_rewards_are_omitted_instead_of_promising_zero() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(bonus=0),
        commission_values=[25, 0],
    )

    assert _welcome(send_notification).args[2] == '🎁 <b>Вы пришли по ссылке друга.</b>'


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
    _, send_notification = await _run_registration(
        live_settings=live_settings,
        commission_values=[25, 37],
    )

    welcome_text = _welcome(send_notification).args[2]
    assert '37%' not in welcome_text
    assert 'по действующим условиям реферальной программы' in welcome_text


@pytest.mark.asyncio
async def test_unreachable_base_percent_does_not_promise_earnings() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(first_payment_percent=0, recurring_tiers='0:0'),
        commission_values=[25, 37],
    )

    welcome_text = _welcome(send_notification).args[2]
    assert 'Приглашайте своих друзей' not in welcome_text
    assert 'по действующим условиям реферальной программы' not in welcome_text


@pytest.mark.asyncio
async def test_fixed_inviter_bonus_keeps_generic_copy_when_commission_is_zero() -> None:
    _, send_notification = await _run_registration(
        live_settings=_settings(
            bonus=0,
            first_payment_percent=0,
            recurring_tiers='0:0',
            inviter_bonus=5_000,
        ),
        commission_values=[25, 37],
    )

    assert 'по действующим условиям реферальной программы' in _welcome(send_notification).args[2]


@pytest.mark.asyncio
async def test_email_only_registration_does_not_send_fake_zero_bonus_email() -> None:
    result, send_notification = await _run_registration(
        live_settings=_settings(),
        commission_values=[25],
        new_user_telegram_id=None,
    )

    # False: приветствия в телеграм не было (его и некуда слать) — в боте такого пути нет,
    # а кабинет это значение не читает.
    assert result is False
    assert send_notification.await_count == 1
    assert send_notification.await_args.args[1] == 2020


@pytest.mark.asyncio
async def test_telegram_delivery_forwards_optional_keyboard() -> None:
    from app.services.referral_service import send_referral_notification

    bot = AsyncMock()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='Дальше →', callback_data=REFERRAL_WELCOME_NEXT_CALLBACK)]]
    )

    delivered = await send_referral_notification(bot, 1010, 'Текст', reply_markup=markup)

    assert delivered is True
    bot.send_message.assert_awaited_once_with(1010, 'Текст', parse_mode='HTML', reply_markup=markup)


@pytest.mark.asyncio
async def test_telegram_delivery_without_keyboard_sends_no_keyboard() -> None:
    """Проверяем ПОВЕДЕНИЕ, а не форму вызова: aiogram сам выбрасывает пустую клавиатуру."""
    from app.services.referral_service import send_referral_notification

    bot = AsyncMock()

    await send_referral_notification(bot, 1010, 'Текст')

    assert bot.send_message.await_args.args == (1010, 'Текст')
    assert bot.send_message.await_args.kwargs['parse_mode'] == 'HTML'
    assert bot.send_message.await_args.kwargs['reply_markup'] is None


@pytest.mark.asyncio
async def test_repeated_registration_sends_nothing_and_reports_no_welcome() -> None:
    """Повтор той же пары: приветствие уже приходило, слать второе нельзя.

    Возврат False обязателен — по нему бот покажет следующий шаг сам, иначе человек
    останется без меню (кнопки «Дальше» у него в этот раз не появилось).
    """
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()
    existing_row = AsyncMock()
    existing_row.scalar_one_or_none = lambda: 777
    db.execute.return_value = existing_row

    new_user = SimpleNamespace(id=10, telegram_id=1010, referred_by_id=20, language='ru', full_name='New User')
    referrer = SimpleNamespace(
        id=20, telegram_id=2020, language='ru', full_name='R', first_name='Сергей', last_name=None, username=None
    )

    with (
        patch('app.services.referral_service.settings', _settings()),
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.send_referral_notification', AsyncMock()) as send_notification,
    ):
        result = await process_referral_registration(
            db, new_user_id=10, referrer_id=20, bot=AsyncMock(), report_welcome_delivery=True
        )

    assert result is False
    send_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_undelivered_welcome_reports_false_so_the_bot_shows_the_next_step_itself() -> None:
    """Telegram отказал в доставке приветствия — признак обязан стать False.

    Именно по нему `app/handlers/start.py` гасит автоматическую отправку следующего шага.
    """
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()
    empty_row = AsyncMock()
    empty_row.scalar_one_or_none = lambda: None
    db.execute.return_value = empty_row

    new_user = SimpleNamespace(id=10, telegram_id=1010, referred_by_id=20, language='ru', full_name='New User')
    referrer = SimpleNamespace(
        id=20, telegram_id=2020, language='ru', full_name='R', first_name='Сергей', last_name=None, username=None
    )

    bot = AsyncMock()

    async def refuse_invited_only(chat_id, *args, **kwargs):
        if chat_id == 1010:
            raise RuntimeError('Telegram refused')
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = refuse_invited_only

    with (
        patch('app.services.referral_service.settings', _settings()),
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.get_user_campaign_id', AsyncMock(return_value=None)),
        patch('app.services.referral_service.create_referral_earning', AsyncMock()),
        patch(
            'app.services.referral_contest_service.referral_contest_service.on_referral_registration',
            AsyncMock(),
        ),
        patch(
            'app.services.referral_service.get_effective_referral_commission_percent',
            side_effect=[25, 25],
        ),
    ):
        result = await process_referral_registration(
            db, new_user_id=10, referrer_id=20, bot=bot, report_welcome_delivery=True
        )

    assert result is False
    # Пригласивший при этом письмо получил: одна осечка не отменяет вторую отправку.
    assert [call.args[0] for call in bot.send_message.await_args_list] == [1010, 2020]


@pytest.mark.asyncio
async def test_callers_that_do_not_defer_the_next_step_get_no_button() -> None:
    """🔴 Кнопку гасят только три вызывающих из восьми.

    Кабинет и ретроактивная привязка на `/start` показывают меню сами — кнопка дала бы
    человеку его копию поверх уже пришедшего.
    """
    from app.services.referral_service import process_referral_registration

    db = AsyncMock()
    empty_row = AsyncMock()
    empty_row.scalar_one_or_none = lambda: None
    db.execute.return_value = empty_row

    new_user = SimpleNamespace(id=10, telegram_id=1010, referred_by_id=20, language='ru', full_name='New User')
    referrer = SimpleNamespace(
        id=20, telegram_id=2020, language='ru', full_name='R', first_name='Сергей', last_name=None, username=None
    )

    with (
        patch('app.services.referral_service.settings', _settings()),
        patch('app.services.referral_service.get_user_by_id', AsyncMock(side_effect=[new_user, referrer])),
        patch('app.services.referral_service.get_user_campaign_id', AsyncMock(return_value=None)),
        patch('app.services.referral_service.create_referral_earning', AsyncMock()),
        patch(
            'app.services.referral_contest_service.referral_contest_service.on_referral_registration',
            AsyncMock(),
        ),
        patch(
            'app.services.referral_service.get_effective_referral_commission_percent',
            side_effect=[25, 25],
        ),
        patch(
            'app.services.referral_service.send_referral_notification',
            AsyncMock(return_value=True),
        ) as send_notification,
    ):
        # без report_welcome_delivery — прежний контракт вызывающего
        result = await process_referral_registration(db, new_user_id=10, referrer_id=20, bot=AsyncMock())

    assert result is True, 'прежний смысл возврата — «регистрация обработана»'
    assert _welcome(send_notification).kwargs['reply_markup'] is None

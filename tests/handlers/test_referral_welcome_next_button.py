"""Кнопка «Дальше» под реферальным приветствием — вторая половина правки 07.09.2026.

Проверяется то, от чего зависит, увидит ли человек третье сообщение онбординга вообще:
порядок «сначала отправить, потом снять кнопку» и обе развилки показываемого экрана.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.handlers import start as start_handlers


def _user(language: str = 'ru') -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        telegram_id=1010,
        language=language,
        balance_kopeks=0,
        has_had_paid_subscription=False,
        subscription=None,
    )


def _callback(bot: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot,
        from_user=SimpleNamespace(id=1010, first_name='Новичок', username='newbie', is_bot=False),
        message=AsyncMock(),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_button_is_stripped_only_after_the_next_step_was_sent() -> None:
    """🔴 Обратный порядок оставлял человека без кнопки И без следующего шага.

    Выход из такого тупика был только один — повторный /start, о котором ему никто не скажет.
    """
    order: list[str] = []
    callback = _callback(AsyncMock())
    callback.message.edit_reply_markup = AsyncMock(side_effect=lambda **_: order.append('strip'))

    async def send(*_args, **_kwargs):
        order.append('send')

    with (
        patch.object(start_handlers, 'get_user_by_telegram_id', AsyncMock(return_value=_user())),
        patch.object(start_handlers, 'was_referral_onboarding_shown', AsyncMock(return_value=False)),
        patch.object(start_handlers, 'mark_referral_onboarding_shown', AsyncMock()),
        patch.object(start_handlers, 'send_onboarding_menu', AsyncMock(side_effect=send)),
    ):
        await start_handlers.handle_referral_welcome_next(callback, AsyncMock())

    assert order == ['send', 'strip']


@pytest.mark.asyncio
async def test_failed_send_keeps_the_button_and_says_so() -> None:
    callback = _callback(AsyncMock())
    callback.message.edit_reply_markup = AsyncMock()

    with (
        patch.object(start_handlers, 'get_user_by_telegram_id', AsyncMock(return_value=_user())),
        patch.object(start_handlers, 'was_referral_onboarding_shown', AsyncMock(return_value=False)),
        patch.object(start_handlers, 'mark_referral_onboarding_shown', AsyncMock()),
        patch.object(
            start_handlers,
            'send_onboarding_menu',
            AsyncMock(side_effect=RuntimeError('Telegram refused')),
        ),
    ):
        await start_handlers.handle_referral_welcome_next(callback, AsyncMock())

    callback.message.edit_reply_markup.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_unknown_user_is_answered_without_sending_anything() -> None:
    callback = _callback(AsyncMock())

    with (
        patch.object(start_handlers, 'get_user_by_telegram_id', AsyncMock(return_value=None)),
        patch.object(start_handlers, 'send_onboarding_menu', AsyncMock()) as send_menu,
    ):
        await start_handlers.handle_referral_welcome_next(callback, AsyncMock())

    send_menu.assert_not_awaited()
    callback.answer.assert_awaited_once()


async def _run_onboarding_menu(offer_text: str | None):
    bot = AsyncMock()
    user = _user()
    tg_user = SimpleNamespace(id=1010, first_name='Новичок', username='newbie')

    with (
        patch('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value=offer_text)),
        patch.object(start_handlers, 'get_active_pinned_message', AsyncMock(return_value=None)),
        patch.object(start_handlers, '_calculate_subscription_flags', lambda _sub: (False, False)),
        patch.object(start_handlers, 'get_main_menu_text', AsyncMock(return_value='МЕНЮ')),
        patch.object(start_handlers, 'get_main_menu_keyboard_async', AsyncMock(return_value='KEYBOARD')),
        patch.object(start_handlers.MainMenuButtonService, 'get_buttons_for_user', AsyncMock(return_value=[])),
        patch.object(start_handlers.SupportSettingsService, 'is_moderator', lambda _tg: False),
        patch('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock()),
    ):
        await start_handlers.send_onboarding_menu(bot, tg_user.id, AsyncMock(), user)

    return bot


@pytest.mark.asyncio
async def test_next_step_shows_the_main_menu_when_no_welcome_text_is_configured() -> None:
    bot = await _run_onboarding_menu(None)

    assert bot.send_message.await_args.kwargs['text'] == 'МЕНЮ'
    assert bot.send_message.await_args.kwargs['reply_markup'] == 'KEYBOARD'


@pytest.mark.asyncio
async def test_next_step_shows_the_admin_welcome_text_when_it_is_configured() -> None:
    """🔴 Развилок ДВЕ. Пока показывали только меню, включение приветствия в админке молча

    отбирало у пришедших по ссылке бот-кнопку бесплатного триала — и только у них.
    """
    bot = await _run_onboarding_menu('Привет от админки!')

    assert bot.send_message.await_args.kwargs['text'] == 'Привет от админки!'
    keyboard = bot.send_message.await_args.kwargs['reply_markup']
    assert any(b.callback_data == 'trial_activate' for row in keyboard.inline_keyboard for b in row)


@pytest.mark.asyncio
async def test_broken_html_in_the_admin_welcome_text_does_not_dead_end_the_button() -> None:
    """🔴 Без повтора без разметки «Дальше» становится тупиком НАВСЕГДА.

    Отправка падала бы детерминированно: человек жмёт кнопку, получает «попробуйте ещё раз»,
    жмёт снова — и так до бесконечности. Повтор есть в обеих автоматических ветках.
    """
    bot = AsyncMock()
    calls: list[str | None] = []

    async def refuse_html(**kwargs):
        calls.append(kwargs.get('parse_mode'))
        if kwargs.get('parse_mode') == 'HTML':
            raise TelegramBadRequest(method='sendMessage', message="Bad Request: can't parse entities")
        return SimpleNamespace(message_id=1)

    bot.send_message.side_effect = refuse_html
    tg_user = SimpleNamespace(id=1010, first_name='Новичок', username='newbie')

    with (
        patch('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value='<b>битый')),
        patch.object(start_handlers, 'get_active_pinned_message', AsyncMock(return_value=None)),
        patch.object(start_handlers, '_calculate_subscription_flags', lambda _sub: (False, False)),
    ):
        await start_handlers.send_onboarding_menu(bot, tg_user.id, AsyncMock(), _user())

    assert calls == ['HTML', None], 'после отказа разметки текст обязан уйти без неё'


@pytest.mark.asyncio
async def test_subscriber_is_not_offered_the_free_trial_again() -> None:
    """Сторож из автоматической ветки: у кого подписка есть, тому пробный не предлагаем."""
    bot = AsyncMock()
    tg_user = SimpleNamespace(id=1010, first_name='Новичок', username='newbie')

    with (
        patch('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value='Привет!')),
        patch.object(start_handlers, 'get_active_pinned_message', AsyncMock(return_value=None)),
        patch.object(start_handlers, '_calculate_subscription_flags', lambda _sub: (True, True)),
    ):
        await start_handlers.send_onboarding_menu(bot, tg_user.id, AsyncMock(), _user())

    keyboard = bot.send_message.await_args.kwargs['reply_markup']
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'trial_activate' not in buttons


@pytest.mark.asyncio
async def test_second_tap_does_not_send_a_second_menu() -> None:
    """Кнопки в сообщении уже нет — шаг показан, копию не шлём."""
    callback = _callback(AsyncMock())
    callback.message.reply_markup = None

    with (
        patch.object(start_handlers, 'get_user_by_telegram_id', AsyncMock(return_value=_user())),
        patch.object(start_handlers, 'was_referral_onboarding_shown', AsyncMock(return_value=False)),
        patch.object(start_handlers, 'mark_referral_onboarding_shown', AsyncMock()),
        patch.object(start_handlers, 'send_onboarding_menu', AsyncMock()) as send_menu,
    ):
        await start_handlers.handle_referral_welcome_next(callback, AsyncMock())

    send_menu.assert_not_awaited()
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_press_after_the_timer_already_delivered_sends_no_copy() -> None:
    """Добор сработал, а кнопка осталась: нажатие обязано снять её и промолчать."""
    callback = _callback(AsyncMock())
    callback.message.edit_reply_markup = AsyncMock()

    with (
        patch.object(start_handlers, 'get_user_by_telegram_id', AsyncMock(return_value=_user())),
        patch.object(start_handlers, 'was_referral_onboarding_shown', AsyncMock(return_value=True)),
        patch.object(start_handlers, 'send_onboarding_menu', AsyncMock()) as send_menu,
    ):
        await start_handlers.handle_referral_welcome_next(callback, AsyncMock())

    send_menu.assert_not_awaited()
    callback.message.edit_reply_markup.assert_awaited_once()
    callback.answer.assert_awaited_once()

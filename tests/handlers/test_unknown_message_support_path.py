from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher, types

from app.handlers import common
from app.keyboards import inline
from app.services.support_settings_service import SupportSettingsService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('db_user', 'language', 'expected_text'),
    [
        (
            SimpleNamespace(language='ru'),
            'ru',
            '❓ Не понял сообщение. Если что-то не работает, выберите способ связи с поддержкой ниже.',
        ),
        (
            SimpleNamespace(language='en'),
            'en',
            "❓ I didn't understand the message. If something isn't working, choose how to contact support below.",
        ),
        (
            None,
            'ru',
            '❓ Не понял сообщение. Если что-то не работает, выберите способ связи с поддержкой ниже.',
        ),
    ],
)
async def test_unknown_message_offers_existing_support_paths(monkeypatch, db_user, language, expected_text) -> None:
    message = SimpleNamespace(answer=AsyncMock(), text='Не работает VPN')
    support_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Support', callback_data='create_ticket')]]
    )
    get_support_keyboard = Mock(return_value=support_markup)
    monkeypatch.setattr(common, 'get_support_keyboard', get_support_keyboard)
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: True)

    await common.handle_unknown_message(message, db_user=db_user)

    message.answer.assert_awaited_once_with(expected_text, reply_markup=support_markup)
    get_support_keyboard.assert_called_once_with(language)


@pytest.mark.asyncio
async def test_unknown_media_explains_that_attachment_is_not_forwarded(monkeypatch) -> None:
    message = SimpleNamespace(answer=AsyncMock(), text=None)
    support_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Support', callback_data='create_ticket')]]
    )
    monkeypatch.setattr(common, 'get_support_keyboard', Mock(return_value=support_markup))
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: True)

    await common.handle_unknown_message(message, db_user=SimpleNamespace(language='ru'))

    message.answer.assert_awaited_once_with(
        '📎 Получил вложение, но вне обращения оно не передаётся поддержке. '
        'Выберите способ связи ниже и опишите, что случилось.',
        reply_markup=support_markup,
    )


@pytest.mark.asyncio
async def test_unknown_message_respects_disabled_support(monkeypatch) -> None:
    message = SimpleNamespace(answer=AsyncMock())
    back_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Back', callback_data='back_to_menu')]]
    )
    get_back_keyboard = Mock(return_value=back_markup)
    get_support_keyboard = Mock()
    monkeypatch.setattr(common, 'get_back_keyboard', get_back_keyboard)
    monkeypatch.setattr(common, 'get_support_keyboard', get_support_keyboard)
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: False)

    await common.handle_unknown_message(message, db_user=SimpleNamespace(language='en'))

    message.answer.assert_awaited_once_with(
        "❓ I didn't understand the message. Support is temporarily unavailable. Return to the menu and try again later.",
        reply_markup=back_markup,
    )
    get_back_keyboard.assert_called_once_with('en')
    get_support_keyboard.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_message_does_not_promise_support_when_contact_mode_has_no_contact(monkeypatch) -> None:
    message = SimpleNamespace(answer=AsyncMock())
    back_only_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Back', callback_data='back_to_menu')]]
    )
    monkeypatch.setattr(common, 'get_support_keyboard', Mock(return_value=back_only_markup))
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: True)

    await common.handle_unknown_message(message, db_user=SimpleNamespace(language='ru'))

    message.answer.assert_awaited_once_with(
        '❓ Не понял сообщение. Поддержка сейчас временно недоступна. Вернитесь в меню и попробуйте позже.',
        reply_markup=back_only_markup,
    )


@pytest.mark.parametrize(
    ('tickets_enabled', 'contact_enabled', 'expected_callbacks', 'expected_urls'),
    [
        (True, False, {'create_ticket', 'my_tickets', 'back_to_menu'}, []),
        (False, True, {'back_to_menu'}, ['https://t.me/teplo_vpnhelp']),
        (True, True, {'create_ticket', 'my_tickets', 'back_to_menu'}, ['https://t.me/teplo_vpnhelp']),
    ],
)
def test_support_keyboard_matches_runtime_mode(
    monkeypatch, tickets_enabled, contact_enabled, expected_callbacks, expected_urls
) -> None:
    monkeypatch.setattr(SupportSettingsService, 'is_tickets_enabled', lambda: tickets_enabled)
    monkeypatch.setattr(SupportSettingsService, 'is_contact_enabled', lambda: contact_enabled)
    monkeypatch.setattr(type(inline.settings), 'get_support_contact_url', lambda self: 'https://t.me/teplo_vpnhelp')

    markup = inline.get_support_keyboard('ru')
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert {button.callback_data for button in buttons if button.callback_data} == expected_callbacks
    assert [button.url for button in buttons if button.url] == expected_urls


def _message_update(*, update_id: int, user_id: int, text: str | None) -> types.Update:
    user = types.User(id=user_id, is_bot=False, first_name='Support path tester')
    content: dict = {'text': text}
    if text is None:
        content = {
            'photo': [
                types.PhotoSize(
                    file_id=f'photo-{update_id}',
                    file_unique_id=f'unique-photo-{update_id}',
                    width=1,
                    height=1,
                )
            ]
        }
    message = types.Message(
        message_id=update_id,
        date=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        chat=types.Chat(id=user_id, type='private'),
        from_user=user,
        **content,
    )
    return types.Update(update_id=update_id, message=message)


@pytest.mark.asyncio
async def test_dispatcher_routes_unknown_text_and_media_only_without_active_state(monkeypatch) -> None:
    bot = Bot(token='123456:ABCDEF_test_token')
    dispatcher = Dispatcher()
    common.register_handlers(dispatcher)
    answer = AsyncMock()
    support_markup = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text='Support', callback_data='create_ticket')]]
    )
    monkeypatch.setattr(types.Message, 'answer', answer)
    monkeypatch.setattr(common, 'get_support_keyboard', Mock(return_value=support_markup))
    monkeypatch.setattr(SupportSettingsService, 'is_support_menu_enabled', lambda: True)
    db_user = SimpleNamespace(language='ru')
    user_id = 700_101

    await dispatcher.feed_update(
        bot,
        _message_update(update_id=1, user_id=user_id, text='Не работает VPN'),
        db_user=db_user,
    )
    await dispatcher.feed_update(
        bot,
        _message_update(update_id=2, user_id=user_id, text=None),
        db_user=db_user,
    )

    assert answer.await_count == 2
    assert answer.await_args_list[0].args[0].startswith('❓ Не понял сообщение.')
    assert answer.await_args_list[1].args[0].startswith('📎 Получил вложение,')
    assert all(call.kwargs['reply_markup'] is support_markup for call in answer.await_args_list)

    state = dispatcher.fsm.get_context(bot=bot, chat_id=user_id, user_id=user_id)
    await state.set_state('support:test-active')
    answer.reset_mock()

    await dispatcher.feed_update(
        bot,
        _message_update(update_id=3, user_id=user_id, text='Текст внутри активного сценария'),
        db_user=db_user,
    )

    answer.assert_not_awaited()
    await bot.session.close()

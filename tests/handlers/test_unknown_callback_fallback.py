from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods.base import TelegramMethod
from fastapi import HTTPException

from app.bot import _register_update_handlers
from app.cabinet.routes import admin_broadcasts as broadcast_routes
from app.cabinet.schemas.broadcasts import (
    BroadcastCreateRequest,
    CombinedBroadcastCreateRequest,
    CustomBroadcastButton,
)
from app.handlers import common
from app.handlers.admin.messages import create_broadcast_keyboard
from app.handlers.subscription import purchase as subscription_purchase
from app.utils import decorators


RU_UNKNOWN_CALLBACK = '❓ Эта кнопка больше недоступна. Откройте актуальное меню командой /start.'
EN_UNKNOWN_CALLBACK = '❓ This button is no longer available. Open the current menu with /start.'


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.methods: list[TelegramMethod] = []

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.methods.append(method)
        return True

    async def close(self) -> None:
        return None

    async def stream_content(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        if False:
            yield b''


def _callback_update(*, update_id: int, callback_id: str, data: str, user_id: int = 700_001) -> types.Update:
    user = types.User(id=user_id, is_bot=False, first_name='Callback tester')
    message = types.Message(
        message_id=update_id,
        date=datetime(2026, 8, 29, 1, 0, tzinfo=UTC),
        chat=types.Chat(id=user_id, type='private'),
        from_user=user,
        text='Broadcast',
    )
    callback = types.CallbackQuery(
        id=callback_id,
        from_user=user,
        chat_instance='test-chat-instance',
        message=message,
        data=data,
    )
    return types.Update(update_id=update_id, callback_query=callback)


def _answer_methods(session: RecordingSession) -> list[AnswerCallbackQuery]:
    return [method for method in session.methods if isinstance(method, AnswerCallbackQuery)]


@pytest.mark.asyncio
async def test_terminal_fallback_handles_unknown_callbacks_without_shadowing_known_handlers(monkeypatch) -> None:
    session = RecordingSession()
    bot = Bot(token='123456:ABCDEF_test_token', session=session)
    dispatcher = Dispatcher(storage=MemoryStorage())
    warning = Mock()
    monkeypatch.setattr(common.logger, 'warning', warning)

    async def menu_buy_marker(callback: types.CallbackQuery, **kwargs) -> None:
        await callback.answer('menu-buy-handler', show_alert=True)

    monkeypatch.setattr(subscription_purchase, 'start_subscription_purchase', menu_buy_marker)

    _register_update_handlers(dispatcher)

    assert dispatcher.sub_routers[-1].name == 'unknown_callback_fallback'
    assert dispatcher.sub_routers[-1].callback_query.handlers[-1].callback is common.handle_unknown_callback

    typo_button = CustomBroadcastButton(label='Купить', action_type='callback', action_value='menu_buu')
    typo_keyboard = create_broadcast_keyboard([], custom_buttons=[typo_button.model_dump()])
    assert typo_keyboard is not None
    assert typo_keyboard.inline_keyboard[0][0].callback_data == 'menu_buu'

    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=1, callback_id='unknown-ru', data='menu_buu'),
        db_user=SimpleNamespace(language='ru'),
    )
    ru_answers = _answer_methods(session)
    assert len(ru_answers) == 1
    assert ru_answers[0].callback_query_id == 'unknown-ru'
    assert ru_answers[0].text == RU_UNKNOWN_CALLBACK
    assert ru_answers[0].show_alert is True

    state = dispatcher.fsm.get_context(bot=bot, chat_id=700_001, user_id=700_001)
    await state.set_state('broadcast:test-active')
    session.methods.clear()
    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=2, callback_id='unknown-en', data='back_to_mneu'),
        db_user=SimpleNamespace(language='en'),
    )
    assert await state.get_state() == 'broadcast:test-active'
    en_answers = _answer_methods(session)
    assert len(en_answers) == 1
    assert en_answers[0].callback_query_id == 'unknown-en'
    assert en_answers[0].text == EN_UNKNOWN_CALLBACK
    assert en_answers[0].show_alert is True

    session.methods.clear()
    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=3, callback_id='known-noop', data='noop'),
        db_user=SimpleNamespace(language='ru'),
    )
    noop_answers = _answer_methods(session)
    assert len(noop_answers) == 1
    assert noop_answers[0].callback_query_id == 'known-noop'
    assert noop_answers[0].text is None
    assert noop_answers[0].show_alert is None

    session.methods.clear()
    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=4, callback_id='known-menu-buy', data='menu_buy'),
        db_user=SimpleNamespace(language='ru'),
    )
    menu_buy_answers = _answer_methods(session)
    assert len(menu_buy_answers) == 1
    assert menu_buy_answers[0].callback_query_id == 'known-menu-buy'
    assert menu_buy_answers[0].text == 'menu-buy-handler'
    assert menu_buy_answers[0].show_alert is True

    session.methods.clear()
    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=5, callback_id='known-late', data='simple_subscription_retired'),
        db_user=SimpleNamespace(language='ru'),
    )
    late_answers = _answer_methods(session)
    assert len(late_answers) == 1
    assert late_answers[0].callback_query_id == 'known-late'
    assert late_answers[0].show_alert is True
    assert 'устаревший способ покупки отключён' in (late_answers[0].text or '')

    session.methods.clear()
    monkeypatch.setattr(type(decorators.settings), 'is_admin', lambda self, _user_id: False)
    await dispatcher.feed_update(
        bot,
        _callback_update(update_id=6, callback_id='known-child', data='reqch:list'),
        db_user=SimpleNamespace(language='ru'),
    )
    child_answers = _answer_methods(session)
    assert len(child_answers) == 1
    assert child_answers[0].callback_query_id == 'known-child'
    assert child_answers[0].text != RU_UNKNOWN_CALLBACK

    assert warning.call_count == 2
    await bot.session.close()


def test_custom_broadcast_callback_allowlist_is_exact_and_urls_are_unaffected() -> None:
    configured_callbacks = {config['callback'] for config in broadcast_routes.BROADCAST_BUTTONS.values()}
    expected_callbacks = configured_callbacks | {'menu_buy'}
    assert expected_callbacks == broadcast_routes.SAFE_CUSTOM_BROADCAST_CALLBACKS

    broadcast_routes._validate_custom_broadcast_callbacks(
        [
            *(
                CustomBroadcastButton(label=value, action_type='callback', action_value=value)
                for value in sorted(expected_callbacks)
            ),
            CustomBroadcastButton(label='Site', action_type='url', action_value='https://example.com'),
        ]
    )

    for unsafe in ('menu_bui', 'activate_button', 'admin_panel', 'tariff_confirm:999'):
        with pytest.raises(HTTPException, match=unsafe) as exc_info:
            broadcast_routes._validate_custom_broadcast_callbacks(
                [CustomBroadcastButton(label='Unsafe', action_type='callback', action_value=unsafe)]
            )
        assert exc_info.value.status_code == 400


class _TariffRows:
    def all(self):
        return []


class _NoWriteSession:
    def __init__(self) -> None:
        self.add_calls = 0
        self.commit_calls = 0

    async def execute(self, statement):
        return _TariffRows()

    def add(self, broadcast) -> None:
        self.add_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1


@pytest.mark.asyncio
@pytest.mark.parametrize('unsafe', ['menu_bui', 'activate_button', 'admin_panel', 'tariff_confirm:999'])
async def test_both_create_routes_reject_unsafe_callbacks_before_db_or_worker(monkeypatch, unsafe: str) -> None:
    telegram_starts = 0
    email_starts = 0

    async def fail_if_telegram_started(*args, **kwargs) -> None:
        nonlocal telegram_starts
        telegram_starts += 1

    async def fail_if_email_started(*args, **kwargs) -> None:
        nonlocal email_starts
        email_starts += 1

    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', fail_if_telegram_started)
    monkeypatch.setattr(broadcast_routes.email_broadcast_service, 'start_broadcast', fail_if_email_started)
    admin = SimpleNamespace(id=7, username='owner')

    for request, create in (
        (
            BroadcastCreateRequest(
                target='all',
                message_text='Legacy route',
                selected_buttons=[],
                custom_buttons=[CustomBroadcastButton(label='Unsafe', action_type='callback', action_value=unsafe)],
            ),
            broadcast_routes.create_broadcast,
        ),
        (
            CombinedBroadcastCreateRequest(
                channel='telegram',
                target='all',
                message_text='Live route',
                selected_buttons=[],
                custom_buttons=[CustomBroadcastButton(label='Unsafe', action_type='callback', action_value=unsafe)],
            ),
            broadcast_routes.create_combined_broadcast,
        ),
        (
            CombinedBroadcastCreateRequest(
                channel='both',
                target='all',
                message_text='Both channels',
                selected_buttons=[],
                custom_buttons=[CustomBroadcastButton(label='Unsafe', action_type='callback', action_value=unsafe)],
                email_subject='Email subject',
                email_html_content='<p>Email body</p>',
            ),
            broadcast_routes.create_combined_broadcast,
        ),
    ):
        session = _NoWriteSession()
        with pytest.raises(HTTPException, match=unsafe) as exc_info:
            await create(request, admin=admin, db=session)
        assert exc_info.value.status_code == 400
        assert session.add_calls == 0
        assert session.commit_calls == 0

    assert telegram_starts == 0
    assert email_starts == 0

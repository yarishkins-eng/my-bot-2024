from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_broadcasts as cabinet_routes
from app.cabinet.schemas.broadcasts import (
    BroadcastCreateRequest as CabinetBroadcastCreateRequest,
    BroadcastMediaRequest as CabinetBroadcastMediaRequest,
    CombinedBroadcastCreateRequest,
)
from app.handlers.admin import messages as admin_messages
from app.services.broadcast_service import BroadcastConfig, BroadcastMediaConfig, BroadcastService
from app.webapi.routes import broadcasts as webapi_routes
from app.webapi.schemas.broadcasts import (
    BroadcastCreateRequest as WebApiBroadcastCreateRequest,
    BroadcastMedia as WebApiBroadcastMedia,
)


_MALFORMED_TEXT = '<b>Привет & пока'
_SAFE_TEXT = '<b>Привет &amp; пока</b>'
_MALFORMED_CAPTION = '<i>Отдельная подпись'
_SAFE_CAPTION = '<i>Отдельная подпись</i>'


class _TariffRows:
    def all(self):
        return []


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commit_calls = 0

    async def execute(self, statement):
        return _TariffRows()

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, value) -> None:
        if getattr(value, 'id', None) is None:
            value.id = 8800 + len(self.added)
        if getattr(value, 'created_at', None) is None:
            value.created_at = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_all_create_routes_store_and_start_only_canonical_telegram_html(monkeypatch) -> None:
    started_configs: list[BroadcastConfig] = []

    async def capture_config(_broadcast_id: int, config: BroadcastConfig) -> None:
        started_configs.append(config)

    monkeypatch.setattr(cabinet_routes.broadcast_service, 'start_broadcast', capture_config)
    admin = SimpleNamespace(id=7, username='owner')

    legacy_session = _RecordingSession()
    await cabinet_routes.create_broadcast(
        CabinetBroadcastCreateRequest(
            target='all',
            message_text=_MALFORMED_TEXT,
            selected_buttons=[],
            media=CabinetBroadcastMediaRequest(
                type='photo',
                file_id='legacy-photo',
                caption=_MALFORMED_CAPTION,
            ),
        ),
        admin=admin,
        db=legacy_session,
    )

    combined_session = _RecordingSession()
    await cabinet_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='telegram',
            target='all',
            message_text=_MALFORMED_TEXT,
            selected_buttons=[],
            media=CabinetBroadcastMediaRequest(
                type='photo',
                file_id='combined-photo',
                caption=_MALFORMED_CAPTION,
            ),
        ),
        admin=admin,
        db=combined_session,
    )

    webapi_session = _RecordingSession()
    await webapi_routes.create_broadcast(
        WebApiBroadcastCreateRequest(
            target='all',
            message_text=_MALFORMED_TEXT,
            selected_buttons=[],
            media=WebApiBroadcastMedia(
                type='photo',
                file_id='webapi-photo',
                caption=_MALFORMED_CAPTION,
            ),
        ),
        token=SimpleNamespace(name='api-owner'),
        db=webapi_session,
    )

    assert [row.message_text for row in legacy_session.added + combined_session.added] == [
        _SAFE_TEXT,
        _SAFE_TEXT,
    ]
    assert [row.media_caption for row in legacy_session.added + combined_session.added] == [
        _SAFE_CAPTION,
        _SAFE_CAPTION,
    ]
    assert webapi_session.added[0].message_text == _SAFE_TEXT
    assert webapi_session.added[0].media_caption == _SAFE_CAPTION
    assert [config.message_text for config in started_configs] == [_SAFE_TEXT] * 3
    assert [config.media.caption for config in started_configs if config.media] == [
        _SAFE_CAPTION,
        _SAFE_CAPTION,
        _SAFE_CAPTION,
    ]


@pytest.mark.asyncio
async def test_script_only_is_rejected_before_history_or_worker_in_all_create_routes(monkeypatch) -> None:
    telegram_start = AsyncMock()
    email_start = AsyncMock()
    monkeypatch.setattr(cabinet_routes.broadcast_service, 'start_broadcast', telegram_start)
    monkeypatch.setattr(cabinet_routes.email_broadcast_service, 'start_broadcast', email_start)
    admin = SimpleNamespace(id=7, username='owner')
    script_only = '<script>hidden()</script><style>body{display:none}</style>'

    cases = (
        (
            cabinet_routes.create_broadcast,
            CabinetBroadcastCreateRequest(target='all', message_text=script_only, selected_buttons=[]),
            {'admin': admin},
        ),
        (
            cabinet_routes.create_combined_broadcast,
            CombinedBroadcastCreateRequest(
                channel='both',
                target='all',
                message_text=script_only,
                selected_buttons=[],
                email_subject='Subject',
                email_html_content='<p>Email remains valid</p>',
            ),
            {'admin': admin},
        ),
        (
            webapi_routes.create_broadcast,
            WebApiBroadcastCreateRequest(target='all', message_text=script_only, selected_buttons=[]),
            {'token': SimpleNamespace(name='api-owner')},
        ),
    )

    for create, request, dependencies in cases:
        session = _RecordingSession()
        with pytest.raises(HTTPException, match='пуст') as exc_info:
            await create(request, db=session, **dependencies)
        assert exc_info.value.status_code == 400
        assert session.added == []
        assert session.commit_calls == 0

    telegram_start.assert_not_awaited()
    email_start.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_effective_media_caption_is_rejected_before_history_or_worker_in_all_routes(monkeypatch) -> None:
    started = AsyncMock()
    monkeypatch.setattr(cabinet_routes.broadcast_service, 'start_broadcast', started)
    admin = SimpleNamespace(id=7, username='owner')
    empty_caption = '<script>hidden()</script>'
    cases = (
        (
            cabinet_routes.create_broadcast,
            CabinetBroadcastCreateRequest(
                target='all',
                message_text='Valid message',
                selected_buttons=[],
                media=CabinetBroadcastMediaRequest(type='photo', file_id='legacy-photo', caption=empty_caption),
            ),
            {'admin': admin},
        ),
        (
            cabinet_routes.create_combined_broadcast,
            CombinedBroadcastCreateRequest(
                channel='telegram',
                target='all',
                message_text='Valid message',
                selected_buttons=[],
                media=CabinetBroadcastMediaRequest(type='photo', file_id='combined-photo', caption=empty_caption),
            ),
            {'admin': admin},
        ),
        (
            webapi_routes.create_broadcast,
            WebApiBroadcastCreateRequest(
                target='all',
                message_text='Valid message',
                selected_buttons=[],
                media=WebApiBroadcastMedia(type='photo', file_id='webapi-photo', caption=empty_caption),
            ),
            {'token': SimpleNamespace(name='api-owner')},
        ),
    )

    for create, request, dependencies in cases:
        session = _RecordingSession()
        with pytest.raises(HTTPException, match='пуст'):
            await create(request, db=session, **dependencies)
        assert session.added == []
        assert session.commit_calls == 0

    started.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_only_does_not_apply_telegram_html_rules(monkeypatch) -> None:
    email_start = AsyncMock()
    monkeypatch.setattr(cabinet_routes.email_broadcast_service, 'start_broadcast', email_start)
    session = _RecordingSession()

    await cabinet_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='email',
            target='all_email',
            message_text='<script>unused Telegram field</script>',
            email_subject='Subject',
            email_html_content='<script>Email HTML is a separate contract</script>',
        ),
        admin=SimpleNamespace(id=7, username='owner'),
        db=session,
    )

    assert session.added[0].message_text is None
    email_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_sends_canonical_text_and_effective_media_caption() -> None:
    service = BroadcastService()
    bot = AsyncMock()
    service.set_bot(bot)
    keyboard = SimpleNamespace(inline_keyboard=[])

    text_config = BroadcastConfig(target='all', message_text=_MALFORMED_TEXT, selected_buttons=[])
    await service._deliver_message(123, text_config, keyboard)
    assert bot.send_message.await_args.kwargs == {
        'chat_id': 123,
        'text': _SAFE_TEXT,
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }

    bot.reset_mock()
    media_config = BroadcastConfig(
        target='all',
        message_text='<b>Not the caption',
        selected_buttons=[],
        media=BroadcastMediaConfig(type='photo', file_id='photo-id', caption=_MALFORMED_CAPTION),
    )
    await service._deliver_message(456, media_config, keyboard)
    assert bot.send_photo.await_args.kwargs['caption'] == _SAFE_CAPTION
    assert bot.send_photo.await_args.kwargs['reply_markup'] is keyboard
    bot.send_message.assert_not_awaited()

    fallback_config = BroadcastConfig(
        target='all',
        message_text=_MALFORMED_TEXT,
        selected_buttons=[],
        media=BroadcastMediaConfig(type='photo', file_id='photo-id', caption='   '),
    )
    assert fallback_config.media.caption == _SAFE_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize(('visible_length', 'uses_caption'), [(1024, True), (1025, False)])
async def test_media_caption_boundary_is_measured_after_html_parsing(
    visible_length: int,
    uses_caption: bool,
) -> None:
    service = BroadcastService()
    bot = AsyncMock()
    service.set_bot(bot)
    visible = 'я' * visible_length
    raw = f'<b>{visible}</b>'
    config = BroadcastConfig(
        target='all',
        message_text='fallback',
        selected_buttons=[],
        media=BroadcastMediaConfig(type='photo', file_id='photo-id', caption=raw),
    )

    await service._deliver_message(123, config, None)

    if uses_caption:
        assert bot.send_photo.await_args.kwargs['caption'] == raw
        bot.send_message.assert_not_awaited()
    else:
        assert 'caption' not in bot.send_photo.await_args.kwargs
        assert bot.send_message.await_args.kwargs['text'] == raw


@pytest.mark.asyncio
async def test_media_caption_boundary_counts_utf16_units() -> None:
    service = BroadcastService()
    bot = AsyncMock()
    service.set_bot(bot)
    raw = '😀' * 513
    config = BroadcastConfig(
        target='all',
        message_text='fallback',
        selected_buttons=[],
        media=BroadcastMediaConfig(type='photo', file_id='photo-id', caption=raw),
    )

    await service._deliver_message(123, config, None)

    assert 'caption' not in bot.send_photo.await_args.kwargs
    assert bot.send_message.await_args.kwargs['text'] == raw


@pytest.mark.asyncio
async def test_chat_admin_canonicalizes_before_saving_fsm_state() -> None:
    original = inspect.unwrap(admin_messages.process_broadcast_message)
    message = SimpleNamespace(text=_MALFORMED_TEXT, answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())

    await original(message, SimpleNamespace(language='ru'), state, SimpleNamespace())

    state.update_data.assert_awaited_once_with(broadcast_message=_SAFE_TEXT)
    assert message.answer.await_count == 1


@pytest.mark.asyncio
async def test_chat_admin_rejects_empty_canonical_text_without_advancing_state() -> None:
    original = inspect.unwrap(admin_messages.process_broadcast_message)
    message = SimpleNamespace(text='<script>hidden</script>', answer=AsyncMock())
    state = SimpleNamespace(update_data=AsyncMock())

    await original(message, SimpleNamespace(language='ru'), state, SimpleNamespace())

    state.update_data.assert_not_awaited()
    assert 'пуст' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_chat_admin_uses_common_delivery_and_preserves_retry_state(monkeypatch) -> None:
    original = inspect.unwrap(admin_messages.confirm_broadcast)
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'broadcast_target': 'all',
                'broadcast_message': _MALFORMED_TEXT,
                'selected_buttons': [],
                'has_media': True,
                'media_type': 'photo',
                'media_file_id': 'chat-photo',
            }
        ),
        clear=AsyncMock(),
    )
    callback = SimpleNamespace(
        bot=AsyncMock(),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=77),
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    db_user = SimpleNamespace(
        id=7,
        full_name='Owner',
        telegram_id=77,
        language='ru',
    )
    session = _RecordingSession()
    deliver_calls: list[tuple[BroadcastConfig, dict[str, bool]]] = []

    async def fake_deliver(self, telegram_id, config, keyboard, delivery_state):
        assert telegram_id == 909
        deliver_calls.append((config, delivery_state))
        if len(deliver_calls) == 1:
            delivery_state['media_sent'] = True
            raise RuntimeError('transient after media')
        assert delivery_state == {'media_sent': True}

    monkeypatch.setattr(admin_messages, 'safe_edit_or_send_text', AsyncMock())
    monkeypatch.setattr(admin_messages, '_get_telegram_target_recipient_ids', AsyncMock(return_value=[909]))
    monkeypatch.setattr(admin_messages, '_persist_broadcast_result', AsyncMock())
    monkeypatch.setattr(admin_messages.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr(BroadcastService, '_deliver_message', fake_deliver)

    await original(callback, db_user, state, session)

    assert len(deliver_calls) == 2
    assert deliver_calls[0][1] is deliver_calls[1][1]
    assert deliver_calls[0][0].message_text == _SAFE_TEXT
    assert deliver_calls[0][0].media.caption == _SAFE_TEXT
    assert session.added[0].message_text == _SAFE_TEXT
    assert session.added[0].media_caption == _SAFE_TEXT
    state.clear.assert_awaited_once()

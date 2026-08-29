"""HD: identity-bound one-recipient Telegram canary from the cabinet."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from fastapi import HTTPException

from app.cabinet.routes import admin_broadcasts as broadcast_routes
from app.cabinet.schemas.broadcasts import (
    BroadcastCreateRequest,
    BroadcastPreviewRequest,
    CombinedBroadcastCreateRequest,
)
from app.database.models import UserStatus
from app.services import broadcast_service as broadcast_module
from app.services.broadcast_service import BroadcastConfig, BroadcastService
from app.webapi.schemas.broadcasts import BroadcastCreateRequest as WebApiBroadcastCreateRequest


_ACTOR_ID = 73
_TELEGRAM_ID = 918273645


class _Rows:
    def all(self):
        return []

    def scalars(self):
        return self


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    async def execute(self, _statement):
        return _Rows()

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        if getattr(value, 'id', None) is None:
            value.id = 501
        if getattr(value, 'created_at', None) is None:
            value.created_at = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        value.completed_at = None
        value.blocked_count = 0


def _admin():
    return SimpleNamespace(id=_ACTOR_ID, username='controlled-owner')


def _legacy_request() -> BroadcastCreateRequest:
    return BroadcastCreateRequest(
        target='self',
        message_text='Controlled canary',
        selected_buttons=[],
        category='system',
    )


def _combined_request(channel: str = 'telegram') -> CombinedBroadcastCreateRequest:
    values = {
        'channel': channel,
        'target': 'self',
        'message_text': 'Controlled canary',
        'selected_buttons': [],
        'category': 'system',
    }
    if channel == 'both':
        values.update(email_subject='Email subject', email_html_content='<p>Email</p>')
    return CombinedBroadcastCreateRequest(**values)


@pytest.mark.asyncio
async def test_self_projection_requires_server_actor_and_respects_status_channel_and_opt_out(
    monkeypatch,
) -> None:
    """Literal ``self`` never falls through to the broad public selector."""

    public_selector = AsyncMock(side_effect=AssertionError('self must not use get_target_users'))
    monkeypatch.setattr(broadcast_module, 'get_target_users', public_selector)

    class Session:
        def __init__(self, user) -> None:
            self.user = user
            self.requested: list[int] = []

        async def get(self, model, user_id: int):
            self.requested.append(user_id)
            return self.user

    active = SimpleNamespace(
        id=_ACTOR_ID,
        telegram_id=_TELEGRAM_ID,
        status=UserStatus.ACTIVE.value,
        notification_settings={},
    )
    assert await broadcast_module.resolve_telegram_broadcast_recipient_ids(
        Session(active),
        'self',
        'system',
        actor_user_id=_ACTOR_ID,
    ) == [_TELEGRAM_ID]

    assert (
        await broadcast_module.resolve_telegram_broadcast_recipient_ids(
            Session(active),
            'self',
            'system',
        )
        == []
    )

    for rejected in (
        None,
        SimpleNamespace(
            id=_ACTOR_ID,
            telegram_id=None,
            status=UserStatus.ACTIVE.value,
            notification_settings={},
        ),
        SimpleNamespace(
            id=_ACTOR_ID,
            telegram_id=_TELEGRAM_ID,
            status=UserStatus.BLOCKED.value,
            notification_settings={},
        ),
        SimpleNamespace(
            id=_ACTOR_ID,
            telegram_id=_TELEGRAM_ID,
            status=UserStatus.ACTIVE.value,
            notification_settings={'promo_offers_enabled': False},
        ),
    ):
        assert (
            await broadcast_module.resolve_telegram_broadcast_recipient_ids(
                Session(rejected),
                'self',
                'promo',
                actor_user_id=_ACTOR_ID,
            )
            == []
        )

    public_selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_and_preview_bind_self_to_authenticated_admin(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None]] = []

    async def fake_get_target_users(_session, target: str):
        assert target == 'all'
        return []

    async def fake_resolver(
        _session,
        target: str,
        category: str,
        *,
        preloaded_users=None,
        actor_user_id=None,
    ):
        calls.append((target, category, actor_user_id))
        return [_TELEGRAM_ID] if target == 'self' and actor_user_id == _ACTOR_ID else []

    async def fake_tariff_counts(_session, _category, *, preloaded_users=None):
        return {}

    monkeypatch.setattr(broadcast_routes, 'get_target_users', fake_get_target_users)
    monkeypatch.setattr(broadcast_routes, 'resolve_telegram_broadcast_recipient_ids', fake_resolver)
    monkeypatch.setattr(broadcast_routes, '_get_tariff_user_counts', fake_tariff_counts)

    session = _RecordingSession()
    catalog = await broadcast_routes.get_filters(category='promo', admin=_admin(), db=session)
    self_filter = next(item for item in catalog.filters if item.key == 'self')
    assert self_filter.count == 1
    assert self_filter.label == 'Тест: только мне'

    preview = await broadcast_routes.preview_broadcast(
        BroadcastPreviewRequest(target='self', category='news'),
        admin=_admin(),
        db=session,
    )
    assert preview.count == 1
    assert ('self', 'promo', _ACTOR_ID) in calls
    assert ('self', 'news', _ACTOR_ID) in calls


@pytest.mark.asyncio
@pytest.mark.parametrize('recipient_ids', [[], [101, 202]])
@pytest.mark.parametrize('route_name', ['legacy', 'combined'])
async def test_both_cabinet_create_routes_block_non_exact_self_before_history(
    monkeypatch,
    recipient_ids,
    route_name: str,
) -> None:
    calls: list[int | None] = []

    async def fake_resolver(_session, target, category, *, actor_user_id=None, **_kwargs):
        assert target == 'self'
        calls.append(actor_user_id)
        return recipient_ids

    telegram_start = AsyncMock()
    monkeypatch.setattr(broadcast_routes, 'resolve_telegram_broadcast_recipient_ids', fake_resolver)
    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', telegram_start)
    session = _RecordingSession()

    with pytest.raises(HTTPException) as exc_info:
        if route_name == 'legacy':
            await broadcast_routes.create_broadcast(_legacy_request(), admin=_admin(), db=session)
        else:
            await broadcast_routes.create_combined_broadcast(
                _combined_request(),
                admin=_admin(),
                db=session,
            )

    assert exc_info.value.status_code == 400
    assert calls == [_ACTOR_ID]
    assert session.added == []
    assert session.commits == 0
    telegram_start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('route_name', ['legacy', 'combined'])
async def test_both_cabinet_create_routes_propagate_only_server_actor_to_worker(
    monkeypatch,
    route_name: str,
) -> None:
    resolver_calls: list[int | None] = []
    started: list[BroadcastConfig] = []

    async def fake_resolver(_session, target, category, *, actor_user_id=None, **_kwargs):
        resolver_calls.append(actor_user_id)
        return [_TELEGRAM_ID]

    async def fake_start(_broadcast_id: int, config: BroadcastConfig) -> None:
        started.append(config)

    monkeypatch.setattr(broadcast_routes, 'resolve_telegram_broadcast_recipient_ids', fake_resolver)
    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', fake_start)
    session = _RecordingSession()

    if route_name == 'legacy':
        await broadcast_routes.create_broadcast(_legacy_request(), admin=_admin(), db=session)
    else:
        await broadcast_routes.create_combined_broadcast(_combined_request(), admin=_admin(), db=session)

    assert resolver_calls == [_ACTOR_ID]
    assert len(session.added) == 1
    assert session.commits == 1
    assert len(started) == 1
    assert started[0].target == 'self'
    assert started[0].actor_user_id == _ACTOR_ID


@pytest.mark.asyncio
async def test_direct_both_with_self_is_rejected_before_history_and_workers(monkeypatch) -> None:
    telegram_start = AsyncMock()
    email_start = AsyncMock()
    resolver = AsyncMock(return_value=[_TELEGRAM_ID])
    monkeypatch.setattr(broadcast_routes, 'resolve_telegram_broadcast_recipient_ids', resolver)
    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', telegram_start)
    monkeypatch.setattr(broadcast_routes.email_broadcast_service, 'start_broadcast', email_start)
    session = _RecordingSession()

    with pytest.raises(HTTPException) as exc_info:
        await broadcast_routes.create_combined_broadcast(
            _combined_request('both'),
            admin=_admin(),
            db=session,
        )

    assert exc_info.value.status_code == 400
    assert session.added == []
    assert session.commits == 0
    resolver.assert_not_awaited()
    telegram_start.assert_not_awaited()
    email_start.assert_not_awaited()


def test_external_api_token_schema_rejects_self_without_authenticated_user() -> None:
    with pytest.raises(ValueError, match='Unsupported target value'):
        WebApiBroadcastCreateRequest(target='self', message_text='No actor', selected_buttons=[])


@pytest.mark.asyncio
@pytest.mark.parametrize('recipient_ids', [[], [101, 202]])
async def test_self_worker_drift_fails_instead_of_completed_zero(monkeypatch, recipient_ids) -> None:
    broadcast = SimpleNamespace(
        id=501,
        status='queued',
        sent_count=0,
        failed_count=0,
        blocked_count=0,
        total_count=0,
    )

    class Session:
        async def get(self, _model, _broadcast_id):
            return broadcast

        async def commit(self):
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return False

    service = BroadcastService()
    service.set_bot(AsyncMock())
    monkeypatch.setattr(broadcast_module, 'AsyncSessionLocal', SessionContext)
    monkeypatch.setattr(service, '_fetch_recipients', AsyncMock(return_value=recipient_ids))
    failed = AsyncMock()
    finished = AsyncMock()
    monkeypatch.setattr(service, '_mark_failed', failed)
    monkeypatch.setattr(service, '_mark_finished', finished)

    await service._run_broadcast(
        501,
        BroadcastConfig(
            target='self',
            message_text='Controlled canary',
            selected_buttons=[],
            actor_user_id=_ACTOR_ID,
        ),
        asyncio.Event(),
    )

    failed.assert_awaited_once()
    finished.assert_not_awaited()
    assert broadcast.total_count == len(recipient_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error_factory',
    [
        lambda method: TelegramRetryAfter(method=method, message='retry', retry_after=0),
        lambda method: TelegramBadRequest(method=method, message='bad html'),
        lambda method: TelegramNetworkError(method=method, message='network'),
        lambda _method: RuntimeError('unexpected'),
    ],
)
async def test_broadcast_worker_error_logs_never_include_raw_telegram_id(
    monkeypatch,
    error_factory,
) -> None:
    service = BroadcastService()
    service.set_bot(AsyncMock())
    method = SendMessage(chat_id=_TELEGRAM_ID, text='Controlled canary')
    monkeypatch.setattr(service, '_deliver_message', AsyncMock(side_effect=error_factory(method)))
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    monkeypatch.setattr(broadcast_module.asyncio, 'sleep', AsyncMock())

    records: list[dict] = []

    def capture(_event, **kwargs):
        records.append(kwargs)

    monkeypatch.setattr(broadcast_module.logger, 'warning', capture)
    monkeypatch.setattr(broadcast_module.logger, 'error', capture)

    await service._send_batched(
        501,
        [_TELEGRAM_ID],
        BroadcastConfig(target='self', message_text='Controlled canary', selected_buttons=[], actor_user_id=_ACTOR_ID),
        None,
        asyncio.Event(),
    )

    assert records
    assert all('telegram_id' not in record for record in records)
    assert str(_TELEGRAM_ID) not in str(records)


@pytest.mark.asyncio
async def test_stop_during_batch_does_not_count_untouched_recipients_as_failures(monkeypatch) -> None:
    """РС-12: после stop нетронутые адресаты не становятся ошибками."""
    service = BroadcastService()
    service.set_bot(AsyncMock())
    cancel_event = asyncio.Event()

    async def deliver_once_then_stop(*_args, **_kwargs) -> None:
        cancel_event.set()

    monkeypatch.setattr(service, '_deliver_message', deliver_once_then_stop)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    mark_cancelled = AsyncMock()
    monkeypatch.setattr(service, '_mark_cancelled', mark_cancelled)
    monkeypatch.setattr(broadcast_module.asyncio, 'sleep', AsyncMock())

    sent, failed, blocked, cancelled = await service._send_batched(
        501,
        [1001, 1002, 1003],
        BroadcastConfig(target='all', message_text='Controlled', selected_buttons=[]),
        None,
        cancel_event,
    )

    assert (sent, failed, blocked, cancelled) == (1, 0, 0, True)
    mark_cancelled.assert_awaited_once_with(501, 1, 0, 0)


@pytest.mark.asyncio
async def test_stop_after_last_delivery_preserves_delivery_outcome(monkeypatch) -> None:
    """A stop racing with the final success skipped nobody and is not cancellation."""
    service = BroadcastService()
    service.set_bot(AsyncMock())
    cancel_event = asyncio.Event()

    async def deliver_then_stop(*_args, **_kwargs) -> None:
        cancel_event.set()

    monkeypatch.setattr(service, '_deliver_message', deliver_then_stop)
    monkeypatch.setattr(service, '_update_progress', AsyncMock())
    mark_cancelled = AsyncMock()
    monkeypatch.setattr(service, '_mark_cancelled', mark_cancelled)
    monkeypatch.setattr(broadcast_module.asyncio, 'sleep', AsyncMock())

    result = await service._send_batched(
        501,
        [1001],
        BroadcastConfig(target='all', message_text='Controlled', selected_buttons=[]),
        None,
        cancel_event,
    )

    assert result == (1, 0, 0, False)
    mark_cancelled.assert_not_awaited()


def test_history_target_labels_hide_internal_filter_keys() -> None:
    assert broadcast_routes._broadcast_target_label('self') == 'Тест: только мне'
    assert broadcast_routes._broadcast_target_label('custom_week') == 'Регистрация за неделю'
    assert broadcast_routes._broadcast_target_label('active_email').startswith('Есть подписка')
    assert broadcast_routes._broadcast_target_label('tariff_17') == 'Тариф #17'
    assert broadcast_routes._broadcast_target_label('tariff_17', {17: 'Премиум'}) == 'Тариф «Премиум»'
    assert broadcast_routes._broadcast_target_label('crafted_unknown') == 'Неизвестная аудитория'


@pytest.mark.asyncio
async def test_stop_refresh_preserves_worker_terminal_status(monkeypatch) -> None:
    """Worker completion between stop request and commit cannot stick at cancelling."""
    broadcast = SimpleNamespace(
        id=501,
        status='in_progress',
        channel='telegram',
        target_type='all',
        message_text='Controlled',
        has_media=False,
        media_type=None,
        media_file_id=None,
        media_caption=None,
        total_count=3,
        sent_count=3,
        failed_count=0,
        blocked_count=0,
        admin_id=_ACTOR_ID,
        admin_name='owner',
        created_at=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        completed_at=None,
        category='system',
        email_subject=None,
        email_html_content=None,
    )

    class RaceSession:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def get(self, _model, _id):
            return broadcast

        async def execute(self, statement):
            self.statements.append(str(statement))
            # The worker wins immediately before the conditional UPDATE.
            broadcast.status = 'completed'
            broadcast.completed_at = datetime(2026, 8, 29, 10, 1, tzinfo=UTC)
            return SimpleNamespace(rowcount=0)

        async def refresh(self, _value) -> None:
            return None

        async def commit(self) -> None:
            return None

    request_stop = AsyncMock(return_value=True)
    monkeypatch.setattr(broadcast_routes.broadcast_service, 'request_stop', request_stop)
    session = RaceSession()

    response = await broadcast_routes.stop_broadcast(501, admin=_admin(), db=session)

    assert response.status == 'completed'
    assert broadcast.status == 'completed'
    assert 'broadcast_history.status IN' in session.statements[0]
    request_stop.assert_not_awaited()

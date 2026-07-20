"""``send_funnel_trial_menu`` всегда обновляет меню после активации триала.

История: в Чате 1 (коммит 4177e10b) к функции добавили параметр ``source`` и
ранний выход при ``source='cabinet'`` — чтобы «не дублировать» меню при активации
триала из React-кабинета. Это была ошибка: webview кабинета и сообщение-меню в
чате Telegram — РАЗНЫЕ поверхности, поэтому у пользователя в чате оставалась
устаревшая кнопка «Попробовать бесплатно». Глушилку сняли — теперь функция шлёт
меню из ЛЮБОГО места активации (кабинет, legacy mini-app, админ-выдача) одинаково,
а единственный легитимный «молчок» — выключенный флаг воронки.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils import funnel_notify


class _BotRecorder:
    """Подмена create_bot; считает, дошли ли до шага отправки сообщения."""

    def __init__(self, bot):
        self.calls = 0
        self._bot = bot

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._bot


def _wire(monkeypatch: pytest.MonkeyPatch, *, funnel_enabled: bool = True) -> _BotRecorder:
    async def _fake_send_message(*args, **kwargs):
        return SimpleNamespace(message_id=7)

    async def _fake_close():
        return None

    fake_bot = SimpleNamespace(
        send_message=_fake_send_message,
        session=SimpleNamespace(close=_fake_close),
    )
    recorder = _BotRecorder(fake_bot)

    async def _noop_delete(bot, telegram_id):
        return None

    async def _noop_remember(telegram_id, message_id):
        return None

    monkeypatch.setattr(funnel_notify, '_funnel_enabled', lambda: funnel_enabled)
    monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', _noop_delete)
    monkeypatch.setattr(funnel_notify, '_remember_menu_message_id', _noop_remember)
    monkeypatch.setattr('app.bot_factory.create_bot', recorder)
    # Клавиатура не-None, чтобы функция дошла до шага отправки.
    monkeypatch.setattr('app.keyboards.inline.build_funnel_menu_keyboard', lambda *a, **k: object())
    return recorder


@pytest.mark.asyncio
async def test_sends_trial_menu_after_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Активация триала (из любого места, включая кабинет) обновляет меню в чате."""
    recorder = _wire(monkeypatch)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.send_funnel_trial_menu(user)

    assert recorder.calls == 1  # меню активного триала отправлено, старое заменено


@pytest.mark.asyncio
async def test_trial_menu_includes_ready_connection_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Новая активная тестовая подписка сразу получает кнопку без /start."""
    _wire(monkeypatch)
    captured = {}

    def _build(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr('app.keyboards.inline.build_funnel_menu_keyboard', _build)
    monkeypatch.setattr(
        'app.utils.subscription_link_access.get_user_subscription_with_available_link',
        lambda user: object(),
    )
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.send_funnel_trial_menu(user)

    assert captured['show_connection_link'] is True


@pytest.mark.asyncio
async def test_notify_trial_menu_refreshes_subscription_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Гейт ссылки видит готовый URL, созданный в той же транзакции выдачи триала."""
    send_menu = AsyncMock()
    monkeypatch.setattr(funnel_notify, 'send_funnel_trial_menu', send_menu)
    db = SimpleNamespace(refresh=AsyncMock())
    user = SimpleNamespace()

    await funnel_notify.notify_trial_menu(db, user)

    db.refresh.assert_awaited_once_with(user, ['subscriptions'])
    send_menu.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_no_send_when_funnel_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Единственный легитимный «молчок» — выключенная воронка."""
    recorder = _wire(monkeypatch, funnel_enabled=False)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.send_funnel_trial_menu(user)

    assert recorder.calls == 0


@pytest.mark.asyncio
async def test_no_send_when_no_telegram_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Email-only пользователь (нет telegram_id) — слать в чат некуда."""
    recorder = _wire(monkeypatch)
    user = SimpleNamespace(telegram_id=None, language='ru')

    await funnel_notify.send_funnel_trial_menu(user)

    assert recorder.calls == 0


@pytest.mark.asyncio
async def test_clear_funnel_menu_deletes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потеря доступа (обнуление): устаревшее меню удаляется из чата."""
    recorder = _wire(monkeypatch)
    deleted = {}

    async def _spy_delete(bot, telegram_id):
        deleted['telegram_id'] = telegram_id

    monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', _spy_delete)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.clear_funnel_menu(user)

    assert deleted.get('telegram_id') == 123
    assert recorder.calls == 1  # бот создан, чтобы удалить сообщение


@pytest.mark.asyncio
async def test_clear_funnel_menu_noop_when_funnel_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _wire(monkeypatch, funnel_enabled=False)
    called = {'delete': False}

    async def _spy_delete(bot, telegram_id):
        called['delete'] = True

    monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', _spy_delete)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.clear_funnel_menu(user)

    assert called['delete'] is False
    assert recorder.calls == 0

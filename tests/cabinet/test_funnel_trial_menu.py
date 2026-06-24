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

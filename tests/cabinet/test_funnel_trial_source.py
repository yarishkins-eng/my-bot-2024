"""``send_funnel_trial_menu(source=...)`` gating (Чат 1 — бэкенд-поля, правка 5).

The trial-activation chat push fires from TWO places: the React cabinet
(``app/cabinet/.../purchase.py``) and the legacy Telegram mini-app
(``app/webapi/routes/miniapp.py``). The React cabinet updates its own screen
after activation, so its chat push is a duplicate — suppress it. The legacy
mini-app keeps pushing (no source). See РЕВЬЮ-волна1: «триал-пуш из ДВУХ мест;
гасим один (верно для React-кабинета)».

Contract: ``source='cabinet'`` → no bot is created / nothing is sent; any other
source (or none) → it proceeds to send as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils import funnel_notify


class _BotRecorder:
    """Stands in for create_bot; records whether the send path was entered."""

    def __init__(self, bot):
        self.calls = 0
        self._bot = bot

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._bot


def _wire(monkeypatch: pytest.MonkeyPatch) -> _BotRecorder:
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

    # Funnel ON + a real telegram_id so the only thing that can stop the send is `source`.
    monkeypatch.setattr(funnel_notify, '_funnel_enabled', lambda: True)
    monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', _noop_delete)
    monkeypatch.setattr(funnel_notify, '_remember_menu_message_id', _noop_remember)
    monkeypatch.setattr('app.bot_factory.create_bot', recorder)
    # Keep the keyboard non-None so the function reaches the bot-send step.
    monkeypatch.setattr('app.keyboards.inline.build_funnel_menu_keyboard', lambda *a, **k: object())
    return recorder


@pytest.mark.asyncio
async def test_cabinet_source_suppresses_chat_push(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _wire(monkeypatch)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.send_funnel_trial_menu(user, source='cabinet')

    assert recorder.calls == 0  # React cabinet: no duplicate chat menu


@pytest.mark.asyncio
async def test_no_source_still_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy mini-app path (no source) must keep sending the menu."""
    recorder = _wire(monkeypatch)
    user = SimpleNamespace(telegram_id=123, language='ru')

    await funnel_notify.send_funnel_trial_menu(user)

    assert recorder.calls == 1

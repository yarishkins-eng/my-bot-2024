"""Мина MU: кабинет не должен получать ссылку-заглушку ``t.me/bot?start=…``.

Имя бота приходит из ``get_me()`` на старте (``sync_bot_username``). Если оно неизвестно,
``settings.get_bot_referral_link`` подставляет «bot», и кабинет показал бы, скопировал и
отправил друзьям мёртвую ссылку — его запасной путь срабатывает только на ПУСТОЙ строке.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.referral import get_referral_info
from app.config import settings


def _db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar=lambda: 0))
    return db


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=7, referral_code='refABC', referral_commission_percent=None, balance_kopeks=0)


@pytest.mark.asyncio
async def test_no_bot_username_means_empty_bot_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'BOT_USERNAME', None)

    response = await get_referral_info(user=_user(), db=_db())

    assert response.bot_referral_link == '', 'заглушка t.me/bot?start=… не должна уходить в кабинет'
    assert response.referral_code == 'refABC'


@pytest.mark.asyncio
async def test_known_bot_username_gives_real_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot')

    response = await get_referral_info(user=_user(), db=_db())

    assert response.bot_referral_link == 'https://t.me/teplo_VPN_bot?start=refABC'
    assert '/bot?start=' not in response.bot_referral_link

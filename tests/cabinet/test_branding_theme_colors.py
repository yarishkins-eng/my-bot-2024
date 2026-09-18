"""Кабинет всегда тёмный (17.09.2026): сервер не знает светлых полей и переключателя тем.

Сторож этапа СТ-3. До него /branding/colors отдавал четыре light-поля, а /branding/themes
говорил, какие темы включены; кабинет с СТ-1 ничего из этого не читает. Строка в базе
может ещё хранить старые light-ключи — ответ их не показывает, а PATCH не сохраняет
присланные.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import branding
from app.services.permission_service import PermissionService


STORED_WITH_LIGHT = {
    'accent': '#abcdef',  # не заводской: отличаем «прочитал строку БД» от «провалился в дефолты»
    'darkBackground': '#0a0f1a',
    'darkSurface': '#0f172a',
    'darkText': '#f1f5f9',
    'darkTextSecondary': '#94a3b8',
    'lightBackground': '#F7E7CE',
    'lightSurface': '#FEF9F0',
    'lightText': '#1F1A12',
    'lightTextSecondary': '#7D6B48',
    'success': '#22c55e',
    'warning': '#f59e0b',
    'error': '#ef4444',
}
DARK_KEYS = {
    'accent',
    'darkBackground',
    'darkSurface',
    'darkText',
    'darkTextSecondary',
    'success',
    'warning',
    'error',
}


def _app(monkeypatch: pytest.MonkeyPatch, saved: list[tuple[str, str]]) -> FastAPI:
    async def fake_get(db, key):
        return json.dumps(STORED_WITH_LIGHT) if key == branding.THEME_COLORS_KEY else None

    async def fake_set(db, key, value):
        saved.append((key, value))

    monkeypatch.setattr(branding, 'get_setting_value', fake_get)
    monkeypatch.setattr(branding, 'set_setting_value', fake_set)
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    app = FastAPI()
    app.include_router(branding.router, prefix='/cabinet')

    async def database():
        yield SimpleNamespace(commit=AsyncMock())

    async def current_user():
        return SimpleNamespace(id=77, telegram_id=1)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    return app


@pytest.mark.asyncio
async def test_colors_response_has_no_light_fields_even_if_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch, saved=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/cabinet/branding/colors')

    assert response.status_code == 200
    assert set(response.json()) == DARK_KEYS
    assert response.json()['accent'] == '#abcdef'
    assert set(branding.DEFAULT_THEME_COLORS) == DARK_KEYS


@pytest.mark.asyncio
async def test_enabled_themes_route_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch, saved=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/cabinet/branding/themes')).status_code == 404
        assert (await client.patch('/cabinet/branding/themes', json={'light': True})).status_code == 404


@pytest.mark.asyncio
async def test_patch_ignores_light_fields_from_client(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[tuple[str, str]] = []
    app = _app(monkeypatch, saved)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.patch(
            '/cabinet/branding/colors',
            json={'accent': '#112233', 'lightBackground': '#ffffff'},
        )

    assert response.status_code == 200
    assert response.json()['accent'] == '#112233'
    assert 'lightBackground' not in response.json()
    assert saved and saved[0][0] == branding.THEME_COLORS_KEY
    stored = json.loads(saved[0][1])
    assert stored['accent'] == '#112233'
    # Присланное светлое поле сервер не принял; старые ключи строки БД до SQL-чистки
    # переживают PATCH как есть — это известно и чистится руками после деплоя.
    assert stored['lightBackground'] == '#F7E7CE'

"""Экран продаж (СП-1): списки людей — только тем, кому видна карточка пользователя; реклама — кому видна статистика
кампаний. Права проверяются все сразу (`require_permission(*perms)`), и роль без второго получает 403, а не список."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_sales_stats, admin_stats
from app.services.permission_service import PermissionService


async def _request(monkeypatch: pytest.MonkeyPatch, path: str, granted: set[str], router=admin_sales_stats.router):
    async def check(db, user, permission, **_kwargs):
        return permission in granted, 'test denial'

    checker = AsyncMock(side_effect=check)
    monkeypatch.setattr(PermissionService, 'check_permission', checker)
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())
    app = FastAPI()
    app.include_router(router, prefix='/cabinet')
    db = SimpleNamespace(commit=AsyncMock())

    async def database():
        yield db

    async def current_user():
        return SimpleNamespace(id=77)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(path)
    return response, [call.args[2] for call in checker.await_args_list]


@pytest.mark.asyncio
async def test_people_list_needs_users_read_too(monkeypatch: pytest.MonkeyPatch) -> None:
    response, asked = await _request(
        monkeypatch, '/cabinet/admin/stats/sales/people?kind=ending_soon', {'sales_stats:read'}
    )

    assert response.status_code == 403
    assert asked == ['sales_stats:read', 'users:read']


@pytest.mark.asyncio
async def test_ads_need_campaign_stats_too(monkeypatch: pytest.MonkeyPatch) -> None:
    response, asked = await _request(monkeypatch, '/cabinet/admin/stats/sales/ads', {'sales_stats:read'})

    assert response.status_code == 403
    assert asked == ['sales_stats:read', 'campaigns:stats']


@pytest.mark.asyncio
async def test_statistics_money_needs_the_same_right_as_the_statistics_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Деньги на «Статистике» (СП-1б) видит тот же, кто видел их там до СП-1: право `stats:read`, как у `/dashboard`."""
    response, asked = await _request(monkeypatch, '/cabinet/admin/stats/money', set(), router=admin_stats.router)

    assert response.status_code == 403
    assert asked == ['stats:read']

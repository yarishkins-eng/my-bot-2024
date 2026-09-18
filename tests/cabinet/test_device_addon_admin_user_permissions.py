"""The manual add-on fulfillment retry stays behind users:subscription."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_users
from app.services.permission_service import PermissionService


@pytest.mark.asyncio
async def test_retry_device_addon_fulfillment_requires_subscription_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'test denial')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())
    app = FastAPI()
    app.include_router(admin_users.router, prefix='/cabinet')

    async def database():
        yield db

    async def current_user():
        return SimpleNamespace(id=77)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/cabinet/admin/users/41/device-addons/7bf502a1-4588-4fe4-9a87-8a7c9b1d87ae/retry-fulfillment'
        )

    assert response.status_code == 403
    check = PermissionService.check_permission
    check.assert_awaited_once()
    assert check.await_args.args[2] == 'users:subscription'
    db.commit.assert_awaited_once()

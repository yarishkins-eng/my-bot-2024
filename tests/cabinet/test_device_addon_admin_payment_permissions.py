"""The destructive-looking add-on recovery action stays behind payments:edit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes import admin_payments
from app.services.permission_service import PermissionService


@pytest.mark.asyncio
async def test_close_device_addon_attempt_requires_payments_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace(commit=AsyncMock())
    denied_log = AsyncMock()
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'test denial')))
    monkeypatch.setattr(PermissionService, 'log_action', denied_log)
    app = FastAPI()
    app.include_router(admin_payments.router, prefix='/cabinet')

    async def database():
        yield db

    async def current_user():
        return SimpleNamespace(id=77)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/cabinet/admin/payments/platega/41/close-device-addon-attempt')

    assert response.status_code == 403
    check = PermissionService.check_permission
    check.assert_awaited_once()
    assert check.await_args.args[2] == 'payments:edit'
    denied_log.assert_awaited_once()
    db.commit.assert_awaited_once()

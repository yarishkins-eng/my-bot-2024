"""Admin routes must not revive a financially closing identity."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import (
    ResolveFinancialAccountErasureRequest,
    UpdateRestrictionsRequest,
    UpdateSubscriptionRequest,
    UpdateUserStatusRequest,
    UserStatusEnum,
)
from app.services import account_erasure_service


def _closing_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=71,
        status='deleted',
        account_erasure_requested_at=datetime.now(UTC),
        restriction_topup=True,
        restriction_subscription=True,
        restriction_reason='account_erasure_requested',
    )


@pytest.mark.asyncio
async def test_admin_status_cannot_revive_financially_closing_account(monkeypatch) -> None:
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_closing_user()))

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_status(
            user_id=71,
            request=UpdateUserStatusRequest(status=UserStatusEnum.ACTIVE),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'account_erasure_pending'


@pytest.mark.asyncio
async def test_admin_restrictions_cannot_reopen_financially_closing_account(monkeypatch) -> None:
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_closing_user()))

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_restrictions(
            user_id=71,
            request=UpdateRestrictionsRequest(restriction_subscription=False),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_admin_unblock_cannot_revive_financially_closing_account(monkeypatch) -> None:
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_closing_user()))

    with pytest.raises(HTTPException) as error:
        await admin_users.unblock_user(user_id=71, admin=SimpleNamespace(id=1), db=SimpleNamespace())

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_admin_subscription_cannot_recreate_financially_closing_account(monkeypatch) -> None:
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_closing_user()))

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_subscription(
            user_id=71,
            request=UpdateSubscriptionRequest(action='create'),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_financial_erasure_resolution_stays_blocked_until_completion(monkeypatch) -> None:
    monkeypatch.setattr(
        account_erasure_service,
        'resolve_financial_account_erasure',
        AsyncMock(
            return_value=account_erasure_service.AccountErasureResult(
                state=account_erasure_service.ERASURE_AWAITING_MANUAL,
                message='Settlement is still required.',
                completed=False,
            )
        ),
    )

    with pytest.raises(HTTPException) as error:
        await admin_users.resolve_financial_account_erasure(
            user_id=71,
            request=ResolveFinancialAccountErasureRequest(
                resolution_code='refund_completed',
                resolution_note='Provider refund recorded.',
            ),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 409

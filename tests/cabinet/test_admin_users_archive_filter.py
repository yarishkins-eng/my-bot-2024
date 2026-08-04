"""Regression tests for the operational user list and erased-user archive."""

from __future__ import annotations

from inspect import signature
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import SortByEnum, UserStatusEnum
from app.database.crud.user import get_users_count, get_users_list, get_users_statistics
from app.database.models import UserStatus


def _list_route_kwargs(status: UserStatusEnum | None = None) -> dict:
    """Concrete values normally injected by FastAPI's Query dependencies."""

    return {
        'offset': 0,
        'limit': 50,
        'search': None,
        'email': None,
        'status': status,
        'subscription_status': None,
        'tariff_id': None,
        'promo_group_id': None,
        'campaign_id': None,
        'partner_id': None,
        'sort_by': SortByEnum.CREATED_AT,
        'admin': SimpleNamespace(id=1),
        'db': SimpleNamespace(),
    }


@pytest.mark.asyncio
async def test_default_admin_list_excludes_erased_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    listed_users = AsyncMock(return_value=[])
    counted_users = AsyncMock(return_value=0)
    monkeypatch.setattr(admin_users, 'get_users_list', listed_users)
    monkeypatch.setattr(admin_users, 'get_users_count', counted_users)

    response = await admin_users.list_users(**_list_route_kwargs())

    assert response.total == 0
    assert listed_users.await_args.kwargs['exclude_deleted'] is True
    assert counted_users.await_args.kwargs['exclude_deleted'] is True


def test_generic_user_lists_hide_erased_accounts_unless_an_archive_caller_opts_in() -> None:
    """Chat-admin searches share the same safe default as the cabinet list."""

    assert signature(get_users_list).parameters['exclude_deleted'].default is True
    assert signature(get_users_count).parameters['exclude_deleted'].default is True


@pytest.mark.asyncio
async def test_deleted_status_is_an_explicit_archive_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    listed_users = AsyncMock(return_value=[])
    counted_users = AsyncMock(return_value=0)
    monkeypatch.setattr(admin_users, 'get_users_list', listed_users)
    monkeypatch.setattr(admin_users, 'get_users_count', counted_users)

    await admin_users.list_users(**_list_route_kwargs(status=UserStatusEnum.DELETED))

    assert listed_users.await_args.kwargs['status'] is UserStatus.DELETED
    assert listed_users.await_args.kwargs['exclude_deleted'] is False
    assert counted_users.await_args.kwargs['status'] is UserStatus.DELETED
    assert counted_users.await_args.kwargs['exclude_deleted'] is False


@pytest.mark.asyncio
async def test_historical_statistics_contract_remains_unchanged() -> None:
    def result(value: int) -> SimpleNamespace:
        return SimpleNamespace(scalar=lambda: value)

    db = AsyncMock()
    # all rows, active, newly created today/week/month
    db.execute.side_effect = [result(10), result(6), result(1), result(3), result(5)]

    stats = await get_users_statistics(db)

    assert stats == {
        'total_users': 10,
        'active_users': 6,
        'blocked_users': 4,
        'new_today': 1,
        'new_week': 3,
        'new_month': 5,
    }


@pytest.mark.asyncio
async def test_cabinet_stats_exclude_erased_accounts_from_operational_counts() -> None:
    def scalar_result(value: int) -> SimpleNamespace:
        return SimpleNamespace(scalar=lambda: value)

    def row_result(value: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(one_or_none=lambda: value)

    db = AsyncMock()
    db.execute.side_effect = [
        scalar_result(10),  # all stored rows
        scalar_result(6),  # active
        scalar_result(1),  # new today
        scalar_result(3),  # new week
        scalar_result(5),  # new month
        row_result(SimpleNamespace(total=4, active=2, trial=1, expired=2)),  # subscriptions, joined to live users
        row_result(SimpleNamespace(total=12_345, avg=1_234)),  # balance
        scalar_result(1),  # active today
        scalar_result(2),  # active week
        scalar_result(3),  # active month
        scalar_result(2),  # erased archive
        scalar_result(2),  # blocked operational users
    ]

    response = await admin_users.get_users_stats(admin=SimpleNamespace(id=1), db=db)

    assert response.total_users == 8
    assert response.active_users == 6
    assert response.blocked_users == 2
    assert response.deleted_users == 2
    assert response.users_with_subscription == 4

    subscription_sql = str(db.execute.await_args_list[5].args[0])
    assert 'JOIN users' in subscription_sql
    assert 'users.status' in subscription_sql

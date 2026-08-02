from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.blocked_users_service import BlockedUsersService


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_blocked_user_cleanup_preserves_device_first_financial_evidence() -> None:
    """A legacy admin cleanup must stop before deleting provider records."""
    service = object.__new__(BlockedUsersService)
    user = SimpleNamespace(id=17, telegram_id=7001, email=None, subscriptions=[])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(user)),
        scalar=AsyncMock(return_value=1),
        delete=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    deleted = await service.delete_user_from_db(db, user.id)

    assert deleted is False
    db.scalar.assert_awaited_once()
    db.delete.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()

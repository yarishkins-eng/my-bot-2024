from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.device_first_checkout_service import reconcile_armed_checkouts


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_reconciler_resumes_every_armed_checkout_after_process_crash():
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result([('checkout-1', 7), ('checkout-2', 8)])),
        rollback=AsyncMock(),
    )

    with patch(
        'app.services.device_first_checkout_service.fulfill_checkout',
        AsyncMock(),
    ) as fulfill:
        processed = await reconcile_armed_checkouts(db)

    assert processed == 2
    assert fulfill.await_args_list[0].args == (db, 'checkout-1', 7)
    assert fulfill.await_args_list[1].args == (db, 'checkout-2', 8)
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciler_isolates_one_failed_checkout_and_continues():
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result([('broken', 7), ('healthy', 8)])),
        rollback=AsyncMock(),
    )

    with patch(
        'app.services.device_first_checkout_service.fulfill_checkout',
        AsyncMock(side_effect=[RuntimeError('temporary'), None]),
    ) as fulfill:
        processed = await reconcile_armed_checkouts(db)

    assert processed == 1
    assert fulfill.await_count == 2
    db.rollback.assert_awaited_once()

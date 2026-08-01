from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.device_first_checkout_service import (
    DIRECT_SETTLEMENT_MODE,
    DeviceFirstError,
    fulfill_direct_external_checkout,
    get_open_checkout_for_user,
    reconcile_armed_checkouts,
    settlement_mode,
)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows

    def scalar_one(self):
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


@pytest.mark.asyncio
async def test_paid_direct_provisioning_checkout_remains_resumable_after_quote_expiry():
    checkout = SimpleNamespace(
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        fulfillment_state='fulfilled',
        provisioning_state='pending',
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(checkout)),
        commit=AsyncMock(),
    )

    result = await get_open_checkout_for_user(db, user_id=7)

    assert result is checkout
    db.commit.assert_not_awaited()


def test_missing_settlement_mode_requires_operator_review():
    with pytest.raises(DeviceFirstError) as raised:
        settlement_mode(SimpleNamespace(settlement_mode=None))
    assert raised.value.code == 'operator_review_required'


@pytest.mark.asyncio
async def test_reversed_direct_attempt_cannot_fulfil_after_operator_review_wins_race():
    """A late success handler must not issue a subscription over an operator hold."""
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(None)))

    with pytest.raises(DeviceFirstError) as raised:
        await fulfill_direct_external_checkout(
            db,
            checkout_id=9,
            provider_payment_id='provider-1',
            payment_attempt_id=41,
        )

    assert raised.value.code == 'invalid_state'


@pytest.mark.asyncio
async def test_already_fulfilled_direct_sale_is_idempotent_for_later_reconciliation():
    attempt = SimpleNamespace(id=41, status='paid_processing')
    checkout = SimpleNamespace(
        id=9,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='fulfilled',
    )
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(attempt), Result(checkout)]))

    result = await fulfill_direct_external_checkout(
        db,
        checkout_id=9,
        provider_payment_id='provider-1',
        payment_attempt_id=41,
    )

    assert result is checkout
    assert db.execute.await_count == 2

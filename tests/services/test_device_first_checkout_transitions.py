from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as service


@pytest.mark.asyncio
async def test_cancel_is_rejected_after_fulfillment_started():
    checkout = SimpleNamespace(
        lifecycle_state='fulfilling',
        fulfillment_state='in_progress',
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    with pytest.raises(service.DeviceFirstError) as error:
        await service.cancel_checkout(db, checkout)

    assert error.value.code == 'invalid_state'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_is_rejected_after_provider_credit_commit():
    checkout = SimpleNamespace(
        lifecycle_state='armed',
        fulfillment_state='in_progress',
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())

    with pytest.raises(service.DeviceFirstError) as error:
        await service.cancel_checkout(db, checkout)

    assert error.value.code == 'invalid_state'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_locked_checkout_query_forces_fresh_orm_state():
    checkout = SimpleNamespace(lifecycle_state='ready')
    result = SimpleNamespace(scalar_one_or_none=lambda: checkout)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    returned = await service.get_owned_checkout(
        db,
        public_id='public-id',
        user_id=7,
        for_update=True,
    )

    query = db.execute.await_args.args[0]
    assert returned is checkout
    assert query.get_execution_options()['populate_existing'] is True


def test_credited_armed_checkout_is_serialized_as_processing():
    checkout = SimpleNamespace(
        lifecycle_state='armed',
        fulfillment_state='in_progress',
        provisioning_state='not_started',
    )

    assert service.checkout_ui_state(checkout) == 'processing'


@pytest.mark.asyncio
async def test_kill_switch_blocks_new_arm(monkeypatch):
    checkout = SimpleNamespace(
        lifecycle_state='confirmed',
        fulfillment_state='not_started',
        armed_at=None,
    )
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', False)

    with pytest.raises(service.DeviceFirstError) as error:
        await service.arm_checkout(db, checkout)

    assert error.value.code == 'feature_disabled'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_switch_still_drains_already_armed_checkout(monkeypatch):
    checkout = SimpleNamespace(
        public_id='checkout-1',
        user_id=7,
        lifecycle_state='armed',
        fulfillment_state='not_started',
        armed_at=object(),
    )
    db = SimpleNamespace(commit=AsyncMock())
    result = SimpleNamespace(lifecycle_state='ready')
    fulfill = AsyncMock(return_value=result)
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', False)
    monkeypatch.setattr(service, 'fulfill_checkout', fulfill)

    returned = await service.arm_checkout(db, checkout)

    assert returned is result
    fulfill.assert_awaited_once_with(db, 'checkout-1', 7)

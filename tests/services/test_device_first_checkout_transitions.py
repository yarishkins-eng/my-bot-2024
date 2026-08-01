from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as service


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


def paid_target(*, device_limit: int = 4):
    return SimpleNamespace(
        id=12,
        tariff_id=99,
        status='active',
        is_trial=False,
        device_limit=device_limit,
        end_date=datetime.now(UTC) + timedelta(days=20),
        updated_at=datetime.now(UTC),
    )


def armed_checkout(target, *, devices: int, quoted_price: int = 10_000):
    return SimpleNamespace(
        id=51,
        public_id='checkout-51',
        user_id=7,
        lifecycle_state='armed',
        fulfillment_state='not_started',
        provisioning_state='not_started',
        target_subscription_id=target.id,
        target_snapshot=service._subscription_snapshot(target),
        expect_no_subscription=False,
        tariff_id=7,
        period_days=30,
        selected_device_limit=devices,
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        pricing_revision=1,
        quoted_price_kopeks=quoted_price,
        quote_state='valid',
        terminal_reason=None,
    )


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
        settlement_mode='legacy_deposit',
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
        settlement_mode='legacy_deposit',
    )
    db = SimpleNamespace(commit=AsyncMock())
    result = SimpleNamespace(lifecycle_state='ready')
    fulfill = AsyncMock(return_value=result)
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', False)
    monkeypatch.setattr(service, 'fulfill_checkout', fulfill)

    returned = await service.arm_checkout(db, checkout)

    assert returned is result
    fulfill.assert_awaited_once_with(db, 'checkout-1', 7)


@pytest.mark.asyncio
async def test_create_rejects_a_direct_cross_tariff_device_downgrade_before_creating_a_checkout(monkeypatch):
    user = SimpleNamespace(id=7, restriction_subscription=False)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), get=AsyncMock())
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(
        service,
        'build_purchase_options',
        AsyncMock(
            return_value={
                'eligible': True,
                'current_subscription': {'is_trial': False, 'device_limit': 4},
                'period_options': [30],
                'device_options': [4, 5],
            }
        ),
    )

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=3,
            source='cabinet',
        )

    assert raised.value.code == 'device_limit_decrease_not_allowed'
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_fulfillment_conflicts_before_debit_when_live_paid_target_would_be_downgraded(monkeypatch):
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=3)
    user = SimpleNamespace(id=7, balance_kopeks=50_000, has_had_paid_subscription=False)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ScalarResult(user),
                ScalarResult(target),
                ScalarResult(SimpleNamespace(id=7, pricing_revision=1)),
            ]
        ),
        commit=AsyncMock(),
        add=AsyncMock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=checkout))
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda tariff, subscription: SimpleNamespace(
            eligible=True,
            period_options=(30,),
            device_options=(3, 4, 5),
        ),
    )
    calculate = AsyncMock(return_value=SimpleNamespace(final_total=10_000))
    monkeypatch.setattr(service.pricing_engine, 'calculate_tariff_purchase_price', calculate)

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.lifecycle_state == 'conflict'
    assert result.terminal_reason == 'device_limit_decrease_not_allowed'
    assert user.balance_kopeks == 50_000
    calculate.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_fulfillment_reprices_before_debit_when_the_current_price_changes(monkeypatch):
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=4, quoted_price=10_000)
    user = SimpleNamespace(id=7, balance_kopeks=50_000, has_had_paid_subscription=False)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ScalarResult(user),
                ScalarResult(target),
                ScalarResult(SimpleNamespace(id=7, pricing_revision=1)),
            ]
        ),
        commit=AsyncMock(),
        add=AsyncMock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=checkout))
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda tariff, subscription: SimpleNamespace(
            eligible=True,
            period_options=(30,),
            device_options=(4, 5),
        ),
    )
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=12_000)),
    )

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.lifecycle_state == 'reprice_required'
    assert result.quote_state == 'price_changed'
    assert user.balance_kopeks == 50_000
    db.add.assert_not_called()

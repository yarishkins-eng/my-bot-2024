import json
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
        settlement_mode='legacy_deposit',
        quote_state='valid',
        terminal_reason=None,
    )


@pytest.mark.parametrize(
    ('raw_kopeks', 'quoted_kopeks'),
    [
        (30_049, 30_000),
        (30_050, 30_100),
        (30_099, 30_100),
    ],
)
def test_device_first_quote_is_rounded_server_side_to_the_nearest_whole_ruble(
    raw_kopeks: int,
    quoted_kopeks: int,
):
    assert service.round_device_first_quote_kopeks(raw_kopeks) == quoted_kopeks


@pytest.mark.parametrize(
    ('price_kopeks', 'balance_kopeks', 'top_up_kopeks'),
    [
        (30_100, 0, 30_100),
        (30_100, 30_100, 0),
        # The provider minimum is 100 ₽, so the user sees a payable top-up
        # instead of a 1 ₽ CTA which Platega would reject.
        (30_100, 30_050, 10_000),
    ],
)
def test_device_first_top_up_is_rounded_up_to_a_whole_ruble(
    price_kopeks: int,
    balance_kopeks: int,
    top_up_kopeks: int,
):
    assert (
        service.device_first_top_up_kopeks(
            price_kopeks=price_kopeks,
            balance_kopeks=balance_kopeks,
        )
        == top_up_kopeks
    )


def test_device_first_top_up_surplus_is_explicit_and_never_lost():
    assert (
        service.device_first_top_up_surplus_kopeks(
            price_kopeks=30_100,
            balance_kopeks=30_050,
        )
        == 9_950
    )


@pytest.mark.asyncio
async def test_purchase_options_quote_the_exact_kopeks_total_that_will_be_charged(monkeypatch):
    tariff = SimpleNamespace(id=7, name='Premium', traffic_limit_gb=100, device_limit=2, pricing_revision=1)
    user = SimpleNamespace(id=9, balance_kopeks=0)
    eligibility = SimpleNamespace(
        eligible=True,
        tariff=tariff,
        device_options=(2,),
        period_options=(30,),
        default_period_days=30,
    )
    db = SimpleNamespace()
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(service, 'is_device_first_canary_user', lambda _user: True)
    monkeypatch.setattr(service, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'resolve_single_eligible_tariff', lambda *_args, **_kwargs: eligibility)
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(
            return_value=SimpleNamespace(
                final_total=30_050,
                base_price=30_050,
                devices_price=0,
                promo_group_discount=0,
                promo_offer_discount=0,
            )
        ),
    )

    options = await service.build_purchase_options(db, user)
    price = options['price_matrix'][0]['prices'][0]

    assert price['price_kopeks'] == 30_050
    assert 'raw_total_kopeks' not in price['breakdown']
    assert 'rounding_adjustment_kopeks' not in price['breakdown']


@pytest.mark.asyncio
async def test_non_positive_direct_quote_stays_on_the_legacy_flow(monkeypatch):
    tariff = SimpleNamespace(id=7, name='Premium', traffic_limit_gb=100, device_limit=2, pricing_revision=1)
    user = SimpleNamespace(id=9, balance_kopeks=0)
    eligibility = SimpleNamespace(
        eligible=True,
        tariff=tariff,
        device_options=(2,),
        period_options=(30,),
        default_period_days=30,
    )
    db = SimpleNamespace()
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(service, 'is_device_first_canary_user', lambda _user: True)
    monkeypatch.setattr(service, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'resolve_single_eligible_tariff', lambda *_args, **_kwargs: eligibility)
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(
            return_value=SimpleNamespace(
                final_total=0,
                base_price=0,
                devices_price=0,
                promo_group_discount=0,
                promo_offer_discount=0,
            )
        ),
    )

    assert await service.build_purchase_options(db, user) == {
        'eligible': False,
        'reason': 'non_positive_quote',
    }


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


@pytest.mark.asyncio
async def test_exact_paid_checkout_does_not_expire_before_delayed_fulfillment():
    checkout = SimpleNamespace(
        lifecycle_state='armed',
        fulfillment_state='in_progress',
        quote_state='committed',
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: checkout)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock(), refresh=AsyncMock())

    returned = await service.get_owned_checkout(
        db,
        public_id='public-id',
        user_id=7,
        for_update=True,
    )

    assert returned is checkout
    assert checkout.lifecycle_state == 'armed'
    db.commit.assert_not_awaited()
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_paid_checkout_remains_resumable_after_the_general_timeout():
    checkout = SimpleNamespace(
        lifecycle_state='fulfilling',
        fulfillment_state='fulfilled',
        provisioning_state='pending',
        quote_state='committed',
        settlement_mode='direct_purchase_v2',
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: checkout)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())

    returned = await service.get_open_checkout_for_user(db, user_id=7)

    assert returned is checkout
    assert checkout.lifecycle_state == 'fulfilling'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_quote_does_not_bulk_expire_an_exact_paid_checkout_waiting_for_fulfillment(monkeypatch):
    user = SimpleNamespace(id=7, restriction_subscription=False)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(
        service,
        'build_purchase_options',
        AsyncMock(return_value={'eligible': False, 'reason': 'feature_disabled'}),
    )

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            source='cabinet',
        )

    assert raised.value.code == 'legacy_only'
    statement = str(db.execute.await_args.args[0].compile(compile_kwargs={'literal_binds': True}))
    assert (
        "NOT (subscription_checkouts.fulfillment_state = 'in_progress' "
        "AND subscription_checkouts.quote_state = 'committed')"
    ) in statement


def test_credited_armed_checkout_is_serialized_as_processing():
    checkout = SimpleNamespace(
        lifecycle_state='armed',
        fulfillment_state='in_progress',
        provisioning_state='not_started',
    )

    assert service.checkout_ui_state(checkout) == 'processing'


@pytest.mark.parametrize(
    ('target_snapshot', 'expected_trial_state'),
    [
        ({'is_trial': True, 'device_limit': 1}, True),
        ({'is_trial': False, 'device_limit': 1}, False),
        ({'device_limit': 1}, None),
        ({}, None),
    ],
)
def test_checkout_serialization_preserves_trial_state_without_guessing(target_snapshot, expected_trial_state):
    now = datetime.now(UTC)
    quote_expires_at = now + timedelta(minutes=30)
    expires_at = now + timedelta(hours=24)
    checkout = SimpleNamespace(
        public_id='checkout-1',
        tariff_id=7,
        target_subscription_id=12,
        period_days=30,
        selected_device_limit=3,
        price_breakdown={},
        quoted_price_kopeks=10_000,
        max_price_kopeks=10_000,
        settlement_mode='legacy_deposit',
        tariff_total_kopeks=10_000,
        wallet_applied_kopeks=0,
        external_payable_kopeks=0,
        funding_mode=None,
        quote_expires_at=quote_expires_at,
        expires_at=expires_at,
        lifecycle_state='confirmed',
        quote_state='valid',
        funding_state='funded',
        fulfillment_state='not_started',
        provisioning_state='not_started',
        terminal_reason=None,
        created_subscription_id=None,
        target_snapshot=target_snapshot,
        created_at=now,
        fulfilled_end_at=None,
    )

    snapshot = service.serialize_checkout(checkout)

    assert snapshot['current_subscription_is_trial'] is expected_trial_state
    assert snapshot['quote_expires_at'] == quote_expires_at.isoformat()
    assert snapshot['expires_at'] == expires_at.isoformat()
    # This is also the payload persisted to device_first_mutations.  A raw
    # datetime here would create the checkout but fail the HTTP response.
    assert json.loads(json.dumps(snapshot)) == snapshot


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
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
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
async def test_expired_quote_cannot_be_armed_or_debited(monkeypatch):
    checkout = SimpleNamespace(
        public_id='checkout-1',
        user_id=7,
        lifecycle_state='confirmed',
        fulfillment_state='not_started',
        armed_at=None,
        quote_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        quote_state='valid',
        terminal_reason=None,
        settlement_mode='legacy_deposit',
    )
    db = SimpleNamespace(commit=AsyncMock())
    fulfill = AsyncMock()
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(service, 'fulfill_checkout', fulfill)

    returned = await service.arm_checkout(db, checkout)

    assert returned is checkout
    assert checkout.lifecycle_state == 'reprice_required'
    assert checkout.quote_state == 'expired'
    assert checkout.terminal_reason == 'quote_expired'
    fulfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_exact_payment_keeps_its_quote_while_the_outbox_finishes(monkeypatch):
    checkout = SimpleNamespace(
        public_id='checkout-1',
        user_id=7,
        lifecycle_state='armed',
        fulfillment_state='in_progress',
        quote_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        quote_state='committed',
        terminal_reason=None,
    )
    db = SimpleNamespace(commit=AsyncMock())

    assert not await service.expire_checkout_quote_if_needed(db, checkout)
    assert checkout.lifecycle_state == 'armed'
    db.commit.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_fulfillment_records_one_completion_time_and_finishes_the_debit(monkeypatch):
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=4, quoted_price=10_000)
    user = SimpleNamespace(id=7, balance_kopeks=50_000, has_had_paid_subscription=False)
    tariff = SimpleNamespace(id=7, pricing_revision=1, traffic_limit_gb=100, allowed_squads=[])
    extended = SimpleNamespace(id=target.id, end_date=datetime.now(UTC) + timedelta(days=50))
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ScalarResult(user),
                ScalarResult(target),
                ScalarResult(tariff),
            ]
        ),
        commit=AsyncMock(),
        add=added.append,
        flush=AsyncMock(),
        refresh=AsyncMock(),
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
        AsyncMock(return_value=SimpleNamespace(final_total=10_000)),
    )
    monkeypatch.setattr(service, 'extend_subscription', AsyncMock(return_value=extended))

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result is checkout
    assert checkout.fulfillment_state == 'fulfilled'
    assert checkout.fulfilled_at is not None
    assert checkout.fulfilled_end_at == extended.end_date
    assert user.balance_kopeks == 40_000
    assert user.has_had_paid_subscription is True
    debit = added[0]
    assert debit.completed_at == checkout.fulfilled_at
    assert debit.amount_kopeks == -10_000
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_handcrafted_zero_price_checkout_never_debits_or_converts_a_trial(monkeypatch):
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=4, quoted_price=0)
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
        AsyncMock(return_value=SimpleNamespace(final_total=0)),
    )

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.lifecycle_state == 'conflict'
    assert result.terminal_reason == 'non_positive_quote'
    assert user.balance_kopeks == 50_000
    assert user.has_had_paid_subscription is False
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_spent_exact_payment_loses_its_expiry_exemption_and_requires_a_new_quote(monkeypatch):
    target = paid_target(device_limit=4)
    checkout = armed_checkout(target, devices=4, quoted_price=10_000)
    checkout.fulfillment_state = 'in_progress'
    checkout.quote_state = 'committed'
    checkout.quote_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    user = SimpleNamespace(id=7, balance_kopeks=0, has_had_paid_subscription=False)
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
        AsyncMock(return_value=SimpleNamespace(final_total=10_000)),
    )

    result = await service.fulfill_checkout(db, checkout.public_id, user.id)

    assert result.lifecycle_state == 'reprice_required'
    assert result.fulfillment_state == 'not_started'
    assert result.quote_state == 'expired'
    assert result.terminal_reason == 'quote_expired'
    assert user.has_had_paid_subscription is False

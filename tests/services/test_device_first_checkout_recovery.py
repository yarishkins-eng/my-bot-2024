from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.device_first_recovery_service as recovery_module
from app.services.device_first_checkout_service import (
    DIRECT_SETTLEMENT_MODE,
    DeviceFirstError,
    fulfill_direct_external_checkout,
    get_open_checkout_for_user,
    process_direct_provisioning_outbox,
    reconcile_armed_checkouts,
    settlement_mode,
)
from app.services.device_first_recovery_service import DeviceFirstRecoveryService


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.rows

    def scalar_one(self):
        return self.rows


class EmptyScalarResult:
    def scalars(self):
        return self

    def all(self):
        return []


class SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


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
        id=91,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        fulfillment_state='fulfilled',
        provisioning_state='pending',
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(checkout)),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )

    result = await get_open_checkout_for_user(db, user_id=7)

    assert result is checkout
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_operator_hold_remains_visible_after_quote_expiry():
    checkout = SimpleNamespace(
        id=92,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='operator_review',
        fulfillment_state='fulfilled',
        provisioning_state='operator_review',
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(checkout)),
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )

    result = await get_open_checkout_for_user(db, user_id=7)

    assert result is checkout
    assert checkout.lifecycle_state == 'operator_review'
    db.commit.assert_not_awaited()


def test_missing_settlement_mode_requires_operator_review():
    with pytest.raises(DeviceFirstError) as raised:
        settlement_mode(SimpleNamespace(settlement_mode=None))
    assert raised.value.code == 'operator_review_required'


@pytest.mark.asyncio
async def test_reversed_direct_attempt_cannot_fulfil_after_operator_review_wins_race():
    """A late success handler must not issue a subscription over an operator hold."""
    db = SimpleNamespace(scalar=AsyncMock(return_value=7), execute=AsyncMock(return_value=Result(None)))

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
    user = SimpleNamespace(id=7)
    checkout = SimpleNamespace(
        id=9,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='fulfilled',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=7),
        execute=AsyncMock(side_effect=[Result(user), Result(attempt), Result(checkout)]),
    )

    result = await fulfill_direct_external_checkout(
        db,
        checkout_id=9,
        provider_payment_id='provider-1',
        payment_attempt_id=41,
    )

    assert result is checkout
    assert db.execute.await_count == 3


@pytest.mark.asyncio
async def test_direct_fulfilment_uses_the_same_user_attempt_checkout_lock_order_as_reversal():
    """A post-paid reversal and its fulfilment cannot circularly wait on their fence rows."""
    user = SimpleNamespace(id=7)
    attempt = SimpleNamespace(id=41, status='paid_processing')
    checkout = SimpleNamespace(
        id=9,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='fulfilled',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=7),
        execute=AsyncMock(side_effect=[Result(user), Result(attempt), Result(checkout)]),
    )

    await fulfill_direct_external_checkout(
        db,
        checkout_id=9,
        provider_payment_id='provider-1',
        payment_attempt_id=41,
    )

    statements = [call.args[0] for call in db.execute.await_args_list]
    assert 'users' in str(statements[0])
    assert 'checkout_payment_attempts' in str(statements[1])
    assert 'subscription_checkouts' in str(statements[2])


@pytest.mark.asyncio
async def test_direct_fulfilment_kicks_only_its_own_post_commit_outbox_when_generic_auto_check_is_off(monkeypatch):
    """A verified direct callback never waits for the generic top-up checker."""
    user = SimpleNamespace(id=7)
    attempt = SimpleNamespace(id=41, status='paid_processing')
    checkout = SimpleNamespace(
        id=9,
        user_id=7,
        target_subscription_id=None,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='not_started',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=7),
        execute=AsyncMock(side_effect=[Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )
    completed = AsyncMock(return_value=checkout)
    kick = AsyncMock(return_value=1)
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.settings.PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED', False
    )

    with (
        patch('app.services.device_first_checkout_service._complete_direct_sale_locked', completed),
        patch('app.services.device_first_checkout_service.process_direct_provisioning_outbox', kick),
    ):
        result = await fulfill_direct_external_checkout(
            db,
            checkout_id=checkout.id,
            provider_payment_id='provider-1',
            payment_attempt_id=attempt.id,
        )

    assert result is checkout
    kick.assert_awaited_once_with(db, checkout_id=checkout.id, limit=1)
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_fulfilment_keeps_durable_sale_when_immediate_outbox_kick_fails():
    user = SimpleNamespace(id=7)
    attempt = SimpleNamespace(id=41, status='paid_processing')
    checkout = SimpleNamespace(
        id=9,
        user_id=7,
        target_subscription_id=None,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='not_started',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=7),
        execute=AsyncMock(side_effect=[Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        rollback=AsyncMock(),
    )

    with (
        patch(
            'app.services.device_first_checkout_service._complete_direct_sale_locked',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.services.device_first_checkout_service.process_direct_provisioning_outbox',
            AsyncMock(side_effect=RuntimeError('temporary-remnawave-error')),
        ),
    ):
        result = await fulfill_direct_external_checkout(
            db,
            checkout_id=checkout.id,
            provider_payment_id='provider-1',
            payment_attempt_id=attempt.id,
        )

    assert result is checkout
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_scoped_direct_provisioning_query_never_selects_another_checkout_or_legacy_row():
    db = SimpleNamespace(execute=AsyncMock(return_value=EmptyScalarResult()))

    await process_direct_provisioning_outbox(db, checkout_id=9, limit=1)

    statement = str(db.execute.await_args.args[0])
    assert 'device_first_outbox.checkout_id = :checkout_id_1' in statement
    assert 'device_first_outbox.settlement_mode = :settlement_mode_1' in statement
    assert 'direct_purchase_v2' not in statement  # bound value is never interpolated into SQL text


@pytest.mark.asyncio
async def test_direct_provisioning_finalization_uses_checkout_then_outbox_lock_order_against_reversal():
    """Regression: a post-paid reversal takes Checkout -> Outbox too, so no circular wait is possible."""
    claimed = SimpleNamespace(
        id=71,
        checkout_id=9,
        payload_json={'subscription_id': 13},
        status='pending',
        attempts=0,
        lease_token=None,
        lease_expires_at=None,
        lease_epoch=0,
    )
    checkout = SimpleNamespace(id=9, provisioning_state='pending', lifecycle_state='fulfilling')
    subscription = SimpleNamespace(id=13)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result([claimed]),  # claim without holding the lock during the remote call
                Result(checkout),  # final critical section begins: Checkout first
                Result(claimed),  # then fenced Outbox, matching reversal's order
                Result(None),
            ]
        ),
        get=AsyncMock(side_effect=[claimed, subscription]),
        commit=AsyncMock(),
        add=MagicMock(),
    )

    with patch(
        'app.services.subscription_service.SubscriptionService.ensure_subscription_synced',
        AsyncMock(return_value=(True, None)),
    ):
        processed = await process_direct_provisioning_outbox(db, checkout_id=checkout.id, limit=1)

    assert processed == 1
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    checkout_lock_index = next(
        index for index, statement in enumerate(statements) if 'subscription_checkouts' in statement
    )
    outbox_lock_index = next(
        index
        for index, statement in enumerate(statements[checkout_lock_index + 1 :], start=checkout_lock_index + 1)
        if 'device_first_outbox' in statement and 'lease_token' in statement
    )
    assert checkout_lock_index < outbox_lock_index


@pytest.mark.asyncio
async def test_direct_provisioning_stale_lease_releases_checkout_lock_before_returning_to_worker():
    """A reversal can win after RemnaWave I/O; its stale worker must not retain Checkout's lock."""
    claimed = SimpleNamespace(
        id=71,
        checkout_id=9,
        payload_json={'subscription_id': 13},
        status='pending',
        attempts=0,
        lease_token=None,
        lease_expires_at=None,
        lease_epoch=0,
    )
    checkout = SimpleNamespace(id=9)
    subscription = SimpleNamespace(id=13)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result([claimed]),
                Result(checkout),
                Result(None),  # reversal changed this row to operator_review
            ]
        ),
        get=AsyncMock(side_effect=[claimed, subscription]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with patch(
        'app.services.subscription_service.SubscriptionService.ensure_subscription_synced',
        AsyncMock(return_value=(True, None)),
    ):
        processed = await process_direct_provisioning_outbox(db, checkout_id=checkout.id, limit=1)

    assert processed == 0
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedicated_recovery_worker_is_direct_only_and_independent_from_generic_auto_check(monkeypatch):
    db = MagicMock()
    reconciler = AsyncMock(return_value=2)
    provisioner = AsyncMock(return_value=3)
    notifier = AsyncMock(return_value=1)
    monkeypatch.setattr(recovery_module, 'AsyncSessionLocal', lambda: SessionContext(db))
    monkeypatch.setattr(recovery_module, 'reconcile_device_first_payments', reconciler)
    monkeypatch.setattr(recovery_module, 'process_direct_provisioning_outbox', provisioner)
    monkeypatch.setattr(recovery_module, 'process_device_first_notification_outbox', notifier)
    monkeypatch.setattr(recovery_module.settings, 'PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED', False)

    result = await DeviceFirstRecoveryService().run_once(bot='bot')

    assert result == (2, 3, 1)
    reconciler.assert_awaited_once_with(db, limit=20, direct_only=True)
    provisioner.assert_awaited_once_with(db, limit=20)
    notifier.assert_awaited_once_with(db, bot='bot', limit=20)

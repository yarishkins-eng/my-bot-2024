from pathlib import Path

from sqlalchemy import inspect

from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceFirstDepositOutbox,
    DeviceFirstMutation,
    DeviceFirstOutbox,
    SubscriptionCheckout,
    Tariff,
    Transaction,
)


def test_model_contains_durable_checkout_and_ledger_constraints():
    checkout_indexes = {index.name for index in SubscriptionCheckout.__table__.indexes}
    attempt_indexes = {index.name for index in CheckoutPaymentAttempt.__table__.indexes}
    assert 'uq_subscription_checkouts_one_open_per_user' in checkout_indexes
    assert 'uq_checkout_attempt_one_active' in attempt_indexes
    assert 'ix_checkout_payment_attempts_next_reconcile_at' in attempt_indexes
    assert not CheckoutPaymentAttempt.__table__.c.reconcile_attempts.nullable
    assert not CheckoutPaymentAttempt.__table__.c.next_reconcile_at.nullable
    assert SubscriptionCheckout.__table__.c.fulfilled_end_at.nullable
    assert Tariff.__table__.c.device_purchase_options.nullable
    assert not Tariff.__table__.c.pricing_revision.nullable
    assert Transaction.__table__.c.device_first_ledger_key.unique
    assert inspect(DeviceFirstMutation).local_table.name == 'device_first_mutations'
    assert inspect(DeviceFirstOutbox).local_table.name == 'device_first_outbox'
    assert inspect(DeviceFirstDepositOutbox).local_table.name == 'device_first_deposit_outbox'
    assert DeviceFirstDepositOutbox.__table__.c.transaction_id.unique
    assert not DeviceFirstDepositOutbox.__table__.c.event_status.nullable
    assert not DeviceFirstDepositOutbox.__table__.c.referral_status.nullable
    assert not DeviceFirstDepositOutbox.__table__.c.fulfillment_status.nullable


def test_migration_is_additive_and_financial_downgrade_is_noop():
    path = Path(__file__).parents[2] / 'migrations/alembic/versions/0095_add_device_first_checkout.py'
    source = path.read_text()
    assert "revision: str = '0095'" in source
    assert "down_revision: Union[str, None] = '0094'" in source
    downgrade = source.split('def downgrade() -> None:', 1)[1]
    assert 'op.drop_' not in downgrade


def test_outbox_worker_reclaims_stale_processing_rows():
    path = Path(__file__).parents[2] / 'app/services/device_first_checkout_service.py'
    source = path.read_text()
    worker = source.split('async def process_provisioning_outbox', 1)[1]
    assert "DeviceFirstOutbox.status == 'processing'" in worker
    assert 'DeviceFirstOutbox.updated_at <= stale_before' in worker

    deposit_path = Path(__file__).parents[2] / 'app/services/device_first_deposit_outbox_service.py'
    deposit_worker = deposit_path.read_text().split('async def process_device_first_deposit_outbox', 1)[1]
    assert "DeviceFirstDepositOutbox.status == 'processing'" in deposit_worker
    assert 'DeviceFirstDepositOutbox.updated_at <= stale_before' in deposit_worker


def test_payment_reconciler_uses_due_time_and_bounded_backoff():
    path = Path(__file__).parents[2] / 'app/services/device_first_payment_service.py'
    source = path.read_text()
    worker = source.split('async def reconcile_device_first_payments', 1)[1]
    assert 'CheckoutPaymentAttempt.next_reconcile_at <=' in worker
    assert '.order_by(' in worker
    assert 'CheckoutPaymentAttempt.next_reconcile_at' in worker
    assert 'minutes=min(60, 2 ** min(attempt.reconcile_attempts, 6))' in worker


def test_financial_workers_preserve_lock_order_and_refresh_locked_users():
    payment_path = Path(__file__).parents[2] / 'app/services/device_first_payment_service.py'
    settlement = payment_path.read_text().split('async def settle_device_first_platega_payment', 1)[1]
    assert settlement.index('select(PlategaPayment)') < settlement.index('select(CheckoutPaymentAttempt)')

    deposit_path = Path(__file__).parents[2] / 'app/services/device_first_deposit_outbox_service.py'
    referral_step = deposit_path.read_text().split('async def _apply_referral_step', 1)[1]
    assert '.execution_options(populate_existing=True)' in referral_step

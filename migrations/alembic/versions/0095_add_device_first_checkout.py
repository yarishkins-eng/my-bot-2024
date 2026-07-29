"""add durable device-first checkout state

Revision ID: 0095
Revises: 0094
Create Date: 2026-07-29

The migration is additive.  Its downgrade is intentionally a no-op: production
checkout and ledger history must never be destroyed by an automated rollback.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0095'
down_revision: Union[str, None] = '0094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return _has_table(bind, table) and any(c['name'] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, 'tariffs', 'device_purchase_options'):
        op.add_column('tariffs', sa.Column('device_purchase_options', sa.JSON(), nullable=True))
    if not _has_column(bind, 'tariffs', 'pricing_revision'):
        op.add_column(
            'tariffs',
            sa.Column('pricing_revision', sa.Integer(), nullable=False, server_default='1'),
        )

    if not _has_table(bind, 'subscription_checkouts'):
        op.create_table(
            'subscription_checkouts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('public_id', sa.String(36), nullable=False, unique=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('source', sa.String(32), nullable=False, server_default='cabinet'),
            sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('target_subscription_id', sa.Integer(), nullable=True),
            sa.Column('expect_no_subscription', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('target_snapshot', sa.JSON(), nullable=False, server_default='{}'),
            sa.Column('period_days', sa.Integer(), nullable=False),
            sa.Column('selected_device_limit', sa.Integer(), nullable=False),
            sa.Column('price_breakdown', sa.JSON(), nullable=False, server_default='{}'),
            sa.Column('quoted_price_kopeks', sa.Integer(), nullable=False),
            sa.Column('max_price_kopeks', sa.Integer(), nullable=False),
            sa.Column('pricing_revision', sa.Integer(), nullable=False),
            sa.Column('quote_expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('lifecycle_state', sa.String(32), nullable=False, server_default='draft'),
            sa.Column('quote_state', sa.String(32), nullable=False, server_default='valid'),
            sa.Column('funding_state', sa.String(32), nullable=False, server_default='unfunded'),
            sa.Column('fulfillment_state', sa.String(32), nullable=False, server_default='not_started'),
            sa.Column('provisioning_state', sa.String(32), nullable=False, server_default='not_started'),
            sa.Column('terminal_reason', sa.String(255), nullable=True),
            sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('armed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('fulfilled_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('fulfilled_end_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='SET NULL')),
            sa.Column('debit_transaction_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint('period_days > 0', name='ck_subscription_checkout_period_positive'),
            sa.CheckConstraint('selected_device_limit > 0', name='ck_subscription_checkout_devices_positive'),
            sa.CheckConstraint('quoted_price_kopeks >= 0', name='ck_subscription_checkout_quote_nonnegative'),
            sa.CheckConstraint('max_price_kopeks >= quoted_price_kopeks', name='ck_subscription_checkout_max_quote'),
        )
        op.create_index('ix_subscription_checkouts_user_created', 'subscription_checkouts', ['user_id', 'created_at'])
        op.create_index(
            'ix_subscription_checkouts_lifecycle',
            'subscription_checkouts',
            ['lifecycle_state', 'expires_at'],
        )
        op.create_index(
            'uq_subscription_checkouts_one_open_per_user',
            'subscription_checkouts',
            ['user_id'],
            unique=True,
            postgresql_where=sa.text("lifecycle_state IN ('draft','confirmed','awaiting_funds','armed','fulfilling')"),
        )

    if not _has_column(bind, 'transactions', 'device_first_checkout_id'):
        op.add_column('transactions', sa.Column('device_first_checkout_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_transactions_device_first_checkout',
            'transactions',
            'subscription_checkouts',
            ['device_first_checkout_id'],
            ['id'],
            ondelete='SET NULL',
        )
        op.create_index('ix_transactions_device_first_checkout_id', 'transactions', ['device_first_checkout_id'])
    if not _has_column(bind, 'transactions', 'device_first_ledger_key'):
        op.add_column('transactions', sa.Column('device_first_ledger_key', sa.String(255), nullable=True))
        op.create_unique_constraint(
            'uq_transactions_device_first_ledger_key',
            'transactions',
            ['device_first_ledger_key'],
        )

    # Deferred because transactions gains its checkout FK above.
    if not any(
        fk.get('name') == 'fk_subscription_checkouts_debit_transaction'
        for fk in sa.inspect(bind).get_foreign_keys('subscription_checkouts')
    ):
        op.create_foreign_key(
            'fk_subscription_checkouts_debit_transaction',
            'subscription_checkouts',
            'transactions',
            ['debit_transaction_id'],
            ['id'],
            ondelete='SET NULL',
        )

    if not _has_table(bind, 'checkout_payment_attempts'):
        op.create_table(
            'checkout_payment_attempts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'checkout_id',
                sa.Integer(),
                sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('merchant_order_key', sa.String(96), nullable=False, unique=True),
            sa.Column('provider', sa.String(32), nullable=False, server_default='platega'),
            sa.Column('method_key', sa.String(32), nullable=False),
            sa.Column('provider_method_code', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False, server_default='RUB'),
            sa.Column('requested_amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('credited_amount_kopeks', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('status', sa.String(32), nullable=False, server_default='creating'),
            sa.Column('provider_payment_id', sa.String(255), nullable=True, unique=True),
            sa.Column(
                'platega_payment_id',
                sa.Integer(),
                sa.ForeignKey('platega_payments.id', ondelete='SET NULL'),
                nullable=True,
                unique=True,
            ),
            sa.Column('redirect_url', sa.Text(), nullable=True),
            sa.Column('reconciliation_reason', sa.Text(), nullable=True),
            sa.Column('reconcile_attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column(
                'next_reconcile_at',
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint('requested_amount_kopeks > 0', name='ck_checkout_attempt_requested_positive'),
            sa.CheckConstraint('credited_amount_kopeks >= 0', name='ck_checkout_attempt_credited_nonnegative'),
        )
        op.create_index(
            'uq_checkout_attempt_one_active',
            'checkout_payment_attempts',
            ['checkout_id'],
            unique=True,
            postgresql_where=sa.text("status IN ('creating','pending','paid_processing')"),
        )
        op.create_index(
            'ix_checkout_attempt_checkout_created',
            'checkout_payment_attempts',
            ['checkout_id', 'created_at'],
        )
        op.create_index(
            'ix_checkout_payment_attempts_next_reconcile_at',
            'checkout_payment_attempts',
            ['next_reconcile_at'],
        )

    if not _has_table(bind, 'device_first_mutations'):
        op.create_table(
            'device_first_mutations',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('owner_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column(
                'checkout_id',
                sa.Integer(),
                sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'),
                nullable=True,
            ),
            sa.Column('action', sa.String(48), nullable=False),
            sa.Column('idempotency_key', sa.String(128), nullable=False),
            sa.Column('request_hash', sa.String(64), nullable=False),
            sa.Column('response_json', sa.JSON(), nullable=True),
            sa.Column('status_code', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint(
                'owner_user_id',
                'action',
                'idempotency_key',
                name='uq_device_first_mutation_key',
            ),
        )

    if not _has_table(bind, 'device_first_outbox'):
        op.create_table(
            'device_first_outbox',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'checkout_id',
                sa.Integer(),
                sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('event_type', sa.String(48), nullable=False, server_default='sync_subscription'),
            sa.Column('payload_json', sa.JSON(), nullable=False, server_default='{}'),
            sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('checkout_id', 'event_type', name='uq_device_first_outbox_event'),
        )
        op.create_index('ix_device_first_outbox_pending', 'device_first_outbox', ['status', 'available_at'])

    if not _has_table(bind, 'device_first_deposit_outbox'):
        op.create_table(
            'device_first_deposit_outbox',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'transaction_id',
                sa.Integer(),
                sa.ForeignKey('transactions.id', ondelete='CASCADE'),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                'checkout_id',
                sa.Integer(),
                sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
            sa.Column('event_status', sa.String(24), nullable=False, server_default='pending'),
            sa.Column('referral_status', sa.String(24), nullable=False, server_default='pending'),
            sa.Column('fulfillment_status', sa.String(24), nullable=False, server_default='not_required'),
            sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('available_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            'ix_device_first_deposit_outbox_checkout_id',
            'device_first_deposit_outbox',
            ['checkout_id'],
        )
        op.create_index(
            'ix_device_first_deposit_outbox_pending',
            'device_first_deposit_outbox',
            ['status', 'available_at'],
        )


def downgrade() -> None:
    # Deliberately irreversible: checkout/payment/ledger history is financial
    # evidence. Roll back application code or disable the feature flag instead.
    pass

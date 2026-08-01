"""make device-first settlement an auditable direct sale

Revision ID: 0096
Revises: 0095
Create Date: 2026-08-01

The migration is deliberately additive.  Rows created by the original
device-first flow are permanently marked ``legacy_deposit`` before readers can
see the new schema.  New rows use ``direct_purchase_v2`` and never enter the
ordinary balance/top-up pipeline.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return _has_table(bind, table) and any(item['name'] == column for item in sa.inspect(bind).get_columns(table))


def _has_check(bind: sa.engine.Connection, table: str, name: str) -> bool:
    return any(item.get('name') == name for item in sa.inspect(bind).get_check_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()

    # Backfill happens while every mode column is nullable.  This is important:
    # a migration retry must never leave an older payment/worker row ambiguous.
    if not _has_column(bind, 'subscription_checkouts', 'settlement_mode'):
        op.add_column('subscription_checkouts', sa.Column('settlement_mode', sa.String(32), nullable=True))
    if not _has_column(bind, 'subscription_checkouts', 'tariff_total_kopeks'):
        op.add_column('subscription_checkouts', sa.Column('tariff_total_kopeks', sa.Integer(), nullable=False, server_default='0'))
    if not _has_column(bind, 'subscription_checkouts', 'wallet_applied_kopeks'):
        op.add_column('subscription_checkouts', sa.Column('wallet_applied_kopeks', sa.Integer(), nullable=False, server_default='0'))
    if not _has_column(bind, 'subscription_checkouts', 'external_payable_kopeks'):
        op.add_column('subscription_checkouts', sa.Column('external_payable_kopeks', sa.Integer(), nullable=False, server_default='0'))
    if not _has_column(bind, 'subscription_checkouts', 'funding_mode'):
        op.add_column('subscription_checkouts', sa.Column('funding_mode', sa.String(32), nullable=True))
    if not _has_column(bind, 'subscription_checkouts', 'sale_snapshot'):
        op.add_column('subscription_checkouts', sa.Column('sale_snapshot', sa.JSON(), nullable=False, server_default='{}'))
    if not _has_column(bind, 'subscription_checkouts', 'financial_committed_at'):
        op.add_column('subscription_checkouts', sa.Column('financial_committed_at', sa.DateTime(timezone=True), nullable=True))

    if not _has_column(bind, 'checkout_payment_attempts', 'settlement_mode'):
        op.add_column('checkout_payment_attempts', sa.Column('settlement_mode', sa.String(32), nullable=True))
    if not _has_column(bind, 'checkout_payment_attempts', 'provider_returned_amount_kopeks'):
        op.add_column('checkout_payment_attempts', sa.Column('provider_returned_amount_kopeks', sa.Integer(), nullable=True))
    if not _has_column(bind, 'checkout_payment_attempts', 'provider_returned_currency'):
        op.add_column('checkout_payment_attempts', sa.Column('provider_returned_currency', sa.String(3), nullable=True))
    if not _has_column(bind, 'checkout_payment_attempts', 'lease_token'):
        op.add_column('checkout_payment_attempts', sa.Column('lease_token', sa.String(64), nullable=True))
        op.create_index('ix_checkout_payment_attempts_lease_token', 'checkout_payment_attempts', ['lease_token'])
    if not _has_column(bind, 'checkout_payment_attempts', 'lease_expires_at'):
        op.add_column('checkout_payment_attempts', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index('ix_checkout_payment_attempts_lease_expires_at', 'checkout_payment_attempts', ['lease_expires_at'])
    if not _has_column(bind, 'checkout_payment_attempts', 'lease_epoch'):
        op.add_column('checkout_payment_attempts', sa.Column('lease_epoch', sa.Integer(), nullable=False, server_default='0'))

    if not _has_column(bind, 'device_first_outbox', 'settlement_mode'):
        op.add_column('device_first_outbox', sa.Column('settlement_mode', sa.String(32), nullable=True))
    if not _has_column(bind, 'device_first_outbox', 'lease_token'):
        op.add_column('device_first_outbox', sa.Column('lease_token', sa.String(64), nullable=True))
        op.create_index('ix_device_first_outbox_lease_token', 'device_first_outbox', ['lease_token'])
    if not _has_column(bind, 'device_first_outbox', 'lease_expires_at'):
        op.add_column('device_first_outbox', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index('ix_device_first_outbox_lease_expires_at', 'device_first_outbox', ['lease_expires_at'])
    if not _has_column(bind, 'device_first_outbox', 'lease_epoch'):
        op.add_column('device_first_outbox', sa.Column('lease_epoch', sa.Integer(), nullable=False, server_default='0'))

    if not _has_column(bind, 'device_first_deposit_outbox', 'settlement_mode'):
        op.add_column('device_first_deposit_outbox', sa.Column('settlement_mode', sa.String(32), nullable=True))

    bind.execute(sa.text("UPDATE subscription_checkouts SET settlement_mode = 'legacy_deposit' WHERE settlement_mode IS NULL"))
    bind.execute(sa.text("UPDATE checkout_payment_attempts SET settlement_mode = 'legacy_deposit' WHERE settlement_mode IS NULL"))
    bind.execute(sa.text("UPDATE device_first_outbox SET settlement_mode = 'legacy_deposit' WHERE settlement_mode IS NULL"))
    bind.execute(sa.text("UPDATE device_first_deposit_outbox SET settlement_mode = 'legacy_deposit' WHERE settlement_mode IS NULL"))

    for table in ('subscription_checkouts', 'checkout_payment_attempts', 'device_first_outbox', 'device_first_deposit_outbox'):
        op.alter_column(table, 'settlement_mode', nullable=False, server_default='legacy_deposit')
    for table, name in (
        ('subscription_checkouts', 'ck_subscription_checkout_settlement_mode'),
        ('checkout_payment_attempts', 'ck_checkout_attempt_settlement_mode'),
        ('device_first_outbox', 'ck_df_outbox_settlement_mode'),
        ('device_first_deposit_outbox', 'ck_df_deposit_outbox_settlement_mode'),
    ):
        if not _has_check(bind, table, name):
            op.create_check_constraint(name, table, "settlement_mode IN ('legacy_deposit', 'direct_purchase_v2')")

    if not _has_table(bind, 'device_first_reconciliation_credits'):
        op.create_table(
            'device_first_reconciliation_credits',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('checkout_id', sa.Integer(), sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('attempt_id', sa.Integer(), sa.ForeignKey('checkout_payment_attempts.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('provider_payment_id', sa.String(255), nullable=False),
            sa.Column('amount_kopeks', sa.Integer(), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('status', sa.String(32), nullable=False, server_default='operator_review'),
            sa.Column('resolution', sa.String(32), nullable=True),
            sa.Column('resolved_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('attempt_id', name='uq_df_reconciliation_credit_attempt'),
            sa.CheckConstraint('amount_kopeks > 0', name='ck_df_reconciliation_credit_positive'),
        )
        op.create_index('ix_df_reconciliation_credit_status', 'device_first_reconciliation_credits', ['status', 'created_at'])

    if not _has_table(bind, 'device_first_notification_outbox'):
        op.create_table(
            'device_first_notification_outbox',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('checkout_id', sa.Integer(), sa.ForeignKey('subscription_checkouts.id', ondelete='CASCADE'), nullable=False),
            sa.Column('notification_type', sa.String(48), nullable=False, server_default='ready'),
            sa.Column('status', sa.String(32), nullable=False, server_default='pending'),
            sa.Column('lease_token', sa.String(64), nullable=True, unique=True),
            sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('sending_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('checkout_id', 'notification_type', name='uq_df_notification_checkout_type'),
        )
        op.create_index('ix_df_notification_status', 'device_first_notification_outbox', ['status', 'created_at'])


def downgrade() -> None:
    # Financial evidence and an operator-review credit cannot be safely erased.
    pass

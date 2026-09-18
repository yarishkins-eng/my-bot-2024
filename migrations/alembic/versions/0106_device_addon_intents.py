"""Add durable manual device add-on intents and their top-up attempts.

Revision ID: 0106
Revises: 0105
"""

import sqlalchemy as sa
from alembic import op


revision = '0106'
down_revision = '0105'
branch_labels = None
depends_on = None


def _install_addon_referral_balance_fence() -> None:
    """Extend 0098 with one evidence-backed deferred reward exception."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION account_erasure_fence_user_balance()
        RETURNS trigger AS $$
        DECLARE
            addon_reward_claimed boolean := FALSE;
        BEGIN
            IF OLD.account_erasure_requested_at IS NOT NULL
               AND NEW.balance_kopeks > OLD.balance_kopeks
               AND OLD.account_erased_at IS NULL THEN
                WITH eligible AS (
                    SELECT addon.id
                      FROM device_addon_topup_attempts addon
                      JOIN transactions source
                        ON source.id = addon.deposit_transaction_id
                       AND source.user_id = addon.user_id
                       AND source.type = 'deposit'
                       AND source.is_completed IS TRUE
                      JOIN platega_payments payment
                        ON payment.id = addon.platega_payment_id
                       AND payment.is_paid IS TRUE
                       AND payment.transaction_id = source.id
                      JOIN transactions reward
                        ON reward.device_first_ledger_key = (
                            'deposit-side-effect:' || addon.deposit_transaction_id::text ||
                            ':' || 'referred-first-bonus'
                        )
                     WHERE addon.user_id = OLD.id
                       AND addon.status = 'paid'
                       AND addon.referral_status = 'pending'
                       AND addon.deposit_transaction_id IS NOT NULL
                       AND addon.credited_amount_kopeks = addon.requested_amount_kopeks
                       AND reward.user_id = OLD.id
                       AND reward.type = 'referral_reward'
                       AND reward.is_completed IS TRUE
                       AND reward.amount_kopeks = NEW.balance_kopeks - OLD.balance_kopeks
                     ORDER BY addon.id
                     FOR UPDATE OF addon
                     LIMIT 1
                ), claimed AS (
                    UPDATE device_addon_topup_attempts addon
                       SET referral_status = 'processing',
                           updated_at = NOW()
                      FROM eligible
                     WHERE addon.id = eligible.id
                       AND addon.referral_status = 'pending'
                    RETURNING addon.id
                )
                SELECT EXISTS (SELECT 1 FROM claimed) INTO addon_reward_claimed;
            END IF;
            IF OLD.account_erasure_requested_at IS NOT NULL
               AND NEW.balance_kopeks IS DISTINCT FROM OLD.balance_kopeks
               AND NOT (
                   current_setting('app.account_erasure_resolution', true) = 'on'
                   AND NEW.balance_kopeks = 0
                   AND NEW.balance_kopeks < OLD.balance_kopeks
               )
               AND NOT addon_reward_claimed THEN
                NEW.balance_kopeks := OLD.balance_kopeks;
                UPDATE account_erasure_requests
                   SET last_late_payment_blocked_at = NOW(),
                       updated_at = NOW(),
                       state = CASE WHEN state = 'completed' THEN state ELSE 'awaiting_manual_resolution' END,
                       resolution_code = CASE WHEN state = 'completed' THEN resolution_code ELSE 'late_legacy_payment_callback' END,
                       financial_resolution_at = CASE WHEN state = 'completed' THEN financial_resolution_at ELSE NULL END,
                       financial_resolved_by_user_id = CASE WHEN state = 'completed' THEN financial_resolved_by_user_id ELSE NULL END,
                       financial_resolution_code = CASE WHEN state = 'completed' THEN financial_resolution_code ELSE NULL END,
                       financial_resolution_note = CASE WHEN state = 'completed' THEN financial_resolution_note ELSE NULL END
                 WHERE user_id = OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _restore_0098_balance_fence() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION account_erasure_fence_user_balance()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.account_erasure_requested_at IS NOT NULL
               AND NEW.balance_kopeks IS DISTINCT FROM OLD.balance_kopeks
               AND NOT (
                   current_setting('app.account_erasure_resolution', true) = 'on'
                   AND NEW.balance_kopeks = 0
                   AND NEW.balance_kopeks < OLD.balance_kopeks
               ) THEN
                NEW.balance_kopeks := OLD.balance_kopeks;
                UPDATE account_erasure_requests
                   SET last_late_payment_blocked_at = NOW(),
                       updated_at = NOW(),
                       state = CASE WHEN state = 'completed' THEN state ELSE 'awaiting_manual_resolution' END,
                       resolution_code = CASE WHEN state = 'completed' THEN resolution_code ELSE 'late_legacy_payment_callback' END,
                       financial_resolution_at = CASE WHEN state = 'completed' THEN financial_resolution_at ELSE NULL END,
                       financial_resolved_by_user_id = CASE WHEN state = 'completed' THEN financial_resolved_by_user_id ELSE NULL END,
                       financial_resolution_code = CASE WHEN state = 'completed' THEN financial_resolution_code ELSE NULL END,
                       financial_resolution_note = CASE WHEN state = 'completed' THEN financial_resolution_note ELSE NULL END
                 WHERE user_id = OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '20s'")
    op.add_column(
        'users',
        sa.Column('device_addon_generation', sa.BigInteger(), nullable=False, server_default='0'),
    )

    op.create_table(
        'device_addon_intents',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=True),
        sa.Column('target_subscription_id', sa.Integer(), nullable=False),
        sa.Column('tariff_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('calculator_revision', sa.String(length=32), nullable=False, server_default='v1'),
        sa.Column('devices_to_add', sa.Integer(), nullable=False),
        sa.Column('original_device_limit', sa.Integer(), nullable=False),
        sa.Column('panel_uuid', sa.String(length=255), nullable=True),
        sa.Column('end_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('device_addon_generation', sa.BigInteger(), nullable=False),
        sa.Column('days_left', sa.Integer(), nullable=False),
        sa.Column('monthly_price_kopeks', sa.Integer(), nullable=False),
        sa.Column('base_price_kopeks', sa.Integer(), nullable=False),
        sa.Column('quoted_price_kopeks', sa.Integer(), nullable=False),
        sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('price_snapshot', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('purchase_state', sa.String(length=32), nullable=False, server_default='draft'),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('receipt_json', sa.JSON(), nullable=True),
        sa.Column('fulfillment_state', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('fulfillment_error_code', sa.String(length=96), nullable=True),
        sa.Column('fulfillment_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_epoch', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('purchased_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fulfilled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('devices_to_add > 0', name='ck_device_addon_intent_devices_positive'),
        sa.CheckConstraint('original_device_limit > 0', name='ck_device_addon_intent_original_limit_positive'),
        sa.CheckConstraint('quoted_price_kopeks >= 0', name='ck_device_addon_intent_quote_nonnegative'),
        sa.CheckConstraint('base_price_kopeks >= 0', name='ck_device_addon_intent_base_nonnegative'),
        sa.CheckConstraint('monthly_price_kopeks >= 0', name='ck_device_addon_intent_monthly_nonnegative'),
        sa.CheckConstraint('discount_percent >= 0 AND discount_percent <= 100', name='ck_device_addon_intent_discount'),
        sa.CheckConstraint("purchase_state IN ('draft', 'purchased')", name='ck_device_addon_intent_purchase_state'),
        sa.CheckConstraint(
            "fulfillment_state IN ('pending', 'ready', 'needs_attention')",
            name='ck_device_addon_intent_fulfillment_state',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_device_addon_intent_user_key'),
        sa.UniqueConstraint('transaction_id'),
    )
    op.create_index('ix_device_addon_intents_public_id', 'device_addon_intents', ['public_id'], unique=True)
    op.create_index('ix_device_addon_intents_user_id', 'device_addon_intents', ['user_id'])
    op.create_index('ix_device_addon_intents_subscription_id', 'device_addon_intents', ['subscription_id'])
    op.create_index(
        'ix_device_addon_intents_target_subscription_id', 'device_addon_intents', ['target_subscription_id']
    )
    op.create_index('ix_device_addon_intents_tariff_id', 'device_addon_intents', ['tariff_id'])
    op.create_index('ix_device_addon_intent_user_created', 'device_addon_intents', ['user_id', 'created_at'])
    op.create_index(
        'ix_device_addon_intent_fulfillment', 'device_addon_intents', ['fulfillment_state', 'next_attempt_at']
    )
    op.create_index('ix_device_addon_intents_lease_token', 'device_addon_intents', ['lease_token'])
    op.create_index('ix_device_addon_intents_lease_expires_at', 'device_addon_intents', ['lease_expires_at'])

    op.create_table(
        'device_addon_topup_attempts',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('public_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('intent_id', sa.BigInteger(), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('payment_method', sa.String(length=32), nullable=False, server_default='platega'),
        sa.Column('method_key', sa.String(length=32), nullable=False),
        sa.Column('provider_method_code', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='RUB'),
        sa.Column('expected_amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('requested_amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('credited_amount_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='prepared'),
        sa.Column('holds_invoice_slot', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('platega_payment_id', sa.Integer(), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=255), nullable=True),
        sa.Column('correlation_id', sa.String(length=64), nullable=False),
        sa.Column('payment_url', sa.Text(), nullable=True),
        sa.Column('reconciliation_reason', sa.Text(), nullable=True),
        sa.Column('provider_returned_amount_kopeks', sa.Integer(), nullable=True),
        sa.Column('provider_returned_currency', sa.String(length=3), nullable=True),
        sa.Column('deposit_transaction_id', sa.Integer(), nullable=True),
        sa.Column('referral_status', sa.String(length=24), nullable=False, server_default='pending'),
        sa.Column('event_status', sa.String(length=24), nullable=False, server_default='pending'),
        sa.Column('referral_enabled_at_credit', sa.Boolean(), nullable=True),
        sa.Column('effects_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('reconcile_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_reconcile_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('lease_token', sa.String(length=64), nullable=True),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('lease_epoch', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('requested_amount_kopeks > 0', name='ck_device_addon_attempt_requested_positive'),
        sa.CheckConstraint('expected_amount_kopeks >= 0', name='ck_device_addon_attempt_expected_nonnegative'),
        sa.CheckConstraint('credited_amount_kopeks >= 0', name='ck_device_addon_attempt_credited_nonnegative'),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'creation_unknown', 'pending', 'reconciling', 'paid', 'terminal', 'operator_review')",
            name='ck_device_addon_attempt_status',
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['intent_id'], ['device_addon_intents.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['platega_payment_id'], ['platega_payments.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['deposit_transaction_id'], ['transactions.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('intent_id', 'idempotency_key', name='uq_device_addon_attempt_intent_key'),
        sa.UniqueConstraint('platega_payment_id'),
        sa.UniqueConstraint('provider_payment_id'),
        sa.UniqueConstraint('correlation_id'),
        sa.UniqueConstraint('deposit_transaction_id'),
    )
    op.create_index(
        'ix_device_addon_topup_attempts_public_id', 'device_addon_topup_attempts', ['public_id'], unique=True
    )
    op.create_index('ix_device_addon_topup_attempts_user_id', 'device_addon_topup_attempts', ['user_id'])
    op.create_index('ix_device_addon_topup_attempts_intent_id', 'device_addon_topup_attempts', ['intent_id'])
    op.create_index(
        'ix_device_addon_attempt_intent_created', 'device_addon_topup_attempts', ['intent_id', 'created_at']
    )
    op.create_index('ix_device_addon_attempt_recovery', 'device_addon_topup_attempts', ['status', 'next_reconcile_at'])
    op.create_index('ix_device_addon_topup_attempts_lease_token', 'device_addon_topup_attempts', ['lease_token'])
    op.create_index(
        'ix_device_addon_topup_attempts_lease_expires_at', 'device_addon_topup_attempts', ['lease_expires_at']
    )
    op.create_index(
        'uq_device_addon_attempt_one_active',
        'device_addon_topup_attempts',
        ['intent_id'],
        unique=True,
        postgresql_where=sa.text('holds_invoice_slot'),
    )
    _install_addon_referral_balance_fence()

    # 0105 dynamically guarded the then-existing financial relations.  These
    # tables arrive later, so attach the same fail-closed reset guard explicitly.
    for relation in ('device_addon_intents', 'device_addon_topup_attempts'):
        op.execute(
            f'CREATE TRIGGER teplo_test_reset_guard BEFORE INSERT OR UPDATE OR DELETE ON {relation} '
            'FOR EACH ROW EXECUTE FUNCTION teplo_test_reset_write_guard()'
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '20s'")
    bind = op.get_bind()
    has_rows = bind.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM device_addon_intents LIMIT 1) '
            'OR EXISTS (SELECT 1 FROM device_addon_topup_attempts LIMIT 1)'
        )
    ).scalar()
    if has_rows:
        raise RuntimeError('Unsafe 0106 downgrade refused: device add-on financial history exists.')
    _restore_0098_balance_fence()
    op.drop_index('uq_device_addon_attempt_one_active', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_attempt_recovery', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_attempt_intent_created', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_topup_attempts_lease_expires_at', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_topup_attempts_lease_token', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_topup_attempts_intent_id', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_topup_attempts_user_id', table_name='device_addon_topup_attempts')
    op.drop_index('ix_device_addon_topup_attempts_public_id', table_name='device_addon_topup_attempts')
    op.drop_table('device_addon_topup_attempts')
    op.drop_index('ix_device_addon_intent_fulfillment', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intent_user_created', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_lease_expires_at', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_lease_token', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_tariff_id', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_subscription_id', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_target_subscription_id', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_user_id', table_name='device_addon_intents')
    op.drop_index('ix_device_addon_intents_public_id', table_name='device_addon_intents')
    op.drop_table('device_addon_intents')
    op.drop_column('users', 'device_addon_generation')

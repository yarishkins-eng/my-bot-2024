"""add an auditable financial account-erasure lifecycle

Revision ID: 0098
Revises: 0097
Create Date: 2026-08-02

The migration is additive and deliberately irreversible.  It does not delete
or rewrite payment evidence; it only records a privacy/account-closure state
that lets an old user row remain a safe foreign-key anchor.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0098'
down_revision: Union[str, None] = '0097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return any(item['name'] == column for item in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, 'users', 'account_erasure_requested_at'):
        op.add_column('users', sa.Column('account_erasure_requested_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index('ix_users_account_erasure_requested_at', 'users', ['account_erasure_requested_at'])
    if not _has_column(bind, 'users', 'account_erased_at'):
        op.add_column('users', sa.Column('account_erased_at', sa.DateTime(timezone=True), nullable=True))
        op.create_index('ix_users_account_erased_at', 'users', ['account_erased_at'])

    if sa.inspect(bind).has_table('account_erasure_requests'):
        if not _has_column(bind, 'account_erasure_requests', 'has_legacy_financial_history'):
            op.add_column(
                'account_erasure_requests',
                sa.Column(
                    'has_legacy_financial_history',
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text('false'),
                ),
            )
    else:
        op.create_table(
            'account_erasure_requests',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('requested_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('state', sa.String(48), nullable=False, server_default='awaiting_reconciliation'),
            sa.Column('panel_state', sa.String(32), nullable=False, server_default='not_required'),
            sa.Column('has_legacy_financial_history', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('panel_cleanup_uuids', sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column('resolution_code', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('user_id', name='uq_account_erasure_request_user'),
        )
        op.create_index('ix_account_erasure_requests_state_created', 'account_erasure_requests', ['state', 'created_at'])

    request_columns = {
        'financial_resolution_at': sa.Column('financial_resolution_at', sa.DateTime(timezone=True), nullable=True),
        'financial_resolved_by_user_id': sa.Column(
            'financial_resolved_by_user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        'financial_resolution_code': sa.Column('financial_resolution_code', sa.String(64), nullable=True),
        'financial_resolution_note': sa.Column('financial_resolution_note', sa.Text(), nullable=True),
        'last_late_payment_blocked_at': sa.Column('last_late_payment_blocked_at', sa.DateTime(timezone=True), nullable=True),
    }
    for column_name, column in request_columns.items():
        if not _has_column(bind, 'account_erasure_requests', column_name):
            op.add_column('account_erasure_requests', column)
    if not _has_column(bind, 'account_erasure_requests', 'panel_cleanup_uuids'):
        op.add_column(
            'account_erasure_requests',
            sa.Column('panel_cleanup_uuids', sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )

    # Last-line safety rails. Application handlers record provider payloads
    # and then this trigger prevents a late legacy callback from changing a
    # closing/tombstoned user's wallet or reactivating VPN access.
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
    op.execute('DROP TRIGGER IF EXISTS trg_account_erasure_fence_user_balance ON users')
    op.execute(
        """
        CREATE TRIGGER trg_account_erasure_fence_user_balance
        BEFORE UPDATE OF balance_kopeks ON users
        FOR EACH ROW EXECUTE FUNCTION account_erasure_fence_user_balance();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION account_erasure_fence_subscription_access()
        RETURNS trigger AS $$
        DECLARE closing_account boolean;
        BEGIN
            SELECT account_erasure_requested_at IS NOT NULL INTO closing_account FROM users WHERE id = NEW.user_id;
            IF closing_account AND NEW.status IN ('active', 'trial', 'limited') THEN
                NEW.status := 'disabled';
                NEW.autopay_enabled := FALSE;
                UPDATE account_erasure_requests
                   SET last_late_payment_blocked_at = NOW(),
                       updated_at = NOW(),
                       state = CASE WHEN state = 'completed' THEN state ELSE 'awaiting_manual_resolution' END,
                       resolution_code = CASE WHEN state = 'completed' THEN resolution_code ELSE 'late_legacy_payment_callback' END,
                       financial_resolution_at = CASE WHEN state = 'completed' THEN financial_resolution_at ELSE NULL END,
                       financial_resolved_by_user_id = CASE WHEN state = 'completed' THEN financial_resolved_by_user_id ELSE NULL END,
                       financial_resolution_code = CASE WHEN state = 'completed' THEN financial_resolution_code ELSE NULL END,
                       financial_resolution_note = CASE WHEN state = 'completed' THEN financial_resolution_note ELSE NULL END
                 WHERE user_id = NEW.user_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute('DROP TRIGGER IF EXISTS trg_account_erasure_fence_subscription_access ON subscriptions')
    op.execute(
        """
        CREATE TRIGGER trg_account_erasure_fence_subscription_access
        BEFORE INSERT OR UPDATE OF status, user_id ON subscriptions
        FOR EACH ROW EXECUTE FUNCTION account_erasure_fence_subscription_access();
        """
    )


def downgrade() -> None:
    # Financial/account-erasure evidence is intentionally retained.
    pass

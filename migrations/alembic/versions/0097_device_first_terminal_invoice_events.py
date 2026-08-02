"""record terminal direct-payment evidence before releasing a checkout

Revision ID: 0097
Revises: 0096
Create Date: 2026-08-02

The table is additive and append-only.  Existing checkout/attempt rows retain
their original state and no historical provider payload, URL or secret is
copied into the new evidence log.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    attempt_columns = {column['name'] for column in sa.inspect(bind).get_columns('checkout_payment_attempts')}
    if 'terminal_observations' not in attempt_columns:
        op.add_column(
            'checkout_payment_attempts',
            sa.Column('terminal_observations', sa.Integer(), nullable=False, server_default='0'),
        )
        op.alter_column('checkout_payment_attempts', 'terminal_observations', server_default=None)

    # Revision 0095 used CASCADE for attempts. A checkout with financial
    # evidence must not be deletable, otherwise both the provider link and its
    # terminal history disappear. PostgreSQL gives implicit FKs stable names;
    # keep the branch dialect-specific so local SQLite schema tests remain
    # read-only and production receives the actual retention policy.
    if bind.dialect.name == 'postgresql':
        for fk in sa.inspect(bind).get_foreign_keys('checkout_payment_attempts'):
            if (
                fk.get('referred_table') == 'subscription_checkouts'
                and fk.get('constrained_columns') == ['checkout_id']
                and (fk.get('options') or {}).get('ondelete', '').upper() != 'RESTRICT'
            ):
                op.drop_constraint(fk['name'], 'checkout_payment_attempts', type_='foreignkey')
                op.create_foreign_key(
                    'fk_checkout_attempt_checkout_restrict',
                    'checkout_payment_attempts',
                    'subscription_checkouts',
                    ['checkout_id'],
                    ['id'],
                    ondelete='RESTRICT',
                )
                break
    if sa.inspect(bind).has_table('device_first_provider_events'):
        return

    op.create_table(
        'device_first_provider_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('checkout_id', sa.Integer(), sa.ForeignKey('subscription_checkouts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('attempt_id', sa.Integer(), sa.ForeignKey('checkout_payment_attempts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('provider_payment_id', sa.String(255), nullable=False),
        sa.Column('provider_status', sa.String(32), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        # A signed callback can legitimately be sparse. It remains audit
        # evidence, while only a later canonical response with an exact
        # amount/method/identity can move financial state.
        sa.Column('amount_kopeks', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('method_key', sa.String(32), nullable=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('authenticated', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('attempt_id', 'source', 'provider_status', 'payload_hash', name='uq_df_provider_event_evidence'),
        sa.CheckConstraint('amount_kopeks IS NULL OR amount_kopeks > 0', name='ck_df_provider_event_amount_positive'),
    )
    op.create_index('ix_df_provider_event_attempt_observed', 'device_first_provider_events', ['attempt_id', 'observed_at'])
    op.create_index('ix_df_provider_event_provider_status', 'device_first_provider_events', ['provider_payment_id', 'provider_status'])


def downgrade() -> None:
    # Provider evidence is financial audit history and is intentionally kept.
    pass

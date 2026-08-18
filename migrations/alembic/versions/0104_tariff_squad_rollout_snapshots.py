"""add durable pre-image table for tariff squad rollouts

Revision ID: 0104
Revises: 0103
Create Date: 2026-08-19

A tariff squad rollout rewrites ``Subscription.connected_squads`` for every
issued subscription of that tariff and pushes the new set to RemnaWave.  The
existing propagation kept its "how it was" pre-image in a process-local dict,
so an interrupted run left the Panel changed, the database not, and nothing to
roll back from.

This table is the durable pre-image: one row per subscription per rollout,
written and committed before its batch touches the Panel.

Deliberately additive.  It converts no tariff, alters no subscription and calls
no RemnaWave endpoint.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0104'
down_revision: Union[str, None] = '0103'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tariff_squad_rollout_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rollout_id', sa.String(length=64), nullable=False),
        sa.Column('tariff_id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('previous_squads', sa.JSON(), nullable=False),
        sa.Column('previous_subscription_url', sa.Text(), nullable=True),
        sa.Column('applied_squads', sa.JSON(), nullable=False),
        sa.Column('batch_no', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('restored_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['tariff_id'], ['tariffs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subscription_id'], ['subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('rollout_id', 'subscription_id', name='uq_tariff_rollout_subscription'),
    )
    op.create_index('ix_tariff_rollout_lookup', 'tariff_squad_rollout_snapshots', ['tariff_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_tariff_rollout_lookup', table_name='tariff_squad_rollout_snapshots')
    op.drop_table('tariff_squad_rollout_snapshots')

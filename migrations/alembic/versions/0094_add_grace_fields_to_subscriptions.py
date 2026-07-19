"""add grace fields to subscriptions (бонус 2 дня после конца)

Adds the columns that back the post-expiry "бонус 2 дня" (internally: grace)
feature — see ЗАДАЧИ-grace-и-доступ-без-VPN.md:

  • in_grace                    — bool, NOT NULL, default False. True while the
                                  subscription is formally EXPIRED but the VPN is
                                  kept alive (panel expireAt shifted forward).
  • grace_until                 — timestamptz, nullable. The real end of the grace
                                  window (end_date + GRACE_PERIOD_DAYS).
  • grace_eligible_period_days  — int, nullable. The paid period (days) of the last
                                  purchase/renewal; grace requires >= GRACE_MIN_PERIOD_DAYS.

Idempotent and column-existence-guarded (same defensive style as 0093) so a
re-run on a partially-migrated production DB cannot fail and crash bot startup.

Revision ID: 0094
Revises: 0093
Create Date: 2026-06-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLE = 'subscriptions'


def _has_column(bind: sa.engine.Connection, column: str) -> bool:
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return False
    return any(col['name'] == column for col in inspector.get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return

    if not _has_column(bind, 'in_grace'):
        op.add_column(
            _TABLE,
            sa.Column('in_grace', sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _has_column(bind, 'grace_until'):
        op.add_column(
            _TABLE,
            sa.Column('grace_until', sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_column(bind, 'grace_eligible_period_days'):
        op.add_column(
            _TABLE,
            sa.Column('grace_eligible_period_days', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, 'grace_eligible_period_days'):
        op.drop_column(_TABLE, 'grace_eligible_period_days')
    if _has_column(bind, 'grace_until'):
        op.drop_column(_TABLE, 'grace_until')
    if _has_column(bind, 'in_grace'):
        op.drop_column(_TABLE, 'in_grace')

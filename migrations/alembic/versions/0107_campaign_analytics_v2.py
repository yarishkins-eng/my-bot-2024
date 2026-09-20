"""Add campaign spend.

Revision ID: 0107
Revises: 0106
"""

import sqlalchemy as sa
from alembic import op


revision = '0107'
down_revision = '0106'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('advertising_campaigns', sa.Column('ad_spend_kopeks', sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        'ck_advertising_campaigns_ad_spend_nonnegative',
        'advertising_campaigns',
        'ad_spend_kopeks IS NULL OR ad_spend_kopeks >= 0',
    )


def downgrade() -> None:
    has_recorded_spend = (
        op.get_bind()
        .execute(sa.text('SELECT EXISTS (SELECT 1 FROM advertising_campaigns WHERE ad_spend_kopeks IS NOT NULL)'))
        .scalar()
    )
    if has_recorded_spend:
        raise RuntimeError(
            'Unsafe 0107 downgrade refused: advertising campaign spend has been recorded. '
            'Preserve the column and roll back application code instead.'
        )

    op.drop_constraint(
        'ck_advertising_campaigns_ad_spend_nonnegative',
        'advertising_campaigns',
        type_='check',
    )
    op.drop_column('advertising_campaigns', 'ad_spend_kopeks')

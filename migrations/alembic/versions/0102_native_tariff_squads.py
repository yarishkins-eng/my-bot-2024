"""add native Internal Squad tariff mode

Revision ID: 0102
Revises: 0101
Create Date: 2026-08-10

``native_squads`` restores the upstream Bedolaga tariff contract:
``Tariff.allowed_squads`` is the current offer and an issued subscription
keeps its own ``connected_squads`` until an explicit propagation changes it.

This is deliberately additive.  It does not convert a tariff, alter a
subscription, remove PublicLocation/AccessPoint evidence, or call RemnaWave.
Those are controlled application operations performed only after a preflight.
"""

from typing import Sequence, Union

from alembic import op


revision: str = '0102'
down_revision: Union[str, None] = '0101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NATIVE_MODE = 'native_squads'
_PREVIOUS_MODES = "'legacy_snapshot', 'location_managed', 'no_locations', 'access_point_managed'"


def upgrade() -> None:
    op.drop_constraint('ck_tariffs_entitlement_mode', 'tariffs', type_='check')
    op.create_check_constraint(
        'ck_tariffs_entitlement_mode',
        'tariffs',
        f"entitlement_mode IN ({_PREVIOUS_MODES}, '{_NATIVE_MODE}')",
    )


def downgrade() -> None:
    """Entitlement modes are forward-only; production never downgrades this schema."""

    raise RuntimeError(
        '0102 is forward-only. Keep the schema and deploy a compatible code rollback instead of Alembic downgrade.'
    )

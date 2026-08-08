"""add fail-closed public-location entitlement domain

Revision ID: 0100
Revises: 0099
Create Date: 2026-08-08

This is intentionally additive.  It does not inspect or change legacy
``allowed_squads`` values, makes no network call, and cannot activate a tariff
or execute a panel plan.  Legacy migration requires a separately approved
manifest outside Alembic.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0100'
down_revision: Union[str, None] = '0099'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tariffs', sa.Column('entitlement_mode', sa.String(32), nullable=False, server_default='legacy_snapshot'))
    op.add_column('tariffs', sa.Column('location_policy_revision', sa.Integer(), nullable=False, server_default='1'))
    op.create_check_constraint(
        'ck_tariffs_entitlement_mode', 'tariffs',
        "entitlement_mode IN ('legacy_snapshot', 'location_managed', 'no_locations')",
    )
    op.add_column('subscriptions', sa.Column('entitlement_snapshot', sa.JSON(), nullable=True))
    op.add_column('subscriptions', sa.Column('entitlement_provenance', sa.String(32), nullable=True))
    op.add_column('subscriptions', sa.Column('entitlement_policy_revision', sa.Integer(), nullable=True))

    op.create_table(
        'public_locations',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('iso_code', sa.String(3), nullable=False, unique=True),
        sa.Column('label_ru', sa.String(120), nullable=False),
        sa.Column('label_en', sa.String(120), nullable=False),
        sa.Column('flag', sa.String(16)),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('lifecycle', sa.String(16), nullable=False, server_default='draft'),
        sa.Column('visibility', sa.String(16), nullable=False, server_default='hidden'),
        sa.Column('health', sa.String(16), nullable=False, server_default='unknown'),
        sa.Column('tariff_assignable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("lifecycle IN ('draft', 'ready', 'published', 'deprecated', 'retired')", name='ck_public_locations_lifecycle'),
        sa.CheckConstraint("visibility IN ('hidden', 'visible')", name='ck_public_locations_visibility'),
        sa.CheckConstraint("health IN ('unknown', 'healthy', 'unhealthy')", name='ck_public_locations_health'),
    )
    op.create_table(
        'public_location_squad_mappings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('public_location_id', sa.String(36), sa.ForeignKey('public_locations.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('internal_squad_uuid', sa.String(255), nullable=False, unique=True),
        sa.Column('is_dedicated_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('inventory_revision', sa.String(128)),
        sa.Column('inventory_membership_hash', sa.String(128)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('public_location_id', 'internal_squad_uuid', name='uq_public_location_squad'),
    )
    op.create_table(
        'tariff_location_entitlements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('public_location_id', sa.String(36), sa.ForeignKey('public_locations.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('tariff_id', 'public_location_id', name='uq_tariff_public_location'),
    )
    op.create_index('ix_tariff_location_entitlements_tariff_id', 'tariff_location_entitlements', ['tariff_id'])
    op.create_table(
        'tariff_legacy_entitlement_manifests',
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('squad_uuids', sa.JSON(), nullable=False),
        sa.Column('membership_hashes', sa.JSON(), nullable=False),
        sa.Column('presentation_locations', sa.JSON(), nullable=False),
        sa.Column('manifest_hash', sa.String(128), nullable=False, unique=True),
        # Production approval is performed in the protected GitHub Environment.
        # It is not safe to guess which of several bot superadmin records was
        # the owner, so the manifest stores the verified deployment actor and
        # reference.  A future cabinet approval can additionally link a user.
        sa.Column('approved_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('approved_by_actor', sa.String(255), nullable=True),
        sa.Column('approval_permission', sa.String(64), nullable=False),
        sa.Column('approval_reference', sa.String(255), nullable=False),
        sa.Column('approval_reason', sa.Text(), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint(
            'approved_by_user_id IS NOT NULL OR approved_by_actor IS NOT NULL',
            name='ck_tariff_legacy_manifest_has_actor',
        ),
    )
    op.create_table(
        'subscription_entitlement_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT')),
        sa.Column('location_ids', sa.JSON(), nullable=False),
        sa.Column('technical_squad_uuids', sa.JSON(), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('provenance', sa.String(32), nullable=False),
        # Equal entitlements are expected for many subscriptions.  The
        # subscription_id uniqueness above, not this content hash, makes the
        # immutable snapshot one-per-subscription.
        sa.Column('snapshot_hash', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'entitlement_change_plans',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('state', sa.String(32), nullable=False, server_default='previewed'),
        sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False), sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('manifest_hash', sa.String(128), nullable=False), sa.Column('plan_hash', sa.String(128), nullable=False, unique=True),
        sa.Column('policy_revision', sa.Integer(), nullable=False), sa.Column('scope', sa.JSON(), nullable=False), sa.Column('preimage', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')), sa.Column('confirmed_at', sa.DateTime(timezone=True)),
    )
    op.create_table(
        'entitlement_plan_approvals',
        sa.Column('id', sa.Integer(), primary_key=True), sa.Column('plan_id', sa.String(36), sa.ForeignKey('entitlement_change_plans.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False), sa.Column('permission', sa.String(64), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False), sa.Column('immutable_hash', sa.String(128), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'entitlement_plan_jobs',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('plan_id', sa.String(36), sa.ForeignKey('entitlement_change_plans.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('panel_identity', sa.String(255), nullable=False), sa.Column('state', sa.String(32), nullable=False, server_default='pending'),
        sa.Column('fencing_token', sa.Integer(), nullable=False, server_default='1'), sa.Column('target_squads', sa.JSON(), nullable=False),
        sa.Column('preimage', sa.JSON(), nullable=False), sa.Column('error', sa.Text()), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_entitlement_plan_jobs_plan_id', 'entitlement_plan_jobs', ['plan_id'])
    op.create_table(
        'entitlement_plan_outbox', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('entitlement_plan_jobs.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('state', sa.String(32), nullable=False, server_default='disabled'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'entitlement_plan_checkpoints', sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('entitlement_plan_jobs.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('stage', sa.String(32), nullable=False), sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_entitlement_plan_checkpoints_job_id', 'entitlement_plan_checkpoints', ['job_id'])


def downgrade() -> None:
    # Production rollback is application compatibility, never Alembic downgrade.
    # The down path exists solely for disposable local test databases.
    for table in ('entitlement_plan_checkpoints', 'entitlement_plan_outbox', 'entitlement_plan_jobs', 'entitlement_plan_approvals', 'entitlement_change_plans', 'subscription_entitlement_snapshots', 'tariff_legacy_entitlement_manifests', 'tariff_location_entitlements', 'public_location_squad_mappings', 'public_locations'):
        op.drop_table(table)
    op.drop_constraint('ck_tariffs_entitlement_mode', 'tariffs', type_='check')
    op.drop_column('subscriptions', 'entitlement_policy_revision')
    op.drop_column('subscriptions', 'entitlement_provenance')
    op.drop_column('subscriptions', 'entitlement_snapshot')
    op.drop_column('tariffs', 'location_policy_revision')
    op.drop_column('tariffs', 'entitlement_mode')

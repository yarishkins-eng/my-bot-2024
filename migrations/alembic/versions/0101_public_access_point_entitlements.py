"""add Host-title access point entitlement domain

Revision ID: 0101
Revises: 0100
Create Date: 2026-08-09

This revision is additive and intentionally creates no catalog rows, policies,
terms, conversion records, or Panel requests.  Existing PublicLocation and
legacy-manifest access stays byte-for-byte authoritative until a separately
approved owner conversion provides dedicated-squad evidence.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0101'
down_revision: Union[str, None] = '0100'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'tariffs', sa.Column('access_point_policy_revision', sa.Integer(), nullable=False, server_default='0')
    )
    op.drop_constraint('ck_tariffs_entitlement_mode', 'tariffs', type_='check')
    op.create_check_constraint(
        'ck_tariffs_entitlement_mode',
        'tariffs',
        "entitlement_mode IN ('legacy_snapshot', 'location_managed', 'no_locations', 'access_point_managed')",
    )
    op.add_column('subscription_entitlement_snapshots', sa.Column('inventory_fingerprint', sa.String(128)))
    # ``subscription_checkouts`` predates 0100.  New Device-First rows must
    # retain the exact entitlement quoted before a wallet debit or provider
    # invoice; the empty default keeps historical rows read-compatible.
    op.add_column(
        'subscription_checkouts',
        sa.Column('entitlement_quote_snapshot', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )

    op.create_table(
        'public_access_points',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('panel_host_key', sa.String(255), nullable=False, unique=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('state', sa.String(32), nullable=False, server_default='needs_verification'),
        sa.Column('state_reason', sa.Text()),
        sa.Column('presentation_revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('entitlement_revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('inventory_revision', sa.String(128)),
        sa.Column('inventory_fingerprint', sa.String(128), nullable=False),
        sa.Column('graph_fingerprint', sa.String(128), nullable=False),
        sa.Column('tariff_assignable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint(
            "state IN ('verified', 'needs_verification', 'needs_reconcile', 'retired')",
            name='ck_public_access_points_state',
        ),
    )
    op.create_table(
        'public_access_point_squad_mappings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'public_access_point_id',
            sa.String(36),
            sa.ForeignKey('public_access_points.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column('internal_squad_key', sa.String(255), nullable=False, unique=True),
        sa.Column('is_dedicated_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_current', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('graph_fingerprint', sa.String(128), nullable=False),
        sa.Column('inventory_revision', sa.String(128)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('public_access_point_id', 'internal_squad_key', name='uq_public_access_point_squad'),
    )
    op.create_index(
        'ix_public_access_point_squad_mappings_public_access_point_id',
        'public_access_point_squad_mappings',
        ['public_access_point_id'],
    )
    op.create_table(
        'tariff_access_point_policy_revisions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('selection_hash', sa.String(128), nullable=False),
        sa.Column('inventory_fingerprint', sa.String(128), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('tariff_id', 'revision', name='uq_tariff_access_point_policy_revision'),
    )
    op.create_index(
        'ix_tariff_access_point_policy_revisions_tariff_id', 'tariff_access_point_policy_revisions', ['tariff_id']
    )
    op.create_table(
        'tariff_access_point_policy_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'policy_revision_id',
            sa.Integer(),
            sa.ForeignKey('tariff_access_point_policy_revisions.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.Column(
            'public_access_point_id',
            sa.String(36),
            sa.ForeignKey('public_access_points.id', ondelete='RESTRICT'),
            nullable=False,
        ),
        sa.UniqueConstraint('policy_revision_id', 'public_access_point_id', name='uq_tariff_access_point_policy_item'),
    )
    op.create_index(
        'ix_tariff_access_point_policy_items_policy_revision_id',
        'tariff_access_point_policy_items',
        ['policy_revision_id'],
    )
    op.create_table(
        'tariff_access_point_conversions',
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('legacy_manifest_hash', sa.String(128), nullable=False, unique=True),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('conversion_hash', sa.String(128), nullable=False, unique=True),
        sa.Column('prepared_operation_reference', sa.String(255), nullable=False),
        sa.Column('readback_evidence_hash', sa.String(128), nullable=False),
        sa.Column('approved_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('approval_reason', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'subscription_entitlement_terms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='RESTRICT'), nullable=False
        ),
        sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='RESTRICT')),
        sa.Column('term_version', sa.Integer(), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('access_point_ids', sa.JSON(), nullable=False),
        sa.Column('technical_squad_keys', sa.JSON(), nullable=False),
        sa.Column('policy_revision', sa.Integer(), nullable=False),
        sa.Column('inventory_fingerprint', sa.String(128), nullable=False),
        sa.Column('source_reference', sa.String(255), unique=True),
        sa.Column('provenance', sa.String(32), nullable=False),
        sa.Column('grant_hash', sa.String(128), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('subscription_id', 'term_version', name='uq_subscription_entitlement_term'),
        sa.CheckConstraint('ends_at > starts_at', name='ck_subscription_entitlement_term_window'),
    )
    op.create_index(
        'ix_subscription_entitlement_terms_subscription_id', 'subscription_entitlement_terms', ['subscription_id']
    )
    # A future paid renewal must be contiguous, but never overlap the active
    # grant.  PostgreSQL needs btree_gist for equality on subscription_id.
    op.execute('CREATE EXTENSION IF NOT EXISTS btree_gist')
    op.execute(
        """
        ALTER TABLE subscription_entitlement_terms
        ADD CONSTRAINT ex_subscription_entitlement_term_no_overlap
        EXCLUDE USING gist (
            subscription_id WITH =,
            tstzrange(starts_at, ends_at, '[)') WITH &&
        )
        """
    )
    # A future AP term is immutable financial evidence.  Its Panel assignment
    # is a distinct, durable, retryable boundary action: never overwrite the
    # currently-effective squads when an early renewal is captured.
    op.create_table(
        'subscription_entitlement_term_projection_outbox',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'term_id',
            sa.Integer(),
            sa.ForeignKey('subscription_entitlement_terms.id', ondelete='RESTRICT'),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            'subscription_id', sa.Integer(), sa.ForeignKey('subscriptions.id', ondelete='RESTRICT'), nullable=False
        ),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('claim_epoch', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('claimed_at', sa.DateTime(timezone=True)),
        sa.Column('delivered_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'delivered', 'manual_reconcile')",
            name='ck_subscription_entitlement_term_projection_state',
        ),
    )
    op.create_index(
        'ix_subscription_entitlement_term_projection_due',
        'subscription_entitlement_term_projection_outbox',
        ['state', 'effective_at'],
    )


def downgrade() -> None:
    # Production rollback is forward reconciliation.  This path is only for a
    # disposable local database that has never issued an access-point term.
    connection = op.get_bind()
    has_access_point_history = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM tariffs WHERE entitlement_mode = 'access_point_managed'
                UNION ALL SELECT 1 FROM subscription_entitlement_terms
                UNION ALL SELECT 1 FROM tariff_access_point_policy_revisions
                UNION ALL SELECT 1 FROM tariff_access_point_conversions
            )
            """
        )
    ).scalar()
    if has_access_point_history:
        raise RuntimeError(
            'Unsafe 0101 downgrade refused: access-point history exists. '
            'Use forward reconciliation and git revert; do not destroy issued entitlement evidence.'
        )
    for table in (
        'subscription_entitlement_term_projection_outbox',
        'subscription_entitlement_terms',
        'tariff_access_point_conversions',
        'tariff_access_point_policy_items',
        'tariff_access_point_policy_revisions',
        'public_access_point_squad_mappings',
        'public_access_points',
    ):
        op.drop_table(table)
    op.drop_constraint('ck_tariffs_entitlement_mode', 'tariffs', type_='check')
    op.create_check_constraint(
        'ck_tariffs_entitlement_mode',
        'tariffs',
        "entitlement_mode IN ('legacy_snapshot', 'location_managed', 'no_locations')",
    )
    op.drop_column('subscription_entitlement_snapshots', 'inventory_fingerprint')
    op.drop_column('subscription_checkouts', 'entitlement_quote_snapshot')
    op.drop_column('tariffs', 'access_point_policy_revision')

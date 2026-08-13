"""add dormant entitlement authority foundation

Revision ID: 0103
Revises: 0102
Create Date: 2026-08-13

Additive schema only: no seed/backfill/repair and no Panel HTTP.  All runtime
flags remain off.  Downgrade is permitted only before any Gate-1 row or flag
is used and is guarded by explicit emptiness checks.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = '0103'
down_revision: Union[str, None] = '0102'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        'entitlement_identities',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('deterministic_owner_key', sa.String(64), nullable=False),
        sa.Column('panel_uuid', sa.String(36), nullable=True),
        sa.Column('panel_uuid_hmac', sa.String(64), nullable=True),
        sa.Column('generation', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('verified_generation', sa.BigInteger(), nullable=True),
        sa.Column('lifecycle_state', sa.String(40), nullable=False, server_default='dormant'),
        sa.Column('quarantine_code', sa.String(64), nullable=True),
        sa.Column('remote_outcome_unknown_generation', sa.BigInteger(), nullable=True),
        sa.Column('reset_epoch', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('revoke_epoch', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('erasure_requested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cleanup_terminal_at', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('user_id', name='uq_entitlement_identities_user'),
        sa.UniqueConstraint('operation_id', name='uq_entitlement_identities_operation'),
        sa.UniqueConstraint('deterministic_owner_key', name='uq_entitlement_identities_owner_key'),
        sa.UniqueConstraint('panel_uuid', name='uq_entitlement_identities_panel_uuid'),
        sa.CheckConstraint('generation >= 0', name='ck_entitlement_identities_generation'),
        sa.CheckConstraint(
            'verified_generation IS NULL OR verified_generation <= generation',
            name='ck_entitlement_identities_verified_generation',
        ),
        sa.CheckConstraint('reset_epoch >= 0 AND revoke_epoch >= 0', name='ck_entitlement_identities_epochs'),
    )
    op.create_index(
        'ix_entitlement_identities_lifecycle_updated',
        'entitlement_identities',
        ['lifecycle_state', 'updated_at'],
    )

    op.create_table(
        'entitlement_source_revisions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('source_type', sa.String(40), nullable=False),
        sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('source_fingerprint', sa.String(64), nullable=False),
        sa.Column('provenance', sa.String(40), nullable=False),
        sa.Column('desired_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('desired_hash', sa.String(64), nullable=False),
        sa.Column('authority_state', sa.String(32), nullable=False, server_default='authorized'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('source_type', 'source_key', name='uq_entitlement_source_type_key'),
        sa.UniqueConstraint('identity_id', 'generation', name='uq_entitlement_source_identity_generation'),
        sa.CheckConstraint('generation > 0', name='ck_entitlement_source_generation'),
        sa.CheckConstraint(
            "jsonb_typeof(desired_snapshot) = 'object' AND "
            "desired_snapshot ?& ARRAY['owner_key','panel_uuid','status','expire_at','traffic_limit_bytes','traffic_limit_strategy','hwid_device_limit','internal_squads','external_squad_uuid','provenance','generation','reset_epoch','revoke_epoch','deny_overlays'] AND "
            "(desired_snapshot - ARRAY['owner_key','panel_uuid','status','expire_at','traffic_limit_bytes','traffic_limit_strategy','hwid_device_limit','internal_squads','external_squad_uuid','provenance','generation','reset_epoch','revoke_epoch','deny_overlays']) = '{}'::jsonb",
            name='ck_entitlement_source_snapshot_keys',
        ),
    )
    op.create_index(
        'ix_entitlement_source_identity_created',
        'entitlement_source_revisions',
        ['identity_id', 'created_at'],
    )

    op.create_table(
        'entitlement_overlays',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'source_revision_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_source_revisions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('overlay_type', sa.String(40), nullable=False),
        sa.Column('epoch', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('source_key', sa.String(128), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('effective_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution_code', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('identity_id', 'overlay_type', 'epoch', name='uq_entitlement_overlay_epoch'),
        sa.UniqueConstraint('source_key', name='uq_entitlement_overlay_source_key'),
        sa.CheckConstraint('generation > 0 AND epoch >= 0', name='ck_entitlement_overlay_generation_epoch'),
    )
    op.create_index('ix_entitlement_overlays_identity_active', 'entitlement_overlays', ['identity_id', 'is_active'])

    op.create_table(
        'entitlement_projection_commands',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'source_revision_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_source_revisions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('command_type', sa.String(32), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('deterministic_create_key', sa.String(64), nullable=True),
        sa.Column('desired_hash', sa.String(64), nullable=False),
        sa.Column('stage', sa.String(40), nullable=False, server_default='pending'),
        sa.Column('lease_owner', sa.String(64), nullable=True),
        sa.Column('lease_epoch', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('mutation_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('remote_outcome_unknown', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_error_code', sa.String(64), nullable=True),
        sa.Column('canonical_observation_hash', sa.String(64), nullable=True),
        *_timestamps(),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('operation_id', name='uq_entitlement_projection_operation'),
        sa.UniqueConstraint('idempotency_key', name='uq_entitlement_projection_idempotency'),
        sa.UniqueConstraint('identity_id', 'generation', name='uq_entitlement_projection_identity_generation'),
        sa.CheckConstraint('generation > 0 AND lease_epoch >= 0', name='ck_entitlement_projection_generation_lease'),
    )
    op.create_index(
        'ix_entitlement_projection_claim',
        'entitlement_projection_commands',
        ['stage', 'lease_expires_at'],
    )

    op.create_table(
        'entitlement_observations',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('source_kind', sa.String(24), nullable=False),
        sa.Column('event_type', sa.String(48), nullable=False),
        sa.Column('event_id_hash', sa.String(64), nullable=False),
        sa.Column('observed_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('observed_hash', sa.String(64), nullable=False),
        sa.Column('comparison_result', sa.String(32), nullable=False),
        sa.Column(
            'mismatch_fields',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('identity_id', 'source_kind', 'event_id_hash', name='uq_entitlement_observation_event'),
        sa.CheckConstraint(
            "jsonb_typeof(observed_snapshot) = 'object' AND "
            "observed_snapshot ?& ARRAY['owner_key','panel_uuid','status','expire_at','traffic_limit_bytes','traffic_limit_strategy','hwid_device_limit','internal_squads','external_squad_uuid','provenance','generation','reset_epoch','revoke_epoch','deny_overlays'] AND "
            "(observed_snapshot - ARRAY['owner_key','panel_uuid','status','expire_at','traffic_limit_bytes','traffic_limit_strategy','hwid_device_limit','internal_squads','external_squad_uuid','provenance','generation','reset_epoch','revoke_epoch','deny_overlays']) = '{}'::jsonb",
            name='ck_entitlement_observation_snapshot_keys',
        ),
        sa.CheckConstraint(
            "jsonb_typeof(mismatch_fields) = 'array'",
            name='ck_entitlement_observation_mismatch_array',
        ),
    )
    op.create_index('ix_entitlement_observations_retention', 'entitlement_observations', ['retention_until'])

    op.create_table(
        'entitlement_webhook_inbox',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('event_id_hash', sa.String(64), nullable=False),
        sa.Column('event_type', sa.String(48), nullable=False),
        sa.Column('normalized_hash', sa.String(64), nullable=False),
        sa.Column('event_timestamp', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('event_id_hash', name='uq_entitlement_webhook_event_hash'),
    )
    op.create_index('ix_entitlement_webhook_retention', 'entitlement_webhook_inbox', ['retention_until'])

    op.create_table(
        'entitlement_notification_intents',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('notification_type', sa.String(32), nullable=False),
        sa.Column('state', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('cancellation_code', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('operation_id', name='uq_entitlement_notification_operation'),
        sa.UniqueConstraint(
            'identity_id',
            'generation',
            'notification_type',
            name='uq_entitlement_notification_generation',
        ),
    )
    op.create_index(
        'ix_entitlement_notifications_state_created',
        'entitlement_notification_intents',
        ['state', 'created_at'],
    )

    op.create_table(
        'entitlement_cleanup_commands',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column(
            'identity_id',
            sa.BigInteger(),
            sa.ForeignKey('entitlement_identities.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('generation', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(32), nullable=False, server_default='erasure_requested'),
        sa.Column('encrypted_panel_uuid', sa.LargeBinary(), nullable=True),
        sa.Column('panel_uuid_hmac', sa.String(64), nullable=False),
        sa.Column('identity_hmac', sa.String(64), nullable=False),
        sa.Column('lease_epoch', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('remote_outcome_unknown', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_error_code', sa.String(64), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('alert_after', sa.DateTime(timezone=True), nullable=False),
        sa.Column('operator_alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('terminal_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint('operation_id', name='uq_entitlement_cleanup_operation'),
        sa.UniqueConstraint('identity_id', 'generation', name='uq_entitlement_cleanup_identity_generation'),
    )
    op.create_index('ix_entitlement_cleanup_alert', 'entitlement_cleanup_commands', ['state', 'alert_after'])

    op.create_table(
        'entitlement_cleanup_tombstones',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column('identity_hmac', sa.String(64), nullable=False),
        sa.Column('panel_uuid_hmac', sa.String(64), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('last_error_code', sa.String(64), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('terminal_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('operation_id', name='uq_entitlement_tombstone_operation'),
    )
    op.create_index(
        'ix_entitlement_tombstones_retention',
        'entitlement_cleanup_tombstones',
        ['state', 'retention_until'],
    )


_TABLES = (
    'entitlement_cleanup_tombstones',
    'entitlement_cleanup_commands',
    'entitlement_notification_intents',
    'entitlement_webhook_inbox',
    'entitlement_observations',
    'entitlement_projection_commands',
    'entitlement_overlays',
    'entitlement_source_revisions',
    'entitlement_identities',
)


def downgrade() -> None:
    connection = op.get_bind()
    # Close the check/drop race: once emptiness is observed, no concurrent
    # writer may insert a Gate-1 row before the additive tables are removed.
    connection.execute(sa.text(f'LOCK TABLE {", ".join(_TABLES)} IN ACCESS EXCLUSIVE MODE'))
    for table in _TABLES:
        if connection.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)')).scalar():
            raise RuntimeError(f'0103 downgrade refused: {table} contains rows')
    for table in _TABLES:
        op.drop_table(table)

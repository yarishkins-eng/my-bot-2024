from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings


DATABASE_URL = os.environ.get('ENTITLEMENT_AUTHORITY_APP_DATABASE_URL')
if DATABASE_URL is None:
    pytest.skip('isolated entitlement authority PostgreSQL is not configured', allow_module_level=True)
ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    'entitlement_identities',
    'entitlement_source_revisions',
    'entitlement_overlays',
    'entitlement_projection_commands',
    'entitlement_observations',
    'entitlement_webhook_inbox',
    'entitlement_notification_intents',
    'entitlement_cleanup_commands',
    'entitlement_cleanup_tombstones',
}


def test_all_foundation_flags_default_off() -> None:
    settings = Settings(BOT_TOKEN='test-token')
    assert settings.ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED is False
    assert settings.ENTITLEMENT_AUTHORITY_SHADOW_ENABLED is False


@pytest.mark.asyncio
async def test_additive_schema_has_all_dormant_tables_and_no_pii_columns() -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT table_name, column_name
                          FROM information_schema.columns
                         WHERE table_schema='public' AND table_name LIKE 'entitlement_%'
                        """
                    )
                )
            ).all()
        found = {row.table_name for row in rows}
        assert found >= TABLES
        forbidden = ('telegram', 'username', 'first_name', 'last_name', 'email', 'subscription_url', 'credential')
        assert not [row for row in rows if any(token in row.column_name.lower() for token in forbidden)]
        assert not any(row.column_name in {'raw_body', 'provider_payload', 'webhook_payload'} for row in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_json_snapshot_constraints_reject_extra_pii_keys() -> None:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            identity_id = (
                await connection.execute(
                    text(
                        """
                        INSERT INTO entitlement_identities(operation_id, deterministic_owner_key, generation)
                        VALUES ('schema-test-operation', 'schema-test-owner', 1) RETURNING id
                        """
                    )
                )
            ).scalar_one()
            invalid = {
                'owner_key': 'owner',
                'panel_uuid': None,
                'status': 'ACTIVE',
                'expire_at': '2026-08-13T00:00:00.000000Z',
                'traffic_limit_bytes': 0,
                'traffic_limit_strategy': 'NO_RESET',
                'hwid_device_limit': None,
                'internal_squads': [],
                'external_squad_uuid': None,
                'provenance': 'paid_sale',
                'generation': 1,
                'reset_epoch': 0,
                'revoke_epoch': 0,
                'deny_overlays': [],
                'email': 'forbidden-value',
            }
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        """
                        INSERT INTO entitlement_source_revisions
                               (identity_id, generation, source_type, source_key, source_fingerprint,
                                provenance, desired_snapshot, desired_hash)
                        VALUES (:identity_id, 1, 'test', 'schema-invalid', :fingerprint,
                                'paid_sale', CAST(:snapshot AS jsonb), :desired_hash)
                        """
                    ),
                    {
                        'identity_id': identity_id,
                        'fingerprint': 'a' * 64,
                        'snapshot': __import__('json').dumps(invalid),
                        'desired_hash': 'b' * 64,
                    },
                )
            await transaction.rollback()
    finally:
        await engine.dispose()


def test_migration_is_schema_only_and_has_no_panel_or_data_repair() -> None:
    source = (ROOT / 'migrations/alembic/versions/0103_entitlement_authority_dormant.py').read_text(encoding='utf-8')
    assert 'requests.' not in source and 'aiohttp' not in source and 'RemnaWave' not in source
    assert 'UPDATE users' not in source and 'UPDATE subscriptions' not in source
    assert 'INSERT INTO' not in source


def test_no_foundation_route_admin_ui_or_export_was_added() -> None:
    changed_service = ROOT / 'app/services/entitlement_authority'
    assert changed_service.is_dir()
    assert not list((ROOT / 'app/cabinet/routes').glob('*entitlement*'))
    assert not list((ROOT / 'app/webapi/routes').glob('*entitlement*'))


def test_shadow_module_has_no_remote_mutation_or_persistence_boundary() -> None:
    source = (ROOT / 'app/services/entitlement_authority/shadow.py').read_text(encoding='utf-8')
    assert 'StrictPanelClient' not in source
    assert 'request_once' not in source
    assert 'AsyncSession' not in source
    assert not any(verb in source for verb in ("'POST'", "'PATCH'", "'DELETE'"))

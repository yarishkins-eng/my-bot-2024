"""Execute the actual additive 0106 migration over a synthetic 0105 schema.

This is migration execution evidence, not a production-backup restore rehearsal.
"""

import importlib
import os
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.database.models import Base, DeviceAddonIntent, DeviceAddonTopupAttempt, User


DATABASE_URL = os.getenv('DEVICE_ADDON_TEST_DATABASE_URL')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE_URL, reason='Requires isolated addon PostgreSQL')]


def run_migration(connection, name, method):
    migration = importlib.import_module(f'migrations.alembic.versions.{name}')
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, method)()


async def test_upgrade_guard_empty_downgrade_and_reupgrade():
    url = make_url(DATABASE_URL)
    if url.host not in {'localhost', '127.0.0.1'} or not url.database.startswith('teplo_device_test_'):
        raise RuntimeError('Only disposable local teplo_device_test_* databases are allowed')
    schema = f'addon_migration_{uuid.uuid4().hex}'
    bootstrap = create_async_engine(DATABASE_URL)
    async with bootstrap.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(DATABASE_URL, connect_args={'server_settings': {'search_path': schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text('DROP TABLE device_addon_topup_attempts'))
            await connection.execute(text('DROP TABLE device_addon_intents'))
            await connection.execute(text('ALTER TABLE users DROP COLUMN device_addon_generation'))
            await connection.run_sync(run_migration, '0105_test_account_reset_fence', 'install_guards')
            await connection.run_sync(run_migration, '0106_device_addon_intents', 'upgrade')
            names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert {'device_addon_intents', 'device_addon_topup_attempts'} <= set(names)
            for model in (DeviceAddonIntent, DeviceAddonTopupAttempt):
                actual_indexes = await connection.run_sync(
                    lambda conn, name=model.__tablename__: inspect(conn).get_indexes(name)
                )
                actual_names = {index['name'] for index in actual_indexes}
                assert {index.name for index in model.__table__.indexes} <= actual_names
            intent_fks = await connection.run_sync(lambda conn: inspect(conn).get_foreign_keys('device_addon_intents'))
            live_target = next(fk for fk in intent_fks if fk['constrained_columns'] == ['subscription_id'])
            assert live_target['options']['ondelete'] == 'SET NULL'
            assert all('target_subscription_id' not in fk['constrained_columns'] for fk in intent_fks)
            guards = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE tgname='teplo_test_reset_guard' "
                    "AND tgrelid IN ('device_addon_intents'::regclass,'device_addon_topup_attempts'::regclass)"
                )
            )
            assert guards == 2
            await connection.run_sync(run_migration, '0106_device_addon_intents', 'downgrade')
            await connection.run_sync(run_migration, '0106_device_addon_intents', 'upgrade')
            await connection.execute(
                User.__table__.insert().values(
                    id=9001, telegram_id=7788800001, balance_kopeks=0, test_reset_state='resetting'
                )
            )
        # Trigger is exercised before the INSERT's unrelated NOT NULL columns:
        # a running reset must reject any stale intent writer.
        async with engine.begin() as connection:
            with pytest.raises(DBAPIError, match='test_account_reset_in_progress'):
                async with connection.begin_nested():
                    await connection.execute(text('INSERT INTO device_addon_intents (user_id) VALUES (9001)'))
        # Once any operation exists, downgrade must refuse instead of deleting
        # receipts/late-payment ownership. Use a historical deleted target.
        async with engine.begin() as connection:
            await connection.execute(User.__table__.insert().values(id=9002, telegram_id=7788800002))
            await connection.execute(
                DeviceAddonIntent.__table__.insert().values(
                    public_id=str(uuid.uuid4()),
                    user_id=9002,
                    subscription_id=None,
                    target_subscription_id=123456,
                    idempotency_key=str(uuid.uuid4()),
                    request_hash='a' * 64,
                    devices_to_add=1,
                    original_device_limit=2,
                    end_date=text('now()'),
                    device_addon_generation=0,
                    days_left=1,
                    monthly_price_kopeks=100,
                    base_price_kopeks=100,
                    quoted_price_kopeks=100,
                )
            )
            with pytest.raises(RuntimeError, match='financial history exists'):
                async with connection.begin_nested():
                    await connection.run_sync(run_migration, '0106_device_addon_intents', 'downgrade')
            assert await connection.scalar(text('SELECT count(*) FROM device_addon_intents')) == 1
    finally:
        await engine.dispose()
        async with bootstrap.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await bootstrap.dispose()

"""Opt-in PostgreSQL proof for the campaign-attribution deletion fence."""

import asyncio
import importlib
import os
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


DATABASE_URL = os.getenv('CAMPAIGN_ANALYTICS_TEST_DATABASE_URL')

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason='CAMPAIGN_ANALYTICS_TEST_DATABASE_URL is required for PostgreSQL concurrency tests',
)


def _asyncpg():
    module = importlib.import_module('asyncpg')
    if hasattr(module, 'connect'):
        return module
    sys.modules.pop('asyncpg', None)
    return importlib.import_module('asyncpg')


@pytest.mark.asyncio
async def test_concurrent_registration_commits_before_delete_check_and_is_never_cascaded() -> None:
    """A child INSERT holding KEY SHARE must win; delete then sees and preserves it."""

    from app.services.campaign_service import delete_campaign_if_unattributed

    asyncpg = _asyncpg()
    raw_url = DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://', 1)
    engine_url = DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://', 1)
    schema = f'rk1_campaign_{uuid.uuid4().hex}'
    setup = await asyncpg.connect(raw_url)
    registration = await asyncpg.connect(raw_url)
    engine = create_async_engine(engine_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    try:
        await setup.execute(f'CREATE SCHEMA "{schema}"')
        await setup.execute(f'CREATE TABLE "{schema}".advertising_campaigns (id INTEGER PRIMARY KEY)')
        await setup.execute(
            f'CREATE TABLE "{schema}".advertising_campaign_registrations ('
            'id INTEGER PRIMARY KEY, campaign_id INTEGER NOT NULL '
            f'REFERENCES "{schema}".advertising_campaigns(id) ON DELETE CASCADE)'
        )
        await setup.execute(f'INSERT INTO "{schema}".advertising_campaigns (id) VALUES (41)')

        await registration.execute(f'SET search_path TO "{schema}"')
        registration_tx = registration.transaction()
        await registration_tx.start()
        await registration.execute('INSERT INTO advertising_campaign_registrations (id, campaign_id) VALUES (1, 41)')

        async with sessions() as deletion_session:
            await deletion_session.execute(text(f'SET search_path TO "{schema}"'))
            deletion = asyncio.create_task(delete_campaign_if_unattributed(deletion_session, 41))
            await asyncio.sleep(0.1)
            assert not deletion.done(), 'DELETE must wait for the concurrent FK KEY SHARE lock'

            await registration_tx.commit()
            assert await asyncio.wait_for(deletion, timeout=3) is False

        assert await setup.fetchval(f'SELECT count(*) FROM "{schema}".advertising_campaigns') == 1
        assert await setup.fetchval(f'SELECT count(*) FROM "{schema}".advertising_campaign_registrations') == 1
    finally:
        await engine.dispose()
        await registration.close()
        if schema.startswith('rk1_campaign_'):
            await setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await setup.close()

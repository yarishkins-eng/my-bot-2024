from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.entitlement_authority import shadow_runtime
from app.services.entitlement_authority.shadow import ShadowPolicy
from app.services.entitlement_authority.shadow_runtime import LegacyPostgresShadowSource


DATABASE_URL = os.environ.get('ENTITLEMENT_AUTHORITY_APP_DATABASE_URL')
if DATABASE_URL is None:
    pytest.skip('isolated Gate 2 PostgreSQL is not configured', allow_module_level=True)


@pytest_asyncio.fixture
async def sessions() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _fingerprint(sessions: async_sessionmaker[AsyncSession]) -> tuple[tuple[str, int, str], ...]:
    async with sessions() as session:
        rows = (
            await session.execute(
                text(
                    """
                    WITH names(table_name) AS (
                        VALUES ('users'), ('subscriptions'), ('subscription_checkouts'),
                               ('subscription_entitlement_terms'), ('entitlement_identities'),
                               ('entitlement_source_revisions'), ('entitlement_projection_commands'),
                               ('entitlement_observations')
                    ), row_hashes(table_name, row_hash) AS (
                        SELECT 'users', md5(row_to_json(t)::text) FROM users t
                        UNION ALL SELECT 'subscriptions', md5(row_to_json(t)::text) FROM subscriptions t
                        UNION ALL SELECT 'subscription_checkouts', md5(row_to_json(t)::text)
                          FROM subscription_checkouts t
                        UNION ALL SELECT 'subscription_entitlement_terms', md5(row_to_json(t)::text)
                          FROM subscription_entitlement_terms t
                        UNION ALL SELECT 'entitlement_identities', md5(row_to_json(t)::text)
                          FROM entitlement_identities t
                        UNION ALL SELECT 'entitlement_source_revisions', md5(row_to_json(t)::text)
                          FROM entitlement_source_revisions t
                        UNION ALL SELECT 'entitlement_projection_commands', md5(row_to_json(t)::text)
                          FROM entitlement_projection_commands t
                        UNION ALL SELECT 'entitlement_observations', md5(row_to_json(t)::text)
                          FROM entitlement_observations t
                    )
                    SELECT names.table_name,
                           count(row_hashes.row_hash)::bigint,
                           md5(coalesce(string_agg(row_hashes.row_hash, '' ORDER BY row_hashes.row_hash), ''))
                      FROM names
                 LEFT JOIN row_hashes USING (table_name)
                     GROUP BY names.table_name
                     ORDER BY names.table_name
                    """
                )
            )
        ).all()
    return tuple((str(name), int(count), str(digest)) for name, count, digest in rows)


@pytest.mark.asyncio
async def test_real_postgres_source_reads_without_row_count_change(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    before = await _fingerprint(sessions)
    candidates = await LegacyPostgresShadowSource(sessions).load_candidates(
        ShadowPolicy(),
        now=datetime.now(UTC),
    )
    after = await _fingerprint(sessions)
    assert after == before
    assert len(candidates) <= ShadowPolicy().max_identities_per_cycle


@pytest.mark.asyncio
async def test_real_postgres_read_only_transaction_rejects_injected_dml(
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = await _fingerprint(sessions)
    monkeypatch.setattr(
        shadow_runtime,
        '_SOURCE_COHORT_SQL',
        'UPDATE entitlement_identities SET generation=generation RETURNING id',
    )
    monkeypatch.setattr(
        shadow_runtime,
        '_SOURCE_PREFLIGHT_SQL',
        """
        SELECT false AS multiple_current_subscriptions,
               false AS owner_uuid_binding_mismatch,
               false AS cross_owner_panel_uuid
        """,
    )
    with pytest.raises(DBAPIError, match='read-only transaction'):
        await LegacyPostgresShadowSource(sessions).load_candidates(
            ShadowPolicy(),
            now=datetime.now(UTC),
        )
    assert await _fingerprint(sessions) == before

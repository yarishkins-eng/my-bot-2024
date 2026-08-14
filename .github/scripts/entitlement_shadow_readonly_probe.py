#!/usr/bin/env python3
"""Prove that PostgreSQL rejects injected DML in the production shadow source."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError

from app.database.database import AsyncSessionLocal, engine
from app.services.entitlement_authority import shadow_runtime
from app.services.entitlement_authority.shadow import ShadowPolicy
from app.services.entitlement_authority.shadow_runtime import LegacyPostgresShadowSource


async def main() -> int:
    shadow_runtime._SOURCE_PREFLIGHT_SQL = """
        SELECT false AS multiple_current_subscriptions,
               false AS owner_uuid_binding_mismatch,
               false AS cross_owner_panel_uuid
    """
    shadow_runtime._SOURCE_COHORT_SQL = 'UPDATE entitlement_identities SET generation=generation RETURNING id'
    try:
        await LegacyPostgresShadowSource(AsyncSessionLocal).load_candidates(
            ShadowPolicy(),
            now=datetime.now(UTC),
        )
    except DBAPIError:
        print('injected-dml-rejected=true')
        return 0
    finally:
        await engine.dispose()
    return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

"""Create the current ORM schema in an empty isolated PostgreSQL database.

This is synthetic schema evidence only; it does not replace Alembic upgrade or
a protected production-like restore test.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.database.database import engine  # noqa: E402
from app.database.models import Base  # noqa: E402


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

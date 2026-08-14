#!/usr/bin/env python3
"""Create the current ORM schema only in the disposable Gate 2 database."""

from __future__ import annotations

import asyncio

from app.database.database import engine
from app.database.models import Base


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

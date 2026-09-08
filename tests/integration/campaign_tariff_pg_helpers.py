"""Private helpers for the campaign-tariff PostgreSQL regression."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User


class UserLockProbe:
    def __init__(self) -> None:
        self.first_locked = asyncio.Event()
        self.second_attempted = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls = 0


class ProbedSession(AsyncSession):
    async def scalar(self, statement, *args, **kwargs):
        probe = self.info.get('campaign_user_lock_probe')
        is_user_lock = (
            probe is not None
            and getattr(statement, '_for_update_arg', None) is not None
            and User.__table__ in statement.get_final_froms()
        )
        if not is_user_lock:
            return await super().scalar(statement, *args, **kwargs)

        probe.calls += 1
        if probe.calls > 1:
            probe.second_attempted.set()
        result = await super().scalar(statement, *args, **kwargs)
        if probe.calls == 1:
            probe.first_locked.set()
            await probe.release_first.wait()
        return result


async def wait_for_backend_lock(maker, backend_pid: int) -> None:
    async def observed() -> None:
        async with maker() as db:
            while True:
                wait_type = await db.scalar(
                    text('SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid'),
                    {'pid': backend_pid},
                )
                if wait_type == 'Lock':
                    return
                await db.rollback()
                await asyncio.sleep(0.02)

    await asyncio.wait_for(observed(), timeout=3)

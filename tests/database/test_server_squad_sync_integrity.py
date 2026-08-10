from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import server_squad


class _Result:
    def __init__(self, *, scalars=(), rows=()):
        self._scalars = list(scalars)
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._scalars

    def fetchall(self):
        return self._rows


@pytest.mark.asyncio
async def test_panel_catalog_sync_keeps_referenced_squad_as_unavailable_tombstone() -> None:
    removed_uuid = 'retired-pl-squad'
    server = SimpleNamespace(
        id=7,
        squad_uuid=removed_uuid,
        display_name='Poland',
        original_name='Poland',
        is_available=True,
    )
    tariff_selection = [removed_uuid]
    subscription_selection = [removed_uuid]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(scalars=[server]),
                _Result(rows=[]),
                _Result(scalars=[tariff_selection]),
                _Result(scalars=[subscription_selection]),
                _Result(rows=[]),
            ]
        ),
        commit=AsyncMock(),
    )

    created, updated, removed = await server_squad.sync_with_remnawave(db, [])

    assert (created, updated, removed) == (0, 1, 0)
    assert server.is_available is False
    assert tariff_selection == [removed_uuid]
    assert subscription_selection == [removed_uuid]
    db.commit.assert_awaited_once()
    assert all('DELETE' not in str(call.args[0]) for call in db.execute.await_args_list)

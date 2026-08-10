from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_tariffs


@pytest.mark.asyncio
async def test_available_servers_endpoint_returns_internal_squad_checkbox_data(monkeypatch) -> None:
    list_squads = AsyncMock(
        return_value=(
            [
                SimpleNamespace(
                    id=1,
                    squad_uuid='de-squad',
                    display_name='Germany',
                    country_code='DE',
                )
            ],
            1,
        )
    )
    monkeypatch.setattr(
        admin_tariffs,
        'get_all_server_squads',
        list_squads,
    )

    db = SimpleNamespace()
    response = await admin_tariffs.get_available_servers(admin=SimpleNamespace(), db=db)

    assert len(response) == 1
    assert response[0].squad_uuid == 'de-squad'
    assert response[0].display_name == 'Germany'
    assert response[0].is_selected is False
    list_squads.assert_awaited_once_with(db, available_only=True, limit=1000)


@pytest.mark.asyncio
async def test_external_squad_catalog_is_not_reopened_by_native_node_rollout() -> None:
    with pytest.raises(HTTPException) as error:
        await admin_tariffs.get_available_external_squads(admin=SimpleNamespace())

    assert error.value.status_code == 410

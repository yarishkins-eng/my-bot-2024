from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import legacy_entitlement_manifest_seed, remnawave_resync_service
from app.services.legacy_entitlement_manifest_seed import (
    _ApprovedSharedSquadInventory,
    _raw_inbound_membership_hash,
    _squad_reference,
)


@pytest.mark.asyncio
async def test_changed_shared_raw_membership_stops_resync_before_panel_write(monkeypatch) -> None:
    squads = ['shared-a', 'shared-b']
    monkeypatch.setattr(
        legacy_entitlement_manifest_seed,
        'OWNER_APPROVED_SHARED_SQUAD_INVENTORY',
        {
            _squad_reference(squad): _ApprovedSharedSquadInventory(
                raw_inbounds_count=2,
                raw_inbound_membership_sha256=_raw_inbound_membership_hash([f'{squad}-one', f'{squad}-two']),
            )
            for squad in squads
        },
    )
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=tuple(squads))),
    )

    panel_write = AsyncMock()
    service = SimpleNamespace(
        is_configured=True,
        _refresh_configuration=lambda: None,
        update_remnawave_user=panel_write,
        create_remnawave_user=AsyncMock(),
    )
    monkeypatch.setattr(remnawave_resync_service, 'SubscriptionService', lambda: service)
    monkeypatch.setattr(
        remnawave_resync_service,
        'get_active_subscriptions_by_user_id',
        AsyncMock(return_value=[SimpleNamespace(id=10, user_id=20, remnawave_uuid=None)]),
    )
    monkeypatch.setattr(
        remnawave_resync_service,
        'settings',
        SimpleNamespace(is_multi_tariff_enabled=lambda: False),
    )

    result = await remnawave_resync_service.resync_user_subscriptions_with_panel(
        SimpleNamespace(refresh=AsyncMock()),
        SimpleNamespace(id=20, remnawave_uuid='panel-user'),
        legacy_shared_squad_inventory_reader=lambda _subscription: {
            'shared-a': ['shared-a-one', 'unexpected-raw-inbound'],
            'shared-b': ['shared-b-one', 'shared-b-two'],
        },
    )

    assert result == {'synced': 0, 'failed': 1, 'total': 1, 'skipped': False}
    panel_write.assert_not_awaited()

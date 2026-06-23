"""``panel_ok`` flag in ``GET /subscription/devices`` (Чат 1 — бэкенд-поля).

The screen lights up "Подключить" when it sees zero devices. But the endpoint
returns ``total: 0`` in THREE different situations, only one of which is a real
failure. Without a flag the screen can't tell them apart and shows a false
"Подключить". So:

* panel raised (slow/unavailable)            → ``panel_ok=False``  (real failure)
* account not provisioned yet (no panel uuid)→ ``panel_ok=True``   (NOT a failure —
  a brand-new user; lighting "Подключить" here is correct)
* normal success (really 0 devices)          → ``panel_ok=True``

The two non-failure branches MUST stay ``panel_ok=True`` or a fresh user's
"Подключить" goes dark.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules import devices


class _FakeApiCtx:
    def __init__(self, api):
        self._api = api

    async def __aenter__(self):
        return self._api

    async def __aexit__(self, *exc):
        return False


class _FakeApi:
    def __init__(self, device_rows=None, raises=False):
        self._rows = device_rows if device_rows is not None else []
        self._raises = raises

    async def get_user_devices_all(self, uuid):
        if self._raises:
            raise RuntimeError('panel unavailable')
        return {'devices': self._rows, 'total': len(self._rows)}


def _install_service(monkeypatch: pytest.MonkeyPatch, api: _FakeApi) -> None:
    class _FakeService:
        def __init__(self, *a, **k):
            pass

        def get_api_client(self):
            return _FakeApiCtx(api)

    # RemnaWaveService is imported inside get_devices → patch it at the source module.
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', _FakeService)


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, panel_uuid: str | None, device_limit: int = 3) -> None:
    sub = SimpleNamespace(device_limit=device_limit)

    async def _fake_resolve(db, user, subscription_id):
        return sub

    async def _fake_aliases(db, user_id):
        return {}

    monkeypatch.setattr(devices, 'resolve_subscription', _fake_resolve)
    monkeypatch.setattr(devices, '_resolve_panel_uuid', lambda subscription, user: panel_uuid)
    monkeypatch.setattr(devices, 'get_aliases_for_user', _fake_aliases)


@pytest.mark.asyncio
async def test_panel_ok_true_when_account_not_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    """No panel uuid (account not provisioned) is NOT a panel failure."""
    _patch_common(monkeypatch, panel_uuid=None, device_limit=2)
    user = SimpleNamespace(id=1)

    result = await devices.get_devices(subscription_id=None, user=user, db=object())

    assert result['panel_ok'] is True
    assert result['total'] == 0
    assert result['device_limit'] == 2


@pytest.mark.asyncio
async def test_panel_ok_true_on_normal_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real devices returned → panel healthy."""
    _patch_common(monkeypatch, panel_uuid='puuid-1', device_limit=5)
    _install_service(
        monkeypatch,
        _FakeApi(device_rows=[{'hwid': 'h1', 'platform': 'iOS', 'deviceModel': 'iPhone'}]),
    )
    user = SimpleNamespace(id=1)

    result = await devices.get_devices(subscription_id=None, user=user, db=object())

    assert result['panel_ok'] is True
    assert result['total'] == 1
    assert result['devices'][0]['hwid'] == 'h1'


@pytest.mark.asyncio
async def test_panel_ok_true_on_zero_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genuinely 0 devices (panel answered, empty) is still panel_ok=True."""
    _patch_common(monkeypatch, panel_uuid='puuid-1', device_limit=1)
    _install_service(monkeypatch, _FakeApi(device_rows=[]))
    user = SimpleNamespace(id=1)

    result = await devices.get_devices(subscription_id=None, user=user, db=object())

    assert result['panel_ok'] is True
    assert result['total'] == 0


@pytest.mark.asyncio
async def test_panel_ok_false_when_panel_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Panel slow/unavailable (raises) → panel_ok=False so the screen shows a
    'panel error' state instead of a false 'Подключить'."""
    _patch_common(monkeypatch, panel_uuid='puuid-1', device_limit=4)
    _install_service(monkeypatch, _FakeApi(raises=True))
    user = SimpleNamespace(id=1)

    result = await devices.get_devices(subscription_id=None, user=user, db=object())

    assert result['panel_ok'] is False
    assert result['total'] == 0
    assert result['device_limit'] == 4

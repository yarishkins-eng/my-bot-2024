from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_tariffs as cabinet_admin_tariffs
from app.cabinet.schemas.tariffs import TariffUpdateRequest
from app.handlers.admin import tariffs as admin_tariffs
from app.services.device_first_eligibility import DeviceFirstConfigurationError


def _tariff(**overrides):
    values = {
        'id': 7,
        'device_limit': 2,
        'device_price_kopeks': 5_000,
        'max_device_limit': 10,
        'device_purchase_options': [2, 3, 10],
        'allowed_squads': [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ('changes', 'reason'),
    [
        ({'device_limit': 1}, 'include'),
        ({'max_device_limit': 8}, 'exceed'),
        ({'device_price_kopeks': None}, 'positive'),
    ],
)
def test_device_purchase_options_guard_rejects_inconsistent_field_changes(changes, reason):
    conflict = admin_tariffs._device_purchase_options_conflict(_tariff(), **changes)

    assert conflict is not None
    assert reason in conflict


def test_device_purchase_options_guard_accepts_consistent_or_legacy_tariff():
    assert admin_tariffs._device_purchase_options_conflict(_tariff(), device_limit=2) is None
    assert (
        admin_tariffs._device_purchase_options_conflict(
            _tariff(device_purchase_options=None),
            device_limit=9,
            device_price_kopeks=None,
            max_device_limit=1,
        )
        is None
    )


def test_device_purchase_options_guard_escapes_the_technical_reason(monkeypatch):
    def fail_validation(*_args, **_kwargs):
        raise DeviceFirstConfigurationError('<b>unsafe reason</b>')

    monkeypatch.setattr(admin_tariffs, 'normalize_device_purchase_options', fail_validation)

    conflict = admin_tariffs._device_purchase_options_conflict(_tariff(), device_limit=1)

    assert 'Причина: настройки устройств противоречат друг другу — ' in conflict
    assert '&lt;b&gt;unsafe reason&lt;/b&gt;' in conflict
    assert '<b>unsafe reason</b>' not in conflict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changes',
    [
        {'device_limit': 1},
        {'max_device_limit': 8},
        {'device_price_kopeks': 0},
    ],
)
async def test_cabinet_rejects_inconsistent_device_field_without_replacement_options(monkeypatch, changes):
    tariff = _tariff()
    update_tariff = AsyncMock()
    monkeypatch.setattr(cabinet_admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(cabinet_admin_tariffs, 'update_tariff', update_tariff)

    with pytest.raises(HTTPException) as error:
        await cabinet_admin_tariffs.update_existing_tariff(
            tariff.id,
            TariffUpdateRequest(**changes),
            admin=SimpleNamespace(id=1),
            db=AsyncMock(),
        )

    assert error.value.status_code == 422
    assert error.value.detail == admin_tariffs._device_purchase_options_conflict(tariff, **changes)
    update_tariff.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_chat_admin_change_does_not_reach_persisted_tariff(monkeypatch):
    stored = {
        'id': 7,
        'device_limit': 2,
        'device_price_kopeks': 5_000,
        'max_device_limit': 10,
        'device_purchase_options': [2, 3, 10],
    }

    class FakeSession:
        def __init__(self):
            self.stored = dict(stored)
            self.loaded = None

        async def load(self, _tariff_id):
            if self.loaded is None:
                self.loaded = SimpleNamespace(**self.stored)
            return self.loaded

        async def commit(self):
            if self.loaded is not None:
                self.stored['device_limit'] = self.loaded.device_limit

        async def reread(self, tariff_id):
            self.loaded = None
            return await self.load(tariff_id)

    async def get_tariff_by_id(db, tariff_id):
        return await db.load(tariff_id)

    db = FakeSession()
    update_tariff = AsyncMock()
    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', get_tariff_by_id)
    monkeypatch.setattr(admin_tariffs, 'get_tariff_subscriptions_count', AsyncMock(return_value=0))
    monkeypatch.setattr(admin_tariffs, 'update_tariff', update_tariff)
    monkeypatch.setattr(admin_tariffs, 'format_tariff_info', lambda *_args: 'tariff')
    monkeypatch.setattr(admin_tariffs, 'get_tariff_view_keyboard', lambda *_args: None)
    message = SimpleNamespace(text='1', answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={'tariff_id': 7}), clear=AsyncMock())
    handler = admin_tariffs.process_edit_tariff_devices
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__

    await handler(
        message,
        SimpleNamespace(language='ru'),
        db,
        state,
    )

    # AuthMiddleware commits at the end of a handled update. Simulating that
    # commit makes this test catch a future in-memory ORM mutation as well as
    # an accidental call to update_tariff.
    await db.commit()
    persisted = await db.reread(7)
    assert persisted.device_limit == 2
    update_tariff.assert_not_awaited()
    assert 'не согласуются' in message.answer.await_args.args[0]

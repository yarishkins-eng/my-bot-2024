from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_auto_messages as api
from app.services.notification_settings_service import NotificationSettingsService as Settings
from tests.cabinet.test_ap4_pr4_prices import price_env  # noqa: F401 -- shared real-pricing fixture.


@pytest.fixture
def endpoint(price_env, monkeypatch):
    monkeypatch.setattr(api, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(api, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_claimed_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_max_promo_group_percent', AsyncMock(return_value=0))
    monkeypatch.setattr(api, '_trial_discount_stats', AsyncMock(return_value=(0, 'No deliveries yet', [])))
    return AsyncMock()


async def patch(db, **changes):
    return await api.patch_auto_message('trial-ending-discount', api.AutoMessagePatch(**changes), db, None)


@pytest.mark.asyncio
async def test_full_patch_saves_exactly_once_and_single_patch_preserves_it(endpoint, monkeypatch):
    save = Mock(wraps=Settings._save)
    monkeypatch.setattr(Settings, '_save', save)
    result = await patch(endpoint, enabled=True, discount_percent=30, carrier=48, audience=0, post_trial_hours=12)
    assert result.params == {'discount_percent': 30, 'carrier': 48, 'audience': 0, 'post_trial_hours': 12}
    assert result.enabled is True and result.discount_prices['30'][0] == {'period_days': 30, 'price_kopeks': 10400}
    assert Settings.get_trial_ending_discount_stats_since() is not None
    save.assert_called_once()
    await patch(endpoint, carrier=24)
    assert Settings.get_trial_ending_discount_percent() == 30
    assert Settings.get_trial_ending_discount_carrier() == 24


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'carrier': 7}, {'discount_percent': 31}, {'enabled': True, 'carrier': 1}])
async def test_one_invalid_field_never_partially_writes(endpoint, changes):
    before = deepcopy(Settings._data)
    raw = Settings._storage_path.read_bytes()
    with pytest.raises(HTTPException) as exc:
        await patch(endpoint, audience=0, **changes)
    assert exc.value.status_code == 422
    assert Settings._data == before and Settings._storage_path.read_bytes() == raw


@pytest.mark.asyncio
async def test_enable_revalidates_untouched_stored_values(endpoint):
    Settings._data['trial_ending_discount']['discount_percent'] = 31
    with pytest.raises(HTTPException) as exc:
        await patch(endpoint, enabled=True)
    assert exc.value.status_code == 422 and Settings.is_trial_ending_discount_enabled() is False


@pytest.mark.asyncio
async def test_save_failure_does_not_publish_any_field_or_first_date(endpoint, monkeypatch):
    before = deepcopy(Settings._data)
    raw = Settings._storage_path.read_bytes()
    monkeypatch.setattr(Settings, '_save', Mock(return_value=False))
    with pytest.raises(HTTPException) as exc:
        await patch(endpoint, enabled=True, discount_percent=30, carrier=48, audience=0, post_trial_hours=24)
    assert exc.value.status_code == 500
    assert Settings._data == before and Settings._storage_path.read_bytes() == raw


@pytest.mark.asyncio
async def test_concurrent_setting_change_during_price_check_is_not_overwritten(endpoint, monkeypatch):
    original = api._discount_examples

    async def changed(db, *, lock=False):
        if lock:
            assert Settings.set_trial_ending_discount_post_trial_hours(12)
        return await original(db, lock=lock)

    monkeypatch.setattr(api, '_discount_examples', changed)
    with pytest.raises(HTTPException) as exc:
        await patch(endpoint, enabled=True)
    assert exc.value.status_code == 409
    assert Settings.get_trial_ending_discount_post_trial_hours() == 12
    assert Settings.is_trial_ending_discount_enabled() is False


@pytest.mark.asyncio
async def test_enable_requires_fresh_thirty_day_price(endpoint, monkeypatch):
    checked = AsyncMock(return_value=({'20': [{'period_days': 90, 'price_kopeks': 31900}]}, {}))
    monkeypatch.setattr(api, '_discount_examples', checked)
    with pytest.raises(HTTPException) as exc:
        await patch(endpoint, enabled=True)
    assert exc.value.status_code == 422 and Settings.is_trial_ending_discount_enabled() is False
    checked.assert_awaited_once_with(endpoint, lock=True)


@pytest.mark.asyncio
async def test_text_plus_new_number_and_unsupported_old_card_field_are_rejected(endpoint):
    before = deepcopy(Settings._data)
    with pytest.raises(HTTPException) as mixed:
        await patch(endpoint, text='new text', carrier=24)
    assert mixed.value.status_code == 400
    with pytest.raises(HTTPException) as unsupported:
        await api.patch_auto_message('trial-discount', api.AutoMessagePatch(carrier=24), endpoint, None)
    assert unsupported.value.status_code == 409
    assert Settings._data == before

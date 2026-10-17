"""The deployed cabinet receives the exact IDs, numeric choices and its per-card limits."""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_auto_messages as api
from app.services.notification_settings_service import NotificationSettingsService as Settings


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'settings.json')
    monkeypatch.setattr(Settings, '_loaded', False)
    monkeypatch.setattr(Settings, '_data', {})
    monkeypatch.setattr(Settings, '_readonly', False)
    Settings.get_config()


def test_card_uses_existing_ui_contract_without_claim_or_fake_sent_type():
    entry = api.CATALOG_BY_ID['trial-ending-discount']
    assert entry['settings_key'] == 'trial_ending_discount'
    assert entry['params'] == ('discount_percent', 'carrier', 'audience', 'post_trial_hours')
    assert 'claim_type' not in entry and 'sent_type' not in entry
    assert entry['buttons'] == [{'label': '🎁 Оформить со скидкой', 'target': 'Кабинет, касса', 'tracked': False}]
    item = api._build_item(entry, {}, {}, {}, {})
    assert item.enabled is False and item.state == 'quiet'
    assert item.params == {'discount_percent': 20, 'carrier': 24, 'audience': 1, 'post_trial_hours': 6}
    assert item.limits == {
        'discount_percent': [1, 30],
        'carrier': [0, 48],
        'audience': [0, 1],
        'post_trial_hours': [1, 48],
    }
    for field in ('carrier', 'audience', 'post_trial_hours'):
        assert item.params[field] in [choice['value'] for choice in item.choices[field]]
        assert all(type(choice['value']) is int and choice['label'] for choice in item.choices[field])
    assert [choice['value'] for choice in item.choices['post_trial_hours']] == [6, 12, 24, 48]
    for field in entry['params']:
        assert hasattr(Settings, api.SETTER_NAMES['trial_ending_discount', field])
        assert hasattr(Settings, api.GETTER_NAMES['trial_ending_discount', field])
        assert api._has_numeric_change(api.AutoMessagePatch(**{field: item.params[field]}))


def test_older_cards_keep_bounds_and_absent_new_fields():
    entry = api.CATALOG_BY_ID['trial-discount']
    assert api._limits_for(entry)['discount_percent'] == [1, 50]
    dump = api._build_item(entry, {}, {}, {}, {}).model_dump()
    assert 'discount_prices' not in dump and 'choices' not in dump
    assert dump['discount_context']['enabled'] is False
    unrelated = api._build_item(api.CATALOG_BY_ID['paid-3d'], {}, {}, {}, {}).model_dump()
    assert 'discount_context' not in unrelated
    assert 'sent_count' in dump


@pytest.mark.parametrize(
    'field,value',
    [('carrier', True), ('carrier', '24'), ('audience', 1.0), ('post_trial_hours', 0), ('post_trial_hours', 49)],
)
def test_numeric_fields_are_strict_and_bounded(field, value):
    with pytest.raises(ValidationError):
        api.AutoMessagePatch(**{field: value})


def test_unknown_fields_never_silently_succeed():
    with pytest.raises(ValidationError):
        api.AutoMessagePatch(stats_since='2026-10-15', carrrier=24)


@pytest.mark.asyncio
async def test_new_discount_ceiling_is_separate_from_old_cards():
    with pytest.raises(HTTPException) as exc:
        await api._assert_discount_is_safe(AsyncMock(), 31, max_percent=30)
    assert exc.value.status_code == 422 and '30%' in exc.value.detail

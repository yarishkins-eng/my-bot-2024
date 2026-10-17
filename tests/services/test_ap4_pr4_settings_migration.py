"""The deployed five-key schema stays usable; failed saves never enable issuance."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from app.services.notification_settings_service import NotificationSettingsService as Settings


OLD = {'enabled': False, 'discount_percent': 17, 'carrier': 48, 'audience': 0, 'post_trial_hours': 12}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'settings.json')
    monkeypatch.setattr(Settings, '_data', {})
    monkeypatch.setattr(Settings, '_loaded', False)
    monkeypatch.setattr(Settings, '_readonly', False)
    return Settings


@pytest.mark.parametrize('enabled', [False, True])
def test_exact_old_schema_migrates_without_changing_values(storage, enabled):
    old = {**OLD, 'enabled': enabled}
    storage._storage_path.write_text(json.dumps({'trial_ending_discount': old, 'owner_text': 'unchanged'}))
    assert storage.get_trial_ending_discount_settings() == {**old, 'stats_since': None}
    assert storage.get_trial_ending_discount_stats_since() is None
    assert json.loads(storage._storage_path.read_text())['owner_text'] == 'unchanged'
    assert storage.set_trial_ending_discount_percent(18)
    assert storage.get_trial_ending_discount_percent() == 18
    storage._loaded = False
    assert storage.is_trial_ending_discount_enabled() is enabled


@pytest.mark.parametrize('old', [{'enabled': True}, {**OLD, 'surprise': 1}, {**OLD, 'discount_percent': 99}])
def test_partial_unknown_or_invalid_old_section_is_never_completed(storage, old):
    storage._storage_path.write_text(json.dumps({'trial_ending_discount': old}))
    assert storage.is_trial_ending_discount_enabled() is False
    assert storage._data['trial_ending_discount'] == old
    assert storage.set_trial_ending_discount_carrier(24) is False
    assert json.loads(storage._storage_path.read_text())['trial_ending_discount'] == old


def test_first_successful_enable_sets_date_once_and_failed_save_publishes_nothing(storage, monkeypatch):
    storage._storage_path.write_text(json.dumps({'trial_ending_discount': OLD}))
    before = storage.get_trial_ending_discount_settings()
    raw = storage._storage_path.read_bytes()
    original = storage._save
    monkeypatch.setattr(storage, '_save', Mock(return_value=False))
    assert storage.set_trial_ending_discount_settings(enabled=True, discount_percent=30) is False
    assert storage.get_trial_ending_discount_settings() == before
    assert storage._storage_path.read_bytes() == raw
    monkeypatch.setattr(storage, '_save', original)
    start = datetime.now(UTC)
    assert storage.set_trial_ending_discount_settings(enabled=True, discount_percent=30)
    since = storage.get_trial_ending_discount_stats_since()
    assert start <= since <= datetime.now(UTC)
    assert storage.set_trial_ending_discount_enabled(False)
    assert storage.set_trial_ending_discount_enabled(True)
    storage._loaded = False
    assert storage.get_trial_ending_discount_stats_since() == since
    assert storage.get_trial_ending_discount_percent() == 30


@pytest.mark.parametrize('date', [False, 123, 'tomorrow', '2026-10-15T17:00:00'])
def test_bad_metadata_stays_off_and_cannot_be_replaced_through_setter(storage, date):
    storage._storage_path.write_text(json.dumps({'trial_ending_discount': {**OLD, 'stats_since': date}}))
    assert storage.is_trial_ending_discount_enabled() is False
    before = deepcopy(storage._data)
    assert storage.set_trial_ending_discount_settings(stats_since=None) is False
    assert storage._data == before

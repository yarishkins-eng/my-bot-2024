"""АП-4 PR-3: a complete discount configuration is validated and saved together."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.notification_settings_service import NotificationSettingsService as Settings


KEY = 'trial_ending_discount'
DEFAULTS = {'enabled': False, 'discount_percent': 20, 'carrier': 24, 'audience': 1, 'post_trial_hours': 6}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(Settings, '_data', {})
    monkeypatch.setattr(Settings, '_loaded', False)
    monkeypatch.setattr(Settings, '_readonly', False)
    Settings.get_config()
    return Settings


def test_default_is_off_and_snapshot_cannot_mutate_storage(storage):
    assert storage._DEFAULTS[KEY] == DEFAULTS
    snapshot = storage.get_trial_ending_discount_settings()
    assert snapshot == DEFAULTS
    assert storage.is_enabled(KEY) is False
    snapshot['enabled'] = True
    assert storage.get_trial_ending_discount_settings() == DEFAULTS


def test_complete_patch_saves_once_and_survives_reload(storage, monkeypatch):
    original_save = storage._save
    save = Mock(wraps=original_save)
    monkeypatch.setattr(storage, '_save', save)
    expected = {'enabled': True, 'discount_percent': 30, 'carrier': 48, 'audience': 0, 'post_trial_hours': 1}
    assert storage.set_trial_ending_discount_settings(**expected) is True
    save.assert_called_once()
    assert json.loads(storage._storage_path.read_text())[KEY] == expected
    storage._loaded = False
    assert storage.get_trial_ending_discount_settings() == expected


@pytest.mark.parametrize(
    'field,value',
    [('discount_percent', value) for value in (0, 31, 100, True, False, 20.0, '20', None)]
    + [('carrier', value) for value in (-1, 1, 2, 23, 25, True, False, 24.0, '24', None)]
    + [('audience', value) for value in (-1, 2, True, False, 1.0, '1', None)]
    + [('post_trial_hours', value) for value in (0, -1, 49, 10**8, True, False, 6.0, '6', None, 10**100)]
    + [('enabled', value) for value in (0, 1, 'true', None)],
)
def test_invalid_patch_never_changes_memory_or_file(storage, monkeypatch, field, value):
    before = deepcopy(storage._data)
    raw = storage._storage_path.read_bytes()
    save = Mock(return_value=True)
    monkeypatch.setattr(storage, '_save', save)
    assert storage.set_trial_ending_discount_settings(**{field: value}) is False
    assert storage._data == before
    assert storage._storage_path.read_bytes() == raw
    save.assert_not_called()


@pytest.mark.parametrize('carrier', [0, 24, 48])
@pytest.mark.parametrize('percent', [1, 20, 30])
@pytest.mark.parametrize('audience', [0, 1])
def test_all_numeric_choices_and_percent_boundaries_are_accepted(storage, carrier, percent, audience):
    assert (
        storage.set_trial_ending_discount_settings(
            carrier=carrier, discount_percent=percent, audience=audience, post_trial_hours=1
        )
        is True
    )
    assert storage.get_trial_ending_discount_settings() == {
        **DEFAULTS,
        'carrier': carrier,
        'discount_percent': percent,
        'audience': audience,
        'post_trial_hours': 1,
    }


@pytest.mark.parametrize('hours', [1, 6, 12, 24, 48])
def test_hours_use_owner_approved_one_to_forty_eight_range(storage, hours):
    """Owner15.10 chose the same ceiling as cabinet choices; old unbounded contract is superseded."""
    assert storage.set_trial_ending_discount_post_trial_hours(hours) is True
    assert storage.get_trial_ending_discount_post_trial_hours() == hours


@pytest.mark.parametrize('changes', [{'carrier': 48, 'discount_percent': 31}, {'carrier': 48, 'unknown': 1}])
def test_invalid_member_cannot_partially_save_other_fields(storage, monkeypatch, changes):
    before = deepcopy(storage._data)
    save = Mock(return_value=True)
    monkeypatch.setattr(storage, '_save', save)
    assert storage.set_trial_ending_discount_settings(**changes) is False
    assert storage._data == before
    save.assert_not_called()


@pytest.mark.parametrize('path', ['named', 'generic', 'field', 'numeric'])
def test_every_update_validates_untouched_saved_values(storage, monkeypatch, path):
    storage._data[KEY]['discount_percent'] = 100
    before = deepcopy(storage._data)
    save = Mock(return_value=True)
    monkeypatch.setattr(storage, '_save', save)
    updates = {
        'named': lambda: storage.set_trial_ending_discount_enabled(True),
        'generic': lambda: storage.set_enabled(KEY, True),
        'field': lambda: storage._set_field(KEY, 'enabled', True),
        'numeric': lambda: storage.set_trial_ending_discount_carrier(48),
    }
    assert updates[path]() is False
    assert storage._data == before
    save.assert_not_called()
    assert storage.is_trial_ending_discount_enabled() is False
    assert storage.is_enabled(KEY) is False


@pytest.mark.parametrize('path', ['generic', 'field'])
def test_generic_paths_do_not_coerce_invalid_new_values(storage, path):
    update = storage.set_enabled if path == 'generic' else lambda key, value: storage._set_field(key, 'enabled', value)
    assert update(KEY, 1) is False
    assert storage._set_field(KEY, 'discount_percent', True) is False
    assert storage._set_field(KEY, 'unknown', 1) is False
    assert storage.get_trial_ending_discount_settings() == DEFAULTS


@pytest.mark.parametrize('section', [None, [], {'enabled': True}, {**DEFAULTS, 'enabled': True, 'carrier': 25}])
def test_corrupt_saved_section_is_off_and_read_does_not_repair_it(storage, monkeypatch, section):
    storage._data[KEY] = section
    before = deepcopy(storage._data)
    save = Mock(return_value=True)
    monkeypatch.setattr(storage, '_save', save)
    assert storage.get_trial_ending_discount_settings() == DEFAULTS
    assert storage.is_enabled(KEY) is False
    assert storage._data == before
    save.assert_not_called()


@pytest.mark.parametrize('field,value', [('percent', 25), ('carrier', 0), ('audience', 0), ('post_trial_hours', 12)])
def test_named_numeric_accessors_use_the_same_validated_config(storage, field, value):
    assert getattr(storage, f'set_trial_ending_discount_{field}')(value) is True
    assert getattr(storage, f'get_trial_ending_discount_{field}')() == value
    assert storage.set_trial_ending_discount_enabled(True) is True
    assert storage.is_trial_ending_discount_enabled() is True


def test_failed_replace_leaves_memory_and_file_unchanged(storage, monkeypatch):
    before = deepcopy(storage._data)
    raw = storage._storage_path.read_bytes()

    def refuse_replace(_source, _target):
        assert storage._data == before
        raise OSError('test write failure')

    monkeypatch.setattr(Path, 'replace', refuse_replace)
    assert storage.set_trial_ending_discount_settings(enabled=True, carrier=48, discount_percent=30) is False
    assert storage._data == before
    assert storage._storage_path.read_bytes() == raw


def test_readonly_file_rejects_patch_without_changing_memory(storage):
    before = deepcopy(storage._data)
    storage._readonly = True
    assert storage.set_trial_ending_discount_settings(enabled=True) is False
    assert storage._data == before


@pytest.mark.parametrize('value,expected', [(0, 1), (1, 1), (30, 30), (50, 50), (100, 50)])
def test_legacy_trial_percent_keeps_its_own_one_to_fifty_limits(storage, value, expected):
    assert storage.set_trial_expired_discount_percent(value) is True
    assert storage.get_trial_expired_discount_percent() == expected
    storage._data['trial_expired_discount']['discount_percent'] = value
    # Reading the old file preserves zero (no offer); only new writes clamp to 1..50.
    assert storage.get_trial_expired_discount_percent() == value
    assert storage.set_second_wave_discount_percent(100) is True
    assert storage.get_second_wave_discount_percent() == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('value,accepted', [('0', False), ('1', True), ('50', True), ('51', False), ('100', False)])
async def test_chat_legacy_trial_percent_enforces_its_actual_limits(storage, value, accepted):
    from app.handlers.admin.monitoring import process_notification_value_input

    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                'notification_setting_key': 'trial_expired_discount',
                'notification_setting_field': 'percent',
                'settings_language': 'ru',
            }
        ),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(text=value, from_user=SimpleNamespace(language_code='ru'), answer=AsyncMock())
    before = storage.get_trial_expired_discount_percent()
    await process_notification_value_input(message, state)
    if accepted:
        state.clear.assert_awaited_once()
        assert storage.get_trial_expired_discount_percent() == int(value)
    else:
        state.clear.assert_not_awaited()
        assert storage.get_trial_expired_discount_percent() == before
        assert '1' in message.answer.call_args.args[0] and '50' in message.answer.call_args.args[0]


@pytest.mark.parametrize(
    'config', [{'enabled': True}, {'enabled': True, 'discount_percent': 20, 'carrier': 24, 'audience': 1}]
)
def test_cold_load_cannot_complete_partial_enabled_configuration(storage, config):
    storage._storage_path.write_text(json.dumps({KEY: config}))
    storage._loaded = False
    assert storage.get_trial_ending_discount_settings()['enabled'] is False

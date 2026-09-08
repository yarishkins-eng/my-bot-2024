"""Configured languages must have their own readable locale, not a fallback."""

import pytest

from app.config import settings
from app.localization import loader
from app.utils.language import get_telegram_language


@pytest.fixture
def locale_dirs(monkeypatch, tmp_path):
    bundled = tmp_path / 'bundled'
    overrides = tmp_path / 'overrides'
    bundled.mkdir()
    overrides.mkdir()
    monkeypatch.setattr(loader, '_DEFAULT_LOCALES_DIR', bundled)
    monkeypatch.setattr(settings, 'LOCALES_PATH', str(overrides))
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'ru')
    monkeypatch.setattr(loader, 'DEFAULT_LANGUAGE', 'ru')
    monkeypatch.setattr(type(settings), 'get_available_languages', lambda self: ['ru', 'en', 'de'])
    (bundled / 'ru.json').write_text('{"WELCOME":"Russian"}')
    (bundled / 'en.json').write_text('{"WELCOME":"English"}')
    loader.clear_locale_cache()
    yield bundled, overrides
    loader.clear_locale_cache()


def test_fallback_text_does_not_make_a_missing_locale_available(locale_dirs):
    assert loader.load_locale('de') == {'WELCOME': 'Russian'}
    assert not loader.has_locale('de')
    assert get_telegram_language('de-DE') == 'ru'
    assert get_telegram_language('EN_gb') == 'en'


def test_installed_but_unconfigured_locale_is_not_selected(locale_dirs, monkeypatch):
    monkeypatch.setattr(type(settings), 'get_available_languages', lambda self: ['ru'])
    assert loader.has_locale('en')
    assert get_telegram_language('en') == 'ru'


def test_custom_locale_is_available_without_a_bundled_file(locale_dirs):
    _, overrides = locale_dirs
    (overrides / 'de.json').write_text('{"WELCOME":"German"}')
    assert get_telegram_language('de-DE') == 'de'


@pytest.mark.parametrize('content', ['{}', '["invalid structure"]', 'invalid JSON'])
def test_unusable_custom_locale_is_excluded(locale_dirs, content):
    _, overrides = locale_dirs
    (overrides / 'de.json').write_text(content)
    assert get_telegram_language('de') == 'ru'


def test_locale_registry_is_refreshed_with_translation_cache(locale_dirs):
    _, overrides = locale_dirs
    assert get_telegram_language('de') == 'ru'
    (overrides / 'de.json').write_text('{"WELCOME":"German"}')
    loader.clear_locale_cache()
    assert get_telegram_language('de') == 'de'


def test_default_outside_effective_allowlist_is_not_returned(locale_dirs, monkeypatch):
    monkeypatch.setattr(settings, 'DEFAULT_LANGUAGE', 'de')
    assert get_telegram_language(None) == 'ru'


def test_no_installed_configured_locale_prevents_language_assignment(locale_dirs, monkeypatch):
    monkeypatch.setattr(type(settings), 'get_available_languages', lambda self: ['de'])
    with pytest.raises(ValueError, match='No configured languages'):
        get_telegram_language('de')

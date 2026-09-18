"""The initial language is an allowed locale, even for absent or malformed hints."""

import pytest

from app.utils.language import resolve_telegram_language


@pytest.mark.parametrize(
    ('hint', 'expected'),
    [
        ('ru', 'ru'),
        ('ru-RU', 'ru'),
        (' RU_ru ', 'ru'),
        ('en', 'en'),
        ('en-US', 'en'),
        ('EN_gb', 'en'),
        ('en-001', 'en'),
        ('uk', 'ru'),
        ('zh-Hans', 'ru'),
        ('fa-IR', 'ru'),
        ('de', 'ru'),
        (None, 'ru'),
        ('', 'ru'),
        (' ', 'ru'),
        ('en--GB', 'ru'),
        ('en@x', 'ru'),
        ('-en', 'ru'),
        ('en-', 'ru'),
        ('e n', 'ru'),
        ('en-123456789', 'ru'),
        ('english', 'ru'),
        ('еn', 'ru'),  # Cyrillic e is not an ASCII language tag.
        (123, 'ru'),
        (False, 'ru'),
        ({'language': 'en'}, 'ru'),
        (['en'], 'ru'),
    ],
)
def test_production_language_matrix(hint, expected):
    assert resolve_telegram_language(hint, available_languages={'ru', 'en'}, default_language='ru') == expected


@pytest.mark.parametrize('hint', ['ru--RU', 'ru@x', '-ru', 'ru ru', '', None])
def test_malformed_russian_tag_uses_default_instead_of_prefix(hint):
    assert resolve_telegram_language(hint, available_languages={'ru', 'en'}, default_language='en') == 'en'


@pytest.mark.parametrize(
    ('hint', 'available', 'default', 'expected'),
    [
        ('uk-UA', {'ua', 'en'}, 'en', 'ua'),
        ('uk', {'ru', 'en'}, 'ru', 'ru'),
        ('uk', {'uk', 'ua', 'en'}, 'en', 'uk'),
        ('de', {'ru', 'en'}, ' EN_gb ', 'en'),
        (None, {'ua', 'en'}, 'UK_ua', 'ua'),
        (None, {'ru', 'en'}, 'ru@x', 'ru'),
        (None, {'en', 'fa'}, None, 'en'),
        (None, {'zh', 'fa', 'ua'}, 'xx', 'fa'),
        ('EN_us', {' RU ', 'EN'}, None, 'en'),
    ],
)
def test_aliases_defaults_and_deterministic_fallback(hint, available, default, expected):
    assert resolve_telegram_language(hint, available_languages=available, default_language=default) == expected


@pytest.mark.parametrize('available', [[], set(), ['en-US', '', '../ru', None]])
def test_empty_canonical_allowlist_is_an_explicit_configuration_error(available):
    with pytest.raises(ValueError, match='No configured languages'):
        resolve_telegram_language('en', available_languages=available, default_language='ru')


def test_fallback_does_not_depend_on_allowlist_order():
    assert {
        resolve_telegram_language(None, available_languages=order, default_language=None)
        for order in [('zh', 'fa', 'ua'), ('ua', 'zh', 'fa'), ('fa', 'ua', 'zh')]
    } == {'fa'}

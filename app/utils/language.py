"""Initial Telegram language selection; saved user preferences take precedence."""

import re
from collections.abc import Collection


_LANGUAGE_TAG = re.compile(r'[a-z]{2,3}(?:-[a-z0-9]{1,8})*', re.ASCII)
_LOCALE_CODE = re.compile(r'[a-z]{2,3}', re.ASCII)


def _base_language(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace('_', '-')
    if not _LANGUAGE_TAG.fullmatch(normalized):
        return None
    return normalized.split('-', 1)[0]


def _canonical_locales(values: Collection[str]) -> set[str]:
    return {
        value.strip().lower()
        for value in values
        if isinstance(value, str) and _LOCALE_CODE.fullmatch(value.strip().lower())
    }


def resolve_telegram_language(
    language_code: str | None,
    *,
    available_languages: Collection[str],
    default_language: str | None,
) -> str:
    """Resolve an optional Telegram tag against a validated, installed allowlist.

    This function performs no I/O and does not read application settings. Callers
    must apply it only to new accounts or the first claim of a username-only
    phantom, never to overwrite an existing account's language.
    """
    available = _canonical_locales(available_languages)
    if not available:
        raise ValueError('No configured languages have an available locale')

    for candidate in (language_code, default_language):
        base = _base_language(candidate)
        if base in available:
            return base
        if base == 'uk' and 'ua' in available:
            return 'ua'

    for fallback in ('ru', 'en'):
        if fallback in available:
            return fallback
    return min(available)


def get_telegram_language(language_code: str | None) -> str:
    """Resolve the initial language using configured, directly loadable locales."""
    from app.config import settings
    from app.localization.loader import has_locale

    configured = _canonical_locales(settings.get_available_languages())
    available = {code for code in configured if has_locale(code)}
    return resolve_telegram_language(
        language_code,
        available_languages=available,
        default_language=settings.DEFAULT_LANGUAGE,
    )

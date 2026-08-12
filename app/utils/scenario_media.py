"""Scenario-specific Telegram images with a neutral fallback for other languages."""

from pathlib import Path

import structlog
from aiogram.types import FSInputFile, Message

from app.utils.message_patch import _cache_logo_file_id, get_logo_media


logger = structlog.get_logger(__name__)

_ASSETS_DIR = Path('assets/telegram')
_SCENARIO_FILENAMES = {
    'tariffs': 'tariffs.jpg',
    'trial_active': 'trial-active.jpg',
}
_scenario_file_ids: dict[str, str] = {}


def _is_russian(language: str | None) -> bool:
    return (language or '').split('-', 1)[0].lower() == 'ru'


def _asset_path(media_key: str) -> Path | None:
    filename = _SCENARIO_FILENAMES.get(media_key)
    return _ASSETS_DIR / filename if filename else None


def get_scenario_media(media_key: str, language: str | None):
    """Return the requested Russian card, otherwise the neutral brand image.

    A Russian text card must never be shown in the English interface.  Missing
    assets fail safely to the global neutral image (and finally to text-only
    delivery if that image is unavailable as well).
    """
    if not _is_russian(language):
        return get_logo_media()

    path = _asset_path(media_key)
    if path is None:
        logger.warning('Unknown scenario media key', media_key=media_key)
        return get_logo_media()
    if not path.is_file():
        logger.warning(
            'Scenario media asset is unavailable; using neutral fallback', media_key=media_key, path=str(path)
        )
        return get_logo_media()

    return _scenario_file_ids.get(media_key) or FSInputFile(path)


def cache_scenario_media_file_id(media_key: str, language: str | None, message: Message | None) -> None:
    """Cache Telegram ``file_id`` after the first successful scenario image send."""
    if not _is_russian(language):
        _cache_logo_file_id(message)
        return

    path = _asset_path(media_key)
    if path is None or not path.is_file() or message is None or not getattr(message, 'photo', None):
        return
    _scenario_file_ids.setdefault(media_key, message.photo[-1].file_id)

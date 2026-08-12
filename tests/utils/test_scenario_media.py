from types import SimpleNamespace

from aiogram.types import FSInputFile

from app.utils import scenario_media


def test_russian_tariffs_card_uses_the_dedicated_asset() -> None:
    scenario_media._scenario_file_ids.clear()

    media = scenario_media.get_scenario_media('tariffs', 'ru')

    assert isinstance(media, FSInputFile)
    assert str(media.path).endswith('assets/telegram/tariffs.jpg')


def test_english_interface_uses_neutral_fallback(monkeypatch) -> None:
    neutral_media = object()
    monkeypatch.setattr(scenario_media, 'get_logo_media', lambda: neutral_media)

    assert scenario_media.get_scenario_media('tariffs', 'en') is neutral_media


def test_successful_russian_send_caches_file_id() -> None:
    scenario_media._scenario_file_ids.clear()
    message = SimpleNamespace(photo=[SimpleNamespace(file_id='telegram-file-id')])

    scenario_media.cache_scenario_media_file_id('trial_active', 'ru', message)

    assert scenario_media.get_scenario_media('trial_active', 'ru') == 'telegram-file-id'

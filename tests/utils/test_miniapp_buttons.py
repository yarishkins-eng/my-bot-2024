from pathlib import Path

from app.cabinet.utils.links import get_campaign_web_link
from app.config import settings
from app.utils.miniapp_buttons import build_cabinet_url


def test_build_cabinet_url_keeps_a_version_query_after_deep_link(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        'MINIAPP_CUSTOM_URL',
        'https://cabinet.example.com:8443?release=cabinet-theme-bg-remount-1',
        raising=False,
    )

    assert build_cabinet_url('/') == 'https://cabinet.example.com:8443?release=cabinet-theme-bg-remount-1'
    assert (
        build_cabinet_url('/balance') == 'https://cabinet.example.com:8443/balance?release=cabinet-theme-bg-remount-1'
    )
    assert (
        build_cabinet_url('connection')
        == 'https://cabinet.example.com:8443/connection?release=cabinet-theme-bg-remount-1'
    )


def test_build_cabinet_url_preserves_existing_plain_url_contract(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.com/', raising=False)

    assert build_cabinet_url('/') == 'https://cabinet.example.com'
    assert build_cabinet_url('/admin/tickets/7') == 'https://cabinet.example.com/admin/tickets/7'


def test_build_cabinet_url_keeps_a_base_path_and_fragment(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        'MINIAPP_CUSTOM_URL',
        'https://cabinet.example.com/app/?release=cabinet-theme-bg-remount-1#telegram',
        raising=False,
    )

    assert build_cabinet_url('/') == 'https://cabinet.example.com/app?release=cabinet-theme-bg-remount-1#telegram'
    assert (
        build_cabinet_url('/connection')
        == 'https://cabinet.example.com/app/connection?release=cabinet-theme-bg-remount-1#telegram'
    )


def test_campaign_link_keeps_the_version_query(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        'CABINET_URL',
        'https://cabinet.example.com:8443?release=cabinet-theme-bg-remount-1',
        raising=False,
    )

    assert (
        get_campaign_web_link('summer sale')
        == 'https://cabinet.example.com:8443?release=cabinet-theme-bg-remount-1&campaign=summer+sale'
    )


def test_custom_connection_launchers_all_use_the_url_builder() -> None:
    project = Path(__file__).parents[2]
    direct_expression = "MINIAPP_CUSTOM_URL.rstrip('/') + '/connection'"
    launchers = [
        project / 'app/keyboards/inline.py',
        project / 'app/handlers/subscription/purchase.py',
        project / 'app/handlers/subscription/links.py',
    ]

    for launcher in launchers:
        assert direct_expression not in launcher.read_text(encoding='utf-8')

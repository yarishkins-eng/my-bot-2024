"""Регрессии доступа к кнопке «Моя ссылка для подключения»."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import status as subscription_status
from app.config import settings
from app.handlers.subscription import links
from app.keyboards.inline import build_funnel_menu_keyboard
from app.localization.texts import get_texts
from app.utils.funnel_state import FunnelState, classify_funnel_state
from app.utils.subscription_link_access import (
    get_user_subscription_with_available_link,
    has_active_subscription_connection,
    has_available_subscription_link,
)


def _configure_link_access(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hidden: bool = False,
    multi_tariff: bool = False,
) -> None:
    settings_cls = type(settings)
    monkeypatch.setattr(settings_cls, 'should_hide_subscription_link', lambda self: hidden, raising=False)
    monkeypatch.setattr(settings_cls, 'is_multi_tariff_enabled', lambda self: multi_tariff, raising=False)
    monkeypatch.setattr(settings_cls, 'is_happ_cryptolink_mode', lambda self: False, raising=False)


def _subscription(
    status: str = 'active',
    *,
    url: str | None = 'https://example.invalid/subscription',
    is_trial: bool = False,
    in_grace: bool = False,
    end_date: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        actual_status=status,
        subscription_url=url,
        subscription_crypto_link=None,
        is_trial=is_trial,
        in_grace=in_grace,
        grace_until=datetime.now(UTC) + timedelta(days=1) if in_grace else None,
        end_date=end_date or datetime.now(UTC) + timedelta(days=1),
    )


@pytest.mark.parametrize(
    ('status', 'is_trial'),
    [
        ('active', True),  # обычный триал и бесплатный Premium/Team после relabel
        ('trial', True),
        ('active', False),  # обычная платная подписка
        ('limited', True),  # ссылка не обходит трафик, но устройство можно настроить
        ('limited', False),
    ],
)
def test_link_is_available_for_every_live_subscription_type(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    is_trial: bool,
) -> None:
    _configure_link_access(monkeypatch)

    assert has_available_subscription_link(_subscription(status, is_trial=is_trial))


def test_link_is_available_during_paid_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch)

    assert has_available_subscription_link(_subscription('expired', in_grace=True))


def test_disabled_subscription_does_not_keep_link_during_stale_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch)

    assert not has_available_subscription_link(_subscription('disabled', in_grace=True))


def test_limited_subscription_past_its_end_date_has_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch)
    subscription = _subscription('limited', end_date=datetime.now(UTC) - timedelta(seconds=1))

    assert not has_active_subscription_connection(subscription)
    assert not has_available_subscription_link(subscription)


@pytest.mark.parametrize('status', ['expired', 'disabled', 'pending'])
def test_link_is_not_available_without_current_vpn_access(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    _configure_link_access(monkeypatch)

    assert not has_available_subscription_link(_subscription(status))


def test_link_is_not_available_before_panel_generates_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch)

    assert not has_available_subscription_link(_subscription(url=None))


def test_hidden_link_setting_hides_button_and_blocks_old_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch, hidden=True)

    assert not has_available_subscription_link(_subscription())


def test_multi_tariff_never_selects_an_arbitrary_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch, multi_tariff=True)
    user = SimpleNamespace(subscription=_subscription())

    assert get_user_subscription_with_available_link(user) is None


def test_paid_history_does_not_hide_an_active_free_relabel(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_link_access(monkeypatch)
    subscription = _subscription('active', is_trial=True)
    user = SimpleNamespace(
        subscription=subscription,
        subscriptions=[subscription],
        has_had_paid_subscription=True,
    )

    assert classify_funnel_state(user) == FunnelState.TRIAL_ACTIVE


def _callbacks(keyboard) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_trial_menu_places_connection_link_above_referral() -> None:
    keyboard = build_funnel_menu_keyboard(
        FunnelState.TRIAL_ACTIVE,
        'ru',
        get_texts('ru'),
        show_connection_link=True,
    )
    callbacks = _callbacks(keyboard)

    assert 'open_subscription_link' in callbacks
    if 'menu_referrals' in callbacks:
        assert callbacks.index('open_subscription_link') < callbacks.index('menu_referrals')


def test_trial_menu_omits_connection_link_without_access() -> None:
    keyboard = build_funnel_menu_keyboard(
        FunnelState.TRIAL_ACTIVE,
        'ru',
        get_texts('ru'),
        show_connection_link=False,
    )

    assert 'open_subscription_link' not in _callbacks(keyboard)


@pytest.mark.anyio('asyncio')
async def test_stale_callback_cannot_reveal_expired_subscription_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_link_access(monkeypatch)
    callback = SimpleNamespace(answer=AsyncMock())
    monkeypatch.setattr(links, '_resolve_subscription', AsyncMock(return_value=(_subscription('expired'), 1)))

    await links.handle_open_subscription_link(callback, SimpleNamespace(language='ru'), SimpleNamespace())

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.anyio('asyncio')
async def test_stale_connect_callback_cannot_open_expired_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_link_access(monkeypatch)
    callback = SimpleNamespace(
        data='subscription_connect',
        answer=AsyncMock(),
        message=SimpleNamespace(),
    )
    monkeypatch.setattr(links, '_resolve_subscription', AsyncMock(return_value=(_subscription('expired'), 1)))

    await links.handle_connect_subscription(callback, SimpleNamespace(language='ru'), SimpleNamespace())

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.parametrize('connect_mode', ['link', 'miniapp_subscription'])
@pytest.mark.anyio('asyncio')
async def test_hidden_link_blocks_stale_callback_that_would_embed_raw_url(
    monkeypatch: pytest.MonkeyPatch,
    connect_mode: str,
) -> None:
    _configure_link_access(monkeypatch, hidden=True)
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', connect_mode)
    callback = SimpleNamespace(
        data='subscription_connect',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    monkeypatch.setattr(links, '_resolve_subscription', AsyncMock(return_value=(_subscription(), 1)))

    await links.handle_connect_subscription(callback, SimpleNamespace(language='ru'), SimpleNamespace())

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs['show_alert'] is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_connection_link_endpoint_rejects_expired_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_link_access(monkeypatch)
    monkeypatch.setattr(
        subscription_status,
        'resolve_subscription',
        AsyncMock(return_value=_subscription('expired')),
    )

    with pytest.raises(HTTPException) as error:
        await subscription_status.get_connection_link(user=SimpleNamespace(), db=SimpleNamespace())

    assert error.value.status_code == 404


@pytest.mark.parametrize('subscription_status_name', ['expired', 'disabled', 'pending'])
@pytest.mark.anyio('asyncio')
async def test_app_config_endpoint_rejects_subscription_without_access(
    monkeypatch: pytest.MonkeyPatch,
    subscription_status_name: str,
) -> None:
    _configure_link_access(monkeypatch)
    monkeypatch.setattr(
        subscription_status,
        'resolve_subscription',
        AsyncMock(return_value=_subscription(subscription_status_name)),
    )

    with pytest.raises(HTTPException) as error:
        await subscription_status.get_app_config(user=SimpleNamespace(), db=SimpleNamespace())

    assert error.value.status_code == 404


@pytest.mark.anyio('asyncio')
async def test_app_config_never_returns_raw_url_when_link_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_link_access(monkeypatch, hidden=True)
    subscription = _subscription()
    subscription.subscription_crypto_link = 'happ://crypt5/opaque-config'
    monkeypatch.setattr(subscription_status, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscription_status, '_load_app_config_async', AsyncMock(return_value={'platforms': {}}))

    config = await subscription_status.get_app_config(user=SimpleNamespace(), db=SimpleNamespace())

    assert config['subscriptionUrl'] is None
    assert config['subscriptionCryptoLink'] == subscription.subscription_crypto_link
    assert config['hasSubscription'] is True

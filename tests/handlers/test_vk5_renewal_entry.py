"""VK-5 renewal uses checkout admission, keeps its fallback and daily guard."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.subscription import device_first, purchase, tariff_purchase
from app.keyboards.inline import build_funnel_menu_keyboard
from app.localization.texts import get_texts
from app.utils.funnel_state import FunnelState


@pytest.mark.asyncio
@pytest.mark.parametrize('is_trial', [False, True])
@pytest.mark.parametrize('admitted', [False, True])
async def test_renewal_enters_checkout_and_false_returns_to_tariff_extend(monkeypatch, is_trial, admitted):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(type(settings), 'is_tariffs_mode', lambda self: True)
    sub = SimpleNamespace(tariff_id=3, is_trial=is_trial)
    user = SimpleNamespace(language='ru', subscription=sub)
    callback = SimpleNamespace(message=SimpleNamespace())
    db, state = AsyncMock(), AsyncMock()
    entry, legacy = AsyncMock(return_value=admitted), AsyncMock()
    monkeypatch.setattr(device_first, 'show_device_first_entry', entry)
    monkeypatch.setattr(tariff_purchase, 'show_tariff_extend', legacy)
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(is_daily=False))
    )
    await purchase.handle_extend_subscription(callback, user, db, state)
    entry.assert_awaited_once_with(callback, user, db, state, origin_callback='back_to_menu')
    if admitted:
        legacy.assert_not_awaited()
    else:
        legacy.assert_awaited_once_with(callback, user, db)


@pytest.mark.asyncio
async def test_daily_subscription_keeps_its_resume_screen_before_checkout(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    sub = SimpleNamespace(tariff_id=3, is_trial=False)
    user = SimpleNamespace(language='ru', subscription=sub)
    callback, db, state = SimpleNamespace(message=SimpleNamespace()), AsyncMock(), AsyncMock()
    entry, legacy, info = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(device_first, 'show_device_first_entry', entry)
    monkeypatch.setattr(tariff_purchase, 'show_tariff_extend', legacy)
    monkeypatch.setattr(purchase, 'show_subscription_info', info)
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=SimpleNamespace(is_daily=True))
    )
    await purchase.handle_extend_subscription(callback, user, db, state)
    info.assert_awaited_once_with(callback, user, db)
    entry.assert_not_awaited()
    legacy.assert_not_awaited()


@pytest.mark.parametrize('state', [FunnelState.PAID_EXPIRING, FunnelState.PAID_EXPIRED])
@pytest.mark.parametrize('url', ['https://example.test', ''])
def test_paid_menu_opens_checkout_or_preserves_callback(monkeypatch, state, url):
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', url)
    monkeypatch.setattr(type(settings), 'is_referral_program_enabled', lambda self: False)
    keyboard = build_funnel_menu_keyboard(state, 'ru', get_texts('ru'))
    button = keyboard.inline_keyboard[0][0]
    if url:
        assert button.web_app.url == url + '/subscription/purchase'
        assert button.callback_data is None
    else:
        assert button.web_app is None
        assert button.callback_data == 'subscription_extend'

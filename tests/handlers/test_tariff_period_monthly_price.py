"""Regression tests for effective monthly prices in tariff period buttons."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.subscription import tariff_purchase
from app.services.pricing_engine import pricing_engine


def _button_texts(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _tariff(*, tariff_id: int = 7, prices: dict[str, int] | None = None) -> MagicMock:
    tariff = MagicMock()
    tariff.id = tariff_id
    tariff.period_prices = prices or {'30': 24900, '90': 69000, '180': 124000, '365': 199000}
    tariff.device_limit = 2
    tariff.device_price_kopeks = 8000
    tariff.min_traffic_gb = 10
    return tariff


@pytest.mark.asyncio
async def test_regular_tariff_periods_use_dynamic_monthly_price_rounded_up(monkeypatch):
    async def price_from_tariff(tariff, period_days, _user, **_kwargs):
        return tariff.period_prices[str(period_days)], 0

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', price_from_tariff)
    keyboard = await tariff_purchase.get_tariff_periods_keyboard(_tariff(), 'ru')

    assert _button_texts(keyboard) == [
        '30 дней — 249 ₽',
        '90 дней — 690 ₽ (230 ₽/мес.)',
        '180 дней — 1240 ₽ (207 ₽/мес.)',
        '365 дней — 1990 ₽ (166 ₽/мес.)',
        '⬅️ Назад',
    ]
    assert [button.callback_data for row in keyboard.inline_keyboard for button in row][:-1] == [
        'tariff_period:7:30',
        'tariff_period:7:90',
        'tariff_period:7:180',
        'tariff_period:7:365',
    ]


@pytest.mark.asyncio
async def test_monthly_price_uses_discounted_total_and_english_localization(monkeypatch):
    user = MagicMock()

    async def discounted_price(*_args, **_kwargs):
        return 62100, 10

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', discounted_price)

    keyboard = await tariff_purchase.get_tariff_periods_keyboard(
        _tariff(prices={'90': 69000}),
        'en',
        db_user=user,
    )

    assert _button_texts(keyboard)[0] == '90 days — 621 ₽ 🔥−10% (207 ₽/mo.)'


@pytest.mark.asyncio
async def test_configurable_traffic_shows_minimum_configuration_price(monkeypatch):
    async def minimum_configuration_price(_tariff, _period, _user, **kwargs):
        assert kwargs['custom_traffic_gb'] == 10
        return 72000, 0

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', minimum_configuration_price)
    keyboard = await tariff_purchase.get_tariff_periods_keyboard_with_traffic(
        _tariff(prices={'90': 69000}),
        'ru',
    )

    assert _button_texts(keyboard)[0] == '90 дней — от 720 ₽ (от 240 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_period_traffic:7:90'


@pytest.mark.asyncio
async def test_regular_period_uses_existing_same_tariff_device_limit(monkeypatch):
    async def price_with_existing_devices(_tariff, _period, _user, **kwargs):
        assert kwargs['device_limit'] == 3
        return 172000, 0

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', price_with_existing_devices)
    keyboard = await tariff_purchase.get_tariff_periods_keyboard(
        _tariff(prices={'180': 124000}),
        'ru',
        subscription_device_limit=3,
    )

    assert _button_texts(keyboard)[0] == '180 дней — 1720 ₽ (287 ₽/мес.)'


@pytest.mark.asyncio
async def test_purchase_confirmation_uses_same_existing_device_limit_as_period_button(monkeypatch):
    tariff = SimpleNamespace(
        id=7,
        is_active=True,
        name='Базовый',
        traffic_limit_gb=0,
        device_limit=2,
        allowed_squads=[],
    )
    existing_subscription = SimpleNamespace(id=22, tariff_id=7, device_limit=3)
    pricing_result = SimpleNamespace(final_total=172000, original_total=172000)
    callback = SimpleNamespace(
        data='tariff_period:7:180',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    user = SimpleNamespace(id=1, balance_kopeks=200000, language='ru')
    state = SimpleNamespace(update_data=AsyncMock())

    monkeypatch.setattr(type(tariff_purchase.settings), 'is_multi_tariff_enabled', lambda _settings: False)
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_user_id', AsyncMock(return_value=existing_subscription))
    pricing_mock = AsyncMock(return_value=pricing_result)
    monkeypatch.setattr(pricing_engine, 'calculate_tariff_purchase_price', pricing_mock)

    await tariff_purchase.select_tariff_period(callback, user, MagicMock(), state)

    pricing_mock.assert_awaited_once_with(tariff, 180, device_limit=3, user=user)
    confirmation_text = callback.message.edit_text.await_args.args[0]
    assert '📱 Устройств: 3' in confirmation_text
    assert '💰 <b>Итого: 1720 ₽</b>' in confirmation_text


@pytest.mark.asyncio
async def test_renewal_monthly_price_includes_extra_devices_and_rounds_up(monkeypatch):
    async def price_with_extra_device(_tariff, _period, _user, **kwargs):
        assert kwargs['device_limit'] == 3
        return 172000, 0

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', price_with_extra_device)
    keyboard = await tariff_purchase.get_tariff_extend_keyboard(
        _tariff(prices={'180': 124000}),
        'ru',
        subscription_device_limit=3,
        subscription_id=22,
    )

    # 1240 ₽ for the tariff + 80 ₽ × 6 months for one extra device = 1720 ₽;
    # 1720 / 6 = 286.66..., therefore the compact display is 287 ₽/month.
    assert _button_texts(keyboard)[0] == '180 дней — 1720 ₽ (287 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_extend:22:7:180'


@pytest.mark.asyncio
async def test_tariff_switch_uses_same_monthly_price_and_keeps_callback_format(monkeypatch):
    async def full_new_tariff_price(_tariff, _period, _user, **kwargs):
        assert kwargs == {}
        return 199000, 0

    monkeypatch.setattr(tariff_purchase, '_calculate_tariff_period_display_price', full_new_tariff_price)
    keyboard = await tariff_purchase.get_tariff_switch_periods_keyboard(
        _tariff(prices={'365': 199000}),
        'ru',
    )

    assert _button_texts(keyboard)[0] == '365 дней — 1990 ₽ (166 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_sw_period:7:365'

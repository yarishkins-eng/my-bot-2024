"""Regression tests for effective monthly prices in tariff period buttons."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.handlers.subscription import tariff_purchase


def _button_texts(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _tariff(*, tariff_id: int = 7, prices: dict[str, int] | None = None) -> MagicMock:
    tariff = MagicMock()
    tariff.id = tariff_id
    tariff.period_prices = prices or {'30': 24900, '90': 69000, '180': 124000, '365': 199000}
    tariff.device_limit = 2
    tariff.device_price_kopeks = 8000
    return tariff


def test_regular_tariff_periods_use_dynamic_monthly_price_rounded_up():
    keyboard = tariff_purchase.get_tariff_periods_keyboard(_tariff(), 'ru')

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


def test_monthly_price_uses_discounted_total_and_english_localization(monkeypatch):
    user = MagicMock()
    monkeypatch.setattr(tariff_purchase, '_get_user_period_discount', lambda _user, _days: (10, 0, 10))

    keyboard = tariff_purchase.get_tariff_periods_keyboard(
        _tariff(prices={'90': 69000}),
        'en',
        db_user=user,
    )

    assert _button_texts(keyboard)[0] == '90 дней — 621 ₽ 🔥−10% (207 ₽/mo.)'


def test_configurable_traffic_shows_monthly_price_as_a_starting_price():
    keyboard = tariff_purchase.get_tariff_periods_keyboard_with_traffic(
        _tariff(prices={'90': 69000}),
        'ru',
    )

    assert _button_texts(keyboard)[0] == '90 дней — 690 ₽ (от 230 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_period_traffic:7:90'


def test_renewal_monthly_price_includes_extra_devices_and_rounds_up():
    keyboard = tariff_purchase.get_tariff_extend_keyboard(
        _tariff(prices={'180': 124000}),
        'ru',
        subscription_device_limit=3,
        subscription_id=22,
    )

    # 1240 ₽ for the tariff + 80 ₽ × 6 months for one extra device = 1720 ₽;
    # 1720 / 6 = 286.66..., therefore the compact display is 287 ₽/month.
    assert _button_texts(keyboard)[0] == '180 дней — 1720 ₽ (287 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_extend:22:7:180'


def test_tariff_switch_uses_same_monthly_price_and_keeps_callback_format():
    keyboard = tariff_purchase.get_tariff_switch_periods_keyboard(
        _tariff(prices={'365': 199000}),
        'ru',
    )

    assert _button_texts(keyboard)[0] == '365 дней — 1990 ₽ (166 ₽/мес.)'
    assert keyboard.inline_keyboard[0][0].callback_data == 'tariff_sw_period:7:365'

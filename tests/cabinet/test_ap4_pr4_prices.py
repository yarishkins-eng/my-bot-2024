from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_auto_messages as api
from app.config import settings
from app.database.crud import promo_group as groups, tariff as tariffs
from app.database.models import PromoGroup, Tariff
from app.services import device_first_checkout_service as checkout
from app.services.notification_settings_service import NotificationSettingsService as Settings


@pytest.fixture
def price_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'settings.json')
    monkeypatch.setattr(Settings, '_loaded', False)
    monkeypatch.setattr(Settings, '_data', {})
    monkeypatch.setattr(Settings, '_readonly', False)
    Settings.get_config()
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _: False)
    monkeypatch.setattr(checkout, 'device_first_new_checkouts_enabled', lambda: True)
    tariff = Tariff(
        id=3,
        is_active=True,
        is_daily=False,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
        device_limit=2,
        max_device_limit=5,
        device_purchase_options=[2, 3],
        device_price_kopeks=5000,
        period_prices={'30': 14900, '90': 39900},
        allowed_promo_groups=[],
    )
    group = PromoGroup(id=1, is_default=True, period_discounts={}, device_discount_percent=0)
    monkeypatch.setattr(groups, 'get_default_promo_group', AsyncMock(return_value=group))
    monkeypatch.setattr(tariffs, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(tariffs, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5)))
    return tariff, group


@pytest.mark.asyncio
@pytest.mark.parametrize('group_percent', [0, 5])
async def test_every_percent_has_its_own_real_thirty_day_price(price_env, group_percent):
    tariff, group = price_env
    group.period_discounts = {'30': group_percent, '90': group_percent}
    prices, options = await api._discount_examples(AsyncMock())
    assert set(prices) == {str(p) for p in range(1, 31)}
    for percent in range(1, 31):
        for row in prices[str(percent)]:
            base = tariff.period_prices[str(row['period_days'])]
            after_group = base - base * group_percent // 100
            expected = max(100, (after_group - after_group * percent // 100) // 100 * 100)
            assert row['price_kopeks'] == expected
        assert any(row['period_days'] == 30 for row in prices[str(percent)])
    month = options['price_matrix'][0]['prices'][0]
    assert month['price_kopeks'] == (11900 if not group_percent else 11300)
    assert options['current_subscription'] == {'is_trial': True, 'tariff_id': 5}
    assert 'promo_offer_discount_percent' not in tariff.__dict__
    assert Settings.is_trial_ending_discount_enabled() is False


@pytest.mark.asyncio
async def test_quote_guard_refreshes_locked_group_and_tariff(price_env):
    tariff, group = price_env
    db = AsyncMock()
    prices, _ = await api._discount_examples(db, lock=True)
    assert prices['30'][0]['price_kopeks'] == 10400
    db.refresh.assert_any_await(group, with_for_update=True)
    db.refresh.assert_any_await(tariff, with_for_update=True)
    db.refresh.assert_any_await(tariff, attribute_names=['allowed_promo_groups'])


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['access', 'default_group'])
async def test_locked_refresh_cannot_enable_a_stale_default_group_quote(price_env, changed):
    tariff, group = price_env

    async def refresh(obj, **_kwargs):
        if changed == 'access' and obj is tariff:
            tariff.allowed_promo_groups = [PromoGroup(id=999)]
        elif changed == 'default_group' and obj is group:
            group.is_default = False

    db = AsyncMock()
    db.refresh.side_effect = refresh
    assert await api._discount_examples(db, lock=True) == ({}, {})


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['no_month', 'multiple', 'disabled', 'free'])
async def test_unavailable_cashier_never_invents_price(price_env, monkeypatch, reason):
    tariff, _ = price_env
    if reason == 'no_month':
        tariff.period_prices = {'90': 39900}
    elif reason == 'multiple':
        monkeypatch.setattr(tariffs, 'get_tariffs_for_user', AsyncMock(return_value=[tariff, tariff]))
    elif reason == 'disabled':
        monkeypatch.setattr(checkout, 'device_first_new_checkouts_enabled', lambda: False)
    else:
        tariff.period_prices = {'30': 0, '90': 0}
    assert await api._discount_examples(AsyncMock()) == ({}, {})


@pytest.mark.asyncio
async def test_non_positive_other_period_rejects_whole_cashier(price_env, monkeypatch):
    tariff, group = price_env
    group.period_discounts = {'90': 100}
    monkeypatch.setattr(checkout, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(checkout, 'is_device_first_canary_user', lambda _: True)
    person = SimpleNamespace(
        id=1,
        balance_kopeks=0,
        promo_group=group,
        promo_offer_discount_percent=20,
        promo_offer_discount_source='trial_ending_discount',
        promo_offer_discount_expires_at=datetime.now(UTC) + timedelta(days=1),
        has_had_paid_subscription=False,
    )
    assert await checkout.build_purchase_options(AsyncMock(), person, subscription=None) == {
        'eligible': False,
        'reason': 'non_positive_quote',
    }
    assert await api._discount_examples(AsyncMock()) == ({}, {})

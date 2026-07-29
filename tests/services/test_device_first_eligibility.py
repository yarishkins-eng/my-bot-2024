from types import SimpleNamespace

import pytest

from app.services.device_first_eligibility import (
    DeviceFirstConfigurationError,
    device_options_for_subscription,
    normalize_device_purchase_options,
    resolve_single_eligible_tariff,
    tariff_eligibility,
)


def tariff(**overrides):
    values = {
        'id': 7,
        'is_active': True,
        'is_daily': False,
        'custom_days_enabled': False,
        'custom_traffic_enabled': False,
        'period_prices': {'30': 99000, '90': 249000},
        'device_limit': 2,
        'device_price_kopeks': 5000,
        'max_device_limit': 5,
        'device_purchase_options': [2, 3, 5],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ('raw', 'message'),
    [
        ([], 'non-empty'),
        ([2, 2], 'unique'),
        ([3, 2], 'strictly increasing'),
        ([1, 3], 'include'),
        ([2, 6], 'max_device_limit'),
    ],
)
def test_device_option_contract_rejects_invalid_admin_values(raw, message):
    with pytest.raises(DeviceFirstConfigurationError, match=message):
        normalize_device_purchase_options(
            raw,
            base_device_limit=2,
            max_device_limit=5,
            device_price_kopeks=5000,
        )


def test_above_base_requires_tariff_own_positive_device_price():
    with pytest.raises(DeviceFirstConfigurationError, match='own positive'):
        normalize_device_purchase_options(
            [2, 3],
            base_device_limit=2,
            max_device_limit=5,
            device_price_kopeks=None,
        )


def test_default_period_prefers_30_and_exactly_one_tariff_is_required(monkeypatch):
    monkeypatch.setattr('app.services.device_first_eligibility.settings.SALES_MODE', 'tariffs')
    monkeypatch.setattr('app.services.device_first_eligibility.settings.MULTI_TARIFF_ENABLED', False)
    first = tariff(id=1)
    result = tariff_eligibility(first)
    assert result.eligible
    assert result.default_period_days == 30
    assert resolve_single_eligible_tariff([first]).tariff is first
    assert not resolve_single_eligible_tariff([first, tariff(id=2)]).eligible


@pytest.mark.parametrize(
    'parallel_legacy_flow',
    [
        {'is_trial_available': True},
        {'show_in_gift': True},
        {'is_trial_available': True, 'show_in_gift': True},
    ],
)
def test_parallel_trial_and_gift_flows_do_not_change_ordinary_purchase_eligibility(
    monkeypatch, parallel_legacy_flow
):
    """Trial/gift flags enable separate legacy routes, not a tariff type."""
    monkeypatch.setattr('app.services.device_first_eligibility.settings.SALES_MODE', 'tariffs')
    monkeypatch.setattr('app.services.device_first_eligibility.settings.MULTI_TARIFF_ENABLED', False)
    assert tariff_eligibility(tariff(**parallel_legacy_flow)).eligible


@pytest.mark.parametrize(
    'override',
    [
        {'is_daily': True},
        {'custom_days_enabled': True},
        {'custom_traffic_enabled': True},
        {'device_purchase_options': None},
        {'period_prices': {}},
    ],
)
def test_legacy_modes_are_not_eligible(monkeypatch, override):
    monkeypatch.setattr('app.services.device_first_eligibility.settings.SALES_MODE', 'tariffs')
    monkeypatch.setattr('app.services.device_first_eligibility.settings.MULTI_TARIFF_ENABLED', False)
    assert not tariff_eligibility(tariff(**override)).eligible


def test_paid_subscription_is_grandfathered_and_cannot_decrease():
    current = SimpleNamespace(tariff_id=7, is_trial=False, device_limit=6)
    configured = tariff(max_device_limit=5, device_purchase_options=[2, 3, 5])
    assert device_options_for_subscription(configured, current) == (6,)


def test_current_limit_plus_configured_higher_options_are_offered():
    current = SimpleNamespace(tariff_id=7, is_trial=False, device_limit=3)
    assert device_options_for_subscription(tariff(), current) == (3, 5)

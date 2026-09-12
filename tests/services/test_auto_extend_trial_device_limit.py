from types import SimpleNamespace

import pytest

from app.services import subscription_auto_purchase_service as service


@pytest.mark.parametrize('cart_device_limit', [None, 1])
def test_trial_conversion_preserves_one_device_for_both_cart_paths(monkeypatch, cart_device_limit):
    monkeypatch.setattr(service.settings, 'DEFAULT_DEVICE_LIMIT', 3)
    subscription = SimpleNamespace(
        is_trial=True,
        traffic_limit_gb=10,
        device_limit=1,
        connected_squads=[],
    )
    context = service.AutoExtendContext(
        subscription=subscription,
        period_days=30,
        price_kopeks=14_900,
        description='test',
        device_limit=cart_device_limit,
    )

    service._apply_extension_updates(context)

    assert subscription.device_limit == 1

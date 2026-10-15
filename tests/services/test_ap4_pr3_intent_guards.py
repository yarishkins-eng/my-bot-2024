"""Review-A MB/MI: paid context ceiling and quote-to-intent expiry boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as s
from tests.cabinet.test_vk16_topup_intent import _options
from tests.services.test_ap4_pr3_money import _paid_context_case


@pytest.mark.parametrize(
    'percent,accepted', [(1, True), (30, True), (31, False), (80, False), (0, False), (True, False)]
)
def test_paid_intent_context_percent_boundary(percent, accepted):
    owner, payment, intent, paid = _paid_context_case()
    owner.promo_offer_discount_source = None
    owner.promo_offer_discount_percent = 0
    owner.promo_offer_discount_expires_at = None
    intent['promo_offer']['discount_percent'] = percent
    actual = s._paid_intent_promo_context(payment, intent, owner, paid)
    assert (actual is not None) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'source,seconds,status',
    [
        ('trial_ending_discount', -60, 'ordinary'),
        ('trial_ending_discount', 60, 'accepted'),
        ('trial_expired_discount', -60, 'accepted'),
    ],
)
async def test_prepare_intent_checks_offer_deadline_after_quote(monkeypatch, source, seconds, status):
    options = _options()
    offer = {
        'source': source,
        'discount_percent': 20,
        'expires_at': (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
    }
    options['price_matrix'][0]['prices'][0]['breakdown'] = {
        'promo_offer': offer,
        'promo_offer_discount_kopeks': 3000,
    }
    monkeypatch.setattr(s, 'topup_intent_unavailable_reason', lambda _: None)
    monkeypatch.setattr(s, 'build_purchase_options', AsyncMock(return_value=options))
    monkeypatch.setattr(s, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(s, '_recent_topup_intent_payments', AsyncMock(return_value=[]))
    monkeypatch.setattr(s, 'recent_purchase', AsyncMock(return_value=None))
    decision = await s.prepare_topup_intent(
        SimpleNamespace(),
        user=SimpleNamespace(id=1, balance_kopeks=0),
        period_days=30,
        devices=1,
        method_code=2,
        min_kopeks=100,
        max_kopeks=100000,
    )
    assert decision.status == status
    if status == 'ordinary':
        assert decision.reason == 'unavailable' and decision.intent is None
    else:
        assert decision.intent['promo_offer'] == offer

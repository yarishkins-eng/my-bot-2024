"""АП-4 PR-0: общая скидочная цена и чек, включая настоящую перепроверку перед деньгами."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import device_first as handler
from app.services import device_first_checkout_service as checkout
from app.services.pricing_engine import pricing_engine
from app.services.public_location_entitlement_service import ResolvedEntitlement


def _tariff(price=14900):
    return SimpleNamespace(
        id=3,
        name='Basic',
        description='',
        traffic_limit_gb=0,
        pricing_revision=9,
        is_daily=False,
        device_limit=1,
        device_price_kopeks=5000,
        period_prices={'30': price, '90': 39900, '365': 109000},
        is_available_for_promo_group=lambda _id: True,
    )


def _user(percent=10, balance=5000, group=None):
    return SimpleNamespace(
        id=7,
        language='ru',
        balance_kopeks=balance,
        promo_offer_discount_percent=percent,
        promo_offer_discount_source='trial_expired_discount',
        promo_offer_discount_expires_at=None,
        promo_group=group,
        get_primary_promo_group=lambda: group,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('days', 'devices', 'percent', 'total'),
    [
        (30, 1, 10, 13400),
        (90, 1, 10, 35900),
        (365, 6, 10, 368100),
        (30, 1, 15, 12600),
        (30, 6, 10, 35900),
        (30, 1, 30, 10400),
    ],
)
async def test_personal_discount_rounds_the_whole_tariff_down(days, devices, percent, total):
    price = await pricing_engine.calculate_tariff_purchase_price(
        _tariff(), days, device_limit=devices, user=_user(percent)
    )
    original = _tariff().period_prices[str(days)] + (devices - 1) * 5000 * price.breakdown['months_in_period']
    assert price.final_total == total
    assert price.original_total == original
    assert price.promo_offer_discount == original - total


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('raw', 'percent', 'total'), [(14950, 0, 14950), (150, 99, 100), (0, 10, 0), (14900, 100, 0), (50, 10, 45)]
)
async def test_undiscounted_free_and_subruble_prices_keep_their_contract(raw, percent, total):
    price = await pricing_engine.calculate_tariff_purchase_price(_tariff(raw), 30, user=_user(percent))
    assert price.final_total == total
    assert price.original_total == raw
    assert price.promo_offer_discount >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(('percent', 'total', 'offer'), [(0, 13455, 0), (10, 12100, 1355)])
async def test_group_discount_keeps_its_amount_and_original_price(percent, total, offer):
    group = SimpleNamespace(id=1, get_discount_percent=lambda *_args: 10)
    price = await pricing_engine.calculate_tariff_purchase_price(_tariff(14950), 30, user=_user(percent, group=group))
    assert (price.final_total, price.promo_group_discount, price.promo_offer_discount) == (total, 1495, offer)
    assert price.original_total == 14950


@pytest.mark.asyncio
async def test_cashier_cell_real_rechecks_and_topup_intent_agree(monkeypatch):
    tariff, user = _tariff(), _user()
    db = SimpleNamespace(scalar=AsyncMock(return_value=tariff), commit=AsyncMock())
    eligibility = SimpleNamespace(
        eligible=True, tariff=tariff, device_options=(1,), period_options=(30,), default_period_days=30
    )
    monkeypatch.setattr(checkout, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(checkout, 'is_device_first_canary_user', lambda _user: True)
    monkeypatch.setattr(checkout, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(checkout, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(checkout, 'resolve_single_eligible_tariff', lambda *_args, **_kwargs: eligibility)
    options = await checkout.build_purchase_options(db, user)
    cell = options['price_matrix'][0]['prices'][0]
    assert cell['price_kopeks'] == 13400
    assert cell['breakdown']['promo_offer_discount_kopeks'] == 1500

    entitlement = ResolvedEntitlement(('point',), ('squad',), 1, 'access_point_policy', 'fingerprint')
    order = SimpleNamespace(
        lifecycle_state='confirmed',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        expect_no_subscription=True,
        user_id=user.id,
        period_days=30,
        selected_device_limit=1,
        pricing_revision=tariff.pricing_revision,
        tariff_total_kopeks=cell['price_kopeks'],
        tariff_id=tariff.id,
        price_breakdown=cell['breakdown'],
        entitlement_quote_snapshot=checkout._entitlement_quote_snapshot(entitlement),
    )
    monkeypatch.setattr(checkout, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(checkout, 'tariff_eligibility', lambda *_args, **_kwargs: eligibility)
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=entitlement),
    )
    assert (
        await checkout._validate_direct_pre_commit(db, checkout=order, user=user, target=None, tariff=tariff)
        == entitlement
    )
    db.commit.assert_not_awaited()

    monkeypatch.setattr(checkout, 'topup_intent_unavailable_reason', lambda _user: None)
    monkeypatch.setattr(checkout, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(checkout, '_recent_topup_intent_payments', AsyncMock(return_value=[]))
    monkeypatch.setattr(checkout, 'recent_purchase', AsyncMock(return_value=None))
    monkeypatch.setattr(checkout.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    decision = await checkout.prepare_topup_intent(
        db, user=user, period_days=30, devices=1, method_code=2, min_kopeks=100, max_kopeks=1000000
    )
    assert (decision.status, decision.price_kopeks, decision.amount_kopeks) == ('accepted', cell['price_kopeks'], 8400)
    assert decision.intent['quote_kopeks'] == cell['price_kopeks']


@pytest.mark.parametrize('balance', [1, 10, 99, 5000])
def test_receipt_never_shows_negative_wallet_contribution(monkeypatch, balance):
    monkeypatch.setattr(handler, 'topup_intent_enabled_for', lambda _user: True)
    monkeypatch.setattr(checkout.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    ru, en = handler._money_block(
        _user(balance=balance),
        balance_kopeks=balance,
        price_kopeks=13410,
        top_up_button=SimpleNamespace(),
        has_methods=True,
    )
    assert '−-' not in ru and '−₽-' not in en

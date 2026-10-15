from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as service
from app.utils.promo_offer import get_user_active_promo_discount_percent, without_trial_ending_promo


def user(source='trial_ending_discount', *, paid=False, expires=None):
    return SimpleNamespace(
        id=7,
        has_had_paid_subscription=paid,
        promo_offer_discount_percent=20,
        promo_offer_discount_source=source,
        promo_offer_discount_expires_at=expires or datetime.now(UTC) + timedelta(hours=1),
    )


def test_first_paid_gate_is_source_specific():
    assert get_user_active_promo_discount_percent(user(paid=True)) == 0
    assert get_user_active_promo_discount_percent(user('ordinary', paid=True)) == 20


def test_gift_pricing_view_does_not_mutate_personal_offer():
    owner = user()
    assert get_user_active_promo_discount_percent(without_trial_ending_promo(owner)) == 0
    assert get_user_active_promo_discount_percent(owner) == 20
    ordinary = user('ordinary')
    assert without_trial_ending_promo(ordinary) is ordinary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'source,first,expected',
    [('trial_ending_discount', True, 0), ('trial_ending_discount', False, 20), ('ordinary', True, 20)],
)
async def test_only_new_first_sale_extinguishes_zero_benefit(monkeypatch, source, first, expected):
    owner = user(source)
    monkeypatch.setattr(service, 'get_latest_claimed_offer_for_user', AsyncMock(return_value=None))

    class Savepoint:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *args):
            pass

    db = SimpleNamespace(begin_nested=lambda: Savepoint())
    monkeypatch.setattr(service, 'log_promo_offer_action', AsyncMock())
    await service._consume_promo_offer_for_sale(
        db,
        user=owner,
        checkout=SimpleNamespace(id=1, public_id='own'),
        applied_discount_kopeks=0,
        expected_source=source,
        was_first_paid_sale=first,
    )
    assert owner.promo_offer_discount_percent == expected


@pytest.mark.asyncio
async def test_explicit_no_subscription_never_normalizes_status(monkeypatch):
    monkeypatch.setattr(service, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(service, 'is_device_first_canary_user', lambda _: True)
    monkeypatch.setattr(service, 'get_tariffs_for_user', AsyncMock(return_value=[]))
    normalizer = AsyncMock(side_effect=AssertionError('must not normalize'))
    monkeypatch.setattr(service, '_current_subscription', normalizer)
    result = await service.build_purchase_options(SimpleNamespace(), user(), subscription=None)
    assert result['eligible'] is False
    normalizer.assert_not_called()


def test_topup_missing_and_explicit_null_are_distinct():
    from app.cabinet.schemas.balance import TopUpResponse

    old = TopUpResponse(amount_kopeks=100, amount_rubles=1, status='pending').model_dump()
    assert 'promo_offer' not in old and 'promo_offer_discount_kopeks' not in old
    own = TopUpResponse(
        amount_kopeks=100, amount_rubles=1, status='pending', promo_offer=None, promo_offer_discount_kopeks=0
    ).model_dump()
    assert own['promo_offer'] is None and own['promo_offer_discount_kopeks'] == 0


def test_topup_reuse_reads_own_intent_not_current_offer():
    from app.cabinet.routes.balance import _topup_intent_response

    snapshot = {'discount_percent': 20, 'source': 'trial_ending_discount', 'expires_at': '2026-10-15T00:00:00+00:00'}
    payment = SimpleNamespace(
        id=5,
        amount_kopeks=9900,
        redirect_url='own',
        expires_at=None,
        payment_method_code=2,
        metadata_json={service.TOPUP_INTENT_KEY: {'promo_offer': snapshot, 'promo_offer_discount_kopeks': 3000}},
    )
    response = _topup_intent_response(service.TopUpIntentDecision('already_paid', payment=payment)).model_dump()
    assert response['promo_offer'] == snapshot and response['promo_offer_discount_kopeks'] == 3000


def _paid_context_case(*, source='trial_ending_discount', created_delta=-10, paid_delta=5, owner=None):
    now = datetime.now(UTC)
    deadline = now - timedelta(minutes=1)
    owner = owner or user(expires=deadline)
    owner.promo_offer_discount_expires_at = deadline
    intent = {
        'status': 'pending',
        'created_at': (deadline + timedelta(minutes=created_delta)).isoformat(),
        'promo_offer': {'discount_percent': 20, 'source': source, 'expires_at': deadline.isoformat()},
    }
    payment = SimpleNamespace(id=9, user_id=owner.id, is_paid=True, transaction_id=4, status='CONFIRMED')
    paid = deadline + timedelta(minutes=paid_delta)
    return owner, payment, intent, paid


def test_paid_intent_price_view_is_scoped_and_preserves_original_expiry():
    owner, payment, intent, paid = _paid_context_case()
    context = service._paid_intent_promo_context(payment, intent, owner, paid)
    assert context is not None
    view = service._paid_promo_pricing_user(owner, context)
    assert get_user_active_promo_discount_percent(view) == 20
    assert get_user_active_promo_discount_percent(owner) == 0
    price = SimpleNamespace(breakdown={'offer_discount_pct': 20})
    assert service._price_promo_offer(owner, price, context)['expires_at'] == intent['promo_offer']['expires_at']


@pytest.mark.parametrize(
    'changes',
    [
        {'source': 'foreign'},
        {'created_delta': 0},
        {'paid_delta': 90},
        {'owner': user(paid=True)},
        {'owner': user('foreign')},
    ],
)
def test_paid_context_rejects_foreign_late_created_late_paid_or_sold(changes):
    owner, payment, intent, paid = _paid_context_case(**changes)
    assert service._paid_intent_promo_context(payment, intent, owner, paid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fresh_source,paid,status,expected',
    [
        ('trial_ending_discount', False, 'pending', None),
        (None, False, 'pending', None),
        ('foreign', False, 'pending', 'price_changed'),
        ('trial_ending_discount', True, 'pending', 'price_changed'),
        ('trial_ending_discount', False, 'cancelled', 'cancelled'),
    ],
)
async def test_paid_final_recheck_refreshes_slot_and_restores_only_own_empty(
    monkeypatch, fresh_source, paid, status, expected
):
    owner, payment, intent, reference = _paid_context_case()
    context = service._paid_intent_promo_context(payment, intent, owner, reference)
    fresh = {**intent, 'status': status}
    payment.metadata_json = {service.TOPUP_INTENT_KEY: fresh}
    owner.balance_kopeks = 20000
    owner.account_erasure_requested_at = owner.account_erased_at = None
    owner.restriction_subscription = False

    async def refresh(_owner, *, attribute_names):
        if 'promo_offer_discount_source' in attribute_names:
            owner.promo_offer_discount_source = fresh_source
            owner.has_had_paid_subscription = paid
            if fresh_source is None:
                owner.promo_offer_discount_percent = 0
                owner.promo_offer_discount_expires_at = None

    db = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(return_value=owner), refresh=refresh)
    monkeypatch.setattr(service, 'get_platega_payment_by_id_for_update', AsyncMock(return_value=payment))
    monkeypatch.setattr(service, '_topup_intent_paid_reference', AsyncMock(return_value=reference))
    monkeypatch.setattr(service, '_topup_intent_rolled_out', lambda _: True)
    monkeypatch.setattr(service, '_subscription_identity', AsyncMock(return_value=None))
    monkeypatch.setattr(service, '_topup_intent_drift', AsyncMock(return_value=None))
    result = await service._topup_intent_recheck_locked(
        db, payment_id=9, user_id=7, intent=intent, paid_intent_promo_context=context, checkout_id=11
    )
    assert result == expected
    if expected is None:
        assert owner.promo_offer_discount_expires_at == context.deadline
        assert get_user_active_promo_discount_percent(owner) == 0
    if fresh_source == 'foreign':
        assert owner.promo_offer_discount_source == 'foreign'


@pytest.mark.asyncio
async def test_first_sale_without_applied_snapshot_extinguishes_current_new_right(monkeypatch):
    owner = user()

    class Savepoint:
        async def __aenter__(self):
            pass

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(service, 'get_latest_claimed_offer_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'log_promo_offer_action', AsyncMock())
    await service._consume_promo_offer_for_sale(
        SimpleNamespace(begin_nested=lambda: Savepoint()),
        user=owner,
        checkout=SimpleNamespace(id=1, public_id='own'),
        applied_discount_kopeks=0,
        expected_source=None,
        was_first_paid_sale=True,
    )
    assert owner.promo_offer_discount_source is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change,reason',
    [
        ({}, 'promo_offer_expired'),
        ({'pricing_revision': 2}, 'price_changed'),
        ({'promo_group_id': 2}, 'price_changed'),
        ({'promo_offer_discount_kopeks': 0}, 'price_changed'),
        ({'base_price_kopeks': 15000}, 'price_changed'),
    ],
)
async def test_expiry_reason_requires_own_positive_discount_and_no_other_price_drift(monkeypatch, change, reason):
    owner = user(expires=datetime.now(UTC) - timedelta(seconds=1))
    own = {
        'discount_percent': 20,
        'source': 'trial_ending_discount',
        'expires_at': owner.promo_offer_discount_expires_at.isoformat(),
    }
    saved = {
        'promo_offer': own,
        'promo_offer_discount_kopeks': 3000,
        'promo_group_id': None,
        'group_discount_pct': {},
        'base_price_kopeks': 14900,
        'devices_price_kopeks': 0,
        'promo_group_discount_kopeks': 0,
        'upgrade_prorate_kopeks': 0,
    }
    saved.update({k: v for k, v in change.items() if k != 'pricing_revision'})
    checkout = SimpleNamespace(
        price_breakdown=saved, pricing_revision=1, period_days=30, selected_device_limit=1, tariff_total_kopeks=11900
    )
    tariff = SimpleNamespace(pricing_revision=change.get('pricing_revision', 1))
    price = SimpleNamespace(
        base_price=14900, devices_price=0, promo_group_discount=0, breakdown={'group_discount_pct': {}}
    )
    monkeypatch.setattr(service.pricing_engine, 'resolve_promo_group', lambda _: None)
    calc = AsyncMock(return_value=SimpleNamespace(final_total=11900, promo_offer_discount=3000))
    monkeypatch.setattr(service.pricing_engine, 'calculate_tariff_purchase_price', calc)
    assert (
        await service._expired_own_promo_reason(checkout, owner, tariff, price, upgrade_from=None, upgrade_days=0)
        == reason
    )


@pytest.mark.asyncio
async def test_paid_view_and_gift_use_real_price_engine_with_original_user_unchanged():
    from app.services.pricing_engine import PricingEngine
    from tests.services.test_device_first_upgrade_prorate import _tariff

    owner, payment, intent, paid = _paid_context_case()
    owner.get_primary_promo_group = lambda: None
    context = service._paid_intent_promo_context(payment, intent, owner, paid)
    engine = PricingEngine()
    assert (await engine.calculate_tariff_purchase_price(_tariff(), 30, user=owner)).final_total == 14900
    priced = await engine.calculate_tariff_purchase_price(
        _tariff(), 30, user=service._paid_promo_pricing_user(owner, context)
    )
    assert priced.final_total == 11900 and priced.promo_offer_discount == 3000
    assert (await engine.calculate_tariff_purchase_price(_tariff(), 30, user=owner)).final_total == 14900
    active = user()
    active.get_primary_promo_group = lambda: None
    assert (
        await engine.calculate_tariff_purchase_price(_tariff(), 30, user=without_trial_ending_promo(active))
    ).final_total == 14900
    assert (await engine.calculate_tariff_purchase_price(_tariff(), 30, user=active)).final_total == 11900


@pytest.mark.asyncio
async def test_options_own_metadata_comes_from_same_real_price_and_expires_publicly(monkeypatch):
    from tests.services.test_device_first_upgrade_prorate import _tariff

    owner = user()
    owner.balance_kopeks = 5000
    owner.get_primary_promo_group = lambda: None
    tariff = _tariff()
    monkeypatch.setattr(service, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(service, 'is_device_first_canary_user', lambda _: True)
    monkeypatch.setattr(service, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(
        service,
        'resolve_single_eligible_tariff',
        lambda *_a, **_k: SimpleNamespace(
            eligible=True, tariff=tariff, period_options=(30,), device_options=(1,), default_period_days=30
        ),
    )
    before = await service.build_purchase_options(SimpleNamespace(), owner, subscription=None)
    cell = before['price_matrix'][0]['prices'][0]
    assert cell['price_kopeks'] == 11900
    assert cell['breakdown']['promo_offer_discount_kopeks'] == 3000
    assert (
        cell['breakdown']['promo_offer']
        == before['promo_offer']
        == {
            'source': 'trial_ending_discount',
            'discount_percent': 20,
            'expires_at': owner.promo_offer_discount_expires_at.isoformat(),
        }
    )
    owner.promo_offer_discount_expires_at = datetime.now(UTC) - timedelta(microseconds=1)
    after = await service.build_purchase_options(SimpleNamespace(), owner, subscription=None)
    assert after['price_matrix'][0]['prices'][0]['price_kopeks'] == 14900
    assert after['promo_offer'] is None

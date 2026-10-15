"""Полная цепочка оплаченного намерения и отказ повторной скидочной продаже со старым ORM."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.orm import Session

from app.database.models import (
    CheckoutPaymentAttempt,
    Subscription,
    SubscriptionCheckout,
    Tariff,
    Transaction,
    User,
)
from app.services import device_first_checkout_service as s
from app.services.public_location_entitlement_service import ResolvedEntitlement
from tests.cabinet.test_vk16_topup_intent import _AsyncOverSync, _intent_payment, _new_session


class DB(_AsyncOverSync):
    @asynccontextmanager
    async def begin_nested(self):
        with self.sync.begin_nested():
            yield


@pytest.fixture
def chain(monkeypatch):
    session = _new_session()
    existing = set(inspect(session.bind).get_table_names())
    for table in User.metadata.sorted_tables:
        if table.name in existing:
            continue
        columns = ', '.join('id INTEGER PRIMARY KEY' if c.name == 'id' else f'"{c.name}"' for c in table.columns)
        session.execute(text(f'CREATE TABLE {table.name} ({columns})'))
    owner = session.get(User, 1)
    owner.balance_kopeks = 11900
    owner.has_had_paid_subscription = False
    owner.restriction_subscription = False
    owner.get_primary_promo_group = lambda: None
    tariff = Tariff(
        id=3,
        name='Review',
        is_active=True,
        is_daily=False,
        period_prices={'30': 14900},
        device_limit=1,
        device_purchase_options=[1],
        device_price_kopeks=5000,
        pricing_revision=1,
        traffic_limit_gb=0,
        custom_days_enabled=False,
        custom_traffic_enabled=False,
        entitlement_mode='native_squads',
    )
    session.add(tariff)
    deadline = datetime.now(UTC) - timedelta(minutes=10)
    owner.promo_offer_discount_source = 'trial_ending_discount'
    owner.promo_offer_discount_percent = 20
    owner.promo_offer_discount_expires_at = deadline
    session.commit()
    payment = _intent_payment(
        session,
        payment_id=97,
        quote_kopeks=11900,
        is_paid=True,
        provider_status='CONFIRMED',
        created_ago=timedelta(minutes=20),
        tariff_id=3,
        had_subscription=False,
        promo_offer={'discount_percent': 20, 'source': 'trial_ending_discount', 'expires_at': deadline.isoformat()},
        promo_offer_discount_kopeks=3000,
    )
    payment.paid_at = deadline + timedelta(minutes=5)
    session.commit()
    db = DB(session)
    entitlement = ResolvedEntitlement(('de',), ('squad-de',), 1, 'native_squads')
    monkeypatch.setattr(s, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(s, 'is_device_first_canary_user', lambda u: True)
    monkeypatch.setattr(s, '_topup_intent_rolled_out', lambda u: True)
    monkeypatch.setattr(s, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(s, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr('app.database.crud.user.get_user_by_id', AsyncMock(return_value=owner))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=entitlement),
    )
    monkeypatch.setattr(s, '_report_entitlement_drift_without_blocking', AsyncMock())
    monkeypatch.setattr(s, '_queue_owner_sale_row', AsyncMock())
    monkeypatch.setattr(s, '_kick_direct_provisioning_post_commit', AsyncMock())
    monkeypatch.setattr(s, 'get_latest_claimed_offer_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(s, 'log_promo_offer_action', AsyncMock())

    async def create_paid(db, **kw):
        sub = Subscription(
            user_id=owner.id,
            tariff_id=3,
            is_trial=False,
            status='active',
            device_limit=1,
            end_date=datetime.now(UTC) + timedelta(days=30),
            connected_squads=['squad-de'],
        )
        db.add(sub)
        await db.flush()
        return sub

    monkeypatch.setattr(s, 'create_paid_subscription', create_paid)

    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(s, '_topup_intent_session', factory)
    return SimpleNamespace(db=db, session=session, owner=owner, tariff=tariff, payment=payment, deadline=deadline)


@pytest.mark.asyncio
async def test_late_paid_whole_chain_preserves_own_price_and_replay(chain):
    public_before = await s.build_purchase_options(chain.db, chain.owner, subscription=None)
    assert public_before['price_matrix'][0]['prices'][0]['price_kopeks'] == 14900
    out = await s.complete_topup_intent(payment_id=97)
    assert out['status'] == 'fulfilled', out
    checkout = chain.session.query(SubscriptionCheckout).one()
    assert checkout.tariff_total_kopeks == 11900
    assert checkout.sale_snapshot['promo_offer_source'] == 'trial_ending_discount'
    assert checkout.sale_snapshot['price_breakdown']['promo_offer_discount_kopeks'] == 3000
    assert chain.owner.balance_kopeks == 0
    assert chain.owner.has_had_paid_subscription
    assert chain.owner.promo_offer_discount_source is None
    assert chain.session.query(Transaction).filter(Transaction.type == 'subscription_payment').count() == 1
    again = await s.complete_topup_intent(payment_id=97)
    assert again == out
    assert chain.session.query(Transaction).filter(Transaction.type == 'subscription_payment').count() == 1


@pytest.mark.asyncio
async def test_late_paid_empty_slot_whole_chain(chain):
    chain.owner.promo_offer_discount_source = None
    chain.owner.promo_offer_discount_percent = 0
    chain.owner.promo_offer_discount_expires_at = None
    chain.session.commit()
    out = await s.complete_topup_intent(payment_id=97)
    assert out['status'] == 'fulfilled', out
    assert chain.owner.balance_kopeks == 0


@pytest.mark.asyncio
async def test_late_paid_foreign_slot_whole_chain_preserves_money(chain):
    chain.owner.promo_offer_discount_source = 'other'
    chain.owner.promo_offer_discount_percent = 30
    chain.owner.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(hours=2)
    chain.session.commit()
    out = await s.complete_topup_intent(payment_id=97)
    assert out['status'] == 'refused', out
    assert chain.owner.balance_kopeks == 11900
    assert chain.owner.promo_offer_discount_source == 'other'
    assert chain.session.query(Transaction).filter(Transaction.type == 'subscription_payment').count() == 0


@pytest.mark.asyncio
async def test_public_request_loaded_before_first_sale_must_not_reuse_new_offer(chain, monkeypatch):
    # This is the ordinary READ COMMITTED identity-map interleaving:
    # request B loads User, request A sells+clears+commits, B later locks User.
    future = datetime.now(UTC) + timedelta(hours=1)
    chain.owner.balance_kopeks = 23800
    chain.owner.promo_offer_discount_expires_at = future
    intent = s.topup_intent_of(chain.payment)
    chain.payment.metadata_json = {
        s.TOPUP_INTENT_KEY: {**intent, 'promo_offer': {**intent['promo_offer'], 'expires_at': future.isoformat()}}
    }
    chain.session.commit()
    waiting = Session(chain.session.bind, expire_on_commit=False, autoflush=False)
    stale = waiting.get(User, 1)
    stale.get_primary_promo_group = lambda: None
    assert stale.has_had_paid_subscription is False
    assert stale.promo_offer_discount_percent == 20
    out = await s.complete_topup_intent(payment_id=97)
    assert out['status'] == 'fulfilled'
    first = chain.session.query(SubscriptionCheckout).one()
    first.lifecycle_state = 'ready'
    first.provisioning_state = 'ready'
    chain.session.commit()
    assert chain.owner.has_had_paid_subscription
    assert chain.owner.promo_offer_discount_percent == 0
    assert stale.has_had_paid_subscription is False

    async def current(db, uid):
        return db.sync.query(Subscription).filter(Subscription.user_id == uid).first()

    async def extend(db, target, days, **kwargs):
        target.end_date += timedelta(days=days)
        return target

    monkeypatch.setattr(s, '_current_subscription', current)
    monkeypatch.setattr(s, 'extend_subscription', extend)
    db2 = DB(waiting)
    with pytest.raises(s.DeviceFirstError) as refused:
        resolved = await s.create_or_resume_direct_checkout(
            db2,
            user=stale,
            period_days=30,
            selected_device_limit=1,
            expected_tariff_total_kopeks=11900,
            funding_mode='wallet',
            method_key=None,
            source='cabinet',
        )
        await s.commit_direct_wallet_checkout(db2, public_id=resolved.checkout.public_id, user_id=1)
    assert refused.value.code == 'reprice_required'
    assert waiting.query(Transaction).filter(Transaction.type == 'subscription_payment').count() == 1


@pytest.mark.asyncio
async def test_external_fulfillment_passes_fresh_user_to_sale(chain, monkeypatch):
    checkout = SubscriptionCheckout(
        id=88,
        public_id='money-probe-88',
        user_id=1,
        settlement_mode=s.DIRECT_SETTLEMENT_MODE,
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='not_started',
        target_subscription_id=None,
    )
    attempt = CheckoutPaymentAttempt(
        id=89,
        checkout_id=88,
        settlement_mode=s.DIRECT_SETTLEMENT_MODE,
        status='paid_processing',
        provider_method_code=2,
    )
    chain.session.add_all([checkout, attempt])
    chain.session.commit()
    assert chain.owner.promo_offer_discount_source == 'trial_ending_discount'
    with chain.session.bind.begin() as conn:
        conn.execute(
            update(User)
            .where(User.id == 1)
            .values(
                promo_offer_discount_source='foreign',
                promo_offer_discount_percent=10,
                has_had_paid_subscription=True,
            )
        )
    assert chain.owner.promo_offer_discount_source == 'trial_ending_discount'

    async def verify_fresh(db, *, user, **kwargs):
        assert user.promo_offer_discount_source == 'foreign', 'stale promo source reached the sale'
        assert user.has_had_paid_subscription is True
        return checkout

    monkeypatch.setattr(s, '_complete_direct_sale_locked', verify_fresh)
    monkeypatch.setattr(s, '_kick_direct_referral_post_commit', AsyncMock())
    await s.fulfill_direct_external_checkout(
        chain.db,
        checkout_id=88,
        provider_payment_id='probe',
        payment_attempt_id=89,
    )

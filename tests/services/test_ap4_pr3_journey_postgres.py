"""Controlled PR3 client path, no network, only disposable per-test PG schemas."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database.models import (
    CheckoutPaymentAttempt,
    DiscountOffer,
    PlategaPayment,
    SentNotification,
    Subscription,
    SubscriptionCheckout,
    Tariff,
    Transaction,
    User,
)
from app.localization.texts import get_texts
from app.services import (
    device_first_checkout_service as s,
    monitoring_service as m,
    trial_ending_discount_service as issuer,
)
from app.services.public_location_entitlement_service import ResolvedEntitlement
from app.utils import promo_offer
from tests.services.test_ap4_pr3_postgres import (
    _read_user,
    _seed,
    sessions as sessions,  # noqa: PLC0414 -- shared isolated PG fixture
)


pytestmark = pytest.mark.asyncio


async def prepare(sessions, monkeypatch, *, balance=5000, language='ru'):
    args = await _seed(sessions)
    async with sessions() as db:
        owner = await db.get(User, args['user_id'])
        owner.balance_kopeks = balance
        owner.language = language
        tariff = await db.scalar(select(Tariff).where(Tariff.is_active.is_(True)))
        tariff.device_purchase_options = [2, 3]
        tariff.device_price_kopeks = 5000
        tariff.pricing_revision = 1
        await db.commit()
    monkeypatch.setattr(s, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(s, 'is_device_first_canary_user', lambda _u: True)
    monkeypatch.setattr(s, '_topup_intent_rolled_out', lambda _u: True)
    monkeypatch.setattr(issuer, 'build_purchase_options', s.build_purchase_options)
    monkeypatch.setattr(s.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    monkeypatch.setattr(s.settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 100_000_000)
    monkeypatch.setattr(s.settings, 'PLATEGA_ENABLED', True)
    monkeypatch.setattr(s.settings, 'PLATEGA_MERCHANT_ID', 'scenario-no-network')
    monkeypatch.setattr(s.settings, 'PLATEGA_SECRET', 'scenario-no-network')
    monkeypatch.setattr(s.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)
    monkeypatch.setattr(s.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', False)
    monkeypatch.setattr(s.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(s.settings, 'MINIAPP_CUSTOM_URL', 'https://example.invalid')
    monkeypatch.setattr(s, '_queue_owner_sale_row', AsyncMock())
    monkeypatch.setattr(s, '_report_entitlement_drift_without_blocking', AsyncMock())
    monkeypatch.setattr(s, '_kick_direct_provisioning_post_commit', AsyncMock())
    monkeypatch.setattr(s, '_kick_direct_referral_post_commit', AsyncMock())
    entitlement = ResolvedEntitlement(('de',), ('squad-de',), 1, 'native_squads')
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=entitlement),
    )

    # DB-only canonical read, no normalizer commits or provisioner.
    async def current(db, uid):
        return await db.scalar(select(Subscription).where(Subscription.user_id == uid))

    monkeypatch.setattr(s, '_current_subscription', current)
    monkeypatch.setattr(s, '_topup_intent_session', sessions)
    return args


async def issue_letter(sessions, monkeypatch, args):
    prepared = await issuer.prepare_trial_ending_discount(**args)
    assert prepared is not None and prepared.discount_percent == 20
    assert prepared.expires_at >= prepared.panel_end + timedelta(hours=6)
    delivered = []

    async def send(**kwargs):
        delivered.append(kwargs)
        return True

    monkeypatch.setattr(m.notification_delivery_service, 'send_notification', send)
    async with sessions() as db:
        user = await _read_user(db, args['user_id'])
        sub = await db.get(Subscription, args['subscription_id'])
        assert await m.monitoring_service.handle_trial_expiring(
            db,
            user,
            sub,
            hours=24,
            bot=None,
            panel_expire_at=args['panel_end'].isoformat(),
            prepared_discount=prepared,
        )
        assert len(delivered) == 1
        msg = delivered[0]['telegram_message']
        assert '20%' in msg and '119' in msg and '149' in msg and '319' in msg and '399' in msg
        assert ('МСК' in msg) if user.language == 'ru' else ('MSK' in msg)
        if user.balance_kopeks == 5000:
            assert (
                ('За месяц' in msg and '69' in msg) if user.language == 'ru' else ('For 1 month' in msg and '69' in msg)
            )
        button = delivered[0]['telegram_markup'].inline_keyboard[0][0]
        assert button.text == get_texts(user.language).t('TRIAL_DISCOUNT_CTA')
        assert button.web_app.url == 'https://example.invalid/subscription/purchase'
        offer = await db.get(DiscountOffer, prepared.offer_id)
        assert offer.extra_data.get('delivered_at')
        assert await db.scalar(select(func.count(SentNotification.id))) == 1
        options = await s.build_purchase_options(db, user, subscription=sub)
        assert options['promo_offer']['source'] == 'trial_ending_discount'
        assert options['promo_offer']['expires_at'] == prepared.expires_at.isoformat()
    return prepared


@pytest.mark.parametrize(
    'funding,days,devices,balance,language',
    [
        ('wallet', 30, 2, 20000, 'ru'),
        ('platega', 90, 3, 5000, 'en'),
    ],
)
async def test_connected_letter_selection_sale_replay(sessions, monkeypatch, funding, days, devices, balance, language):
    args = await prepare(sessions, monkeypatch, balance=balance, language=language)
    await issue_letter(sessions, monkeypatch, args)
    async with sessions() as db:
        owner = await _read_user(db, args['user_id'])
        options = await s.build_purchase_options(db, owner)
        cell = next(
            c
            for row in options['price_matrix']
            if row['period_days'] == days
            for c in row['prices']
            if c['device_limit'] == devices
        )
        price = cell['price_kopeks']
        assert cell['breakdown']['promo_offer_discount_kopeks'] > 0
        resolved = await s.create_or_resume_direct_checkout(
            db,
            user=owner,
            period_days=days,
            selected_device_limit=devices,
            expected_tariff_total_kopeks=price,
            funding_mode=funding,
            method_key='sbp' if funding == 'platega' else None,
            source='cabinet',
        )
        assert resolved.proceed_to_payment
        checkout = resolved.checkout
        if funding == 'wallet':
            checkout = await s.commit_direct_wallet_checkout(db, public_id=checkout.public_id, user_id=owner.id)
        else:
            checkout = await s.prepare_direct_external_checkout(db, public_id=checkout.public_id, user_id=owner.id)
            assert checkout.wallet_applied_kopeks == 0 and checkout.external_payable_kopeks == price
            # Boundary: authenticated exact provider settlement is injected locally, no bank.
            attempt = CheckoutPaymentAttempt(
                checkout_id=checkout.id,
                merchant_order_key='scenario-1',
                method_key='sbp',
                provider_method_code=2,
                requested_amount_kopeks=price,
                settlement_mode=s.DIRECT_SETTLEMENT_MODE,
                status='paid_processing',
                provider_payment_id='scenario-paid-1',
            )
            db.add(attempt)
            checkout.lifecycle_state, checkout.funding_state = 'fulfilling', 'paid'
            await db.commit()
            checkout = await s.fulfill_direct_external_checkout(
                db,
                checkout_id=checkout.id,
                provider_payment_id='scenario-paid-1',
                payment_attempt_id=attempt.id,
            )
        assert checkout.fulfillment_state == 'fulfilled'
        assert checkout.sale_snapshot['promo_offer_source'] == 'trial_ending_discount'
        assert checkout.sale_snapshot['price_breakdown']['promo_offer'] == options['promo_offer']
        assert (
            checkout.sale_snapshot['price_breakdown']['promo_offer_discount_kopeks']
            == cell['breakdown']['promo_offer_discount_kopeks']
        )
        await db.refresh(owner)
        assert owner.has_had_paid_subscription and owner.promo_offer_discount_source is None
        assert owner.balance_kopeks == (balance - price if funding == 'wallet' else balance)
        sub = await db.get(Subscription, args['subscription_id'])
        assert sub.is_trial is False and sub.device_limit == devices
        assert (
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.type == 'subscription_payment')) == 1
        )
        if funding == 'wallet':
            again = await s.commit_direct_wallet_checkout(db, public_id=checkout.public_id, user_id=owner.id)
        else:
            again = await s.fulfill_direct_external_checkout(
                db, checkout_id=checkout.id, provider_payment_id='scenario-paid-1', payment_attempt_id=attempt.id
            )
        assert again.id == checkout.id
        assert (
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.type == 'subscription_payment')) == 1
        )


@pytest.mark.parametrize('foreign', [False, True])
async def test_connected_letter_topup_after_discount_deadline(sessions, monkeypatch, foreign):
    args = await prepare(sessions, monkeypatch)
    prepared = await issue_letter(sessions, monkeypatch, args)
    clock = {'now': prepared.expires_at - timedelta(minutes=10)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['now'].astimezone(tz) if tz else clock['now'].replace(tzinfo=None)

    monkeypatch.setattr(s, 'datetime', Clock)
    monkeypatch.setattr(promo_offer, 'datetime', Clock)
    async with sessions() as db:
        owner = await _read_user(db, args['user_id'])
        decision = await s.prepare_topup_intent(
            db, user=owner, period_days=30, devices=2, method_code=2, min_kopeks=100, max_kopeks=100_000_000
        )
        assert decision.status == 'accepted', decision
        assert decision.price_kopeks == 11900 and decision.amount_kopeks == 6900
        assert decision.intent['promo_offer']['expires_at'] == prepared.expires_at.isoformat()
        payment = PlategaPayment(
            user_id=owner.id,
            correlation_id='scenario-topup',
            amount_kopeks=6900,
            payment_method_code=2,
            status='CONFIRMED',
            is_paid=True,
            paid_at=prepared.expires_at + timedelta(minutes=2),
            metadata_json={s.TOPUP_INTENT_KEY: decision.intent},
            created_at=clock['now'],
        )
        db.add(payment)
        # Boundary: bank credit already finalized; test exercises durable purchase after it.
        owner.balance_kopeks += 6900
        if foreign:
            owner.promo_offer_discount_source = 'foreign-offer'
        await db.commit()
        payment_id = payment.id
    clock['now'] = prepared.expires_at + timedelta(minutes=5)
    async with sessions() as db:
        owner = await _read_user(db, args['user_id'])
        fresh = await s.build_purchase_options(db, owner)
        assert fresh['promo_offer'] is None
        assert next(r for r in fresh['price_matrix'] if r['period_days'] == 30)['prices'][0]['price_kopeks'] == 14900
    outcome = await s.complete_topup_intent(payment_id=payment_id)
    assert outcome['status'] == ('refused' if foreign else 'fulfilled'), outcome
    replay = await s.complete_topup_intent(payment_id=payment_id)
    assert replay == outcome
    async with sessions() as db:
        owner = await db.get(User, args['user_id'])
        count = await db.scalar(select(func.count(Transaction.id)).where(Transaction.type == 'subscription_payment'))
        assert count == (0 if foreign else 1)
        assert owner.balance_kopeks == (11900 if foreign else 0)
        if not foreign:
            checkout = await db.scalar(select(SubscriptionCheckout))
            assert checkout.sale_snapshot['promo_offer_source'] == 'trial_ending_discount'
            assert checkout.tariff_total_kopeks == 11900
            assert checkout.sale_snapshot['price_breakdown']['promo_offer_discount_kopeks'] == 3000
            assert (
                checkout.sale_snapshot['price_breakdown']['promo_offer']['expires_at']
                == prepared.expires_at.isoformat()
            )
            assert owner.has_had_paid_subscription and owner.promo_offer_discount_source is None


@pytest.mark.parametrize(
    'gate',
    [
        'no-vpn',
        'unknown-vpn',
        'off',
        'nonstand',
        'other-coupon',
        'first-paid',
        'reset',
        'other-carrier',
        'retired-uuid',
    ],
)
async def test_connected_issuer_negative(sessions, monkeypatch, gate):
    args = await prepare(sessions, monkeypatch)
    async with sessions() as db:
        owner = await db.get(User, args['user_id'])
        if gate == 'no-vpn':
            args['lifetime_bytes'] = 0
        elif gate == 'unknown-vpn':
            args['lifetime_bytes'] = None
        elif gate == 'off':
            config = issuer.NotificationSettingsService.get_trial_ending_discount_settings()
            monkeypatch.setattr(
                issuer.NotificationSettingsService,
                'get_trial_ending_discount_settings',
                lambda: {**config, 'enabled': False},
            )
        elif gate == 'nonstand':
            owner.test_account_enabled = False
        elif gate == 'other-coupon':
            owner.promo_offer_discount_percent = 5
            owner.promo_offer_discount_source = 'other-coupon'
            owner.promo_offer_discount_expires_at = None
        elif gate == 'first-paid':
            owner.has_had_paid_subscription = True
        elif gate == 'reset':
            owner.test_reset_state = 'resetting'
        elif gate == 'other-carrier':
            args['carrier'] = 48
        elif gate == 'retired-uuid':
            args['panel_uuid'] = 'retired-uuid'
        await db.commit()
    assert await issuer.prepare_trial_ending_discount(**args) is None
    async with sessions() as db:
        assert await db.scalar(select(func.count(DiscountOffer.id))) == 0
        owner = await db.get(User, args['user_id'])
        assert owner.promo_offer_discount_source == ('other-coupon' if gate == 'other-coupon' else None)


@pytest.mark.parametrize('cleared,reason', [(False, 'promo_offer_expired'), (True, 'price_changed')])
async def test_frozen_checkout_expiry_reason_and_known_cleanup_bound(sessions, monkeypatch, cleared, reason):
    args = await prepare(sessions, monkeypatch, balance=20000)
    prepared = await issue_letter(sessions, monkeypatch, args)
    clock = {'now': prepared.expires_at - timedelta(minutes=10)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock['now'].astimezone(tz) if tz else clock['now'].replace(tzinfo=None)

    monkeypatch.setattr(s, 'datetime', Clock)
    monkeypatch.setattr(promo_offer, 'datetime', Clock)
    async with sessions() as db:
        owner = await _read_user(db, args['user_id'])
        resolved = await s.create_or_resume_direct_checkout(
            db,
            user=owner,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=11900,
            funding_mode='wallet',
            method_key=None,
            source='cabinet',
        )
        checkout = resolved.checkout
        if cleared:
            owner.promo_offer_discount_percent = 0
            owner.promo_offer_discount_source = None
            owner.promo_offer_discount_expires_at = None
        await db.commit()
        clock['now'] = prepared.expires_at + timedelta(seconds=1)
        assert checkout.quote_expires_at > clock['now']
        with pytest.raises(s.DeviceFirstError) as caught:
            await s.commit_direct_wallet_checkout(db, public_id=checkout.public_id, user_id=owner.id)
        assert caught.value.code == 'reprice_required'
        assert checkout.terminal_reason == reason
        await db.refresh(owner)
        assert owner.balance_kopeks == 20000
        assert (
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.type == 'subscription_payment')) == 0
        )

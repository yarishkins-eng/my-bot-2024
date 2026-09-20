from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Base,
    CheckoutPaymentAttempt,
    CloudPaymentsPayment,
    DeviceFirstProviderEvent,
    Subscription,
    SubscriptionCheckout,
    SubscriptionEvent,
    Transaction,
    TransactionType,
    YooKassaPayment,
)
from app.services.admin_notification_service import AdminNotificationService
from app.services.campaign_service import get_campaign_performance


TABLES = [
    AdvertisingCampaign.__table__,
    AdvertisingCampaignRegistration.__table__,
    SubscriptionEvent.__table__,
    Subscription.__table__,
    Transaction.__table__,
    SubscriptionCheckout.__table__,
    CheckoutPaymentAttempt.__table__,
    DeviceFirstProviderEvent.__table__,
    YooKassaPayment.__table__,
    CloudPaymentsPayment.__table__,
]


def _make_db():
    """Execute production queries against real SQLite tables through an async shim."""

    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=TABLES)
    sync_db = sessionmaker(engine, expire_on_commit=False)()

    async def execute(statement, *args, **kwargs):
        return sync_db.execute(statement, *args, **kwargs)

    async def get(entity, ident):
        return sync_db.get(entity, ident)

    async def commit():
        sync_db.commit()

    async def refresh(instance, *args, **kwargs):
        sync_db.refresh(instance, *args, **kwargs)

    async def rollback():
        sync_db.rollback()

    return (
        engine,
        sync_db,
        SimpleNamespace(
            execute=execute,
            get=get,
            add=sync_db.add,
            commit=commit,
            refresh=refresh,
            rollback=rollback,
        ),
    )


def _campaign(campaign_id: int, *, spend: int | None = None) -> AdvertisingCampaign:
    return AdvertisingCampaign(
        id=campaign_id,
        name=f'Campaign {campaign_id}',
        start_parameter=f'campaign-{campaign_id}',
        bonus_type='none',
        ad_spend_kopeks=spend,
        is_active=True,
    )


def _registration(registration_id: int, campaign_id: int, user_id: int, at: datetime):
    return AdvertisingCampaignRegistration(
        id=registration_id,
        campaign_id=campaign_id,
        user_id=user_id,
        bonus_type='none',
        created_at=at,
    )


def _transaction(
    transaction_id: int,
    user_id: int,
    at: datetime,
    *,
    ledger_key: str | None,
    payment_method: str = 'balance',
    checkout_id: int | None = None,
    external_id: str | None = None,
) -> Transaction:
    return Transaction(
        id=transaction_id,
        user_id=user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=-14_900,
        description='Продление подписки' if ledger_key is None else 'Оплата подписки через кассу',
        payment_method=payment_method,
        device_first_checkout_id=checkout_id,
        device_first_ledger_key=ledger_key,
        external_id=external_id,
        is_completed=True,
        completed_at=at,
        created_at=at,
    )


def _purchase_event(
    event_id: int,
    user_id: int,
    transaction_id: int,
    at: datetime,
    purchase_type: str,
) -> SubscriptionEvent:
    return SubscriptionEvent(
        id=event_id,
        user_id=user_id,
        event_type='purchase',
        transaction_id=transaction_id,
        amount_kopeks=14_900,
        message='Subscription purchase',
        occurred_at=at,
        extra={'purchase_type': purchase_type},
        created_at=at,
    )


@pytest.mark.asyncio
async def test_campaign_performance_executes_cashier_sql_and_keeps_money_primary() -> None:
    now = datetime(2026, 9, 19, 18, 0, tzinfo=UTC)
    touch = now - timedelta(days=10)
    engine, sync_db, db = _make_db()
    try:
        sync_db.add_all([_campaign(4, spend=70_000), _campaign(5)])
        sync_db.add_all(
            [_registration(index, 4, index, touch) for index in range(1, 9)]
            + [_registration(9, 5, 9, touch), _registration(10, 4, 10, touch)]
        )

        # A: card checkout without an event; exactly touch + 24h.
        sync_db.add(
            _transaction(
                101,
                1,
                touch + timedelta(hours=24),
                ledger_key='direct-sale:101',
                payment_method='platega',
            )
        )
        # B: balance checkout plus a duplicate lifecycle event; 24h + 1s.
        sync_db.add(_transaction(102, 2, touch + timedelta(hours=24, seconds=1), ledger_key='direct-sale:102'))
        sync_db.add(_purchase_event(102, 2, 102, touch + timedelta(hours=24, seconds=1), 'first_purchase'))
        # C: trial followed by a checkout purchase.
        sync_db.add(
            SubscriptionEvent(
                id=103,
                user_id=3,
                event_type='activation',
                message='Trial activation',
                occurred_at=touch + timedelta(hours=1),
                created_at=touch + timedelta(hours=1),
            )
        )
        sync_db.add(_transaction(103, 3, touch + timedelta(days=3), ledger_key='debit:103'))
        # D: a transaction from the old screen has no checkout ledger key.
        sync_db.add(_transaction(104, 4, touch + timedelta(days=2), ledger_key=None))
        # E: the user's first checkout payment predates the advertising touch;
        # a later checkout renewal must not replace that global first payment.
        sync_db.add(_transaction(105, 5, touch - timedelta(days=1), ledger_key='direct-sale:105'))
        sync_db.add(_transaction(110, 5, touch + timedelta(days=4), ledger_key='direct-sale:110'))
        # F: a sale:repeat lifecycle event must not cancel the cashier payment.
        sync_db.add(_transaction(106, 6, touch + timedelta(days=2), ledger_key='direct-sale:106'))
        sync_db.add(_purchase_event(106, 6, 106, touch + timedelta(days=2), 'renewal'))
        # Known sandbox payment with a linked purchase event is excluded.
        sync_db.add(
            _transaction(
                107,
                7,
                touch + timedelta(days=2),
                ledger_key='direct-sale:107',
                payment_method='yookassa',
            )
        )
        sync_db.add(_purchase_event(107, 7, 107, touch + timedelta(days=2), 'first_purchase'))
        sync_db.add(
            YooKassaPayment(
                id=107,
                user_id=7,
                yookassa_payment_id='sandbox-107',
                amount_kopeks=14_900,
                currency='RUB',
                status='succeeded',
                is_paid=True,
                test_mode=True,
                transaction_id=107,
            )
        )
        # A durable reversal is excluded by the same SQL predicate.
        sync_db.add(
            SubscriptionCheckout(
                id=108,
                public_id='checkout-108',
                user_id=8,
                tariff_id=3,
                period_days=30,
                selected_device_limit=1,
                quoted_price_kopeks=14_900,
                max_price_kopeks=14_900,
                pricing_revision=1,
                quote_expires_at=now,
                expires_at=now,
                terminal_reason='post_paid_provider_terminal:chargebacked',
            )
        )
        sync_db.add(
            _transaction(
                108,
                8,
                touch + timedelta(days=2),
                ledger_key='direct-sale:108',
                payment_method='platega',
                checkout_id=108,
                external_id='reversed-108',
            )
        )
        sync_db.add(_purchase_event(108, 8, 108, touch + timedelta(days=2), 'first_purchase'))

        # G: a proven earlier purchase (linked event, no checkout key) before the touch
        # moves the first purchase back, so a later checkout sale is not attributed.
        sync_db.add(_transaction(111, 10, touch - timedelta(days=2), ledger_key=None))
        sync_db.add(_purchase_event(111, 10, 111, touch - timedelta(days=2), 'first_purchase'))
        sync_db.add(_transaction(112, 10, touch + timedelta(days=3), ledger_key='direct-sale:112'))

        # A separate clean campaign proves that partial is computed, not hard-coded.
        sync_db.add(_transaction(109, 9, touch + timedelta(days=1), ledger_key='direct-sale:109'))
        sync_db.commit()

        result = await get_campaign_performance(db, 4, now=now)
        assert result is not None
        assert result['leads'] == 9
        assert result['historical_trial_users_count'] == 1
        assert result['paid_subscription_users_count'] == 4
        assert result['paid_after_trial_count'] == 1
        assert result['paid_without_trial_count'] == 3
        assert result['lead_to_paid_subscription_rate'] == 44.4
        assert result['data_quality']['status'] == 'partial'
        assert result['delay_curve'][0] == {
            'hours': 24,
            'eligible_leads': 9,
            'converted_leads': 1,
            'conversion_rate': 11.1,
        }

        clean_result = await get_campaign_performance(db, 5, now=now)
        assert clean_result is not None
        assert clean_result['paid_subscription_users_count'] == 1
        assert clean_result['data_quality']['status'] == 'complete'
    finally:
        sync_db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_purchase_notification_persists_purchase_type_when_delivery_is_disabled() -> None:
    engine, sync_db, db = _make_db()
    try:
        happened_at = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
        service = AdminNotificationService(AsyncMock())
        service.enabled = False

        delivered = await service.send_subscription_purchase_notification(
            db,
            SimpleNamespace(id=901, has_had_paid_subscription=False),
            SimpleNamespace(id=902),
            SimpleNamespace(
                id=903,
                amount_kopeks=-14_900,
                payment_method='platega',
                completed_at=happened_at,
                created_at=happened_at,
            ),
            30,
            purchase_type='first_purchase',
        )

        assert delivered is False
        event = sync_db.execute(select(SubscriptionEvent)).scalar_one()
        assert event.extra['purchase_type'] == 'first_purchase'
    finally:
        sync_db.close()
        engine.dispose()


def test_campaign_spend_update_distinguishes_omitted_null_and_rejects_negative() -> None:
    from pydantic import ValidationError

    from app.cabinet.schemas.campaigns import CampaignUpdateRequest

    omitted = CampaignUpdateRequest()
    cleared = CampaignUpdateRequest(ad_spend_kopeks=None)
    explicit_zero = CampaignUpdateRequest(ad_spend_kopeks=0)

    assert 'ad_spend_kopeks' not in omitted.model_fields_set
    assert 'ad_spend_kopeks' in cleared.model_fields_set
    assert explicit_zero.ad_spend_kopeks == 0
    with pytest.raises(ValidationError):
        CampaignUpdateRequest(ad_spend_kopeks=-1)


@pytest.mark.asyncio
async def test_campaign_analytics_v2_route_keeps_paid_users_and_receipts_separate(monkeypatch) -> None:
    from app.cabinet.routes import admin_campaigns

    payload = {
        'campaign_id': 13,
        'generated_at': datetime(2026, 9, 19, 18, 0, tzinfo=UTC),
        'timezone': 'Europe/Moscow',
        'leads': 133,
        'historical_trial_users_count': 38,
        'active_trials_count': 35,
        'lead_to_trial_rate': 28.6,
        'paid_subscription_users_count': 5,
        'lead_to_paid_subscription_rate': 3.8,
        'paid_after_trial_count': 3,
        'paid_without_trial_count': 2,
        'trial_to_paid_rate': 7.9,
        'confirmed_receipts_kopeks': 144_600,
        'ad_spend_kopeks': 800_000,
        'cost_per_lead_kopeks': 6_015,
        'cost_per_trial_kopeks': 21_053,
        'customer_acquisition_cost_kopeks': 160_000,
        'gross_roas_percent': 18.1,
        'receipts_minus_ad_spend_kopeks': -655_400,
        'maturity_horizon_days': 7,
        'immature_leads_count': 43,
        'last_lead_at': None,
        'last_trial_at': None,
        'last_paid_subscription_at': None,
        'data_quality': {'status': 'complete'},
        'daily_cohorts': [],
        'delay_curve': [],
        'cumulative_receipts': [],
    }
    monkeypatch.setattr(admin_campaigns, 'get_campaign_performance', AsyncMock(return_value=payload))

    response = await admin_campaigns.get_campaign_analytics_v2(
        campaign_id=13,
        admin=SimpleNamespace(id=1),
        db=SimpleNamespace(),
    )

    assert response.paid_subscription_users_count == 5
    assert response.paid_after_trial_count == 3
    assert response.paid_without_trial_count == 2
    assert response.confirmed_receipts_kopeks == 144_600
    assert response.data_quality.status == 'complete'

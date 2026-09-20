from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import record_campaign_registration
from app.database.crud.subscription import (
    create_paid_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.crud.user import add_user_balance
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    CheckoutPaymentAttempt,
    CloudPaymentsPayment,
    DeviceFirstProviderEvent,
    Subscription,
    SubscriptionCheckout,
    SubscriptionConversion,
    SubscriptionEvent,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    YooKassaPayment,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

_MOSCOW = ZoneInfo('Europe/Moscow')
_PURCHASE_HORIZONS_HOURS = (24, 72, 168, 336, 720)
_POST_PAID_REVERSAL_PREFIX = 'post_paid_provider_terminal%'
_DURABLE_POST_PAID_REVERSAL_STATUSES = ('CHARGEBACKED',)


def known_test_payment_clause():
    return or_(
        select(YooKassaPayment.id)
        .where(YooKassaPayment.transaction_id == Transaction.id, YooKassaPayment.test_mode.is_(True))
        .correlate(Transaction)
        .exists(),
        select(CloudPaymentsPayment.id)
        .where(CloudPaymentsPayment.transaction_id == Transaction.id, CloudPaymentsPayment.test_mode.is_(True))
        .correlate(Transaction)
        .exists(),
    )


def post_paid_reversal_clause():
    reversed_attempt = (
        select(CheckoutPaymentAttempt.id)
        .where(
            CheckoutPaymentAttempt.checkout_id == Transaction.device_first_checkout_id,
            CheckoutPaymentAttempt.provider_payment_id == Transaction.external_id,
            CheckoutPaymentAttempt.reconciliation_reason.like(_POST_PAID_REVERSAL_PREFIX),
        )
        .correlate(Transaction)
        .exists()
    )
    reversed_event = (
        select(DeviceFirstProviderEvent.id)
        .where(
            DeviceFirstProviderEvent.checkout_id == Transaction.device_first_checkout_id,
            DeviceFirstProviderEvent.provider_payment_id == Transaction.external_id,
            func.upper(DeviceFirstProviderEvent.provider_status).in_(_DURABLE_POST_PAID_REVERSAL_STATUSES),
        )
        .correlate(Transaction)
        .exists()
    )
    return or_(
        func.coalesce(SubscriptionCheckout.terminal_reason, '').like(_POST_PAID_REVERSAL_PREFIX),
        reversed_attempt,
        reversed_event,
    )


def _format_user_log(user: User) -> str:
    """Format user identifier for logging (supports email-only users)."""
    if user.telegram_id:
        return str(user.telegram_id)
    if user.email:
        return f'{user.id} ({user.email})'
    return f'#{user.id}'


@dataclass
class CampaignBonusResult:
    success: bool
    bonus_type: str | None = None
    balance_kopeks: int = 0
    subscription_days: int | None = None
    subscription_traffic_gb: int | None = None
    subscription_device_limit: int | None = None
    subscription_squads: list[str] | None = None
    # Поля для tariff
    tariff_id: int | None = None
    tariff_name: str | None = None
    tariff_duration_days: int | None = None
    # True если запись в advertising_campaign_registrations была создана этим вызовом
    # (а не вернулась как existing). Используется caller'ом, чтобы понять, нужно ли
    # слать админу уведомление о регистрации (один раз на первую успешную).
    is_new_registration: bool = False


@dataclass(frozen=True)
class CampaignAnalytics:
    """Two explicit contracts: legacy fields and first-touch external receipts."""

    campaign_id: int
    registrations: int
    conversion_count: int
    paid_users_count: int
    conversion_rate: float
    total_revenue_kopeks: int
    avg_revenue_per_user_kopeks: int
    leads: int
    paying_leads: int
    payment_conversion_rate: float
    confirmed_receipts_kopeks: int
    avg_confirmed_receipts_per_lead_kopeks: int


def _campaign_analytics_statement(campaign_ids: list[int] | None = None):
    """Build the aggregate without filtering registrations before first-touch ranking."""

    ranked_registrations = select(
        AdvertisingCampaignRegistration.campaign_id,
        AdvertisingCampaignRegistration.user_id,
        AdvertisingCampaignRegistration.created_at,
        func.row_number()
        .over(
            partition_by=AdvertisingCampaignRegistration.user_id,
            order_by=(
                AdvertisingCampaignRegistration.created_at.asc().nulls_last(),
                AdvertisingCampaignRegistration.id.asc(),
            ),
        )
        .label('touch_rank'),
    ).cte('ranked_campaign_registrations')
    first_touch = (
        select(
            ranked_registrations.c.campaign_id,
            ranked_registrations.c.user_id,
            ranked_registrations.c.created_at,
        )
        .where(ranked_registrations.c.touch_rank == 1)
        .cte('campaign_first_touch')
    )

    known_test_payment = known_test_payment_clause()
    post_paid_reversal = post_paid_reversal_clause()

    receipts = (
        select(
            first_touch.c.campaign_id,
            first_touch.c.user_id,
            Transaction.id.label('transaction_id'),
            Transaction.amount_kopeks,
        )
        .join(Transaction, Transaction.user_id == first_touch.c.user_id)
        .outerjoin(SubscriptionCheckout, SubscriptionCheckout.id == Transaction.device_first_checkout_id)
        .where(
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
            Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            func.coalesce(Transaction.completed_at, Transaction.created_at) >= first_touch.c.created_at,
            ~known_test_payment,
            or_(
                Transaction.type != TransactionType.PROVIDER_RECEIPT.value,
                ~post_paid_reversal,
            ),
        )
        .cte('campaign_qualified_receipts')
    )

    leads = (
        select(first_touch.c.campaign_id, func.count().label('leads'))
        .group_by(first_touch.c.campaign_id)
        .cte('campaign_leads')
    )
    receipt_totals = (
        select(
            receipts.c.campaign_id,
            func.count(func.distinct(receipts.c.user_id)).label('paying_leads'),
            func.coalesce(func.sum(receipts.c.amount_kopeks), 0).label('confirmed_receipts_kopeks'),
        )
        .group_by(receipts.c.campaign_id)
        .cte('campaign_receipt_totals')
    )
    registrations = (
        select(
            AdvertisingCampaignRegistration.campaign_id,
            func.count(AdvertisingCampaignRegistration.id).label('registrations'),
        )
        .group_by(AdvertisingCampaignRegistration.campaign_id)
        .cte('campaign_registration_totals')
    )
    legacy_registration_users = (
        select(
            AdvertisingCampaignRegistration.campaign_id,
            AdvertisingCampaignRegistration.user_id,
        )
        .distinct()
        .cte('campaign_legacy_registration_users')
    )
    legacy_deposits = (
        select(
            legacy_registration_users.c.campaign_id,
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_revenue_kopeks'),
        )
        .join(Transaction, Transaction.user_id == legacy_registration_users.c.user_id)
        .where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_deposits')
    )
    legacy_payment_users = (
        select(SubscriptionConversion.user_id.label('user_id'))
        .union(
            select(Transaction.user_id.label('user_id')).where(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
            )
        )
        .cte('campaign_legacy_payment_users')
    )
    legacy_conversions = (
        select(
            legacy_registration_users.c.campaign_id,
            func.count(func.distinct(legacy_registration_users.c.user_id)).label('conversion_count'),
        )
        .join(legacy_payment_users, legacy_payment_users.c.user_id == legacy_registration_users.c.user_id)
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_conversions')
    )
    legacy_paid_flags = (
        select(
            legacy_registration_users.c.campaign_id,
            func.count(func.distinct(legacy_registration_users.c.user_id)).label('paid_flag_count'),
        )
        .join(User, User.id == legacy_registration_users.c.user_id)
        .where(User.has_had_paid_subscription.is_(True))
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_paid_flags')
    )

    statement = (
        select(
            AdvertisingCampaign.id.label('campaign_id'),
            func.coalesce(registrations.c.registrations, 0).label('registrations'),
            func.coalesce(legacy_conversions.c.conversion_count, 0).label('conversion_count'),
            func.coalesce(legacy_paid_flags.c.paid_flag_count, 0).label('paid_flag_count'),
            func.coalesce(legacy_deposits.c.total_revenue_kopeks, 0).label('total_revenue_kopeks'),
            func.coalesce(leads.c.leads, 0).label('leads'),
            func.coalesce(receipt_totals.c.paying_leads, 0).label('paying_leads'),
            func.coalesce(receipt_totals.c.confirmed_receipts_kopeks, 0).label('confirmed_receipts_kopeks'),
        )
        .outerjoin(registrations, registrations.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_conversions, legacy_conversions.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_paid_flags, legacy_paid_flags.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_deposits, legacy_deposits.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(leads, leads.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(receipt_totals, receipt_totals.c.campaign_id == AdvertisingCampaign.id)
        .order_by(AdvertisingCampaign.id)
    )
    if campaign_ids is not None:
        statement = statement.where(AdvertisingCampaign.id.in_(campaign_ids))
    return statement


async def get_campaign_analytics(
    db: AsyncSession,
    campaign_ids: list[int] | None = None,
) -> dict[int, CampaignAnalytics]:
    """Return legacy-compatible and honest receipt metrics in one SQL round-trip."""

    if campaign_ids == []:
        return {}
    rows = (await db.execute(_campaign_analytics_statement(campaign_ids))).mappings().all()
    result: dict[int, CampaignAnalytics] = {}
    for row in rows:
        registrations_count = int(row['registrations'] or 0)
        conversion_count = int(row['conversion_count'] or 0)
        paid_users_count = max(conversion_count, int(row['paid_flag_count'] or 0))
        legacy_revenue = int(row['total_revenue_kopeks'] or 0)
        leads_count = int(row['leads'] or 0)
        paying_leads = int(row['paying_leads'] or 0)
        confirmed_receipts = int(row['confirmed_receipts_kopeks'] or 0)
        result[int(row['campaign_id'])] = CampaignAnalytics(
            campaign_id=int(row['campaign_id']),
            registrations=registrations_count,
            conversion_count=conversion_count,
            paid_users_count=paid_users_count,
            conversion_rate=round((paid_users_count / registrations_count) * 100, 1) if registrations_count else 0.0,
            total_revenue_kopeks=legacy_revenue,
            avg_revenue_per_user_kopeks=int(legacy_revenue / registrations_count) if registrations_count else 0,
            leads=leads_count,
            paying_leads=paying_leads,
            payment_conversion_rate=round((paying_leads / leads_count) * 100, 1) if leads_count else 0.0,
            confirmed_receipts_kopeks=confirmed_receipts,
            avg_confirmed_receipts_per_lead_kopeks=(int(confirmed_receipts / leads_count) if leads_count else 0),
        )
    return result


def _performance_first_touch_cte(as_of: datetime):
    """Global first-touch population containing only evidence known at ``as_of``."""

    ranked = (
        select(
            AdvertisingCampaignRegistration.campaign_id,
            AdvertisingCampaignRegistration.user_id,
            AdvertisingCampaignRegistration.created_at,
            func.row_number()
            .over(
                partition_by=AdvertisingCampaignRegistration.user_id,
                order_by=(
                    AdvertisingCampaignRegistration.created_at.asc().nulls_last(),
                    AdvertisingCampaignRegistration.id.asc(),
                ),
            )
            .label('touch_rank'),
        )
        .where(
            or_(
                AdvertisingCampaignRegistration.created_at.is_(None),
                AdvertisingCampaignRegistration.created_at <= as_of,
            )
        )
        .cte('performance_ranked_campaign_registrations')
    )
    return (
        select(ranked.c.campaign_id, ranked.c.user_id, ranked.c.created_at)
        .where(ranked.c.touch_rank == 1)
        .cte('performance_campaign_first_touch')
    )


def _qualified_receipt_rows_statement(first_touch, campaign_id: int, as_of: datetime):
    known_test_payment = known_test_payment_clause()
    post_paid_reversal = post_paid_reversal_clause()
    return (
        select(
            first_touch.c.user_id,
            Transaction.id.label('transaction_id'),
            Transaction.amount_kopeks,
            func.coalesce(Transaction.completed_at, Transaction.created_at).label('receipt_at'),
        )
        .join(Transaction, Transaction.user_id == first_touch.c.user_id)
        .outerjoin(SubscriptionCheckout, SubscriptionCheckout.id == Transaction.device_first_checkout_id)
        .where(
            first_touch.c.campaign_id == campaign_id,
            first_touch.c.created_at.is_not(None),
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
            Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            func.coalesce(Transaction.completed_at, Transaction.created_at) >= first_touch.c.created_at,
            func.coalesce(Transaction.completed_at, Transaction.created_at) <= as_of,
            ~known_test_payment,
            or_(
                Transaction.type != TransactionType.PROVIDER_RECEIPT.value,
                ~post_paid_reversal,
            ),
        )
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def _purchase_event_classification(row: Any) -> str:
    """Classify a purchase event without treating weak legacy evidence as acquisition."""

    extra = row.extra if isinstance(row.extra, dict) else {}
    purchase_type = extra.get('purchase_type')
    if row.transaction_id is None or row.transaction_type != TransactionType.SUBSCRIPTION_PAYMENT.value:
        # A lifecycle label is sufficient only as negative evidence: an older
        # renewal/switch proves the later event cannot be a first purchase, but
        # an unlinked first_purchase is never strong enough to add a customer.
        if purchase_type in {'renewal', 'tariff_switch'}:
            return 'later_lifecycle'
        return 'unresolved'
    if not row.transaction_completed or not row.transaction_amount_kopeks:
        return 'unresolved'
    if getattr(row, 'transaction_is_test', False):
        return 'test'
    if getattr(row, 'transaction_reversed', False):
        return 'reversed'

    if purchase_type is not None:
        return 'acquisition' if purchase_type == 'first_purchase' else 'later_lifecycle'
    ledger_key = row.device_first_ledger_key or ''
    return 'acquisition' if ledger_key.startswith('direct-sale:') else 'unresolved'


async def get_campaign_performance(
    db: AsyncSession,
    campaign_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the honest v2 campaign funnel without changing legacy analytics contracts."""

    campaign = await db.get(AdvertisingCampaign, campaign_id)
    if campaign is None:
        return None

    current_time = _aware_utc(now) or datetime.now(UTC)
    first_touch = _performance_first_touch_cte(current_time)

    lead_rows = (
        await db.execute(
            select(first_touch.c.user_id, first_touch.c.created_at)
            .where(first_touch.c.campaign_id == campaign_id)
            .order_by(first_touch.c.created_at.asc().nulls_last(), first_touch.c.user_id)
        )
    ).all()
    touches: dict[int, datetime | None] = {}
    for row in lead_rows:
        touch_at = _aware_utc(row.created_at)
        if touch_at is None or touch_at <= current_time:
            touches[int(row.user_id)] = touch_at

    trial_event_rows = (
        await db.execute(
            select(
                SubscriptionEvent.user_id,
                func.min(SubscriptionEvent.occurred_at).label('occurred_at'),
            )
            .join(first_touch, first_touch.c.user_id == SubscriptionEvent.user_id)
            .where(
                first_touch.c.campaign_id == campaign_id,
                first_touch.c.created_at.is_not(None),
                SubscriptionEvent.event_type == 'activation',
                SubscriptionEvent.message == 'Trial activation',
                SubscriptionEvent.occurred_at <= current_time,
            )
            .group_by(SubscriptionEvent.user_id)
        )
    ).all()
    trial_events = {
        int(row.user_id): occurred_at
        for row in trial_event_rows
        if (occurred_at := _aware_utc(row.occurred_at)) is not None and occurred_at <= current_time
    }

    trial_subscription_rows = (
        await db.execute(
            select(
                Subscription.user_id,
                Subscription.start_date,
                Subscription.status,
                Subscription.end_date,
            )
            .join(first_touch, first_touch.c.user_id == Subscription.user_id)
            .where(
                first_touch.c.campaign_id == campaign_id,
                first_touch.c.created_at.is_not(None),
                Subscription.is_trial.is_(True),
                Subscription.start_date <= current_time,
            )
            .order_by(Subscription.user_id, Subscription.start_date)
        )
    ).all()
    trial_subscriptions: dict[int, datetime] = {}
    active_trial_users: set[int] = set()
    for row in trial_subscription_rows:
        user_id = int(row.user_id)
        started_at = _aware_utc(row.start_date)
        if started_at is not None and started_at <= current_time:
            trial_subscriptions.setdefault(user_id, started_at)
        end_at = _aware_utc(row.end_date)
        touch_at = touches.get(user_id)
        if (
            touch_at is not None
            and started_at is not None
            and started_at >= touch_at
            and started_at <= current_time
            and row.status == SubscriptionStatus.ACTIVE.value
            and end_at is not None
            and end_at > current_time
        ):
            active_trial_users.add(user_id)

    cashier_purchase_rows = (
        await db.execute(
            select(
                first_touch.c.user_id,
                func.min(func.coalesce(Transaction.completed_at, Transaction.created_at)).label('purchased_at'),
            )
            .join(Transaction, Transaction.user_id == first_touch.c.user_id)
            .outerjoin(SubscriptionCheckout, SubscriptionCheckout.id == Transaction.device_first_checkout_id)
            .where(
                first_touch.c.campaign_id == campaign_id,
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
                Transaction.amount_kopeks != 0,
                or_(
                    Transaction.device_first_ledger_key.like('direct-sale:%'),
                    Transaction.device_first_ledger_key.like('debit:%'),
                ),
                func.coalesce(Transaction.completed_at, Transaction.created_at) <= current_time,
                ~known_test_payment_clause(),
                ~post_paid_reversal_clause(),
            )
            .group_by(first_touch.c.user_id)
        )
    ).all()
    cashier_purchases = {
        int(row.user_id): purchased_at
        for row in cashier_purchase_rows
        if (purchased_at := _aware_utc(row.purchased_at)) is not None
    }

    purchase_event_rows = (
        await db.execute(
            select(
                SubscriptionEvent.user_id,
                SubscriptionEvent.occurred_at,
                SubscriptionEvent.extra,
                SubscriptionEvent.transaction_id,
                Transaction.type.label('transaction_type'),
                Transaction.is_completed.label('transaction_completed'),
                Transaction.amount_kopeks.label('transaction_amount_kopeks'),
                Transaction.device_first_ledger_key,
                known_test_payment_clause().label('transaction_is_test'),
                post_paid_reversal_clause().label('transaction_reversed'),
            )
            .join(first_touch, first_touch.c.user_id == SubscriptionEvent.user_id)
            .outerjoin(Transaction, Transaction.id == SubscriptionEvent.transaction_id)
            .outerjoin(SubscriptionCheckout, SubscriptionCheckout.id == Transaction.device_first_checkout_id)
            .where(
                first_touch.c.campaign_id == campaign_id,
                SubscriptionEvent.event_type == 'purchase',
                SubscriptionEvent.message == 'Subscription purchase',
                SubscriptionEvent.occurred_at <= current_time,
            )
            .order_by(SubscriptionEvent.user_id, SubscriptionEvent.occurred_at, SubscriptionEvent.id)
        )
    ).all()

    event_purchases: dict[int, datetime] = {}
    prior_paid_evidence: dict[int, datetime] = {}
    unresolved_purchase_events = 0
    excluded_reversed_purchases = 0
    excluded_test_purchases = 0
    for row in purchase_event_rows:
        occurred_at = _aware_utc(row.occurred_at)
        if occurred_at is None:
            unresolved_purchase_events += 1
            continue
        if occurred_at > current_time:
            continue
        classification = _purchase_event_classification(row)
        if classification == 'unresolved':
            unresolved_purchase_events += 1
            continue
        if classification == 'test':
            excluded_test_purchases += 1
            continue
        if classification == 'reversed':
            excluded_reversed_purchases += 1
            continue
        user_id = int(row.user_id)
        if classification == 'later_lifecycle':
            if user_id not in event_purchases:
                previous_evidence = prior_paid_evidence.get(user_id)
                if previous_evidence is None or occurred_at < previous_evidence:
                    prior_paid_evidence[user_id] = occurred_at
            continue
        barrier_at = prior_paid_evidence.get(user_id)
        if barrier_at is not None and barrier_at <= occurred_at:
            continue
        previous_purchase = event_purchases.get(user_id)
        if previous_purchase is None or occurred_at < previous_purchase:
            event_purchases[user_id] = occurred_at

    # Деньги первичны: событие не может отменить продажу кассы, но доказанная более ранняя
    # покупка сдвигает дату первой покупки назад — иначе кампания присвоит себе клиента,
    # который впервые заплатил ещё до касания (нашёл скептик на приёмке 20.09.2026).
    first_purchases = dict(cashier_purchases)
    for user_id, purchased_at in event_purchases.items():
        if user_id not in first_purchases or purchased_at < first_purchases[user_id]:
            first_purchases[user_id] = purchased_at

    historical_trials: dict[int, datetime] = {}
    pre_touch_trial_users: set[int] = set()
    for user_id, touch_at in touches.items():
        candidates = [trial_events.get(user_id), trial_subscriptions.get(user_id)]
        known = [candidate for candidate in candidates if candidate is not None]
        if touch_at is None:
            continue
        if known:
            first_trial_at = min(known)
            if first_trial_at < touch_at:
                pre_touch_trial_users.add(user_id)
                continue
            historical_trials[user_id] = first_trial_at

    attributed_purchases = {
        user_id: purchase_at
        for user_id, purchase_at in first_purchases.items()
        if touches.get(user_id) is not None and purchase_at >= touches[user_id]
    }
    paid_after_trial = {
        user_id
        for user_id, purchase_at in attributed_purchases.items()
        if user_id in historical_trials and historical_trials[user_id] <= purchase_at
    }
    paid_without_trial = set(attributed_purchases) - paid_after_trial

    receipt_rows = (await db.execute(_qualified_receipt_rows_statement(first_touch, campaign_id, current_time))).all()
    confirmed_receipts = sum(
        int(row.amount_kopeks or 0)
        for row in receipt_rows
        if (receipt_at := _aware_utc(row.receipt_at)) is not None and receipt_at <= current_time
    )

    leads_count = len(touches)
    trial_count = len(historical_trials)
    paid_count = len(attributed_purchases)
    ad_spend = int(campaign.ad_spend_kopeks) if campaign.ad_spend_kopeks is not None else None

    def unit_cost(denominator: int) -> int | None:
        if ad_spend is None or denominator <= 0:
            return None
        return round(ad_spend / denominator)

    gross_roas = None
    if ad_spend is not None and ad_spend > 0:
        gross_roas = round((confirmed_receipts / ad_spend) * 100, 1)

    today_moscow = current_time.astimezone(_MOSCOW).date()
    cohort_start = today_moscow - timedelta(days=29)
    cohort_buckets: dict[str, dict[str, int | bool]] = {}
    for offset in range(30):
        day = cohort_start + timedelta(days=offset)
        cohort_buckets[day.isoformat()] = {
            'leads': 0,
            'trial_users': 0,
            'paid_subscription_users': 0,
            'mature_7d': False,
        }
    for user_id, touch_at in touches.items():
        if touch_at is None:
            continue
        day = touch_at.astimezone(_MOSCOW).date()
        bucket = cohort_buckets.get(day.isoformat())
        if bucket is None:
            continue
        previous_leads = int(bucket['leads'])
        bucket['leads'] = previous_leads + 1
        if user_id in historical_trials:
            bucket['trial_users'] = int(bucket['trial_users']) + 1
        if user_id in attributed_purchases:
            bucket['paid_subscription_users'] = int(bucket['paid_subscription_users']) + 1
        lead_is_mature = current_time >= touch_at + timedelta(days=7)
        bucket['mature_7d'] = lead_is_mature if previous_leads == 0 else bool(bucket['mature_7d']) and lead_is_mature
    daily_cohorts = [{'date': date, **values} for date, values in cohort_buckets.items()]

    delay_curve: list[dict[str, int | float | None]] = []
    for hours in _PURCHASE_HORIZONS_HOURS:
        eligible = [
            user_id
            for user_id, touch_at in touches.items()
            if touch_at is not None and current_time >= touch_at + timedelta(hours=hours)
        ]
        converted = sum(
            1
            for user_id in eligible
            if user_id in attributed_purchases
            and attributed_purchases[user_id] <= touches[user_id] + timedelta(hours=hours)
        )
        delay_curve.append(
            {
                'hours': hours,
                'eligible_leads': len(eligible),
                'converted_leads': converted,
                'conversion_rate': _percent(converted, len(eligible)) if eligible else None,
            }
        )

    receipt_by_day: dict[str, int] = defaultdict(int)
    for row in receipt_rows:
        receipt_at = _aware_utc(row.receipt_at)
        if receipt_at is not None and receipt_at <= current_time:
            receipt_by_day[receipt_at.astimezone(_MOSCOW).date().isoformat()] += int(row.amount_kopeks or 0)
    first_dated_touch = min((value for value in touches.values() if value is not None), default=current_time)
    payback_start = max(first_dated_touch.astimezone(_MOSCOW).date(), today_moscow - timedelta(days=89))
    running_total = sum(
        amount for date, amount in receipt_by_day.items() if datetime.fromisoformat(date).date() < payback_start
    )
    cumulative_receipts: list[dict[str, int | str | None]] = []
    day = payback_start
    while day <= today_moscow:
        running_total += receipt_by_day.get(day.isoformat(), 0)
        cumulative_receipts.append(
            {
                'date': day.isoformat(),
                'confirmed_receipts_kopeks': running_total,
                'ad_spend_kopeks': ad_spend,
            }
        )
        day += timedelta(days=1)

    data_quality_status = (
        'partial'
        if unresolved_purchase_events or excluded_test_purchases or excluded_reversed_purchases or pre_touch_trial_users
        else 'complete'
    )

    last_lead_at = max((value for value in touches.values() if value is not None), default=None)
    last_trial_at = max(historical_trials.values(), default=None)
    last_purchase_at = max(attributed_purchases.values(), default=None)
    immature_7d = sum(
        1 for touch_at in touches.values() if touch_at is not None and current_time < touch_at + timedelta(days=7)
    )

    return {
        'campaign_id': campaign_id,
        'generated_at': current_time,
        'timezone': 'Europe/Moscow',
        'leads': leads_count,
        'historical_trial_users_count': trial_count,
        'active_trials_count': len(active_trial_users),
        'lead_to_trial_rate': _percent(trial_count, leads_count),
        'paid_subscription_users_count': paid_count,
        'lead_to_paid_subscription_rate': _percent(paid_count, leads_count),
        'paid_after_trial_count': len(paid_after_trial),
        'paid_without_trial_count': len(paid_without_trial),
        'trial_to_paid_rate': _percent(len(paid_after_trial), trial_count),
        'confirmed_receipts_kopeks': confirmed_receipts,
        'ad_spend_kopeks': ad_spend,
        'cost_per_lead_kopeks': unit_cost(leads_count),
        'cost_per_trial_kopeks': unit_cost(trial_count),
        'customer_acquisition_cost_kopeks': unit_cost(paid_count),
        'gross_roas_percent': gross_roas,
        'receipts_minus_ad_spend_kopeks': (confirmed_receipts - ad_spend if ad_spend is not None else None),
        'maturity_horizon_days': 7,
        'immature_leads_count': immature_7d,
        'last_lead_at': last_lead_at,
        'last_trial_at': last_trial_at,
        'last_paid_subscription_at': last_purchase_at,
        'data_quality': {'status': data_quality_status},
        'daily_cohorts': daily_cohorts,
        'delay_curve': delay_curve,
        'cumulative_receipts': cumulative_receipts,
    }


async def delete_campaign_if_unattributed(db: AsyncSession, campaign_id: int) -> bool:
    """Lock the campaign before checking history, fencing concurrent registrations."""

    locked_campaign_id = await db.scalar(
        select(AdvertisingCampaign.id).where(AdvertisingCampaign.id == campaign_id).with_for_update()
    )
    if locked_campaign_id is None:
        await db.rollback()
        return False

    has_registrations = await db.scalar(
        select(
            select(AdvertisingCampaignRegistration.id)
            .where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
            .exists()
        )
    )
    if has_registrations:
        await db.rollback()
        return False

    deleted = await db.execute(delete(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign_id))
    if int(deleted.rowcount or 0) != 1:
        await db.rollback()
        return False
    await db.commit()
    return True


class AdvertisingCampaignService:
    def __init__(self) -> None:
        self.subscription_service = SubscriptionService()

    async def apply_campaign_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        if not campaign.is_active:
            logger.warning('⚠️ Попытка выдать бонус по неактивной кампании', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Prevent partner from being attributed to their own campaign
        if campaign.partner_user_id and campaign.partner_user_id == user.id:
            logger.info(
                'Skipping campaign bonus: user is the campaign partner',
                user_id=user.id,
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(success=False)

        if campaign.is_balance_bonus:
            return await self._apply_balance_bonus(db, user, campaign)

        if campaign.is_subscription_bonus:
            return await self._apply_subscription_bonus(db, user, campaign)

        if campaign.is_none_bonus:
            return await self._apply_none_bonus(db, user, campaign)

        if campaign.is_tariff_bonus:
            return await self._apply_tariff_bonus(db, user, campaign)

        logger.error('❌ Неизвестный тип бонуса кампании', bonus_type=campaign.bonus_type)
        return CampaignBonusResult(success=False)

    async def _apply_balance_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        amount = campaign.balance_bonus_kopeks or 0
        if amount <= 0:
            logger.info('ℹ️ Кампания не имеет бонуса на баланс', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Регистрируем ДО начисления баланса, чтобы при повторном /start (created=False)
        # не накрутить бонус второй раз. UNIQUE constraint в record_campaign_registration
        # + savepoint защищают и от concurrent race conditions.
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='balance',
            balance_bonus_kopeks=amount,
        )

        if not created:
            logger.info(
                'ℹ️ Балансный бонус уже был начислен по этой кампании ранее, пропускаем',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(
                success=True,
                bonus_type='balance',
                balance_kopeks=amount,
                is_new_registration=False,
            )

        # 🔴 РЕК-1: имя кампании в подпись НЕ подставляем. Эта строка ложится в проводку и
        # печатается КЛИЕНТУ на вкладке «Баланс» кабинета (`cabinet/routes/balance.py` →
        # `Balance.tsx`). ⚠️ Поправка прогона сценария: в БОТЕ она сегодня недостижима —
        # у воронки нет кнопки баланса ни в одном состоянии (`keyboards/inline.py`), а команды
        # `/balance` не существует (`utils/bot_commands.py`); экран «📊 История операций»
        # (`handlers/balance/main.py`) живёт, но рекламному клиенту в него не попасть. У владельца
        # в именах кампаний рекламный бюджет («Кувалда 7000₽»), и человек читал его там же,
        # куда идёт с вопросом «откуда деньги». Причину начисления строка называть обязана —
        # приветствие о ней больше не говорит, и это её единственное объяснение.
        # ⛔ Кампанию владелец различает по АДМИНСКИМ поверхностям: уведомление о регистрации
        # (`admin_notification_service.py`) и экран статистики РК-1/РК-2 — они читают модель.
        description = 'Бонус за регистрацию'
        success = await add_user_balance(
            db,
            user,
            amount,
            description=description,
        )

        if not success:
            # Маркер регистрации остался — баланс не начислился. Это лучше, чем
            # начислить деньги без записи в БД (откатить запись теперь нельзя).
            logger.error(
                '❌ Регистрация записана, но баланс не начислился',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                amount_kopeks=amount,
            )
            return CampaignBonusResult(success=False)

        logger.info(
            '💰 Пользователю начислен бонус ₽ по кампании',
            format_user_log=_format_user_log(user),
            amount=amount / 100,
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='balance',
            balance_kopeks=amount,
            is_new_registration=created,
        )

    async def _apply_subscription_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs:
                # Multi-tariff: extend the best existing subscription instead of blocking
                _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                _pool = _non_daily or active_subs
                existing_subscription = max(_pool, key=lambda s: s.days_left)
            else:
                existing_subscription = None
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        duration_days = campaign.subscription_duration_days or 0
        if duration_days <= 0:
            logger.info('ℹ️ Кампания не содержит корректной длительности подписки', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Legacy campaign-level technical squads have no PublicLocation policy
        # or immutable tariff evidence.  Do not issue them implicitly.
        logger.error(
            'Campaign subscription bonus without tariff entitlement is retired',
            campaign_id=campaign.id,
        )
        return CampaignBonusResult(success=False)

        traffic_limit = campaign.subscription_traffic_gb
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        try:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            squads = await get_effective_tariff_squad_uuids(db, campaign.subscription_squads)
        except Exception as error:
            logger.error('Не удалось подобрать сквады для кампании', campaign_id=campaign.id, error=error)
            squads = list(campaign.subscription_squads or [])

        if existing_subscription:
            # Multi-tariff: extend the best existing subscription
            from app.database.crud.subscription import extend_subscription

            await extend_subscription(db, existing_subscription, duration_days)
            try:
                await self.subscription_service.update_remnawave_user(db, existing_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave при продлении кампании', campaign_id=campaign.id, error=error
                )

            logger.info(
                '🎁 Подписка пользователя продлена по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
                subscription_id=existing_subscription.id,
            )
        else:
            new_subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit or 0,
                device_limit=device_limit,
                connected_squads=squads,
                update_server_counters=True,
                is_trial=True,
            )

            try:
                await self.subscription_service.create_remnawave_user(db, new_subscription)
            except Exception as error:
                logger.error('❌ Ошибка синхронизации RemnaWave для кампании', campaign_id=campaign.id, error=error)

            logger.info(
                '🎁 Пользователю выдана подписка по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
            )

        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='subscription',
            subscription_duration_days=duration_days,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='subscription',
            subscription_days=duration_days,
            subscription_traffic_gb=traffic_limit or 0,
            subscription_device_limit=device_limit,
            subscription_squads=squads,
            is_new_registration=created,
        )

    async def _apply_none_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Обычная ссылка без награды - только регистрация для отслеживания."""
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='none',
        )

        logger.info(
            '📊 Пользователь зарегистрирован по ссылке кампании (без награды)',
            format_user_log=_format_user_log(user),
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='none',
            is_new_registration=created,
        )

    async def _apply_tariff_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Выдача тарифа на определённое время."""
        locked_user_id = await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
        if locked_user_id is None:
            logger.error('Пользователь исчез до выдачи тарифа кампании', user_id=user.id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        existing_registration = await db.scalar(
            select(AdvertisingCampaignRegistration).where(
                and_(
                    AdvertisingCampaignRegistration.campaign_id == campaign.id,
                    AdvertisingCampaignRegistration.user_id == user.id,
                )
            )
        )
        if existing_registration is not None:
            logger.info(
                'ℹ️ Тариф кампании уже был выдан пользователю, пропускаем',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(
                success=True,
                bonus_type='tariff',
                tariff_id=existing_registration.tariff_id or campaign.tariff_id,
                tariff_duration_days=(existing_registration.tariff_duration_days or campaign.tariff_duration_days),
                is_new_registration=False,
            )

        existing_subscription = None
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs and campaign.tariff_id:
                # Multi-tariff: only check for THIS specific tariff
                same_tariff_subs = [s for s in active_subs if s.tariff_id == campaign.tariff_id]
                if same_tariff_subs:
                    existing_subscription = max(same_tariff_subs, key=lambda s: s.days_left)
                # If no sub for this tariff, existing_subscription stays None -> create new
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус тарифа кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        if not campaign.tariff_id:
            logger.error('❌ Кампания не имеет указанного тарифа для выдачи', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        duration_days = campaign.tariff_duration_days or 0
        if duration_days <= 0:
            logger.error('❌ Кампания не имеет указанной длительности тарифа', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Получаем тариф для извлечения параметров
        tariff = await get_tariff_by_id(db, campaign.tariff_id)
        if not tariff:
            logger.error('❌ Тариф не найден для кампании', tariff_id=campaign.tariff_id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        if not tariff.is_active:
            logger.warning('⚠️ Тариф неактивен, бонус кампании пропущен', tariff_id=tariff.id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        traffic_limit = tariff.traffic_limit_gb
        device_limit = tariff.device_limit
        try:
            from app.services.public_location_entitlement_service import resolve_tariff_entitlement

            squads = list((await resolve_tariff_entitlement(db, tariff)).squad_uuids)
        except Exception as error:
            logger.error('Не удалось разрешить entitlement тарифа кампании', campaign_id=campaign.id, error=error)
            return CampaignBonusResult(success=False)

        try:
            if existing_subscription:
                # Multi-tariff: extend the existing subscription for this tariff
                from app.database.crud.subscription import extend_subscription

                await extend_subscription(
                    db,
                    existing_subscription,
                    duration_days,
                    tariff_id=tariff.id,
                    commit=False,
                )
                subscription = existing_subscription
                panel_action = 'update'
            else:
                # Создаём подписку как платную (не trial) с привязкой к тарифу
                subscription = await create_paid_subscription(
                    db=db,
                    user_id=user.id,
                    duration_days=duration_days,
                    traffic_limit_gb=traffic_limit or 0,
                    device_limit=device_limit,
                    connected_squads=squads,
                    update_server_counters=True,
                    is_trial=False,
                    tariff_id=tariff.id,
                    commit=False,
                )
                panel_action = 'create'

            registration = AdvertisingCampaignRegistration(
                campaign_id=campaign.id,
                user_id=user.id,
                bonus_type='tariff',
                balance_bonus_kopeks=0,
                subscription_duration_days=None,
                tariff_id=tariff.id,
                tariff_duration_days=duration_days,
            )
            db.add(registration)
            await db.flush()

            # Panel helpers deliberately swallow provider/DB errors and may roll
            # back their session before returning None. Capture every value needed
            # for retry and the durable response while ORM rows are still usable.
            subscription_id = subscription.id
            user_id = user.id
            campaign_id = campaign.id
            result = CampaignBonusResult(
                success=True,
                bonus_type='tariff',
                tariff_id=tariff.id,
                tariff_name=tariff.name,
                tariff_duration_days=duration_days,
                subscription_traffic_gb=traffic_limit or 0,
                subscription_device_limit=device_limit,
                subscription_squads=squads,
                is_new_registration=True,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

        try:
            if panel_action == 'update':
                panel_result = await self.subscription_service.update_remnawave_user(db, subscription)
            else:
                panel_result = await self.subscription_service.create_remnawave_user(db, subscription)
        except Exception as error:
            panel_result = None
            logger.error(
                '❌ Ошибка синхронизации RemnaWave для тарифа кампании',
                campaign_id=campaign_id,
                subscription_id=subscription_id,
                error=error,
            )

        if panel_result is None:
            try:
                await db.rollback()
            except Exception as error:
                logger.error(
                    'Не удалось очистить сессию после ошибки синхронизации тарифа кампании',
                    campaign_id=campaign_id,
                    subscription_id=subscription_id,
                    error=error,
                )

            try:
                from app.services.remnawave_retry_queue import remnawave_retry_queue

                remnawave_retry_queue.enqueue(
                    subscription_id=subscription_id,
                    user_id=user_id,
                    action=panel_action,
                )
            except Exception as error:
                # The DB grant is already durable. The retry queue is in-memory,
                # so failure here can only be surfaced for manual reconciliation;
                # it must not turn a committed grant into an apparent DB failure.
                logger.error(
                    'Не удалось поставить синхронизацию тарифа кампании в очередь',
                    campaign_id=campaign_id,
                    subscription_id=subscription_id,
                    error=error,
                )

            # rollback expires ORM state in some session configurations. The
            # bot caller still reads the passed user before its own later refresh,
            # so restore that row or propagate an operational failure only after
            # the durable panel retry has been recorded.
            await db.refresh(user)

        logger.info(
            (
                '🎁 Подписка пользователя продлена по тарифу кампании на дней'
                if panel_action == 'update'
                else '🎁 Пользователю выдан тариф по кампании на дней'
            ),
            user_id=user_id,
            tariff_name=result.tariff_name,
            campaign_id=campaign_id,
            duration_days=result.tariff_duration_days,
            subscription_id=subscription_id,
        )

        return result

"""PostgreSQL source-of-truth workflow for device-first purchases."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    create_paid_subscription,
    extend_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariffs_for_user
from app.database.models import (
    DeviceFirstMutation,
    DeviceFirstOutbox,
    Subscription,
    SubscriptionCheckout,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services.device_first_eligibility import resolve_single_eligible_tariff, tariff_eligibility
from app.services.pricing_engine import pricing_engine


OPEN_STATES = {'draft', 'confirmed', 'awaiting_funds', 'armed', 'fulfilling'}
TERMINAL_STATES = {'ready', 'cancelled', 'expired', 'failed', 'reprice_required', 'conflict'}
logger = structlog.get_logger(__name__)


def _event(name: str, checkout: SubscriptionCheckout, **fields: Any) -> None:
    logger.info(
        'device_first_event',
        action=name,
        checkout_id=checkout.public_id,
        user_id=checkout.user_id,
        **fields,
    )


class DeviceFirstError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def request_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def checkout_ui_state(checkout: SubscriptionCheckout) -> str:
    if checkout.lifecycle_state == 'ready':
        return 'ready' if checkout.provisioning_state == 'ready' else 'provisioning'
    if (
        checkout.lifecycle_state == 'fulfilling'
        and checkout.fulfillment_state == 'fulfilled'
        and checkout.provisioning_state != 'ready'
    ):
        return 'provisioning'
    if checkout.lifecycle_state in {'armed', 'fulfilling'} and checkout.fulfillment_state == 'in_progress':
        return 'processing'
    if checkout.lifecycle_state in {'reprice_required', 'conflict', 'cancelled', 'expired', 'failed'}:
        return checkout.lifecycle_state
    if checkout.lifecycle_state == 'draft':
        return 'configuration'
    if checkout.lifecycle_state == 'confirmed':
        return 'confirmation'
    if checkout.lifecycle_state in {'awaiting_funds', 'armed'}:
        return 'awaiting_payment'
    if checkout.lifecycle_state == 'fulfilling':
        return 'processing'
    return 'processing'


def serialize_checkout(
    checkout: SubscriptionCheckout,
    *,
    balance_kopeks: int | None = None,
) -> dict[str, Any]:
    snapshot_end = (checkout.target_snapshot or {}).get('end_date')
    try:
        parsed_end = datetime.fromisoformat(snapshot_end) if snapshot_end else None
    except (TypeError, ValueError):
        parsed_end = None
    base_end = parsed_end if parsed_end and parsed_end > checkout.created_at else checkout.created_at
    return {
        'id': checkout.public_id,
        'tariff_id': checkout.tariff_id,
        'target_subscription_id': checkout.target_subscription_id,
        'period_days': checkout.period_days,
        'selected_device_limit': checkout.selected_device_limit,
        'price_breakdown': checkout.price_breakdown,
        'quoted_price_kopeks': checkout.quoted_price_kopeks,
        'max_price_kopeks': checkout.max_price_kopeks,
        'quote_expires_at': checkout.quote_expires_at,
        'expires_at': checkout.expires_at,
        'lifecycle_state': checkout.lifecycle_state,
        'quote_state': checkout.quote_state,
        'funding_state': checkout.funding_state,
        'fulfillment_state': checkout.fulfillment_state,
        'provisioning_state': checkout.provisioning_state,
        'terminal_reason': checkout.terminal_reason,
        'ui_state': checkout_ui_state(checkout),
        'created_subscription_id': checkout.created_subscription_id,
        'current_device_limit': (checkout.target_snapshot or {}).get('device_limit'),
        'estimated_end_at': (
            checkout.fulfilled_end_at or (base_end + timedelta(days=checkout.period_days))
        ).isoformat(),
        'balance_kopeks': balance_kopeks,
        'shortage_kopeks': (max(0, checkout.max_price_kopeks - balance_kopeks) if balance_kopeks is not None else None),
    }


async def get_owned_checkout(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
    for_update: bool = False,
) -> SubscriptionCheckout:
    query = select(SubscriptionCheckout).where(
        SubscriptionCheckout.public_id == public_id,
        SubscriptionCheckout.user_id == user_id,
    )
    if for_update:
        # A caller may already have loaded this checkout into its identity map.
        # Refresh it after acquiring the row lock so a concurrent cancellation
        # or provider settlement cannot be hidden by stale ORM state.
        query = query.with_for_update().execution_options(populate_existing=True)
    checkout = (await db.execute(query)).scalar_one_or_none()
    if checkout is None:
        # Deliberately hide whether a foreign checkout exists.
        raise DeviceFirstError('not_found', 'Checkout not found', status_code=404)
    if (
        checkout.lifecycle_state in OPEN_STATES
        and checkout.fulfillment_state != 'fulfilled'
        and checkout.expires_at <= datetime.now(UTC)
    ):
        checkout.lifecycle_state = 'expired'
        checkout.terminal_reason = 'checkout_expired'
        checkout.quote_state = 'expired'
        await db.commit()
        await db.refresh(checkout)
    return checkout


async def get_open_checkout_for_user(
    db: AsyncSession,
    *,
    user_id: int,
) -> SubscriptionCheckout | None:
    """Return the owner's resumable checkout, never another user's checkout."""
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(
                SubscriptionCheckout.user_id == user_id,
                SubscriptionCheckout.lifecycle_state.in_(OPEN_STATES),
                SubscriptionCheckout.fulfillment_state != 'fulfilled',
            )
            .order_by(SubscriptionCheckout.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if checkout is None:
        return None
    if checkout.expires_at <= datetime.now(UTC):
        checkout.lifecycle_state = 'expired'
        checkout.terminal_reason = 'checkout_expired'
        checkout.quote_state = 'expired'
        await db.commit()
        return None
    return checkout


async def _current_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    return await get_subscription_by_user_id(db, user_id)


def _subscription_snapshot(subscription: Subscription | None) -> dict[str, Any]:
    if subscription is None:
        return {}
    return {
        'id': subscription.id,
        'tariff_id': subscription.tariff_id,
        'status': subscription.status,
        'is_trial': bool(subscription.is_trial),
        'device_limit': int(subscription.device_limit or 0),
        'end_date': subscription.end_date.isoformat() if subscription.end_date else None,
        'updated_at': subscription.updated_at.isoformat() if subscription.updated_at else None,
    }


async def build_purchase_options(db: AsyncSession, user: User) -> dict[str, Any]:
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED:
        return {'eligible': False, 'reason': 'feature_disabled'}
    promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
    if promo_group is None:
        promo_group = getattr(user, 'promo_group', None)
    tariffs = await get_tariffs_for_user(db, promo_group.id if promo_group else None)
    subscription = await _current_subscription(db, user.id)
    eligibility = resolve_single_eligible_tariff(tariffs, subscription=subscription)
    if not eligibility.eligible or eligibility.tariff is None:
        return {'eligible': False, 'reason': eligibility.reason}
    tariff = eligibility.tariff
    matrix = []
    for days in eligibility.period_options:
        prices = []
        for devices in eligibility.device_options:
            price = await pricing_engine.calculate_tariff_purchase_price(
                tariff,
                days,
                device_limit=devices,
                user=user,
            )
            prices.append(
                {
                    'device_limit': devices,
                    'price_kopeks': price.final_total,
                    'breakdown': {
                        'base_price_kopeks': price.base_price,
                        'devices_price_kopeks': price.devices_price,
                        'promo_group_discount_kopeks': price.promo_group_discount,
                        'promo_offer_discount_kopeks': price.promo_offer_discount,
                    },
                }
            )
        matrix.append({'period_days': days, 'prices': prices})
    return {
        'eligible': True,
        'tariff': {
            'id': tariff.id,
            'name': tariff.name,
            'traffic_limit_gb': tariff.traffic_limit_gb,
            'base_device_limit': tariff.device_limit,
            'pricing_revision': tariff.pricing_revision,
        },
        'device_options': list(eligibility.device_options),
        'period_options': list(eligibility.period_options),
        'default_period_days': eligibility.default_period_days,
        'current_subscription': _subscription_snapshot(subscription) or None,
        'balance_kopeks': user.balance_kopeks,
        'price_matrix': matrix,
    }


async def create_checkout(
    db: AsyncSession,
    *,
    user: User,
    period_days: int,
    selected_device_limit: int,
    source: str,
    mutation: DeviceFirstMutation | None = None,
) -> SubscriptionCheckout:
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED:
        raise DeviceFirstError('feature_disabled', 'New device-first checkouts are disabled', status_code=404)
    if user.restriction_subscription:
        raise DeviceFirstError('subscription_restricted', 'Subscription purchase is restricted', status_code=403)

    now = datetime.now(UTC)
    await db.execute(
        update(SubscriptionCheckout)
        .where(
            SubscriptionCheckout.user_id == user.id,
            SubscriptionCheckout.lifecycle_state.in_(OPEN_STATES),
            SubscriptionCheckout.fulfillment_state != 'fulfilled',
            SubscriptionCheckout.expires_at <= now,
        )
        .values(
            lifecycle_state='expired',
            quote_state='expired',
            terminal_reason='checkout_expired',
            updated_at=now,
        )
    )
    await db.commit()

    options = await build_purchase_options(db, user)
    if not options.get('eligible'):
        raise DeviceFirstError('legacy_only', 'Device-first checkout is unavailable', status_code=404)
    current_subscription = options.get('current_subscription') or {}
    if not current_subscription.get('is_trial', False) and selected_device_limit < int(
        current_subscription.get('device_limit') or 0
    ):
        raise DeviceFirstError(
            'device_limit_decrease_not_allowed',
            'A paid subscription cannot be extended with fewer devices',
            status_code=422,
        )
    if period_days not in options['period_options'] or selected_device_limit not in options['device_options']:
        raise DeviceFirstError('invalid_selection', 'Unsupported period or device limit', status_code=422)

    tariff = await db.get(Tariff, options['tariff']['id'])
    subscription = await _current_subscription(db, user.id)
    selected = next(
        price
        for row in options['price_matrix']
        if row['period_days'] == period_days
        for price in row['prices']
        if price['device_limit'] == selected_device_limit
    )
    checkout = SubscriptionCheckout(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        source=source,
        tariff_id=tariff.id,
        target_subscription_id=subscription.id if subscription else None,
        expect_no_subscription=subscription is None,
        target_snapshot=_subscription_snapshot(subscription),
        period_days=period_days,
        selected_device_limit=selected_device_limit,
        price_breakdown=selected['breakdown'],
        quoted_price_kopeks=selected['price_kopeks'],
        max_price_kopeks=selected['price_kopeks'],
        pricing_revision=int(tariff.pricing_revision or 1),
        quote_expires_at=now + timedelta(minutes=30),
        expires_at=now + timedelta(hours=24),
    )
    db.add(checkout)
    try:
        await db.flush()
        if mutation is not None:
            mutation.checkout_id = checkout.id
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise DeviceFirstError(
            'open_checkout_exists',
            'An active checkout already exists; resume or cancel it first',
        ) from error
    await db.refresh(checkout)
    _event(
        'quote_created',
        checkout,
        tariff_id=checkout.tariff_id,
        period_days=checkout.period_days,
        device_limit=checkout.selected_device_limit,
        quoted_price_kopeks=checkout.quoted_price_kopeks,
    )
    return checkout


async def confirm_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    if checkout.lifecycle_state == 'draft':
        now = datetime.now(UTC)
        if now >= checkout.quote_expires_at:
            checkout.lifecycle_state = 'reprice_required'
            checkout.quote_state = 'expired'
            checkout.terminal_reason = 'quote_expired'
        else:
            checkout.lifecycle_state = 'confirmed'
            checkout.confirmed_at = now
        await db.commit()
        await db.refresh(checkout)
        _event('confirmed' if checkout.lifecycle_state == 'confirmed' else 'reprice_required', checkout)
    return checkout


async def cancel_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    if checkout.lifecycle_state in OPEN_STATES and checkout.fulfillment_state == 'not_started':
        checkout.lifecycle_state = 'cancelled'
        checkout.terminal_reason = 'cancelled_by_user'
        await db.commit()
        await db.refresh(checkout)
        _event('cancelled', checkout)
    elif checkout.fulfillment_state != 'not_started':
        raise DeviceFirstError('invalid_state', 'A fulfilled checkout cannot be cancelled')
    return checkout


async def arm_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    if checkout.lifecycle_state not in {'confirmed', 'awaiting_funds', 'armed'}:
        raise DeviceFirstError('invalid_state', 'Checkout cannot be armed in its current state')
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED and checkout.armed_at is None:
        raise DeviceFirstError('feature_disabled', 'New checkouts are temporarily disabled')
    checkout.lifecycle_state = 'armed'
    checkout.armed_at = checkout.armed_at or datetime.now(UTC)
    await db.commit()
    _event('armed', checkout)
    return await fulfill_checkout(db, checkout.public_id, checkout.user_id)


async def fulfill_checkout(db: AsyncSession, public_id: str, user_id: int) -> SubscriptionCheckout:
    # Lock order is contractual: checkout -> user -> target subscription -> tariff.
    checkout = await get_owned_checkout(db, public_id=public_id, user_id=user_id, for_update=True)
    if checkout.lifecycle_state == 'ready':
        return checkout
    if checkout.fulfillment_state == 'fulfilled':
        return checkout
    if checkout.lifecycle_state not in {'armed', 'fulfilling'}:
        return checkout

    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    target = None
    if checkout.target_subscription_id is not None:
        target = (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.id == checkout.target_subscription_id,
                    Subscription.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
    tariff = (
        await db.execute(select(Tariff).where(Tariff.id == checkout.tariff_id).with_for_update())
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    if now >= checkout.quote_expires_at:
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'expired'
        checkout.terminal_reason = 'quote_expired'
        await db.commit()
        _event('reprice_required', checkout, reason='quote_expired')
        return checkout
    current_eligibility = tariff_eligibility(tariff, subscription=target) if tariff is not None else None
    if (
        current_eligibility is None
        or not current_eligibility.eligible
        or checkout.period_days not in current_eligibility.period_options
        or checkout.selected_device_limit not in current_eligibility.device_options
    ):
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'tariff_no_longer_eligible'
        await db.commit()
        _event('conflict', checkout, reason=checkout.terminal_reason)
        return checkout
    if checkout.expect_no_subscription:
        current = await _current_subscription(db, user_id)
        if current is not None:
            checkout.lifecycle_state = 'conflict'
            checkout.terminal_reason = 'subscription_appeared'
            await db.commit()
            _event('conflict', checkout, reason=checkout.terminal_reason)
            return checkout
    elif target is None or _subscription_snapshot(target) != checkout.target_snapshot:
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'target_subscription_changed'
        await db.commit()
        _event('conflict', checkout, reason=checkout.terminal_reason)
        return checkout

    if target is not None and not target.is_trial and checkout.selected_device_limit < int(target.device_limit or 0):
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'device_limit_decrease_not_allowed'
        await db.commit()
        _event('conflict', checkout, reason=checkout.terminal_reason)
        return checkout

    current_price = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        checkout.period_days,
        device_limit=checkout.selected_device_limit,
        user=user,
    )
    charge = current_price.final_total
    if int(tariff.pricing_revision or 1) != checkout.pricing_revision or charge != checkout.quoted_price_kopeks:
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'price_changed'
        await db.commit()
        _event('reprice_required', checkout, reason='price_changed')
        return checkout
    if user.balance_kopeks < charge:
        checkout.lifecycle_state = 'awaiting_funds'
        checkout.funding_state = 'partial' if user.balance_kopeks > 0 else 'unfunded'
        await db.commit()
        _event('awaiting_funds', checkout, balance_kopeks=user.balance_kopeks)
        return checkout

    checkout.lifecycle_state = 'fulfilling'
    checkout.fulfillment_state = 'in_progress'
    user.balance_kopeks -= charge
    user.has_had_paid_subscription = True
    ledger = Transaction(
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=-charge,
        description=(
            f'Device-first: {checkout.period_days} дн., '
            f'{checkout.selected_device_limit} устр., checkout {checkout.public_id}'
        ),
        payment_method='balance',
        external_id=f'device-first:{checkout.public_id}:debit',
        device_first_checkout_id=checkout.id,
        device_first_ledger_key=f'debit:{checkout.id}',
        is_completed=True,
        completed_at=now,
    )
    db.add(ledger)
    await db.flush()

    if target is None:
        target = await create_paid_subscription(
            db,
            user_id=user.id,
            duration_days=checkout.period_days,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=checkout.selected_device_limit,
            connected_squads=list(tariff.allowed_squads or []),
            tariff_id=tariff.id,
            commit=False,
        )
    else:
        target = await extend_subscription(
            db,
            target,
            checkout.period_days,
            tariff_id=tariff.id,
            traffic_limit_gb=tariff.traffic_limit_gb,
            device_limit=checkout.selected_device_limit,
            connected_squads=list(tariff.allowed_squads or []),
            convert_trial=True,
            commit=False,
        )

    checkout.created_subscription_id = target.id
    checkout.debit_transaction_id = ledger.id
    checkout.lifecycle_state = 'fulfilling'
    checkout.funding_state = 'funded'
    checkout.fulfillment_state = 'fulfilled'
    checkout.provisioning_state = 'pending'
    checkout.fulfilled_at = now
    checkout.fulfilled_end_at = target.end_date
    db.add(
        DeviceFirstOutbox(
            checkout_id=checkout.id,
            payload_json={'subscription_id': target.id},
        )
    )
    await db.commit()
    await db.refresh(checkout)
    _event(
        'fulfilled',
        checkout,
        subscription_id=target.id,
        debit_transaction_id=ledger.id,
        charged_kopeks=charge,
    )
    return checkout


async def reconcile_armed_checkouts(db: AsyncSession, *, limit: int = 20) -> int:
    """Resume checkouts if a process stopped after the durable arm commit."""
    rows = list(
        (
            await db.execute(
                select(
                    SubscriptionCheckout.public_id,
                    SubscriptionCheckout.user_id,
                )
                .where(
                    SubscriptionCheckout.lifecycle_state == 'armed',
                    SubscriptionCheckout.fulfillment_state != 'fulfilled',
                )
                .order_by(SubscriptionCheckout.id)
                .limit(limit)
            )
        ).all()
    )
    processed = 0
    for public_id, user_id in rows:
        try:
            await fulfill_checkout(db, public_id, user_id)
            processed += 1
        except Exception as error:
            await db.rollback()
            logger.error(
                'device_first_armed_reconciliation_failed',
                checkout_id=public_id,
                user_id=user_id,
                error=str(error),
            )
    return processed


async def process_provisioning_outbox(db: AsyncSession, *, limit: int = 20, bot=None) -> int:
    """Claim one batch, then call RemnaWave only after the claim transaction."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    rows = list(
        (
            await db.execute(
                select(DeviceFirstOutbox)
                .where(
                    or_(
                        and_(
                            DeviceFirstOutbox.status.in_(['pending', 'retry']),
                            DeviceFirstOutbox.available_at <= now,
                        ),
                        and_(
                            DeviceFirstOutbox.status == 'processing',
                            DeviceFirstOutbox.updated_at <= stale_before,
                        ),
                    )
                )
                .order_by(DeviceFirstOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = 'processing'
        row.attempts += 1
        row.updated_at = now
    await db.commit()

    from app.services.subscription_service import SubscriptionService

    processed = 0
    for row in rows:
        checkout = await db.get(SubscriptionCheckout, row.checkout_id)
        subscription = await db.get(Subscription, row.payload_json['subscription_id'])
        try:
            ok, error = await SubscriptionService().ensure_subscription_synced(db, subscription)
            row.status = 'done' if ok else 'retry'
            row.last_error = error
            row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
            checkout.provisioning_state = 'ready' if ok else 'retry'
            if ok:
                checkout.lifecycle_state = 'ready'
            if ok and bot is not None:
                user = await db.get(User, checkout.user_id)
                if user and user.telegram_id:
                    try:
                        text = (
                            '✅ Your VPN subscription is ready. Open the cabinet to connect.'
                            if user.language == 'en'
                            else '✅ Ваша VPN-подписка готова. Откройте кабинет, чтобы подключиться.'
                        )
                        await bot.send_message(user.telegram_id, text)
                    except Exception as delivery_error:
                        logger.warning(
                            'Device-first notification delivery failed',
                            checkout_id=checkout.public_id,
                            error=str(delivery_error),
                        )
            processed += int(ok)
        except Exception as error:  # worker must preserve retry evidence
            row.status = 'retry'
            row.last_error = str(error)
            row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
            checkout.provisioning_state = 'retry'
        await db.commit()
    return processed


async def store_mutation_result(
    db: AsyncSession,
    mutation: DeviceFirstMutation,
    *,
    response: dict[str, Any],
    status_code: int = 200,
) -> None:
    mutation.response_json = response
    mutation.status_code = status_code
    await db.commit()

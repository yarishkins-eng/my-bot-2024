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
    CheckoutPaymentAttempt,
    DeviceFirstMutation,
    DeviceFirstNotificationOutbox,
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
TERMINAL_STATES = {'ready', 'cancelled', 'expired', 'failed', 'reprice_required', 'conflict', 'operator_review'}
LEGACY_SETTLEMENT_MODE = 'legacy_deposit'
DIRECT_SETTLEMENT_MODE = 'direct_purchase_v2'
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


def device_first_canary_user_ids() -> frozenset[int]:
    """Parse the deliberately empty-by-default controlled-payment allowlist."""
    result: set[int] = set()
    for raw in (settings.DEVICE_FIRST_CANARY_USER_IDS or '').split(','):
        value = raw.strip()
        if not value:
            continue
        try:
            user_id = int(value)
        except ValueError:
            logger.warning('device_first_invalid_canary_configuration')
            continue
        if user_id > 0:
            result.add(user_id)
    return frozenset(result)


def is_device_first_canary_user(user: User) -> bool:
    return user.id in device_first_canary_user_ids()


def settlement_mode(row: Any) -> str:
    mode = getattr(row, 'settlement_mode', None)
    # Revision 0096 backfills every historical row before enforcing its CHECK
    # constraint.  A missing or corrupted discriminator must therefore never
    # be guessed as legacy money flow: stop it for operator review instead.
    if mode not in {LEGACY_SETTLEMENT_MODE, DIRECT_SETTLEMENT_MODE}:
        raise DeviceFirstError('operator_review_required', 'Checkout settlement mode requires operator review')
    return mode


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
    if checkout.lifecycle_state in {
        'reprice_required',
        'conflict',
        'cancelled',
        'expired',
        'failed',
        'operator_review',
    }:
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
        'settlement_mode': settlement_mode(checkout),
        'tariff_total_kopeks': checkout.tariff_total_kopeks,
        'wallet_applied_kopeks': checkout.wallet_applied_kopeks,
        'external_payable_kopeks': checkout.external_payable_kopeks,
        'funding_mode': checkout.funding_mode,
        # This payload is also persisted verbatim for idempotency replays.
        # Keep it JSON-native, not merely FastAPI-serializable at the HTTP edge.
        'quote_expires_at': checkout.quote_expires_at.isoformat() if checkout.quote_expires_at else None,
        'expires_at': checkout.expires_at.isoformat() if checkout.expires_at else None,
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
    # An exact provider payment is already committed before its fulfilment
    # worker runs.  Its checkout must outlive the general UI timeout so a
    # delayed worker cannot strand accepted money behind an expired quote.
    fulfillment_committed = (
        getattr(checkout, 'fulfillment_state', 'not_started') == 'in_progress'
        and getattr(checkout, 'quote_state', None) == 'committed'
    )
    if (
        checkout.lifecycle_state in OPEN_STATES
        and checkout.fulfillment_state != 'fulfilled'
        and not fulfillment_committed
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
    # Direct checkout money is committed before provisioning.  Keep that
    # owner-visible recovery record resumable until fenced provisioning reports
    # ready, even though its fulfilment step is already complete.
    direct_provisioning_recovery = and_(
        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
        SubscriptionCheckout.lifecycle_state == 'fulfilling',
        SubscriptionCheckout.fulfillment_state == 'fulfilled',
        SubscriptionCheckout.provisioning_state.in_(['pending', 'retry']),
    )
    # A terminal *direct-sale* record is still a recovery record: an amount
    # mismatch, post-paid reversal, or operator hold must not disappear after
    # a WebView restart and silently allow a second order to start. Historical
    # legacy terminals stay out of this projection to preserve their old UI.
    terminal_recovery = and_(
        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
        SubscriptionCheckout.lifecycle_state.in_(TERMINAL_STATES),
    )
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(
                SubscriptionCheckout.user_id == user_id,
                or_(
                    and_(
                        SubscriptionCheckout.lifecycle_state.in_(OPEN_STATES),
                        SubscriptionCheckout.fulfillment_state != 'fulfilled',
                    ),
                    direct_provisioning_recovery,
                    terminal_recovery,
                ),
            )
            .order_by(SubscriptionCheckout.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if checkout is None:
        return None
    direct_paid_recovery = (
        settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE
        and checkout.lifecycle_state == 'fulfilling'
        and checkout.fulfillment_state == 'fulfilled'
    )
    # An operator hold after direct payment is a durable owner-facing recovery
    # record, not an expirable quote.  Hiding it would let a restarted client
    # start another order while the original money still needs reconciliation.
    direct_operator_hold = (
        settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE and checkout.lifecycle_state == 'operator_review'
    )
    if not (direct_paid_recovery or direct_operator_hold) and checkout.expires_at <= datetime.now(UTC):
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
    if not is_device_first_canary_user(user):
        return {'eligible': False, 'reason': 'canary_not_allowed'}
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
        'version': 2,
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
    # The per-user direct-sale fence serializes a late provider reversal with a
    # new quote.  Every direct financial commit takes this same lock before it
    # can create an invoice or debit the wallet.
    user = (await db.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
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
            ~and_(
                SubscriptionCheckout.fulfillment_state == 'in_progress',
                SubscriptionCheckout.quote_state == 'committed',
            ),
        )
        .values(
            lifecycle_state='expired',
            quote_state='expired',
            terminal_reason='checkout_expired',
            updated_at=now,
        )
    )

    operator_hold = (
        await db.execute(
            select(SubscriptionCheckout.id)
            .where(
                SubscriptionCheckout.user_id == user.id,
                SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                SubscriptionCheckout.lifecycle_state == 'operator_review',
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if operator_hold is not None:
        raise DeviceFirstError(
            'operator_review_required',
            'An earlier direct payment requires operator review before a new checkout',
        )

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
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        tariff_total_kopeks=selected['price_kopeks'],
        wallet_applied_kopeks=0,
        external_payable_kopeks=0,
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
    if settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE:
        raise DeviceFirstError('direct_commit_required', 'Choose a funding method before the final purchase')
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
            settlement_mode=LEGACY_SETTLEMENT_MODE,
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


async def _lock_direct_context(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> tuple[SubscriptionCheckout, User, Subscription | None, Tariff]:
    """Lock direct commits in the canonical order: user → checkout → sub → tariff."""
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    checkout = await get_owned_checkout(db, public_id=public_id, user_id=user_id, for_update=True)
    if settlement_mode(checkout) != DIRECT_SETTLEMENT_MODE:
        raise DeviceFirstError('legacy_checkout', 'This older checkout uses its original settlement path')
    operator_hold = (
        await db.execute(
            select(SubscriptionCheckout.id)
            .where(
                SubscriptionCheckout.user_id == user.id,
                SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                SubscriptionCheckout.lifecycle_state == 'operator_review',
                SubscriptionCheckout.id != checkout.id,
            )
            .with_for_update()
            .limit(1)
        )
    ).scalar_one_or_none()
    if operator_hold is not None:
        raise DeviceFirstError(
            'operator_review_required',
            'An earlier direct payment requires operator review before another purchase can continue',
        )
    target = None
    if checkout.target_subscription_id is not None:
        target = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == checkout.target_subscription_id, Subscription.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    tariff = (
        await db.execute(select(Tariff).where(Tariff.id == checkout.tariff_id).with_for_update())
    ).scalar_one_or_none()
    if tariff is None:
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'tariff_missing_after_quote'
        await db.commit()
        raise DeviceFirstError('operator_review_required', 'The order requires operator review')
    return checkout, user, target, tariff


async def _validate_direct_pre_commit(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    user: User,
    target: Subscription | None,
    tariff: Tariff,
) -> bool:
    """Fail before money if the quoted target is no longer safe to sell."""
    now = datetime.now(UTC)
    if checkout.lifecycle_state != 'confirmed':
        raise DeviceFirstError('invalid_state', 'Checkout is not ready for final confirmation')
    if now >= checkout.quote_expires_at:
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'expired'
        checkout.terminal_reason = 'quote_expired'
        await db.commit()
        return False
    if checkout.expect_no_subscription:
        if await _current_subscription(db, user.id) is not None:
            checkout.lifecycle_state = 'conflict'
            checkout.terminal_reason = 'subscription_appeared'
            await db.commit()
            return False
    elif target is None or _subscription_snapshot(target) != checkout.target_snapshot:
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'target_subscription_changed'
        await db.commit()
        return False
    if target is not None and not target.is_trial and checkout.selected_device_limit < int(target.device_limit or 0):
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'device_limit_decrease_not_allowed'
        await db.commit()
        return False
    eligibility = tariff_eligibility(tariff, subscription=target)
    if (
        not eligibility.eligible
        or checkout.period_days not in eligibility.period_options
        or checkout.selected_device_limit not in eligibility.device_options
    ):
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'tariff_no_longer_eligible'
        await db.commit()
        return False
    current_price = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        checkout.period_days,
        device_limit=checkout.selected_device_limit,
        user=user,
    )
    if (
        int(tariff.pricing_revision or 1) != checkout.pricing_revision
        or current_price.final_total != checkout.tariff_total_kopeks
    ):
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'price_changed'
        await db.commit()
        return False
    return True


def _direct_sale_snapshot(checkout: SubscriptionCheckout, tariff: Tariff, *, funding_mode: str) -> dict[str, Any]:
    """The post-confirmation source of truth; no later tariff repricing is allowed."""
    return {
        'tariff_id': checkout.tariff_id,
        'tariff_name': tariff.name,
        'period_days': checkout.period_days,
        'device_limit': checkout.selected_device_limit,
        'traffic_limit_gb': tariff.traffic_limit_gb,
        # Required to fulfil an immutable entitlement without consulting a
        # mutable tariff after an exact external payment.
        'allowed_squads': list(tariff.allowed_squads or []),
        'currency': 'RUB',
        'tariff_total_kopeks': checkout.tariff_total_kopeks,
        'price_breakdown': checkout.price_breakdown,
        'pricing_revision': checkout.pricing_revision,
        'funding_mode': funding_mode,
        'target_snapshot': checkout.target_snapshot,
        'expires_at': checkout.quote_expires_at.isoformat(),
    }


async def prepare_direct_external_checkout(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
    commit: bool = True,
) -> SubscriptionCheckout:
    """Freeze a full-price external sale before the provider POST.

    ``commit=False`` lets the invoice intent and protected payment row commit
    in the same transaction as the funding choice. No v2 checkout can then be
    durable in ``invoice_pending`` without a durable attempt to recover.
    """
    checkout, user, target, tariff = await _lock_direct_context(db, public_id=public_id, user_id=user_id)
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED or not is_device_first_canary_user(user):
        raise DeviceFirstError('feature_disabled', 'New checkouts are temporarily disabled')
    if checkout.financial_committed_at is not None:
        if checkout.funding_mode != 'platega':
            raise DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
        # A committed v2 checkout is never generically re-opened.  The only
        # non-terminal state that can be inspected by payment recovery is the
        # exact pre-fulfilment invoice state; terminal/paid/order-review
        # checkouts must stay fenced from any further provider POST.
        if (
            checkout.lifecycle_state != 'awaiting_funds'
            or checkout.funding_state != 'invoice_pending'
            or checkout.fulfillment_state != 'not_started'
            or checkout.created_subscription_id is not None
            or checkout.debit_transaction_id is not None
        ):
            raise DeviceFirstError('invalid_state', 'Checkout cannot accept another payment invoice')
        return checkout
    if not await _validate_direct_pre_commit(db, checkout=checkout, user=user, target=target, tariff=tariff):
        raise DeviceFirstError('reprice_required', 'The quote changed; create a new checkout')
    total = checkout.tariff_total_kopeks
    checkout.wallet_applied_kopeks = 0
    checkout.external_payable_kopeks = total
    checkout.funding_mode = 'platega'
    checkout.sale_snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='platega')
    checkout.financial_committed_at = datetime.now(UTC)
    checkout.lifecycle_state = 'awaiting_funds'
    checkout.funding_state = 'invoice_pending'
    if commit:
        await db.commit()
        await db.refresh(checkout)
    else:
        await db.flush()
    return checkout


async def _complete_direct_sale_locked(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    user: User,
    target: Subscription | None,
    provider_payment_id: str | None = None,
) -> SubscriptionCheckout:
    """Create exactly one direct-sale ledger and local subscription in this transaction."""
    snapshot = dict(checkout.sale_snapshot or {})
    total = int(snapshot.get('tariff_total_kopeks') or 0)
    if (
        not snapshot
        or snapshot.get('currency') != 'RUB'
        or total <= 0
        or total != checkout.tariff_total_kopeks
        or snapshot.get('funding_mode') != checkout.funding_mode
    ):
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'invalid_sale_snapshot'
        raise DeviceFirstError('operator_review_required', 'The immutable sale snapshot is invalid')
    if checkout.expect_no_subscription:
        if await _current_subscription(db, user.id) is not None:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = 'subscription_appeared_after_payment'
            raise DeviceFirstError('operator_review_required', 'Subscription changed after payment')
    elif target is None or _subscription_snapshot(target) != snapshot.get('target_snapshot'):
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'target_subscription_changed_after_payment'
        raise DeviceFirstError('operator_review_required', 'Subscription changed after payment')

    if checkout.fulfillment_state == 'fulfilled':
        return checkout
    now = datetime.now(UTC)
    if checkout.funding_mode == 'wallet':
        if checkout.wallet_applied_kopeks != total or checkout.external_payable_kopeks != 0:
            raise DeviceFirstError('operator_review_required', 'Invalid wallet settlement values')
        if user.balance_kopeks < total:
            raise DeviceFirstError('wallet_insufficient', 'The balance no longer covers the checkout', status_code=422)
        user.balance_kopeks -= total
    elif checkout.funding_mode == 'platega':
        if checkout.wallet_applied_kopeks != 0 or checkout.external_payable_kopeks != total or not provider_payment_id:
            raise DeviceFirstError('operator_review_required', 'Invalid provider settlement values')
        receipt_key = f'provider-receipt:{checkout.id}'
        receipt = (
            await db.execute(select(Transaction).where(Transaction.device_first_ledger_key == receipt_key))
        ).scalar_one_or_none()
        if receipt is None:
            db.add(
                Transaction(
                    user_id=user.id,
                    type=TransactionType.PROVIDER_RECEIPT.value,
                    amount_kopeks=total,
                    description=f'Device-first provider receipt for checkout {checkout.public_id}',
                    payment_method='platega',
                    external_id=provider_payment_id,
                    device_first_checkout_id=checkout.id,
                    device_first_ledger_key=receipt_key,
                    is_completed=True,
                    completed_at=now,
                )
            )
    else:
        raise DeviceFirstError('operator_review_required', 'Unknown funding method')

    sale_key = f'direct-sale:{checkout.id}'
    sale = (
        await db.execute(select(Transaction).where(Transaction.device_first_ledger_key == sale_key))
    ).scalar_one_or_none()
    if sale is None:
        sale = Transaction(
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT.value,
            amount_kopeks=-total,
            description=(
                f'Device-first direct sale: {snapshot["period_days"]} days, '
                f'{snapshot["device_limit"]} devices, checkout {checkout.public_id}'
            ),
            payment_method='balance' if checkout.funding_mode == 'wallet' else 'platega',
            external_id=f'device-first-sale:{checkout.public_id}',
            device_first_checkout_id=checkout.id,
            device_first_ledger_key=sale_key,
            is_completed=True,
            completed_at=now,
        )
        db.add(sale)
        await db.flush()

    if target is None:
        target = await create_paid_subscription(
            db,
            user_id=user.id,
            duration_days=int(snapshot['period_days']),
            traffic_limit_gb=int(snapshot['traffic_limit_gb']),
            device_limit=int(snapshot['device_limit']),
            connected_squads=list(snapshot['allowed_squads']),
            tariff_id=int(snapshot['tariff_id']),
            commit=False,
        )
    else:
        target = await extend_subscription(
            db,
            target,
            int(snapshot['period_days']),
            tariff_id=int(snapshot['tariff_id']),
            traffic_limit_gb=int(snapshot['traffic_limit_gb']),
            device_limit=int(snapshot['device_limit']),
            connected_squads=list(snapshot['allowed_squads']),
            convert_trial=True,
            commit=False,
        )
    user.has_had_paid_subscription = True
    checkout.created_subscription_id = target.id
    checkout.debit_transaction_id = sale.id
    checkout.lifecycle_state = 'fulfilling'
    checkout.funding_state = 'funded'
    checkout.fulfillment_state = 'fulfilled'
    checkout.provisioning_state = 'pending'
    checkout.fulfilled_at = now
    checkout.fulfilled_end_at = target.end_date
    existing_outbox = (
        await db.execute(
            select(DeviceFirstOutbox).where(
                DeviceFirstOutbox.checkout_id == checkout.id,
                DeviceFirstOutbox.event_type == 'sync_subscription',
            )
        )
    ).scalar_one_or_none()
    if existing_outbox is None:
        db.add(
            DeviceFirstOutbox(
                checkout_id=checkout.id,
                settlement_mode=DIRECT_SETTLEMENT_MODE,
                payload_json={'subscription_id': target.id, 'checkout_id': checkout.public_id},
            )
        )
    return checkout


async def commit_direct_wallet_checkout(
    db: AsyncSession,
    *,
    public_id: str,
    user_id: int,
) -> SubscriptionCheckout:
    """The explicit full-wallet path; partial balance is never combined with Platega."""
    checkout, user, target, tariff = await _lock_direct_context(db, public_id=public_id, user_id=user_id)
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED or not is_device_first_canary_user(user):
        raise DeviceFirstError('feature_disabled', 'New checkouts are temporarily disabled')
    if checkout.financial_committed_at is not None:
        if checkout.funding_mode != 'wallet':
            raise DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
        return checkout
    if not await _validate_direct_pre_commit(db, checkout=checkout, user=user, target=target, tariff=tariff):
        raise DeviceFirstError('reprice_required', 'The quote changed; create a new checkout')
    total = checkout.tariff_total_kopeks
    if user.balance_kopeks < total:
        raise DeviceFirstError('wallet_insufficient', 'The balance does not cover this checkout', status_code=422)
    checkout.wallet_applied_kopeks = total
    checkout.external_payable_kopeks = 0
    checkout.funding_mode = 'wallet'
    checkout.sale_snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='wallet')
    checkout.financial_committed_at = datetime.now(UTC)
    await _complete_direct_sale_locked(db, checkout=checkout, user=user, target=target)
    await db.commit()
    await db.refresh(checkout)
    return checkout


async def fulfill_direct_external_checkout(
    db: AsyncSession,
    *,
    checkout_id: int,
    provider_payment_id: str,
    payment_attempt_id: int | None = None,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> SubscriptionCheckout:
    """Recovery-safe completion after an authenticated exact provider payment."""
    if (lease_token is None) != (lease_epoch is None):
        raise ValueError('direct fulfilment lease requires both token and epoch')
    if payment_attempt_id is None:
        raise DeviceFirstError('invalid_state', 'Direct fulfilment requires its paid payment attempt')
    # Locking contract for direct paid paths is user → attempt → checkout.
    # Read the immutable owner key without a row lock first; taking Attempt
    # before User would deadlock a concurrent post-paid reversal, which fences
    # the same three rows in this canonical order.
    checkout_owner_id = await db.scalar(
        select(SubscriptionCheckout.user_id).where(SubscriptionCheckout.id == checkout_id)
    )
    if checkout_owner_id is None:
        raise DeviceFirstError('invalid_state', 'Direct checkout is no longer eligible for fulfilment')
    user = (await db.execute(select(User).where(User.id == checkout_owner_id).with_for_update())).scalar_one()
    attempt_predicates = [
        CheckoutPaymentAttempt.id == payment_attempt_id,
        CheckoutPaymentAttempt.checkout_id == checkout_id,
        CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
        # A provider reversal/operator hold wins before sale issuance.  The
        # previous callback can no longer fulfil an already fenced checkout.
        CheckoutPaymentAttempt.status == 'paid_processing',
    ]
    if lease_token is not None and lease_epoch is not None:
        attempt_predicates.extend(
            [
                CheckoutPaymentAttempt.lease_token == lease_token,
                CheckoutPaymentAttempt.lease_epoch == lease_epoch,
                CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
            ]
        )
    owned_attempt = (
        await db.execute(select(CheckoutPaymentAttempt).where(*attempt_predicates).with_for_update())
    ).scalar_one_or_none()
    if owned_attempt is None:
        raise DeviceFirstError('invalid_state', 'Direct payment attempt is no longer eligible for fulfilment')
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == checkout_id, SubscriptionCheckout.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one()
    if settlement_mode(checkout) != DIRECT_SETTLEMENT_MODE:
        raise DeviceFirstError('legacy_checkout', 'This checkout is not a direct sale')
    # Fulfilment is deliberately idempotent: the direct attempt remains
    # ``paid_processing`` while the provisioning outbox runs, so a later
    # reconciler pass must observe the durable sale rather than turn it into a
    # noisy retry. Operator-review/terminal states do not take this path.
    if checkout.fulfillment_state == 'fulfilled' and checkout.lifecycle_state in {'fulfilling', 'ready'}:
        return checkout
    if (
        checkout.lifecycle_state != 'fulfilling'
        or checkout.funding_state != 'paid'
        or checkout.fulfillment_state != 'not_started'
    ):
        raise DeviceFirstError('invalid_state', 'Direct checkout is no longer eligible for fulfilment')
    target = None
    if checkout.target_subscription_id is not None:
        target = (
            await db.execute(
                select(Subscription)
                .where(Subscription.id == checkout.target_subscription_id, Subscription.user_id == checkout.user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
    try:
        result = await _complete_direct_sale_locked(
            db,
            checkout=checkout,
            user=user,
            target=target,
            provider_payment_id=provider_payment_id,
        )
    except DeviceFirstError:
        await db.commit()
        raise
    await db.commit()
    await db.refresh(result)
    return result


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
    """Drain legacy work and fence v2 provisioning before any RemnaWave call."""
    processed = await _process_direct_provisioning_outbox(db, limit=limit)
    # Historical deposits remain drainable with their established worker.  v2
    # rows never enter this unfenced legacy branch.
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    rows = list(
        (
            await db.execute(
                select(DeviceFirstOutbox)
                .where(
                    DeviceFirstOutbox.settlement_mode == LEGACY_SETTLEMENT_MODE,
                    or_(
                        and_(
                            DeviceFirstOutbox.status.in_(['pending', 'retry']),
                            DeviceFirstOutbox.available_at <= now,
                        ),
                        and_(
                            DeviceFirstOutbox.status == 'processing',
                            DeviceFirstOutbox.updated_at <= stale_before,
                        ),
                    ),
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

    legacy_processed = 0
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
            legacy_processed += int(ok)
        except Exception as error:  # worker must preserve retry evidence
            row.status = 'retry'
            row.last_error = str(error)
            row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
            checkout.provisioning_state = 'retry'
        await db.commit()
    return processed + legacy_processed


async def _process_direct_provisioning_outbox(db: AsyncSession, *, limit: int) -> int:
    """Lease-fenced v2 delivery; a stale worker can never mark a newer lease done."""
    now = datetime.now(UTC)
    rows = list(
        (
            await db.execute(
                select(DeviceFirstOutbox)
                .where(
                    DeviceFirstOutbox.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    or_(
                        and_(
                            DeviceFirstOutbox.status.in_(['pending', 'retry']),
                            DeviceFirstOutbox.available_at <= now,
                            or_(
                                DeviceFirstOutbox.lease_token.is_(None),
                                DeviceFirstOutbox.lease_expires_at < now,
                            ),
                        ),
                        # A worker may die after committing its claim and before
                        # the RemnaWave call. Reclaim only its expired fenced
                        # lease; an old token still cannot complete afterward.
                        and_(
                            DeviceFirstOutbox.status == 'processing',
                            DeviceFirstOutbox.lease_expires_at < now,
                        ),
                    ),
                )
                .order_by(DeviceFirstOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claims: list[tuple[int, str]] = []
    for row in rows:
        token = uuid.uuid4().hex
        row.status = 'processing'
        row.attempts += 1
        row.lease_token = token
        row.lease_expires_at = now + timedelta(minutes=5)
        row.lease_epoch += 1
        claims.append((row.id, token))
    if claims:
        await db.commit()

    from app.services.subscription_service import SubscriptionService

    processed = 0
    for row_id, token in claims:
        row = await db.get(DeviceFirstOutbox, row_id)
        if row is None:
            continue
        subscription = await db.get(Subscription, row.payload_json['subscription_id'])
        try:
            ok, error = await SubscriptionService().ensure_subscription_synced(db, subscription)
        except Exception as error:  # no stale write; preserve concise retry evidence
            ok = False
            error = type(error).__name__
        current = (
            await db.execute(
                select(DeviceFirstOutbox)
                .where(
                    DeviceFirstOutbox.id == row_id,
                    DeviceFirstOutbox.lease_token == token,
                    DeviceFirstOutbox.status == 'processing',
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            continue
        checkout = (
            await db.execute(
                select(SubscriptionCheckout).where(SubscriptionCheckout.id == current.checkout_id).with_for_update()
            )
        ).scalar_one()
        current.lease_token = None
        current.lease_expires_at = None
        current.last_error = error
        if ok:
            current.status = 'done'
            checkout.provisioning_state = 'ready'
            checkout.lifecycle_state = 'ready'
            notification = (
                await db.execute(
                    select(DeviceFirstNotificationOutbox).where(
                        DeviceFirstNotificationOutbox.checkout_id == checkout.id,
                        DeviceFirstNotificationOutbox.notification_type == 'ready',
                    )
                )
            ).scalar_one_or_none()
            if notification is None:
                db.add(DeviceFirstNotificationOutbox(checkout_id=checkout.id, notification_type='ready'))
            processed += 1
        else:
            current.status = 'retry'
            current.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**current.attempts))
            checkout.provisioning_state = 'retry'
        await db.commit()
    return processed


async def process_device_first_notification_outbox(db: AsyncSession, *, bot, limit: int = 20) -> int:
    """Send at most once: a crash after handing off to Telegram stays ``sending``."""
    if bot is None:
        return 0
    rows = list(
        (
            await db.execute(
                select(DeviceFirstNotificationOutbox)
                .where(DeviceFirstNotificationOutbox.status == 'pending')
                .order_by(DeviceFirstNotificationOutbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    claims: list[tuple[int, str]] = []
    for row in rows:
        token = uuid.uuid4().hex
        row.status = 'sending'
        row.lease_token = token
        row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        row.sending_at = datetime.now(UTC)
        claims.append((row.id, token))
    if claims:
        await db.commit()

    sent = 0
    for row_id, token in claims:
        row = await db.get(DeviceFirstNotificationOutbox, row_id)
        if row is None:
            continue
        checkout = await db.get(SubscriptionCheckout, row.checkout_id)
        user = await db.get(User, checkout.user_id) if checkout else None
        error: str | None = None
        try:
            if user is None or not user.telegram_id:
                raise RuntimeError('telegram_recipient_unavailable')
            text = (
                '✅ Your VPN subscription is ready. Open the cabinet to connect.'
                if user.language == 'en'
                else '✅ Ваша VPN-подписка готова. Откройте кабинет, чтобы подключиться.'
            )
            await bot.send_message(user.telegram_id, text)
        except Exception as exc:  # do not retry an uncertain external send
            error = type(exc).__name__
        current = (
            await db.execute(
                select(DeviceFirstNotificationOutbox)
                .where(DeviceFirstNotificationOutbox.id == row_id, DeviceFirstNotificationOutbox.lease_token == token)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            continue
        current.lease_token = None
        current.lease_expires_at = None
        if error is None:
            current.status = 'sent'
            current.sent_at = datetime.now(UTC)
            sent += 1
        else:
            current.status = 'failed'
            current.last_error = error
        await db.commit()
    return sent


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

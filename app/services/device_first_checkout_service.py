"""PostgreSQL source-of-truth workflow for device-first purchases."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import structlog
from sqlalchemy import and_, exists, func, or_, select, update
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
    SubscriptionEntitlementTerm,
    SubscriptionEntitlementTermProjectionOutbox,
    SubscriptionStatus,
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
KOPEKS_PER_RUBLE = 100
READY_NOTIFICATION_TYPE = 'ready'
OWNER_ALERT_NOTIFICATION_TYPE = 'order_stuck'
# Окно свежести для строки владельцу. Оно же — второй замок от смертельной ловушки:
# пять архивных заказов тарифа 3 не обновлялись с 03.08.2026, в окно они не попадают.
OWNER_ALERT_LOOKBACK = timedelta(hours=24)
# Сколько раз выдача должна отлежаться в повторе, прежде чем это перестанет быть
# «панель моргнула» и станет «оплата не доехала». Повторы при этом НЕ прекращаются.
OWNER_ALERT_STUCK_PROVISION_ATTEMPTS = 5
# Повтор уведомлений (пункт 4.8). Колонок attempts/available_at у этой таблицы нет,
# заводить их нельзя (app/database → exit 14 → ручной воркфлоу), поэтому паузу между
# попытками держим по updated_at, а число попыток не храним вовсе.
NOTIFICATION_RETRY_AFTER = timedelta(minutes=10)
NOTIFICATION_MAX_AGE = timedelta(hours=6)
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


def round_device_first_quote_kopeks(amount_kopeks: int) -> int:
    """Make the device-first order amount a whole-ruble customer price.

    Tariff and promo calculations deliberately retain kopek precision elsewhere
    in the product. This checkout is the exception requested by the product:
    its quote, balance debit and provider invoice must all be the same whole
    number of rubles. A half kopek-ruble rounds up.
    """
    amount = max(0, int(amount_kopeks))
    return ((amount + KOPEKS_PER_RUBLE // 2) // KOPEKS_PER_RUBLE) * KOPEKS_PER_RUBLE


def device_first_top_up_kopeks(*, price_kopeks: int, balance_kopeks: int) -> int:
    """Return a whole-ruble, provider-valid top-up that funds the quote.

    Balance can contain kopeks from previous payments. We round a positive
    shortfall up, and enforce Platega's configured minimum, so the invoice shown
    to the customer never underfunds an order or fails after the CTA. Any small
    excess remains on the user's balance.
    """
    shortage = max(0, int(price_kopeks) - int(balance_kopeks))
    if shortage == 0:
        return 0
    whole_ruble_shortage = ((shortage + KOPEKS_PER_RUBLE - 1) // KOPEKS_PER_RUBLE) * KOPEKS_PER_RUBLE
    minimum_top_up = (
        (max(0, int(settings.PLATEGA_MIN_AMOUNT_KOPEKS)) + KOPEKS_PER_RUBLE - 1) // KOPEKS_PER_RUBLE
    ) * KOPEKS_PER_RUBLE
    return max(whole_ruble_shortage, minimum_top_up)


def device_first_top_up_surplus_kopeks(*, price_kopeks: int, balance_kopeks: int) -> int:
    """Return the part of a provider-valid top-up that remains on balance."""
    raw_shortage = max(0, int(price_kopeks) - int(balance_kopeks))
    if raw_shortage == 0:
        return 0
    return (
        device_first_top_up_kopeks(
            price_kopeks=price_kopeks,
            balance_kopeks=balance_kopeks,
        )
        - raw_shortage
    )


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


def device_first_new_checkouts_enabled() -> bool:
    """Return whether new v2 checkouts are open in either approved rollout mode."""
    return bool(settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED or settings.DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED)


def is_device_first_canary_user(user: User) -> bool:
    """Allow every eligible user in public mode, otherwise retain the canary fence."""
    if settings.DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED:
        return True
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
    target_snapshot = checkout.target_snapshot or {}
    snapshot_end = target_snapshot.get('end_date')
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
        # The same payload is returned to the client and persisted in the
        # idempotency record. Keep timestamps JSON-native for both paths.
        'quote_expires_at': checkout.quote_expires_at.isoformat(),
        'expires_at': checkout.expires_at.isoformat(),
        'lifecycle_state': checkout.lifecycle_state,
        'quote_state': checkout.quote_state,
        'funding_state': checkout.funding_state,
        'fulfillment_state': checkout.fulfillment_state,
        'provisioning_state': checkout.provisioning_state,
        'terminal_reason': checkout.terminal_reason,
        'ui_state': checkout_ui_state(checkout),
        'created_subscription_id': checkout.created_subscription_id,
        'current_device_limit': target_snapshot.get('device_limit'),
        # The client must not infer that a trial's temporary device count is a
        # paid subscription being changed.  A missing key is intentionally
        # unknown (rather than False) for old checkout snapshots.
        'current_subscription_is_trial': (
            target_snapshot.get('is_trial') if isinstance(target_snapshot.get('is_trial'), bool) else None
        ),
        'estimated_end_at': (
            checkout.fulfilled_end_at or (base_end + timedelta(days=checkout.period_days))
        ).isoformat(),
        'balance_kopeks': balance_kopeks,
        'shortage_kopeks': (
            device_first_top_up_kopeks(
                price_kopeks=checkout.max_price_kopeks,
                balance_kopeks=balance_kopeks,
            )
            if balance_kopeks is not None
            else None
        ),
        'top_up_surplus_kopeks': (
            device_first_top_up_surplus_kopeks(
                price_kopeks=checkout.max_price_kopeks,
                balance_kopeks=balance_kopeks,
            )
            if balance_kopeks is not None
            else None
        ),
    }


async def expire_checkout_quote_if_needed(
    db: AsyncSession,
    checkout: SubscriptionCheckout,
) -> bool:
    """Expire an open quote before any money-moving transition.

    A device-first quote is deliberately short-lived: its price is a snapshot
    of tariff, promo and device rules.  The same transition is used by the
    browser, native Telegram flow and payment adapter so an expired quote can
    never create a fresh provider invoice or debit a balance.
    """
    # Once an exact provider payment or balance debit has been atomically
    # committed, the user already approved this snapshot. Do not turn a slow
    # outbox worker into a surprise repricing; only pre-payment quotes expire.
    # A quote remains a cancellable draft only until the customer chooses an
    # external method.  Once an exact provider invoice exists, its immutable
    # amount is the customer's approved financial snapshot.  A local UI TTL
    # must never invalidate it while Platega can still confirm it; the
    # provider-status lifecycle owns that transition from this point on.
    has_direct_provider_attempt = (
        settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE
        and await db.scalar(
            select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
        )
        is not None
    )
    if (
        checkout.lifecycle_state not in OPEN_STATES
        or has_direct_provider_attempt
        or (
            getattr(checkout, 'fulfillment_state', 'not_started') == 'in_progress'
            and getattr(checkout, 'quote_state', None) == 'committed'
        )
        or datetime.now(UTC) < checkout.quote_expires_at
    ):
        return False

    checkout.lifecycle_state = 'reprice_required'
    checkout.quote_state = 'expired'
    checkout.terminal_reason = 'quote_expired'
    await db.commit()
    _event('reprice_required', checkout, reason='quote_expired')
    return True


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
    # An exact provider payment is already credited to the internal balance
    # before its fulfilment worker runs.  Its checkout must remain resumable
    # beyond the general 24-hour UI timeout: expiring it would strand the
    # customer's payment with no way for the outbox to finish the order.
    fulfillment_committed = (
        getattr(checkout, 'fulfillment_state', 'not_started') == 'in_progress'
        and getattr(checkout, 'quote_state', None) == 'committed'
    )
    has_direct_provider_attempt = (
        settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE
        and await db.scalar(
            select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
        )
        is not None
    )
    if (
        checkout.lifecycle_state in OPEN_STATES
        and checkout.fulfillment_state != 'fulfilled'
        and not fulfillment_committed
        and not has_direct_provider_attempt
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
    # Only an explicit financial/operator hold remains owner-visible as a
    # blocker. Every other terminal record is immutable audit history and must
    # not trap the customer in an old calculation. In particular a verified
    # cancelled/expired/failed invoice releases the next quote; a late
    # CONFIRMED is still fenced by its attempt and becomes a reconciliation
    # credit rather than an old subscription.
    terminal_recovery = and_(
        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
        SubscriptionCheckout.lifecycle_state == 'operator_review',
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
    has_direct_provider_attempt = (
        settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE
        and await db.scalar(
            select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
        )
        is not None
    )
    if not (
        direct_paid_recovery or direct_operator_hold or has_direct_provider_attempt
    ) and checkout.expires_at <= datetime.now(UTC):
        checkout.lifecycle_state = 'expired'
        checkout.terminal_reason = 'checkout_expired'
        checkout.quote_state = 'expired'
        await db.commit()
        return None
    return checkout


async def _current_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    return await get_subscription_by_user_id(db, user_id)


async def _has_locked_legacy_pending_trial(db: AsyncSession, *, user_id: int) -> bool:
    """Return whether an old externally-paid trial still owns a payment outcome.

    The historical paid-trial implementation created a ``PENDING, is_trial``
    subscription *before* redirecting to its payment provider.  Device-first
    sales must never repurpose that row: a later legacy callback would then
    have accepted money but find no pending trial to activate.  There is no
    safe generic provider cancellation contract for those old invoices, so the
    only correct transition is to reconcile that payment before a direct sale
    can be created or finalised.

    Every caller already holds the per-user financial lock.  Taking the
    subscription row lock here preserves the direct-flow order of User →
    Checkout/Attempt → Subscription and makes the callback race deterministic.
    """
    result = await db.execute(
        select(Subscription.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.PENDING.value,
            Subscription.is_trial.is_(True),
        )
        .with_for_update()
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _require_no_legacy_pending_trial(
    db: AsyncSession,
    *,
    user_id: int,
    checkout: SubscriptionCheckout | None = None,
) -> None:
    """Fence a direct sale while a historical paid-trial callback is possible."""
    if not await _has_locked_legacy_pending_trial(db, user_id=user_id):
        return

    if checkout is not None:
        # An already-created older checkout must not mutate the historical
        # trial row after this release.  If it has already been paid, its
        # receipt stays durable and this explicit hold gives an operator one
        # auditable reconciliation point instead of silently losing either
        # payment's entitlement.
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'legacy_pending_trial_reconciliation_required'
        await db.commit()
        _event('operator_review', checkout, reason=checkout.terminal_reason)

    raise DeviceFirstError(
        'legacy_trial_reconciliation_required',
        'A previous trial payment is still awaiting reconciliation before another purchase can continue',
    )


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
    if not device_first_new_checkouts_enabled():
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
            if price.final_total <= 0:
                # A full promo/free period must retain the established trial or
                # gift semantics, never be mistaken for a paid checkout.
                return {'eligible': False, 'reason': 'non_positive_quote'}
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
            'description': tariff.description,
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
    initial_lifecycle_state: str = 'draft',
) -> SubscriptionCheckout:
    """Persist one checkout; ``initial_lifecycle_state`` never changes legacy births.

    The deprecated showcase callers keep the ``draft`` default untouched.  The
    fused pay-time resolver passes ``confirmed`` so the row is born ready for
    the existing direct commit chains inside the same payment request; that
    ``confirmed`` state lives for microseconds and is never a browsing draft.
    """
    if initial_lifecycle_state not in {'draft', 'confirmed'}:
        raise ValueError(f'Unsupported initial checkout lifecycle state: {initial_lifecycle_state}')
    if not device_first_new_checkouts_enabled():
        raise DeviceFirstError('feature_disabled', 'New device-first checkouts are disabled', status_code=404)
    # The per-user direct-sale fence serializes a late provider reversal with a
    # new quote.  Every direct financial commit takes this same lock before it
    # can create an invoice or debit the wallet.
    user = (await db.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
    if getattr(user, 'account_erasure_requested_at', None) is not None:
        raise DeviceFirstError(
            'account_closing',
            'Account closure is in progress; new checkout creation is unavailable',
            status_code=403,
        )
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
            ~and_(
                SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                exists().where(CheckoutPaymentAttempt.checkout_id == SubscriptionCheckout.id),
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

    # ``PENDING + is_trial`` belongs exclusively to the retired external
    # paid-trial flow.  Do not let a new direct quote target or overwrite it:
    # provider callbacks for that historical invoice must retain an intact
    # local entitlement until they are reconciled.
    await _require_no_legacy_pending_trial(db, user_id=user.id)

    # A new explicit configuration replaces only an uncommitted direct quote.
    # This closes the Cabinet C1 state (create → confirm, no provider method
    # yet) atomically under the per-user fence.  Once an attempt exists it is
    # never touched here: it must resume, be locally abandoned by the
    # dedicated P->U->A->C path, or be reconciled from the provider.
    stale_pre_invoice = list(
        (
            await db.execute(
                select(SubscriptionCheckout)
                .where(
                    SubscriptionCheckout.user_id == user.id,
                    SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    SubscriptionCheckout.lifecycle_state.in_({'draft', 'confirmed', 'awaiting_funds'}),
                    SubscriptionCheckout.fulfillment_state == 'not_started',
                    ~exists().where(CheckoutPaymentAttempt.checkout_id == SubscriptionCheckout.id),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for stale_checkout in stale_pre_invoice:
        stale_checkout.lifecycle_state = 'cancelled'
        stale_checkout.quote_state = 'expired'
        stale_checkout.funding_state = 'not_started'
        stale_checkout.terminal_reason = 'superseded_before_payment_method'
        stale_checkout.updated_at = now
    if stale_pre_invoice:
        await db.flush()

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

    # Share a tariff-row lock with the native-squad editor.  Thus either a
    # checkout captures the pre-change AP entitlement first (and blocks the
    # conversion), or the tariff conversion commits first and this checkout
    # resolves the new native selection — never a mixed invoice.
    tariff = await db.get(Tariff, options['tariff']['id'], with_for_update=True, populate_existing=True)
    if tariff is None:
        raise DeviceFirstError('tariff_missing', 'The selected tariff no longer exists', status_code=404)
    subscription = await _current_subscription(db, user.id)
    selected = next(
        price
        for row in options['price_matrix']
        if row['period_days'] == period_days
        for price in row['prices']
        if price['device_limit'] == selected_device_limit
    )
    if int(selected['price_kopeks']) <= 0:
        raise DeviceFirstError('non_positive_quote', 'Free quotes use the legacy flow', status_code=422)
    from app.services.public_location_entitlement_service import (
        EntitlementResolutionError,
        get_subscription_resolved_entitlement,
        resolve_tariff_entitlement,
    )

    try:
        # A renewal keeps the currently issued squad set until an explicit
        # admin propagation updates that subscription.  A brand-new sale (or
        # a trial conversion) quotes the tariff's current native selection.
        quoted_entitlement = (
            await get_subscription_resolved_entitlement(db, subscription.id)
            if subscription is not None
            and not subscription.is_trial
            and getattr(tariff, 'entitlement_mode', None) == 'native_squads'
            else await resolve_tariff_entitlement(db, tariff, access_point_quote_context=True)
        )
    except EntitlementResolutionError as error:
        # A point tariff with stale/unsafe evidence must fail before creating
        # even a disposable checkout.  This avoids an unpaid quote that could
        # later be mistaken for a valid funding intent.
        raise DeviceFirstError('location_policy_not_sellable', str(error)) from error
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
        entitlement_quote_snapshot=_entitlement_quote_snapshot(quoted_entitlement),
        lifecycle_state=initial_lifecycle_state,
        confirmed_at=now if initial_lifecycle_state == 'confirmed' else None,
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
    # ``quote_created`` stays the deprecated-showcase metric: on the transition
    # it still means "a browsing draft appeared".  A fused pay-time birth is a
    # different funnel and gets its own event so the two never mix.
    _event(
        'quote_created' if initial_lifecycle_state == 'draft' else 'fused_checkout_created',
        checkout,
        tariff_id=checkout.tariff_id,
        period_days=checkout.period_days,
        device_limit=checkout.selected_device_limit,
        quoted_price_kopeks=checkout.quoted_price_kopeks,
    )
    return checkout


class FusedDirectCheckout(NamedTuple):
    """Outcome of the pay-time checkout resolver.

    ``proceed_to_payment`` is False when an already-paid or operator-held
    checkout won the race: the caller must render that canonical state and
    never create or fund anything else in this request.
    """

    checkout: SubscriptionCheckout
    proceed_to_payment: bool


def _fused_selection_price(options: dict[str, Any], *, period_days: int, selected_device_limit: int) -> int:
    """Extract the raw server price for one selection from purchase options."""
    selected = next(
        (
            price
            for row in options['price_matrix']
            if row['period_days'] == period_days
            for price in row['prices']
            if price['device_limit'] == selected_device_limit
        ),
        None,
    )
    if selected is None:
        raise DeviceFirstError('invalid_selection', 'Unsupported period or device limit', status_code=422)
    # Raw kopeks, never rounded: the client token is optimistic and only an
    # exact match proves it was rendered from the current server price.
    return int(selected['price_kopeks'])


async def _resume_fused_direct_checkout(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    user_id: int,
    funding_mode: str,
    lock_row: bool,
    mutation: DeviceFirstMutation | None,
) -> FusedDirectCheckout:
    """Resume the one open direct checkout after a fresh re-read.

    A paid/provisioning/held checkout is its own canonical answer.  A live
    committed invoice rejects a funding switch instead of a silent supersede.
    Only a committable row proceeds to payment (``confirmed``, or a committed
    invoice in ``awaiting_funds``): an expired draft comes back from
    ``confirm_checkout`` as ``reprice_required`` and a pre-commit
    ``awaiting_funds`` crash row can never pass the direct pre-commit
    validation, so both get the canonical reprice flow instead of a generic
    commit-chain error.

    ``lock_row`` must be False whenever the per-user fence is not held (the
    optimistic phase): a surviving Checkout lock would be followed by the
    commit chain's User lock, inverting the canonical U -> C order that the
    provider settle/reversal paths rely on (``prepare_direct_external_checkout``
    deliberately does not pre-lock the checkout for the same reason).  The
    resume verdicts here are read-only decisions; the commit chain
    authoritatively re-validates everything under U -> C in
    ``_lock_direct_context``/``_validate_direct_pre_commit``.
    """
    locked = await get_owned_checkout(db, public_id=checkout.public_id, user_id=user_id, for_update=lock_row)
    if locked.lifecycle_state not in {'draft', 'confirmed', 'awaiting_funds'}:
        # armed/fulfilling/operator_review/recovery already took money or are
        # provisioning it: their canonical state is the answer.
        if mutation is not None:
            mutation.checkout_id = locked.id
        return FusedDirectCheckout(checkout=locked, proceed_to_payment=False)
    if (
        locked.financial_committed_at is not None
        and locked.funding_mode is not None
        and locked.funding_mode != funding_mode
    ):
        # A funding switch over a live provider invoice is never a silent
        # supersede: the client must show its explicit abandon screen (a late
        # payment becomes one wallet credit).
        raise DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
    # The immutable invoice/quote owns its approved price, so a resume
    # deliberately ignores the optimistic expected price.
    if locked.lifecycle_state == 'draft':
        locked = await confirm_checkout(db, locked)
    committable = locked.lifecycle_state == 'confirmed' or (
        locked.lifecycle_state == 'awaiting_funds' and locked.financial_committed_at is not None
    )
    if not committable:
        raise DeviceFirstError('reprice_required', 'The quote changed; review the new quote')
    if mutation is not None:
        mutation.checkout_id = locked.id
    return FusedDirectCheckout(checkout=locked, proceed_to_payment=True)


async def create_or_resume_direct_checkout(
    db: AsyncSession,
    *,
    user: User,
    period_days: int,
    selected_device_limit: int,
    expected_tariff_total_kopeks: int,
    funding_mode: str,
    method_key: str | None,
    source: str,
    mutation: DeviceFirstMutation | None = None,
) -> FusedDirectCheckout:
    """Resolve the one direct checkout at the moment of payment.

    New purchase paths never persist a browsing draft: a click on «Pay» is the
    only event that may birth a durable checkout, and it is born ``confirmed``
    for the existing direct commit chains.  The order is contractual: the raw
    server price first, and only then resume/supersede/create — so a stale
    optimistic price can never insert a junk row or cancel a live invoice.
    """
    # Deferred import: the payment service already imports this module.
    from app.services.device_first_payment_service import abandon_direct_checkout_for_new_calculation

    if funding_mode not in {'wallet', 'platega'}:
        raise DeviceFirstError('invalid_funding_request', 'Unknown funding mode', status_code=422)
    logger.info(
        'direct_pay_clicked',
        user_id=user.id,
        period_days=period_days,
        device_limit=selected_device_limit,
        funding_mode=funding_mode,
        method_key=method_key,
        expected_tariff_total_kopeks=int(expected_tariff_total_kopeks),
        source=source,
        flow_version='fused_v1',
    )
    # Lock-order contract: the provider settle/reversal paths take their locks
    # as Payment -> User -> Attempt -> Checkout, so this resolver must never
    # hold the per-user fence while reaching for a PlategaPayment lock.  The
    # price/selection reads, the open-checkout read and the supersede below
    # therefore run WITHOUT the fence; the authoritative re-read in phase 4
    # and every money step afterwards (``create_checkout``,
    # ``_lock_direct_context``) take the fence first and re-validate under it.

    # Phase 1: optimistic, lock-free price and selection validation.
    options = await build_purchase_options(db, user)
    if not options.get('eligible'):
        raise DeviceFirstError('legacy_only', 'Device-first checkout is unavailable', status_code=404)
    server_price_kopeks = _fused_selection_price(
        options, period_days=period_days, selected_device_limit=selected_device_limit
    )
    expected_kopeks = int(expected_tariff_total_kopeks)

    # Phase 2: cheap rejections that must leave every live row untouched.
    if funding_mode == 'platega' and not (
        settings.PLATEGA_MIN_AMOUNT_KOPEKS <= server_price_kopeks <= settings.PLATEGA_MAX_AMOUNT_KOPEKS
    ):
        # A checkout committed at an out-of-range amount could never produce
        # its provider invoice: reject before any INSERT, not after.
        raise DeviceFirstError(
            'provider_amount_out_of_range', 'Required amount is outside Platega limits', status_code=422
        )
    if funding_mode == 'wallet' and user.balance_kopeks < server_price_kopeks:
        # An unfunded wallet click must neither leave a durable checkout row
        # nor abandon a live invoice; the wallet commit chain re-checks the
        # balance under its own fence.
        raise DeviceFirstError('wallet_insufficient', 'The balance does not cover this checkout', status_code=422)

    # Phase 3: optimistic open-checkout read; any supersede happens here,
    # before the per-user fence, so the abandon path keeps its canonical
    # P -> U -> A -> C lock order.
    existing = await get_open_checkout_for_user(db, user_id=user.id)
    if existing is not None and settlement_mode(existing) == DIRECT_SETTLEMENT_MODE:
        same_configuration = (
            existing.period_days == period_days and existing.selected_device_limit == selected_device_limit
        )
        if same_configuration:
            # No fence is held here, so the resume must not leave a Checkout
            # row lock behind: the commit chain takes User -> Checkout itself
            # and a held C lock would invert that canonical order.
            return await _resume_fused_direct_checkout(
                db,
                checkout=existing,
                user_id=user.id,
                funding_mode=funding_mode,
                lock_row=False,
                mutation=mutation,
            )
        # A different selection is replaced only after the client token is
        # proven fresh; a stale price must never cancel the live invoice.
        if expected_kopeks != server_price_kopeks:
            raise DeviceFirstError('reprice_required', 'The price changed; review the new quote')
        abandoned = await abandon_direct_checkout_for_new_calculation(
            db,
            checkout_public_id=existing.public_id,
            user_id=user.id,
        )
        if abandoned is None:
            # No provider attempt exists: a disposable pre-invoice quote is
            # replaced atomically, exactly like the deprecated create route.
            locked_quote = await get_owned_checkout(
                db,
                public_id=existing.public_id,
                user_id=user.id,
                for_update=True,
            )
            cancelled = await cancel_checkout_for_new_calculation(db, locked_quote)
            if cancelled.lifecycle_state != 'cancelled':
                # A recovery transition won the row meanwhile: never fall
                # through to a second INSERT; answer its canonical state.
                if mutation is not None:
                    mutation.checkout_id = cancelled.id
                return FusedDirectCheckout(checkout=cancelled, proceed_to_payment=False)
        elif abandoned.lifecycle_state != 'cancelled':
            # A provider callback won the financial lock while the customer
            # changed parameters; its canonical state is the response.
            if mutation is not None:
                mutation.checkout_id = abandoned.id
            return FusedDirectCheckout(checkout=abandoned, proceed_to_payment=False)
    elif expected_kopeks != server_price_kopeks:
        # No live checkout to preserve and a stale optimistic price: reject
        # before any INSERT, so browsing never leaves junk rows behind.
        raise DeviceFirstError('reprice_required', 'The price changed; review the new quote')

    # Phase 4: the authoritative pass under the per-user fence.  Everything
    # above was optimistic; re-read and re-price before any INSERT.
    user = (await db.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
    fenced_open = await get_open_checkout_for_user(db, user_id=user.id)
    if fenced_open is not None and settlement_mode(fenced_open) == DIRECT_SETTLEMENT_MODE:
        fenced_same_configuration = (
            fenced_open.period_days == period_days and fenced_open.selected_device_limit == selected_device_limit
        )
        if fenced_same_configuration:
            # A concurrent click created/resumed this checkout between the
            # optimistic read and the fence: resume it, never a second order.
            # The fence is held, so the row lock follows the canonical U -> C.
            return await _resume_fused_direct_checkout(
                db,
                checkout=fenced_open,
                user_id=user.id,
                funding_mode=funding_mode,
                lock_row=True,
                mutation=mutation,
            )
        # A different fresh checkout won the race: its canonical state is the
        # answer, never a competing INSERT.
        if mutation is not None:
            mutation.checkout_id = fenced_open.id
        return FusedDirectCheckout(checkout=fenced_open, proceed_to_payment=False)

    fenced_options = await build_purchase_options(db, user)
    if not fenced_options.get('eligible'):
        raise DeviceFirstError('legacy_only', 'Device-first checkout is unavailable', status_code=404)
    fenced_price_kopeks = _fused_selection_price(
        fenced_options, period_days=period_days, selected_device_limit=selected_device_limit
    )
    if expected_kopeks != fenced_price_kopeks:
        raise DeviceFirstError('reprice_required', 'The price changed; review the new quote')
    if funding_mode == 'platega' and not (
        settings.PLATEGA_MIN_AMOUNT_KOPEKS <= fenced_price_kopeks <= settings.PLATEGA_MAX_AMOUNT_KOPEKS
    ):
        raise DeviceFirstError(
            'provider_amount_out_of_range', 'Required amount is outside Platega limits', status_code=422
        )
    if funding_mode == 'wallet' and user.balance_kopeks < fenced_price_kopeks:
        raise DeviceFirstError('wallet_insufficient', 'The balance does not cover this checkout', status_code=422)

    checkout = await create_checkout(
        db,
        user=user,
        period_days=period_days,
        selected_device_limit=selected_device_limit,
        source=source,
        mutation=mutation,
        initial_lifecycle_state='confirmed',
    )
    return FusedDirectCheckout(checkout=checkout, proceed_to_payment=True)


async def confirm_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    if checkout.lifecycle_state == 'draft':
        if await expire_checkout_quote_if_needed(db, checkout):
            return checkout
        checkout.lifecycle_state = 'confirmed'
        checkout.confirmed_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(checkout)
        _event('confirmed', checkout)
    return checkout


async def cancel_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    # A direct checkout becomes financially irreversible for the UI as soon
    # as a provider attempt exists.  Platega does not expose a reliable
    # cancellation API, so an old invoice could still be paid after a local
    # cancellation.  Never let that produce a second quote/order.
    # A checkout that has already entered fulfilment is never cancellable.
    # Check it before any provider lookup: it preserves the contract for
    # already-paid/processing orders and avoids treating them as an invoice
    # recovery case merely because they use the direct settlement mode.
    if checkout.fulfillment_state != 'not_started':
        raise DeviceFirstError('invalid_state', 'A fulfilled checkout cannot be cancelled')
    if checkout.lifecycle_state not in OPEN_STATES:
        return checkout
    if settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE:
        external_attempt_exists = await db.scalar(
            select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
        )
        if external_attempt_exists is not None:
            raise DeviceFirstError(
                'external_invoice_active',
                'An external payment invoice is already active and cannot be cancelled',
            )
    checkout.lifecycle_state = 'cancelled'
    checkout.terminal_reason = 'cancelled_by_user'
    await db.commit()
    await db.refresh(checkout)
    _event('cancelled', checkout)
    return checkout


async def cancel_checkout_for_new_calculation(
    db: AsyncSession,
    checkout: SubscriptionCheckout,
) -> SubscriptionCheckout:
    """Close only a checkout that cannot still collect money.

    ``Тарифы`` is an explicit request to start over.  A v2 quote with no
    provider attempt is safe to cancel in every pre-fulfilment open state,
    including the historical ``awaiting_funds`` pre-attempt crash state.  A
    provider attempt always remains a recovery record: provider terminal
    statuses can still be followed by a late paid callback, so no historic
    external attempt is ever released automatically.

    The caller must own a freshly ``FOR UPDATE``-locked checkout.  This is a
    narrow entry-point policy; generic cancellation after an invoice remains
    deliberately blocked by :func:`cancel_checkout`.
    """
    if (
        settlement_mode(checkout) != DIRECT_SETTLEMENT_MODE
        or checkout.lifecycle_state not in {'draft', 'confirmed', 'awaiting_funds'}
        or checkout.fulfillment_state != 'not_started'
    ):
        return checkout

    attempts = list(
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(CheckoutPaymentAttempt.checkout_id == checkout.id)
                .order_by(CheckoutPaymentAttempt.id.desc())
            )
        ).scalars()
    )
    if not attempts:
        return await cancel_checkout(db, checkout)

    return checkout


async def arm_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> SubscriptionCheckout:
    if settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE:
        raise DeviceFirstError('direct_commit_required', 'Choose a funding method before the final purchase')
    if checkout.lifecycle_state not in {'confirmed', 'awaiting_funds', 'armed'}:
        raise DeviceFirstError('invalid_state', 'Checkout cannot be armed in its current state')
    if not device_first_new_checkouts_enabled() and checkout.armed_at is None:
        raise DeviceFirstError('feature_disabled', 'New checkouts are temporarily disabled')
    if await expire_checkout_quote_if_needed(db, checkout):
        return checkout
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

    # One authoritative completion time is persisted both on the balance debit
    # and on the checkout.  It must be defined before any successful-payment
    # mutation, otherwise an exact provider payment would fail with NameError
    # after the order has been armed but before the debit/fulfilment commit.
    now = datetime.now(UTC)

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

    if await expire_checkout_quote_if_needed(db, checkout):
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

    from app.services.public_location_entitlement_service import (
        EntitlementResolutionError,
        get_subscription_resolved_entitlement,
        resolve_tariff_entitlement,
    )

    try:
        entitlement = (
            await get_subscription_resolved_entitlement(db, target.id)
            if target is not None
            and not target.is_trial
            and getattr(tariff, 'entitlement_mode', None) == 'native_squads'
            else await resolve_tariff_entitlement(db, tariff)
        )
    except EntitlementResolutionError as error:
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'location_policy_not_sellable'
        await db.commit()
        _event('conflict', checkout, reason=checkout.terminal_reason, error=str(error))
        return checkout

    current_price = await pricing_engine.calculate_tariff_purchase_price(
        tariff,
        checkout.period_days,
        device_limit=checkout.selected_device_limit,
        user=user,
    )
    charge = round_device_first_quote_kopeks(current_price.final_total)
    if charge <= 0:
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'non_positive_quote'
        await db.commit()
        _event('conflict', checkout, reason=checkout.terminal_reason)
        return checkout
    if int(tariff.pricing_revision or 1) != checkout.pricing_revision or charge != checkout.quoted_price_kopeks:
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'price_changed'
        await db.commit()
        _event('reprice_required', checkout, reason='price_changed')
        return checkout
    if user.balance_kopeks < charge:
        had_committed_payment = checkout.fulfillment_state == 'in_progress' and checkout.quote_state == 'committed'
        if had_committed_payment:
            # A concurrent balance action consumed funds after an exact
            # provider payment. The deposit remains in the ledger, but the old
            # quote must not retain its post-payment expiry exemption forever.
            checkout.fulfillment_state = 'not_started'
            checkout.quote_state = 'valid'
            if datetime.now(UTC) >= checkout.quote_expires_at:
                checkout.lifecycle_state = 'reprice_required'
                checkout.quote_state = 'expired'
                checkout.terminal_reason = 'quote_expired'
                await db.commit()
                _event('reprice_required', checkout, reason='funds_spent_after_payment')
                return checkout
        checkout.lifecycle_state = 'awaiting_funds'
        checkout.funding_state = 'partial' if user.balance_kopeks > 0 else 'unfunded'
        await db.commit()
        _event('awaiting_funds', checkout, balance_kopeks=user.balance_kopeks)
        return checkout

    checkout.lifecycle_state = 'fulfilling'
    checkout.fulfillment_state = 'in_progress'
    checkout.quote_state = 'committed'
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
            connected_squads=list(entitlement.squad_uuids),
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
            connected_squads=list(entitlement.squad_uuids),
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
):
    """Fail before money if price, target or entitlement quote drifted.

    The returned object is the immutable entitlement captured at quote birth.
    It is intentionally handed to the funding and fulfilment stages so they
    never resolve a newer tariff policy after the customer accepted a price.
    """
    await _require_no_legacy_pending_trial(db, user_id=user.id, checkout=checkout)
    now = datetime.now(UTC)
    if checkout.lifecycle_state != 'confirmed':
        raise DeviceFirstError('invalid_state', 'Checkout is not ready for final confirmation')
    if now >= checkout.quote_expires_at:
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'expired'
        checkout.terminal_reason = 'quote_expired'
        await db.commit()
        return None
    if checkout.expect_no_subscription:
        if await _current_subscription(db, user.id) is not None:
            checkout.lifecycle_state = 'conflict'
            checkout.terminal_reason = 'subscription_appeared'
            await db.commit()
            return None
    elif target is None or _subscription_snapshot(target) != checkout.target_snapshot:
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'target_subscription_changed'
        await db.commit()
        return None
    if target is not None and not target.is_trial and checkout.selected_device_limit < int(target.device_limit or 0):
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'device_limit_decrease_not_allowed'
        await db.commit()
        return None
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
        return None
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
        return None
    try:
        quoted_entitlement = _load_checkout_quoted_entitlement(checkout)
    except (KeyError, TypeError, ValueError):
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'entitlement_quote_missing_or_invalid'
        await db.commit()
        return None
    from app.services.public_location_entitlement_service import (
        EntitlementResolutionError,
        get_subscription_resolved_entitlement,
        resolve_tariff_entitlement,
    )

    try:
        # Quote finalisation locks the tariff and every selected AP evidence
        # row through the debit/invoice transaction.  Discovery/policy writes
        # acquire the same row locks on UPDATE, eliminating a quote→money
        # inventory or policy TOCTOU window.
        locked_tariff = await db.scalar(
            select(Tariff).where(Tariff.id == tariff.id).with_for_update().execution_options(populate_existing=True)
        )
        if locked_tariff is None:
            raise EntitlementResolutionError('tariff disappeared during final quote validation')
        tariff = locked_tariff
        locked_price = await pricing_engine.calculate_tariff_purchase_price(
            tariff,
            checkout.period_days,
            device_limit=checkout.selected_device_limit,
            user=user,
        )
        if (
            int(tariff.pricing_revision or 1) != checkout.pricing_revision
            or locked_price.final_total != checkout.tariff_total_kopeks
        ):
            checkout.lifecycle_state = 'reprice_required'
            checkout.quote_state = 'price_changed'
            checkout.terminal_reason = 'price_changed'
            await db.commit()
            return None
        current_entitlement = (
            await get_subscription_resolved_entitlement(db, target.id)
            if target is not None
            and not target.is_trial
            and getattr(tariff, 'entitlement_mode', None) == 'native_squads'
            else await resolve_tariff_entitlement(
                db,
                tariff,
                access_point_quote_context=True,
                lock_access_point_evidence=True,
            )
        )
    except EntitlementResolutionError:
        current_entitlement = None
    if current_entitlement is None or current_entitlement.snapshot_hash != quoted_entitlement.snapshot_hash:
        # Host title-only presentation changes do not change the AP
        # fingerprint/hash.  A policy, mapping or health drift does.
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'price_changed'
        checkout.terminal_reason = 'entitlement_changed'
        await db.commit()
        return None
    return quoted_entitlement


def _entitlement_from_payload(raw_entitlement: object):
    """Deserialize immutable quote/sale evidence without consulting tariff state."""

    from app.services.public_location_entitlement_service import ResolvedEntitlement

    if not isinstance(raw_entitlement, dict):
        raise ValueError('entitlement payload is not an object')
    return ResolvedEntitlement(
        tuple(raw_entitlement['location_ids']),
        tuple(raw_entitlement['technical_squad_uuids']),
        int(raw_entitlement['policy_revision']),
        str(raw_entitlement['provenance']),
        (
            str(raw_entitlement['inventory_fingerprint'])
            if raw_entitlement.get('inventory_fingerprint') is not None
            else None
        ),
    )


def _entitlement_quote_snapshot(entitlement) -> dict[str, Any]:
    """Store the access fence at quote birth, before funding selection."""

    return {
        'entitlement': entitlement.snapshot_payload(),
        'entitlement_hash': entitlement.snapshot_hash,
        'policy_revision': entitlement.policy_revision,
        'inventory_fingerprint': entitlement.inventory_fingerprint,
    }


def _load_checkout_quoted_entitlement(checkout: SubscriptionCheckout):
    """Reject malformed/missing immutable quote evidence before money."""

    snapshot = dict(getattr(checkout, 'entitlement_quote_snapshot', None) or {})
    entitlement = _entitlement_from_payload(snapshot.get('entitlement'))
    if (
        entitlement.snapshot_hash != snapshot.get('entitlement_hash')
        or entitlement.policy_revision != snapshot.get('policy_revision')
        or entitlement.inventory_fingerprint != snapshot.get('inventory_fingerprint')
    ):
        raise ValueError('checkout entitlement quote hash is invalid')
    return entitlement


def _direct_sale_snapshot(
    checkout: SubscriptionCheckout, tariff: Tariff, *, funding_mode: str, entitlement
) -> dict[str, Any]:
    """The post-confirmation source of truth; no later tariff repricing is allowed."""
    return {
        'tariff_id': checkout.tariff_id,
        'tariff_name': tariff.name,
        'period_days': checkout.period_days,
        'device_limit': checkout.selected_device_limit,
        'traffic_limit_gb': tariff.traffic_limit_gb,
        # Immutable business and technical evidence captured before provider
        # invoice creation.  Fulfilment after capture must not re-resolve the
        # mutable tariff policy.
        'entitlement': entitlement.snapshot_payload(),
        'entitlement_hash': entitlement.snapshot_hash,
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
    if not device_first_new_checkouts_enabled() or not is_device_first_canary_user(user):
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
    entitlement = await _validate_direct_pre_commit(db, checkout=checkout, user=user, target=target, tariff=tariff)
    if entitlement is None:
        raise DeviceFirstError('reprice_required', 'The quote changed; create a new checkout')
    total = checkout.tariff_total_kopeks
    checkout.wallet_applied_kopeks = 0
    checkout.external_payable_kopeks = total
    checkout.funding_mode = 'platega'
    checkout.sale_snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='platega', entitlement=entitlement)
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
    await _require_no_legacy_pending_trial(db, user_id=user.id, checkout=checkout)
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
    from app.services.public_location_entitlement_service import EntitlementResolutionError, ResolvedEntitlement

    raw_entitlement = snapshot.get('entitlement') or {}
    try:
        entitlement = ResolvedEntitlement(
            tuple(raw_entitlement['location_ids']),
            tuple(raw_entitlement['technical_squad_uuids']),
            int(raw_entitlement['policy_revision']),
            str(raw_entitlement['provenance']),
            (
                str(raw_entitlement['inventory_fingerprint'])
                if raw_entitlement.get('inventory_fingerprint') is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'invalid_entitlement_snapshot'
        raise DeviceFirstError('operator_review_required', 'The entitlement snapshot is invalid') from error
    if entitlement.snapshot_hash != snapshot.get('entitlement_hash'):
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'entitlement_snapshot_hash_mismatch'
        raise DeviceFirstError('operator_review_required', 'The entitlement snapshot was modified')
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
    elif checkout.funding_mode == 'platega':
        if checkout.wallet_applied_kopeks != 0 or checkout.external_payable_kopeks != total or not provider_payment_id:
            raise DeviceFirstError('operator_review_required', 'Invalid provider settlement values')
    else:
        raise DeviceFirstError('operator_review_required', 'Unknown funding method')

    if entitlement.provenance == 'access_point_policy':
        # The invoice may have remained unpaid after the quote-finalisation
        # transaction released its locks.  A later policy/health/graph change
        # must not be silently delivered after provider capture.  Re-check
        # only for equality under the same tariff/evidence locks; never use a
        # newly resolved policy as the customer's entitlement.
        from app.services.public_location_entitlement_service import resolve_tariff_entitlement

        try:
            locked_tariff = await db.scalar(
                select(Tariff)
                .where(Tariff.id == int(snapshot['tariff_id']))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            current_entitlement = (
                await resolve_tariff_entitlement(
                    db,
                    locked_tariff,
                    access_point_quote_context=True,
                    lock_access_point_evidence=True,
                )
                if locked_tariff is not None
                else None
            )
        except (EntitlementResolutionError, ValueError):
            current_entitlement = None
        if current_entitlement is None or current_entitlement.snapshot_hash != entitlement.snapshot_hash:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = 'captured_entitlement_changed_after_payment'
            raise DeviceFirstError(
                'operator_review_required',
                'The captured access evidence changed after the unpaid invoice',
            )

    # The subscription row/immutable term is prepared before the debit or
    # provider receipt is made durable.  A term constraint or source-fence
    # failure therefore rolls back the entire financial transition.
    try:
        # Savepoint rolls back every subscription/snapshot/term mutation if
        # the captured term cannot be created.  The outer transaction may then
        # safely commit only the operator-review state for an already-paid
        # provider callback; no access is granted without exact term evidence.
        async with db.begin_nested():
            if target is None:
                target = await create_paid_subscription(
                    db,
                    user_id=user.id,
                    duration_days=int(snapshot['period_days']),
                    traffic_limit_gb=int(snapshot['traffic_limit_gb']),
                    device_limit=int(snapshot['device_limit']),
                    connected_squads=list(entitlement.squad_uuids),
                    tariff_id=int(snapshot['tariff_id']),
                    commit=False,
                    _resolved_entitlement=entitlement,
                    _access_point_term_source_reference=f'device-first-checkout:{checkout.public_id}',
                    _access_point_term_provenance='device_first_checkout',
                )
            else:
                target = await extend_subscription(
                    db,
                    target,
                    int(snapshot['period_days']),
                    tariff_id=int(snapshot['tariff_id']),
                    traffic_limit_gb=int(snapshot['traffic_limit_gb']),
                    device_limit=int(snapshot['device_limit']),
                    connected_squads=list(entitlement.squad_uuids),
                    convert_trial=True,
                    commit=False,
                    _resolved_entitlement=entitlement,
                    _access_point_term_source_reference=f'device-first-checkout:{checkout.public_id}',
                    _access_point_term_provenance='device_first_checkout',
                )
    except (ValueError, EntitlementResolutionError, IntegrityError) as error:
        # A provider may already have accepted funds.  Preserve the exact
        # checkout for financial reconciliation; never retry against a newer
        # tariff policy or silently credit a different access term.
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'captured_entitlement_term_unavailable'
        raise DeviceFirstError(
            'operator_review_required', 'The captured access term requires operator reconciliation'
        ) from error

    if checkout.funding_mode == 'wallet':
        user.balance_kopeks -= total
    else:
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
    if not device_first_new_checkouts_enabled() or not is_device_first_canary_user(user):
        raise DeviceFirstError('feature_disabled', 'New checkouts are temporarily disabled')
    if checkout.financial_committed_at is not None:
        if checkout.funding_mode != 'wallet':
            raise DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
        return checkout
    entitlement = await _validate_direct_pre_commit(db, checkout=checkout, user=user, target=target, tariff=tariff)
    if entitlement is None:
        raise DeviceFirstError('reprice_required', 'The quote changed; create a new checkout')
    total = checkout.tariff_total_kopeks
    if user.balance_kopeks < total:
        raise DeviceFirstError('wallet_insufficient', 'The balance does not cover this checkout', status_code=422)
    checkout.wallet_applied_kopeks = total
    checkout.external_payable_kopeks = 0
    checkout.funding_mode = 'wallet'
    checkout.sale_snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='wallet', entitlement=entitlement)
    checkout.financial_committed_at = datetime.now(UTC)
    await _complete_direct_sale_locked(db, checkout=checkout, user=user, target=target)
    await db.commit()
    await _kick_direct_provisioning_post_commit(db, checkout_id=checkout.id)
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
    except DeviceFirstError as error:
        if error.code in {'legacy_trial_reconciliation_required', 'operator_review_required'}:
            # A pre-release direct invoice may be paid after a historical
            # pending trial has been discovered.  Do not let the recovery
            # worker spin or silently overwrite the old trial; hold this exact
            # paid attempt for one explicit financial reconciliation.
            owned_attempt.status = 'operator_review'
            owned_attempt.reconciliation_reason = (
                'legacy_pending_trial_reconciliation_required'
                if error.code == 'legacy_trial_reconciliation_required'
                else 'captured_entitlement_term_unavailable'
            )
        await db.commit()
        raise
    await db.commit()
    await _kick_direct_provisioning_post_commit(db, checkout_id=result.id)
    await db.refresh(result)
    return result


async def _kick_direct_provisioning_post_commit(db: AsyncSession, *, checkout_id: int) -> None:
    """Best-effort immediate v2 delivery after its sale is durable.

    This is deliberately post-commit: a failed RemnaWave request can never
    undo a confirmed receipt or subscription sale.  The narrow worker only
    claims this checkout's direct outbox row; its durable lease/backoff and the
    dedicated recovery loop take over if this first attempt cannot finish.
    """
    try:
        # A future access-point term may have been committed by this checkout.
        # Wake the dedicated boundary executor only after the financial/term
        # transaction is durable, so it can never miss the newly-created row.
        from app.services.monitoring_service import monitoring_service

        monitoring_service.wake_access_point_term_projection_scheduler()
    except Exception as error:
        logger.warning(
            'device_first_access_point_projection_wakeup_failed',
            checkout_id=checkout_id,
            error=type(error).__name__,
        )
    try:
        await process_direct_provisioning_outbox(db, checkout_id=checkout_id, limit=1)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # The financial commit above is already durable. Reset this session for
        # the webhook caller and leave the outbox pending/retry for recovery.
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning(
            'device_first_direct_provisioning_kick_failed',
            checkout_id=checkout_id,
            error=type(error).__name__,
        )


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
    processed = await process_direct_provisioning_outbox(db, limit=limit)
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
            # Выдача обязана дойти до панели даже когда ссылка и UUID уже есть: у пришедших
            # с триала они есть всегда, и без форса панель не получала ни срок, ни устройства.
            ok, error = await SubscriptionService().ensure_subscription_synced(
                db,
                subscription,
                force_panel_sync=True,
            )
            row.status = 'done' if ok else 'retry'
            row.last_error = error
            row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**row.attempts))
            checkout.provisioning_state = 'ready' if ok else 'retry'
            if ok:
                checkout.lifecycle_state = 'ready'
            elif error == 'no_entitlements_to_provision':
                # Повтор не поможет: выдавать нечего. Нужен оператор, а не круг повторов.
                # Статус строки — как в post-paid reversal (payment/platega.py): выборки
                # воркера берут только pending/retry/processing, значит очередь встала.
                row.status = 'operator_review'
                checkout.lifecycle_state = 'operator_review'
                checkout.terminal_reason = error
                _event('operator_review', checkout, reason=error)
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


async def process_direct_provisioning_outbox(
    db: AsyncSession,
    *,
    limit: int = 20,
    checkout_id: int | None = None,
) -> int:
    """Drain only fenced v2 provisioning; an optional id scopes an inline kick."""
    return await _process_direct_provisioning_outbox(db, limit=limit, checkout_id=checkout_id)


async def _access_point_checkout_projection_delivered(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    subscription: Subscription,
) -> bool | None:
    """Return whether an AP checkout's sole canonical Panel projection ran.

    ``None`` means a non-AP tariff.  AP direct-provisioning retries may only
    mark their checkout ready after the immutable-term outbox has performed
    the Panel write; they must never resurrect raw ``connected_squads``.
    """
    tariff_mode = await db.scalar(select(Tariff.entitlement_mode).where(Tariff.id == subscription.tariff_id))
    if tariff_mode != 'access_point_managed':
        return None
    term_id = await db.scalar(
        select(SubscriptionEntitlementTerm.id).where(
            SubscriptionEntitlementTerm.subscription_id == subscription.id,
            SubscriptionEntitlementTerm.source_reference == f'device-first-checkout:{checkout.public_id}',
        )
    )
    if term_id is None:
        return False
    state = await db.scalar(
        select(SubscriptionEntitlementTermProjectionOutbox.state).where(
            SubscriptionEntitlementTermProjectionOutbox.term_id == term_id
        )
    )
    return state == 'delivered'


async def _process_direct_provisioning_outbox(
    db: AsyncSession,
    *,
    limit: int,
    checkout_id: int | None = None,
) -> int:
    """Lease-fenced v2 delivery; a stale worker can never mark a newer lease done."""
    now = datetime.now(UTC)
    predicates = [DeviceFirstOutbox.settlement_mode == DIRECT_SETTLEMENT_MODE]
    if checkout_id is not None:
        predicates.append(DeviceFirstOutbox.checkout_id == checkout_id)
    predicates.append(
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
        )
    )
    rows = list(
        (
            await db.execute(
                select(DeviceFirstOutbox)
                .where(*predicates)
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
        sync_error: str | None = None
        try:
            if subscription is None:
                ok, sync_error = False, 'subscription_missing'
            else:
                access_point_delivered = await _access_point_checkout_projection_delivered(
                    db,
                    checkout=await db.get(SubscriptionCheckout, row.checkout_id),
                    subscription=subscription,
                )
                if access_point_delivered is None:
                    # Форс обязателен: без него подписка с уже существующими ссылкой и UUID
                    # объявляется готовой, не получив в панели ни нового срока, ни устройств.
                    ok, sync_error = await SubscriptionService().ensure_subscription_synced(
                        db,
                        subscription,
                        force_panel_sync=True,
                    )
                elif access_point_delivered:
                    ok, sync_error = True, None
                else:
                    ok, sync_error = False, 'access_point_term_projection_pending'
        except Exception as exc:  # no stale write; preserve concise retry evidence
            ok = False
            sync_error = type(exc).__name__
        # Keep the final critical section in the same lock order as the
        # post-paid reversal: Checkout -> Outbox.  The remote sync above is
        # deliberately outside a DB transaction; taking Outbox first here
        # would otherwise deadlock with reversal's Checkout -> Outbox fence.
        checkout = (
            await db.execute(
                select(SubscriptionCheckout).where(SubscriptionCheckout.id == row.checkout_id).with_for_update()
            )
        ).scalar_one()
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
            # A post-paid reversal may have won after the external sync.  It
            # holds Checkout then Outbox as well and moves the row to review;
            # never resurrect delivery from this stale lease.
            # Release the Checkout lock before the dedicated worker can move
            # on to its notification pass (which may perform Telegram I/O).
            await db.rollback()
            continue
        current.lease_token = None
        current.lease_expires_at = None
        current.last_error = sync_error
        if ok:
            current.status = 'done'
            checkout.provisioning_state = 'ready'
            checkout.lifecycle_state = 'ready'
            notification = (
                await db.execute(
                    select(DeviceFirstNotificationOutbox).where(
                        DeviceFirstNotificationOutbox.checkout_id == checkout.id,
                        DeviceFirstNotificationOutbox.notification_type == READY_NOTIFICATION_TYPE,
                    )
                )
            ).scalar_one_or_none()
            if notification is None:
                db.add(
                    DeviceFirstNotificationOutbox(
                        checkout_id=checkout.id,
                        notification_type=READY_NOTIFICATION_TYPE,
                    )
                )
            processed += 1
        elif sync_error == 'no_entitlements_to_provision':
            # Повтор не поможет: выдавать нечего. Нужен оператор, а не круг повторов.
            # Статус строки — как в post-paid reversal (payment/platega.py:138).
            current.status = 'operator_review'
            checkout.provisioning_state = 'retry'
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = sync_error
            _event('operator_review', checkout, reason=sync_error)
        else:
            current.status = 'retry'
            current.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2**current.attempts))
            checkout.provisioning_state = 'retry'
        await db.commit()
    return processed


def _owner_alerts_enabled() -> bool:
    """Владелец получает строки, только если админ-чат вообще настроен.

    Определение «включено» берём готовое (`settings.is_admin_notifications_enabled`),
    а не пишем своё: два расходящихся определения одного и того же — то, с чем
    проект и борется. Оно же проверяет, что chat_id разбирается в число.
    """
    return settings.is_admin_notifications_enabled()


def owner_alert_candidate_query(*, since: datetime, limit: int):
    """Заказы, о которых владелец ещё не знает. Оба замка живут здесь — и только здесь."""
    already_queued = exists(
        select(DeviceFirstNotificationOutbox.id).where(
            DeviceFirstNotificationOutbox.checkout_id == SubscriptionCheckout.id,
            DeviceFirstNotificationOutbox.notification_type == OWNER_ALERT_NOTIFICATION_TYPE,
        )
    )
    # Выдача, застрявшая в повторе, деньгами уже забрана, но заказ ещё не терминален:
    # для владельца это тот же случай «оплата не доехала».
    stuck_provisioning = exists(
        select(DeviceFirstOutbox.id).where(
            DeviceFirstOutbox.checkout_id == SubscriptionCheckout.id,
            DeviceFirstOutbox.status == 'retry',
            DeviceFirstOutbox.attempts >= OWNER_ALERT_STUCK_PROVISION_ATTEMPTS,
        )
    )
    return (
        select(SubscriptionCheckout.id)
        .join(User, User.id == SubscriptionCheckout.user_id)
        .where(
            # ЗАМОК 1 — архивная когорта требует удалённого пользователя.
            User.account_erased_at.is_(None),
            # И не рассказываем владельцу про человека, который уже попросил себя удалить:
            # его заказ загоняет в `operator_review` отдельный забор, а PII на этот момент
            # ещё не вычищено — карточка с именем и ссылкой ушла бы в админ-чат.
            User.account_erasure_requested_at.is_(None),
            # ЗАМОК 2 — окно свежести; архивные заказы старше его на две недели.
            SubscriptionCheckout.updated_at >= since,
            or_(SubscriptionCheckout.lifecycle_state == 'operator_review', stuck_provisioning),
            # ЗАМОК 3 — та же уникальность, что защищает от повторной отправки.
            ~already_queued,
        )
        .order_by(SubscriptionCheckout.id)
        .limit(limit)
    )


async def queue_owner_order_stuck_alerts(db: AsyncSession, *, limit: int = 20) -> int:
    """Поставить владельцу по одной строке на каждый свежезастрявший заказ.

    🔴 **Никакого backfill.** Строка аутбокса уведомлений по любому из пяти архивных
    заказов тарифа 3 навсегда запретила бы менять его серверы: когорта отбирается
    условием «у заказа НЕТ строки в этом аутбоксе» (``crud/tariff.py:106-110``), а её
    отпечаток сверяется точно (``:290-296``). Отсюда два замка. Они НЕ равнозначны:

    1. **Постоянный.** ``User.account_erased_at IS NULL`` — когорта требует **удалённого**
       пользователя (``crud/tariff.py:168-171``), а все пять заказов принадлежат удалённым
       170 и 173. Поле пишется ровно в одном месте (``account_erasure_service.py``) и
       нигде не обнуляется: «восстановления» удалённого аккаунта в коде нет. Этого замка
       достаточно самого по себе, и он не истекает.
    2. **Временный.** Окно ``OWNER_ALERT_LOOKBACK`` по ``updated_at``: те пять не менялись
       с 03.08.2026. 🔴 Но ``updated_at`` стоит с ``onupdate``, поэтому **любой** UPDATE по
       этим строкам втянет их в окно. Этап 4.4 (разбор архивных заказов) сделает ровно это —
       после него держит только замок 1. Не считать этот замок вечным.

    Уникальность ``(checkout_id, notification_type)`` — защита от дубля, а не от ловушки:
    у пяти архивных заказов строки нет, значит и не помешает.
    """
    if not _owner_alerts_enabled():
        # Без этой строки выключенные уведомления выглядели бы как «застрявших заказов нет».
        logger.warning('device_first_owner_alerts_disabled')
        return 0
    since = datetime.now(UTC) - OWNER_ALERT_LOOKBACK
    checkout_ids = list((await db.execute(owner_alert_candidate_query(since=since, limit=limit))).scalars().all())
    queued = 0
    for checkout_id in checkout_ids:
        db.add(
            DeviceFirstNotificationOutbox(
                checkout_id=checkout_id,
                notification_type=OWNER_ALERT_NOTIFICATION_TYPE,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # Второй воркер успел раньше: строка на этот заказ уже есть.
            await db.rollback()
            continue
        queued += 1
    return queued


async def revive_stale_notifications(db: AsyncSession) -> tuple[int, int]:
    """Пункт 4.8: строка ``failed`` больше не мертва навсегда — но только для владельца.

    Воркер берёт только ``pending``, а неудачная отправка ставила ``failed``: строка не
    повторялась никогда и нигде не была видна.

    🔴 **Повтор сознательно ограничен строками владельцу.** Для клиентского «✅ Подписка
    готова» ``failed`` означает **неизвестный** исход: Telegram мог сообщение принять, а
    ответ потеряться. Прежний автор выбрал здесь «не более одного раза» и написал это в
    комментарии; повторять такую отправку — молча отменить чужое решение и слать клиенту
    дубли. Клиентская строка вместо этого перестаёт быть невидимой: через срок годности
    она уходит в ``dead`` и попадает в счётчик и в лог с номерами заказов.

    ``sending`` не трогаем ни для кого: это та же защита «не отправить дважды».
    ``NOTIFICATION_MAX_AGE`` — именно срок годности сообщения, а не число попыток:
    сообщать владельцу о заказе шестичасовой давности уже поздно.
    """
    now = datetime.now(UTC)
    # `sending` попадает в этот свип, но НЕ в оживление: строка, брошенная крахом между
    # «взял» и «отправил», иначе оставалась бы невидимой вечно. Возраст 6 часов на порядки
    # больше десятиминутной аренды, так что живую отправку свип не задевает.
    hopeless = and_(
        DeviceFirstNotificationOutbox.status.in_(('failed', 'sending')),
        DeviceFirstNotificationOutbox.created_at <= now - NOTIFICATION_MAX_AGE,
    )
    doomed = list((await db.execute(select(DeviceFirstNotificationOutbox.checkout_id).where(hopeless))).scalars().all())
    dead = await db.execute(
        update(DeviceFirstNotificationOutbox)
        .where(hopeless)
        .values(status='dead', lease_token=None, lease_expires_at=None)
    )
    revived = await db.execute(
        update(DeviceFirstNotificationOutbox)
        .where(
            DeviceFirstNotificationOutbox.notification_type == OWNER_ALERT_NOTIFICATION_TYPE,
            or_(
                and_(
                    DeviceFirstNotificationOutbox.status == 'failed',
                    DeviceFirstNotificationOutbox.updated_at <= now - NOTIFICATION_RETRY_AFTER,
                ),
                # Перезапуск бота между «взял строку» и «отправил» оставлял её в `sending`
                # НАВСЕГДА: воркер берёт только `pending`, а новую строку по тому же заказу
                # запрещает уникальность. Для владельца это отменяло весь смысл 4.3, поэтому
                # просроченную аренду возвращаем в работу. Клиентские строки — по-прежнему нет.
                and_(
                    DeviceFirstNotificationOutbox.status == 'sending',
                    DeviceFirstNotificationOutbox.lease_expires_at.is_not(None),
                    DeviceFirstNotificationOutbox.lease_expires_at <= now,
                ),
            ),
        )
        .values(status='pending', lease_token=None, lease_expires_at=None)
    )
    dead_count = max(0, int(dead.rowcount or 0))
    revived_count = max(0, int(revived.rowcount or 0))
    # Коммитим всегда: сессия общая с циклом мониторинга, и `rollback` на тихом проходе
    # откатил бы чужую незакоммиченную работу. Два UPDATE транзакцию уже открыли.
    await db.commit()
    if dead_count:
        logger.error('device_first_notification_gave_up', rows=dead_count, checkout_ids=doomed)
    return revived_count, dead_count


_TERMINAL_REASON_RU = {
    'provider_invoice_missing_or_elapsed_expiry': 'счёт у платёжной системы просрочен или не найден',
    'direct_payment_binding_mismatch': 'платёж не сошёлся с заказом',
    'no_entitlements_to_provision': 'выдавать нечего: у тарифа не осталось серверов',
    'captured_entitlement_term_unavailable': 'условия тарифа изменились после оплаты',
    'legacy_pending_trial_reconciliation_required': 'мешает незакрытая старая пробная оплата',
    'panel_update_failed': 'панель VPN не приняла изменения',
    'panel_unavailable': 'панель VPN не отвечает',
    'panel_squads_not_applied': 'панель VPN не применила серверы',
}
_PROVISIONING_RU = {
    'not_started': 'не начиналась',
    'pending': 'в очереди',
    'retry': 'застряла в повторах',
    'ready': 'выдана',
}
_POST_PAID_REVERSAL_PREFIX = 'post_paid_provider_terminal'


async def _money_verdict(db: AsyncSession, checkout: SubscriptionCheckout) -> str:
    """Что владельцу делать с деньгами. Цена ошибки здесь — двойной возврат либо отказ в нём."""
    if str(checkout.terminal_reason or '').startswith(_POST_PAID_REVERSAL_PREFIX):
        return '⚠️ Деньги: платёж отменён или отозван на стороне платёжной системы. Возврат делать НЕ нужно.'
    # 🔴 `debit_transaction_id` заполняется при ОБОИХ способах оплаты
    # (`_commit_direct_sale`), поэтому источник денег определяет `funding_mode`, а не он.
    if checkout.funding_mode == 'wallet':
        if checkout.debit_transaction_id is not None:
            return '🔴 Деньги: списаны с баланса клиента.'
        return '🟢 Деньги: с баланса не списаны, остались у клиента.'
    # 🔴 Попыток у заказа может быть несколько, и `credited_amount_kopeks > 0` ставится
    # при ТРЁХ разных статусах: `credited` (деньги уже на балансе клиента),
    # `operator_review` (удержаны как кредит сверки, на баланс НЕ попали), `paid_processing`.
    # Выбирать «какую-нибудь» попытку нельзя: вердикт стал бы невоспроизводимым, а это
    # ровно те две ошибки, от которых функция и защищает. Поэтому спрашиваем фактами.
    money_on_balance = await db.scalar(
        select(func.count(CheckoutPaymentAttempt.id)).where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
            CheckoutPaymentAttempt.status == 'credited',
        )
    )
    if money_on_balance:
        return '🟡 Деньги: оплата прошла и уже лежит на балансе клиента. Возврат не нужен.'
    money_at_provider = await db.scalar(
        select(func.count(CheckoutPaymentAttempt.id)).where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
        )
    )
    if checkout.debit_transaction_id is not None or money_at_provider:
        return '🔴 Деньги: клиент оплатил через платёжную систему. С баланса не списывали.'
    if str(checkout.terminal_reason or '') == 'provider_invoice_missing_or_elapsed_expiry':
        # Тут код ЗНАЕТ ответ: счёт истёк неоплаченным. Отправлять владельца искать
        # несуществующий платёж — сочинять ему работу и сомнение на пустом месте.
        return '🟢 Деньги: клиент не оплатил, счёт истёк. Списания не было, возврат не нужен.'
    return '⚠️ Деньги: в базе списания не видно. Проверьте платёж в личном кабинете Platega.'


async def _owner_order_stuck_text(db: AsyncSession, checkout: SubscriptionCheckout) -> str:
    """Одно сообщение владельцу: кто, за что, сколько, деньги и что делать прямо сейчас."""
    from app.utils.timezone import format_local_datetime

    user = await db.get(User, checkout.user_id)
    snapshot = checkout.sale_snapshot if isinstance(checkout.sale_snapshot, dict) else {}
    tariff_name = html.escape(str(snapshot.get('tariff_name') or f'тариф {checkout.tariff_id}'))
    reason_code = str(checkout.terminal_reason or '')
    reason_ru = _TERMINAL_REASON_RU.get(reason_code)
    provisioning_ru = _PROVISIONING_RU.get(str(checkout.provisioning_state), str(checkout.provisioning_state))
    paid_by = 'с баланса' if checkout.funding_mode == 'wallet' else 'через платёжную систему'

    lines = ['🔴 <b>ЗАКАЗ ЗАВИС</b>', '']
    if user is not None:
        name = html.escape(user.full_name or f'клиент {checkout.user_id}')
        # `telegram_id` намеренно nullable (email-only пользователи). Подставить сюда None —
        # значит получить отказ Telegram на всё сообщение и потерять тревогу целиком.
        if user.telegram_id:
            lines.append(f'👤 <a href="tg://user?id={user.telegram_id}">{name}</a> (<code>{user.telegram_id}</code>)')
        else:
            lines.append(f'👤 {name} (id {checkout.user_id}, телеграма нет)')
        if user.username:
            lines.append(f'📱 @{html.escape(user.username)}')
    else:
        lines.append(f'👤 клиент id {checkout.user_id} (карточка недоступна)')
    lines += [
        '',
        f'💵 <b>{settings.format_price(checkout.tariff_total_kopeks or 0)}</b> • оплата {paid_by}',
        f'🏷️ Тариф: <b>{tariff_name}</b>',
        f'📅 {checkout.period_days} дн. • 📱 {checkout.selected_device_limit} устр.',
        f'⏰ Завис {format_local_datetime(checkout.updated_at, "%d.%m.%Y в %H:%M")}',
        '',
        await _money_verdict(db, checkout),
    ]
    if checkout.lifecycle_state == 'operator_review':
        # Формулировка «Оплату нужно проверить» взята слово в слово из кабинета и из
        # клиентского экрана бота: одно состояние обязано звучать одинаково везде.
        lines.append(f'⚠️ Оплату нужно проверить: {reason_ru or "причину видно только по коду"}.')
    else:
        # Настоящая ошибка панели лежит не в заказе, а в строке очереди выдачи. Без неё
        # владелец видел «выдача застряла» вообще без причины.
        stuck_error = await db.scalar(
            select(DeviceFirstOutbox.last_error)
            .where(DeviceFirstOutbox.checkout_id == checkout.id, DeviceFirstOutbox.status == 'retry')
            .order_by(DeviceFirstOutbox.id.desc())
            .limit(1)
        )
        cause = _TERMINAL_REASON_RU.get(str(stuck_error or ''), str(stuck_error or '')) if stuck_error else ''
        lines.append(
            f'⚠️ Выдача {provisioning_ru}{f": {html.escape(cause)}" if cause else ""}. Бот продолжает пробовать сам.'
        )
    if str(checkout.provisioning_state) != 'ready':
        # При chargeback подписка уже выдана и работает — безусловная строка была бы ложью.
        lines.append('📵 VPN клиенту не выдан.')
    # Блокировка нового заказа шире, чем `operator_review`: заказ, застрявший на выдаче,
    # держит клиента через `direct_provisioning_recovery` ровно так же.
    blocked = checkout.lifecycle_state == 'operator_review' or str(checkout.provisioning_state) in {'pending', 'retry'}
    if blocked and str(getattr(checkout, 'settlement_mode', '')) == DIRECT_SETTLEMENT_MODE:
        lines.append('🚫 Новый заказ он оформить не может, пока висит этот.')
    lines += [
        '',
        'Кнопки для разбора в боте пока нет. Напишите клиенту сами. Возврат делается '
        'в Platega, подписку выдаём руками <b>в кабинете</b>: пользователь → «Создать подписку».',
        '⛔ В чат-админке этого не делать: выдача там заглушена, а кнопка «Обнулить подписку» '
        'оставляет подписку без серверов и намертво запирает смену серверов тарифа.',
        '',
        f'Заказ: <code>{html.escape(str(checkout.public_id))}</code>',
        f'Состояние: <code>{html.escape(str(checkout.lifecycle_state))}</code> / '
        f'выдача <code>{html.escape(str(checkout.provisioning_state))}</code>',
    ]
    if reason_code:
        lines.append(f'Код причины: <code>{html.escape(reason_code)}</code>')
    return '\n'.join(lines)


# 🔴 НЕ заменять на `TERMINAL_STATES` (`:44`) и не сводить с набором из `crud/tariff.py:32`
# при исполнении «мины C». Здесь смысл другой: это «заказ больше не требует владельца».
# `operator_review` терминален для заказа, но для тревоги — наоборот, единственный повод её
# послать. Добавите его сюда — тревоги перестанут уходить совсем, молча.
_OWNER_ALERT_RESOLVED_STATES = frozenset({'ready', 'cancelled', 'expired'})


def _owner_alert_is_obsolete(checkout: SubscriptionCheckout) -> bool:
    """Заказ успел доехать сам, пока строка ждала отправки — тревогу слать уже нельзя."""
    return checkout.lifecycle_state in _OWNER_ALERT_RESOLVED_STATES


async def _send_owner_order_stuck_alert(db: AsyncSession, *, bot, checkout: SubscriptionCheckout) -> bool:
    """Вернуть False, если тревога устарела и отправлять её не нужно."""
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    if _owner_alert_is_obsolete(checkout):
        _event('owner_alert_obsolete', checkout, lifecycle_state=checkout.lifecycle_state)
        return False
    service = AdminNotificationService(bot)
    if not service.is_enabled:
        raise RuntimeError('admin_notifications_disabled')
    text = await _owner_order_stuck_text(db, checkout)
    if not await service.send_admin_notification(text, category=NotificationCategory.ERRORS):
        raise RuntimeError('admin_notification_not_delivered')
    return True


async def _send_client_ready_message(db: AsyncSession, *, bot, checkout: SubscriptionCheckout) -> None:
    user = await db.get(User, checkout.user_id)
    if user is None or not user.telegram_id:
        raise RuntimeError('telegram_recipient_unavailable')
    text = (
        '✅ Your VPN subscription is ready. Open the cabinet to connect.'
        if user.language == 'en'
        else '✅ Ваша VPN-подписка готова. Откройте кабинет, чтобы подключиться.'
    )
    await bot.send_message(user.telegram_id, text)


async def process_device_first_notification_outbox(db: AsyncSession, *, bot, limit: int = 20) -> int:
    """Клиенту — не более одного раза: крах после передачи в Telegram оставляет ``sending``.

    Строкам владельцу (``order_stuck``) это правило не подходит: там потеря дороже дубля,
    поэтому их повторяет и переоткрывает ``revive_stale_notifications``.
    """
    if bot is None:
        return 0
    # Постановка тревог и оживление строк не должны отменять доставку клиентам:
    # раньше любое исключение здесь обрывало весь проход воркера.
    try:
        await queue_owner_order_stuck_alerts(db, limit=limit)
        await revive_stale_notifications(db)
    except Exception as error:
        # Здесь rollback обязателен, в отличие от тихого прохода: после исключения сессия
        # отравлена, и без него следующий же запрос упал бы с PendingRollbackError.
        await db.rollback()
        logger.error('device_first_owner_alert_pass_failed', error=type(error).__name__)
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
        # 🔴 Не `db.get`: сессия общая с циклом мониторинга, `expire_on_commit=False`, и заказ
        # почти наверняка уже лежит в её памяти с полями получасовой давности. Тревога тогда
        # решала бы «доехал или нет» по протухшей копии — и владелец вернул бы деньги за
        # выданную подписку. Перечитываем из базы принудительно, как это делает erasure-сервис.
        checkout = (
            await db.execute(
                select(SubscriptionCheckout)
                .where(SubscriptionCheckout.id == row.checkout_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        error: str | None = None
        obsolete = False
        try:
            if checkout is None:
                raise RuntimeError('checkout_unavailable')
            # 🔴 Развилка по типу обязательна: без неё строка для владельца уходит
            # клиентским текстом «✅ Подписка готова» — про сорванный заказ.
            if row.notification_type == OWNER_ALERT_NOTIFICATION_TYPE:
                obsolete = not await _send_owner_order_stuck_alert(db, bot=bot, checkout=checkout)
            else:
                await _send_client_ready_message(db, bot=bot, checkout=checkout)
        except Exception as exc:  # do not retry an uncertain external send
            # Причину пишем текстом: иначе `last_error` у всех отказов был бы 'RuntimeError'
            # и «категория выключена» не отличалось бы от «Telegram не принял».
            error = f'{type(exc).__name__}: {exc}'[:200]
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
        if obsolete:
            # Заказ доехал сам, пока строка ждала очереди: тревога устарела, слать нечего.
            current.status = 'obsolete'
        elif error is None:
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

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import (
    create_trial_subscription,
    decrement_subscription_server_counts,
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import add_user_balance, subtract_user_balance
from app.database.models import (
    CheckoutPaymentAttempt,
    PaymentMethod,
    PlategaPayment,
    Subscription,
    SubscriptionCheckout,
    SubscriptionStatus,
    Tariff,
    TransactionType,
    User,
)
from app.services.device_first_checkout_service import DIRECT_SETTLEMENT_MODE


logger = structlog.get_logger(__name__)


class TrialPaymentError(Exception):
    """Base exception for trial activation payment issues."""


@dataclass(slots=True)
class TrialPaymentInsufficientFunds(TrialPaymentError):
    required_amount: int
    balance_amount: int

    @property
    def missing_amount(self) -> int:
        return max(0, self.required_amount - self.balance_amount)


class TrialPaymentChargeFailed(TrialPaymentError):
    """Raised when balance charge could not be completed."""


class TrialCheckoutResolutionError(TrialPaymentError):
    """A typed, non-financial rejection from the trial/checkout state gate."""

    def __init__(self, code: str, message: str, *, status_code: int = 409, context: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.context = context or {}


@dataclass(slots=True)
class TrialCheckoutSummary:
    public_id: str
    tariff_name: str
    period_days: int
    device_limit: int
    amount_kopeks: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            'id': self.public_id,
            'tariff_name': self.tariff_name,
            'period_days': self.period_days,
            'device_limit': self.device_limit,
            'amount_kopeks': self.amount_kopeks,
        }


@dataclass(slots=True)
class TrialCheckoutContext:
    """Server-authoritative preflight for the dedicated trial journey."""

    state: Literal['ready', 'discardable_quote', 'pending_invoice', 'reconciliation_required']
    checkout: TrialCheckoutSummary | None = None


@dataclass(slots=True)
class TrialActivationResult:
    subscription: Subscription
    charged_amount_kopeks: int
    abandoned_checkout: TrialCheckoutSummary | None
    charge_transaction_id: int | None = None
    already_active: bool = False


@dataclass(slots=True)
class _LockedDirectContext:
    user: User
    checkouts: list[SubscriptionCheckout]
    attempts_by_checkout_id: dict[int, list[CheckoutPaymentAttempt]]
    payments_by_id: dict[int, PlategaPayment]


@dataclass(slots=True)
class TrialActivationReversionResult:
    refunded: bool = True
    subscription_rolled_back: bool = True


def get_trial_activation_charge_amount() -> int:
    """Returns the configured activation charge in kopeks if payment is enabled."""

    if not settings.is_trial_paid_activation_enabled():
        return 0

    try:
        price_kopeks = int(settings.get_trial_activation_price() or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        price_kopeks = 0

    return max(0, price_kopeks)


def preview_trial_activation_charge(user: User) -> int:
    """Validates that the user can afford the trial activation charge."""

    price_kopeks = get_trial_activation_charge_amount()
    if price_kopeks <= 0:
        return 0

    balance = int(getattr(user, 'balance_kopeks', 0) or 0)
    if balance < price_kopeks:
        raise TrialPaymentInsufficientFunds(price_kopeks, balance)

    return price_kopeks


async def charge_trial_activation_if_required(
    db: AsyncSession,
    user: User,
    *,
    description: str | None = None,
    commit: bool = True,
) -> int:
    """Charges the user's balance if paid trial activation is enabled.

    Returns the charged amount in kopeks. If payment is not required or the
    configured price is zero, the function returns ``0``.
    """

    price_kopeks = preview_trial_activation_charge(user)
    if price_kopeks <= 0:
        return 0

    charge_description = description or 'Активация триальной подписки'

    success = await subtract_user_balance(
        db,
        user,
        price_kopeks,
        charge_description,
        mark_as_paid_subscription=True,
        commit=commit,
    )
    if not success:
        raise TrialPaymentChargeFailed

    # Создаём транзакцию для учёта списания за триал
    await create_transaction(
        db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=price_kopeks,
        description=charge_description,
        payment_method=PaymentMethod.BALANCE,
        commit=commit,
    )

    return int(price_kopeks)


async def refund_trial_activation_charge(
    db: AsyncSession,
    user: User,
    amount_kopeks: int,
    *,
    description: str | None = None,
) -> bool:
    """Refunds a previously charged trial activation amount back to the user."""

    if amount_kopeks <= 0:
        return True

    refund_description = description or 'Возврат оплаты за активацию триальной подписки'

    success = await add_user_balance(
        db,
        user,
        amount_kopeks,
        refund_description,
        transaction_type=TransactionType.REFUND,
    )

    if not success:
        logger.error(
            'Failed to refund kopeks for user during trial activation rollback',
            amount_kopeks=amount_kopeks,
            getattr=getattr(user, 'id', '<unknown>'),
        )

    return success


async def rollback_trial_subscription_activation(
    db: AsyncSession,
    subscription: Subscription | None,
) -> bool:
    """Attempts to undo a previously created trial subscription.

    Returns ``True`` when the rollback succeeds or when ``subscription`` is
    falsy. In case of a database failure the function returns ``False`` after
    logging the error so callers can decide how to proceed.
    """

    if not subscription:
        return True

    try:
        await decrement_subscription_server_counts(db, subscription)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Failed to decrement server counters during trial rollback', user_id=subscription.user_id, error=error
        )

    try:
        await db.delete(subscription)
        await db.commit()
    except Exception as error:  # pragma: no cover - defensive logging
        logger.error(
            'Failed to remove trial subscription after charge failure',
            getattr=getattr(subscription, 'id', '<unknown>'),
            error=error,
        )
        await db.rollback()
        return False

    return True


async def revert_trial_activation(
    db: AsyncSession,
    user: User,
    subscription: Subscription | None,
    charged_amount: int,
    *,
    refund_description: str | None = None,
) -> TrialActivationReversionResult:
    """Rolls back a trial subscription and refunds any charged amount."""

    rollback_success = await rollback_trial_subscription_activation(db, subscription)
    refund_success = await refund_trial_activation_charge(
        db,
        user,
        charged_amount,
        description=refund_description,
    )

    try:
        await db.refresh(user)
    except Exception as error:  # pragma: no cover - defensive logging
        logger.warning(
            'Failed to refresh user after reverting trial activation',
            getattr=getattr(user, 'id', '<unknown>'),
            error=error,
        )

    return TrialActivationReversionResult(
        refunded=refund_success,
        subscription_rolled_back=rollback_success,
    )


def _summary(checkout: SubscriptionCheckout, tariff: Tariff | None) -> TrialCheckoutSummary:
    """Return only person-facing, server-owned order facts to the Cabinet."""

    return TrialCheckoutSummary(
        public_id=str(checkout.public_id),
        tariff_name=str(getattr(tariff, 'name', None) or 'VPN'),
        period_days=int(checkout.period_days),
        device_limit=int(checkout.selected_device_limit),
        amount_kopeks=int(checkout.tariff_total_kopeks or checkout.quoted_price_kopeks or 0),
    )


def _is_locally_abandoned(checkout: SubscriptionCheckout) -> bool:
    return checkout.lifecycle_state == 'cancelled' and checkout.terminal_reason == 'cancelled_by_user_after_invoice'


def _is_discardable_direct_quote(checkout: SubscriptionCheckout, attempts: list[CheckoutPaymentAttempt]) -> bool:
    return (
        not attempts
        and checkout.lifecycle_state in {'draft', 'confirmed', 'awaiting_funds'}
        and checkout.fulfillment_state == 'not_started'
        and checkout.financial_committed_at is None
    )


def _is_exact_pending_direct_invoice(
    checkout: SubscriptionCheckout,
    attempts: list[CheckoutPaymentAttempt],
    payments_by_id: dict[int, PlategaPayment],
) -> CheckoutPaymentAttempt | None:
    """Accept only the one state where an explicit local abandonment is safe.

    The provider is deliberately not cancelled here.  This is solely the local
    entitlement fence that preserves an exact late callback as a one-time
    wallet credit instead of granting the abandoned plan.
    """

    if len(attempts) != 1:
        return None
    attempt = attempts[0]
    payment = payments_by_id.get(attempt.platega_payment_id)
    if payment is None:
        return None
    if (
        attempt.settlement_mode != DIRECT_SETTLEMENT_MODE
        or not attempt.platega_payment_id
        or not attempt.provider_payment_id
        or attempt.status != 'pending'
        or payment.is_paid
        or str(payment.status or '').upper() not in {'PENDING', 'INPROGRESS'}
        or checkout.lifecycle_state != 'awaiting_funds'
        or checkout.funding_state != 'invoice_pending'
        or checkout.fulfillment_state != 'not_started'
    ):
        return None
    return attempt


async def _tariff_for_checkout(db: AsyncSession, checkout: SubscriptionCheckout) -> Tariff | None:
    return await db.get(Tariff, checkout.tariff_id)


async def _read_direct_trial_context(db: AsyncSession, *, user_id: int) -> TrialCheckoutContext:
    """Read-only context for `/trial`; command-time checks always repeat under locks."""

    checkouts = list(
        (
            await db.execute(
                select(SubscriptionCheckout)
                .where(
                    SubscriptionCheckout.user_id == user_id,
                    SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                )
                .order_by(SubscriptionCheckout.id.asc())
            )
        ).scalars()
    )
    if not checkouts:
        return TrialCheckoutContext('ready')
    checkout_ids = [checkout.id for checkout in checkouts]
    attempts = list(
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(CheckoutPaymentAttempt.checkout_id.in_(checkout_ids))
                .order_by(CheckoutPaymentAttempt.id.asc())
            )
        ).scalars()
    )
    payment_ids = [attempt.platega_payment_id for attempt in attempts if attempt.platega_payment_id]
    payments = {}
    if payment_ids:
        payments = {
            payment.id: payment
            for payment in (
                await db.execute(select(PlategaPayment).where(PlategaPayment.id.in_(payment_ids)))
            ).scalars()
        }
    by_checkout: dict[int, list[CheckoutPaymentAttempt]] = {}
    for attempt in attempts:
        by_checkout.setdefault(attempt.checkout_id, []).append(attempt)

    discardable: SubscriptionCheckout | None = None
    for checkout in checkouts:
        checkout_attempts = by_checkout.get(checkout.id, [])
        if _is_locally_abandoned(checkout):
            continue
        if _is_discardable_direct_quote(checkout, checkout_attempts):
            discardable = checkout
            continue
        if _is_exact_pending_direct_invoice(checkout, checkout_attempts, payments) is not None:
            return TrialCheckoutContext('pending_invoice', _summary(checkout, await _tariff_for_checkout(db, checkout)))
        # Terminal provider history is not a new purchase blocker.  All other
        # states can still represent money or a provider request in flight.
        if checkout.lifecycle_state in {'cancelled', 'expired', 'ready'}:
            continue
        return TrialCheckoutContext(
            'reconciliation_required',
            _summary(checkout, await _tariff_for_checkout(db, checkout)),
        )
    if discardable is not None:
        return TrialCheckoutContext(
            'discardable_quote', _summary(discardable, await _tariff_for_checkout(db, discardable))
        )
    return TrialCheckoutContext('ready')


async def get_trial_checkout_context(db: AsyncSession, *, user_id: int) -> TrialCheckoutContext:
    """Public read model.  Never use it to authorize mutation without rechecking."""

    return await _read_direct_trial_context(db, user_id=user_id)


async def _lock_direct_context_for_trial(
    db: AsyncSession,
    *,
    user_id: int,
) -> _LockedDirectContext:
    """Lock trial-vs-payment state in Payment → User → Attempt → Checkout order.

    A read before the user lock finds Payment rows first.  It is repeated after
    the user lock; if a direct prepare committed between these two moments we
    roll back and retry rather than acquiring Payment after User.
    """

    for _ in range(3):
        payment_ids = list(
            (
                await db.execute(
                    select(PlategaPayment.id)
                    .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
                    .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                    .where(
                        SubscriptionCheckout.user_id == user_id,
                        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    )
                    .order_by(PlategaPayment.id.asc())
                )
            ).scalars()
        )
        payments: list[PlategaPayment] = []
        for payment_id in payment_ids:
            payment = (
                await db.execute(
                    select(PlategaPayment)
                    .where(PlategaPayment.id == payment_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if payment is not None:
                payments.append(payment)

        user = (
            await db.execute(
                select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
            )
        ).scalar_one()

        rechecked_payment_ids = list(
            (
                await db.execute(
                    select(PlategaPayment.id)
                    .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
                    .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                    .where(
                        SubscriptionCheckout.user_id == user_id,
                        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    )
                    .order_by(PlategaPayment.id.asc())
                )
            ).scalars()
        )
        if rechecked_payment_ids != payment_ids:
            await db.rollback()
            continue

        # Keep the documented global order (Payment → User → Attempt →
        # Checkout).  The attempts query joins through the checkout solely to
        # identify the owner's rows; PostgreSQL locks the selected attempt
        # rows, not the joined checkout row.  This avoids an ABBA deadlock
        # with the direct-checkout path, which locks its attempt before it
        # finalises the checkout state.
        attempts = list(
            (
                await db.execute(
                    select(CheckoutPaymentAttempt)
                    .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                    .where(
                        SubscriptionCheckout.user_id == user_id,
                        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    )
                    .order_by(CheckoutPaymentAttempt.id.asc())
                    .with_for_update(of=CheckoutPaymentAttempt)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        checkouts = list(
            (
                await db.execute(
                    select(SubscriptionCheckout)
                    .where(
                        SubscriptionCheckout.user_id == user_id,
                        SubscriptionCheckout.settlement_mode == DIRECT_SETTLEMENT_MODE,
                    )
                    .order_by(SubscriptionCheckout.id.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        by_checkout: dict[int, list[CheckoutPaymentAttempt]] = {}
        for attempt in attempts:
            by_checkout.setdefault(attempt.checkout_id, []).append(attempt)
        return _LockedDirectContext(
            user=user,
            checkouts=checkouts,
            attempts_by_checkout_id=by_checkout,
            payments_by_id={payment.id: payment for payment in payments},
        )
    raise TrialCheckoutResolutionError(
        'trial_state_changed_retry',
        'The previous order changed while it was being checked. Please retry.',
        status_code=409,
    )


async def _trial_parameters(db: AsyncSession, *, forced_device_limit: int | None = None) -> dict:
    duration_days = settings.TRIAL_DURATION_DAYS
    traffic_limit_gb = settings.TRIAL_TRAFFIC_LIMIT_GB
    device_limit = forced_device_limit if forced_device_limit is not None else settings.TRIAL_DEVICE_LIMIT
    connected_squads: list[str] = []
    tariff_id: int | None = None
    from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff
    from app.services.public_location_entitlement_service import resolve_tariff_entitlement

    tariff = await get_trial_tariff(db)
    if tariff is None and settings.get_trial_tariff_id() > 0:
        tariff = await get_tariff_by_id(db, settings.get_trial_tariff_id())
    if tariff is None:
        raise TrialCheckoutResolutionError(
            'trial_tariff_not_configured',
            'Trial cannot be issued until its location policy is configured.',
            status_code=409,
        )
    duration_days = getattr(tariff, 'trial_duration_days', None) or duration_days
    traffic_limit_gb = tariff.traffic_limit_gb
    device_limit = tariff.device_limit
    entitlement = await resolve_tariff_entitlement(db, tariff)
    connected_squads = list(entitlement.squad_uuids)
    tariff_id = tariff.id
    return {
        'duration_days': duration_days,
        'traffic_limit_gb': traffic_limit_gb,
        'device_limit': device_limit,
        'connected_squads': connected_squads,
        'tariff_id': tariff_id,
    }


async def get_trial_offer_parameters(
    db: AsyncSession,
    *,
    forced_device_limit: int | None = None,
) -> dict:
    """Read the same server-owned offer used by activation and `/trial` UI."""

    return await _trial_parameters(db, forced_device_limit=forced_device_limit)


async def activate_trial_with_checkout_resolution(
    db: AsyncSession,
    *,
    user_id: int,
    resolution: Literal['activate', 'abandon_pending_invoice'] = 'activate',
    expected_checkout_public_id: str | None = None,
    forced_device_limit: int | None = None,
    charge_description: str = 'Активация триальной подписки',
) -> TrialActivationResult:
    """Activate a trial without racing a Device-First external invoice.

    This is the sole mutation coordinator for Cabinet and Telegram.  It never
    calls Platega, RemnaWave or Telegram while the transaction is open.
    """

    locked = await _lock_direct_context_for_trial(db, user_id=user_id)
    user = locked.user
    live_subscriptions = list(
        (
            await db.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(
                        [
                            SubscriptionStatus.ACTIVE.value,
                            SubscriptionStatus.TRIAL.value,
                            SubscriptionStatus.LIMITED.value,
                        ]
                    ),
                )
                .order_by(Subscription.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    existing_trial = next((subscription for subscription in live_subscriptions if subscription.is_trial), None)
    if existing_trial is not None:
        # The coordinator acquired Payment/User/Subscription row locks above.
        # Release them before callers run any post-commit RemnaWave recovery;
        # an idempotent replay must never hold database locks across network IO.
        await db.commit()
        return TrialActivationResult(existing_trial, 0, None, already_active=True)
    if live_subscriptions:
        raise TrialCheckoutResolutionError(
            'active_subscription', 'An active subscription already exists', status_code=409
        )
    if getattr(user, 'restriction_subscription', False):
        raise TrialCheckoutResolutionError(
            'subscription_restricted', 'Subscription activation is restricted', status_code=403
        )
    if settings.TRIAL_DURATION_DAYS <= 0 or settings.is_trial_disabled_for_user(getattr(user, 'auth_type', 'telegram')):
        raise TrialCheckoutResolutionError(
            'trial_unavailable', 'Trial is not available for this account', status_code=400
        )
    # Re-read every historical subscription after the user lock.  A pending
    # trial is the only intentional retry exception in the product rule.
    historical_subscriptions = list(
        (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if any(subscription.status != SubscriptionStatus.PENDING.value for subscription in historical_subscriptions):
        raise TrialCheckoutResolutionError('trial_already_used', 'Trial was already used', status_code=409)

    discardable: list[SubscriptionCheckout] = []
    pending_checkout: SubscriptionCheckout | None = None
    pending_attempt: CheckoutPaymentAttempt | None = None
    for checkout in locked.checkouts:
        attempts = locked.attempts_by_checkout_id.get(checkout.id, [])
        if _is_locally_abandoned(checkout):
            continue
        if _is_discardable_direct_quote(checkout, attempts):
            discardable.append(checkout)
            continue
        exact_pending = _is_exact_pending_direct_invoice(checkout, attempts, locked.payments_by_id)
        if exact_pending is not None:
            if pending_checkout is not None:
                raise TrialCheckoutResolutionError(
                    'trial_blocked_by_reconciliation',
                    'More than one previous invoice requires reconciliation',
                )
            pending_checkout, pending_attempt = checkout, exact_pending
            continue
        if checkout.lifecycle_state in {'cancelled', 'expired', 'ready'}:
            continue
        raise TrialCheckoutResolutionError(
            'trial_blocked_by_reconciliation',
            'The previous payment must be checked before a trial can start',
            context={'checkout': _summary(checkout, await _tariff_for_checkout(db, checkout)).as_dict()},
        )

    abandoned_summary: TrialCheckoutSummary | None = None
    if pending_checkout is not None:
        pending_summary = _summary(pending_checkout, await _tariff_for_checkout(db, pending_checkout))
        if resolution != 'abandon_pending_invoice' or expected_checkout_public_id != str(pending_checkout.public_id):
            raise TrialCheckoutResolutionError(
                'pending_checkout_requires_resolution',
                'A pending invoice requires an explicit choice',
                context={'checkout': pending_summary.as_dict()},
            )
        payment = locked.payments_by_id.get(pending_attempt.platega_payment_id) if pending_attempt else None
        if payment is None:  # defensive: the classifier already required it
            raise TrialCheckoutResolutionError('trial_blocked_by_reconciliation', 'Payment binding is incomplete')
        pending_checkout.lifecycle_state = 'cancelled'
        pending_checkout.quote_state = 'expired'
        pending_checkout.funding_state = 'invoice_abandoned'
        pending_checkout.terminal_reason = 'cancelled_by_user_after_invoice'
        pending_checkout.updated_at = datetime.now(UTC)
        pending_attempt.status = 'reconciliation'
        pending_attempt.reconciliation_reason = 'provider_invoice_abandoned_by_user'
        pending_attempt.next_reconcile_at = datetime.now(UTC)
        payment.status = 'VERIFYING'
        abandoned_summary = pending_summary
    elif resolution != 'activate':
        raise TrialCheckoutResolutionError('trial_resolution_not_applicable', 'There is no pending invoice to close')

    for checkout in discardable:
        checkout.lifecycle_state = 'cancelled'
        checkout.quote_state = 'expired'
        checkout.funding_state = 'not_started'
        checkout.terminal_reason = 'superseded_by_trial_activation'
        checkout.updated_at = datetime.now(UTC)

    parameters = await _trial_parameters(db, forced_device_limit=forced_device_limit)
    charged_amount = 0
    charge_transaction_id: int | None = None
    price_kopeks = preview_trial_activation_charge(user)
    if price_kopeks > 0:
        charged = await subtract_user_balance(
            db,
            user,
            price_kopeks,
            charge_description,
            mark_as_paid_subscription=True,
            commit=False,
        )
        if not charged:
            raise TrialPaymentChargeFailed
        transaction = await create_transaction(
            db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price_kopeks,
            description=charge_description,
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )
        charged_amount = price_kopeks
        charge_transaction_id = transaction.id
    try:
        subscription = await create_trial_subscription(
            db,
            user.id,
            **parameters,
            commit=False,
        )
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise TrialCheckoutResolutionError(
            'trial_state_changed_retry',
            'Trial state changed while it was being activated. Please retry.',
        ) from error
    await db.refresh(subscription)
    return TrialActivationResult(
        subscription,
        charged_amount,
        abandoned_summary,
        charge_transaction_id=charge_transaction_id,
    )

"""Platega adapter for device-first checkout.

The attempt is committed before the network call. An ambiguous timeout is
fail-closed and requires reconciliation; the same attempt never creates a
second provider invoice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    CheckoutPaymentAttempt,
    PaymentMethodConfig,
    PlategaPayment,
    SubscriptionCheckout,
    Transaction,
    TransactionType,
    User,
)
from app.services.device_first_checkout_service import (
    DeviceFirstError,
    get_owned_checkout,
)
from app.services.device_first_deposit_outbox_service import (
    ensure_deposit_outbox,
    process_device_first_deposit_outbox,
)
from app.services.platega_service import PlategaService


PLATEGA_METHODS = {
    'sbp': 2,
    'cards_ru': 11,
    'crypto': 13,
}
logger = structlog.get_logger(__name__)


def _checkout_return_url(checkout_public_id: str, *, failed: bool = False) -> str | None:
    configured = settings.get_platega_failed_url() if failed else settings.get_platega_return_url()
    if not configured:
        return None
    parsed = urlsplit(configured)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    query = {'checkout': checkout_public_id}
    if failed:
        query['payment'] = 'failed'
    return urlunsplit((parsed.scheme, parsed.netloc, '/subscription/purchase', urlencode(query), ''))


def available_platega_methods() -> list[dict[str, object]]:
    if not settings.is_platega_enabled():
        return []
    active = set(settings.get_platega_active_methods())
    return [{'key': key, 'provider_code': code} for key, code in PLATEGA_METHODS.items() if code in active]


async def available_platega_methods_for_db(
    db: AsyncSession,
    user: User | None = None,
) -> list[dict[str, object]]:
    """Intersect code allowlist, live provider methods and cabinet admin config."""
    methods = available_platega_methods()
    from app.services.payment_method_config_service import get_enabled_methods_for_user

    configured = await get_enabled_methods_for_user(
        db,
        user=user,
        is_first_topup=bool(
            user
            and not (
                await db.scalar(
                    select(
                        exists().where(
                            Transaction.user_id == user.id,
                            Transaction.type == TransactionType.DEPOSIT.value,
                            Transaction.is_completed.is_(True),
                        )
                    )
                )
            )
        ),
    )
    platega = next((method for method in configured if method['id'] == 'platega'), None)
    if platega is None:
        return []
    config = (
        await db.execute(select(PaymentMethodConfig).where(PaymentMethodConfig.method_id == 'platega'))
    ).scalar_one_or_none()
    if config is None:
        return methods
    sub_options = config.sub_options or {}
    if not sub_options:
        return methods
    return [item for item in methods if bool(sub_options.get(str(item['provider_code']), False))]


async def create_platega_attempt(
    db: AsyncSession,
    *,
    checkout_public_id: str,
    user_id: int,
    method_key: str,
) -> CheckoutPaymentAttempt:
    method_code = PLATEGA_METHODS.get(method_key)
    payment_user = await db.get(User, user_id)
    allowed_methods = await available_platega_methods_for_db(db, payment_user)
    if method_code is None or method_code not in {item['provider_code'] for item in allowed_methods}:
        raise DeviceFirstError('payment_method_unavailable', 'Payment method is unavailable', status_code=422)
    checkout = await get_owned_checkout(
        db,
        public_id=checkout_public_id,
        user_id=user_id,
        for_update=True,
    )
    if checkout.lifecycle_state not in {'awaiting_funds', 'armed'} or checkout.armed_at is None:
        raise DeviceFirstError('invalid_state', 'Checkout is not ready for a payment attempt')
    existing = (
        await db.execute(
            select(CheckoutPaymentAttempt).where(
                CheckoutPaymentAttempt.checkout_id == checkout.id,
                CheckoutPaymentAttempt.status.in_(['creating', 'pending', 'paid_processing', 'reconciliation']),
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.status in {'creating', 'reconciliation'}:
            raise DeviceFirstError(
                'reconciliation_required',
                'Invoice creation outcome is ambiguous; automatic duplicate creation is blocked',
            )
        return existing
    if not settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED:
        raise DeviceFirstError('feature_disabled', 'New payment attempts are temporarily disabled')

    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    shortage = max(0, checkout.max_price_kopeks - user.balance_kopeks)
    if shortage <= 0:
        raise DeviceFirstError('funding_not_required', 'Balance already covers the checkout')
    if shortage < settings.PLATEGA_MIN_AMOUNT_KOPEKS or shortage > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
        raise DeviceFirstError(
            'provider_amount_out_of_range', 'Required amount is outside Platega limits', status_code=422
        )

    correlation_id = uuid.uuid4().hex
    return_url = _checkout_return_url(checkout.public_id)
    failed_url = _checkout_return_url(checkout.public_id, failed=True)
    attempt = CheckoutPaymentAttempt(
        checkout_id=checkout.id,
        merchant_order_key=f'df-{checkout.public_id}-{uuid.uuid4().hex[:12]}',
        method_key=method_key,
        provider_method_code=method_code,
        currency='RUB',
        requested_amount_kopeks=shortage,
        status='creating',
    )
    db.add(attempt)
    await db.flush()
    payment = PlategaPayment(
        user_id=user_id,
        correlation_id=correlation_id,
        amount_kopeks=shortage,
        currency='RUB',
        description=f'Device-first checkout {checkout.public_id}',
        payment_method_code=method_code,
        status='CREATING',
        return_url=return_url,
        failed_url=failed_url,
        payload=f'platega:{correlation_id}',
        metadata_json={
            'device_first_attempt_id': attempt.id,
            'merchant_order_key': attempt.merchant_order_key,
        },
    )
    db.add(payment)
    await db.flush()
    attempt.platega_payment_id = payment.id
    await db.commit()
    await db.refresh(attempt)

    service = PlategaService()
    # Provider does not expose an idempotency key. Retrying an ambiguous POST can
    # duplicate invoices, therefore this bounded adapter performs one POST only.
    service._max_retries = 1
    try:
        response = await service.create_payment(
            payment_method=method_code,
            amount=float(Decimal(shortage) / Decimal(100)),
            currency='RUB',
            description=f'VPN checkout {checkout.public_id[:8]}',
            return_url=return_url,
            failed_url=failed_url,
            payload=f'platega:{correlation_id}',
        )
    except Exception as error:
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = f'create_exception:{type(error).__name__}'
        payment.status = 'RECONCILIATION'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider result is ambiguous') from error
    if not response:
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'empty_provider_response'
        payment.status = 'RECONCILIATION'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider did not return a canonical invoice')

    transaction_id = response.get('transactionId') or response.get('id')
    redirect_url = PlategaService.parse_redirect_url(response)
    if not transaction_id or not redirect_url:
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'provider_response_missing_identity_or_url'
        payment.status = 'RECONCILIATION'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider invoice requires reconciliation')

    payment.platega_transaction_id = str(transaction_id)
    payment.status = str(response.get('status') or 'PENDING').upper()
    payment.redirect_url = redirect_url
    payment.metadata_json = {**(payment.metadata_json or {}), 'raw_response': response}
    attempt.provider_payment_id = str(transaction_id)
    attempt.redirect_url = redirect_url
    attempt.status = 'pending'
    await db.commit()
    await db.refresh(attempt)
    logger.info(
        'device_first_event',
        action='payment_attempt_created',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
        method_key=attempt.method_key,
        requested_amount_kopeks=attempt.requested_amount_kopeks,
    )
    return attempt


def _verified_amount(payload: dict | None) -> tuple[int, str] | None:
    if not isinstance(payload, dict):
        return None
    details = payload.get('paymentDetails')
    details = details if isinstance(details, dict) else {}
    raw_amount = details.get('amount', payload.get('amount'))
    currency = str(details.get('currency', payload.get('currency', ''))).upper()
    if currency != 'RUB':
        return None
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    kopeks = int((amount * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return kopeks, currency


async def settle_device_first_platega_payment(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    payload: dict | None,
) -> PlategaPayment:
    payment = (
        await db.execute(select(PlategaPayment).where(PlategaPayment.id == payment.id).with_for_update())
    ).scalar_one()
    metadata = dict(payment.metadata_json or {})
    attempt_id = metadata.get('device_first_attempt_id')
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id).with_for_update()
        )
    ).scalar_one_or_none()
    transaction_id = str((payload or {}).get('id') or (payload or {}).get('transactionId') or '').strip()
    method_raw = (payload or {}).get('paymentMethod')
    if method_raw is None:
        method_raw = (payload or {}).get('paymentMethodCode')
    identity_mismatch = bool(
        attempt and attempt.provider_payment_id and transaction_id and transaction_id != attempt.provider_payment_id
    )
    method_mismatch = False
    if attempt is not None and method_raw is not None:
        try:
            method_mismatch = int(method_raw) != attempt.provider_method_code
        except (TypeError, ValueError):
            method_mismatch = True
    if attempt is not None and (identity_mismatch or method_mismatch):
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = (
            'provider_identity_mismatch' if identity_mismatch else 'provider_method_mismatch'
        )
        payment.callback_payload = payload
        await db.commit()
        return payment
    if attempt is not None and not attempt.provider_payment_id and transaction_id:
        attempt.provider_payment_id = transaction_id
    verified = _verified_amount(payload)
    if attempt is None or verified is None:
        if attempt is not None:
            attempt.status = 'reconciliation'
            attempt.reconciliation_reason = 'missing_or_invalid_verified_rub_amount'
            await db.commit()
        return payment

    checkout = (
        await db.execute(
            select(SubscriptionCheckout).where(SubscriptionCheckout.id == attempt.checkout_id).with_for_update()
        )
    ).scalar_one()
    user = (await db.execute(select(User).where(User.id == payment.user_id).with_for_update())).scalar_one()
    amount_kopeks, _ = verified
    ledger_key = f'deposit:{attempt.id}'
    existing = (
        await db.execute(select(Transaction).where(Transaction.device_first_ledger_key == ledger_key))
    ).scalar_one_or_none()
    if existing is not None and attempt.status == 'credited':
        payment.transaction_id = existing.id
        payment.status = 'CONFIRMED'
        payment.is_paid = True
        payment.callback_payload = payload
        await ensure_deposit_outbox(
            db,
            transaction_id=existing.id,
            checkout_id=checkout.id,
        )
        await db.commit()
        await process_device_first_deposit_outbox(
            db,
            transaction_id=existing.id,
            limit=1,
        )
        return payment
    created_transaction = existing is None
    transaction = existing
    if created_transaction:
        user.balance_kopeks += amount_kopeks
        transaction = Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=amount_kopeks,
            description=f'Platega для checkout {checkout.public_id}',
            payment_method='platega',
            external_id=payment.platega_transaction_id or payment.correlation_id,
            device_first_checkout_id=checkout.id,
            device_first_ledger_key=ledger_key,
            is_completed=True,
        )
        db.add(transaction)
        await db.flush()
    payment.transaction_id = transaction.id
    deposit_job = await ensure_deposit_outbox(
        db,
        transaction_id=transaction.id,
        checkout_id=checkout.id,
    )
    payment.status = 'CONFIRMED'
    payment.is_paid = True
    payment.callback_payload = payload
    payment.metadata_json = {**metadata, 'webhook': payload, 'balance_credited': True}
    attempt.credited_amount_kopeks = amount_kopeks
    attempt.status = 'credited'
    is_underpayment = amount_kopeks < attempt.requested_amount_kopeks
    if amount_kopeks != attempt.requested_amount_kopeks:
        attempt.reconciliation_reason = (
            f'amount_mismatch:requested={attempt.requested_amount_kopeks}:actual={amount_kopeks}'
        )
    if checkout.lifecycle_state in {'cancelled', 'expired', 'failed', 'reprice_required', 'conflict'}:
        attempt.reconciliation_reason = 'late_paid_credited_to_balance_only'
        deposit_job.fulfillment_status = 'not_required'
    elif checkout.armed_at is None:
        attempt.reconciliation_reason = 'paid_checkout_was_not_armed'
        deposit_job.fulfillment_status = 'not_required'
        checkout.lifecycle_state = 'awaiting_funds'
    elif is_underpayment:
        checkout.lifecycle_state = 'awaiting_funds'
        checkout.funding_state = 'partial'
        deposit_job.fulfillment_status = 'action_required'
    elif checkout.armed_at is not None:
        checkout.lifecycle_state = 'armed'
        # Crediting the exact/greater provider amount is the financial commit
        # point. Mark fulfillment as started in the same DB transaction so a
        # concurrent user cancellation cannot strand credited money without
        # completing the explicitly approved purchase.
        checkout.fulfillment_state = 'in_progress'
        deposit_job.fulfillment_status = 'pending'
    await db.commit()
    await process_device_first_deposit_outbox(
        db,
        transaction_id=transaction.id,
        limit=1,
    )
    logger.info(
        'device_first_event',
        action='payment_credited',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
        credited_amount_kopeks=amount_kopeks,
        late_balance_only=checkout.lifecycle_state
        in {'cancelled', 'expired', 'failed', 'reprice_required', 'conflict'},
    )

    return payment


async def reconcile_device_first_payments(db: AsyncSession, *, limit: int = 20) -> int:
    """Poll known Platega identities; correlation-only rows remain webhook-recoverable."""
    attempts = list(
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(
                    CheckoutPaymentAttempt.status.in_(['pending', 'reconciliation']),
                    CheckoutPaymentAttempt.provider_payment_id.is_not(None),
                    CheckoutPaymentAttempt.platega_payment_id.is_not(None),
                    CheckoutPaymentAttempt.next_reconcile_at <= datetime.now(UTC),
                )
                .order_by(
                    CheckoutPaymentAttempt.next_reconcile_at,
                    CheckoutPaymentAttempt.id,
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    service = PlategaService()
    reconciled = 0
    for attempt in attempts:
        attempt.reconcile_attempts += 1
        attempt.next_reconcile_at = datetime.now(UTC) + timedelta(
            minutes=min(60, 2 ** min(attempt.reconcile_attempts, 6))
        )
        await db.commit()
        try:
            payload = await service.get_transaction(attempt.provider_payment_id)
        except Exception as error:
            attempt.reconciliation_reason = f'status_lookup:{type(error).__name__}'
            await db.commit()
            continue
        if not payload:
            attempt.reconciliation_reason = 'status_lookup:empty'
            await db.commit()
            continue
        status = str(payload.get('status') or '').upper()
        payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        if payment is None:
            continue
        if status == 'CONFIRMED':
            await settle_device_first_platega_payment(db, payment=payment, payload=payload)
            reconciled += 1
        else:
            payment = (
                await db.execute(select(PlategaPayment).where(PlategaPayment.id == payment.id).with_for_update())
            ).scalar_one()
            attempt = (
                await db.execute(
                    select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt.id).with_for_update()
                )
            ).scalar_one()
            if status in {'FAILED', 'CANCELED', 'EXPIRED'}:
                attempt.status = 'failed'
                attempt.reconciliation_reason = f'provider_terminal:{status.lower()}'
                payment.status = status
            else:
                attempt.reconciliation_reason = f'provider_pending:{status or "unknown"}'
            await db.commit()
    return reconciled

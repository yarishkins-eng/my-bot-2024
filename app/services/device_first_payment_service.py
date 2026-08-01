"""Platega adapter for device-first checkout.

The attempt is committed before the network call. An ambiguous timeout is
fail-closed and requires reconciliation; the same attempt never creates a
second provider invoice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceFirstReconciliationCredit,
    PaymentMethodConfig,
    PlategaPayment,
    SubscriptionCheckout,
    Transaction,
    TransactionType,
    User,
)
from app.services.device_first_checkout_service import (
    DIRECT_SETTLEMENT_MODE,
    LEGACY_SETTLEMENT_MODE,
    DeviceFirstError,
    device_first_new_checkouts_enabled,
    device_first_top_up_kopeks,
    expire_checkout_quote_if_needed,
    fulfill_direct_external_checkout,
    get_owned_checkout,
    prepare_direct_external_checkout,
    settlement_mode,
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
PENDING_ATTEMPT_STATUSES = frozenset({'creating', 'pending', 'paid_processing', 'reconciliation'})


def platega_method_label(method_key: str, *, language: str) -> str:
    """Return a person-facing label; provider keys are never Telegram UI text."""
    labels = {
        'sbp': ('СБП', 'SBP'),
        'cards_ru': ('Карта российского банка', 'Russian bank card'),
        'crypto': ('Криптовалюта', 'Cryptocurrency'),
    }
    russian, english = labels.get(method_key, ('Способ оплаты', 'Payment method'))
    return english if language == 'en' else russian


async def get_pending_platega_attempt(
    db: AsyncSession,
    *,
    checkout_id: int,
) -> CheckoutPaymentAttempt | None:
    """Return the newest unresolved invoice, if it blocks creating another one."""
    return (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(
                CheckoutPaymentAttempt.checkout_id == checkout_id,
                CheckoutPaymentAttempt.status.in_(PENDING_ATTEMPT_STATUSES),
            )
            .order_by(CheckoutPaymentAttempt.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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


def _direct_checkout_return_url(checkout_public_id: str, *, failed: bool = False) -> str | None:
    """Never inherit the generic top-up redirect (it may be a Telegram URL)."""
    configured = (settings.CABINET_URL or '').strip()
    parsed = urlsplit(configured)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.netloc.lower() == 't.me':
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
    allow_direct_pre_attempt_recovery: bool = False,
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
        # Direct v2 takes its canonical user → checkout lock inside
        # ``prepare_direct_external_checkout``.  Do not pre-lock the checkout
        # here or a concurrent provider reversal could invert that order.
        for_update=False,
    )
    if settlement_mode(checkout) == DIRECT_SETTLEMENT_MODE:
        return await _create_direct_platega_attempt(
            db,
            checkout_public_id=checkout_public_id,
            user_id=user_id,
            method_key=method_key,
            method_code=method_code,
            was_financially_committed=checkout.financial_committed_at is not None,
            allow_pre_attempt_recovery=allow_direct_pre_attempt_recovery,
        )
    checkout = await get_owned_checkout(
        db,
        public_id=checkout_public_id,
        user_id=user_id,
        for_update=True,
    )
    if checkout.lifecycle_state not in {'awaiting_funds', 'armed'} or checkout.armed_at is None:
        raise DeviceFirstError('invalid_state', 'Checkout is not ready for a payment attempt')
    # An exact provider payment moves the checkout into fulfillment before the
    # outbox finishes.  It may still look ``armed`` while the worker runs, but
    # it is no longer an order that can accept an invoice.  This closes the
    # short replay window between provider settlement and fulfillment.
    if checkout.fulfillment_state != 'not_started':
        raise DeviceFirstError('invalid_state', 'Checkout is already being fulfilled')
    if await expire_checkout_quote_if_needed(db, checkout):
        raise DeviceFirstError('quote_expired', 'Checkout quote has expired')
    existing = await get_pending_platega_attempt(db, checkout_id=checkout.id)
    if existing:
        if existing.status in {'creating', 'reconciliation'}:
            raise DeviceFirstError(
                'reconciliation_required',
                'Invoice creation outcome is ambiguous; automatic duplicate creation is blocked',
            )
        return existing
    if not device_first_new_checkouts_enabled():
        raise DeviceFirstError('feature_disabled', 'New payment attempts are temporarily disabled')

    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    shortage = device_first_top_up_kopeks(
        price_kopeks=checkout.max_price_kopeks,
        balance_kopeks=user.balance_kopeks,
    )
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
        settlement_mode=LEGACY_SETTLEMENT_MODE,
        status='creating',
    )
    db.add(attempt)
    try:
        await db.flush()
    except IntegrityError as error:
        # Two concurrent final taps may both pass the preliminary read. The
        # partial active-attempt constraint decides the winner; the loser only
        # resumes that one invoice and must never POST a second provider bill.
        await db.rollback()
        existing = await get_pending_platega_attempt(db, checkout_id=checkout.id)
        if existing is not None:
            return existing
        raise DeviceFirstError('reconciliation_required', 'Invoice creation requires reconciliation') from error
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


async def _create_direct_platega_attempt(
    db: AsyncSession,
    *,
    checkout_public_id: str,
    user_id: int,
    method_key: str,
    method_code: int,
    was_financially_committed: bool,
    allow_pre_attempt_recovery: bool = False,
) -> CheckoutPaymentAttempt:
    """Create one full-price v2 invoice after the funding choice is durable."""
    return_url = _direct_checkout_return_url(checkout_public_id)
    failed_url = _direct_checkout_return_url(checkout_public_id, failed=True)
    if not return_url or not failed_url:
        raise DeviceFirstError('cabinet_return_unavailable', 'Secure cabinet return URL is unavailable')

    checkout = await prepare_direct_external_checkout(
        db,
        public_id=checkout_public_id,
        user_id=user_id,
        commit=False,
    )
    existing = await get_pending_platega_attempt(db, checkout_id=checkout.id)
    if existing is not None:
        return existing
    # A regular final-confirmation request can only resume an already-known
    # durable attempt.  The separate recovery endpoint may create exactly one
    # invoice only for the explicitly checked pre-attempt crash state.
    if was_financially_committed and not allow_pre_attempt_recovery:
        raise DeviceFirstError('invalid_state', 'Checkout has no resumable payment attempt')
    payable = checkout.external_payable_kopeks
    if payable <= 0 or payable != checkout.tariff_total_kopeks or checkout.wallet_applied_kopeks != 0:
        raise DeviceFirstError('operator_review_required', 'Invalid direct-sale funding totals')
    if payable < settings.PLATEGA_MIN_AMOUNT_KOPEKS or payable > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
        raise DeviceFirstError(
            'provider_amount_out_of_range', 'Required amount is outside Platega limits', status_code=422
        )

    correlation_id = uuid.uuid4().hex
    attempt = CheckoutPaymentAttempt(
        checkout_id=checkout.id,
        merchant_order_key=f'dfv2-{checkout.public_id}-{uuid.uuid4().hex[:12]}',
        method_key=method_key,
        provider_method_code=method_code,
        currency='RUB',
        requested_amount_kopeks=payable,
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        status='creating',
        # Let the caller complete its one bounded provider POST first. If it
        # dies, reconciliation will later preserve the attempt for operator
        # review rather than guessing that a second invoice is safe to create.
        next_reconcile_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(attempt)
    try:
        await db.flush()
    except IntegrityError as error:
        # The partial unique constraint is the final arbiter for concurrent
        # commits. Reuse the winner, never send a second provider POST.
        await db.rollback()
        existing = await get_pending_platega_attempt(db, checkout_id=checkout.id)
        if existing is not None:
            return existing
        raise DeviceFirstError('reconciliation_required', 'Invoice creation requires reconciliation') from error
    payment = PlategaPayment(
        user_id=user_id,
        correlation_id=correlation_id,
        amount_kopeks=payable,
        currency='RUB',
        description=f'Device-first checkout {checkout.public_id}',
        payment_method_code=method_code,
        status='CREATING',
        return_url=return_url,
        failed_url=failed_url,
        payload=f'platega:{correlation_id}',
        metadata_json={
            'device_first_attempt_id': attempt.id,
            'settlement_mode': DIRECT_SETTLEMENT_MODE,
            'merchant_order_key': attempt.merchant_order_key,
        },
    )
    db.add(payment)
    await db.flush()
    attempt.platega_payment_id = payment.id
    await db.commit()
    await db.refresh(attempt)

    service = PlategaService()
    service._max_retries = 1
    try:
        response = await service.create_payment(
            payment_method=method_code,
            amount=float(Decimal(payable) / Decimal(100)),
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
    transaction_id = str((response or {}).get('transactionId') or (response or {}).get('id') or '').strip()
    redirect_url = PlategaService.parse_redirect_url(response)
    returned = PlategaService.parse_amount_currency(response)
    if not response or not transaction_id or not redirect_url or returned is None:
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'provider_response_missing_canonical_invoice'
        payment.status = 'RECONCILIATION'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider did not return a canonical invoice')

    returned_amount, returned_currency = returned
    attempt.provider_payment_id = transaction_id
    attempt.provider_returned_amount_kopeks = returned_amount
    attempt.provider_returned_currency = returned_currency
    payment.platega_transaction_id = transaction_id
    # The URL is stored only in the existing protected provider-payment record;
    # response metadata and the direct attempt deliberately never retain it.
    payment.redirect_url = redirect_url
    payment.expires_at = PlategaService.parse_expires_at(response.get('expiresIn'))
    if returned_amount != payable or returned_currency != 'RUB':
        attempt.status = 'provider_invoice_mismatch'
        attempt.reconciliation_reason = 'provider_invoice_mismatch'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'provider_invoice_mismatch'
        payment.status = 'RECONCILIATION'
        await db.commit()
        await service.cancel_payment(transaction_id)
        raise DeviceFirstError('provider_invoice_mismatch', 'Provider invoice amount does not match the final price')

    payment.status = str(response.get('status') or 'PENDING').upper()
    attempt.status = 'pending'
    await db.commit()
    await db.refresh(attempt)
    logger.info(
        'device_first_event',
        action='direct_payment_attempt_created',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
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
    kopeks_decimal = amount * Decimal(100)
    # The provider is the financial source of truth.  Do not make a fractional
    # kopek look like an exact invoice by rounding it in either direction.
    if kopeks_decimal != kopeks_decimal.to_integral_value():
        return None
    kopeks = int(kopeks_decimal)
    return kopeks, currency


async def settle_device_first_platega_payment(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    payload: dict | None,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> PlategaPayment | None:
    if (lease_token is None) != (lease_epoch is None):
        raise ValueError('direct payment lease requires both token and epoch')
    # The reconciler may have read this row before an authenticated webhook
    # committed. ``expire_on_commit=False`` is deliberate in this project, so
    # the provider lock must also refresh the identity-map instance before a
    # terminal provider result is allowed to mutate a direct checkout.
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    metadata = dict(payment.metadata_json or {})
    attempt_id = metadata.get('device_first_attempt_id')
    attempt_query = select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id)
    if lease_token is not None:
        attempt_query = attempt_query.where(
            CheckoutPaymentAttempt.lease_token == lease_token,
            CheckoutPaymentAttempt.lease_epoch == lease_epoch,
            CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
        )
    attempt = (
        await db.execute(attempt_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if attempt is None and lease_token is not None:
        logger.warning('device_first_direct_payment_lease_lost', payment_id=payment.id)
        return None
    direct_payment = metadata.get('settlement_mode') == DIRECT_SETTLEMENT_MODE
    if direct_payment and attempt is None:
        # A protected direct provider row without its matching attempt cannot
        # be classified as a legacy deposit. Preserve the evidence and require
        # an operator; do not credit balance or synthesize a sale.
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()
        logger.error('device_first_direct_payment_attempt_missing', payment_id=payment.id)
        return payment
    if direct_payment and settlement_mode(attempt) != DIRECT_SETTLEMENT_MODE:
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_payment_attempt_mode_mismatch'
        await db.commit()
        logger.error('device_first_direct_payment_mode_mismatch', payment_id=payment.id, attempt_id=attempt.id)
        return payment
    if attempt is not None and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
        return await _settle_direct_platega_payment_locked(
            db,
            payment=payment,
            attempt=attempt,
            payload=payload,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
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
    amount_matches_invoice = amount_kopeks == attempt.requested_amount_kopeks
    quote_expired_after_invoice = (
        checkout.lifecycle_state in {'draft', 'confirmed', 'awaiting_funds', 'armed', 'fulfilling'}
        and not (
            getattr(checkout, 'fulfillment_state', 'not_started') == 'in_progress'
            and getattr(checkout, 'quote_state', None) == 'committed'
        )
        and datetime.now(UTC) >= checkout.quote_expires_at
    )
    if checkout.lifecycle_state in {'cancelled', 'expired', 'failed', 'reprice_required', 'conflict'}:
        attempt.reconciliation_reason = 'late_paid_credited_to_balance_only'
        deposit_job.fulfillment_status = 'not_required'
    elif quote_expired_after_invoice:
        # Platega may settle an already opened invoice after its local quote
        # expired. Keep the money, but never apply an old quote automatically.
        checkout.lifecycle_state = 'reprice_required'
        checkout.quote_state = 'expired'
        checkout.terminal_reason = 'quote_expired'
        attempt.reconciliation_reason = 'quote_expired_paid_credited_to_balance_only'
        deposit_job.fulfillment_status = 'not_required'
    elif checkout.armed_at is None:
        attempt.reconciliation_reason = 'paid_checkout_was_not_armed'
        deposit_job.fulfillment_status = 'not_required'
        checkout.lifecycle_state = 'awaiting_funds'
    elif not amount_matches_invoice:
        # Never infer customer consent from an invoice whose provider-reported
        # amount differs from the amount displayed in the checkout. The actual
        # funds remain available on balance for a fresh, explicit quote.
        checkout.lifecycle_state = 'conflict'
        checkout.terminal_reason = 'payment_amount_mismatch'
        attempt.reconciliation_reason = (
            f'amount_mismatch:requested={attempt.requested_amount_kopeks}:actual={amount_kopeks}'
        )
        deposit_job.fulfillment_status = 'not_required'
    elif checkout.armed_at is not None:
        checkout.lifecycle_state = 'armed'
        # Crediting the exact provider amount is the financial commit
        # point. Mark fulfillment as started in the same DB transaction so a
        # concurrent user cancellation cannot strand credited money without
        # completing the explicitly approved purchase.
        checkout.fulfillment_state = 'in_progress'
        checkout.funding_state = 'funded'
        checkout.quote_state = 'committed'
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


async def _store_reconciliation_credit(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    attempt: CheckoutPaymentAttempt,
    payment: PlategaPayment,
    provider_payment_id: str,
    amount_kopeks: int,
    currency: str,
) -> DeviceFirstReconciliationCredit:
    """Record provider money once without turning it into spendable balance."""
    credit = (
        await db.execute(
            select(DeviceFirstReconciliationCredit)
            .where(DeviceFirstReconciliationCredit.attempt_id == attempt.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credit is None:
        credit = DeviceFirstReconciliationCredit(
            checkout_id=checkout.id,
            attempt_id=attempt.id,
            user_id=payment.user_id,
            provider_payment_id=provider_payment_id,
            amount_kopeks=amount_kopeks,
            currency=currency,
        )
        db.add(credit)
    return credit


async def _settle_direct_platega_payment_locked(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    attempt: CheckoutPaymentAttempt,
    payload: dict | None,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> PlategaPayment:
    """Settle only an authenticated exact direct-sale provider payment.

    This function is called from the verified webhook/status-lookup path while
    the provider payment and attempt rows are locked.  Browser returns and
    client polling only observe the resulting checkout state.
    """
    provider_payment_id = str((payload or {}).get('id') or (payload or {}).get('transactionId') or '').strip()
    method_raw = (payload or {}).get('paymentMethod', (payload or {}).get('paymentMethodCode'))
    verified = _verified_amount(payload)
    identity_ok = bool(provider_payment_id and provider_payment_id == attempt.provider_payment_id)
    try:
        method_ok = method_raw is not None and int(method_raw) == attempt.provider_method_code
    except (TypeError, ValueError):
        method_ok = False
    mismatch_reason: str | None = None
    if not identity_ok:
        mismatch_reason = 'provider_identity_mismatch'
    elif not method_ok:
        mismatch_reason = 'provider_method_mismatch'
    elif verified is None:
        mismatch_reason = 'missing_or_invalid_verified_rub_amount'
    else:
        amount_kopeks, currency = verified
        if amount_kopeks != attempt.requested_amount_kopeks or currency != 'RUB':
            mismatch_reason = 'provider_callback_amount_mismatch'

    # The webhook can already have committed the exact payment and fulfilled
    # the checkout while a worker was waiting for this lock.  A refreshed
    # paid-processing attempt is a completed idempotent transition: do not
    # inspect the now-``ready`` checkout or downgrade it to operator review.
    if mismatch_reason is None and attempt.status == 'paid_processing':
        return payment

    # Use the same per-user lock as direct checkout creation/final commit
    # before this callback can write an operator hold.  This prevents an old
    # provider contradiction from racing a newer direct draft into payment.
    if payment.user_id is not None:
        await db.execute(select(User).where(User.id == payment.user_id).with_for_update())
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if mismatch_reason is None and checkout.lifecycle_state not in {'awaiting_funds', 'fulfilling'}:
        mismatch_reason = 'late_paid_direct_checkout'

    if mismatch_reason is not None:
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = mismatch_reason
        payment.status = 'OPERATOR_REVIEW'
        payment.is_paid = True
        if verified is not None and provider_payment_id:
            amount_kopeks, currency = verified
            attempt.credited_amount_kopeks = amount_kopeks
            await _store_reconciliation_credit(
                db,
                checkout=checkout,
                attempt=attempt,
                payment=payment,
                provider_payment_id=provider_payment_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
            )
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = mismatch_reason
        await db.commit()
        return payment

    amount_kopeks, _currency = verified
    payment.status = 'CONFIRMED'
    payment.is_paid = True
    attempt.credited_amount_kopeks = amount_kopeks
    attempt.status = 'paid_processing'
    checkout.lifecycle_state = 'fulfilling'
    checkout.funding_state = 'paid'
    # Do not copy payment URLs, browser data or callback signatures into logs
    # or metadata.  The protected provider row is the correlation source.
    payment.metadata_json = {
        **(payment.metadata_json or {}),
        'direct_sale_paid': True,
    }
    await db.commit()
    try:
        await fulfill_direct_external_checkout(
            db,
            checkout_id=checkout.id,
            provider_payment_id=provider_payment_id,
            payment_attempt_id=attempt.id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
        )
    except DeviceFirstError:
        # The durable paid_processing status lets the scoped worker continue or
        # leave the checkout in an explicit operator-review state.
        logger.warning('device_first_direct_fulfillment_requires_recovery', checkout_id=checkout.public_id)
    return payment


async def _release_direct_attempt_lease(
    db: AsyncSession,
    *,
    attempt_id: int,
    lease_token: str | None,
    lease_epoch: int | None,
) -> None:
    if lease_token is None or lease_epoch is None:
        return
    await db.execute(
        update(CheckoutPaymentAttempt)
        .where(
            CheckoutPaymentAttempt.id == attempt_id,
            CheckoutPaymentAttempt.lease_token == lease_token,
            CheckoutPaymentAttempt.lease_epoch == lease_epoch,
        )
        .values(lease_token=None, lease_expires_at=None)
    )
    await db.commit()


async def _lock_owned_direct_attempt_lease(
    db: AsyncSession,
    *,
    attempt_id: int,
    lease_token: str,
    lease_epoch: int,
) -> CheckoutPaymentAttempt | None:
    """Lock the attempt only if this worker still owns its unexpired lease."""
    return (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(
                CheckoutPaymentAttempt.id == attempt_id,
                CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
                CheckoutPaymentAttempt.lease_token == lease_token,
                CheckoutPaymentAttempt.lease_epoch == lease_epoch,
                CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def reconcile_device_first_payments(
    db: AsyncSession,
    *,
    limit: int = 20,
    direct_only: bool = False,
) -> int:
    """Poll known identities with v2 lease fencing; browser state never settles money."""
    now = datetime.now(UTC)
    predicates = [
        CheckoutPaymentAttempt.next_reconcile_at <= now,
        or_(
            and_(
                CheckoutPaymentAttempt.status.in_(['pending', 'reconciliation', 'paid_processing']),
                CheckoutPaymentAttempt.provider_payment_id.is_not(None),
                CheckoutPaymentAttempt.platega_payment_id.is_not(None),
            ),
            # A crash after the atomic intent commit but before a
            # provider identity is known is never retried: Platega
            # has no idempotency key. Escalate it after a grace
            # period instead of risking a second invoice.
            and_(
                CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
                CheckoutPaymentAttempt.status == 'creating',
                CheckoutPaymentAttempt.provider_payment_id.is_(None),
            ),
        ),
    ]
    if direct_only:
        predicates.append(CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE)
    attempts = list(
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(*predicates)
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
        # Keep this stable across re-locks. A failed fence check returns None;
        # replacing ``attempt`` with it used to make the finally block crash
        # while releasing the lease and could abort the monitoring batch.
        attempt_id = attempt.id
        lease_token: str | None = None
        lease_epoch: int | None = None
        if settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
            lease_token = uuid.uuid4().hex
            claimed = await db.execute(
                update(CheckoutPaymentAttempt)
                .where(
                    CheckoutPaymentAttempt.id == attempt_id,
                    or_(
                        CheckoutPaymentAttempt.lease_token.is_(None),
                        CheckoutPaymentAttempt.lease_expires_at < now,
                    ),
                )
                .values(
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(minutes=5),
                    lease_epoch=CheckoutPaymentAttempt.lease_epoch + 1,
                )
            )
            if claimed.rowcount != 1:
                continue
            await db.commit()
            attempt = await db.get(CheckoutPaymentAttempt, attempt_id)
            if attempt is None:
                continue
            lease_epoch = attempt.lease_epoch
        if attempt.status == 'creating' and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
            try:
                checkout_owner_id = await db.scalar(
                    select(SubscriptionCheckout.user_id).where(SubscriptionCheckout.id == attempt.checkout_id)
                )
                if checkout_owner_id is not None:
                    await db.execute(select(User).where(User.id == checkout_owner_id).with_for_update())
                owned_attempt = await _lock_owned_direct_attempt_lease(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token or '',
                    lease_epoch=lease_epoch or -1,
                )
                if owned_attempt is None:
                    continue
                payment = (
                    await db.execute(
                        select(PlategaPayment)
                        .where(PlategaPayment.id == owned_attempt.platega_payment_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                checkout = (
                    await db.execute(
                        select(SubscriptionCheckout)
                        .where(SubscriptionCheckout.id == owned_attempt.checkout_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                owned_attempt.status = 'operator_review'
                owned_attempt.reconciliation_reason = 'provider_invoice_creation_incomplete'
                if payment is not None:
                    payment.status = 'OPERATOR_REVIEW'
                if checkout is not None:
                    checkout.lifecycle_state = 'operator_review'
                    checkout.terminal_reason = 'provider_invoice_creation_incomplete'
                await db.commit()
            finally:
                await _release_direct_attempt_lease(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
            continue
        if attempt.status == 'paid_processing' and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
            try:
                await fulfill_direct_external_checkout(
                    db,
                    checkout_id=attempt.checkout_id,
                    provider_payment_id=attempt.provider_payment_id or '',
                    payment_attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                reconciled += 1
            except DeviceFirstError as error:
                logger.warning('device_first_direct_paid_recovery_failed', attempt_id=attempt_id, code=error.code)
            finally:
                await _release_direct_attempt_lease(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
            continue
        try:
            if lease_token is not None and lease_epoch is not None:
                owned_attempt = await _lock_owned_direct_attempt_lease(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                if owned_attempt is None:
                    continue
                attempt = owned_attempt
            attempt.reconcile_attempts += 1
            attempt.next_reconcile_at = datetime.now(UTC) + timedelta(
                minutes=min(60, 2 ** min(attempt.reconcile_attempts, 6))
            )
            await db.commit()
            try:
                payload = await service.get_transaction(attempt.provider_payment_id)
            except Exception as error:
                if lease_token is not None and lease_epoch is not None:
                    attempt = await _lock_owned_direct_attempt_lease(
                        db,
                        attempt_id=attempt_id,
                        lease_token=lease_token,
                        lease_epoch=lease_epoch,
                    )
                    if attempt is None:
                        continue
                attempt.reconciliation_reason = f'status_lookup:{type(error).__name__}'
                await db.commit()
                continue
            if not payload:
                if lease_token is not None and lease_epoch is not None:
                    attempt = await _lock_owned_direct_attempt_lease(
                        db,
                        attempt_id=attempt_id,
                        lease_token=lease_token,
                        lease_epoch=lease_epoch,
                    )
                    if attempt is None:
                        continue
                attempt.reconciliation_reason = 'status_lookup:empty'
                await db.commit()
                continue
            status = str(payload.get('status') or '').upper()
            payment = await db.get(PlategaPayment, attempt.platega_payment_id)
            if payment is None:
                continue
            if status in {'FAILED', 'CANCELED', 'EXPIRED'} and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
                # The fence takes User → Attempt → Checkout.  Do not acquire
                # Attempt here first: a paid fulfilment takes that same order
                # and an inverted worker path would deadlock the reversal.
                from app.services.payment.platega import PlategaPaymentMixin

                payment = (
                    await db.execute(
                        select(PlategaPayment)
                        .where(PlategaPayment.id == payment.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if payment.is_paid:
                    await PlategaPaymentMixin()._mark_direct_post_paid_reversal(
                        db,
                        payment=payment,
                        provider_status=status,
                        lease_token=lease_token,
                        lease_epoch=lease_epoch,
                    )
                    continue
            if status == 'CONFIRMED':
                settled = await settle_device_first_platega_payment(
                    db,
                    payment=payment,
                    payload=payload,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                reconciled += int(settled is not None)
            else:
                payment = (
                    await db.execute(
                        select(PlategaPayment)
                        .where(PlategaPayment.id == payment.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if lease_token is not None and lease_epoch is not None:
                    attempt = await _lock_owned_direct_attempt_lease(
                        db,
                        attempt_id=attempt_id,
                        lease_token=lease_token,
                        lease_epoch=lease_epoch,
                    )
                    if attempt is None:
                        continue
                else:
                    attempt = (
                        await db.execute(
                            select(CheckoutPaymentAttempt)
                            .where(CheckoutPaymentAttempt.id == attempt_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                    ).scalar_one()
                if status in {'FAILED', 'CANCELED', 'EXPIRED'}:
                    attempt.status = 'failed'
                    attempt.reconciliation_reason = f'provider_terminal:{status.lower()}'
                    payment.status = status
                else:
                    attempt.reconciliation_reason = f'provider_pending:{status or "unknown"}'
                await db.commit()
        finally:
            await _release_direct_attempt_lease(
                db,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_epoch=lease_epoch,
            )
    return reconciled

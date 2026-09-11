"""Durable Platega wallet top-ups bound to device add-on intents.

The local payment graph is committed before the single provider POST.  Signed
callbacks only bind exact correlation evidence and wake canonical GET
reconciliation; they never credit a wallet by themselves.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog
from aiogram import types
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import emit_transaction_side_effects
from app.database.models import (
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PaymentMethod,
    PlategaPayment,
    Subscription,
    Transaction,
    TransactionType,
    User,
)
from app.services.account_test_reset_service import device_addon_account_activity_lock, reset_is_busy
from app.services.device_addon_service import (
    DeviceAddonError,
    calculate_device_addon,
    quote_for_calculation,
)
from app.services.device_first_deposit_outbox_service import (
    ReferralRewardBalanceFencedError,
    apply_deposit_referral_money,
)
from app.services.device_first_payment_service import (
    _provider_method_code,
    _provider_transaction_id,
    available_platega_methods_for_db,
)
from app.services.platega_service import PlategaCreateRejected, PlategaService


logger = structlog.get_logger(__name__)

_PROVIDER_LIVE = frozenset({'PENDING', 'INPROGRESS'})
_PROVIDER_TERMINAL = frozenset({'FAILED', 'CANCELED', 'EXPIRED'})
_PROVIDER_REVERSAL = frozenset({'CHARGEBACKED'})
_ACTIVE_ATTEMPTS = frozenset(
    {'prepared', 'dispatching', 'creation_unknown', 'pending', 'reconciling', 'operator_review'}
)
_NO_ID_REVIEW_DELAY = timedelta(minutes=5)
_TERMINAL_RECHECK_DELAY = timedelta(hours=6)
_OPERATOR_RECHECK_DELAY = timedelta(hours=1)
_MAX_TERMINAL_RECONCILE_ATTEMPTS = 4
# The first add-on release is proven against live canonical invoices only for
# SBP and Russian cards.  Other globally enabled Platega methods remain
# available to their existing flows, but cannot create an add-on invoice until
# their provider contract has equivalent evidence.
_PLATEGA_OPTION_CODES = {'sbp': 2, 'cards_ru': 11, '2': 2, '11': 11}


def _safe_https_url(value: Any) -> str | None:
    if not value:
        return None
    candidate = str(value)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return None
    return candidate


def _cabinet_return_url(*, intent_public_id: str, attempt_public_id: str, failed: bool) -> str | None:
    configured = (settings.CABINET_URL or '').strip()
    try:
        parsed = urlsplit(configured)
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.netloc or parsed.netloc.lower() == 't.me':
        return None
    query = {'attempt': attempt_public_id}
    if failed:
        query['payment'] = 'failed'
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f'/subscription/device-topup/{intent_public_id}',
            urlencode(query),
            '',
        )
    )


def _validate_return_surface(return_surface: str) -> None:
    if return_surface == 'telegram':
        from app.utils.miniapp_buttons import build_main_miniapp_startapp_url

        if not build_main_miniapp_startapp_url('dtu-validation'):
            raise DeviceAddonError(
                'payment_return_unavailable',
                'Не удалось подготовить безопасный возврат после оплаты.',
                status_code=503,
            )
        return
    if (
        return_surface != 'cabinet'
        or _cabinet_return_url(intent_public_id='validation', attempt_public_id='validation', failed=False) is None
    ):
        raise DeviceAddonError(
            'payment_return_unavailable',
            'Не удалось подготовить безопасный возврат после оплаты.',
            status_code=503,
        )


def _request_amount(*, price_kopeks: int, user: User) -> int:
    shortage = max(0, int(price_kopeks) - int(user.balance_kopeks or 0))
    return max(shortage, int(settings.PLATEGA_MIN_AMOUNT_KOPEKS)) if shortage else 0


def _exact_provider_invoice(attempt: DeviceAddonTopupAttempt, payload: dict[str, Any] | None) -> bool:
    correlation = (payload or {}).get('payload')
    return (
        _provider_transaction_id(payload) == str(attempt.provider_payment_id or '')
        and _provider_method_code(payload) == int(attempt.provider_method_code)
        and PlategaService.parse_amount_currency(payload) == (int(attempt.requested_amount_kopeks), 'RUB')
        and (correlation is None or hmac.compare_digest(str(correlation), f'platega:{attempt.correlation_id}'))
    )


async def _lock_payment_graph(
    db: AsyncSession,
    *,
    payment_id: int,
    attempt_id: int,
) -> tuple[PlategaPayment, User, DeviceAddonTopupAttempt, DeviceAddonIntent]:
    """Lock the immutable graph in its published financial order."""
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    user = (
        await db.execute(
            select(User).where(User.id == payment.user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    attempt = (
        await db.execute(
            select(DeviceAddonTopupAttempt)
            .where(DeviceAddonTopupAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    intent = (
        await db.execute(
            select(DeviceAddonIntent)
            .where(DeviceAddonIntent.id == attempt.intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return payment, user, attempt, intent


def _binding_is_exact(*, payment: PlategaPayment, attempt: DeviceAddonTopupAttempt, intent: DeviceAddonIntent) -> bool:
    expected_payload = f'platega:{payment.correlation_id}'
    return bool(
        attempt.platega_payment_id == payment.id
        and attempt.intent_id == intent.id
        and attempt.user_id == payment.user_id == intent.user_id
        and attempt.correlation_id == payment.correlation_id
        # Account erasure deliberately redacts provider payload/metadata while
        # retaining the FK graph and correlation id. If a payload survives it
        # must match; absence is a supported redacted evidence state.
        and (payment.payload is None or hmac.compare_digest(str(payment.payload), expected_payload))
    )


def _provider_identity_is_exact(*, payment: PlategaPayment, attempt: DeviceAddonTopupAttempt) -> bool:
    attempt_provider_id = str(attempt.provider_payment_id) if attempt.provider_payment_id else None
    payment_provider_id = str(payment.platega_transaction_id) if payment.platega_transaction_id else None
    return bool(
        attempt_provider_id and payment_provider_id and hmac.compare_digest(attempt_provider_id, payment_provider_id)
    )


def _mark_operator_review(
    *,
    payment: PlategaPayment,
    attempt: DeviceAddonTopupAttempt,
    intent: DeviceAddonIntent,
    reason: str,
) -> None:
    """Hold an ambiguity without erasing an already-owned deposit receipt."""
    if attempt.deposit_transaction_id is not None:
        attempt.status = 'paid'
        attempt.holds_invoice_slot = False
    else:
        if attempt.status != 'operator_review':
            attempt.reconcile_attempts = 0
        attempt.status = 'operator_review'
        attempt.next_reconcile_at = datetime.now(UTC) + _OPERATOR_RECHECK_DELAY
        logger.error(
            'device_addon_payment_operator_review',
            reason=reason,
            intent_public_id=intent.public_id,
            attempt_public_id=attempt.public_id,
            amount_kopeks=int(attempt.requested_amount_kopeks),
        )
    attempt.reconciliation_reason = reason
    payment.status = 'OPERATOR_REVIEW'


async def _existing_attempt_for_key(
    db: AsyncSession,
    *,
    intent_id: int,
    idempotency_key: str,
) -> DeviceAddonTopupAttempt | None:
    return await db.scalar(
        select(DeviceAddonTopupAttempt).where(
            DeviceAddonTopupAttempt.intent_id == intent_id,
            DeviceAddonTopupAttempt.idempotency_key == idempotency_key,
        )
    )


async def _active_attempt(db: AsyncSession, *, intent_id: int) -> DeviceAddonTopupAttempt | None:
    return await db.scalar(
        select(DeviceAddonTopupAttempt)
        .where(
            DeviceAddonTopupAttempt.intent_id == intent_id,
            DeviceAddonTopupAttempt.status.in_(_ACTIVE_ATTEMPTS),
            DeviceAddonTopupAttempt.holds_invoice_slot.is_(True),
        )
        .order_by(DeviceAddonTopupAttempt.id.desc())
        .limit(1)
    )


def _assert_idempotency(idempotency_key: str, request_hash: str) -> None:
    if not idempotency_key or len(idempotency_key) > 128 or len(request_hash) != 64:
        raise DeviceAddonError('idempotency_key_required', 'Нужен корректный ключ повтора.', status_code=422)


async def create_device_addon_topup(
    db: AsyncSession,
    *,
    intent_public_id: str,
    user_id: int,
    idempotency_key: str,
    request_hash: str,
    method_key: str,
    expected_amount_kopeks: int,
    return_url: str | None,
    failed_url: str | None,
    return_surface: str = 'cabinet',
) -> DeviceAddonTopupAttempt:
    """Create or replay one intent-bound invoice without an ambiguous POST retry."""
    _assert_idempotency(idempotency_key, request_hash)
    intent_stub = await db.scalar(
        select(DeviceAddonIntent).where(
            DeviceAddonIntent.public_id == intent_public_id,
            DeviceAddonIntent.user_id == user_id,
        )
    )
    if intent_stub is None:
        raise DeviceAddonError('intent_not_found', 'Операция не найдена.', status_code=404)
    existing = await _existing_attempt_for_key(db, intent_id=intent_stub.id, idempotency_key=idempotency_key)
    if existing is not None:
        if hmac.compare_digest(existing.request_hash, request_hash):
            return existing
        raise DeviceAddonError('idempotency_conflict', 'Этот ключ уже использован с другим выбором.')

    method_code = _PLATEGA_OPTION_CODES.get(method_key)
    payment_user = await db.get(User, user_id)
    allowed = await available_platega_methods_for_db(db, payment_user)
    if method_code is None or method_code not in {int(item['provider_code']) for item in allowed}:
        raise DeviceAddonError('payment_method_unavailable', 'Способ оплаты недоступен.', status_code=422)

    # Published wallet order: User -> Subscription -> Intent. Provider
    # callbacks begin with Payment, then take the same financial locks.
    user = await db.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    target_subscription_id = int(intent_stub.target_subscription_id)
    if intent_stub.subscription_id is None or int(intent_stub.subscription_id) != target_subscription_id:
        raise DeviceAddonError('target_unavailable', 'Целевая подписка больше недоступна.', status_code=409)
    subscription = await db.scalar(
        select(Subscription)
        .where(
            Subscription.id == target_subscription_id,
            Subscription.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    intent = await db.scalar(
        select(DeviceAddonIntent)
        .where(DeviceAddonIntent.id == intent_stub.id, DeviceAddonIntent.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or intent is None:
        raise DeviceAddonError('intent_not_found', 'Операция не найдена.', status_code=404)
    if subscription is None or intent.subscription_id != intent.target_subscription_id:
        raise DeviceAddonError('target_unavailable', 'Целевая подписка больше недоступна.', status_code=409)
    existing = await _existing_attempt_for_key(db, intent_id=intent.id, idempotency_key=idempotency_key)
    if existing is not None:
        if hmac.compare_digest(existing.request_hash, request_hash):
            return existing
        raise DeviceAddonError('idempotency_conflict', 'Этот ключ уже использован с другим выбором.')
    active = await _active_attempt(db, intent_id=intent.id)
    if active is not None:
        # Only the attempt's own durable idempotency key may replay it. If a
        # different key were accepted here without an alias row, repeating
        # that key after this invoice became terminal could create a second
        # provider invoice, violating the API's idempotency promise.
        raise DeviceAddonError(
            'payment_attempt_active',
            'Для этой операции уже проверяется другой платёж.',
        )
    _validate_return_surface(return_surface)
    if intent.purchase_state != 'draft':
        raise DeviceAddonError('already_purchased', 'Устройства уже куплены.')
    if not settings.DEVICE_ADDON_PURCHASE_ENABLED:
        raise DeviceAddonError(
            'device_addon_purchase_disabled',
            'Покупка устройств временно недоступна.',
            status_code=503,
        )
    if (
        getattr(user, 'status', None) != 'active'
        or getattr(user, 'restriction_subscription', False)
        or getattr(user, 'restriction_topup', False)
    ):
        raise DeviceAddonError(
            'subscription_restricted', 'Покупка устройств недоступна для этого аккаунта.', status_code=403
        )
    if (
        int(user.device_addon_generation or 0) != int(intent.device_addon_generation)
        or getattr(user, 'account_erasure_requested_at', None) is not None
        or reset_is_busy(user)
    ):
        raise DeviceAddonError('account_lifecycle_changed', 'Состояние аккаунта изменилось. Создайте новый расчёт.')

    fresh = await calculate_device_addon(
        db,
        user=user,
        subscription_id=subscription.id,
        devices_to_add=int(intent.devices_to_add),
        lock=True,
    )
    requested_amount = _request_amount(price_kopeks=fresh.price_kopeks, user=user)
    if requested_amount <= 0:
        raise DeviceAddonError('funding_not_required', 'Баланс уже покрывает покупку.')
    if requested_amount > int(settings.PLATEGA_MAX_AMOUNT_KOPEKS):
        raise DeviceAddonError(
            'provider_amount_out_of_range', 'Сумма недоступна для этого способа оплаты.', status_code=422
        )
    if int(expected_amount_kopeks) != requested_amount:
        raise DeviceAddonError(
            'funding_changed',
            'Сумма пополнения изменилась. Проверьте расчёт ещё раз.',
            status_code=409,
            quote=quote_for_calculation(fresh, user_id=user.id),
        )

    correlation_id = uuid.uuid4().hex
    payment = PlategaPayment(
        user_id=user_id,
        correlation_id=correlation_id,
        amount_kopeks=requested_amount,
        currency='RUB',
        description=f'Device add-on top-up {intent.public_id}',
        payment_method_code=method_code,
        status='PREPARED',
        return_url=return_url,
        failed_url=failed_url,
        payload=f'platega:{correlation_id}',
        metadata_json={'settlement_mode': 'device_addon_topup_v1'},
    )
    db.add(payment)
    await db.flush()
    attempt = DeviceAddonTopupAttempt(
        public_id=str(uuid.uuid4()),
        user_id=user_id,
        intent_id=intent.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        payment_method='platega',
        method_key=method_key,
        provider_method_code=method_code,
        currency='RUB',
        expected_amount_kopeks=expected_amount_kopeks,
        requested_amount_kopeks=requested_amount,
        status='prepared',
        platega_payment_id=payment.id,
        correlation_id=correlation_id,
        next_reconcile_at=datetime.now(UTC) + _NO_ID_REVIEW_DELAY,
    )
    db.add(attempt)
    intent_id = int(intent.id)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        winner = await _existing_attempt_for_key(
            db, intent_id=intent_id, idempotency_key=idempotency_key
        ) or await _active_attempt(db, intent_id=intent_id)
        if winner is not None:
            return winner
        raise DeviceAddonError('payment_reconciliation_required', 'Платёж требует проверки.') from error
    payment.metadata_json = {
        'settlement_mode': 'device_addon_topup_v1',
        'device_addon_attempt_id': attempt.id,
    }
    if return_surface == 'telegram':
        from app.utils.miniapp_buttons import build_main_miniapp_startapp_url

        telegram_return = build_main_miniapp_startapp_url(f'dtu-{attempt.public_id}')
        effective_return_url = telegram_return
        effective_failed_url = telegram_return
    else:
        effective_return_url = _safe_https_url(return_url) or _cabinet_return_url(
            intent_public_id=intent.public_id,
            attempt_public_id=attempt.public_id,
            failed=False,
        )
        effective_failed_url = _safe_https_url(failed_url) or _cabinet_return_url(
            intent_public_id=intent.public_id,
            attempt_public_id=attempt.public_id,
            failed=True,
        )
    payment.return_url = effective_return_url
    payment.failed_url = effective_failed_url
    await db.commit()

    # ``dispatching`` is durable proof that the POST may have reached Platega.
    # A crash before this commit leaves ``prepared``, which recovery may safely
    # release without guessing that an invoice exists.
    attempt.status = 'dispatching'
    payment.status = 'DISPATCHING'
    attempt.next_reconcile_at = datetime.now(UTC) + _NO_ID_REVIEW_DELAY
    await db.commit()

    service = PlategaService()
    service._max_retries = 1
    try:
        response = await service.create_device_addon_payment(
            payment_method=method_code,
            amount=float(Decimal(requested_amount) / Decimal(100)),
            currency='RUB',
            description=f'VPN devices {intent.public_id[:8]}',
            return_url=effective_return_url,
            failed_url=effective_failed_url,
            payload=f'platega:{correlation_id}',
        )
    except PlategaCreateRejected as error:
        payment, _, attempt, _ = await _lock_payment_graph(db, payment_id=payment.id, attempt_id=attempt.id)
        if attempt.status == 'dispatching' and not attempt.provider_payment_id:
            attempt.status = 'terminal'
            attempt.holds_invoice_slot = False
            attempt.reconciliation_reason = f'provider_create_rejected:{error.status_code}'
            attempt.next_reconcile_at = datetime.now(UTC) + _TERMINAL_RECHECK_DELAY
            payment.status = f'REJECTED_{error.status_code}'
        await db.commit()
        return attempt
    except Exception as error:
        payment, _, attempt, _ = await _lock_payment_graph(db, payment_id=payment.id, attempt_id=attempt.id)
        if attempt.status == 'dispatching' and not attempt.provider_payment_id:
            attempt.status = 'creation_unknown'
            attempt.reconciliation_reason = f'provider_create_exception:{type(error).__name__}'
            payment.status = 'CREATION_UNKNOWN'
        await db.commit()
        return attempt

    provider_id = _provider_transaction_id(response)
    if not provider_id:
        payment, _, attempt, _ = await _lock_payment_graph(db, payment_id=payment.id, attempt_id=attempt.id)
        if attempt.status == 'dispatching' and not attempt.provider_payment_id:
            attempt.status = 'creation_unknown'
            attempt.reconciliation_reason = 'provider_create_missing_identity'
            payment.status = 'CREATION_UNKNOWN'
        await db.commit()
        return attempt

    redirect = _safe_https_url(PlategaService.parse_redirect_url(response))
    # Re-lock the full graph because the signed callback can race the POST.
    payment, _, attempt, bound_intent = await _lock_payment_graph(db, payment_id=payment.id, attempt_id=attempt.id)
    if not _binding_is_exact(payment=payment, attempt=attempt, intent=bound_intent):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=bound_intent,
            reason='durable_payment_binding_mismatch',
        )
        await db.commit()
        return attempt
    if attempt.provider_payment_id and not hmac.compare_digest(str(attempt.provider_payment_id), provider_id):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=bound_intent,
            reason='provider_identity_conflict',
        )
        await db.commit()
        return attempt
    if not attempt.provider_payment_id:
        attempt.provider_payment_id = provider_id
        payment.platega_transaction_id = provider_id
    if redirect is not None:
        attempt.payment_url = redirect
        payment.redirect_url = redirect
    if attempt.status in {'paid', 'operator_review'}:
        # A signed callback may race the provider POST response.  Preserve its
        # review/paid decision, but retain the trusted identity returned by the
        # create call so an operator cannot close a known remote invoice and
        # accidentally allow a second one.
        await db.commit()
        return attempt
    attempt.status = 'reconciling'
    attempt.reconciliation_reason = 'canonical_verification_pending'
    attempt.next_reconcile_at = datetime.now(UTC)
    payment.status = 'RECONCILING'
    await db.commit()

    try:
        canonical = await service.get_transaction(provider_id)
    except Exception as error:
        _, _, attempt, _ = await _lock_payment_graph(db, payment_id=payment.id, attempt_id=attempt.id)
        if attempt.status not in {'paid', 'operator_review'}:
            attempt.reconciliation_reason = f'provider_status_exception:{type(error).__name__}'
        await db.commit()
        return attempt
    return await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)


async def handle_device_addon_platega_callback(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    payload: dict[str, Any],
) -> bool:
    """Record an authenticated callback and wake canonical reconciliation."""
    attempt = await db.scalar(
        select(DeviceAddonTopupAttempt).where(DeviceAddonTopupAttempt.platega_payment_id == payment.id)
    )
    if attempt is None:
        return False
    await db.execute(select(User).where(User.id == attempt.user_id).with_for_update())
    attempt = (
        await db.execute(
            select(DeviceAddonTopupAttempt)
            .where(DeviceAddonTopupAttempt.id == attempt.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    intent = (
        await db.execute(
            select(DeviceAddonIntent)
            .where(DeviceAddonIntent.id == attempt.intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if not _binding_is_exact(payment=payment, attempt=attempt, intent=intent):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='durable_payment_binding_mismatch',
        )
        await db.commit()
        return True
    expected_payload = f'platega:{payment.correlation_id}'
    callback_payload = str(payload.get('payload') or '')
    provider_id = _provider_transaction_id(payload)
    amount_currency = PlategaService.parse_amount_currency(payload)
    method_code = _provider_method_code(payload)
    exact_correlation = hmac.compare_digest(callback_payload, expected_payload)
    payment_details = payload.get('paymentDetails')
    if not isinstance(payment_details, dict):
        payment_details = {}
    amount_present = payment_details.get('amount') is not None or payload.get('amount') is not None
    method_present = payload.get('paymentMethod') is not None or payload.get('paymentMethodCode') is not None
    financial_mismatch = (amount_present and amount_currency != (int(attempt.requested_amount_kopeks), 'RUB')) or (
        method_present and method_code != int(attempt.provider_method_code)
    )
    if not exact_correlation or not provider_id:
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='callback_correlation_or_invoice_mismatch',
        )
        await db.commit()
        return True
    known_provider_ids = tuple(
        str(value) for value in (attempt.provider_payment_id, payment.platega_transaction_id) if value
    )
    if any(not hmac.compare_digest(known_id, provider_id) for known_id in known_provider_ids):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='callback_provider_identity_conflict',
        )
        await db.commit()
        return True
    # Exact signed correlation is enough to bind the remote identity, but not
    # enough to credit. Persist both mirrors before validating optional invoice
    # fields so every known remote invoice remains canonical-GET-only and can
    # never be closed as an unknown-ID attempt.
    attempt.provider_payment_id = provider_id
    payment.platega_transaction_id = provider_id
    if financial_mismatch:
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='callback_correlation_or_invoice_mismatch',
        )
        await db.commit()
        return True
    status = str(payload.get('status') or '').upper()
    if attempt.deposit_transaction_id is not None and status != 'CONFIRMED':
        # The owned deposit ledger is the financial source of truth. A late
        # provider regression needs review but must not erase the paid receipt
        # or make completed side effects eligible for replay.
        attempt.status = 'paid'
        attempt.holds_invoice_slot = False
        attempt.reconciliation_reason = f'post_paid_callback_status:{status.lower() or "unknown"}'
        payment.status = 'OPERATOR_REVIEW'
    elif attempt.deposit_transaction_id is None:
        if attempt.holds_invoice_slot:
            attempt.status = 'reconciling'
        attempt.reconciliation_reason = f'callback_awaiting_canonical:{status.lower() or "unknown"}'
        attempt.next_reconcile_at = datetime.now(UTC)
        payment.status = 'RECONCILING'
    # Never retain raw callback bodies or signatures for this flow.
    payment.callback_payload = None
    await db.commit()
    return True


async def _settle_locked(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    user: User,
    attempt: DeviceAddonTopupAttempt,
    intent: DeviceAddonIntent,
    payload: dict[str, Any],
) -> DeviceAddonTopupAttempt:
    amount_kopeks, currency = PlategaService.parse_amount_currency(payload) or (0, '')
    attempt.provider_returned_amount_kopeks = amount_kopeks or None
    attempt.provider_returned_currency = currency or None
    if not _exact_provider_invoice(attempt, payload):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='canonical_invoice_mismatch',
        )
        await db.commit()
        return attempt
    if attempt.deposit_transaction_id is not None:
        # Canonical replay only repairs receipt markers. It must preserve the
        # already committed referral/event progress and timestamps.
        attempt.status = 'paid'
        attempt.holds_invoice_slot = False
        payment.is_paid = True
        payment.status = 'CONFIRMED'
        payment.transaction_id = attempt.deposit_transaction_id
        await db.commit()
        return attempt
    if (
        int(user.device_addon_generation or 0) != int(intent.device_addon_generation)
        or getattr(user, 'account_erasure_requested_at', None) is not None
        or reset_is_busy(user)
    ):
        if getattr(user, 'account_erasure_requested_at', None) is not None:
            from app.services.account_erasure_service import invalidate_financial_resolution_for_late_payment

            await invalidate_financial_resolution_for_late_payment(db, user.id)
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='paid_after_account_lifecycle_change',
        )
        await db.commit()
        return attempt

    balance_before_kopeks = int(user.balance_kopeks or 0)
    was_first_topup = not bool(user.has_made_first_topup)
    external_id = str(attempt.provider_payment_id)
    transaction = await db.scalar(
        select(Transaction).where(
            Transaction.external_id == external_id,
            Transaction.payment_method == PaymentMethod.PLATEGA.value,
        )
    )
    ledger_key = f'device-addon-deposit:{attempt.id}'
    ledger_transaction = await db.scalar(select(Transaction).where(Transaction.device_first_ledger_key == ledger_key))
    if transaction is not None and ledger_transaction is None:
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='provider_transaction_without_owned_ledger',
        )
        await db.commit()
        return attempt
    if transaction is not None and transaction.id != getattr(ledger_transaction, 'id', None):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='provider_transaction_already_bound',
        )
        await db.commit()
        return attempt
    transaction = ledger_transaction or transaction
    if transaction is not None and (
        transaction.user_id != user.id
        or transaction.type != TransactionType.DEPOSIT.value
        or int(transaction.amount_kopeks) != int(attempt.requested_amount_kopeks)
    ):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='existing_deposit_mismatch',
        )
        await db.commit()
        return attempt
    if transaction is None:
        user.balance_kopeks = int(user.balance_kopeks or 0) + int(attempt.requested_amount_kopeks)
        transaction = Transaction(
            user_id=user.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=attempt.requested_amount_kopeks,
            description=f'Пополнение Platega для докупки устройств {intent.public_id}',
            payment_method=PaymentMethod.PLATEGA.value,
            external_id=external_id,
            device_first_ledger_key=ledger_key,
            is_completed=True,
            completed_at=datetime.now(UTC),
        )
        db.add(transaction)
        await db.flush()
    payment.transaction_id = transaction.id
    payment.is_paid = True
    payment.status = 'CONFIRMED'
    payment.paid_at = datetime.now(UTC)
    payment.callback_payload = None
    payment.metadata_json = {
        'settlement_mode': 'device_addon_topup_v1',
        'device_addon_attempt_id': attempt.id,
        'balance_credited': True,
        'balance_before_kopeks': balance_before_kopeks,
        'was_first_topup': was_first_topup,
    }
    attempt.deposit_transaction_id = transaction.id
    attempt.credited_amount_kopeks = attempt.requested_amount_kopeks
    attempt.status = 'paid'
    attempt.holds_invoice_slot = False
    attempt.paid_at = datetime.now(UTC)
    attempt.reconciliation_reason = 'wallet_credited_purchase_requires_confirmation'
    attempt.referral_enabled_at_credit = settings.is_referral_program_enabled()
    attempt.referral_status = 'pending' if attempt.referral_enabled_at_credit else 'done'
    attempt.event_status = 'pending'
    attempt.next_reconcile_at = datetime.now(UTC)
    attempt.lease_token = None
    attempt.lease_expires_at = None
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def _send_paid_effect_notifications(
    db: AsyncSession,
    *,
    bot: Any | None,
    user: User,
    transaction: Transaction,
    intent: DeviceAddonIntent,
    payment: PlategaPayment,
) -> None:
    """Send the retryable admin/client receipts for one credited top-up."""
    metadata = payment.metadata_json if isinstance(payment.metadata_json, dict) else {}
    user_id = int(user.id)
    telegram_id = user.telegram_id
    language = user.language
    amount_kopeks = int(transaction.amount_kopeks)
    intent_public_id = str(intent.public_id)
    subscription = await db.get(Subscription, intent.subscription_id) if intent.subscription_id is not None else None

    if bot is not None:
        try:
            from app.services.admin_notification_service import AdminNotificationService

            notification_service = AdminNotificationService(bot)
            referrer_info = await notification_service._get_referrer_info(db, user.referred_by_id)
            promo_group = await notification_service._get_user_promo_group(db, user)
            old_balance = int(
                metadata.get(
                    'balance_before_kopeks',
                    max(0, int(user.balance_kopeks or 0) - amount_kopeks),
                )
            )
            topup_status = '🆕 Первое пополнение' if metadata.get('was_first_topup') else '🔄 Пополнение'
            await notification_service.send_balance_topup_notification(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
                db=db,
            )
        except Exception as error:
            logger.error(
                'device_addon_topup_admin_notification_failed',
                attempt_id=metadata.get('device_addon_attempt_id'),
                user_id=user_id,
                error=error,
            )

    if not telegram_id:
        return
    if bot is None:
        raise RuntimeError('device add-on top-up customer notification requires bot')

    from app.utils.miniapp_buttons import build_cabinet_url

    return_url = build_cabinet_url(f'/subscription/device-topup/{intent_public_id}')
    if not return_url:
        raise RuntimeError('device add-on top-up customer return URL is unavailable')
    if language == 'en':
        text = (
            '✅ <b>Top-up successful!</b>\n\n'
            f'💰 Amount: {settings.format_price(amount_kopeks)}\n\n'
            'To add devices, return and confirm the purchase.'
        )
        button_text = 'Return to purchase'
    else:
        text = (
            '✅ <b>Пополнение успешно!</b>\n\n'
            f'💰 Сумма: {settings.format_price(amount_kopeks)}\n\n'
            'Чтобы добавить устройства, вернитесь и подтвердите покупку.'
        )
        button_text = 'Вернуться к покупке'
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=button_text, web_app=types.WebAppInfo(url=return_url))]]
    )
    await bot.send_message(
        telegram_id,
        text,
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def reconcile_device_addon_payment(
    db: AsyncSession,
    *,
    attempt_id: int,
    payload: dict[str, Any] | None,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> DeviceAddonTopupAttempt:
    """Apply one canonical provider observation under the payment-first lock order."""
    stub = await db.get(DeviceAddonTopupAttempt, attempt_id)
    if stub is None:
        raise DeviceAddonError('attempt_not_found', 'Платёж не найден.', status_code=404)
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == stub.platega_payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    user = (
        await db.execute(
            select(User).where(User.id == payment.user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    attempt_query = select(DeviceAddonTopupAttempt).where(DeviceAddonTopupAttempt.id == attempt_id)
    if lease_token is not None and lease_epoch is not None:
        attempt_query = attempt_query.where(
            DeviceAddonTopupAttempt.lease_token == lease_token,
            DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
            DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
        )
    attempt = (
        await db.execute(attempt_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if attempt is None:
        raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.', status_code=409)
    intent = (
        await db.execute(
            select(DeviceAddonIntent)
            .where(DeviceAddonIntent.id == attempt.intent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if not _binding_is_exact(payment=payment, attempt=attempt, intent=intent):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='durable_payment_binding_mismatch',
        )
        await db.commit()
        return attempt
    if not _provider_identity_is_exact(payment=payment, attempt=attempt):
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='durable_provider_identity_mismatch',
        )
        attempt.reconcile_attempts = int(attempt.reconcile_attempts or 0) + 1
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return attempt
    financially_settled = attempt.deposit_transaction_id is not None
    was_terminal = attempt.status == 'terminal'
    if not isinstance(payload, dict):
        if financially_settled:
            attempt.status = 'paid'
            attempt.holds_invoice_slot = False
            attempt.lease_token = None
            attempt.lease_expires_at = None
            await db.commit()
            return attempt
        if attempt.status not in {'terminal', 'operator_review'}:
            attempt.status = 'reconciling'
        attempt.reconciliation_reason = 'canonical_status_unavailable'
        if attempt.status == 'terminal':
            attempt.next_reconcile_at = datetime.now(UTC) + _TERMINAL_RECHECK_DELAY
        elif attempt.status == 'operator_review':
            attempt.next_reconcile_at = datetime.now(UTC) + _OPERATOR_RECHECK_DELAY
        else:
            attempt.next_reconcile_at = datetime.now(UTC) + timedelta(minutes=5)
        attempt.reconcile_attempts = int(attempt.reconcile_attempts or 0) + 1
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return attempt
    amount_currency = PlategaService.parse_amount_currency(payload)
    if amount_currency is not None:
        attempt.provider_returned_amount_kopeks, attempt.provider_returned_currency = amount_currency
    terminal_financial_mismatch = was_terminal and (
        amount_currency != (int(attempt.requested_amount_kopeks), 'RUB')
        or _provider_method_code(payload) != int(attempt.provider_method_code)
    )
    if not _exact_provider_invoice(attempt, payload):
        if financially_settled:
            attempt.status = 'paid'
            attempt.holds_invoice_slot = False
            attempt.reconciliation_reason = 'post_paid_canonical_invoice_mismatch'
            payment.status = 'OPERATOR_REVIEW'
        elif terminal_financial_mismatch:
            attempt.reconciliation_reason = 'terminal_recheck_canonical_invoice_mismatch'
            attempt.next_reconcile_at = datetime.now(UTC) + _TERMINAL_RECHECK_DELAY
        else:
            _mark_operator_review(
                payment=payment,
                attempt=attempt,
                intent=intent,
                reason='canonical_invoice_mismatch',
            )
        attempt.reconcile_attempts = int(attempt.reconcile_attempts or 0) + 1
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return attempt
    status = str(payload.get('status') or '').upper()
    if status == 'CONFIRMED':
        return await _settle_locked(db, payment=payment, user=user, attempt=attempt, intent=intent, payload=payload)
    if financially_settled:
        attempt.status = 'paid'
        attempt.holds_invoice_slot = False
        if status != 'CONFIRMED':
            attempt.reconciliation_reason = f'post_paid_provider_status:{status.lower() or "unknown"}'
            payment.status = 'OPERATOR_REVIEW'
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return attempt
    if status in _PROVIDER_TERMINAL:
        if attempt.status != 'terminal':
            attempt.reconcile_attempts = 0
        attempt.status = 'terminal'
        attempt.holds_invoice_slot = False
        attempt.reconciliation_reason = f'provider_terminal:{status.lower()}'
        attempt.next_reconcile_at = datetime.now(UTC) + _TERMINAL_RECHECK_DELAY
        payment.status = status
    elif status in _PROVIDER_LIVE:
        if attempt.status == 'terminal' or (attempt.status == 'operator_review' and not attempt.holds_invoice_slot):
            # A terminal invoice never reclaims the one-invoice slot.  A
            # contradictory later PENDING remains in the bounded hourly review
            # lane on every subsequent observation and cannot become a second
            # customer-facing payment URL.
            _mark_operator_review(
                payment=payment,
                attempt=attempt,
                intent=intent,
                reason='provider_terminal_status_regressed',
            )
        else:
            redirect = _safe_https_url(PlategaService.parse_redirect_url(payload))
            if redirect is not None:
                attempt.payment_url = redirect
                payment.redirect_url = redirect
            if attempt.payment_url is None:
                _mark_operator_review(
                    payment=payment,
                    attempt=attempt,
                    intent=intent,
                    reason='canonical_invoice_missing_safe_redirect',
                )
            else:
                attempt.status = 'pending'
                attempt.reconciliation_reason = None
                attempt.next_reconcile_at = datetime.now(UTC) + timedelta(minutes=2)
                payment.status = status
    elif status in _PROVIDER_REVERSAL:
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason='provider_chargeback_before_credit',
        )
    else:
        _mark_operator_review(
            payment=payment,
            attempt=attempt,
            intent=intent,
            reason=f'provider_unknown_status:{status.lower() or "empty"}',
        )
    attempt.reconcile_attempts = int(attempt.reconcile_attempts or 0) + 1
    attempt.lease_token = None
    attempt.lease_expires_at = None
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def _run_paid_effects(
    db: AsyncSession,
    *,
    attempt_id: int,
    lease_token: str,
    lease_epoch: int,
    bot: Any | None,
) -> None:
    """Run paid effects while account ownership/reset is stable across IO."""
    while True:
        attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
        if attempt is None or attempt.status != 'paid' or not attempt.deposit_transaction_id:
            return
        owner_id = int(attempt.user_id)
        await db.rollback()
        async with device_addon_account_activity_lock(db, owner_id):
            attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
            if attempt is None or attempt.status != 'paid' or not attempt.deposit_transaction_id:
                return
            if int(attempt.user_id) != owner_id:
                await db.rollback()
                continue
            await _run_paid_effects_under_account_activity(
                db,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_epoch=lease_epoch,
                bot=bot,
                allow_reset_busy=False,
            )
            return


async def _run_paid_effects_under_account_activity(
    db: AsyncSession,
    *,
    attempt_id: int,
    lease_token: str,
    lease_epoch: int,
    bot: Any | None,
    allow_reset_busy: bool,
) -> None:
    attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
    if attempt is None or attempt.status != 'paid' or not attempt.deposit_transaction_id:
        return
    current_owner = await db.get(User, attempt.user_id, populate_existing=True)
    if current_owner is None:
        raise RuntimeError('device add-on payment owner disappeared')
    if reset_is_busy(current_owner) and not allow_reset_busy:
        attempt = (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(
                    DeviceAddonTopupAttempt.id == attempt_id,
                    DeviceAddonTopupAttempt.lease_token == lease_token,
                    DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if attempt is not None:
            attempt.lease_token = None
            attempt.lease_expires_at = None
            await db.commit()
        return
    if attempt.referral_status in {'pending', 'processing'}:
        # The monetary helper locks Transaction -> Users.  Enter the same
        # Platega row that merge/reset lock first, then revalidate the claimed
        # attempt.  This serializes account ownership changes without making a
        # durable ``paid`` row a lifecycle blocker or changing P -> U -> A -> I
        # in provider settlement paths.
        payment_lock = (
            await db.execute(
                select(PlategaPayment)
                .where(PlategaPayment.id == attempt.platega_payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        attempt = (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(
                    DeviceAddonTopupAttempt.id == attempt_id,
                    DeviceAddonTopupAttempt.lease_token == lease_token,
                    DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                    DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if payment_lock is None or attempt is None:
            await db.rollback()
            raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
        owner = await db.get(User, payment_lock.user_id, populate_existing=True)
        if owner is None:
            await db.rollback()
            raise RuntimeError('device add-on payment owner disappeared')
        if reset_is_busy(owner):
            # Reset already won the payment-first fence.  Relinquish this
            # claim; cleanup may now remove the complete graph without a stale
            # worker recreating any state after its marker commit.
            attempt.lease_token = None
            attempt.lease_expires_at = None
            await db.commit()
            return
        try:
            await apply_deposit_referral_money(db, source_transaction_id=attempt.deposit_transaction_id)
        except ReferralRewardBalanceFencedError:
            await db.rollback()
            attempt = (
                await db.execute(
                    select(DeviceAddonTopupAttempt)
                    .where(
                        DeviceAddonTopupAttempt.id == attempt_id,
                        DeviceAddonTopupAttempt.lease_token == lease_token,
                        DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                        DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if attempt is None:
                raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
            attempt.referral_status = 'operator_review'
            attempt.reconciliation_reason = 'referral_reward_recipient_account_closed'
            attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=365)
            attempt.lease_token = None
            attempt.lease_expires_at = None
            from app.services.account_erasure_service import invalidate_financial_resolution_for_late_payment

            await invalidate_financial_resolution_for_late_payment(db, attempt.user_id)
            await db.commit()
            return
        attempt = (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(
                    DeviceAddonTopupAttempt.id == attempt_id,
                    DeviceAddonTopupAttempt.lease_token == lease_token,
                    DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                    DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if attempt is None:
            await db.rollback()
            raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
        attempt.referral_status = 'done'
        attempt.effects_attempts = int(attempt.effects_attempts or 0) + 1
        await db.commit()
    user = await db.get(User, attempt.user_id, populate_existing=True)
    if user is not None and getattr(user, 'account_erasure_requested_at', None) is not None:
        # The immutable deposit/referral ledgers are sufficient evidence for a
        # closing account. Deferred emission also runs promo-group assignment,
        # which must not recreate operational state after erasure begins.
        attempt = (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(
                    DeviceAddonTopupAttempt.id == attempt_id,
                    DeviceAddonTopupAttempt.lease_token == lease_token,
                    DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                    DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
        attempt.event_status = 'done'
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return
    if attempt.event_status == 'done':
        attempt.lease_token = None
        attempt.lease_expires_at = None
        await db.commit()
        return
    attempt = (
        await db.execute(
            select(DeviceAddonTopupAttempt)
            .where(
                DeviceAddonTopupAttempt.id == attempt_id,
                DeviceAddonTopupAttempt.lease_token == lease_token,
                DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
                DeviceAddonTopupAttempt.lease_expires_at >= datetime.now(UTC),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
    attempt.event_status = 'processing'
    attempt.effects_attempts = int(attempt.effects_attempts or 0) + 1
    await db.commit()

    transaction = await db.get(Transaction, attempt.deposit_transaction_id)
    if transaction is None:
        raise RuntimeError('device add-on deposit transaction disappeared')
    await emit_transaction_side_effects(
        db,
        transaction,
        amount_kopeks=transaction.amount_kopeks,
        user_id=transaction.user_id,
        type=TransactionType.DEPOSIT,
        payment_method=PaymentMethod.PLATEGA,
        external_id=transaction.external_id,
        description=transaction.description or '',
        raise_on_error=True,
    )
    user = await db.get(User, attempt.user_id, populate_existing=True)
    intent = await db.get(DeviceAddonIntent, attempt.intent_id, populate_existing=True)
    payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
    if user is None or intent is None or payment is None:
        raise RuntimeError('device add-on notification graph disappeared')
    await _send_paid_effect_notifications(
        db,
        bot=bot,
        user=user,
        transaction=transaction,
        intent=intent,
        payment=payment,
    )

    attempt = (
        await db.execute(
            select(DeviceAddonTopupAttempt)
            .where(
                DeviceAddonTopupAttempt.id == attempt_id,
                DeviceAddonTopupAttempt.lease_token == lease_token,
                DeviceAddonTopupAttempt.lease_epoch == lease_epoch,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise DeviceAddonError('recovery_lease_lost', 'Платёж уже проверяется.')
    attempt.event_status = 'done'
    attempt.lease_token = None
    attempt.lease_expires_at = None
    await db.commit()


async def drain_device_addon_paid_effects_for_reset(db: AsyncSession, *, user_id: int) -> bool:
    """Finish claimed/orphan paid effects before reset deletes their graph.

    The caller owns the account activity lock, so replacing a stale worker
    lease cannot race live external IO. Any failure leaves the reset marker
    untouched and the complete graph available for a retry.
    """
    own_bot = None
    try:
        while True:
            now = datetime.now(UTC)
            attempt = (
                await db.execute(
                    select(DeviceAddonTopupAttempt)
                    .where(
                        DeviceAddonTopupAttempt.user_id == user_id,
                        DeviceAddonTopupAttempt.status == 'paid',
                        DeviceAddonTopupAttempt.deposit_transaction_id.is_not(None),
                        DeviceAddonTopupAttempt.referral_status.in_(['pending', 'processing', 'done']),
                        or_(
                            DeviceAddonTopupAttempt.referral_status.in_(['pending', 'processing']),
                            DeviceAddonTopupAttempt.event_status != 'done',
                        ),
                    )
                    .order_by(DeviceAddonTopupAttempt.id)
                    .limit(1)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if attempt is None:
                return True
            attempt.lease_token = uuid.uuid4().hex
            attempt.lease_epoch = int(attempt.lease_epoch or 0) + 1
            attempt.lease_expires_at = now + timedelta(minutes=2)
            attempt.next_reconcile_at = now
            attempt_id = int(attempt.id)
            lease_token = str(attempt.lease_token)
            lease_epoch = int(attempt.lease_epoch)
            await db.commit()
            try:
                if own_bot is None:
                    from app.bot_factory import create_bot

                    own_bot = create_bot()
                await _run_paid_effects_under_account_activity(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                    bot=own_bot,
                    allow_reset_busy=True,
                )
            except Exception as error:
                await db.rollback()
                attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
                if attempt is not None and attempt.lease_token == lease_token and attempt.lease_epoch == lease_epoch:
                    attempt.reconciliation_reason = f'reset_paid_effect_drain_error:{type(error).__name__}'
                    attempt.next_reconcile_at = datetime.now(UTC) + timedelta(minutes=5)
                    attempt.lease_token = None
                    attempt.lease_expires_at = None
                await db.commit()
                logger.error('device_addon_reset_paid_effect_drain_failed', attempt_id=attempt_id, error=str(error))
                return False
    finally:
        if own_bot is not None:
            try:
                await own_bot.session.close()
            except Exception as error:
                logger.warning('device_addon_reset_drain_bot_close_failed', error=str(error))


async def check_device_addon_payment_now(
    db: AsyncSession,
    *,
    platega_payment_id: int,
) -> DeviceAddonTopupAttempt:
    """Run one canonical GET and reconcile through the ordinary locked path."""
    attempt = await db.scalar(
        select(DeviceAddonTopupAttempt).where(
            DeviceAddonTopupAttempt.platega_payment_id == platega_payment_id,
        )
    )
    if attempt is None:
        raise DeviceAddonError('attempt_not_found', 'Платёж докупки не найден.', status_code=404)
    payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
    if payment is None or not attempt.provider_payment_id or not payment.platega_transaction_id:
        raise DeviceAddonError(
            'provider_identity_unknown',
            'У платежа нет ID провайдера; его нельзя проверить автоматически.',
        )
    if not _provider_identity_is_exact(payment=payment, attempt=attempt):
        raise DeviceAddonError(
            'provider_identity_mismatch',
            'ID провайдера в платеже не совпадает; нужна проверка оператора.',
            status_code=409,
        )
    attempt_provider_id = str(attempt.provider_payment_id)
    service = PlategaService()
    service._max_retries = 1
    try:
        payload = await service.get_transaction(attempt_provider_id)
    except Exception:
        payload = None
    return await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)


async def close_device_addon_attempt_without_credit(
    db: AsyncSession,
    *,
    platega_payment_id: int,
) -> DeviceAddonTopupAttempt:
    """Release one no-ID operator hold without touching wallet or ledger."""
    stub = await db.scalar(
        select(DeviceAddonTopupAttempt).where(
            DeviceAddonTopupAttempt.platega_payment_id == platega_payment_id,
        )
    )
    if stub is None:
        raise DeviceAddonError('attempt_not_found', 'Платёж докупки не найден.', status_code=404)
    payment, _, attempt, intent = await _lock_payment_graph(
        db,
        payment_id=platega_payment_id,
        attempt_id=stub.id,
    )
    if not _binding_is_exact(payment=payment, attempt=attempt, intent=intent):
        raise DeviceAddonError('payment_binding_mismatch', 'Связь платежа повреждена.', status_code=409)
    if (
        attempt.status != 'operator_review'
        or attempt.provider_payment_id is not None
        or payment.platega_transaction_id is not None
        or attempt.deposit_transaction_id is not None
    ):
        raise DeviceAddonError(
            'attempt_cannot_be_closed',
            'Эту попытку нельзя закрыть без проверки.',
            status_code=409,
        )
    attempt.status = 'terminal'
    attempt.holds_invoice_slot = False
    attempt.reconciliation_reason = 'closed_by_operator'
    attempt.lease_token = None
    attempt.lease_expires_at = None
    payment.status = 'CLOSED_BY_OPERATOR'
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def recover_device_addon_payments(db: AsyncSession, *, limit: int = 25, bot: Any | None = None) -> int:
    """Reconcile due invoices and drain post-credit effects with bounded leases."""
    now = datetime.now(UTC)
    processed = 0
    service = PlategaService()
    service._max_retries = 1
    for _ in range(limit):
        now = datetime.now(UTC)
        row = (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(
                    or_(
                        and_(
                            DeviceAddonTopupAttempt.status.in_(
                                ['prepared', 'dispatching', 'creation_unknown', 'pending', 'reconciling']
                            ),
                            DeviceAddonTopupAttempt.next_reconcile_at <= now,
                        ),
                        and_(
                            DeviceAddonTopupAttempt.status == 'terminal',
                            DeviceAddonTopupAttempt.provider_payment_id.is_not(None),
                            DeviceAddonTopupAttempt.reconcile_attempts < _MAX_TERMINAL_RECONCILE_ATTEMPTS,
                            DeviceAddonTopupAttempt.next_reconcile_at <= now,
                        ),
                        and_(
                            DeviceAddonTopupAttempt.status == 'operator_review',
                            DeviceAddonTopupAttempt.provider_payment_id.is_not(None),
                            DeviceAddonTopupAttempt.reconcile_attempts < 24,
                            DeviceAddonTopupAttempt.next_reconcile_at <= now,
                        ),
                        and_(
                            DeviceAddonTopupAttempt.status == 'paid',
                            DeviceAddonTopupAttempt.referral_status.in_(['pending', 'processing', 'done']),
                            DeviceAddonTopupAttempt.next_reconcile_at <= now,
                            or_(
                                DeviceAddonTopupAttempt.referral_status.in_(['pending', 'processing']),
                                DeviceAddonTopupAttempt.event_status != 'done',
                            ),
                        ),
                    ),
                    or_(
                        DeviceAddonTopupAttempt.lease_expires_at.is_(None),
                        DeviceAddonTopupAttempt.lease_expires_at < now,
                    ),
                )
                .order_by(DeviceAddonTopupAttempt.next_reconcile_at, DeviceAddonTopupAttempt.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            break
        row.lease_token = uuid.uuid4().hex
        row.lease_epoch = int(row.lease_epoch or 0) + 1
        row.lease_expires_at = now + timedelta(minutes=2)
        attempt_id, lease_token, lease_epoch = row.id, row.lease_token, row.lease_epoch
        await db.commit()
        try:
            attempt = await db.get(DeviceAddonTopupAttempt, attempt_id, populate_existing=True)
            if attempt is None:
                continue
            if attempt.status == 'paid':
                await _run_paid_effects(
                    db,
                    attempt_id=attempt_id,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                    bot=bot,
                )
                processed += 1
                continue
            if attempt.status == 'prepared':
                attempt.status = 'terminal'
                attempt.holds_invoice_slot = False
                attempt.reconciliation_reason = 'local_not_dispatched'
                attempt.next_reconcile_at = now + _TERMINAL_RECHECK_DELAY
                attempt.lease_token = None
                attempt.lease_expires_at = None
                await db.commit()
                processed += 1
                continue
            if not attempt.provider_payment_id:
                payment = await db.get(PlategaPayment, attempt.platega_payment_id)
                intent = await db.get(DeviceAddonIntent, attempt.intent_id)
                if payment is None or intent is None:
                    raise RuntimeError('device add-on payment graph disappeared')
                _mark_operator_review(
                    payment=payment,
                    attempt=attempt,
                    intent=intent,
                    reason='provider_identity_unknown_no_retry',
                )
                attempt.lease_token = None
                attempt.lease_expires_at = None
                await db.commit()
                processed += 1
                continue
            payload = await service.get_transaction(attempt.provider_payment_id)
            await reconcile_device_addon_payment(
                db,
                attempt_id=attempt_id,
                payload=payload,
                lease_token=lease_token,
                lease_epoch=lease_epoch,
            )
            processed += 1
        except Exception as error:
            await db.rollback()
            attempt = await db.get(DeviceAddonTopupAttempt, attempt_id)
            if attempt is not None and attempt.lease_token == lease_token and attempt.lease_epoch == lease_epoch:
                attempt.reconciliation_reason = f'recovery_error:{type(error).__name__}'
                attempt.next_reconcile_at = datetime.now(UTC) + timedelta(minutes=5)
                attempt.lease_token = None
                attempt.lease_expires_at = None
                await db.commit()
            logger.error('device_addon_payment_recovery_failed', attempt_id=attempt_id, error=str(error))
    return processed


# Compatibility name used by startup/monitoring integration notes.
run_device_addon_payment_recovery = recover_device_addon_payments

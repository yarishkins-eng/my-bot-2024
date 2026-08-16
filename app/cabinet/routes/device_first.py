"""Authenticated API for the durable device-first checkout state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceFirstMutation,
    DeviceFirstOutbox,
    DeviceFirstProviderEvent,
    DeviceFirstReconciliationCredit,
    PlategaPayment,
    SubscriptionCheckout,
    Transaction,
    User,
)
from app.services.device_first_checkout_service import (
    DIRECT_SETTLEMENT_MODE,
    DeviceFirstError,
    arm_checkout,
    build_purchase_options,
    cancel_checkout,
    cancel_checkout_for_new_calculation,
    checkout_money_state,
    commit_direct_wallet_checkout,
    confirm_checkout,
    create_checkout,
    create_or_resume_direct_checkout,
    fulfill_checkout,
    get_open_checkout_for_user,
    get_owned_checkout,
    request_hash,
    serialize_checkout,
    store_mutation_result,
)
from app.services.device_first_payment_service import (
    abandon_direct_checkout_for_new_calculation,
    available_platega_methods_for_db,
    create_platega_attempt,
)
from app.utils.cache import RateLimitCache

from ..dependencies import (
    get_cabinet_db,
    get_current_cabinet_user,
    get_current_native_launch_user,
    require_permission,
)
from ..schemas.device_first import DirectCheckoutCommitRequest


router = APIRouter(prefix='/device-first', tags=['Cabinet Device First'])


class CheckoutCreateRequest(BaseModel):
    period_days: int = Field(..., gt=0)
    selected_device_limit: int = Field(..., gt=0)


class PaymentAttemptRequest(BaseModel):
    method_key: str = Field(..., min_length=1, max_length=32)


class CheckoutCommitRequest(BaseModel):
    """One explicit v2 funding choice; mixed funding is intentionally absent."""

    funding_mode: str = Field(..., pattern='^(wallet|platega)$')
    method_key: str | None = Field(None, min_length=1, max_length=32)


class NativeCheckoutLaunchRequest(BaseModel):
    """A provider method selected from a Telegram-native Mini App button."""

    method_key: str = Field(..., min_length=1, max_length=32)


class ReconciliationResolutionRequest(BaseModel):
    """An audited operator decision, never an implicit wallet credit."""

    resolution: str = Field(..., pattern='^(refund_requested|transfer_to_wallet|hold)$')


def _raise(error: DeviceFirstError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={'code': error.code, 'message': str(error)},
    ) from error


async def _mutation(
    db: AsyncSession,
    *,
    user_id: int,
    action: str,
    key: str | None,
    payload: Any,
) -> tuple[DeviceFirstMutation, dict[str, Any] | None]:
    if not key or not key.strip() or len(key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'idempotency_key_required', 'message': 'Valid Idempotency-Key header is required'},
        )
    digest = request_hash(payload)
    existing = (
        await db.execute(
            select(DeviceFirstMutation).where(
                DeviceFirstMutation.owner_user_id == user_id,
                DeviceFirstMutation.action == action,
                DeviceFirstMutation.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={'code': 'idempotency_conflict', 'message': 'Key was already used with another request'},
            )
        if existing.response_json is not None:
            if existing.status_code and existing.status_code >= 400:
                raise HTTPException(status_code=existing.status_code, detail=existing.response_json)
            return existing, existing.response_json
        # Re-enter the idempotent command. The previous process may have
        # committed the business operation before persisting its HTTP response.
        return existing, None
    mutation = DeviceFirstMutation(
        owner_user_id=user_id,
        action=action,
        idempotency_key=key,
        request_hash=digest,
    )
    db.add(mutation)
    try:
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status_code=409, detail={'code': 'concurrent_idempotency_key'}) from error
    await db.refresh(mutation)
    return mutation, None


async def _rate_limit(user_id: int, action: str, *, limit: int = 10) -> None:
    if await RateLimitCache.is_rate_limited(user_id, f'device_first_{action}', limit=limit, window=60):
        raise HTTPException(status_code=429, detail={'code': 'rate_limited', 'message': 'Too many requests'})


def _redact_mutation_response(response: dict[str, Any]) -> dict[str, Any]:
    """Keep redirect URLs out of generic idempotency JSON storage."""
    return {key: value for key, value in response.items() if key != 'redirect_url'}


def _is_live_direct_provider_invoice(
    checkout: SubscriptionCheckout,
    attempt: CheckoutPaymentAttempt,
    payment: PlategaPayment | None,
) -> bool:
    """Expose a direct redirect only while the exact invoice is still usable."""
    if (
        payment is None
        or not payment.redirect_url
        or payment.is_paid
        or attempt.status != 'pending'
        or str(payment.status).upper() not in {'PENDING', 'INPROGRESS'}
        or checkout.lifecycle_state != 'awaiting_funds'
        or checkout.funding_state != 'invoice_pending'
        or checkout.fulfillment_state != 'not_started'
    ):
        return False
    # The provider's canonical PENDING/INPROGRESS status is authoritative.
    # Some valid Platega responses omit ``expiresIn`` entirely; withholding a
    # verified one-invoice redirect in that case traps an ordinary customer in
    # support despite an exact ID/method/amount/currency match.  Absence of a
    # deadline never permits another provider POST: the durable attempt above
    # remains the unique payment path and the reconciliation worker continues
    # to verify it.
    return payment.expires_at is None or payment.expires_at > datetime.now(UTC)


async def _serialize_cabinet_checkout(
    db: AsyncSession,
    checkout: SubscriptionCheckout,
    *,
    balance_kopeks: int | None = None,
) -> dict[str, Any]:
    """Add the optional provider deadline without exposing payment internals.

    ``checkout.expires_at`` is the quote TTL until Platega supplies a canonical
    invoice deadline. Keeping the two concepts separate prevents the Mini App
    from falsely telling a customer that an invoice with an omitted provider
    TTL expires at the old quote deadline.
    """
    payload = serialize_checkout(checkout, balance_kopeks=balance_kopeks)
    if (
        getattr(checkout, 'settlement_mode', None) == DIRECT_SETTLEMENT_MODE
        and getattr(checkout, 'funding_state', None) == 'invoice_pending'
        and callable(getattr(db, 'scalar', None))
    ):
        invoice_expires_at = await db.scalar(
            select(PlategaPayment.expires_at)
            .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
            .where(CheckoutPaymentAttempt.checkout_id == checkout.id)
            .order_by(CheckoutPaymentAttempt.id.desc())
            .limit(1)
        )
        if invoice_expires_at is None or isinstance(invoice_expires_at, datetime):
            payload['provider_invoice_expires_at'] = (
                invoice_expires_at.isoformat() if invoice_expires_at is not None else None
            )
    # Экран разбора — единственное место, где мини-апп обязан говорить про деньги, и до
    # пункта 4.2б он утверждал списание всем подряд. Вердикт считает бэкенд по фактам:
    # ветка «по `terminal_reason`» на фронте при поздней оплате сказала бы «денег не было»,
    # когда деньги есть. Ключа может не быть (старый бэкенд) — фронт обязан это пережить.
    # 🔴 Без защитного `callable(getattr(db, 'scalar'))`: у настоящей сессии `scalar` есть
    # всегда, а такое условие молча выключило бы честный вердикт на любой будущей обёртке
    # сессии — и ни один тест бы не покраснел.
    if payload.get('ui_state') == 'operator_review':
        payload['money_state'] = await checkout_money_state(db, checkout)
    return payload


async def _rehydrate_owned_direct_redirect(
    db: AsyncSession,
    *,
    user: User,
    stored_response: dict[str, Any],
) -> dict[str, Any]:
    """Read a current provider URL only for the same owned direct checkout.

    Mutation storage contains a redacted checkout snapshot.  A replay may
    obtain a redirect only from the protected ``PlategaPayment`` record, never
    from generic JSON that could be retained in logs, backups or admin views.
    """
    response = _redact_mutation_response(stored_response)
    checkout_payload = response.get('checkout')
    checkout_id = checkout_payload.get('id') if isinstance(checkout_payload, dict) else None
    if not isinstance(checkout_id, str):
        return response
    checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
    if checkout.settlement_mode != DIRECT_SETTLEMENT_MODE:
        return response
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(
                CheckoutPaymentAttempt.checkout_id == checkout.id,
                CheckoutPaymentAttempt.status == 'pending',
                CheckoutPaymentAttempt.platega_payment_id.is_not(None),
            )
            .order_by(CheckoutPaymentAttempt.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        return response
    payment = await db.get(PlategaPayment, attempt.platega_payment_id)
    if not _is_live_direct_provider_invoice(checkout, attempt, payment):
        return response
    return {**response, 'redirect_url': payment.redirect_url}


@router.get('/purchase-options')
async def purchase_options(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await build_purchase_options(db, user)


@router.get('/payment-methods')
async def payment_methods(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return {'methods': await available_platega_methods_for_db(db, user)}


@router.post('/checkout')
async def checkout_create(
    request: CheckoutCreateRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    await _rate_limit(user.id, 'create', limit=5)
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action='create',
        key=idempotency_key,
        payload=request.model_dump(),
    )
    if replay is not None:
        return replay
    if mutation.checkout_id is not None:
        existing_checkout = await db.get(SubscriptionCheckout, mutation.checkout_id)
        if existing_checkout is not None:
            response = await _serialize_cabinet_checkout(db, existing_checkout, balance_kopeks=user.balance_kopeks)
            await store_mutation_result(db, mutation, response=response, status_code=201)
            return response
    try:
        # A new *different* selection is an explicit customer decision to
        # replace an old direct invoice. Keep the provider evidence, but
        # locally archive the old configuration under P->U->A->C so an exact
        # late payment becomes wallet credit rather than stale fulfilment.
        # Repeating the same selection resumes its existing invoice instead.
        existing = await get_open_checkout_for_user(db, user_id=user.id)
        if existing is not None and existing.settlement_mode == DIRECT_SETTLEMENT_MODE:
            configuration_changed = (
                existing.period_days != request.period_days
                or existing.selected_device_limit != request.selected_device_limit
            )
            if not configuration_changed:
                # A repeated choice must be a true resume, not an error that
                # makes the client wonder whether it can safely pay.  It is
                # also deliberately not a new provider invoice.
                response = await _serialize_cabinet_checkout(
                    db,
                    existing,
                    balance_kopeks=user.balance_kopeks,
                )
                mutation.checkout_id = existing.id
                await store_mutation_result(db, mutation, response=response)
                return response

            abandoned = await abandon_direct_checkout_for_new_calculation(
                db,
                checkout_public_id=existing.public_id,
                user_id=user.id,
            )
            if abandoned is None:
                # Before a provider method is chosen there is no immutable
                # financial attempt.  This is a disposable quote, so replace
                # it atomically instead of resuming the old configuration.
                locked_quote = await get_owned_checkout(
                    db,
                    public_id=existing.public_id,
                    user_id=user.id,
                    for_update=True,
                )
                await cancel_checkout_for_new_calculation(db, locked_quote)
            elif abandoned.lifecycle_state != 'cancelled':
                # A callback can win the financial lock while the customer is
                # changing parameters.  Return that canonical state instead
                # of attempting a second checkout/invoice.
                response = await _serialize_cabinet_checkout(
                    db,
                    abandoned,
                    balance_kopeks=user.balance_kopeks,
                )
                mutation.checkout_id = abandoned.id
                await store_mutation_result(db, mutation, response=response)
                return response
        checkout = await create_checkout(
            db,
            user=user,
            period_days=request.period_days,
            selected_device_limit=request.selected_device_limit,
            source='cabinet',
            mutation=mutation,
        )
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    response = await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)
    mutation.checkout_id = checkout.id
    await store_mutation_result(db, mutation, response=response, status_code=201)
    return response


@router.get('/checkout/open')
async def checkout_open(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    checkout = await get_open_checkout_for_user(db, user_id=user.id)
    if checkout is None:
        raise HTTPException(status_code=404, detail={'code': 'no_open_checkout'})
    return await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)


@router.get('/checkout/{checkout_id}')
async def checkout_get(
    checkout_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        return await _serialize_cabinet_checkout(
            db,
            await get_owned_checkout(db, public_id=checkout_id, user_id=user.id),
            balance_kopeks=user.balance_kopeks,
        )
    except DeviceFirstError as error:
        _raise(error)


async def _checkout_command(
    *,
    action: str,
    checkout_id: str,
    idempotency_key: str | None,
    user: User,
    db: AsyncSession,
) -> dict[str, Any]:
    await _rate_limit(user.id, action)
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action=action,
        key=idempotency_key,
        payload={'checkout_id': checkout_id},
    )
    if replay is not None:
        return replay
    if mutation.checkout_id is not None:
        recovered = await db.get(SubscriptionCheckout, mutation.checkout_id)
        if recovered is not None:
            if action == 'arm' and recovered.lifecycle_state in {'armed', 'fulfilling'}:
                recovered = await fulfill_checkout(db, recovered.public_id, user.id)
            response = await _serialize_cabinet_checkout(db, recovered, balance_kopeks=user.balance_kopeks)
            await store_mutation_result(db, mutation, response=response)
            return response
    try:
        checkout = await get_owned_checkout(
            db,
            public_id=checkout_id,
            user_id=user.id,
            for_update=True,
        )
        mutation.checkout_id = checkout.id
        if action == 'confirm':
            checkout = await confirm_checkout(db, checkout)
        elif action == 'arm':
            checkout = await arm_checkout(db, checkout)
        elif action == 'cancel':
            # ``cancel`` remains the inexpensive pre-invoice operation.  An
            # issued provider invoice has a separate, explicitly named
            # abandon endpoint below; treating it as an ordinary cancellation
            # would hide the different late-payment contract from callers.
            checkout = await cancel_checkout(db, checkout)
        else:  # pragma: no cover - internal programming guard
            raise RuntimeError(action)
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await db.refresh(user)
    response = await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)
    await store_mutation_result(db, mutation, response=response)
    return response


@router.post('/checkout/{checkout_id}/confirm')
async def checkout_confirm(
    checkout_id: str,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await _checkout_command(
        action='confirm',
        checkout_id=checkout_id,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
    )


@router.post('/checkout/{checkout_id}/arm')
async def checkout_arm(
    checkout_id: str,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await _checkout_command(
        action='arm',
        checkout_id=checkout_id,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
    )


async def _commit_checkout(
    *,
    checkout_id: str,
    request: CheckoutCommitRequest,
    idempotency_key: str | None,
    user: User,
    db: AsyncSession,
    action: str,
) -> dict[str, Any]:
    """Commit one v2 sale or create its exact full-price provider invoice.

    The redirect is deliberately returned only by this authenticated, owned
    mutation response.  Status reads and public values never expose it.
    """
    await _rate_limit(user.id, action, limit=5)
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action=action,
        key=idempotency_key,
        payload={'checkout_id': checkout_id, **request.model_dump()},
    )
    if replay is not None:
        return await _rehydrate_owned_direct_redirect(db, user=user, stored_response=replay)
    try:
        checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
        if checkout.settlement_mode != DIRECT_SETTLEMENT_MODE:
            raise DeviceFirstError('legacy_checkout', 'This checkout must use the legacy payment path')
        mutation.checkout_id = checkout.id
        if request.funding_mode == 'wallet':
            if request.method_key is not None:
                raise DeviceFirstError(
                    'invalid_funding_request', 'Wallet checkout has no provider method', status_code=422
                )
            checkout = await commit_direct_wallet_checkout(db, public_id=checkout_id, user_id=user.id)
            await db.refresh(user)
            response = {'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)}
        else:
            if not request.method_key:
                raise DeviceFirstError('payment_method_required', 'Select an available payment method', status_code=422)
            attempt = await create_platega_attempt(
                db,
                checkout_public_id=checkout_id,
                user_id=user.id,
                method_key=request.method_key,
            )
            payment = await db.get(PlategaPayment, attempt.platega_payment_id)
            checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
            if _is_live_direct_provider_invoice(checkout, attempt, payment):
                response = {
                    'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks),
                    'redirect_url': payment.redirect_url,
                }
            elif checkout.lifecycle_state in {'fulfilling', 'ready'}:
                # The canonical create-follow-up may have found an already
                # paid invoice and completed the same strict settlement path.
                # Return its owned checkout state, never a stale provider URL.
                response = {
                    'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)
                }
            else:
                raise DeviceFirstError('reconciliation_required', 'Provider invoice requires reconciliation')
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await store_mutation_result(db, mutation, response=_redact_mutation_response(response))
    return response


@router.post('/checkout/{checkout_id}/commit')
async def checkout_commit(
    checkout_id: str,
    request: CheckoutCommitRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Explicit Cabinet checkout action, including deliberate wallet payment."""
    return await _commit_checkout(
        checkout_id=checkout_id,
        request=request,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
        action='commit',
    )


@router.post('/checkout/{checkout_id}/native-launch')
async def checkout_native_launch(
    checkout_id: str,
    request: NativeCheckoutLaunchRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_native_launch_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Launch one provider invoice from an authenticated Telegram Mini App.

    Query parameters that opened the Mini App are not an authority.  This
    mutation requires a signed Telegram identity matching the Cabinet session,
    then delegates to the exact same owner-scoped direct-payment state machine
    as the normal Cabinet button.  It can never debit the wallet.
    """
    return await _commit_checkout(
        checkout_id=checkout_id,
        request=CheckoutCommitRequest(funding_mode='platega', method_key=request.method_key),
        idempotency_key=idempotency_key,
        user=user,
        db=db,
        action='native_launch',
    )


async def _fused_commit_checkout(
    *,
    request: DirectCheckoutCommitRequest,
    idempotency_key: str | None,
    user: User,
    db: AsyncSession,
    action: str,
) -> dict[str, Any]:
    """Resolve or birth one direct checkout at pay time, then fund it.

    No durable checkout exists before this mutation: the fused resolver either
    resumes the one open checkout, supersedes it through the canonical abandon
    path, or creates a fresh ``confirmed`` row — then the existing wallet or
    provider-invoice commit chains finish the request unchanged.
    """
    await _rate_limit(user.id, action, limit=5)
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action=action,
        key=idempotency_key,
        payload=request.model_dump(),
    )
    if replay is not None:
        return await _rehydrate_owned_direct_redirect(db, user=user, stored_response=replay)
    try:
        if request.funding_mode == 'wallet':
            if request.method_key is not None:
                raise DeviceFirstError(
                    'invalid_funding_request', 'Wallet checkout has no provider method', status_code=422
                )
        elif not request.method_key:
            raise DeviceFirstError('payment_method_required', 'Select an available payment method', status_code=422)
        resolved = await create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=request.period_days,
            selected_device_limit=request.selected_device_limit,
            expected_tariff_total_kopeks=request.expected_tariff_total_kopeks,
            funding_mode=request.funding_mode,
            method_key=request.method_key,
            source='cabinet',
            mutation=mutation,
        )
        checkout = resolved.checkout
        mutation.checkout_id = checkout.id
        if not resolved.proceed_to_payment:
            # A provider callback won the race while the customer was paying:
            # answer with that canonical checkout state, never a second order.
            response = {'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)}
        elif request.funding_mode == 'wallet':
            checkout = await commit_direct_wallet_checkout(db, public_id=checkout.public_id, user_id=user.id)
            await db.refresh(user)
            response = {'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)}
        else:
            attempt = await create_platega_attempt(
                db,
                checkout_public_id=checkout.public_id,
                user_id=user.id,
                method_key=request.method_key,
            )
            payment = await db.get(PlategaPayment, attempt.platega_payment_id)
            checkout = await get_owned_checkout(db, public_id=checkout.public_id, user_id=user.id)
            if _is_live_direct_provider_invoice(checkout, attempt, payment):
                response = {
                    'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks),
                    'redirect_url': payment.redirect_url,
                }
            elif checkout.lifecycle_state in {'fulfilling', 'ready'}:
                # The canonical create-follow-up may have found an already
                # paid invoice and completed the same strict settlement path.
                # Return its owned checkout state, never a stale provider URL.
                response = {
                    'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)
                }
            else:
                raise DeviceFirstError('reconciliation_required', 'Provider invoice requires reconciliation')
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await store_mutation_result(db, mutation, response=_redact_mutation_response(response))
    return response


@router.post('/commit')
async def direct_checkout_commit(
    request: DirectCheckoutCommitRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Fused pay-time mutation: the order is born only now, at «Pay»."""
    return await _fused_commit_checkout(
        request=request,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
        action='fused_commit',
    )


@router.post('/native-launch')
async def direct_native_launch(
    request: DirectCheckoutCommitRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_native_launch_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Fused provider launch from an authenticated Telegram Mini App.

    The signed Telegram identity is verified before any order exists, and —
    exactly like the deprecated per-checkout native launch — this endpoint can
    never debit the wallet.
    """
    if request.funding_mode != 'platega':
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                'code': 'invalid_funding_request',
                'message': 'A native launch can only start an external provider payment',
            },
        )
    return await _fused_commit_checkout(
        request=request,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
        action='fused_native_launch',
    )


@router.post('/checkout/{checkout_id}/resume-invoice')
async def checkout_resume_invoice(
    checkout_id: str,
    request: PaymentAttemptRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Owner-only recovery for a pre-attempt crash; never retries a known POST."""
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action='resume_invoice',
        key=idempotency_key,
        payload={'checkout_id': checkout_id, **request.model_dump()},
    )
    if replay is not None:
        return await _rehydrate_owned_direct_redirect(db, user=user, stored_response=replay)
    try:
        checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
        if (
            checkout.settlement_mode != DIRECT_SETTLEMENT_MODE
            or checkout.funding_mode != 'platega'
            or checkout.financial_committed_at is None
            or checkout.lifecycle_state != 'awaiting_funds'
            or checkout.funding_state != 'invoice_pending'
            or checkout.fulfillment_state != 'not_started'
            or checkout.created_subscription_id is not None
            or checkout.debit_transaction_id is not None
        ):
            raise DeviceFirstError('invoice_resume_unavailable', 'This checkout has no resumable direct invoice')
        known_attempt = (
            await db.execute(
                select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
            )
        ).scalar_one_or_none()
        if known_attempt is not None:
            raise DeviceFirstError('invoice_resume_unavailable', 'This checkout already has a payment attempt')
        mutation.checkout_id = checkout.id
        attempt = await create_platega_attempt(
            db,
            checkout_public_id=checkout_id,
            user_id=user.id,
            method_key=request.method_key,
            allow_direct_pre_attempt_recovery=True,
        )
        payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
        response = {
            'checkout': await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks),
            # A missing redirect means the existing durable attempt is
            # ambiguous/reconciling. Do not make a second provider POST.
            **(
                {'redirect_url': payment.redirect_url}
                if _is_live_direct_provider_invoice(checkout, attempt, payment)
                else {}
            ),
        }
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await store_mutation_result(db, mutation, response=_redact_mutation_response(response))
    return response


@router.get('/checkout/{checkout_id}/pending-payment')
async def checkout_pending_payment(
    checkout_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Owned recovery read for the still-open direct invoice only."""
    checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
    if checkout.settlement_mode != DIRECT_SETTLEMENT_MODE:
        raise HTTPException(status_code=404, detail={'code': 'pending_payment_not_found'})
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(
                CheckoutPaymentAttempt.checkout_id == checkout.id,
                CheckoutPaymentAttempt.status.in_(['creating', 'pending', 'reconciliation']),
            )
            .order_by(CheckoutPaymentAttempt.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        # This can only represent a crash from a version predating the atomic
        # intent commit. The owner may choose a method once to resume; unknown
        # provider POSTs are never retried.
        known_attempt = (
            await db.execute(
                select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id == checkout.id).limit(1)
            )
        ).scalar_one_or_none()
        resume_allowed = (
            known_attempt is None
            and checkout.funding_mode == 'platega'
            and checkout.financial_committed_at is not None
            and checkout.lifecycle_state == 'awaiting_funds'
            and checkout.funding_state == 'invoice_pending'
            and checkout.fulfillment_state == 'not_started'
            and checkout.created_subscription_id is None
            and checkout.debit_transaction_id is None
        )
        return {'redirect_url': None, 'status': 'missing', 'resume_allowed': resume_allowed}
    payment = await db.get(PlategaPayment, attempt.platega_payment_id) if attempt.platega_payment_id else None
    return {
        'redirect_url': payment.redirect_url if _is_live_direct_provider_invoice(checkout, attempt, payment) else None,
        'status': attempt.status,
        'resume_allowed': False,
    }


@router.post('/checkout/{checkout_id}/cancel')
async def checkout_cancel(
    checkout_id: str,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    return await _checkout_command(
        action='cancel',
        checkout_id=checkout_id,
        idempotency_key=idempotency_key,
        user=user,
        db=db,
    )


@router.post('/checkout/{checkout_id}/abandon')
async def checkout_abandon(
    checkout_id: str,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Explicitly supersede one already verified direct invoice.

    This is intentionally not a provider cancellation and not an alias for
    ``/cancel``.  The payment evidence stays available for canonical polling;
    a late exact confirmation can only become a one-time wallet credit.
    """
    await _rate_limit(user.id, 'abandon')
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action='abandon',
        key=idempotency_key,
        payload={'checkout_id': checkout_id},
    )
    if replay is not None:
        return replay
    if mutation.checkout_id is not None:
        recovered = await db.get(SubscriptionCheckout, mutation.checkout_id)
        if recovered is not None:
            response = await _serialize_cabinet_checkout(db, recovered, balance_kopeks=user.balance_kopeks)
            await store_mutation_result(db, mutation, response=response)
            return response
    try:
        checkout = await abandon_direct_checkout_for_new_calculation(
            db,
            checkout_public_id=checkout_id,
            user_id=user.id,
        )
        if checkout is None:
            raise DeviceFirstError('invalid_state', 'This checkout has no provider invoice to abandon')
        mutation.checkout_id = checkout.id
    except DeviceFirstError as error:
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await db.refresh(user)
    response = await _serialize_cabinet_checkout(db, checkout, balance_kopeks=user.balance_kopeks)
    await store_mutation_result(db, mutation, response=response)
    return response


@router.post('/checkout/{checkout_id}/payment-attempt')
async def checkout_payment_attempt(
    checkout_id: str,
    request: PaymentAttemptRequest,
    idempotency_key: str | None = Header(None, alias='Idempotency-Key'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    await _rate_limit(user.id, 'payment_attempt', limit=5)
    mutation, replay = await _mutation(
        db,
        user_id=user.id,
        action='payment_attempt',
        key=idempotency_key,
        payload={'checkout_id': checkout_id, **request.model_dump()},
    )
    if replay is not None:
        return replay
    if mutation.checkout_id is not None:
        existing_attempt = (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(CheckoutPaymentAttempt.checkout_id == mutation.checkout_id)
                .order_by(CheckoutPaymentAttempt.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing_attempt is not None and existing_attempt.redirect_url:
            response = {
                'attempt_id': existing_attempt.id,
                'status': existing_attempt.status,
                'method_key': existing_attempt.method_key,
                'amount_kopeks': existing_attempt.requested_amount_kopeks,
                'currency': existing_attempt.currency,
                'redirect_url': existing_attempt.redirect_url,
            }
            await store_mutation_result(db, mutation, response=response, status_code=201)
            return response
    owned_checkout = await get_owned_checkout(db, public_id=checkout_id, user_id=user.id)
    mutation.checkout_id = owned_checkout.id
    if owned_checkout.settlement_mode == DIRECT_SETTLEMENT_MODE:
        error = DeviceFirstError(
            'direct_commit_required',
            'Direct checkout payment can only be created by the final commit command',
        )
        await store_mutation_result(
            db,
            mutation,
            response={'code': error.code, 'message': str(error)},
            status_code=error.status_code,
        )
        _raise(error)
    await db.commit()
    try:
        attempt = await create_platega_attempt(
            db,
            checkout_public_id=checkout_id,
            user_id=user.id,
            method_key=request.method_key,
        )
    except DeviceFirstError as error:
        _raise(error)
    mutation.checkout_id = attempt.checkout_id
    response = {
        'attempt_id': attempt.id,
        'status': attempt.status,
        'method_key': attempt.method_key,
        'amount_kopeks': attempt.requested_amount_kopeks,
        'currency': attempt.currency,
        'redirect_url': attempt.redirect_url,
    }
    await store_mutation_result(db, mutation, response=response, status_code=201)
    return response


@router.get('/admin/checkouts/{checkout_id}')
async def admin_checkout_read_only(
    checkout_id: str,
    admin: User = Depends(require_permission('payments:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Support evidence view. Deliberately read-only in v1."""
    checkout = (
        await db.execute(select(SubscriptionCheckout).where(SubscriptionCheckout.public_id == checkout_id))
    ).scalar_one_or_none()
    if checkout is None:
        raise HTTPException(status_code=404, detail='Checkout not found')
    attempts = list(
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .where(CheckoutPaymentAttempt.checkout_id == checkout.id)
                .order_by(CheckoutPaymentAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    ledger = list(
        (
            await db.execute(
                select(Transaction).where(Transaction.device_first_checkout_id == checkout.id).order_by(Transaction.id)
            )
        )
        .scalars()
        .all()
    )
    outbox = (
        await db.execute(select(DeviceFirstOutbox).where(DeviceFirstOutbox.checkout_id == checkout.id))
    ).scalar_one_or_none()
    reconciliation_credits = list(
        (
            await db.execute(
                select(DeviceFirstReconciliationCredit)
                .where(DeviceFirstReconciliationCredit.checkout_id == checkout.id)
                .order_by(DeviceFirstReconciliationCredit.id)
            )
        )
        .scalars()
        .all()
    )
    provider_events = list(
        (
            await db.execute(
                select(DeviceFirstProviderEvent)
                .where(DeviceFirstProviderEvent.checkout_id == checkout.id)
                .order_by(DeviceFirstProviderEvent.observed_at, DeviceFirstProviderEvent.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        'checkout': serialize_checkout(checkout),
        'user_id': checkout.user_id,
        'attempts': [
            {
                'id': attempt.id,
                'provider': attempt.provider,
                'method_key': attempt.method_key,
                'requested_amount_kopeks': attempt.requested_amount_kopeks,
                'credited_amount_kopeks': attempt.credited_amount_kopeks,
                'status': attempt.status,
                'provider_payment_id': attempt.provider_payment_id,
                'reconciliation_reason': attempt.reconciliation_reason,
            }
            for attempt in attempts
        ],
        'ledger': [
            {
                'id': item.id,
                'type': item.type,
                'amount_kopeks': item.amount_kopeks,
                'payment_method': item.payment_method,
                'is_completed': item.is_completed,
                'ledger_key': item.device_first_ledger_key,
            }
            for item in ledger
        ],
        'provisioning': (
            {
                'status': outbox.status,
                'attempts': outbox.attempts,
                'last_error': outbox.last_error,
            }
            if outbox
            else None
        ),
        'reconciliation_credits': [
            {
                'id': item.id,
                'attempt_id': item.attempt_id,
                'provider_payment_id': item.provider_payment_id,
                'amount_kopeks': item.amount_kopeks,
                'currency': item.currency,
                'status': item.status,
                'resolution': item.resolution,
                'resolved_by_user_id': item.resolved_by_user_id,
                'resolved_at': item.resolved_at,
            }
            for item in reconciliation_credits
        ],
        # Evidence is deliberately normalized/redacted: no payment URL,
        # callback signature or raw provider payload is exposed here.
        'provider_events': [
            {
                'id': event.id,
                'attempt_id': event.attempt_id,
                'provider_payment_id': event.provider_payment_id,
                'provider_status': event.provider_status,
                'source': event.source,
                'amount_kopeks': event.amount_kopeks,
                'currency': event.currency,
                'method_key': event.method_key,
                'payload_hash': event.payload_hash,
                'authenticated': event.authenticated,
                'observed_at': event.observed_at,
            }
            for event in provider_events
        ],
    }


@router.post('/admin/reconciliation-credits/{credit_id}/resolve')
async def admin_resolve_reconciliation_credit(
    credit_id: int,
    request: ReconciliationResolutionRequest,
    admin: User = Depends(require_permission('payments:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Record an accountable resolution; never mutate wallet automatically."""
    credit = (
        await db.execute(
            select(DeviceFirstReconciliationCredit)
            .where(DeviceFirstReconciliationCredit.id == credit_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credit is None:
        raise HTTPException(status_code=404, detail='Reconciliation credit not found')
    if credit.status != 'operator_review':
        raise HTTPException(status_code=409, detail={'code': 'already_resolved'})
    # ``transfer_to_wallet`` is a request for the separately authorised support
    # process; this endpoint stores no balance mutation and no provider action.
    from datetime import UTC, datetime

    credit.status = 'resolved'
    credit.resolution = request.resolution
    credit.resolved_by_user_id = admin.id
    credit.resolved_at = datetime.now(UTC)
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='device_first_reconciliation_resolution',
        resource_type='device_first_reconciliation_credit',
        resource_id=str(credit.id),
        details={
            'checkout_id': credit.checkout_id,
            'attempt_id': credit.attempt_id,
            'resolution': request.resolution,
            'amount_kopeks': credit.amount_kopeks,
        },
        status='success',
    )
    await db.commit()
    return {'id': credit.id, 'status': credit.status, 'resolution': credit.resolution}

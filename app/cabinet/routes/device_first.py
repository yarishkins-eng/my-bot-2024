"""Authenticated API for the durable device-first checkout state machine."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceFirstMutation,
    DeviceFirstOutbox,
    SubscriptionCheckout,
    Transaction,
    User,
)
from app.services.device_first_checkout_service import (
    DeviceFirstError,
    arm_checkout,
    build_purchase_options,
    cancel_checkout,
    confirm_checkout,
    create_checkout,
    fulfill_checkout,
    get_open_checkout_for_user,
    get_owned_checkout,
    request_hash,
    serialize_checkout,
    store_mutation_result,
)
from app.services.device_first_payment_service import (
    available_platega_methods_for_db,
    create_platega_attempt,
)
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user, require_permission


router = APIRouter(prefix='/device-first', tags=['Cabinet Device First'])


class CheckoutCreateRequest(BaseModel):
    period_days: int = Field(..., gt=0)
    selected_device_limit: int = Field(..., gt=0)


class PaymentAttemptRequest(BaseModel):
    method_key: str = Field(..., min_length=1, max_length=32)


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
            response = serialize_checkout(existing_checkout, balance_kopeks=user.balance_kopeks)
            await store_mutation_result(db, mutation, response=response, status_code=201)
            return response
    try:
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
    response = serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)
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
    return serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)


@router.get('/checkout/{checkout_id}')
async def checkout_get(
    checkout_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        return serialize_checkout(
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
            response = serialize_checkout(recovered, balance_kopeks=user.balance_kopeks)
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
    response = serialize_checkout(checkout, balance_kopeks=user.balance_kopeks)
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
    }

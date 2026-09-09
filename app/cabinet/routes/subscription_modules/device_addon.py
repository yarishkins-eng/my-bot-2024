"""Protected cabinet API for manual device add-ons."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.schemas.device_addon import (
    DeviceAddonIntentCreateRequest,
    DeviceAddonPurchaseRequest,
    DeviceAddonTopupRequest,
)
from app.database.models import DeviceAddonIntent, DeviceAddonTopupAttempt, User
from app.services.device_addon_service import (
    DeviceAddonError,
    calculate_device_addon,
    create_intent,
    get_owned_intent,
    purchase_intent,
    quote_for_calculation,
    serialize_intent,
    serialize_topup_attempt,
)


router = APIRouter(prefix='/devices')


def _raise(error: DeviceAddonError) -> None:
    detail = {'code': error.code, 'message': str(error)}
    if error.quote is not None:
        detail['quote'] = error.quote
    raise HTTPException(status_code=error.status_code, detail=detail) from error


def _pending_payment_url(attempt: DeviceAddonTopupAttempt) -> str | None:
    if attempt.status != 'pending' or not attempt.holds_invoice_slot or not attempt.payment_url:
        return None
    try:
        parsed = urlsplit(attempt.payment_url)
    except ValueError:
        return None
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return None
    return attempt.payment_url


def _with_public_intent_id(payload: dict, intent_id: str) -> dict:
    result = dict(payload)
    result['intent_id'] = intent_id
    return result


async def _serialize(db: AsyncSession, *, intent, user: User) -> dict:
    payload = await serialize_intent(db, intent=intent, user=user)
    payload['topup_attempts'] = [
        _with_public_intent_id(attempt, intent.public_id) for attempt in payload.get('topup_attempts', [])
    ]
    return payload


@router.get('/quote')
async def get_device_addon_quote(
    subscription_id: int | None = Query(None),
    devices: int = Query(..., ge=1, le=100),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        calculation = await calculate_device_addon(
            db, user=user, subscription_id=subscription_id, devices_to_add=devices
        )
        return quote_for_calculation(calculation, user_id=user.id)
    except DeviceAddonError as error:
        _raise(error)


@router.post('/intents', status_code=status.HTTP_201_CREATED)
async def create_device_addon_intent(
    request: DeviceAddonIntentCreateRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        intent = await create_intent(
            db, user=user, quote_token=request.quote_token, idempotency_key=request.idempotency_key
        )
        return await _serialize(db, intent=intent, user=user)
    except DeviceAddonError as error:
        _raise(error)


@router.get('/intents/{intent_id}')
async def get_device_addon_intent(
    intent_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        return await _serialize(db, intent=await get_owned_intent(db, user_id=user.id, public_id=intent_id), user=user)
    except DeviceAddonError as error:
        _raise(error)


@router.post('/intents/{intent_id}/purchase')
async def purchase_device_addon_intent(
    intent_id: str,
    request: DeviceAddonPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    try:
        intent = await purchase_intent(db, user=user, public_id=intent_id, quote_token=request.quote_token)
        return await _serialize(db, intent=intent, user=user)
    except DeviceAddonError as error:
        _raise(error)


@router.post('/intents/{intent_id}/topup', status_code=status.HTTP_201_CREATED)
async def create_device_addon_topup(
    intent_id: str,
    request: DeviceAddonTopupRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create or replay exactly one protected Platega invoice for this intent."""
    try:
        from app.services.device_addon_payment_service import create_device_addon_topup as create_payment

        attempt = await create_payment(
            db,
            intent_public_id=intent_id,
            user_id=user.id,
            idempotency_key=request.idempotency_key,
            request_hash=hashlib.sha256(
                json.dumps(
                    {
                        'idempotency_key': request.idempotency_key,
                        'payment_method': request.payment_method,
                        'payment_option': request.payment_option,
                        'expected_amount_kopeks': request.expected_amount_kopeks,
                        'return_surface': request.return_surface,
                    },
                    sort_keys=True,
                    separators=(',', ':'),
                ).encode('utf-8')
            ).hexdigest(),
            method_key=request.payment_option,
            expected_amount_kopeks=request.expected_amount_kopeks,
            return_url=None,
            failed_url=None,
            return_surface=request.return_surface,
        )
        return {
            'attempt': _with_public_intent_id(
                serialize_topup_attempt(attempt, can_create_new_attempt=False), intent_id
            ),
            'payment_url': _pending_payment_url(attempt),
            'return_start_param': f'dtu-{attempt.public_id}',
        }
    except DeviceAddonError as error:
        _raise(error)


@router.get('/topups/{attempt_id}')
async def get_device_addon_topup(
    attempt_id: str,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    attempt = (
        await db.execute(
            select(DeviceAddonTopupAttempt).where(
                DeviceAddonTopupAttempt.public_id == attempt_id,
                DeviceAddonTopupAttempt.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status_code=404, detail={'code': 'topup_not_found', 'message': 'Счёт не найден.'})
    intent = (
        await db.execute(
            select(DeviceAddonIntent).where(
                DeviceAddonIntent.id == attempt.intent_id, DeviceAddonIntent.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if intent is None:  # Defensive: the RESTRICT FK normally makes this impossible.
        raise HTTPException(status_code=404, detail={'code': 'intent_not_found', 'message': 'Операция не найдена.'})
    serialized_intent = await _serialize(db, intent=intent, user=user)
    serialized_attempt = next(
        candidate for candidate in serialized_intent['topup_attempts'] if candidate['id'] == attempt.public_id
    )
    return {
        'attempt': serialized_attempt,
        'intent': serialized_intent,
        'payment_url': _pending_payment_url(attempt),
        'return_start_param': f'dtu-{attempt.public_id}',
    }

"""Durable, manually confirmed device add-on pricing and wallet purchase.

Provider invoices and their settlement live in ``device_addon_payment_service``.
The wallet mutation stays independent of provider, Redis and Panel IO; its
notifications and transaction events run only after the atomic commit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.models import (
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PaymentMethod,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
)


QUOTE_TTL = timedelta(minutes=15)
CALCULATOR_REVISION = 'v1'
logger = structlog.get_logger(__name__)

# Account merge and test reset must make the same lifecycle decision.  Keep
# this positive list next to the add-on state machine: a newly introduced
# attempt state is allowed until it is deliberately classified here, while an
# entitlement already being issued remains protected by the intent predicate.
ACCOUNT_CHANGE_BLOCKING_ATTEMPT_STATES = frozenset(
    {'prepared', 'dispatching', 'creation_unknown', 'pending', 'reconciling'}
)


def device_addon_attempt_blocks_account_change(attempt: DeviceAddonTopupAttempt) -> bool:
    """Whether merge/reset could detach an invoice that is still in flight."""
    return attempt.status in ACCOUNT_CHANGE_BLOCKING_ATTEMPT_STATES or (
        attempt.status == 'operator_review' and bool(attempt.provider_payment_id)
    )


def device_addon_intent_blocks_account_change(intent: DeviceAddonIntent) -> bool:
    """Whether merge/reset could lose a purchased entitlement still being issued."""
    return intent.purchase_state == 'purchased' and intent.fulfillment_state == 'pending'


class DeviceAddonError(Exception):
    """A user-safe, structured rejection for cabinet routes."""

    def __init__(self, code: str, message: str, *, status_code: int = 409, quote: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.quote = quote


@dataclass(frozen=True, slots=True)
class DeviceAddonCalculation:
    subscription_id: int
    devices_to_add: int
    original_device_limit: int
    new_device_limit: int
    max_device_limit: int | None
    included_free_devices: int
    chargeable_devices: int
    monthly_price_kopeks: int
    base_price_kopeks: int
    discount_percent: int
    price_kopeks: int
    balance_kopeks: int
    missing_kopeks: int
    days_left: int
    end_date: datetime
    tariff_id: int | None
    panel_uuid: str | None
    generation: int


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')


def _quote_key() -> bytes:
    secret = settings.get_cabinet_jwt_secret()
    if not secret:
        raise DeviceAddonError('quote_unavailable', 'Не удалось подготовить подтверждение цены.', status_code=503)
    return hmac.new(secret.encode('utf-8'), b'teplo/device-addon-quote/v1', hashlib.sha256).digest()


def _encode_quote(payload: dict[str, Any]) -> str:
    raw = _canonical_json(payload)
    signature = hmac.new(_quote_key(), raw, hashlib.sha256).digest()
    return f'{base64.urlsafe_b64encode(raw).rstrip(b"=").decode()}.{base64.urlsafe_b64encode(signature).rstrip(b"=").decode()}'


def _decode_part(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def decode_quote(token: str) -> dict[str, Any]:
    try:
        encoded_payload, encoded_signature = token.split('.', 1)
        raw = _decode_part(encoded_payload)
        signature = _decode_part(encoded_signature)
        expected = hmac.new(_quote_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('signature')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('v') != 1:
            raise ValueError('payload')
        if int(payload['exp']) < int(datetime.now(UTC).timestamp()):
            raise ValueError('expired')
        return payload
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeviceAddonError('quote_invalid', 'Подтвердите цену заново.', status_code=409) from error


def _quote_payload(calculation: DeviceAddonCalculation, *, user_id: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        'v': 1,
        'iat': int(now.timestamp()),
        'exp': int((now + QUOTE_TTL).timestamp()),
        'user_id': user_id,
        'subscription_id': calculation.subscription_id,
        'devices_to_add': calculation.devices_to_add,
        'original_device_limit': calculation.original_device_limit,
        'new_device_limit': calculation.new_device_limit,
        'tariff_id': calculation.tariff_id,
        'end_date': calculation.end_date.isoformat(),
        'panel_uuid': calculation.panel_uuid,
        'generation': calculation.generation,
        'calculator_revision': CALCULATOR_REVISION,
        'monthly_price_kopeks': calculation.monthly_price_kopeks,
        'included_free_devices': calculation.included_free_devices,
        'max_device_limit': calculation.max_device_limit,
        'chargeable_devices': calculation.chargeable_devices,
        'days_left': calculation.days_left,
        'base_price_kopeks': calculation.base_price_kopeks,
        'discount_percent': calculation.discount_percent,
        'price_kopeks': calculation.price_kopeks,
    }


def _hash_request(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _quote_response(calculation: DeviceAddonCalculation, *, user_id: int) -> dict[str, Any]:
    payload = _quote_payload(calculation, user_id=user_id)
    return {
        **{
            key: value
            for key, value in payload.items()
            if key not in {'v', 'iat', 'exp', 'user_id', 'generation', 'calculator_revision'}
        },
        'balance_kopeks': calculation.balance_kopeks,
        'missing_kopeks': calculation.missing_kopeks,
        'purchase_enabled': bool(settings.DEVICE_ADDON_PURCHASE_ENABLED),
        'quote_token': _encode_quote(payload),
        'quote_expires_at': datetime.fromtimestamp(payload['exp'], UTC).isoformat(),
    }


def _assert_purchase_enabled() -> None:
    if not settings.DEVICE_ADDON_PURCHASE_ENABLED:
        raise DeviceAddonError(
            'device_addon_purchase_disabled', 'Покупка устройств временно недоступна.', status_code=503
        )


async def _locked_user(db: AsyncSession, user_id: int) -> User:
    user = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise DeviceAddonError('not_found', 'Пользователь не найден.', status_code=404)
    return user


async def _locked_subscription(db: AsyncSession, *, user_id: int, subscription_id: int) -> Subscription:
    subscription = (
        await db.execute(
            select(Subscription)
            .where(and_(Subscription.id == subscription_id, Subscription.user_id == user_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if subscription is None:
        raise DeviceAddonError('subscription_not_found', 'Подписка не найдена.', status_code=404)
    return subscription


async def _find_subscription(db: AsyncSession, *, user: User, subscription_id: int | None, lock: bool) -> Subscription:
    if subscription_id is not None:
        query = select(Subscription).where(Subscription.id == subscription_id, Subscription.user_id == user.id)
    else:
        query = (
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    subscription = (await db.execute(query)).scalar_one_or_none()
    if subscription is None:
        raise DeviceAddonError('subscription_not_found', 'Подписка не найдена.', status_code=404)
    return subscription


async def calculate_device_addon(
    db: AsyncSession,
    *,
    user: User,
    subscription_id: int | None,
    devices_to_add: int,
    lock: bool = False,
) -> DeviceAddonCalculation:
    # Keep this service independent of the subscription-router package.  That
    # package registers this API router during import, so an eager import of
    # its helpers would create a startup-only circular dependency.
    from app.cabinet.routes.subscription_modules.helpers import _apply_addon_discount, _resolve_device_addon_price

    if devices_to_add <= 0 or devices_to_add > 100:
        raise DeviceAddonError('invalid_devices', 'Укажите допустимое число устройств.', status_code=422)
    if getattr(user, 'status', None) != 'active':
        raise DeviceAddonError('user_inactive', 'Покупка устройств недоступна для этого аккаунта.', status_code=403)
    if getattr(user, 'restriction_subscription', False):
        raise DeviceAddonError(
            'subscription_restricted', 'Покупка устройств недоступна для этого аккаунта.', status_code=403
        )
    if getattr(user, 'account_erasure_requested_at', None) is not None:
        raise DeviceAddonError(
            'account_erasure_pending', 'Аккаунт закрывается после финансовой сверки.', status_code=409
        )
    from app.services.account_test_reset_service import reset_is_busy

    if reset_is_busy(user):
        raise DeviceAddonError('test_account_reset', 'Тестовый аккаунт сейчас сбрасывается.', status_code=409)

    subscription = await _find_subscription(db, user=user, subscription_id=subscription_id, lock=lock)
    if subscription.status not in {'active', 'trial'}:
        raise DeviceAddonError('subscription_inactive', 'Подписка неактивна.', status_code=409)
    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id else None
    if tariff is not None and lock:
        tariff = await db.get(Tariff, tariff.id, with_for_update=True, populate_existing=True)
    from app.services.public_access_point_service import AccessPointPolicyError, assert_no_manual_access_point_grant

    try:
        await assert_no_manual_access_point_grant(db, subscription, action='device add-on')
    except AccessPointPolicyError as error:
        raise DeviceAddonError('access_point_addon_unsupported', str(error), status_code=409) from error

    monthly_price, max_device_limit = _resolve_device_addon_price(tariff)
    if not monthly_price or monthly_price <= 0:
        raise DeviceAddonError('device_addon_unavailable', 'Докупка устройств недоступна.', status_code=409)
    current = subscription.device_limit or 1
    new_limit = current + devices_to_add
    if max_device_limit is not None and new_limit > max_device_limit:
        raise DeviceAddonError(
            'max_device_limit', f'Максимальное количество устройств: {max_device_limit}', status_code=409
        )
    included = tariff.device_limit if tariff is not None else settings.DEFAULT_DEVICE_LIMIT
    included = max(0, int(included or 0))
    free = max(0, included - current)
    chargeable = max(0, devices_to_add - free)
    end_date = subscription.end_date if subscription.end_date.tzinfo else subscription.end_date.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if end_date <= now:
        raise DeviceAddonError('subscription_expired', 'Срок подписки закончился.', status_code=409)
    days_left = max(1, math.ceil((end_date - now).total_seconds() / 86400))
    base = int(monthly_price * chargeable * days_left / 30)
    if chargeable:
        base = max(100, base)
    discount = _apply_addon_discount(user, 'devices', base, days_left)
    price = int(discount['discounted'])
    if discount['percent'] < 100 and price > 0:
        price = max(100, price)
    panel_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
    if not panel_uuid:
        # A quote is also the durable binding to the exact Panel subject that
        # will receive the narrow HWID PATCH.  Selling without it would debit
        # the wallet and leave the worker no safe target to fulfill.
        raise DeviceAddonError(
            'panel_identity_unavailable',
            'Устройства пока нельзя добавить: подключение подписки обновляется.',
            status_code=409,
        )
    balance = int(user.balance_kopeks or 0)
    return DeviceAddonCalculation(
        subscription_id=subscription.id,
        devices_to_add=devices_to_add,
        original_device_limit=current,
        new_device_limit=new_limit,
        max_device_limit=max_device_limit,
        included_free_devices=free,
        chargeable_devices=chargeable,
        monthly_price_kopeks=int(monthly_price),
        base_price_kopeks=base,
        discount_percent=int(discount['percent']),
        price_kopeks=price,
        balance_kopeks=balance,
        missing_kopeks=max(0, price - balance),
        days_left=days_left,
        end_date=end_date,
        tariff_id=subscription.tariff_id,
        panel_uuid=panel_uuid,
        generation=int(user.device_addon_generation or 0),
    )


def quote_for_calculation(calculation: DeviceAddonCalculation, *, user_id: int) -> dict[str, Any]:
    return _quote_response(calculation, user_id=user_id)


def _matches_quote(calculation: DeviceAddonCalculation, payload: dict[str, Any], *, user_id: int) -> bool:
    expected = _quote_payload(calculation, user_id=user_id)
    return all(payload.get(key) == value for key, value in expected.items() if key not in {'iat', 'exp'})


async def create_intent(db: AsyncSession, *, user: User, quote_token: str, idempotency_key: str) -> DeviceAddonIntent:
    if not idempotency_key or len(idempotency_key) > 128:
        raise DeviceAddonError('idempotency_key_required', 'Нужен корректный ключ повтора.', status_code=422)
    owner_id = int(user.id)
    request_hash = _hash_request({'quote_token': quote_token})
    # Replay is a read of the already-created operation.  It remains safe when
    # the original quote expired or the kill switch was turned on meanwhile.
    existing = (
        await db.execute(
            select(DeviceAddonIntent).where(
                DeviceAddonIntent.user_id == owner_id,
                DeviceAddonIntent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if hmac.compare_digest(existing.request_hash, request_hash):
            return existing
        raise DeviceAddonError('idempotency_conflict', 'Этот ключ уже использован с другим выбором.', status_code=409)
    _assert_purchase_enabled()
    quote = decode_quote(quote_token)
    if quote.get('user_id') != owner_id:
        raise DeviceAddonError('quote_invalid', 'Подтвердите цену заново.', status_code=409)
    from app.database.crud.user import lock_user_for_pricing

    locked_user = await lock_user_for_pricing(db, owner_id)
    # The first pre-lock lookup prevents needless work.  This second lookup
    # closes the concurrent lost-response window: a peer may have inserted and
    # even purchased the intent while this request waited for the user lock.
    existing = (
        await db.execute(
            select(DeviceAddonIntent).where(
                DeviceAddonIntent.user_id == owner_id,
                DeviceAddonIntent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if hmac.compare_digest(existing.request_hash, request_hash):
            return existing
        raise DeviceAddonError('idempotency_conflict', 'Этот ключ уже использован с другим выбором.', status_code=409)
    calculation = await calculate_device_addon(
        db,
        user=locked_user,
        subscription_id=int(quote['subscription_id']),
        devices_to_add=int(quote['devices_to_add']),
        lock=True,
    )
    if not _matches_quote(calculation, quote, user_id=owner_id):
        raise DeviceAddonError(
            'quote_changed',
            'Условия докупки изменились. Подтвердите новую цену.',
            quote=quote_for_calculation(calculation, user_id=owner_id),
        )
    intent = DeviceAddonIntent(
        public_id=str(uuid4()),
        user_id=owner_id,
        subscription_id=calculation.subscription_id,
        target_subscription_id=calculation.subscription_id,
        tariff_id=calculation.tariff_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        calculator_revision=CALCULATOR_REVISION,
        devices_to_add=calculation.devices_to_add,
        original_device_limit=calculation.original_device_limit,
        panel_uuid=calculation.panel_uuid,
        end_date=calculation.end_date,
        device_addon_generation=calculation.generation,
        days_left=calculation.days_left,
        monthly_price_kopeks=calculation.monthly_price_kopeks,
        base_price_kopeks=calculation.base_price_kopeks,
        quoted_price_kopeks=calculation.price_kopeks,
        discount_percent=calculation.discount_percent,
        price_snapshot=_quote_payload(calculation, user_id=owner_id),
    )
    db.add(intent)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(DeviceAddonIntent).where(
                    DeviceAddonIntent.user_id == owner_id,
                    DeviceAddonIntent.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None and hmac.compare_digest(existing.request_hash, request_hash):
            return existing
        raise DeviceAddonError('idempotency_conflict', 'Этот ключ уже использован с другим выбором.', status_code=409)
    await db.refresh(intent)
    return intent


async def get_owned_intent(
    db: AsyncSession, *, user_id: int, public_id: str, for_update: bool = False
) -> DeviceAddonIntent:
    query = select(DeviceAddonIntent).where(
        DeviceAddonIntent.public_id == public_id, DeviceAddonIntent.user_id == user_id
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    intent = (await db.execute(query)).scalar_one_or_none()
    if intent is None:
        raise DeviceAddonError('intent_not_found', 'Операция не найдена.', status_code=404)
    return intent


def _intent_quote_calculation(intent: DeviceAddonIntent, *, balance_kopeks: int) -> DeviceAddonCalculation:
    snapshot = dict(intent.price_snapshot or {})
    max_limit = snapshot.get('max_device_limit')
    return DeviceAddonCalculation(
        subscription_id=int(intent.target_subscription_id),
        devices_to_add=int(intent.devices_to_add),
        original_device_limit=int(intent.original_device_limit),
        new_device_limit=int(intent.original_device_limit + intent.devices_to_add),
        max_device_limit=int(max_limit) if max_limit is not None else None,
        included_free_devices=int(snapshot.get('included_free_devices', 0)),
        chargeable_devices=int(snapshot.get('chargeable_devices', intent.devices_to_add)),
        monthly_price_kopeks=int(intent.monthly_price_kopeks),
        base_price_kopeks=int(intent.base_price_kopeks),
        discount_percent=int(intent.discount_percent),
        price_kopeks=int(intent.quoted_price_kopeks),
        balance_kopeks=balance_kopeks,
        missing_kopeks=max(0, int(intent.quoted_price_kopeks) - balance_kopeks),
        days_left=int(intent.days_left),
        end_date=intent.end_date,
        tariff_id=intent.tariff_id,
        panel_uuid=intent.panel_uuid,
        generation=int(intent.device_addon_generation),
    )


async def serialize_intent(
    db: AsyncSession, *, intent: DeviceAddonIntent, user: User, include_quote: bool = True
) -> dict[str, Any]:
    attempts = (
        (
            await db.execute(
                select(DeviceAddonTopupAttempt)
                .where(DeviceAddonTopupAttempt.intent_id == intent.id)
                .order_by(DeviceAddonTopupAttempt.created_at)
            )
        )
        .scalars()
        .all()
    )
    response: dict[str, Any] = {
        'id': intent.public_id,
        # The live relation may be cleared by a legacy subscription deletion;
        # retain the immutable target in the receipt/API history without ever
        # treating it as a live, actionable subscription.
        'subscription_id': intent.target_subscription_id,
        'devices_to_add': intent.devices_to_add,
        'price_kopeks': intent.quoted_price_kopeks,
        'purchase_state': intent.purchase_state,
        'receipt': intent.receipt_json,
        'fulfillment_status': intent.fulfillment_state,
        'fulfillment_error_code': intent.fulfillment_error_code,
        'purchase_enabled': bool(settings.DEVICE_ADDON_PURCHASE_ENABLED),
    }
    can_create_topup = False
    if include_quote and intent.purchase_state != 'purchased':
        # A return from bank may be far later than the original 15-minute
        # quote.  Recalculate now; a stored price snapshot is evidence of what
        # was selected, never permission to debit at that old price.
        try:
            if intent.subscription_id is None or int(intent.subscription_id) != int(intent.target_subscription_id):
                raise DeviceAddonError(
                    'target_unavailable', 'Подписка для этой операции больше недоступна.', status_code=409
                )
            calculation = await calculate_device_addon(
                db,
                user=user,
                subscription_id=int(intent.subscription_id),
                devices_to_add=intent.devices_to_add,
            )
            response['quote'] = quote_for_calculation(calculation, user_id=user.id)
            can_create_topup = (
                calculation.missing_kopeks > 0
                and settings.DEVICE_ADDON_PURCHASE_ENABLED
                and not getattr(user, 'restriction_topup', False)
            )
        except DeviceAddonError as error:
            response['quote'] = None
            response['quote_error'] = {'code': error.code, 'message': str(error)}
    # This is deliberately derived from the full intent graph, not from a
    # single attempt status.  A late observation can move an old terminal
    # invoice into reconciliation while a newer invoice owns the slot.
    unresolved_states = {'creation_unknown', 'reconciling', 'operator_review'}
    response['topup_attempts'] = [
        serialize_topup_attempt(
            attempt,
            can_create_new_attempt=(
                can_create_topup
                and intent.purchase_state == 'draft'
                and attempt.status in {'terminal', 'paid', 'operator_review'}
                and not attempt.holds_invoice_slot
                and not any(other.holds_invoice_slot for other in attempts)
                and not any(other.holds_invoice_slot and other.status in unresolved_states for other in attempts)
            ),
        )
        for attempt in attempts
    ]
    return response


def serialize_topup_attempt(
    attempt: DeviceAddonTopupAttempt, *, can_create_new_attempt: bool = False
) -> dict[str, Any]:
    return {
        'id': attempt.public_id,
        'intent_id': None,  # Routes insert the public intent ID; internal FK stays private.
        'requested_amount_kopeks': attempt.requested_amount_kopeks,
        'payment_method': attempt.payment_method,
        'payment_option': attempt.method_key,
        'status': attempt.status,
        'credited_amount_kopeks': attempt.credited_amount_kopeks,
        # An external URL is actionable only for the one durable invoice that
        # still owns this intent's slot.  ``payment_url`` itself stays at the
        # route level so it can be URL-validated before leaving the API.
        'can_open_payment': bool(attempt.payment_url and attempt.holds_invoice_slot and attempt.status == 'pending'),
        'can_create_new_attempt': can_create_new_attempt,
        'action_required': attempt.status == 'operator_review',
    }


async def _run_purchase_post_commit_effects(
    db: AsyncSession,
    *,
    transaction: Transaction,
    user: User,
    subscription: Subscription,
    old_device_limit: int,
    new_device_limit: int,
    price_kopeks: int,
) -> None:
    """Emit non-atomic purchase effects without invalidating its receipt."""
    try:
        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=price_kopeks,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            payment_method=PaymentMethod.BALANCE,
            description=transaction.description or '',
        )
    except Exception as error:
        logger.error(
            'device_addon_purchase_side_effects_failed',
            transaction_id=transaction.id,
            user_id=user.id,
            error=error,
        )

    try:
        from app.bot_factory import create_bot
        from app.services.admin_notification_service import AdminNotificationService

        if settings.ADMIN_NOTIFICATIONS_ENABLED:
            bot = create_bot()
            try:
                await AdminNotificationService(bot).send_subscription_update_notification(
                    db=db,
                    user=user,
                    subscription=subscription,
                    update_type='devices',
                    old_value=old_device_limit,
                    new_value=new_device_limit,
                    price_paid=price_kopeks,
                )
            finally:
                await bot.session.close()
    except Exception as error:
        logger.error(
            'device_addon_purchase_admin_notification_failed',
            transaction_id=transaction.id,
            user_id=user.id,
            error=error,
        )


async def purchase_intent(db: AsyncSession, *, user: User, public_id: str, quote_token: str) -> DeviceAddonIntent:
    locked_user = await _locked_user(db, user.id)
    # U -> S -> I is the published wallet lock order.
    intent_stub = await get_owned_intent(db, user_id=user.id, public_id=public_id)
    # A completed debit is a durable receipt.  It must remain replayable even
    # if a later legacy deletion cleared the live subscription relation.
    if intent_stub.purchase_state == 'purchased':
        return intent_stub
    if intent_stub.subscription_id is None or int(intent_stub.subscription_id) != int(
        intent_stub.target_subscription_id
    ):
        raise DeviceAddonError('target_unavailable', 'Подписка для этой операции больше недоступна.', status_code=409)
    subscription = await _locked_subscription(db, user_id=user.id, subscription_id=int(intent_stub.subscription_id))
    intent = await get_owned_intent(db, user_id=user.id, public_id=public_id, for_update=True)
    if intent.purchase_state == 'purchased':
        return intent
    if intent.subscription_id is None or int(intent.subscription_id) != int(intent.target_subscription_id):
        raise DeviceAddonError('target_unavailable', 'Подписка для этой операции больше недоступна.', status_code=409)
    _assert_purchase_enabled()
    quote = decode_quote(quote_token)
    if quote.get('user_id') != user.id:
        raise DeviceAddonError('quote_invalid', 'Подтвердите цену заново.', status_code=409)
    from app.database.crud.user import lock_user_for_pricing

    locked_user = await lock_user_for_pricing(db, user.id)
    calculation = await calculate_device_addon(
        db,
        user=locked_user,
        subscription_id=subscription.id,
        devices_to_add=int(intent.devices_to_add),
        lock=True,
    )
    if not _matches_quote(calculation, quote, user_id=user.id):
        raise DeviceAddonError(
            'quote_changed',
            'Условия докупки изменились. Подтвердите новую цену.',
            quote=quote_for_calculation(calculation, user_id=user.id),
        )
    if locked_user.balance_kopeks < calculation.price_kopeks:
        raise DeviceAddonError(
            'insufficient_funds',
            'Недостаточно средств на балансе.',
            status_code=402,
            quote=quote_for_calculation(calculation, user_id=user.id),
        )
    # An explicit fresh quote can intentionally replace the old snapshot after
    # a tariff/discount/term change.  The new amount is recorded atomically
    # with the debit; no stale quote can silently alter a draft intent.
    intent.original_device_limit = calculation.original_device_limit
    intent.tariff_id = calculation.tariff_id
    intent.panel_uuid = calculation.panel_uuid
    intent.end_date = calculation.end_date
    intent.device_addon_generation = calculation.generation
    intent.days_left = calculation.days_left
    intent.monthly_price_kopeks = calculation.monthly_price_kopeks
    intent.base_price_kopeks = calculation.base_price_kopeks
    intent.quoted_price_kopeks = calculation.price_kopeks
    intent.discount_percent = calculation.discount_percent
    refreshed_snapshot = _quote_payload(calculation, user_id=user.id)
    # Account merge may have re-keyed a row when two formerly independent
    # per-user idempotency namespaces collided.  A later explicit purchase is
    # allowed to refresh pricing, but must not erase that forensic history.
    previous_snapshot = dict(intent.price_snapshot or {})
    merge_history = previous_snapshot.get('merge_idempotency_history')
    if isinstance(merge_history, list) and merge_history:
        refreshed_snapshot['merge_idempotency_history'] = list(merge_history)
    intent.price_snapshot = refreshed_snapshot
    transaction: Transaction | None = None
    if calculation.price_kopeks:
        locked_user.balance_kopeks -= calculation.price_kopeks
        transaction = await create_transaction(
            db,
            user_id=locked_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=calculation.price_kopeks,
            description=f'Покупка {intent.devices_to_add} доп. устройств',
            payment_method=PaymentMethod.BALANCE,
            commit=False,
        )
        intent.transaction_id = transaction.id
    subscription.device_limit = calculation.new_device_limit
    intent.purchase_state = 'purchased'
    intent.purchased_at = datetime.now(UTC)
    intent.fulfillment_state = 'pending'
    intent.next_attempt_at = datetime.now(UTC)
    intent.receipt_json = {
        'devices_added': intent.devices_to_add,
        'new_device_limit': calculation.new_device_limit,
        'amount_paid_kopeks': calculation.price_kopeks,
    }
    await db.commit()
    await db.refresh(intent)
    if transaction is not None:
        await _run_purchase_post_commit_effects(
            db,
            transaction=transaction,
            user=locked_user,
            subscription=subscription,
            old_device_limit=calculation.original_device_limit,
            new_device_limit=calculation.new_device_limit,
            price_kopeks=calculation.price_kopeks,
        )
    return intent


def random_lease_token() -> str:
    return secrets.token_urlsafe(32)

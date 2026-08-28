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
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.discount_offer import get_latest_claimed_offer_for_user
from app.database.crud.promo_offer_log import log_promo_offer_action
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
    DeviceFirstReconciliationCredit,
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
from app.services.device_first_deposit_outbox_service import (
    ensure_deposit_outbox,
    process_device_first_deposit_outbox,
)
from app.services.device_first_eligibility import resolve_single_eligible_tariff, tariff_eligibility
from app.services.pricing_engine import pricing_engine
from app.utils.miniapp_buttons import build_miniapp_or_callback_button


OPEN_STATES = {'draft', 'confirmed', 'awaiting_funds', 'armed', 'fulfilling'}
TERMINAL_STATES = {'ready', 'cancelled', 'expired', 'failed', 'reprice_required', 'conflict', 'operator_review'}
LEGACY_SETTLEMENT_MODE = 'legacy_deposit'
DIRECT_SETTLEMENT_MODE = 'direct_purchase_v2'
KOPEKS_PER_RUBLE = 100
READY_NOTIFICATION_TYPE = 'ready'
# РФ-1 п.1.3: device-first платил реферальную комиссию МОЛЧА — `_add_reward` кладёт деньги на
# баланс, и обращения к боту в том файле нет вовсе. Партнёру при регистрации обещают процент,
# и он его получал, не зная об этом. Тип идёт через ту же очередь сообщений: у неё уже есть
# бот и защита «ровно один раз» уникальным ключом (заказ, тип).
REFERRAL_REWARD_NOTIFICATION_TYPE = 'referral_reward'
OWNER_ALERT_NOTIFICATION_TYPE = 'order_stuck'
# Пункт 4.1-Б. Права тарифа изменились между расчётом и оплатой. Заказ при этом выдаётся
# захваченным набором — расхождение только СООБЩАЕТСЯ. Колонка `notification_type` —
# обычный `String(48)` без CHECK (`models.py:3042`), поэтому новое значение не требует
# ни миграции, ни ручного воркфлоу.
ENTITLEMENT_DRIFT_NOTIFICATION_TYPE = 'entitlement_drift'
# Пункт 4.1-А. Подписка менялась под уже оплаченным заказом, но не «подменой», поэтому
# заказ выдан. Владельцу об этом обязаны сказать: иначе его собственное «Обнулить
# подписку» отменяется пришедшим следом платежом совершенно молча.
TARGET_DRIFT_NOTIFICATION_TYPE = 'target_drift'
# Оба типа выше — строки ВЛАДЕЛЬЦУ. Для него проект уже решил, что потеря дороже дубля,
# поэтому они обязаны попадать в оживление наравне с `order_stuck` (пункт 4.8).
OWNER_NOTIFICATION_TYPES = (
    OWNER_ALERT_NOTIFICATION_TYPE,
    ENTITLEMENT_DRIFT_NOTIFICATION_TYPE,
    TARGET_DRIFT_NOTIFICATION_TYPE,
)
# 🔴 РФ-1, найдено критиком полноты. Оживление было ограничено строками владельцу, и
# обоснование верное: для «✅ Подписка готова» отказ означает НЕИЗВЕСТНЫЙ исход, повтор дал бы
# клиенту дубль. Для строки о реферальной награде расклад обратный: у повторной комиссии
# получатель ровно ОДИН, и единственный отказ Телеграма хоронил бы деньги молча навсегда —
# то есть отменял бы весь смысл пункта «партнёр получает комиссию И УЗНАЁТ о ней». Дубль
# «вам начислено» безобиден, молчание — нет.
RETRYABLE_NOTIFICATION_TYPES = (*OWNER_NOTIFICATION_TYPES, REFERRAL_REWARD_NOTIFICATION_TYPE)
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


# Пункт 4.1. Ключи, расхождение по которым означает «это уже ДРУГАЯ подписка», а не
# «прошло время». Только они запрещают выдать оплаченный заказ.
#
# 🔴 Почему список явный, а не «сравнить словари целиком». Слепок снимается в
# `create_checkout` и сверяется трижды (ищи `_target_snapshot_drift`, а не номера строк —
# они уезжали уже внутри этой ветки): дважды до денег и один раз ПОСЛЕ, где отказ означает
# «деньги взяли, подписку не выдали». Сравнение целиком ловило не подмену, а время:
#   `updated_at` стоит с `onupdate=func.now()` (`models.py:2486`) — его двигает ЛЮБАЯ
#   запись в строку подписки. Синхронизация трафика (`subscription_service.py:1127`) идёт
#   при открытии мини-аппы и в хендлере покупки; отдельного планировщика у неё нет, но
#   человек, оформляющий заказ, эти экраны и открывает. То есть слепок протухал сам собой
#   ровно у того, кто продлевает.
#   `status` и `end_date` законно меняет монитор (`MONITORING_INTERVAL=60`): триал,
#   истёкший между расчётом и оплатой, — это норма, а не повод уводить заказ в разбор.
# Проверено на боевом: слепок заказа 36 (создан 12:41) держал `status='active'` и
# `updated_at` от 14.08, а к 13:20 строка подписки 134 была переписана. Заплати человек
# по тому счёту — заказ ушёл бы в `operator_review` с забранными деньгами.
_SNAPSHOT_IDENTITY_KEYS = ('id', 'tariff_id', 'is_trial')

# Двигаются законно и выдачу не запрещают. Но расхождение по ним пишется в лог: без этой
# строки «сверку ослабили» невозможно отличить от «сверка никогда и не срабатывала».
# 🔴 `updated_at` здесь обязателен, хотя он и есть главный «шум»: без него самый частый
# случай (фоновая запись сдвинула только его) не оставлял бы ВООБЩЕ никакого следа, и
# обещание строкой выше было бы неправдой ровно там, где оно нужнее всего.
_SNAPSHOT_TOLERATED_KEYS = ('status', 'end_date', 'device_limit', 'updated_at')

# Статусы, в которые подписка сама не переходит: их ставит человек из админки.
# `expired` и `limited` сюда НЕ входят — это часы и трафик.
_ADMIN_SUPPRESSED_STATUSES = ('disabled',)


def _snapshot_identity_drift(captured: Any, target: Subscription | None) -> tuple[str, ...]:
    """Ключи, по которым захваченный слепок разошёлся с живой подпиской «по личности».

    🔴 Отсутствие значимого ключа в старом слепке — это «не знаем», а НЕ «совпало».
    Слепок без `id` молча проходил бы любую проверку, то есть сверка выключилась бы сама
    на старых заказах. Считаем такой ключ разошедшимся и пересчитываем заказ.
    """
    if target is None:
        return ('id',)
    if not isinstance(captured, dict) or not captured:
        return _SNAPSHOT_IDENTITY_KEYS
    fresh = _subscription_snapshot(target)
    return tuple(key for key in _SNAPSHOT_IDENTITY_KEYS if key not in captured or captured[key] != fresh[key])


def _snapshot_tolerated_changes(captured: Any, target: Subscription | None) -> tuple[str, ...]:
    if target is None or not isinstance(captured, dict) or not captured:
        return ()
    fresh = _subscription_snapshot(target)
    return tuple(key for key in _SNAPSHOT_TOLERATED_KEYS if key in captured and captured[key] != fresh[key])


def _administrative_target_change(captured: Any, target: Subscription | None) -> tuple[str, ...]:
    """Изменения под оплаченным заказом, которые сделал ЧЕЛОВЕК, а не часы.

    🔴 Тревожить владельца можно только этим. Терпимых расхождений много, и почти все они
    штатные: истёк триал, кончился трафик (`status='limited'`), клиент сам докупил
    устройства, сдвинулся `updated_at`. Слать по ним строку — значит заливать канал
    сообщениями «вы отключали этого человека?» там, где владелец не делал ничего, и
    подталкивать его отключить платящего клиента. Канал, который кричит на каждую
    покупку, перестают читать — ровно этим уже испорчены другие тревоги проекта.

    Признак вмешательства — подпись кнопки «Обнулить подписку»
    (`crud/subscription.py:1743-1749`): статус становится `disabled`, а срок уезжает НАЗАД.
    """
    if target is None or not isinstance(captured, dict) or not captured:
        return ()
    fresh = _subscription_snapshot(target)
    changed: list[str] = []
    if fresh['status'] != captured.get('status') and fresh['status'] in _ADMIN_SUPPRESSED_STATUSES:
        changed.append('status')
    before, after = captured.get('end_date'), fresh['end_date']
    if isinstance(before, str) and isinstance(after, str):
        try:
            if datetime.fromisoformat(after) < datetime.fromisoformat(before):
                changed.append('end_date')
        except ValueError:
            pass
    return tuple(changed)


def _target_snapshot_drift(
    checkout: SubscriptionCheckout,
    captured: Any,
    target: Subscription | None,
    *,
    stage: str,
) -> tuple[str, ...]:
    """Решить, разошёлся ли слепок, и записать в лог то, что мы сознательно пропустили."""
    drift = _snapshot_identity_drift(captured, target)
    tolerated = _snapshot_tolerated_changes(captured, target)
    if drift or tolerated:
        _event(
            'target_snapshot_compared',
            checkout,
            stage=stage,
            drift=','.join(drift) or None,
            tolerated=','.join(tolerated) or None,
        )
    return drift


async def _resolve_checkout_entitlement(
    db: AsyncSession,
    *,
    tariff: Tariff,
    target: Subscription | None,
):
    """Чьи права мы берём — подписки или тарифа.

    ⚠️ Это НЕ единственная копия правила: то же условие живёт в `create_checkout` и в
    `_validate_direct_pre_commit`. Условие там дословно такое же, а вызов отличается: те два
    передают `access_point_quote_context=True`, а этот хелпер — как и код, из которого он
    выделен, — не передаёт ничего. Для боевых режимов это безразлично: в access-point-ветку
    сверка расхождения не заходит. Правишь правило — проверь все три, иначе место захвата и
    место сверки разъедутся, и тревога придёт на КАЖДУЮ покупку.

    🔴 Сверка перед выдачей
    обязана переспрашивать ТОТ ЖЕ источник: у платной подписки на `native_squads`
    права берутся из её собственных `connected_squads`
    (`public_location_entitlement_service.py:467-471`), а не из `allowed_squads` тарифа.
    Сравни одно с другим — и каждое продление отчиталось бы ложным расхождением: на
    боевом 105 подписок сидят на старых сквадах, а тариф 3 уже переведён на три новых.
    Ровно в переезд Польши это залило бы владельца ложными тревогами.
    """
    from app.services.public_location_entitlement_service import (
        get_subscription_resolved_entitlement,
        resolve_tariff_entitlement,
    )

    if target is not None and not target.is_trial and getattr(tariff, 'entitlement_mode', None) == 'native_squads':
        return await get_subscription_resolved_entitlement(db, target.id)
    return await resolve_tariff_entitlement(db, tariff)


async def _queue_owner_checkout_drift_row(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    user: User,
    notification_type: str,
) -> None:
    """Одна строка владельцу про заказ, который мы выдали, хотя под ним что-то изменилось.

    Замки те же, что у тревоги 4.3, и по тем же причинам:
    - выключенный админ-чат — не повод копить строки, которые гарантированно упадут;
    - удалённый (или попросивший удаления) пользователь — не повод отправлять его имя в чат.
    """
    if not _owner_alerts_enabled():
        logger.warning('device_first_owner_alerts_disabled', checkout_id=checkout.public_id)
        return
    if user.account_erased_at is not None or user.account_erasure_requested_at is not None:
        return
    # Не полагаемся на IntegrityError: перехватить его здесь нечем — rollback отменил бы саму
    # продажу. Гонки нет: заказ на этот момент держит `with_for_update`.
    already = await db.scalar(
        select(DeviceFirstNotificationOutbox.id).where(
            DeviceFirstNotificationOutbox.checkout_id == checkout.id,
            DeviceFirstNotificationOutbox.notification_type == notification_type,
        )
    )
    if already is None:
        db.add(DeviceFirstNotificationOutbox(checkout_id=checkout.id, notification_type=notification_type))


async def _report_entitlement_drift_without_blocking(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    user: User,
    target: Subscription | None,
    captured: Any,
) -> None:
    """Пункт 4.1-Б: права разъехались между расчётом и оплатой — сказать, но НЕ запретить.

    🔴 **Запретом это делать нельзя, и это решение владельца, а не упрощение.** Отказ в
    этой точке означает: деньги взяты, подписка не выдана, клиент заперт `operator_hold`,
    тариф заперт забором живых заказов — а разобрать заказ нечем до пункта 4.4. Этап 3 и
    есть массовая правка `allowed_squads`, то есть запрет производил бы такие оплаченные
    заказы пачками прямо в момент переезда Польши. Клиент получает захваченный набор — тот,
    за который заплатил, — а расхождение уходит владельцу.
    Превращать в запрет — отдельным решением после 4.4, когда логи покажут, что ложных
    срабатываний нет.

    🔴 Функция не имеет права отказать в выдаче оплаченной подписки — ни своим исключением,
    ни своим выводом. Оговорка про «немоту» честная, но НЕ абсолютная: она стоит внутри
    денежной транзакции, и настоящий отказ базы (обрыв, таймаут) её переживёт и убьёт
    продажу дальше по коду. Сессия создаётся с `autoflush=False`
    (`app/database/database.py:100`), поэтому лишнего флаша её SELECT-ы не вызывают.
    """
    from app.services.public_location_entitlement_service import EntitlementResolutionError

    try:
        tariff = await db.get(Tariff, checkout.tariff_id)
        if tariff is None:
            return
        try:
            current = await _resolve_checkout_entitlement(db, tariff=tariff, target=target)
        except EntitlementResolutionError as error:
            # 🔴 «Не смогли посчитать» — это НЕ «расхождения нет», и молчать здесь значит
            # молчать ровно в переезде Польши: смена серверов сплошь и рядом сначала ломает
            # разрешение прав (legacy-манифест не переодобрен, сквад ещё не помечен
            # доступным) и только потом даёт другой ответ.
            _event('entitlement_drift_unresolvable', checkout, error=str(error)[:120])
            await _queue_owner_checkout_drift_row(
                db, checkout=checkout, user=user, notification_type=ENTITLEMENT_DRIFT_NOTIFICATION_TYPE
            )
            return
        # Сравниваем СОСТАВ, а не порядок: `snapshot_hash` считается по упорядоченному
        # списку, и пересохранение тех же стран в другом порядке дало бы ложную тревогу.
        # Строгий хеш нужен блокирующим проверкам, а эта — сообщающая.
        if frozenset(current.squad_uuids) == frozenset(getattr(captured, 'squad_uuids', ())):
            return
        _event(
            'entitlement_drift_tolerated',
            checkout,
            captured_squads=len(captured.squad_uuids),
            current_squads=len(current.squad_uuids),
            provenance=current.provenance,
        )
        await _queue_owner_checkout_drift_row(
            db, checkout=checkout, user=user, notification_type=ENTITLEMENT_DRIFT_NOTIFICATION_TYPE
        )
    except Exception as error:
        logger.warning(
            'device_first_entitlement_drift_check_failed',
            checkout_id=checkout.public_id,
            error=type(error).__name__,
        )


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
    elif _target_snapshot_drift(checkout, checkout.target_snapshot, target, stage='arm'):
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

    from app.services.public_location_entitlement_service import EntitlementResolutionError

    try:
        entitlement = await _resolve_checkout_entitlement(db, tariff=tariff, target=target)
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
    # Скидку меряем ПЕРЕСЧИТАННОЙ ценой, а не замороженной разбивкой заказа: выше
    # стоит забор `charge != checkout.quoted_price_kopeks`, но он сверяет ИТОГ, а не
    # его состав. Именно `current_price` и дал ту сумму, которую сейчас списываем.
    await _consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=checkout,
        applied_discount_kopeks=int(current_price.promo_offer_discount or 0),
        # Здесь цена пересчитана ПРЯМО СЕЙЧАС из текущего предложения человека, поэтому
        # применённое и гасимое — одно и то же по построению, и сверка тождественна.
        # Окна, в которое предложение могло смениться, у этой ветки нет.
        expected_source=getattr(user, 'promo_offer_discount_source', None),
    )
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
    elif _target_snapshot_drift(checkout, checkout.target_snapshot, target, stage='confirm'):
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
    checkout: SubscriptionCheckout, tariff: Tariff, *, funding_mode: str, entitlement, user: User
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
        # 🔴 ЛИЧНОСТЬ предложения, а не только его сумма. Между заморозкой цены и приходом
        # денег человек может забрать ДРУГОЕ предложение — оно в двух нажатиях в кабинете.
        # ⚠️ Окно — ТРИДЦАТЬ МИНУТ (`quote_expires_at`, `:1028`), а не часы: оплату после
        # истечения котировки уводит в «деньги только на баланс»
        # (`device_first_payment_service.py:1925-1932`), до гашения она не доходит.
        # Первая формулировка говорила «часами» — преувеличение, поймано критиком полноты:
        # две линзы ревью разошлись в этом месте, разобрано по коду. Без этой строки гасилось
        # бы то, что лежит на нём в момент вебхука, а не то, что вошло в цену, — и клиент
        # терял бы неиспользованную скидку. Нашли две линзы ревью независимо.
        'promo_offer_source': getattr(user, 'promo_offer_discount_source', None),
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
    checkout.sale_snapshot = _direct_sale_snapshot(
        checkout, tariff, funding_mode='platega', entitlement=entitlement, user=user
    )
    checkout.financial_committed_at = datetime.now(UTC)
    checkout.lifecycle_state = 'awaiting_funds'
    checkout.funding_state = 'invoice_pending'
    if commit:
        await db.commit()
        await db.refresh(checkout)
    else:
        await db.flush()
    return checkout


async def _consume_promo_offer_for_sale(
    db: AsyncSession,
    *,
    user: User,
    checkout: SubscriptionCheckout,
    applied_discount_kopeks: int,
    expected_source: str | None,
) -> None:
    """Погасить одноразовую скидку, которую эта продажа ДЕЙСТВИТЕЛЬНО применила.

    🔴 Гасим по ФАКТУ применения (сколько копеек скидки ушло в цену), а не по праву
    на скидку. Право бывает и тогда, когда скидка на эту покупку вышла нулевой, — и
    тогда предложение сгорает впустую. Это мина **BC**, живой пример соседа, который
    так делать не надо: `app/handlers/menu.py:1814`. Легаси-пути гасят правильно
    (`handlers/subscription/purchase.py:2634`, `.../tariff_purchase.py:1263`).

    🔴 Звать ТОЛЬКО из блока, который исполняется один раз на заказ: повтор вебхука
    провайдера не должен гасить второе предложение. Сегодня таких мест два, и оба
    прикрыты уникальным `device_first_ledger_key` на денежной строке.

    ⚠️ Лог пишем В ТОЙ ЖЕ сессии (`commit=False`), а не в отдельной, как это делает
    сосед `subtract_user_balance` (`app/database/crud/user.py:780-800`). У соседа
    деньги к этому моменту уже закоммичены, и отдельная сессия защищает его от
    `MissingGreenlet`. Здесь коммита ещё не было: запись в отдельной сессии
    пережила бы откат продажи и соврала бы, что скидка потрачена.
    """
    if applied_discount_kopeks <= 0:
        return
    try:
        percent = int(getattr(user, 'promo_offer_discount_percent', 0) or 0)
    except (TypeError, ValueError):
        percent = 0
    if percent <= 0:
        return

    source = getattr(user, 'promo_offer_discount_source', None)
    if source != expected_source:
        # Предложение сменилось между расчётом цены и приходом денег. Гасить нельзя:
        # человек потерял бы предложение, которого эта покупка не применяла. Несовпадение
        # всегда решаем в пользу клиента — оставить скидку дешевле, чем сжечь чужую.
        logger.info(
            'Promo offer changed between the quote and the sale; leaving it untouched',
            user_id=user.id,
            checkout_id=checkout.id,
            quoted_source=expected_source,
            current_source=source,
        )
        return
    try:
        offer = await get_latest_claimed_offer_for_user(db, user.id, source)
    except Exception as lookup_error:  # pragma: no cover - defensive logging
        # Поиск предложения нужен только чтобы лог был подробнее. Продажу он не решает.
        logger.warning(
            'Failed to fetch claimed promo offer for device-first sale',
            user_id=user.id,
            checkout_id=checkout.id,
            lookup_error=lookup_error,
        )
        offer = None

    user.promo_offer_discount_percent = 0
    user.promo_offer_discount_source = None
    user.promo_offer_discount_expires_at = None

    # 🔴 Журнал — ВСПОМОГАТЕЛЬНЫЙ, ронять им оплаченную продажу нельзя. Сосед прикрывает
    # свою такую запись голым `try/except` (`app/database/crud/user.py:811-828`), и этого
    # ЗДЕСЬ мало: упавший `flush()` отравляет транзакцию, и следующий `commit()` всё равно
    # падает `PendingRollbackError` — на карточном пути это откат продажи, за которую
    # провайдер уже взял деньги. Поэтому сейвпоинт, как это уже сделано в том же файле
    # вокруг выдачи подписки: откатывается только журнал, продажа остаётся.
    # ⛔ НЕ УБИРАТЬ сейвпоинт: тестом он не сторожится и снятие его набор переживает.
    # С подделкой сессии сейвпоинт неотличим от голого `try/except` — разница видна
    # только на настоящей транзакции. Пробел записан в плане, а не замолчан.
    try:
        async with db.begin_nested():
            await log_promo_offer_action(
                db,
                user_id=user.id,
                offer_id=offer.id if offer else None,
                action='consumed',
                source=source,
                percent=percent,
                effect_type=offer.effect_type if offer else None,
                # ⚠️ Ключи именно эти: `description` и `amount_kopeks` экран журнала промо
                # уже умеет рисовать (`handlers/admin/promo_offers.py:356-368`). Свои имена
                # (`checkout_public_id`, `discount_kopeks`) не рисовались бы НИГДЕ — то есть
                # владелец не увидел бы ровно того, ради чего их кладут.
                details={
                    'reason': 'device_first_checkout',
                    # ⚠️ Сумму скидки кладём В ОПИСАНИЕ, а не в `amount_kopeks`. Экран
                    # журнала рисует этот ключ как «💰 Сумма», и у соседних записей он
                    # означает СПИСАННОЕ (`crud/user.py:803`). Владелец прочитал бы
                    # «Сумма: 24,90 ₽» под покупкой за 224,10 ₽ как цену покупки.
                    'description': (
                        f'Покупка в кассе, заказ {checkout.public_id} · скидка {applied_discount_kopeks / 100:.2f} ₽'
                    ),
                },
                commit=False,
            )
    except Exception as log_error:  # pragma: no cover - defensive logging
        logger.warning(
            'Failed to record promo offer consumption for a device-first sale',
            user_id=user.id,
            checkout_id=checkout.id,
            log_error=log_error,
        )


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
    elif _target_snapshot_drift(checkout, snapshot.get('target_snapshot'), target, stage='fulfil'):
        # 🔴 Единственное место сверки, где отказ стоит денег: сюда приходят уже
        # оплаченные заказы. Поэтому здесь запрещает только подмена самой подписки
        # (`_SNAPSHOT_IDENTITY_KEYS`), а не прошедшее время.
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'target_subscription_changed_after_payment'
        raise DeviceFirstError('operator_review_required', 'Subscription changed after payment')
    elif _administrative_target_change(snapshot.get('target_snapshot'), target):
        # 🔴 Подписку под оплаченным заказом ГАСИЛ человек — выдаём (деньги взяты), но
        # обязаны сказать владельцу. Самый дорогой случай — «Обнулить подписку»: она не
        # трогает ни `id`, ни `tariff_id`, ни `is_trial`, поэтому пришедший следом платёж
        # отменяет её целиком, включая отключение в панели. Без этой строки — молча.
        await _queue_owner_checkout_drift_row(
            db, checkout=checkout, user=user, notification_type=TARGET_DRIFT_NOTIFICATION_TYPE
        )

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
    else:
        await _report_entitlement_drift_without_blocking(
            db, checkout=checkout, user=user, target=target, captured=entitlement
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
                    # 🔴 У ПЛАТНОЙ подписки никогда не опускаем живой лимит: `device_limit`
                    # теперь терпимый ключ, а `extend_subscription` присваивает его без
                    # разговоров — значит человек, докупивший устройства в кабинете, пока шёл
                    # платёж, потерял бы их вместе с деньгами. Оба лимита оплачены.
                    # 🔴 А у ТРИАЛА — строго то, что купили. Триальщику намеренно разрешено
                    # взять меньше устройств, чем было в триале (`device_first_eligibility.py:90`
                    # отдаёт ему весь список без нижней границы, и оба живых гарда исключают
                    # триал явно). Возьми здесь max — и триальные устройства достались бы
                    # бесплатно, а потом стали бы полом навсегда (`:93-107` делает текущий
                    # лимит полом для платной). На боевом это живой случай: 21 триал на трёх
                    # устройствах против платных на двух.
                    device_limit=(
                        int(snapshot['device_limit'])
                        if target.is_trial
                        else max(int(snapshot['device_limit']), int(target.device_limit or 0))
                    ),
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
            receipt = Transaction(
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
            db.add(receipt)
            # 🔴 Номер прихода нужен строкой ниже, а `db.add` его не присваивает.
            await db.flush()
        # 🔴 РФ-1 п.1.2б. До этого этапа прямая оплата не платила реферальную комиссию ВООБЩЕ:
        # приход создавался и на этом всё заканчивалось. Это главный путь продаж с 02.08.2026,
        # и через него уже прошло 6 197 ₽ от приглашённых, с которых партнёрам не досталось
        # ничего. Заводим ту же durable-работу, что и кошельковый депозит: она атомарна одним
        # коммитом и защищена двумя уникальными ключами (`transaction_id` работы и
        # `device_first_ledger_key` наградной строки), то есть повтор вебхука или рестарт
        # контейнера её не удвоят.
        # ⛔ Заводить надо ровно на ПРИХОД. Списание за подписку — те же деньги с обратным
        # знаком, и вторая работа на него означала бы вторую выплату с одной покупки.
        await ensure_deposit_outbox(
            db,
            transaction_id=receipt.id,
            checkout_id=checkout.id,
            # Приход — не пополнение кошелька: событие о пополнении здесь было бы ложью.
            emit_deposit_event=False,
            # Выключатель спрашиваем ЗДЕСЬ, где обязательство возникает (РФ-1 п.1.1).
            pay_referral=settings.is_referral_program_enabled(),
            # Иначе работа легла бы в базу с меткой «легаси-депозит», и будущий разбор
            # «откуда пришли деньги» соврал бы про главный путь продаж.
            settlement_mode=DIRECT_SETTLEMENT_MODE,
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
    # Читаем из СНИМКА, а не из колонки заказа. Сегодня они совпадают (колонка пишется один
    # раз при создании), но вся остальная функция построена на снимке именно потому, что
    # колонка изменяемая, — а на неё нет забора расхождения. Нашла линза «деньги».
    _frozen_breakdown = (checkout.sale_snapshot or {}).get('price_breakdown') or {}
    await _consume_promo_offer_for_sale(
        db,
        user=user,
        checkout=checkout,
        applied_discount_kopeks=int(_frozen_breakdown.get('promo_offer_discount_kopeks') or 0),
        # Заказы, заведённые ДО этой правки, личности предложения не несут. Для них
        # запасное значение делает сверку тождественной — то есть прежнее правило «по сумме».
        expected_source=(checkout.sale_snapshot or {}).get(
            'promo_offer_source', getattr(user, 'promo_offer_discount_source', None)
        ),
    )
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
    checkout.sale_snapshot = _direct_sale_snapshot(
        checkout, tariff, funding_mode='wallet', entitlement=entitlement, user=user
    )
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
    await _kick_direct_referral_post_commit(db, checkout_id=result.id)
    await db.refresh(result)
    return result


async def _kick_direct_referral_post_commit(db: AsyncSession, *, checkout_id: int) -> None:
    """Разбудить очередь выплат сразу, а не ждать фонового обхода (РФ-1 п.1.2б).

    🔴 Без этого партнёр ждал бы комиссию и уведомление до ЧАСА: депозитную очередь сливает
    только петля `monitoring_service`, а её период `MONITORING_INTERVAL` на боевом не задан —
    значит действует умолчание 60 минут. Легаси-путь пополнения будит себя сам
    (`device_first_payment_service`), прямая продажа этого не делала.

    Best-effort и строго после коммита, по образцу соседней побудки выдачи: отказ здесь не
    может отменить уже состоявшийся приход, а работа останется в очереди и будет доведена
    фоновым обходом со своей выдержкой.
    """
    receipt_key = f'provider-receipt:{checkout_id}'
    try:
        receipt_id = (
            await db.execute(select(Transaction.id).where(Transaction.device_first_ledger_key == receipt_key))
        ).scalar_one_or_none()
        if receipt_id is None:
            return
        await process_device_first_deposit_outbox(db, transaction_id=receipt_id, limit=1)
    except Exception as error:
        # 🔴 Найдено ревью. Без этого отката отказ здесь оставлял сессию отравленной, и
        # следующая же строка вызывающего (`db.refresh`) падала с PendingRollbackError —
        # то есть вебхук провайдера получал ошибку ПОСЛЕ того, как деньги взяты, а подписка
        # выдана. Соседняя побудка выдачи делает ровно это же и по той же причине.
        logger.warning('device_first_direct_referral_kick_failed', checkout_id=checkout_id, error=str(error))
        try:
            await db.rollback()
        except Exception:
            pass


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
                        await bot.send_message(user.telegram_id, text, reply_markup=_client_ready_keyboard(user))
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
            # 🔴 `like` рядом со списком: с РФ-3 тип реферальной строки несёт получателя,
            # и один только `.in_()` перестал бы её оживлять — то есть отменил бы повтор
            # ровно там, где он и был заведён.
            or_(
                DeviceFirstNotificationOutbox.notification_type.in_(RETRYABLE_NOTIFICATION_TYPES),
                DeviceFirstNotificationOutbox.notification_type.like(f'{REFERRAL_REWARD_NOTIFICATION_TYPE}:%'),
            ),
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
    # 🔴 Пункт 4.5. Причины остановившихся заказов (`conflict`/`reprice_required`). Без них
    # карточка на КАЖДОМ таком заказе печатала «причину видно только по коду» — то есть
    # экран, сделанный ради того, чтобы перестать врать, просто замолчал. Все одиннадцать
    # проверены грепом по присваиваниям `terminal_reason`, ни одной выдуманной.
    'quote_expired': 'расчёт устарел, цену надо пересчитать',
    'price_changed': 'цена изменилась после расчёта',
    'entitlement_changed': 'набор серверов тарифа изменился после расчёта',
    'tariff_no_longer_eligible': 'этот тариф клиенту больше не подходит',
    'subscription_appeared': 'подписка у клиента появилась, пока шёл заказ',
    'target_subscription_changed': 'подписка клиента изменилась, пока шёл заказ',
    'device_limit_decrease_not_allowed': 'в заказе меньше устройств, чем у действующей подписки',
    'location_policy_not_sellable': 'страну из заказа сейчас не продаём',
    'non_positive_quote': 'расчёт вышел нулевым или отрицательным',
    'entitlement_quote_missing_or_invalid': 'расчёт прав доступа не сошёлся',
    'payment_amount_mismatch': 'платёжная система вернула сумму, отличную от счёта',
    # 🔴 Критик полноты: первая правка добавила названия только остановившимся заказам —
    # то есть той половине, где денег нет. А на заказе с деньгами живого клиента карточка
    # по-прежнему писала «причину видно только по коду». Здесь двенадцать причин ветки
    # `operator_review`, каждая проверена грепом. Наглядно: `subscription_appeared` был
    # переведён, а его платный близнец `subscription_appeared_after_payment` — нет.
    'subscription_appeared_after_payment': 'подписка у клиента появилась уже после оплаты',
    'target_subscription_changed_after_payment': 'подписка клиента изменилась уже после оплаты',
    'captured_entitlement_changed_after_payment': 'набор серверов изменился уже после оплаты',
    'invalid_sale_snapshot': 'слепок заказа испорчен',
    'invalid_entitlement_snapshot': 'слепок прав доступа испорчен',
    'entitlement_snapshot_hash_mismatch': 'слепок прав доступа не сошёлся с подписью',
    'tariff_missing_after_quote': 'тариф исчез после расчёта',
    'provider_terminal_identity_mismatch': 'платёжная система вернула чужой платёж',
    'provider_terminal_status_regressed': 'платёжная система передумала: счёт снова в ожидании',
    'provider_invoice_verification_mismatch': 'платёж не сошёлся при проверке',
    'provider_identity_binding_conflict': 'платёж привязан к другому заказу',
    'direct_payment_attempt_mode_or_binding_mismatch': 'платёж не сошёлся с попыткой оплаты',
}
_PROVISIONING_RU = {
    'not_started': 'не начиналась',
    'pending': 'в очереди',
    'retry': 'застряла в повторах',
    'ready': 'выдана',
}
_POST_PAID_REVERSAL_PREFIX = 'post_paid_provider_terminal'

# Причина, при которой счёт заведомо не оплачен. 🔴 Список намеренно из ОДНОГО члена.
# Аудит пункта 4.2б называл четыре, но три из них проверены по коду и оказались не про
# отсутствие денег:
#   · `provider_response_missing_safe_redirect` — счёт у Platega УЖЕ СОЗДАН и привязан
#     (`device_first_payment_service.py:1391`), брак только в ссылке; деньги могут прийти,
#     а переопроса у этого состояния нет (`:2138-2144` берёт лишь причину ниже);
#   · `provider_invoice_creation_incomplete` — состояние максимального незнания: id счёта
#     нет вовсе, и код прямо пишет, что повтор невозможен (`:2114-2117`). «Денег не было» —
#     догадка, а не факт;
#   · `tariff_missing_after_quote` — ставится до всякой проверки состояния заказа
#     (`_lock_direct_context`) и о деньгах не знает ничего.
# Всем троим верный ответ — `unknown`: у него есть свой честный текст.
# Сам список — ЗАПАСНОЙ вопрос, а не первый: он спрашивается только после того, как факты
# о деньгах в базе ответили «ничего не приходило». Обратный порядок дал бы «денег не было»
# при поздней оплате — она деньги приносит, а `terminal_reason` оставляет прежним.
#
# 🔴 Мина F дописала ВТОРУЮ причину: с этого момента брошенная корзина закрывается не в
# `operator_review`, а в `cancelled` с причиной `cancelled_by_user_after_invoice`
# (`device_first_payment_service.py:696-737`).
# ⚠️ Обоснование именно такое, а не «иначе текст 4.2б деградирует в „не знаем“»: то было
# неверно и исправлено ревизией плана. Экран разбора после мины F не выполняется вовсе, и
# текст не деградирует, а исчезает вместе с состоянием. Набор нужен для ДРУГОГО, и это
# сегодня живые пути: вердикт владельцу (`_money_verdict` ниже, он же пригодится 4.4) и
# ветка `cancelled` в боте, которая спрашивает тот же вердикт перед тем, как сказать
# «деньги не списаны» (`app/handlers/subscription/device_first.py:1310-1338`).
# Причина доказана по коду, а не выбрана по смыслу названия:
#   · она ставится ТОЛЬКО там, где `payment.is_paid` ложно, а счёт у провайдера ещё
#     не оплачен, — три места: мина F, ручная отмена (`:1006-1013`) и отказ от счёта ради
#     триала (`trial_activation_service.py:697-705`);
#   · пришедшие ПОЗЖЕ деньги эту причину не сохраняют: они либо переписывают её в
#     `late_paid_wallet_credit` (`device_first_payment_service.py:2019-2023`), либо уводят
#     заказ в `operator_review` с причиной удаления аккаунта (`:1853-1865`). То есть
#     «деньги есть, а причина прежняя» — состояние, недостижимое по коду;
#   · и всё равно этот список спрашивается ВТОРЫМ: факт зачисления в базе сильнее.
_NO_MONEY_TERMINAL_REASONS = frozenset(
    {
        'provider_invoice_missing_or_elapsed_expiry',
        'cancelled_by_user_after_invoice',
    }
)


async def checkout_money_state(db: AsyncSession, checkout: SubscriptionCheckout) -> str:
    """Брали ли с клиента деньги: `no_money` | `money_in_flight` | `unknown`.

    Этим полем ветвятся клиентские экраны бота и кабинета, поэтому вопрос ровно один и
    ответ грубый. Владельцу нужен подробный разбор — он в `_money_verdict` ниже, и оба
    ответа обязаны сходиться: сторож `test_money_state_agrees_with_owner_verdict`.
    """
    if str(checkout.terminal_reason or '').startswith(_POST_PAID_REVERSAL_PREFIX):
        # Платёж был и отозван платёжной системой. Зачисленная сумма на попытке при этом
        # НЕ обнуляется, поэтому без этой проверки клиент прочитал бы «платёж получен» про
        # деньги, которые ему уже вернули. Своего экрана у отзыва нет — честнее не
        # утверждать ничего. Тот же вопрос первым задаёт и `_money_verdict`.
        return 'unknown'
    if checkout.funding_mode == 'wallet':
        # Оплата с баланса не создаёт `CheckoutPaymentAttempt` вовсе, поэтому единственный
        # признак денег здесь — само списание.
        return 'money_in_flight' if checkout.debit_transaction_id is not None else 'no_money'
    credited_attempts = await db.scalar(
        select(func.count(CheckoutPaymentAttempt.id)).where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
        )
    )
    if credited_attempts or checkout.debit_transaction_id is not None:
        return 'money_in_flight'
    if str(checkout.terminal_reason or '') in _NO_MONEY_TERMINAL_REASONS:
        return 'no_money'
    return 'unknown'


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
    if str(checkout.terminal_reason or '') in _NO_MONEY_TERMINAL_REASONS:
        # Тут код ЗНАЕТ ответ: счёт закрыт неоплаченным. Отправлять владельца искать
        # несуществующий платёж — сочинять ему работу и сомнение на пустом месте.
        # Список общий с клиентским вердиктом: два ответа про один заказ обязаны сходиться,
        # иначе владелец возвращает деньги, о которых клиенту сказали «их не было».
        # 🔴 Формулировка без слова «истёк»: с миной F в наборе две причины, и вторая
        # («локально брошено») ставится в том числе когда человек сам отказался от счёта.
        return '🟢 Деньги: клиент не оплатил, счёт закрыт неоплаченным. Списания не было, возврат не нужен.'
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
    # 🔴 `funding_mode` у остановившегося заказа бывает NULL: способ оплаты ещё не
    # выбирался. Прежняя развилка «не wallet — значит платёжка» его выдумывала.
    paid_by = {'wallet': 'с баланса', 'platega': 'через платёжную систему'}.get(
        str(checkout.funding_mode or ''), 'способ ещё не выбран'
    )

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
        # 🔴 Строка ВЛАДЕЛЬЦУ, и с пункта 4.2б она намеренно НЕ совпадает с клиентской.
        # У клиента теперь три разных экрана по факту денег; владельцу же нужен один вход
        # в разбор, а точный ответ про деньги стоит строкой выше (`_money_verdict`).
        lines.append(f'⚠️ Оплату нужно проверить: {reason_ru or "причину видно только по коду"}.')
    elif checkout.lifecycle_state in OPERATOR_REVIEWABLE_STATES:
        # Пункт 4.5. Эти заказы карточка раньше не показывала вовсе, а текст ниже про них
        # врал: «бот продолжает пробовать сам» — не пробует. Свип их не выбирает
        # (`get_open_checkout_for_user`), очереди выдачи у них не бывает: строка
        # 🔴 Доказательство недостижимости (первая версия этого комментария была НЕВЕРНА,
        # опровергнута скептиком: переходы из вебхука случаются когда угодно, «до» тут ни
        # при чём). Настоящая причина: `provisioning_state ∈ {pending, retry}` бывает
        # только при `fulfillment_state == 'fulfilled'`, а `fulfill_checkout` на таком
        # заказе выходит сразу и в `conflict` его не уводит. На исторических legacy-строках
        # пара достижима — там этот совет будет неточен, и это записано честно.
        lines.append(f'⚠️ Заказ остановился: {reason_ru or "причину видно только по коду"}. Сам он не продолжится.')
        # Карточка называет ТЕКУЩИЙ вред, а `operator_close_unblocks` — что даст закрытие.
        # Это разные утверждения, и склеивать их в одно нельзя: у заказов, остановленных
        # из-за уже существующей подписки, вреда нет вовсе.
        if str(checkout.terminal_reason or '') in _REASONS_PROVING_CLIENT_ALREADY_HAS_SUBSCRIPTION:
            lines.append('ℹ️ Клиенту он скорее всего не мешает: подписка у него уже есть.')
        else:
            lines.append('🎁 Пока этот заказ висит, клиент не возьмёт пробный период.')
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
        # 🔴 «По этому заказу», а не «клиенту»: пункт 4.5 привёл сюда продлевающие заказы
        # (`target_subscription_changed`, `device_limit_decrease_not_allowed`), где VPN у
        # клиента работает, и прежняя формулировка звала оператора выдавать подписку заново.
        lines.append('📵 По этому заказу VPN не выдан.')
    # Блокировка нового заказа шире, чем `operator_review`: заказ, застрявший на выдаче,
    # держит клиента через `direct_provisioning_recovery` ровно так же.
    blocked = checkout.lifecycle_state == 'operator_review' or str(checkout.provisioning_state) in {'pending', 'retry'}
    if blocked and str(getattr(checkout, 'settlement_mode', '')) == DIRECT_SETTLEMENT_MODE:
        lines.append('🚫 Новый заказ он оформить не может, пока висит этот.')
    lines.append('')
    # 🔴 Пункт 4.4 переписал эти строки: до него кнопки разбора не существовало нигде,
    # и текст честно отправлял владельца делать всё руками. Теперь она есть — но ТОЛЬКО
    # для заказов в `operator_review`. Тревога же рассылается по ДВУМ условиям (см.
    # `owner_alert_candidate_query`), и вторая половина — застрявшая выдача — в раздел
    # разбора не попадает. Отправить туда владельца с таким заказом значило бы повторить
    # ровно ту мину H, которую этот же пункт и снимает: инструкция, ведущая в пустоту.
    if checkout.lifecycle_state == 'operator_review':
        lines += [
            '🛠️ Разобрать: админ-панель → «🧾 Заказы на разборе» → этот заказ. Там две кнопки: '
            'вернуть деньги клиенту на баланс и закрыть заказ.',
            '⛔ Возврат руками в кабинете Platega база НЕ увидит: клиент останется заперт, '
            'а кнопка возврата предложит вернуть те же деньги второй раз.',
        ]
    elif checkout.lifecycle_state in OPERATOR_REVIEWABLE_STATES:
        # Ключ здесь — только состояние заказа, потому что видимость в разделе решается
        # только им. Кнопки называем так же, как в ветке выше: оператор приходит на экран,
        # заранее зная, что там его ждёт.
        lines.append(
            '🛠️ Разобрать: админ-панель → «🧾 Заказы на разборе» → этот заказ. Там две кнопки: '
            'вернуть деньги клиенту на баланс и закрыть заказ. Про деньги — строка со значком 💵 выше.'
        )
    else:
        lines.append(
            '🛠️ Разбирать руками не нужно и нечем: бот продолжает пробовать выдачу сам. '
            'В разделе «🧾 Заказы на разборе» этого заказа не будет — он туда не попадает.'
        )
    lines += [
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


def _owner_client_line(user: User | None, checkout: SubscriptionCheckout) -> str:
    """Как опознать клиента: ссылка на профиль, telegram id и @username.

    ⚠️ Не тождественно тревоге «ЗАКАЗ ЗАВИС»: там username вынесен отдельной строкой.
    Свести оба текста в один хелпер — отдельная уборка, в объём пункта она не входит.

    `telegram_id` намеренно nullable (есть email-only пользователи). Подставить сюда None —
    значит получить отказ Telegram на всё сообщение и потерять его целиком.
    """
    if user is None:
        return f'👤 клиент id {checkout.user_id} (карточка недоступна)'
    name = html.escape(user.full_name or f'клиент {checkout.user_id}')
    if user.telegram_id:
        line = f'👤 <a href="tg://user?id={user.telegram_id}">{name}</a> (<code>{user.telegram_id}</code>)'
    else:
        line = f'👤 {name} (id {checkout.user_id}, телеграма нет)'
    return f'{line} • @{html.escape(user.username)}' if user.username else line


async def _send_owner_checkout_drift_alert(
    db: AsyncSession,
    *,
    bot,
    checkout: SubscriptionCheckout,
    notification_type: str,
) -> None:
    """Пункт 4.1: под оплаченным заказом что-то изменилось, но заказ мы всё равно выдали.

    🔴 `_owner_alert_is_obsolete` здесь применять нельзя: она гасит строку на `ready`, а эта
    строка как раз про успешно выданный заказ — она была бы «устаревшей» всегда.

    🔴 И нельзя утверждать «подписка выдана» безусловно. Строка ставится в очередь ВНУТРИ
    продажи, а выдача идёт после коммита; проверено экспериментом на нашей версии
    SQLAlchemy: строка, добавленная до `begin_nested`, переживает откат этого сейвпоинта.
    То есть заказ мог уйти в разбор, а мы бы написали «разбирать нечего» — и владелец
    перестал бы смотреть ровно там, где деньги взяты, а подписки нет.
    """
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    service = AdminNotificationService(bot)
    if not service.is_enabled:
        raise RuntimeError('admin_notifications_disabled')
    user = await db.get(User, checkout.user_id)
    snapshot = checkout.sale_snapshot if isinstance(checkout.sale_snapshot, dict) else {}
    tariff_name = html.escape(str(snapshot.get('tariff_name') or f'тариф {checkout.tariff_id}'))
    delivered = checkout.lifecycle_state == 'ready' and str(checkout.provisioning_state) == 'ready'
    if notification_type == ENTITLEMENT_DRIFT_NOTIFICATION_TYPE:
        headline = 'ℹ️ <b>Клиент оплатил в момент смены серверов</b>'
        what_happened = (
            'Пока человек платил, набор серверов по этому заказу успел поменяться. Деньги пришли, '
            'и за заказом закреплены те серверы, которые были в нём на момент оплаты. '
        )
        # 🔴 НЕ НАЗЫВАЕМ ПУТЬ ВООБЩЕ — их сегодня нет ни одного, и это проверено по коду:
        #   чат-админка «🌍 Сменить сервер» заглушена (`app/handlers/admin/users.py:3836`:
        #     экран открывается, нажатие отвечает отказом);
        #   кабинет «Сменить тариф» отвечает 409 до всякой записи
        #     (`app/cabinet/routes/admin_users.py:1442-1445`);
        #   кабинет «Сбросить подписку» физически УДАЛЯЕТ подписку (`:2864-2866`).
        # Две предыдущие версии этого текста звали в первые два — то есть повторяли мину AB
        # дважды. Честнее сказать «кнопки нет», чем послать владельца уничтожить оплаченный
        # срок. На все три факта стоят сторожа: оживят любую кнопку — тест покраснеет.
        # 🔴 Про будущее не обещаем ничего: «кнопка появится вместе с переездом» — это план,
        # а не код, и протухло бы в день выхода этапа 3. Название кнопки уточнено до
        # «в кабинете»: в соседней тревоге фигурирует чат-админская «Обнулить подписку»,
        # и два похожих названия с противоположным советом легко перепутать.
        # 🔴 19.08 (пункт 3.2): кнопка ПОЯВИЛАСЬ — «Раскатать серверы на подписки» на
        # карточке тарифа. Прежнее «готовой кнопки пока нет» стало ложью в день выкладки.
        # Раскатка берёт всех, чьи серверы разошлись с тарифом, — этот клиент как раз такой,
        # переносить его отдельно не нужно. Запрет на «Сбросить подписку» остаётся в силе:
        # он защищает от потери оплаченного срока и к появлению кнопки отношения не имеет.
        advice = (
            'На будущее: если вы сейчас переводите тариф на новые серверы, этот клиент сам туда '
            'не переедет — его серверы записаны на момент оплаты. Его подхватит кнопка '
            '«Раскатать серверы на подписки» на карточке тарифа в кабинете, вместе с остальными. '
            '⛔ Не пытайтесь сделать это кнопкой «Сбросить подписку» в кабинете: она удалит '
            'оплаченную подписку целиком, вместе с оплаченным сроком.'
        )
    else:
        headline = 'ℹ️ <b>Подписка клиента менялась, пока шла оплата</b>'
        what_happened = (
            'Пока человек платил, его подписку меняли — например кнопкой «Обнулить подписку» или '
            'правкой срока. Деньги пришли, и оплаченный срок лёг на подписку поверх этих правок. '
        )
        advice = 'Если вы отключали этого человека намеренно, отключение придётся повторить: пришедший платёж его снял.'
    outcome = (
        'Подписка выдана, разбирать тут нечего.'
        if delivered
        else 'Выдача ещё идёт. Если она застрянет, по этому же заказу придёт отдельное сообщение '
        '«ЗАКАЗ ЗАВИС» — действовать нужно будет по нему.'
    )
    text = '\n'.join(
        [
            headline,
            '',
            _owner_client_line(user, checkout),
            f'🏷️ Тариф: <b>{tariff_name}</b> • заказ <code>{html.escape(str(checkout.public_id))}</code>',
            '',
            what_happened + outcome,
            '',
            advice,
        ]
    )
    if not await service.send_admin_notification(text, category=NotificationCategory.ERRORS):
        raise RuntimeError('admin_notification_not_delivered')


def _client_ready_keyboard(user: User) -> InlineKeyboardMarkup:
    """Кнопка «подключиться» под сообщением о готовой подписке.

    Пункт 1 реза 22.08.2026. Сообщение уходило голым текстом «откройте кабинет», не давая
    ничего, чем его открыть. Это половина той же беды, что и уход на оплату: человек уже
    заплатил, а дальше должен догадаться сам.

    Второй повод, ради которого кнопка идёт ТЕМ ЖЕ пунктом, что и `openLink`. Адрес
    возврата после оплаты — https-кабинет, а не `t.me` (`_direct_checkout_return_url`,
    `device_first_payment_service.py:1160`), и в браузере он приземляется на экран входа.
    Мини-приложение при этом остаётся живым за спиной у браузера, но человеку, который его
    закрыл, нужна дверь обратно — вот она.

    Берём общий строитель (`miniapp_buttons.py:188`), а не свой `WebAppInfo`: он сам решает,
    открыть кабинет или уйти в callback, если кабинетный режим выключен. Запасной callback
    `subscription_connect` живой и зарегистрирован (`subscription/purchase.py:4536`).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                build_miniapp_or_callback_button(
                    text='\U0001f517 Connect' if user.language == 'en' else '\U0001f517 Подключиться',
                    callback_data='subscription_connect',
                    cabinet_path='/subscription',
                )
            ]
        ]
    )


async def _send_client_ready_message(db: AsyncSession, *, bot, checkout: SubscriptionCheckout) -> None:
    user = await db.get(User, checkout.user_id)
    if user is None or not user.telegram_id:
        raise RuntimeError('telegram_recipient_unavailable')
    text = (
        '✅ Your VPN subscription is ready. Open the cabinet to connect.'
        if user.language == 'en'
        else '✅ Ваша VPN-подписка готова. Откройте кабинет, чтобы подключиться.'
    )
    await bot.send_message(user.telegram_id, text, reply_markup=_client_ready_keyboard(user))


def _referral_reward_recipient(notification_type: str) -> int | None:
    """Получатель из типа строки. `None` — строка старого формата, до РФ-3."""
    _, _, tail = notification_type.partition(':')
    return int(tail) if tail.isdigit() else None


async def _send_referral_reward_message(
    db: AsyncSession, *, bot, checkout: SubscriptionCheckout, recipient_id: int | None = None
) -> None:
    """Партнёр узнаёт о комиссии, а новичок — о своём бонусе (РФ-1 п.1.3).

    Кому и сколько — из уже созданных наградных строк. Текст различает три случая, потому что
    они значат разное: первая награда (фикс + процент), повторная комиссия (только процент) и
    бонус новичка. Без имени друга сообщение бесполезно тому, у кого приглашённых несколько.
    """
    rewards = list(
        (
            await db.execute(
                select(Transaction)
                .where(
                    Transaction.device_first_checkout_id == checkout.id,
                    Transaction.type == TransactionType.REFERRAL_REWARD.value,
                )
                .order_by(Transaction.id)
            )
        )
        .scalars()
        .all()
    )
    if recipient_id is not None:
        # 🔴 Одна строка — один человек. Без этого фильтра каждая из строк заказа
        # разослала бы письма ВСЕМ, и при двух получателях каждый получил бы по два.
        rewards = [reward for reward in rewards if reward.user_id == recipient_id]
    if not rewards:
        # Начисления не было: у покупателя нет пригласившего, программа выключена или сумма
        # ниже порога. Сообщать нечего — строка закрывается выполненной, а не падает.
        return

    friend = await db.get(User, checkout.user_id)
    friend_name = html.escape(friend.full_name) if friend is not None else ''
    source = (
        await db.execute(
            select(Transaction)
            .where(
                Transaction.device_first_checkout_id == checkout.id,
                Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
            )
            # 🔴 Найдено ревью: без порядка по номеру операторский возврат создаёт по тому же
            # заказу второй приход, и в письмо попала бы произвольная сумма. Берём первый —
            # он и есть та оплата, с которой посчитана награда.
            .order_by(Transaction.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    paid = settings.format_price(source.amount_kopeks) if source is not None else ''

    failures: list[str] = []
    for reward in rewards:
        recipient = await db.get(User, reward.user_id)
        if recipient is None or not recipient.telegram_id:
            continue
        english = recipient.language == 'en'
        suffix = (reward.device_first_ledger_key or '').rsplit(':', 1)[-1]
        # 🔴 Точная сумма, без округления. `format_price` при включённом
        # PRICE_ROUNDING_ENABLED отбрасывает копейки и округляет ВВЕРХ: партнёру
        # обещали бы 400 ₽, а на баланс легло бы 399,75 ₽. Число, которое человек
        # сверяет со своим балансом, округлять нельзя.
        amount = f'{reward.amount_kopeks // 100},{reward.amount_kopeks % 100:02d} ₽'

        if suffix == 'referred-first-bonus':
            text = (
                f'🎁 <b>Welcome bonus</b>\n\n'
                f"Thanks for your first payment. We've added <b>{amount}</b> to your balance, "
                f'and you can put it toward your next renewal.\n\n'
                f'Glad to have you with us!'
                if english
                else f'🎁 <b>Бонус новичка зачислен</b>\n\n'
                f'За вашу первую оплату мы начислили <b>{amount}</b> на баланс. '
                f'Эти деньги можно потратить на продление.\n\n'
                f'Спасибо, что вы с нами!'
            )
        else:
            # 🔴 Найдено ревью: процент НЕЛЬЗЯ вычислять заново в момент отправки. Платит его
            # `calculate_referral_commission_percent` — со ступенями и отдельной ставкой первой
            # оплаты, — а базовая ставка может от неё отличаться. Письмо тогда назвало бы
            # процент, которого не было в расчёте. Берём расшифровку из описания наградной
            # строки: п.1.4 положил туда ровно те числа, по которым посчитаны деньги.
            # rsplit, а не split: имя из Телеграма может содержать «: » и утащить свой кусок
            # в денежную строку письма. Расшифровка всегда идёт последней.
            breakdown = (reward.description or '').rsplit(': ', 1)
            details = breakdown[1] if len(breakdown) == 2 else ''
            first = suffix == 'inviter-first-reward'
            head = 'Реферальная награда!' if first else 'Реферальная комиссия!'
            head_en = 'Referral reward!' if first else 'Referral commission!'
            if english:
                text = (
                    f'💰 <b>{head_en}</b>\n\n'
                    f'Your friend <b>{friend_name}</b> paid {paid} for a subscription.\n\n'
                    f'🎁 Your reward: <b>{amount}</b>\n\n'
                    f"It's already in your balance — see Balance → History for the breakdown."
                )
            else:
                text = (
                    f'💰 <b>{head}</b>\n\n'
                    f'Ваш друг <b>{friend_name}</b> оплатил подписку на {paid}.\n\n'
                    f'🎁 Ваша награда: <b>{amount}</b>'
                    + (f'\n<i>{html.escape(details)}</i>' if details else '')
                    + '\n\nДеньги уже на балансе.'
                )
        # 🔴 Найдено ревью: без своего перехвата один заблокировавший бота получатель уносил
        # сообщение второго. Награды идут по возрастанию номера, бонус другу создаётся первым —
        # то есть покупатель, закрывший бота, лишал партнёра уведомления НАВСЕГДА: строка ушла бы
        # в `failed`, а оживляет `revive_stale_notifications` только строки владельцу.
        # Деньги при этом уже начислены, так что отказ доставки не повод считать шаг несделанным.
        try:
            await bot.send_message(recipient.telegram_id, text, parse_mode='HTML')
        except Exception as error:
            failures.append(f'{recipient.id}:{type(error).__name__}')

    # 🔴 РФ-3: у строки теперь ОДИН получатель, поэтому «дошло хоть кому-то» больше не
    # оправдание. Любой отказ роняет строку — и оживление повторит её для этого человека,
    # не трогая тех, кому уже дошло.
    if failures:
        # 🔴 Падаем от ЛЮБОГО отказа. Раньше здесь стояло «не дошло ни одно», и при частичной
        # доставке строка помечалась выполненной: второй человек не узнавал о деньгах никогда.
        # Теперь у строки один получатель, поэтому «дошло кому-то» больше не оправдание.
        raise RuntimeError(f'referral_reward_delivery_failed: {", ".join(failures)}')


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
            elif row.notification_type in (ENTITLEMENT_DRIFT_NOTIFICATION_TYPE, TARGET_DRIFT_NOTIFICATION_TYPE):
                # 🔴 Ветка обязана стоять ДО `else`: без неё строка владельцу ушла бы
                # клиенту текстом «✅ Подписка готова» — вторым сообщением про тот же заказ.
                await _send_owner_checkout_drift_alert(
                    db, bot=bot, checkout=checkout, notification_type=row.notification_type
                )
            elif row.notification_type.startswith(REFERRAL_REWARD_NOTIFICATION_TYPE):
                # 🔴 Ветка обязана стоять ДО `else`: иначе строка о реферальной награде
                # уйдёт покупателю текстом «✅ Подписка готова» — вторым сообщением про
                # тот же заказ, а партнёр так и не узнает о деньгах.
                # 🔴 `startswith`, а не `==`: с РФ-3 тип несёт получателя
                # (`referral_reward:<id>`). Точное равенство увело бы новую строку в
                # `unknown_notification_type`, а оттуда её не подняло бы и оживление —
                # реферальные письма перестали бы повторяться вовсе.
                await _send_referral_reward_message(
                    db, bot=bot, checkout=checkout, recipient_id=_referral_reward_recipient(row.notification_type)
                )
            elif row.notification_type == READY_NOTIFICATION_TYPE:
                await _send_client_ready_message(db, bot=bot, checkout=checkout)
            else:
                # 🔴 Fail-closed. Раньше здесь стоял голый `else`, и любой НОВЫЙ тип уходил
                # бы клиенту текстом «✅ Подписка готова». Незнакомый тип — это наша ошибка,
                # и увидеть её должны мы, а не клиент.
                raise RuntimeError(f'unknown_notification_type:{row.notification_type}')
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


def _drop_frozen_money_state(response: dict[str, Any]) -> dict[str, Any]:
    """Вердикт о деньгах не хранится в записи идемпотентности.

    Повтор с тем же ключом отдаёт сохранённый ответ дословно. Замороженное «мы ничего не
    списали» пережило бы приход денег — ровно тот отказ, ради которого вердикт и вынесли
    на бэкенд. Без ключа фронт показывает нейтральный текст и ничего не утверждает.
    """
    cleaned = {key: value for key, value in response.items() if key != 'money_state'}
    nested = cleaned.get('checkout')
    if isinstance(nested, dict) and 'money_state' in nested:
        cleaned['checkout'] = {key: value for key, value in nested.items() if key != 'money_state'}
    return cleaned


async def store_mutation_result(
    db: AsyncSession,
    mutation: DeviceFirstMutation,
    *,
    response: dict[str, Any],
    status_code: int = 200,
) -> None:
    mutation.response_json = _drop_frozen_money_state(response)
    mutation.status_code = status_code
    await db.commit()


# ---------------------------------------------------------------------------
# Пункт 4.4. Разбор заказов оператором.
#
# До этого пункта заказ, упавший в `operator_review`, разобрать было НЕЧЕМ: списка нет
# ни в чат-админке, ни в веб-админке, ни в кабинете (проверено grep'ом, ноль вхождений).
# А заказ при этом держит клиента (`operator_hold`, `:879` и `:1609`) и запирает смену
# серверов тарифа (`crud/tariff.py`). Ниже — ровно три вещи: показать, вернуть, закрыть.
# ---------------------------------------------------------------------------

# 🔴 Причина закрытия ОПЕРАТОРОМ. Три места, где она обязана быть учтена, — и почему:
#   1) `_NO_MONEY_TERMINAL_REASONS` — сюда НЕ добавлять. Этот набор означает «код ЗНАЕТ,
#      что денег не брали». Оператор закрывает и оплаченные заказы тоже, и утверждать за
#      него «списания не было» — ровно та ложь, которую снимал пункт 4.2б.
#   2) Условие позднего платежа в `device_first_payment_service` — добавить ОБЯЗАТЕЛЬНО,
#      иначе деньги, пришедшие после закрытия, не вернутся клиенту на баланс, а заново
#      запрут его в `operator_review` (мина F, требование про `provider_terminal:*`).
#   3) Забор триала (`trial_activation_service`) смотрит на `lifecycle_state`, а не на
#      причину, и `cancelled` его устраивает — трогать там нечего.
OPERATOR_CLOSED_TERMINAL_REASON = 'cancelled_by_operator_review'
# Метка на попытке после ручного возврата. Её единственная работа — попасть в ранний
# выход платёжного сервиса, чтобы повторный вебхук провайдера не заплатил второй раз.
OPERATOR_REFUND_RECONCILIATION_REASON = 'operator_refund_wallet_credit'
# Ключ книги для возврата. Идемпотентность строится на нём, а не на статусах: повторное
# нажатие кнопки обязано быть безопасным, а Telegram доставляет колбэки не по разу.
OPERATOR_REFUND_LEDGER_PREFIX = 'operator_review_refund'
# Пункт 4.5. Какие заказы попадают на экран «🧾 Заказы на разборе». ОДИН набор на всё:
# видимость списка, счётчик, карточка, возврат и закрытие. Разойдутся — экран покажет
# заказ, по которому кнопки откажут, и это уже случалось (P0 пункта 4.4). Сторож на само
# согласие списка и счётчика — `test_the_counter_and_the_list_ask_the_same_question`:
# без него подмена набора у счётчика проходила весь набор тестов молча.
#
# 🔴 НЕ сводить ни с `TERMINAL_STATES` (`:46`), ни с `_CHECKOUT_TERMINAL_STATES`
# (`crud/tariff.py:52`). У них другой вопрос. Здесь вопрос один: «этот заказ сам никуда
# не поедет, и его надо закрыть руками?»
#
# 🔴 И НЕ подставлять этот набор в `find_tariff_operator_review_order` (`:3378`), хотя
# план 4.5 это и предписывал. Та функция объясняет, ПОЧЕМУ забор тарифа отказал, а забор
# (`crud/tariff.py:_assert_tariff_squad_change_has_no_live_checkout`) устроен из ДВУХ
# дизъюнктов: первый эти три состояния не видит (они в его терминальном наборе), второй —
# `live_provider_attempt` — смотрит на статус попытки и состояние заказа не спрашивает
# вовсе. Значит `conflict` с живой попыткой тариф всё-таки держит, но закрытие статус
# попытки не меняет, то есть замок не снимет. Расширить функцию значит назвать владельцу
# заказ, закрытие которого ему не поможет, и увести от настоящей причины отказа.
# ⚠️ `failed` в наборе — про исторические строки: `lifecycle_state = 'failed'` сегодня не
# присваивается нигде в `app/` (проверено грепом). Живых состояний тут три, не четыре.
OPERATOR_REVIEWABLE_STATES = frozenset({'operator_review', 'conflict', 'failed', 'reprice_required'})
# 🔴 Причины, которые ставятся клиенту с уже существующей подпиской. Забор триала отбивает
# таких раньше, чем доходит до заказов: любая не-`PENDING` подписка даёт `trial_already_used`
# (`trial_activation_service.py:654-656`). Значит закрытие такого заказа скорее всего не
# вернёт клиенту ни покупку, ни триал — и обещать это оператору нельзя.
# ⚠️ Оговорка (мина BQ, ревизия 20.08.2026): «скорее всего», а не «точно». Подписка могла
# быть в статусе `PENDING` — её тот забор пропускает, и тогда закрытие триал как раз вернёт.
# Ответ по факту требует запроса к подпискам; здесь мы отвечаем по причине заказа, поэтому
# формулировка обязана быть осторожной. Настоящее лечение — вопрос к базе, дом: 4ост.
_REASONS_PROVING_CLIENT_ALREADY_HAS_SUBSCRIPTION = frozenset(
    {'subscription_appeared', 'target_subscription_changed', 'device_limit_decrease_not_allowed'}
)


def operator_close_unblocks(checkout: SubscriptionCheckout) -> str:
    """Что именно даёт закрытие ЭТОГО заказа. Для разных состояний это разные вещи.

    Заказ на разборе держит клиенту новую покупку (`operator_hold` отбивает её напрямую).
    Остановившийся заказ (`conflict`/`failed`/`reprice_required`) покупку не держит вовсе —
    он держит **пробный период**: забор триала пропускает только `cancelled`, `expired` и
    `ready` (`trial_activation_service.py:400`, `:677`). А если заказ остановлен потому, что
    подписка у клиента уже есть, — не держит и триала: тот закрыт другим забором, раньше.
    Обещать оператору одно на все три случая — соврать ему в двух.
    """
    if checkout.lifecycle_state == 'operator_review':
        return 'Клиент снова сможет оформить покупку.'
    if str(checkout.terminal_reason or '') in _REASONS_PROVING_CLIENT_ALREADY_HAS_SUBSCRIPTION:
        return 'Клиенту это скорее всего ничего не даст: заказ остановился из-за его подписки.'
    return 'Клиент снова сможет взять пробный период, если ещё им не пользовался.'


async def list_operator_review_checkouts(
    db: AsyncSession,
    *,
    limit: int = 20,
) -> list[SubscriptionCheckout]:
    """Заказы, ждущие человека. Сначала те, где могут быть деньги, потом самые свежие.

    🔴 Пункт 4.5. Раньше сортировка была только по свежести, и это было верно: все строки
    списка значили одно и то же. Теперь список смешивает два несравнимых случая, и
    `reprice_required` — это рутина (протухший за 30 минут расчёт, смена цены), которая
    всегда свежее. Без этого приоритета двадцати таких заказов хватило бы, чтобы вытеснить
    за экран `operator_review` с удержанными деньгами живого клиента, а ни листалки, ни
    поиска по номеру в проекте нет.
    """
    rows = await db.execute(
        select(SubscriptionCheckout)
        .join(User, User.id == SubscriptionCheckout.user_id)
        .where(*_operator_review_visible_conditions())
        .order_by(
            (SubscriptionCheckout.lifecycle_state == 'operator_review').desc(),
            SubscriptionCheckout.updated_at.desc(),
            SubscriptionCheckout.id.desc(),
        )
        .limit(limit)
    )
    return list(rows.scalars().all())


def _operator_review_visible_conditions(states: frozenset[str] = OPERATOR_REVIEWABLE_STATES) -> tuple[Any, ...]:
    """Условия видимости заказа на разборе. ОДИН источник для списка и счётчика.

    🔴 Раньше замок стоял только на списке, а счётчик считал всё. Расхождение читалось
    как страничная навигация («Показаны свежие 5» из 6), которой нет, а в худшем случае
    экран писал «разбирать нечего» при удержанных деньгах живого клиента. Прогон
    сценария нашёл оба исхода.

    Набор состояний параметром, а замок PII — общий и всегда. Единственный, кто сужает
    набор, — `find_tariff_operator_review_order`: у него другой вопрос (см. там же).
    """
    return (
        # Пункт 4.5 добавил сюда `conflict`/`failed`/`reprice_required`. Их не выбирает
        # свип (`get_open_checkout_for_user`), они не истекают и никем не закрываются —
        # то есть остаются навечно и навсегда лишают клиента пробного периода.
        SubscriptionCheckout.lifecycle_state.in_(sorted(states)),
        # 🔴 Тот же замок, что стоит на тревоге владельцу, и по той же причине:
        # человек подал заявку на удаление, но PII ещё НЕ вычищено (чистка идёт
        # только при финализации). Карточка показала бы оператору имя, @username и
        # ссылку `tg://user?id=` на того, кто просил себя забыть. Заказы уже
        # ФИНАЛИЗИРОВАННЫХ удалений остаются: там поля обнулены, показывать нечего,
        # а закрывать их надо — это и есть пять архивных заказов тарифа 3.
        or_(
            User.account_erasure_requested_at.is_(None),
            User.account_erased_at.is_not(None),
        ),
    )


async def find_tariff_operator_review_order(db: AsyncSession, *, tariff_id: int) -> tuple[str, bool] | None:
    """Заказ тарифа, ждущий человека: (первые 8 знаков номера, найдёт ли его владелец).

    Живёт здесь, а не у маршрута раскатки, ровно по той же причине, по которой список
    и счётчик берут условия из одной функции: «какие заказы владелец видит глазами» —
    это один вопрос, и отвечать на него в двух местах по-разному нельзя.

    Второй элемент — не украшение. Заказы клиентов с неоконченной заявкой на удаление
    из списка вырезаны намеренно (PII ещё не вычищено), и послать владельца в
    «🧾 Заказы на разборе» за таким заказом — значит послать его в пустой экран.
    Порядок ТОТ ЖЕ, что у списка: иначе названный заказ окажется его последней
    строкой, а при двадцати с лишним застрявших — вообще за пределами экрана.

    🔴 Пункт 4.5 расширил разбор на `conflict`/`failed`/`reprice_required`, а сюда их
    НЕ пускает — намеренно, вопреки букве плана. Здесь отвечают не «кому нужен человек»,
    а «что именно держит замок на тарифе», и держит его только `operator_review`: три
    новых состояния входят в терминальный набор забора (`crud/tariff.py:52`), то есть
    забор их не видит. Пустив их сюда, мы назвали бы владельцу свежий остановившийся
    заказ (сортировка по `updated_at`) вместо настоящего блокировщика — и он закрыл бы
    не тот заказ, а отказ бы остался.
    """

    visible = await db.scalar(
        select(SubscriptionCheckout.public_id)
        .join(User, User.id == SubscriptionCheckout.user_id)
        .where(
            SubscriptionCheckout.tariff_id == tariff_id,
            *_operator_review_visible_conditions(frozenset({'operator_review'})),
        )
        .order_by(SubscriptionCheckout.updated_at.desc(), SubscriptionCheckout.id.desc())
        .limit(1)
    )
    if isinstance(visible, str) and visible:
        return visible[:8], True

    # Тот же вопрос без замка видимости: заказ есть, но его не показывают.
    hidden = await db.scalar(
        select(SubscriptionCheckout.public_id)
        .where(
            SubscriptionCheckout.tariff_id == tariff_id,
            SubscriptionCheckout.lifecycle_state == 'operator_review',
        )
        .order_by(SubscriptionCheckout.updated_at.desc(), SubscriptionCheckout.id.desc())
        .limit(1)
    )
    if isinstance(hidden, str) and hidden:
        return hidden[:8], False
    return None


async def count_operator_review_checkouts(db: AsyncSession) -> int:
    """Сколько ждёт разбора. Считается ТЕМ ЖЕ условием, что и список."""
    return int(
        await db.scalar(
            select(func.count(SubscriptionCheckout.id))
            .join(User, User.id == SubscriptionCheckout.user_id)
            .where(*_operator_review_visible_conditions())
        )
        or 0
    )


async def operator_review_card(db: AsyncSession, checkout: SubscriptionCheckout) -> str:
    """Карточка заказа для оператора.

    🔴 Намеренно ТОТ ЖЕ текст, что уходит тревогой владельцу (`_owner_order_stuck_text`).
    ⚠️ С пункта 4.5 это верно лишь наполовину: тревога рассылается только по
    `operator_review`, поэтому у трёх новых состояний этот текст живёт ТОЛЬКО как карточка.
    Две причины: во-первых, один текст — одно место правки; во-вторых, тревога в чате —
    это снимок момента (мина M), а карточка собирается заново при каждом открытии, то
    есть оператор всегда видит живое состояние заказа, даже если тревоге неделя.
    """
    return await _owner_order_stuck_text(db, checkout)


async def refundable_amount_kopeks(db: AsyncSession, checkout: SubscriptionCheckout) -> tuple[int, str]:
    """Сколько можно вернуть и почему. Второе значение — причина отказа, если сумма 0.

    🔴 Возвращаем ТОЛЬКО то, что база может доказать. Нельзя брать `tariff_total_kopeks`:
    клиент мог заплатить другую сумму (частичная оплата, курс, комиссия провайдера), и
    возврат «по прайсу» — это выдача чужих денег.
    """
    # 🔴 ЗАБОР 1 — отзыв платежа. `credited_amount_kopeks` при chargeback НЕ обнуляется
    # (это зафиксировано в `checkout_money_state` выше), поэтому без этой проверки кнопка
    # вернула бы деньги, которые платёжная система уже забрала у нас и отдала клиенту.
    # Тот же вопрос оба канонических вердикта задают ПЕРВЫМ — задаём и мы.
    if str(checkout.terminal_reason or '').startswith(_POST_PAID_REVERSAL_PREFIX):
        return 0, 'платёж отозван платёжной системой — она уже вернула деньги клиенту'
    # 🔴 ЗАБОР 2 — подписка по заказу уже создана. `debit_transaction_id` и
    # `created_subscription_id` присваиваются ОДНИМ блоком (`:1571-1575` и `:2127-2131`),
    # сразу после `create_paid_subscription`/`extend_subscription`. Значит у заказа с
    # оплатой с баланса, дошедшего до списания, подписка есть всегда, и возврат сделал бы
    # её бесплатной. Этот же забор ловит провайдерский заказ, застрявший на ВЫДАЧЕ:
    # деньги там наши, но лечится это выдачей, а не возвратом.
    if checkout.created_subscription_id is not None:
        return 0, 'подписка по заказу уже создана — возврат сделал бы её бесплатной'
    if checkout.funding_mode == 'wallet':
        # Досюда кошельковый заказ доходит только без списания: с ним забор 2 уже сработал.
        return 0, 'с баланса ничего не списывали — возвращать нечего'
    # 🔴 ЗАБОР 3 — аккаунт закрывается. `_fence_account_erasure_payment` ставит ровно ту
    # пару признаков, по которой мы считаем сумму, и его докстринг требует обратного:
    # «reviewable, never creditable». Деньги на баланс удаляемого аккаунта — это ещё и
    # тихой перевод ворот удаления, которые смотрят на наличие баланса.
    owner = await db.get(User, checkout.user_id)
    if owner is not None and (owner.account_erased_at or owner.account_erasure_requested_at):
        return 0, 'аккаунт клиента удаляется — на такой баланс зачислять нельзя, возврат делается в Platega'
    # 🔴 ЗАБОР 4 — оператор уже решил судьбу денег в кабинете. Строка сверки со статусом
    # `resolved` означает «решение принято»; если это был возврат в Platega, вторая
    # выплата здесь отдала бы те же деньги дважды (мина L).
    resolved_credit = await db.scalar(
        select(func.count(DeviceFirstReconciliationCredit.id)).where(
            DeviceFirstReconciliationCredit.checkout_id == checkout.id,
            DeviceFirstReconciliationCredit.status == 'resolved',
        )
    )
    if resolved_credit:
        return 0, 'по этому платежу уже записано решение оператора в кабинете — проверьте его, прежде чем платить'
    # Провайдерская оплата. `credited_amount_kopeks > 0` живёт при трёх статусах попытки,
    # и вернуть можно только удержанное (`operator_review`): при `credited` деньги уже
    # лежат у клиента на балансе, при `paid_processing` заказ ещё в штатной выдаче.
    held = await db.scalar(
        select(func.sum(CheckoutPaymentAttempt.credited_amount_kopeks)).where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.status == 'operator_review',
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
        )
    )
    if held:
        return int(held), ''
    already_credited = await db.scalar(
        select(func.count(CheckoutPaymentAttempt.id)).where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.status == 'credited',
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
        )
    )
    if already_credited:
        return 0, 'деньги уже вернулись клиенту на баланс — второй раз не нужно'
    return 0, 'в базе нет подтверждённого списания. Проверьте платёж в кабинете Platega и вернитесь'


async def refund_operator_review_checkout(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    admin_user_id: int,
) -> tuple[bool, str]:
    """Вернуть деньги заказа на баланс клиента. Идемпотентно по ключу книги."""
    # Сторож состояния — такой же, как у закрытия. Инлайн-кнопка живёт в переписке
    # Telegram вечно, поэтому старая карточка остаётся рабочей кнопкой возврата ещё
    # долго после того, как заказ уехал из разбора.
    # 🔴 Набор ТОТ ЖЕ, что у видимости: план 4.5 про этот сторож забыл, а без него кнопка
    # возврата рисовалась бы на новых состояниях и отказывала — ровно мёртвая кнопка.
    if checkout.lifecycle_state not in OPERATOR_REVIEWABLE_STATES:
        return False, f'Заказ уже не на разборе (сейчас «{checkout.lifecycle_state}») — возврат не сделан.'
    ledger_key = f'{OPERATOR_REFUND_LEDGER_PREFIX}:{checkout.id}'
    existing = (
        await db.execute(select(Transaction).where(Transaction.device_first_ledger_key == ledger_key).with_for_update())
    ).scalar_one_or_none()
    if existing is not None:
        return False, f'Возврат уже сделан раньше: {settings.format_price(int(existing.amount_kopeks))}.'
    amount_kopeks, refusal = await refundable_amount_kopeks(db, checkout)
    if amount_kopeks <= 0:
        return False, f'Возврат не сделан: {refusal}.'
    # 🔴 Блокировка строки клиента ОБЯЗАТЕЛЬНА: `db.get` — обычный SELECT, а `+=` ниже
    # это read-modify-write. Параллельное пополнение в этом окне потерялось бы целиком.
    # Канонический путь возврата (`device_first_payment_service`) держит весь финансовый
    # граф под `FOR UPDATE` — здесь нужен хотя бы владелец баланса.
    user = (
        await db.execute(
            select(User).where(User.id == checkout.user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        return False, 'Возврат не сделан: карточка клиента недоступна.'
    user.balance_kopeks += amount_kopeks
    transaction = Transaction(
        user_id=user.id,
        type=TransactionType.DEPOSIT.value,
        amount_kopeks=amount_kopeks,
        description=f'Возврат по заказу {checkout.public_id} (разбор оператором)',
        payment_method='manual',
        device_first_checkout_id=checkout.id,
        device_first_ledger_key=ledger_key,
        is_completed=True,
    )
    db.add(transaction)
    await db.flush()
    # 🔴 Пометить попытки ОБЯЗАТЕЛЬНО, и вот почему. У возврата поздних денег свой ключ
    # книги (`direct_late_invoice:{attempt.id}`), и он про наш ключ ничего не знает.
    # Provider повторяет одно и то же подписанное подтверждение — код это прямо
    # признаёт. Без пометки повторный вебхук после закрытия заказа зачислил бы ту же
    # сумму ВТОРОЙ раз. Пара «`credited` + причина» — это ровно тот ранний выход,
    # который уже есть в платёжном сервисе, и мы попадаем в него.
    await db.execute(
        update(CheckoutPaymentAttempt)
        .where(
            CheckoutPaymentAttempt.checkout_id == checkout.id,
            CheckoutPaymentAttempt.status == 'operator_review',
            CheckoutPaymentAttempt.credited_amount_kopeks > 0,
        )
        .values(status='credited', reconciliation_reason=OPERATOR_REFUND_RECONCILIATION_REASON)
    )
    # Строка сверки — то место, где кабинет показывает «ждёт решения». Оставить её
    # открытой значит позвать второго оператора вернуть те же деньги ещё раз.
    await db.execute(
        update(DeviceFirstReconciliationCredit)
        .where(
            DeviceFirstReconciliationCredit.checkout_id == checkout.id,
            DeviceFirstReconciliationCredit.status == 'operator_review',
        )
        .values(
            status='resolved',
            resolution='transfer_to_wallet',
            resolved_by_user_id=admin_user_id,
            resolved_at=datetime.now(UTC),
        )
    )
    # 🔴 Мина U. Прямая запись `Transaction` минует аутбокс, а он раздаёт побочные эффекты
    # пополнения: событие для кабинета и внешних вебхуков, проверку промогруппы.
    # ⚠️ Сообщения в Telegram аутбокс НЕ шлёт — подписчиков у события в проекте ноль
    # (проверено: `event_emitter.on(` не вызывается нигде). Прежний комментарий здесь это
    # утверждал, и это было неправдой. Клиента уведомляет обработчик кнопки, у него есть бот.
    job = await ensure_deposit_outbox(db, transaction_id=transaction.id, checkout_id=checkout.id)
    # 🔴 А вот реферальную комиссию с ВОЗВРАТА платить нельзя: покупки не состоялось,
    # деньги просто вернулись владельцу. Шаг комиссии пропускается только статусом
    # `done` — воркер смотрит `!= 'done'`. Уведомление при этом остаётся: у него свой шаг.
    job.referral_status = 'done'
    await db.commit()
    logger.warning(
        'device_first_operator_refund',
        checkout_id=checkout.public_id,
        amount_kopeks=amount_kopeks,
        admin_user_id=admin_user_id,
    )
    return True, f'Возвращено на баланс: {settings.format_price(amount_kopeks)}.'


async def close_operator_review_checkout(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    admin_user_id: int,
) -> tuple[bool, str]:
    """Закрыть заказ. Это снимает с клиента замок — какой именно, зависит от состояния."""
    if checkout.lifecycle_state not in OPERATOR_REVIEWABLE_STATES:
        return False, f'Заказ уже не на разборе (сейчас «{checkout.lifecycle_state}»).'
    # 🔴 Спросить ДО мутации: сразу после неё состояние станет `cancelled`, и ответ
    # оператору был бы про закрытый заказ, а не про тот, который он закрывал.
    unblocks = operator_close_unblocks(checkout)
    # Исходную причину затирать нельзя молча: по ней потом восстанавливают, что вообще
    # произошло, а в поле останется только «закрыл оператор». Пишем её в лог перед заменой.
    previous_reason = str(checkout.terminal_reason or '')
    checkout.lifecycle_state = 'cancelled'
    checkout.terminal_reason = OPERATOR_CLOSED_TERMINAL_REASON
    await db.commit()
    logger.warning(
        'device_first_operator_closed_checkout',
        checkout_id=checkout.public_id,
        admin_user_id=admin_user_id,
        previous_terminal_reason=previous_reason or None,
    )
    # 🔴 Обещать «и тариф разблокирован» здесь нельзя. Забор тарифа снимается, только
    # когда по нему не осталось НИ ОДНОГО неразобранного заказа, а их бывает несколько.
    return True, f'Заказ закрыт. {unblocks}'

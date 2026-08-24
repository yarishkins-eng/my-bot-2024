"""Platega adapter for device-first checkout.

The attempt is committed before the network call. An ambiguous timeout is
fail-closed and requires reconciliation; the same attempt never creates a
second provider invoice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    CheckoutPaymentAttempt,
    DeviceFirstProviderEvent,
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
    OPERATOR_CLOSED_TERMINAL_REASON,
    OPERATOR_REFUND_RECONCILIATION_REASON,
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
# Canonical GET returns method names, while the signed callback returns an
# integer. These are the explicit provider spellings we accept; an unfamiliar
# label is an identity failure, never a permissive match.
PLATEGA_CANONICAL_METHOD_CODES = {
    'SBPQR': 2,
    'SBP_QR': 2,
    'SBP': 2,
    # Card method 11 is the active Russian card-acquiring route in this bot.
    # ``CARDRU`` is retained for the provider's historical response spelling.
    'CARD': 11,
    'CARDRU': 11,
    'CARD_RU': 11,
    'CARDACQUIRING': 11,
    'CARD_ACQUIRING': 11,
    'CRYPTO': 13,
    'CRYPTOCURRENCY': 13,
}
logger = structlog.get_logger(__name__)
PENDING_ATTEMPT_STATUSES = frozenset({'creating', 'pending', 'paid_processing', 'reconciliation'})
PROVIDER_TERMINAL_STATUSES = frozenset({'FAILED', 'CANCELED', 'EXPIRED'})
POST_PAID_REVERSAL_STATUSES = frozenset({'CHARGEBACKED'})

# 🔴 Мина BO. Две из четырёх ветвей отбора воркера находят строку не по статусу, а по
# ПРИЧИНЕ: архивную — по префиксу, операторскую — по точному значению. То есть причина
# здесь не комментарий, а ключ, по которому строка возвращается в следующий проход.
# Воркер же писал в это поле исход опроса — и строка выпадала из пула навсегда.
# На боевом так выпало 8 попыток (id 11, 12, 14, 16, 17, 19, 21, 34), самая старая —
# просрочена на 5 суток; ни одну не трогали после наступления срока.
POOL_KEY_TERMINAL_PREFIX = 'provider_terminal:'
POOL_KEY_OPERATOR_REASON = 'provider_invoice_missing_or_elapsed_expiry'


def reason_keeps_attempt_in_pool(reason: str | None) -> bool:
    """Держит ли эта причина строку в пуле вечной сверки.

    🔴 Строго `startswith`, не `in`: `post_paid_provider_terminal:` — ДРУГАЯ причина
    (заморозка по чарджбэку, `payment/platega.py`), и принимать её за ключ пула значит
    запретить записать откат постоплаты.
    """
    text = str(reason or '')
    return text.startswith(POOL_KEY_TERMINAL_PREFIX) or text == POOL_KEY_OPERATOR_REASON


# Мины R и AD (пункт 4.5). Закрытый провайдером счёт остаётся под опросом НАВСЕГДА, и это
# намеренно: контракта финальности провайдер не даёт, а поздняя оплата обязана вернуться
# клиенту на баланс. Выключать опрос нельзя. Но интервал был плоский — «+6 часов» без
# потолка и без конца, то есть на каждую брошенную корзину четыре обращения к Platega в
# сутки и четыре транзакции с пятью блокировками строк, вечно и с накоплением
# (на боевом 19.08.2026 таких попыток 16). Счётчик наблюдений при этом уже писался и не
# читался нигде — читаем его: интервал удваивается от 6 часов до недели и там остаётся.
_TERMINAL_RECONCILE_BASE_HOURS = 6
_TERMINAL_RECONCILE_MAX_HOURS = 24 * 7


def note_provider_lookup_outcome(attempt: CheckoutPaymentAttempt, outcome: str) -> None:
    """Записать исход опроса, не разрушив ключ, по которому строка вернётся в пул.

    Для строки, которая держится в пуле причиной, исход в это поле не пишется — иначе
    следующий проход её не найдёт (мина BO). Вместо этого гасится частота.

    🔴 Гашение обязательно, и это не украшение. Общий откат воркера упирается в потолок
    60 минут, а выборка сортируется по сроку с `limit=20`: архивная строка, просроченная
    на часы, встаёт в очередь ВПЕРЕДИ живого счёта. Без гашения архив занимал бы все
    двадцать мест (на 20.08 в пуле 12 строк плюс 8 возвращаемых — ровно двадцать).
    ⚠️ Не «часами»: быстрый воркер (`device_first_recovery_service`) ходит раз в десять
    секунд, медленный (`monitoring_service`) — раз в час; задержка живого счёта
    измерялась бы секундами, а цена — сотнями лишних обращений к провайдеру в сутки.
    Здесь ставится тот же затухающий срок (6 ч → неделя), которым живут закрытые счета.

    🔴 Гасится ТОЛЬКО архив. Операторская причина держит строку в пуле, но это не архив,
    а холд по, возможно, ещё живому счёту: пока он длится, клиент не может оформить новую
    покупку, а выход из него даёт только опрос. Отодвинуть такую строку на шесть часов
    из-за одного таймаута значит запереть человека на шесть часов. Ей ключ сохраняем,
    частоту оставляем общую.

    🔴 Счётчик молчаний растёт ЗДЕСЬ, и это единственная рабочая форма. Две неверные,
    обе пойманы ревью 20.08.2026: читать `terminal_observations`, не увеличивая его, —
    он растёт только при настоящем терминальном ответе, значит на молчании стоит, и
    затухание вырождается в плоские шесть часов. Читать `reconcile_attempts` — он копит
    за всю жизнь строки и доходит до шести за первый же час, значит архивная строка
    прыгает СРАЗУ на недельный потолок (замер на боевом: у восьми выпавших он от 7 до
    401). Верно — считать сами молчания: тогда 6 ч → 12 → 24 → … → неделя честно.
    """
    if not reason_keeps_attempt_in_pool(attempt.reconciliation_reason):
        attempt.reconciliation_reason = outcome
        return

    if str(attempt.reconciliation_reason or '').startswith(POOL_KEY_TERMINAL_PREFIX):
        attempt.terminal_observations = int(attempt.terminal_observations or 0) + 1
        attempt.next_reconcile_at = datetime.now(UTC) + terminal_reconcile_delay(attempt.terminal_observations)
    logger.warning(
        'direct_provider_lookup_unresolved_pool_key_preserved',
        attempt_id=attempt.id,
        outcome=outcome,
        reconciliation_reason=attempt.reconciliation_reason,
        reconcile_attempts=attempt.reconcile_attempts,
        terminal_observations=attempt.terminal_observations,
    )


def terminal_reconcile_delay(observations: int | None) -> timedelta:
    """6 ч → 12 → 24 → 48 → … → неделя, и не реже раза в неделю. Никогда не «хватит»."""
    steps = max(int(observations or 0) - 1, 0)
    hours = min(_TERMINAL_RECONCILE_BASE_HOURS * 2 ** min(steps, 10), _TERMINAL_RECONCILE_MAX_HOURS)
    return timedelta(hours=hours)


def _has_durable_direct_payment_binding(
    payment: PlategaPayment,
    attempt: CheckoutPaymentAttempt,
    checkout: SubscriptionCheckout,
) -> bool:
    """Verify the immutable direct-sale graph without relying on redacted JSON.

    ``PlategaPayment.metadata_json`` is operational data and is intentionally
    cleared when a financial account erasure is finalized.  It may therefore
    only be an additional live-account fence, never the sole proof that an
    erased historical payment belongs to its retained attempt.
    """
    return bool(
        payment.user_id is not None
        and checkout.user_id == payment.user_id
        and attempt.checkout_id == checkout.id
        and attempt.platega_payment_id == payment.id
        and attempt.provider == 'platega'
        and attempt.settlement_mode == DIRECT_SETTLEMENT_MODE
        and checkout.settlement_mode == DIRECT_SETTLEMENT_MODE
        and payment.platega_transaction_id
        and payment.platega_transaction_id == attempt.provider_payment_id
        and payment.amount_kopeks == attempt.requested_amount_kopeks
        and str(payment.currency or '').upper() == 'RUB'
        and str(attempt.currency or '').upper() == 'RUB'
        and payment.payment_method_code == attempt.provider_method_code
    )


async def _is_finalized_account_erasure(
    db: AsyncSession,
    *,
    user: User,
) -> bool:
    """Return true only for the irreversibly redacted account state.

    The request row is locked after Payment -> User -> Attempt -> Checkout,
    matching the closure service's lock order.  An ordinary deletion request
    is deliberately insufficient: its invoices must remain fail-closed.
    """
    if getattr(user, 'account_erased_at', None) is None:
        return False
    request = (
        await db.execute(
            select(AccountErasureRequest)
            .where(AccountErasureRequest.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return bool(request is not None and request.state == 'completed')


async def get_durable_direct_attempt_id(
    db: AsyncSession,
    *,
    payment_id: int,
) -> int | None:
    """Identify a retained Direct attempt when payment metadata was redacted.

    This is classification only; state-changing callers must still acquire the
    standard Payment -> User -> Attempt -> Checkout locks and validate every
    immutable financial field before doing anything.
    """
    return await db.scalar(
        select(CheckoutPaymentAttempt.id).where(
            CheckoutPaymentAttempt.platega_payment_id == payment_id,
            CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
        )
    )


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


def _provider_transaction_id(payload: dict | None) -> str:
    """Extract only the provider-owned immutable transaction identifier."""
    return str((payload or {}).get('transactionId') or (payload or {}).get('id') or '').strip()


def _safe_provider_redirect_url(payload: dict | None) -> str | None:
    """Accept an invoice redirect only when it is an absolute HTTPS URL."""
    redirect_url = PlategaService.parse_redirect_url(payload)
    if not redirect_url:
        return None
    parsed = urlsplit(redirect_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return None
    return redirect_url


def _provider_method_code(payload: dict | None) -> int | None:
    """Return Platega's numeric method code without guessing semantic labels."""
    raw_method = (payload or {}).get('paymentMethod')
    if raw_method is None:
        raw_method = (payload or {}).get('paymentMethodCode')
    # Platega's canonical GET documents a human-readable ``SBPQR`` while its
    # signed callback documents numeric method codes. Unknown labels remain
    # unknown and are fenced for review rather than guessed.
    if isinstance(raw_method, str):
        normalized = raw_method.strip().upper().replace('-', '_').replace(' ', '_')
        if normalized in PLATEGA_CANONICAL_METHOD_CODES:
            return PLATEGA_CANONICAL_METHOD_CODES[normalized]
    try:
        return int(raw_method) if raw_method is not None else None
    except (TypeError, ValueError):
        return None


def _has_exact_direct_invoice_details(attempt: CheckoutPaymentAttempt, payload: dict | None) -> bool:
    """Require immutable provider identity, method, amount and currency."""
    verified = PlategaService.parse_amount_currency(payload)
    method_code = _provider_method_code(payload)
    return (
        _provider_transaction_id(payload) == str(attempt.provider_payment_id or '')
        and method_code == int(attempt.provider_method_code)
        and verified
        == (
            int(attempt.requested_amount_kopeks),
            'RUB',
        )
    )


def _redacted_provider_payload_hash(payload: dict | None, *, observed_status: str | None = None) -> str:
    """Hash only normalized financial evidence; never persist callback secrets."""
    verified = PlategaService.parse_amount_currency(payload)
    evidence = {
        'provider_payment_id': _provider_transaction_id(payload),
        'provider_status': str(observed_status or (payload or {}).get('status') or '').upper(),
        'provider_method_code': _provider_method_code(payload),
        'amount_kopeks': verified[0] if verified else None,
        'currency': verified[1] if verified else None,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _append_direct_provider_event(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    attempt: CheckoutPaymentAttempt,
    payload: dict[str, Any] | None,
    provider_status: str,
    source: str,
) -> None:
    """Append a redacted provider receipt, separate from settlement proof."""
    verified = PlategaService.parse_amount_currency(payload)
    provider_payment_id = _provider_transaction_id(payload) or str(attempt.provider_payment_id or '')
    if not provider_payment_id:
        return
    amount_kopeks, currency = verified if verified is not None else (None, None)
    normalized_status = str(provider_status or '').upper()
    payload_hash = _redacted_provider_payload_hash(payload, observed_status=normalized_status)
    existing_event = await db.scalar(
        select(DeviceFirstProviderEvent.id).where(
            DeviceFirstProviderEvent.attempt_id == attempt.id,
            DeviceFirstProviderEvent.source == source,
            DeviceFirstProviderEvent.provider_status == normalized_status,
            DeviceFirstProviderEvent.payload_hash == payload_hash,
        )
    )
    if existing_event is None:
        db.add(
            DeviceFirstProviderEvent(
                checkout_id=checkout.id,
                attempt_id=attempt.id,
                provider_payment_id=provider_payment_id,
                provider_status=normalized_status,
                source=source,
                amount_kopeks=amount_kopeks,
                currency=currency,
                method_key=attempt.method_key if verified is not None else None,
                payload_hash=payload_hash,
                authenticated=True,
                observed_at=datetime.now(UTC),
            )
        )


async def _release_direct_terminal_invoice(
    db: AsyncSession,
    *,
    attempt_id: int,
    payment_id: int,
    payload: dict[str, Any] | None,
    provider_status: str,
    source: str,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """Archive an exact terminal provider invoice and release a fresh quote.

    This is intentionally the only automatic release path after an external
    invoice exists. It records compact append-only provider evidence and keeps
    every later callback fenced by the original attempt. Any missing or
    conflicting immutable field becomes operator review instead of a reset.
    """
    normalized_status = str(provider_status or '').upper()
    if normalized_status not in PROVIDER_TERMINAL_STATUSES:
        raise ValueError('direct terminal release requires a provider terminal status')
    if not isinstance(attempt_id, int):
        return False

    # All direct provider transitions use the same lock order:
    # PlategaPayment → User → Attempt → Checkout.  Webhooks already start
    # from the provider payment, while checkout creation/fulfilment fence on
    # the user; keeping this order avoids an inverse P↔U wait during a late
    # callback and a concurrent fresh quote.
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None or payment.user_id is None:
        return False
    user = (await db.execute(select(User).where(User.id == payment.user_id).with_for_update())).scalar_one_or_none()
    if user is None:
        return False
    attempt_query = select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id)
    if lease_token is not None and lease_epoch is not None:
        attempt_query = attempt_query.where(
            CheckoutPaymentAttempt.lease_token == lease_token,
            CheckoutPaymentAttempt.lease_epoch == lease_epoch,
            CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
        )
    attempt = (
        await db.execute(attempt_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if attempt is None:
        return False
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if checkout is None:
        return False

    metadata_attempt_id = (payment.metadata_json or {}).get('device_first_attempt_id')
    durable_binding = _has_durable_direct_payment_binding(payment, attempt, checkout)
    metadata_binding = metadata_attempt_id == attempt.id
    finalized_erasure = False
    if durable_binding and not metadata_binding:
        finalized_erasure = await _is_finalized_account_erasure(db, user=user)
    if not durable_binding or (not metadata_binding and not finalized_erasure):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_payment_binding_mismatch'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'direct_payment_binding_mismatch'
        await db.commit()
        logger.error(
            'device_first_terminal_invoice_binding_mismatch',
            checkout_id=checkout.public_id,
            attempt_id=attempt.id,
            payment_id=payment.id,
        )
        return False

    # Final privacy redaction deliberately removed the transient JSON marker.
    # A later *canonical* exact terminal-unpaid observation is harmless: it
    # must not recreate metadata, turn the immutable history into review, or
    # emit a false binding-mismatch LogError.  Callback evidence reaches this
    # function only after a canonical GET, so a callback alone cannot use the
    # exception.
    if (
        finalized_erasure
        and not metadata_binding
        and source in {'poll', 'canonical_get'}
        and not payment.is_paid
        and _has_exact_direct_invoice_details(attempt, payload)
    ):
        return False

    if payment.is_paid:
        # A caller that observes this must take the explicit post-paid
        # reversal path. Never overwrite a paid settlement with an archive.
        return False
    # Keep the signed/canonical receipt even when its financial fields are
    # incomplete. The release gate below remains strictly ID+method+amount+RUB.
    await _append_direct_provider_event(
        db,
        checkout=checkout,
        attempt=attempt,
        payload=payload,
        provider_status=normalized_status,
        source=source,
    )
    # A callback is authenticated evidence, but it is not the canonical
    # account view.  In particular, a delayed terminal callback can race a
    # successful payment or a provider-side status correction.  It therefore
    # only wakes canonical reconciliation; only GET/poll may release a new
    # quote after an external invoice has been issued.
    if source == 'callback':
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'provider_terminal_callback_awaiting_canonical'
        attempt.next_reconcile_at = datetime.now(UTC)
        payment.status = 'VERIFYING'
        await db.commit()
        logger.info(
            'device_first_terminal_invoice_callback_queued_for_canonical_check',
            checkout_id=checkout.public_id,
            attempt_id=attempt.id,
            provider_status=normalized_status,
        )
        return False

    if not _has_exact_direct_invoice_details(attempt, payload):
        # Webhooks may deliberately be sparse.  A sparse *callback* is not
        # evidence that a new checkout is safe: schedule the authenticated
        # canonical GET first.  A sparse/conflicting canonical response,
        # however, is a real identity failure and requires an operator.
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'provider_terminal_identity_mismatch'
        payment.status = 'OPERATOR_REVIEW'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'provider_terminal_identity_mismatch'
        await db.commit()
        logger.error(
            'device_first_terminal_invoice_identity_mismatch',
            checkout_id=checkout.public_id,
            attempt_id=attempt.id,
            source=source,
        )
        return False

    attempt.status = 'failed'
    attempt.reconciliation_reason = f'provider_terminal:{normalized_status.lower()}'
    # Keep reconciling historical provider invoices at a low frequency.  A
    # late exact CONFIRMED is credited to the wallet, never applied to this
    # archived checkout.  We do not assume terminal-status finality that is
    # not documented by the provider.
    if source == 'poll' and checkout.lifecycle_state == 'cancelled':
        attempt.terminal_observations += 1
    # Мины R и AD: интервал растёт по числу наблюдений, а не стоит плоским (см. константы).
    attempt.next_reconcile_at = datetime.now(UTC) + terminal_reconcile_delay(attempt.terminal_observations)
    payment.status = normalized_status
    checkout.lifecycle_state = 'cancelled'
    checkout.quote_state = 'expired'
    checkout.funding_state = 'invoice_terminal'
    checkout.terminal_reason = f'provider_terminal:{normalized_status.lower()}'
    await db.commit()
    logger.info(
        'device_first_terminal_invoice_released',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
        provider_status=normalized_status,
        source=source,
    )
    return True


async def _hold_direct_invoice_for_review(
    db: AsyncSession,
    *,
    attempt_id: int,
    payment_id: int | None,
    reason: str,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """Fence an ambiguous direct invoice without retrying the provider POST.

    With a provider payment this follows the direct financial lock contract:
    Payment -> User -> Attempt -> Checkout. A worker that lost its lease must
    not overwrite a newer webhook decision.
    """
    payment = None
    if payment_id is not None:
        payment = (
            await db.execute(
                select(PlategaPayment)
                .where(PlategaPayment.id == payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if payment is None or payment.user_id is None:
            return False
        owner_id = payment.user_id
    else:
        attempt_stub = await db.get(CheckoutPaymentAttempt, attempt_id)
        if attempt_stub is None:
            return False
        owner_id = await db.scalar(
            select(SubscriptionCheckout.user_id).where(SubscriptionCheckout.id == attempt_stub.checkout_id)
        )
        if owner_id is None:
            return False
    await db.execute(select(User).where(User.id == owner_id).with_for_update())
    attempt_query = select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id)
    if lease_token is not None and lease_epoch is not None:
        attempt_query = attempt_query.where(
            CheckoutPaymentAttempt.lease_token == lease_token,
            CheckoutPaymentAttempt.lease_epoch == lease_epoch,
            CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
        )
    owned_attempt = (
        await db.execute(attempt_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if owned_attempt is None:
        return False
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == owned_attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is not None and (
        payment.user_id != owner_id
        or payment.id != owned_attempt.platega_payment_id
        or (payment.metadata_json or {}).get('device_first_attempt_id') != owned_attempt.id
    ):
        payment.status = 'OPERATOR_REVIEW'
        owned_attempt.status = 'operator_review'
        owned_attempt.reconciliation_reason = 'direct_payment_binding_mismatch'
        if checkout is not None:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = 'direct_payment_binding_mismatch'
        await db.commit()
        return False
    owned_attempt.status = 'operator_review'
    owned_attempt.reconciliation_reason = reason
    if payment is not None:
        payment.status = 'OPERATOR_REVIEW'
    if checkout is not None:
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = reason
    await db.commit()
    return True


async def _apply_direct_pending_provider_observation(
    db: AsyncSession,
    *,
    attempt_id: int,
    payment_id: int,
    payload: dict[str, Any],
    observed_after: datetime,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """Apply a canonical live-invoice observation without stale regression.

    A poll reads the provider outside the database transaction.  It must
    re-enter through the same P -> U -> A -> C lock fence as terminal and
    confirmed paths; otherwise an older ``PENDING`` answer could overwrite a
    newer callback that already settled or archived the payment.
    """
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None or payment.user_id is None:
        return False
    await db.execute(select(User).where(User.id == payment.user_id).with_for_update())
    attempt_query = select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id)
    if lease_token is not None and lease_epoch is not None:
        attempt_query = attempt_query.where(
            CheckoutPaymentAttempt.lease_token == lease_token,
            CheckoutPaymentAttempt.lease_epoch == lease_epoch,
            CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
        )
    attempt = (
        await db.execute(attempt_query.with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if attempt is None:
        return False
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if checkout is None:
        return False

    metadata_attempt_id = (payment.metadata_json or {}).get('device_first_attempt_id')
    if (
        checkout.user_id != payment.user_id
        or attempt.platega_payment_id != payment.id
        or metadata_attempt_id != attempt.id
    ):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_payment_binding_mismatch'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'direct_payment_binding_mismatch'
        await db.commit()
        return False

    # A local terminal/paid transition whose write is newer than the provider
    # request proves this PENDING payload is stale. Ignore it, never demote it.
    last_local_change = max(
        (
            value
            for value in (getattr(payment, 'updated_at', None), getattr(checkout, 'updated_at', None))
            if value is not None
        ),
        default=None,
    )
    if (
        last_local_change is not None
        and last_local_change > observed_after
        and (
            payment.is_paid
            or attempt.status in {'failed', 'paid_processing', 'credited', 'operator_review'}
            or checkout.lifecycle_state in {'cancelled', 'fulfilling', 'ready', 'operator_review'}
        )
    ):
        return False

    # If the canonical provider still says PENDING *after* our own terminal
    # transition, that is a real contradiction, not a stale poll response.
    if attempt.status == 'failed' and str(attempt.reconciliation_reason or '').startswith(POOL_KEY_TERMINAL_PREFIX):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'provider_terminal_status_regressed'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'provider_terminal_status_regressed'
        await db.commit()
        return False

    # Before this correction, a verified live invoice without Platega's
    # optional ``expiresIn`` field was put into operator review.  That is not
    # a financial mismatch: exact provider identity/method/amount/RUB was
    # already established and the one-invoice fence remains intact. Allow only
    # this precise historical state to be rechecked by canonical GET; every
    # other operator review remains fail-closed.
    recovering_missing_deadline = (
        attempt.status == 'operator_review'
        and attempt.reconciliation_reason == POOL_KEY_OPERATOR_REASON
        and checkout.lifecycle_state == 'operator_review'
        and checkout.terminal_reason == 'provider_invoice_missing_or_elapsed_expiry'
    )
    if (
        payment.is_paid
        or (attempt.status not in {'creating', 'pending', 'reconciliation'} and not recovering_missing_deadline)
        or (checkout.lifecycle_state != 'awaiting_funds' and not recovering_missing_deadline)
        or checkout.funding_state != 'invoice_pending'
        or checkout.fulfillment_state != 'not_started'
    ):
        return False
    if not _has_exact_direct_invoice_details(attempt, payload):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'provider_invoice_verification_mismatch'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'provider_invoice_verification_mismatch'
        await db.commit()
        return False
    observed_expiry = PlategaService.parse_expires_at(payload.get('expiresIn'))
    # The provider documents a duration (``expiresIn``), not an absolute
    # timestamp.  Anchor it on the first verified canonical observation and
    # never extend it later: a static TTL in repeated GET responses must not
    # keep an invoice alive forever.
    # ``checkout.expires_at`` is a pre-invoice quote TTL until this first
    # canonical observation. It must not shorten or otherwise masquerade as a
    # provider deadline; after this function succeeds we persist the canonical
    # deadline on ``payment`` and can safely apply the monotonic min rule.
    existing_expiry = payment.expires_at
    provider_expires_at = (
        min(existing_expiry, observed_expiry)
        if existing_expiry is not None and observed_expiry is not None
        else existing_expiry or observed_expiry
    )
    # A missing deadline is not a hold: Platega has confirmed the exact invoice
    # is PENDING/INPROGRESS but does not expose its TTL. Keep one durable
    # invoice and poll its canonical status.
    #
    # 🔴 Мина F. Истёкший срок счёта — это БРОШЕННАЯ КОРЗИНА, а не авария, и здесь есть
    # всё, чтобы одно отличить от другого: счёт опознан точно (`_has_exact_direct_invoice_details`
    # выше), денег нет (`payment.is_paid` ложно, иначе вышли бы на :661), а срок назвала
    # САМА Platega (`expiresIn`, :676 и :718) и он уже прошёл. Разбирать тут нечего: оба
    # живых случая (заказы 32 и 34) закрылись сами, когда провайдер через ~6 часов отчитался
    # отменой. Прежний `operator_review` при этом запирал клиенту новую покупку
    # (`device_first_checkout_service.py:631-647`), запирал смену серверов тарифа
    # (`crud/tariff.py:320-357`) и слал владельцу ложную тревогу «ЗАКАЗ ЗАВИС».
    #
    # 🔴 Целевое состояние выбрано не произвольно: оно обязано совпасть с ручной отменой
    # (`abandon_direct_checkout_for_new_calculation`, `:1038-1045`), потому что
    #   · `cancelled` + `cancelled_by_user_after_invoice` — единственная пара, при которой
    #     ПОЗДНЯЯ оплата вернётся человеку на баланс (`:1976-1985`). Закрытие в `expired` или
    #     с новой причиной сломало бы возврат: сумма повисла бы кредитом сверки;
    #   · та же пара снимает забор триала (`trial_activation_service.py:298-299`);
    #   · 🔴 попытка ОБЯЗАНА уйти из `pending`, иначе главного мина F не сделает: забор смены
    #     серверов — это `or_` из двух ветвей (`crud/tariff.py:348-354`), и отмена заказа
    #     размыкает только первую. Вторая (`live_provider_attempt`, `:336-342`) смотрит мимо
    #     заказа, на `CheckoutPaymentAttempt.status in ('creating','pending','paid_processing')`
    #     (`:53`), — тариф остался бы заперт молча, до первой попытки владельца сменить серверы.
    # Название причины попытки отличается от ручной отмены намеренно (читателей у неё ноль,
    # проверено грепом по `app/`), но 🔴 считать мину F по базе им НЕЛЬЗЯ: когда провайдер
    # наконец отчитается отменой, `_release_direct_terminal_invoice` перезапишет и причину
    # попытки, и `terminal_reason` заказа (`:444-457`) — по живым случаям это ~6 часов.
    # Долговечный след ровно один: событие лога `device_first_direct_invoice_abandoned_after_
    # provider_deadline` ниже. Считать по нему.
    # `next_reconcile_at` намеренно не трогаем: у вызова из воркера бэкофф уже проставлен
    # (`:2283-2287`), у вызова при создании счёта стоит срок из `:1353`. В обоих случаях счёт
    # остаётся в выборке опроса (`:2140-2145`) — поздняя оплата не потеряется.
    if provider_expires_at is not None and provider_expires_at <= datetime.now(UTC):
        payment.status = 'VERIFYING'
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'provider_invoice_abandoned_after_expiry'
        checkout.lifecycle_state = 'cancelled'
        checkout.quote_state = 'expired'
        checkout.funding_state = 'invoice_abandoned'
        checkout.terminal_reason = 'cancelled_by_user_after_invoice'
        await db.commit()
        logger.info(
            'device_first_direct_invoice_abandoned_after_provider_deadline',
            checkout_id=checkout.public_id,
            attempt_id=attempt.id,
            payment_id=payment.id,
        )
        return False

    amount_kopeks, currency = PlategaService.parse_amount_currency(payload) or (0, '')
    attempt.provider_returned_amount_kopeks = amount_kopeks
    attempt.provider_returned_currency = currency
    attempt.status = 'pending'
    attempt.reconciliation_reason = None
    now = datetime.now(UTC)
    attempt.next_reconcile_at = (
        min(now + timedelta(minutes=2), provider_expires_at)
        if provider_expires_at is not None
        else now + timedelta(minutes=2)
    )
    payment.status = 'PENDING'
    if provider_expires_at is not None:
        payment.expires_at = provider_expires_at
        checkout.expires_at = provider_expires_at
    if recovering_missing_deadline:
        checkout.lifecycle_state = 'awaiting_funds'
        checkout.terminal_reason = None
    await db.commit()
    return True


async def _queue_direct_callback_for_canonical_reconciliation(
    db: AsyncSession,
    *,
    payment_id: int,
    payload: dict[str, Any],
    reason: str,
    attempt_id: int | None = None,
) -> bool:
    """Journal a direct callback without letting it project payment state.

    A callback can arrive before the create response has bound the provider
    transaction id, or after a canonical result has become newer.  In both
    cases the callback is valuable audit evidence but cannot be allowed to
    downgrade/terminalise the checkout.  It only schedules canonical GET.
    """
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None or payment.user_id is None:
        return False
    user = (await db.execute(select(User).where(User.id == payment.user_id).with_for_update())).scalar_one_or_none()
    if user is None:
        return False
    metadata_attempt_id = (payment.metadata_json or {}).get('device_first_attempt_id')
    attempt_id = attempt_id if isinstance(attempt_id, int) else metadata_attempt_id
    if not isinstance(attempt_id, int):
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()
        return False
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(CheckoutPaymentAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()
        return False
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    durable_binding = checkout is not None and _has_durable_direct_payment_binding(payment, attempt, checkout)
    metadata_binding = metadata_attempt_id == attempt.id
    finalized_erasure = False
    if durable_binding and not metadata_binding:
        finalized_erasure = await _is_finalized_account_erasure(db, user=user)
    if not durable_binding or (not metadata_binding and not finalized_erasure):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_payment_binding_mismatch'
        if checkout is not None:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = 'direct_payment_binding_mismatch'
        await db.commit()
        return False
    if finalized_erasure and not metadata_binding:
        # Finalized accounts retain no callback payload.  A signed callback is
        # not canonical settlement proof, so leave the already archived graph
        # untouched; the historical worker will still perform its canonical
        # GET according to its durable schedule.
        return True
    await _append_direct_provider_event(
        db,
        checkout=checkout,
        attempt=attempt,
        payload=payload,
        provider_status=str(payload.get('status') or '').upper(),
        source='callback',
    )
    if payment.is_paid or attempt.status in {'paid_processing', 'credited', 'operator_review'}:
        await db.commit()
        return False
    attempt.status = 'reconciliation'
    attempt.reconciliation_reason = reason
    attempt.next_reconcile_at = datetime.now(UTC)
    payment.status = 'VERIFYING'
    await db.commit()
    return True


async def _bind_direct_provider_identity(
    db: AsyncSession,
    *,
    attempt_id: int,
    payment_id: int,
    provider_payment_id: str,
    redirect_url: str | None,
) -> CheckoutPaymentAttempt | None:
    """Bind the POST response through the direct P -> U -> A -> C fence.

    The attempt/payment intent is persisted before POST so a callback may win
    the race with this response.  Binding therefore never writes a blind
    ``PENDING`` projection and always rechecks the fresh locked rows.
    """
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None or payment.user_id is None:
        return None
    await db.execute(select(User).where(User.id == payment.user_id).with_for_update())
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(CheckoutPaymentAttempt.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        return None
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(SubscriptionCheckout.id == attempt.checkout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if checkout is None or (
        checkout.user_id != payment.user_id
        or attempt.platega_payment_id != payment.id
        or (payment.metadata_json or {}).get('device_first_attempt_id') != attempt.id
        or attempt.settlement_mode != DIRECT_SETTLEMENT_MODE
    ):
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_payment_binding_mismatch'
        if checkout is not None:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = 'direct_payment_binding_mismatch'
        await db.commit()
        return None
    if attempt.provider_payment_id and attempt.provider_payment_id != provider_payment_id:
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'provider_identity_binding_conflict'
        checkout.lifecycle_state = 'operator_review'
        checkout.terminal_reason = 'provider_identity_binding_conflict'
        await db.commit()
        return None
    if payment.is_paid or attempt.status in {'paid_processing', 'credited', 'operator_review'}:
        # Nothing in create may overwrite a callback/worker that reached a
        # later financial transition while the HTTP POST was in flight.
        await db.commit()
        return attempt
    attempt.provider_payment_id = provider_payment_id
    payment.platega_transaction_id = provider_payment_id
    if redirect_url is not None:
        payment.redirect_url = redirect_url
        attempt.redirect_url = redirect_url
    attempt.status = 'reconciliation'
    attempt.reconciliation_reason = 'provider_invoice_verification_pending'
    attempt.next_reconcile_at = datetime.now(UTC)
    payment.status = 'VERIFYING'
    await db.commit()
    return attempt


async def abandon_direct_checkout_for_new_calculation(
    db: AsyncSession,
    *,
    checkout_public_id: str,
    user_id: int,
) -> SubscriptionCheckout | None:
    """Locally abandon one canonical pending invoice and release a new choice.

    This is deliberately a *local* customer decision, not a provider refund or
    void request.  The immutable attempt remains reconciled; an exact late
    CONFIRMED is credited once to the wallet and cannot fulfil this abandoned
    configuration.  It is only safe once the one provider invoice is fully
    bound and canonically observed as PENDING/INPROGRESS.  In particular, an
    in-flight POST or an identity-less reconciliation record is not
    cancellable: releasing a new configuration then could create two live
    provider invoices.  Lock order stays Payment -> User -> Attempt ->
    Checkout, never Checkout -> Payment.
    """
    # Resolve identity without locking joined rows. PostgreSQL's bare
    # ``FOR UPDATE`` on a join locks *all* relations and would effectively
    # acquire Attempt/Checkout before User. The actual lock sequence below is
    # the contractual Payment -> User -> Attempt -> Checkout.
    payment_id = await db.scalar(
        select(PlategaPayment.id)
        .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
        .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
        .where(
            SubscriptionCheckout.public_id == checkout_public_id,
            SubscriptionCheckout.user_id == user_id,
            CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
        )
        .order_by(CheckoutPaymentAttempt.id.desc())
        .limit(1)
    )
    if payment_id is None:
        return None
    payment = (
        await db.execute(
            select(PlategaPayment)
            .where(PlategaPayment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if payment is None:
        return None
    if payment.user_id != user_id:
        return None
    await db.execute(select(User).where(User.id == user_id).with_for_update())
    attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt)
            .where(CheckoutPaymentAttempt.platega_payment_id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if attempt is None:
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()
        return None
    checkout = (
        await db.execute(
            select(SubscriptionCheckout)
            .where(
                SubscriptionCheckout.id == attempt.checkout_id,
                SubscriptionCheckout.public_id == checkout_public_id,
                SubscriptionCheckout.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if checkout is None or attempt.settlement_mode != DIRECT_SETTLEMENT_MODE:
        payment.status = 'OPERATOR_REVIEW'
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = 'direct_checkout_abandon_binding_mismatch'
        await db.commit()
        return checkout
    if (
        payment.is_paid
        or attempt.status in {'paid_processing', 'credited', 'operator_review'}
        or checkout.fulfillment_state != 'not_started'
        or checkout.lifecycle_state not in {'draft', 'confirmed', 'awaiting_funds'}
    ):
        # A paid/fulfilling purchase must remain visible; only a live external
        # invoice can be superseded by a fresh user configuration.
        return checkout
    if (
        not attempt.platega_payment_id
        or not attempt.provider_payment_id
        or attempt.status != 'pending'
        or str(payment.status).upper() not in {'PENDING', 'INPROGRESS'}
        or checkout.lifecycle_state != 'awaiting_funds'
        or checkout.funding_state != 'invoice_pending'
    ):
        # Never turn an unbound/ambiguous provider POST into an opportunity to
        # create another invoice.  Canonical reconciliation owns this state.
        raise DeviceFirstError(
            'reconciliation_required',
            'Provider invoice must be reconciled before this checkout can be replaced',
        )

    checkout.lifecycle_state = 'cancelled'
    checkout.quote_state = 'expired'
    checkout.funding_state = 'invoice_abandoned'
    checkout.terminal_reason = 'cancelled_by_user_after_invoice'
    attempt.status = 'reconciliation'
    attempt.reconciliation_reason = 'provider_invoice_abandoned_by_user'
    attempt.next_reconcile_at = datetime.now(UTC)
    payment.status = 'VERIFYING'
    await db.commit()
    logger.info(
        'device_first_direct_invoice_locally_abandoned',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
        payment_id=payment.id,
    )
    return checkout


def _checkout_return_url(checkout_public_id: str, *, failed: bool = False) -> str | None:
    configured = settings.get_platega_failed_url() if failed else settings.get_platega_return_url()
    if not configured:
        return None
    parsed = urlsplit(configured)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    # 🔴 Этап В-1. У этой функции ЕСТЬ настройка, из которой она берёт только хост, — и на боевом
    # там стоит `https://t.me/teplo_VPN_bot`. Приклеив к нему свой путь, она собирала
    # `https://t.me/subscription/purchase?checkout=…` — ссылку, которая не ведёт никуда.
    # Возвращаем None: тогда платёжная система подставит настройку целиком (`create_platega_payment`
    # делает `return_url or get_platega_return_url()`), и человек попадёт хотя бы в чат с ботом.
    # ⚠️ Путь СПИТ: `create_checkout` жёстко ставит прямой режим, а все шесть точек вызова
    # `create_platega_attempt` пропускают только НЕ прямой. Разбудит его этап Б-3.
    if parsed.netloc.lower() == 't.me':
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
    transaction_id = _provider_transaction_id(response)
    redirect_url = _safe_provider_redirect_url(response)
    if not response or not transaction_id:
        # No provider identity means no future status lookup is possible.  Do
        # not make another POST: the worker escalates this durable ambiguity
        # to operator review after its grace period.
        attempt.status = 'reconciliation'
        attempt.reconciliation_reason = 'provider_response_missing_identity'
        payment.status = 'RECONCILIATION'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider invoice identity requires reconciliation')

    # POST and webhook are independent channels.  A callback may have been
    # recorded while this request was in flight, so bind the provider identity
    # under the same direct financial fence and never write ``PENDING`` here.
    # Only the following canonical GET can publish a live invoice.
    bound_attempt = await _bind_direct_provider_identity(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        provider_payment_id=transaction_id,
        redirect_url=redirect_url,
    )
    if bound_attempt is None:
        raise DeviceFirstError('reconciliation_required', 'Provider invoice requires operator review')
    attempt = bound_attempt

    if redirect_url is None:
        await _hold_direct_invoice_for_review(
            db,
            attempt_id=attempt.id,
            payment_id=payment.id,
            reason='provider_response_missing_safe_redirect',
        )
        raise DeviceFirstError('reconciliation_required', 'Provider invoice requires operator review')

    canonical_requested_at = datetime.now(UTC)
    try:
        details = await service.get_transaction(transaction_id)
    except Exception as error:
        attempt.reconciliation_reason = f'provider_status_lookup:{type(error).__name__}'
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider invoice is being verified') from error
    if not _has_exact_direct_invoice_details(attempt, details):
        await _hold_direct_invoice_for_review(
            db,
            attempt_id=attempt.id,
            payment_id=payment.id,
            reason='provider_invoice_verification_mismatch',
        )
        raise DeviceFirstError('reconciliation_required', 'Provider invoice requires operator review')

    provider_status = str((details or {}).get('status') or '').upper()
    if provider_status == 'CONFIRMED':
        # A paid canonical response is never a browser redirect. Reuse the
        # same strict identity/method/amount settlement path as a webhook; if
        # a field is missing or conflicts it becomes operator review instead.
        await settle_device_first_platega_payment(db, payment=payment, payload=details)
        return attempt
    if provider_status in PROVIDER_TERMINAL_STATUSES:
        released = await _release_direct_terminal_invoice(
            db,
            attempt_id=attempt.id,
            payment_id=payment.id,
            payload=details,
            provider_status=provider_status,
            source='canonical_get',
        )
        if released:
            # A terminal invoice is not a support-only error. Its historical
            # checkout has been archived and a new choice can start safely.
            raise DeviceFirstError('invoice_terminal', 'Provider invoice is no longer payable')
        raise DeviceFirstError('reconciliation_required', 'Provider invoice requires operator review')
    if provider_status not in {'PENDING', 'INPROGRESS'}:
        # An undocumented/incomplete state is not equivalent to a live
        # invoice. Keep the known identity in VERIFYING so only GET polling
        # can resolve it; no second provider POST is permitted.
        attempt.reconciliation_reason = f'provider_status:{provider_status or "unknown"}'
        payment.status = 'VERIFYING'
        attempt.next_reconcile_at = datetime.now(UTC)
        await db.commit()
        raise DeviceFirstError('reconciliation_required', 'Provider invoice is being verified')

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload=details,
        observed_after=canonical_requested_at,
    )
    if not applied:
        raise DeviceFirstError('reconciliation_required', 'Provider invoice is being verified')
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
    direct_payment = metadata.get('settlement_mode') == DIRECT_SETTLEMENT_MODE
    if not direct_payment and not isinstance(attempt_id, int):
        attempt_id = await get_durable_direct_attempt_id(db, payment_id=payment.id)
        direct_payment = attempt_id is not None

    # Direct-sale financial transitions have one non-negotiable order:
    # Payment -> User -> Attempt -> Checkout.  The payment is already locked
    # above; acquire the user before the attempt so a new quote, a webhook and
    # the reconciler cannot form an inverse P<->U lock cycle.
    if direct_payment:
        if not isinstance(attempt_id, int) or payment.user_id is None:
            payment.status = 'OPERATOR_REVIEW'
            await db.commit()
            logger.error('device_first_direct_payment_attempt_missing', payment_id=payment.id)
            return payment
        user = (await db.execute(select(User).where(User.id == payment.user_id).with_for_update())).scalar_one()
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
        if attempt is None:
            if lease_token is not None:
                logger.warning('device_first_direct_payment_lease_lost', payment_id=payment.id)
                return None
            payment.status = 'OPERATOR_REVIEW'
            await db.commit()
            logger.error('device_first_direct_payment_attempt_missing', payment_id=payment.id)
            return payment
        metadata_binding = metadata.get('device_first_attempt_id') == attempt.id
        checkout = (
            await db.execute(
                select(SubscriptionCheckout)
                .where(SubscriptionCheckout.id == attempt.checkout_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        # ``metadata_json`` is deliberately erased after a completed account
        # erasure, and it is never the financial authority in either case.
        # A present marker can route the payment here, but the immutable graph
        # still has to prove the exact direct-sale invoice before any balance
        # or subscription transition is possible.
        durable_binding = checkout is not None and _has_durable_direct_payment_binding(payment, attempt, checkout)
        finalized_erasure = (
            await _is_finalized_account_erasure(db, user=user) if durable_binding and not metadata_binding else False
        )
        if not durable_binding or (not metadata_binding and not finalized_erasure):
            payment.status = 'OPERATOR_REVIEW'
            attempt.status = 'operator_review'
            attempt.reconciliation_reason = 'direct_payment_attempt_mode_or_binding_mismatch'
            if checkout is not None:
                checkout.lifecycle_state = 'operator_review'
                checkout.terminal_reason = 'direct_payment_attempt_mode_or_binding_mismatch'
            await db.commit()
            logger.error('device_first_direct_payment_mode_mismatch', payment_id=payment.id, attempt_id=attempt.id)
            return payment
        return await _settle_direct_platega_payment_locked(
            db,
            payment=payment,
            attempt=attempt,
            checkout=checkout,
            user=user,
            payload=payload,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
            finalized_erasure=finalized_erasure,
        )

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
    if getattr(user, 'account_erasure_requested_at', None) is not None:
        return await _fence_account_erasure_payment(
            db,
            checkout=checkout,
            attempt=attempt,
            payment=payment,
            provider_payment_id=transaction_id,
            verified=verified,
        )
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


async def _fence_account_erasure_payment(
    db: AsyncSession,
    *,
    checkout: SubscriptionCheckout,
    attempt: CheckoutPaymentAttempt,
    payment: PlategaPayment,
    provider_payment_id: str,
    verified: tuple[int, str] | None,
    finalized_erasure: bool = False,
) -> PlategaPayment:
    """Keep a late payment reviewable, never creditable, after account closure.

    The old user row remains a financial anchor but no longer represents an
    accessible customer identity.  In particular, this must run before the
    historical "late invoice -> wallet" branch: a later Telegram account with
    the same ID is a new person/account for payment purposes.
    """
    attempt.status = 'operator_review'
    attempt.reconciliation_reason = (
        'account_erased_late_confirmed' if finalized_erasure else 'account_erasure_requested'
    )
    payment.status = 'OPERATOR_REVIEW'
    payment.is_paid = True
    # A finalized account must never reacquire a new JSON marker or payload.
    # The retained reconciliation credit/request is the privacy-safe audit
    # record for a later real payment.
    if not finalized_erasure:
        payment.metadata_json = {**(payment.metadata_json or {}), 'account_erasure_review': True}
    checkout.lifecycle_state = 'operator_review'
    checkout.terminal_reason = attempt.reconciliation_reason
    from app.services.account_erasure_service import invalidate_financial_resolution_for_late_payment

    await invalidate_financial_resolution_for_late_payment(db, payment.user_id)
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
    await db.commit()
    logger.warning(
        'device_first_account_erasure_payment_requires_operator_review',
        checkout_id=checkout.public_id,
        attempt_id=attempt.id,
        payment_id=payment.id,
    )
    return payment


async def _settle_direct_platega_payment_locked(
    db: AsyncSession,
    *,
    payment: PlategaPayment,
    attempt: CheckoutPaymentAttempt,
    checkout: SubscriptionCheckout,
    user: User,
    payload: dict | None,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
    finalized_erasure: bool = False,
) -> PlategaPayment:
    """Settle only an authenticated exact direct-sale provider payment.

    This function is called from the verified webhook/status-lookup path while
    the provider payment and attempt rows are locked.  Browser returns and
    client polling only observe the resulting checkout state.
    """
    provider_payment_id = str((payload or {}).get('id') or (payload or {}).get('transactionId') or '').strip()
    verified = _verified_amount(payload)
    identity_ok = bool(provider_payment_id and provider_payment_id == attempt.provider_payment_id)
    # Canonical Platega GET uses semantic strings (e.g. ``SBPQR``), while a
    # callback uses numeric codes.  The shared allow-list accepts only known
    # equivalences; unknown labels never become an identity match.
    method_ok = _provider_method_code(payload) == attempt.provider_method_code
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
    # A late exact confirmation after an already terminal provider invoice is
    # settled into the customer's wallet, never into the stale subscription.
    # The same signed/canonical confirmation may arrive again, so that credit
    # must be as idempotent as the ordinary paid-processing path.
    # 🔴 Вторая метка — пункт 4.4. Оператор вернул деньги кнопкой разбора, и у того
    # возврата СВОЙ ключ книги (`operator_review_refund:{checkout}`), про который ключ
    # `direct_late_invoice:{attempt}` ниже ничего не знает. Без этой метки повторное
    # подтверждение того же платежа прошло бы мимо обоих сторожей и зачислило сумму
    # второй раз. Метку ставит `refund_operator_review_checkout`.
    # 🔴 ПОРЯДОК БЛОКОВ НЕСУЩИЙ, и это не стиль (найдено ревью пункта 4.5). Проверка ниже
    # требует `mismatch_reason is None`, а `late_paid_direct_checkout` присваивается
    # СЛЕДУЮЩИМ блоком. Поднимите то присваивание выше — и повторное подтверждение по уже
    # возвращённому и закрытому заказу уедет в ветку позднего зачисления, у которой другой
    # ключ книги, то есть та же сумма будет выплачена дважды.
    if (
        mismatch_reason is None
        and payment.is_paid
        and attempt.status == 'credited'
        and attempt.reconciliation_reason in ('late_paid_wallet_credit', OPERATOR_REFUND_RECONCILIATION_REASON)
    ):
        return payment

    # ``settle_device_first_platega_payment`` already holds Payment -> User
    # -> Attempt -> Checkout.  It passes that locked checkout here so this
    # transition never reorders or reacquires the financial graph.
    if mismatch_reason is None and checkout.lifecycle_state not in {'awaiting_funds', 'fulfilling'}:
        mismatch_reason = 'late_paid_direct_checkout'

    # Record every normalized CONFIRMED observation, including a late or
    # mismatched one. The current payment/checkout rows remain projections;
    # this compact evidence is what explains why money was fulfilled or sent
    # to reconciliation instead.
    await _append_direct_provider_event(
        db,
        checkout=checkout,
        attempt=attempt,
        payload=payload,
        provider_status='CONFIRMED',
        source='settlement',
    )

    if getattr(user, 'account_erasure_requested_at', None) is not None:
        return await _fence_account_erasure_payment(
            db,
            checkout=checkout,
            attempt=attempt,
            payment=payment,
            provider_payment_id=provider_payment_id,
            verified=verified,
            finalized_erasure=finalized_erasure,
        )

    if mismatch_reason is not None:
        if (
            mismatch_reason == 'late_paid_direct_checkout'
            and verified is not None
            and checkout.lifecycle_state == 'cancelled'
            and (
                str(checkout.terminal_reason or '').startswith('provider_terminal:')
                or checkout.terminal_reason == 'cancelled_by_user_after_invoice'
                # Пункт 4.4. Заказ закрыл оператор кнопкой разбора. Деньги, пришедшие
                # после этого, обязаны вернуться клиенту на баланс ровно так же: без
                # этого члена они заново запрут его в `operator_review`, из которого мы
                # его только что вывели, и он снова не сможет оформить покупку.
                or checkout.terminal_reason == OPERATOR_CLOSED_TERMINAL_REASON
            )
        ):
            # The provider accepted money after it had authoritatively closed
            # the old invoice. Never fulfil that stale order, but do give the
            # exact, idempotent amount back to the owner as spendable wallet
            # credit so they can purchase their current configuration.
            amount_kopeks, currency = verified
            ledger_key = f'direct_late_invoice:{attempt.id}'
            transaction = (
                await db.execute(
                    select(Transaction).where(Transaction.device_first_ledger_key == ledger_key).with_for_update()
                )
            ).scalar_one_or_none()
            if transaction is None:
                user.balance_kopeks += amount_kopeks
                transaction = Transaction(
                    user_id=user.id,
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=amount_kopeks,
                    description=f'Late Platega invoice for checkout {checkout.public_id}',
                    payment_method='platega',
                    external_id=payment.platega_transaction_id or provider_payment_id,
                    device_first_checkout_id=checkout.id,
                    device_first_ledger_key=ledger_key,
                    is_completed=True,
                )
                db.add(transaction)
                await db.flush()
            payment.transaction_id = transaction.id
            payment.status = 'CONFIRMED'
            payment.is_paid = True
            payment.metadata_json = {
                **(payment.metadata_json or {}),
                'late_invoice_wallet_credit': True,
            }
            attempt.status = 'credited'
            attempt.credited_amount_kopeks = amount_kopeks
            attempt.reconciliation_reason = 'late_paid_wallet_credit'
            checkout.funding_state = 'late_paid_wallet_credit'
            checkout.terminal_reason = 'late_paid_wallet_credit'
            await db.commit()
            logger.warning(
                'device_first_late_archived_invoice_credited_to_wallet',
                checkout_id=checkout.public_id,
                attempt_id=attempt.id,
                amount_kopeks=amount_kopeks,
                currency=currency,
            )
            return payment

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
                CheckoutPaymentAttempt.status.in_(['creating', 'reconciliation']),
                CheckoutPaymentAttempt.provider_payment_id.is_(None),
            ),
            # Archive releases the customer, but the historical provider
            # invoice remains under low-frequency reconciliation forever. A
            # later exact confirmation is wallet credit, never stale VPN
            # fulfillment; without a documented provider-finality contract we
            # must not stop looking for it after an arbitrary number of polls.
            and_(
                CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
                CheckoutPaymentAttempt.status == 'failed',
                CheckoutPaymentAttempt.reconciliation_reason.like(f'{POOL_KEY_TERMINAL_PREFIX}%'),
                CheckoutPaymentAttempt.provider_payment_id.is_not(None),
                CheckoutPaymentAttempt.platega_payment_id.is_not(None),
            ),
            # A prior build incorrectly treated an omitted provider TTL as a
            # financial hold. Re-open only this named, exact-proof state via
            # canonical GET; all other operator holds stay excluded.
            and_(
                CheckoutPaymentAttempt.settlement_mode == DIRECT_SETTLEMENT_MODE,
                CheckoutPaymentAttempt.status == 'operator_review',
                CheckoutPaymentAttempt.reconciliation_reason == POOL_KEY_OPERATOR_REASON,
                CheckoutPaymentAttempt.provider_payment_id.is_not(None),
                CheckoutPaymentAttempt.platega_payment_id.is_not(None),
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
        if (
            attempt.status in {'creating', 'reconciliation'}
            and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE
            and attempt.provider_payment_id is None
        ):
            try:
                held = await _hold_direct_invoice_for_review(
                    db,
                    attempt_id=attempt_id,
                    payment_id=attempt.platega_payment_id,
                    reason='provider_invoice_creation_incomplete',
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                if not held:
                    continue
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
            provider_request_started_at = datetime.now(UTC)
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
                note_provider_lookup_outcome(attempt, f'status_lookup:{type(error).__name__}')
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
                note_provider_lookup_outcome(attempt, 'status_lookup:empty')
                await db.commit()
                continue
            status = str(payload.get('status') or '').upper()
            payment = await db.get(PlategaPayment, attempt.platega_payment_id)
            if payment is None:
                continue
            if settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE and status in {'PENDING', 'INPROGRESS'}:
                await _apply_direct_pending_provider_observation(
                    db,
                    attempt_id=attempt_id,
                    payment_id=payment.id,
                    payload=payload,
                    # This is deliberately captured before the provider GET:
                    # a callback that wins while the HTTP request is in
                    # flight makes the PENDING answer stale and unwriteable.
                    observed_after=provider_request_started_at,
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                continue
            if status in PROVIDER_TERMINAL_STATUSES and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
                # This enters the central P -> U -> A -> C terminal fence.
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
                released = await _release_direct_terminal_invoice(
                    db,
                    attempt_id=attempt_id,
                    payment_id=payment.id,
                    payload=payload,
                    provider_status=status,
                    source='poll',
                    lease_token=lease_token,
                    lease_epoch=lease_epoch,
                )
                reconciled += int(released)
                continue
            if status in POST_PAID_REVERSAL_STATUSES and settlement_mode(attempt) == DIRECT_SETTLEMENT_MODE:
                # A chargeback is never an ordinary unpaid cancellation. It
                # freezes the direct sale for an accountable financial review,
                # regardless of whether an earlier worker already marked it paid.
                from app.services.payment.platega import PlategaPaymentMixin

                payment = (
                    await db.execute(
                        select(PlategaPayment)
                        .where(PlategaPayment.id == payment.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
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
                note_provider_lookup_outcome(attempt, f'provider_pending:{status or "unknown"}')
                await db.commit()
        finally:
            await _release_direct_attempt_lease(
                db,
                attempt_id=attempt_id,
                lease_token=lease_token,
                lease_epoch=lease_epoch,
            )
    return reconciled

"""Safe closure of accounts that have immutable Device-First payment evidence.

The ordinary ``deleted`` status is intentionally reversible: it is used for
inactive accounts and a signed Telegram login restores it.  A financial erasure
is different.  It first freezes access, then waits until every known provider
invoice is safe to close, and finally removes identity/authentication and VPN
credentials while retaining the minimal ledger graph required for a late
provider callback or a refund review.

No external RemnaWave call is made before the durable request is committed.
All financial locking follows the direct-sale order: payment -> user ->
attempt -> checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccountErasureRequest,
    CabinetRefreshToken,
    CheckoutPaymentAttempt,
    DeviceFirstDepositOutbox,
    DeviceFirstMutation,
    DeviceFirstNotificationOutbox,
    DeviceFirstOutbox,
    GuestPurchase,
    PlategaPayment,
    SavedPaymentMethod,
    Subscription,
    SubscriptionCheckout,
    Ticket,
    TicketNotification,
    Transaction,
    TransactionType,
    User,
    UserDeviceAlias,
    UserStatus,
)


logger = structlog.get_logger(__name__)

ERASURE_AWAITING_RECONCILIATION = 'awaiting_reconciliation'
ERASURE_AWAITING_MANUAL = 'awaiting_manual_resolution'
ERASURE_READY = 'ready_for_anonymization'
ERASURE_PANEL_RETRY = 'panel_cleanup_retry'
ERASURE_COMPLETED = 'completed'

FINANCIAL_RESOLUTION_CODES = frozenset(
    {'provider_terminal_verified', 'refund_completed', 'chargeback_resolved', 'balance_writeoff_approved'}
)


@dataclass(frozen=True)
class AccountErasureResult:
    """Product-facing outcome; ``completed`` means identity is now reusable."""

    state: str
    message: str
    completed: bool = False
    panel_deactivated: bool = False


@dataclass
class _ErasureContext:
    user: User
    request: AccountErasureRequest | None
    attempts: list[CheckoutPaymentAttempt]
    checkouts: list[SubscriptionCheckout]
    payments: list[PlategaPayment]
    subscriptions: list[Subscription]


def _queued_panel_cleanup_uuids(request: AccountErasureRequest | None) -> set[str]:
    """Return the durable list of panel identities still awaiting deletion."""
    return {str(value) for value in (getattr(request, 'panel_cleanup_uuids', None) or []) if value}


async def record_panel_cleanup_retry_for_financial_closure(*, user_id: int, panel_uuid: str) -> bool:
    """Durably hand a raced panel identity back to the closure worker.

    This deliberately uses a fresh transaction.  A caller may be inside an
    unrelated subscription/payment transaction, so committing its session
    merely to persist the cleanup UUID could publish unrelated state.
    """
    if not panel_uuid:
        return False

    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as cleanup_db:
        request = (
            await cleanup_db.execute(
                select(AccountErasureRequest).where(AccountErasureRequest.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if request is None:
            logger.error('panel_cleanup_retry_without_erasure_request', user_id=user_id, panel_uuid=panel_uuid)
            return False
        request.panel_cleanup_uuids = sorted(_queued_panel_cleanup_uuids(request) | {panel_uuid})
        request.panel_state = 'retry'
        # Completion remains an immutable account/privacy state.  The panel
        # cleanup retry is represented separately so the worker can repair a
        # late distributed race without reviving the erased identity.
        if request.state != ERASURE_COMPLETED:
            request.state = ERASURE_PANEL_RETRY
            request.resolution_code = 'panel_deactivation_failed'
        await cleanup_db.commit()
    return True


async def _lock_context(db: AsyncSession, *, user_id: int) -> _ErasureContext | None:
    """Load a consistent financial snapshot under the global payment lock order."""
    # A provider callback obtains this row first.  Selecting only the payment
    # table makes PostgreSQL lock exactly the intended relation, not joined
    # checkout rows in a different order.
    payments = (
        (
            await db.execute(
                select(PlategaPayment)
                .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
                .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                .where(SubscriptionCheckout.user_id == user_id)
                .with_for_update(of=PlategaPayment)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    user = (
        await db.execute(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        return None

    attempts = (
        (
            await db.execute(
                select(CheckoutPaymentAttempt)
                .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                .where(SubscriptionCheckout.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    checkouts = (
        (
            await db.execute(
                select(SubscriptionCheckout)
                .where(SubscriptionCheckout.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    request = (
        await db.execute(
            select(AccountErasureRequest)
            .where(AccountErasureRequest.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    subscriptions = (
        (
            await db.execute(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return _ErasureContext(
        user=user,
        request=request,
        attempts=attempts,
        checkouts=checkouts,
        payments=payments,
        subscriptions=subscriptions,
    )


def _has_live_subscription(subscriptions: list[Subscription]) -> bool:
    return any(
        getattr(subscription, 'actual_status', subscription.status) in {'active', 'trial', 'limited'}
        for subscription in subscriptions
    )


def _safe_terminal_attempt(attempt: CheckoutPaymentAttempt) -> bool:
    """Only a canonical, exact provider terminal result releases PII.

    A locally cancelled UI or a fixed polling count is intentionally not
    considered final: Platega may still send an exact CONFIRMED callback.
    """
    # Тот же признак, что держит строку в пуле сверки: берём его оттуда, а не своей копией.
    # Пока причина затёрта (мина BO), эта проверка даёт False, и заявка на удаление
    # аккаунта висит в `awaiting_reconciliation`, ожидая сверки, которой уже не будет.
    from app.services.device_first_payment_service import POOL_KEY_TERMINAL_PREFIX

    return attempt.status == 'failed' and str(attempt.reconciliation_reason or '').startswith(POOL_KEY_TERMINAL_PREFIX)


def _target_state(context: _ErasureContext) -> tuple[str, str | None]:
    if _has_live_subscription(context.subscriptions):
        return ERASURE_AWAITING_MANUAL, 'active_subscription'
    if int(context.user.balance_kopeks or 0) > 0:
        return ERASURE_AWAITING_MANUAL, 'positive_balance'

    payments_by_id = {payment.id: payment for payment in context.payments}
    for attempt in context.attempts:
        payment = payments_by_id.get(attempt.platega_payment_id)
        if attempt.status == 'operator_review' or (
            payment is not None
            and (
                payment.is_paid or str(getattr(payment, 'status', '') or '').upper() in {'CONFIRMED', 'OPERATOR_REVIEW'}
            )
        ):
            return ERASURE_AWAITING_MANUAL, 'paid_or_review_payment'
        if not _safe_terminal_attempt(attempt):
            return ERASURE_AWAITING_RECONCILIATION, 'provider_invoice_unresolved'
    return ERASURE_READY, None


def _message_for_state(state: str, resolution_code: str | None) -> str:
    if state == ERASURE_COMPLETED:
        return 'Аккаунт закрыт. Старые платёжные записи сохранены для финансовой сверки.'
    if state == ERASURE_READY:
        return 'Аккаунт закрывается: отключаем доступ к VPN и удаляем данные профиля.'
    if resolution_code == 'active_subscription':
        return 'Аккаунт закрыт для входа; активная подписка требует ручного завершения перед удалением данных.'
    if resolution_code == 'positive_balance':
        return 'Аккаунт закрыт для входа; положительный баланс требует ручного возврата или решения перед удалением данных.'
    if resolution_code == 'paid_or_review_payment':
        return 'Аккаунт закрыт для входа; платёж ожидает ручной финансовой сверки.'
    if resolution_code == 'legacy_financial_history':
        return 'Аккаунт закрыт для входа; архивные платёжные записи требуют ручной финансовой сверки.'
    return 'Аккаунт закрыт для входа. Проверяем ранее созданный счёт; новые платежи и новые заказы недоступны.'


def _legacy_requires_manual_resolution(request: AccountErasureRequest | None) -> bool:
    return bool(
        request is not None
        and getattr(request, 'has_legacy_financial_history', False)
        and getattr(request, 'financial_resolution_at', None) is None
    )


def _target_state_after_financial_resolution(
    context: _ErasureContext, request: AccountErasureRequest | None
) -> tuple[str, str | None]:
    """An explicit settlement is the only way to supersede a manual hold.

    A late Device-First or legacy callback clears ``financial_resolution_at``
    before this function can run, so the ordinary strict classifier resumes.
    """
    if request is not None and getattr(request, 'financial_resolution_at', None) is not None:
        return ERASURE_READY, None
    return _target_state(context)


async def request_financial_account_erasure(
    db: AsyncSession,
    *,
    user_id: int,
    requested_by_user_id: int | None,
    deactivate_panel: bool,
    has_legacy_financial_history: bool = False,
) -> AccountErasureResult:
    """Create/retry a safe erasure request without deleting financial evidence.

    ``deactivate_panel`` is retained only for compatibility with legacy admin
    callers. It is deliberately ignored for an account with checkout
    evidence: financial closure always removes the remote VPN identity.
    Leaving a live panel identity after local anonymisation would retain
    access while making the account impossible to administer safely.
    """
    if not deactivate_panel:
        logger.info('financial_account_erasure_forcing_panel_cleanup', user_id=user_id)
    deactivate_panel = True
    context = await _lock_context(db, user_id=user_id)
    if context is None:
        return AccountErasureResult(state='not_found', message='Пользователь не найден.')
    if (
        context.user.account_erased_at is not None
        and context.request is not None
        and context.request.panel_state == 'deactivated'
        and not _queued_panel_cleanup_uuids(context.request)
    ):
        return AccountErasureResult(
            state=ERASURE_COMPLETED,
            message=_message_for_state(ERASURE_COMPLETED, None),
            completed=True,
        )

    # Device-First has an exact provider reconciliation state machine. Older
    # providers do not share that proof, so their retained records require an
    # audited operator resolution before anonymisation. This is deliberately
    # fail-closed: a retry must never make an old pending invoice disappear.
    legacy_history = has_legacy_financial_history or bool(
        context.request and getattr(context.request, 'has_legacy_financial_history', False)
    )
    # A historical provider row has no shared canonical reconciliation
    # protocol.  Therefore every retry must keep it in the manual state until
    # an operator has recorded a settlement.  Do not derive safety from the
    # fact that the first delete already disabled the subscription: a second
    # click must never turn an unresolved legacy invoice into auto-erasure.
    legacy_requires_manual_resolution = legacy_history and (
        context.request is None or getattr(context.request, 'financial_resolution_at', None) is None
    )
    if legacy_requires_manual_resolution:
        state, resolution_code = ERASURE_AWAITING_MANUAL, 'legacy_financial_history'
    else:
        state, resolution_code = _target_state_after_financial_resolution(context, context.request)
    request = context.request
    now = datetime.now(UTC)
    if request is None:
        request = AccountErasureRequest(
            user_id=context.user.id,
            requested_by_user_id=requested_by_user_id,
            state=state,
            panel_state='pending' if deactivate_panel and state == ERASURE_READY else 'not_required',
            has_legacy_financial_history=legacy_history,
            resolution_code=resolution_code,
        )
        db.add(request)
    else:
        request.state = state
        request.has_legacy_financial_history = legacy_history
        request.resolution_code = resolution_code
        if deactivate_panel and state == ERASURE_READY and request.panel_state != 'deactivated':
            request.panel_state = 'pending'

    # Persist the fence before any RemnaWave side effect.  Existing tokens are
    # revoked in the same transaction; a signed Telegram launch must not
    # revive an account that asked to be closed.
    context.user.status = UserStatus.DELETED.value
    context.user.restriction_subscription = True
    context.user.restriction_topup = True
    context.user.restriction_reason = 'account_erasure_requested'
    # This happens in the *first* durable closure transaction, before any
    # provider/panel call.  A closing account may never enter either balance
    # autopay worker or a saved-card recurrent-charge worker while an operator
    # is reconciling its retained financial history.
    for subscription in context.subscriptions:
        subscription.status = 'disabled'
        subscription.autopay_enabled = False
    # The saved token is not immutable financial evidence.  Removing our local
    # reference stops every future YooKassa recurrent-charge attempt; provider
    # payment events are still retained in their respective ledger rows.
    await db.execute(delete(SavedPaymentMethod).where(SavedPaymentMethod.user_id == context.user.id))
    context.user.account_erasure_requested_at = context.user.account_erasure_requested_at or now
    await db.execute(delete(CabinetRefreshToken).where(CabinetRefreshToken.user_id == context.user.id))
    await db.commit()

    # Removing VPN access is mandatory for every financial closure, including
    # one that stays in manual financial reconciliation. The database fence is
    # already durable, so a panel webhook cannot revive the account.
    if request.panel_state != 'deactivated':
        if not await _remove_panel_identity(context):
            locked = await _lock_context(db, user_id=context.user.id)
            if locked is not None and locked.request is not None:
                locked.request.panel_state = 'retry'
                await db.commit()
            return AccountErasureResult(
                state=ERASURE_PANEL_RETRY,
                message='Аккаунт закрыт для входа. Доступ к VPN ещё отключается; повторная попытка выполнится безопасно.',
            )
        locked = await _lock_context(db, user_id=context.user.id)
        if locked is None or locked.request is None:
            return AccountErasureResult(state='not_found', message='Пользователь не найден.')
        locked.request.panel_state = 'deactivated'
        await db.commit()

    if state != ERASURE_READY:
        # The initial classifier runs before the durable fence.  Closing an
        # otherwise unfinanced active subscription disables its access in that
        # same fence transaction, so re-evaluate once rather than leaving an
        # unnecessary manual hold behind.  Balance, paid/reviewed invoices and
        # legacy history still remain fail-closed.
        refreshed = await _lock_context(db, user_id=context.user.id)
        if refreshed is None or refreshed.request is None:
            return AccountErasureResult(state='not_found', message='Пользователь не найден.')
        if _legacy_requires_manual_resolution(refreshed.request):
            refreshed_state, refreshed_reason = ERASURE_AWAITING_MANUAL, 'legacy_financial_history'
        else:
            refreshed_state, refreshed_reason = _target_state_after_financial_resolution(refreshed, refreshed.request)
        if refreshed_state == ERASURE_READY:
            return await _complete_ready_financial_account_erasure(
                db,
                user_id=context.user.id,
                deactivate_panel=True,
            )
        refreshed.request.state = refreshed_state
        refreshed.request.resolution_code = refreshed_reason
        await db.commit()
        logger.info(
            'financial_account_erasure_queued',
            user_id=context.user.id,
            state=refreshed_state,
            resolution_code=refreshed_reason,
        )
        return AccountErasureResult(
            state=refreshed_state,
            message=_message_for_state(refreshed_state, refreshed_reason),
        )

    return await _complete_ready_financial_account_erasure(
        db,
        user_id=context.user.id,
        deactivate_panel=deactivate_panel,
    )


async def _remove_panel_identity(context: _ErasureContext) -> bool:
    """Physically remove panel identity after the database-side payment fence.

    The retained financial evidence is local.  Removing the RemnaWave user is
    therefore compatible with audit retention and fulfils the documented
    meaning of the cabinet's full-delete action.  The webhook guard must be
    installed before the first remote delete to prevent a panel event from
    reviving or otherwise mutating the closing account.
    """
    panel_uuids = {
        value
        for value in [
            context.user.remnawave_uuid,
            *(subscription.remnawave_uuid for subscription in context.subscriptions),
            *_queued_panel_cleanup_uuids(context.request),
        ]
        if value
    }

    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.remnawave_webhook_service import RemnaWaveWebhookService
    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    # A POST may have reached RemnaWave while its HTTP response was lost, so
    # no local UUID exists to compensate.  Before final anonymisation we find
    # every identity by the stable identifiers that still exist on the closing
    # user.  A lookup error is fail-closed: do not erase local identity until
    # the panel can be proved empty.
    try:
        async with service.get_api_client() as api:
            if context.user.telegram_id is not None:
                panel_uuids.update(
                    item.uuid for item in await api.get_user_by_telegram_id(int(context.user.telegram_id)) if item.uuid
                )
            if context.user.email:
                panel_uuids.update(item.uuid for item in await api.get_user_by_email(context.user.email) if item.uuid)

            if not panel_uuids:
                if context.request is not None:
                    context.request.panel_cleanup_uuids = []
                return True

            RemnaWaveWebhookService.mark_intentional_panel_deletion(
                panel_uuids=sorted(panel_uuids),
                telegram_id=int(context.user.telegram_id) if context.user.telegram_id is not None else None,
            )
            ordered_uuids = sorted(panel_uuids)
            for index, panel_uuid in enumerate(ordered_uuids):
                try:
                    deleted = await api.delete_user(panel_uuid)
                except RemnaWaveAPIError as error:
                    if error.status_code == 404:
                        deleted = True
                    else:
                        raise
                if not deleted:
                    logger.warning('financial_account_erasure_panel_delete_returned_false', user_id=context.user.id)
                    if context.request is not None:
                        context.request.panel_cleanup_uuids = ordered_uuids[index:]
                    return False
    except Exception:
        logger.exception('financial_account_erasure_panel_delete_or_discovery_failed', user_id=context.user.id)
        if context.request is not None:
            context.request.panel_cleanup_uuids = sorted(panel_uuids)
        return False
    if context.request is not None:
        context.request.panel_cleanup_uuids = []
    return True


async def mark_late_legacy_payment_for_manual_review(db: AsyncSession, user: User) -> bool:
    """Stop post-topup side effects for an account that is being closed.

    Provider-specific handlers still preserve their verified payment payload.
    This common post-credit boundary makes the next step deterministic: no
    cart purchase, auto-extension, referral/notification chain or VPN access
    is resumed. PostgreSQL triggers in migration 0098 are the independent
    last-line fence for direct legacy balance/subscription writes.
    """
    if getattr(user, 'account_erasure_requested_at', None) is None:
        return False

    request = (
        await db.execute(
            select(AccountErasureRequest)
            .where(AccountErasureRequest.user_id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if request is not None:
        request.last_late_payment_blocked_at = datetime.now(UTC)
        _invalidate_financial_resolution(request, 'late_legacy_payment_callback')
        await db.commit()
    logger.warning('account_erasure_late_legacy_payment_fenced', user_id=user.id)
    return True


def _invalidate_financial_resolution(request: AccountErasureRequest, reason: str) -> None:
    """Make a newly observed payment win over an earlier operator decision."""
    if request.state == ERASURE_COMPLETED:
        return
    request.state = ERASURE_AWAITING_MANUAL
    request.resolution_code = reason
    request.financial_resolution_at = None
    request.financial_resolved_by_user_id = None
    request.financial_resolution_code = None
    request.financial_resolution_note = None


async def invalidate_financial_resolution_for_late_payment(db: AsyncSession, user_id: int) -> None:
    """Called by Device-First callback fencing while payment rows are locked."""
    request = (
        await db.execute(
            select(AccountErasureRequest)
            .where(AccountErasureRequest.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if request is not None:
        request.last_late_payment_blocked_at = datetime.now(UTC)
        _invalidate_financial_resolution(request, 'late_device_first_payment_callback')


async def _complete_ready_financial_account_erasure(
    db: AsyncSession,
    *,
    user_id: int,
    deactivate_panel: bool,
) -> AccountErasureResult:
    """Delete panel access then atomically anonymize a still-safe account."""
    if not deactivate_panel:
        logger.info('financial_account_erasure_completion_forcing_panel_cleanup', user_id=user_id)
    deactivate_panel = True
    context = await _lock_context(db, user_id=user_id)
    if context is None:
        return AccountErasureResult(state='not_found', message='Пользователь не найден.')
    request = context.request
    if request is None:
        return AccountErasureResult(state='not_found', message='Запрос на закрытие аккаунта не найден.')
    if (
        (context.user.account_erased_at is not None or request.state == ERASURE_COMPLETED)
        and request.panel_state == 'deactivated'
        and not _queued_panel_cleanup_uuids(request)
    ):
        return AccountErasureResult(
            state=ERASURE_COMPLETED,
            message=_message_for_state(ERASURE_COMPLETED, None),
            completed=True,
            panel_deactivated=request.panel_state == 'deactivated',
        )

    if _legacy_requires_manual_resolution(request):
        state, resolution_code = ERASURE_AWAITING_MANUAL, 'legacy_financial_history'
    else:
        state, resolution_code = _target_state_after_financial_resolution(context, request)
    if state != ERASURE_READY:
        request.state = state
        request.resolution_code = resolution_code
        await db.commit()
        return AccountErasureResult(state=state, message=_message_for_state(state, resolution_code))

    if deactivate_panel and request.panel_state != 'deactivated':
        request.panel_state = 'pending'
        await db.commit()
        if not await _remove_panel_identity(context):
            request = (
                await db.execute(
                    select(AccountErasureRequest)
                    .where(AccountErasureRequest.id == request.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            request.state = ERASURE_PANEL_RETRY
            request.panel_state = 'retry'
            request.resolution_code = 'panel_deactivation_failed'
            await db.commit()
            return AccountErasureResult(
                state=ERASURE_PANEL_RETRY,
                message='Аккаунт закрыт для входа. Доступ к VPN ещё отключается; повторная попытка выполнится безопасно.',
            )

    # A callback can arrive while the external call is in flight. Re-lock and
    # reclassify before redaction; if money was observed, do not race it.
    context = await _lock_context(db, user_id=user_id)
    if context is None or context.request is None:
        return AccountErasureResult(state='not_found', message='Пользователь не найден.')
    request = context.request
    if _legacy_requires_manual_resolution(request):
        state, resolution_code = ERASURE_AWAITING_MANUAL, 'legacy_financial_history'
    else:
        state, resolution_code = _target_state_after_financial_resolution(context, request)
    if state != ERASURE_READY:
        request.state = state
        request.resolution_code = resolution_code
        await db.commit()
        return AccountErasureResult(state=state, message=_message_for_state(state, resolution_code))

    # Invalidate all credentials/configuration while leaving the financial
    # graph (user -> checkout -> attempt -> provider event) immutable.
    for subscription in context.subscriptions:
        subscription.status = 'disabled'
        subscription.autopay_enabled = False
        subscription.subscription_url = None
        subscription.subscription_crypto_link = None
        subscription.remnawave_uuid = None
        subscription.remnawave_short_uuid = None
        subscription.connected_squads = []
        # The column is non-null + unique, so replace the old public suffix
        # with a deterministic non-identifying value instead of leaving a VPN
        # credential behind. The hyphen makes it disjoint from the ordinary
        # hexadecimal generator, avoiding a collision with a live account.
        subscription.remnawave_short_id = f'erased-{subscription.id:x}'

    # The terminal Device-First receipt keeps only identifiers, amounts,
    # currency, status and timestamps.  Redirect links and raw provider
    # payloads can carry reusable payment tokens or personal data, so they do
    # not belong in the retained financial ledger after completion.
    for payment in context.payments:
        payment.redirect_url = None
        payment.return_url = None
        payment.failed_url = None
        payment.payload = None
        payment.metadata_json = {}
        payment.callback_payload = {}
        payment.description = 'account erased; financial evidence retained'
    for attempt in context.attempts:
        attempt.redirect_url = None

    # These records are operational/user-content data, not immutable money
    # evidence. Leaving them reachable through the retained user FK would
    # undermine the meaning of final account erasure and could also let an
    # already-queued provision/notification run after access was disabled.
    checkout_ids = [checkout.id for checkout in context.checkouts]
    if checkout_ids:
        await db.execute(delete(DeviceFirstOutbox).where(DeviceFirstOutbox.checkout_id.in_(checkout_ids)))
        await db.execute(delete(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.checkout_id.in_(checkout_ids)))
        await db.execute(
            delete(DeviceFirstNotificationOutbox).where(DeviceFirstNotificationOutbox.checkout_id.in_(checkout_ids))
        )
    await db.execute(delete(DeviceFirstMutation).where(DeviceFirstMutation.owner_user_id == context.user.id))
    await db.execute(delete(TicketNotification).where(TicketNotification.user_id == context.user.id))
    await db.execute(delete(Ticket).where(Ticket.user_id == context.user.id))
    await db.execute(delete(UserDeviceAlias).where(UserDeviceAlias.user_id == context.user.id))

    # A landing order is retained as financial evidence, not as a second
    # identity store. Remove contacts, login/subscription secrets and
    # attribution identifiers, then leave only amount/status/provider fields
    # for reconciliation. The durable hold also makes a late guest webhook
    # consume the order without attempting to deliver it to a future account.
    await db.execute(
        update(GuestPurchase)
        .where(or_(GuestPurchase.user_id == context.user.id, GuestPurchase.buyer_user_id == context.user.id))
        .values(
            buyer_user_id=None,
            user_id=None,
            contact_value='erased-account',
            gift_recipient_value=None,
            gift_message=None,
            subscription_url=None,
            subscription_crypto_link=None,
            cabinet_password=None,
            auto_login_token=None,
            recipient_warning='account_erasure_financial_review',
            yandex_cid=None,
            subid=None,
            referrer=None,
        )
    )

    user = context.user
    user.telegram_id = None
    user.auth_type = 'erased'
    user.username = None
    user.first_name = None
    user.last_name = None
    user.email = None
    user.email_verified = False
    user.email_verified_at = None
    user.email_verification_source = None
    user.password_hash = None
    user.email_verification_token = None
    user.email_verification_expires = None
    user.password_reset_token = None
    user.password_reset_expires = None
    user.email_change_new = None
    user.email_change_code = None
    user.email_change_expires = None
    user.google_id = None
    user.yandex_id = None
    user.discord_id = None
    user.vk_id = None
    user.referral_code = None
    user.referred_by_id = None
    user.remnawave_uuid = None
    user.trojan_password = None
    user.vless_uuid = None
    user.ss_password = None
    user.pending_campaign_slug = None
    user.notification_settings = {}
    user.last_pinned_message_id = None
    user.status = UserStatus.DELETED.value
    user.account_erased_at = datetime.now(UTC)
    user.restriction_subscription = True
    user.restriction_topup = True
    user.restriction_reason = 'account_erased'

    request.state = ERASURE_COMPLETED
    request.panel_state = 'deactivated' if deactivate_panel else 'not_required'
    request.resolution_code = None
    request.finalized_at = user.account_erased_at
    await db.execute(delete(CabinetRefreshToken).where(CabinetRefreshToken.user_id == user.id))
    await db.commit()
    logger.info('financial_account_erasure_completed', user_id=user.id, request_id=request.id)
    return AccountErasureResult(
        state=ERASURE_COMPLETED,
        message=_message_for_state(ERASURE_COMPLETED, None),
        completed=True,
        panel_deactivated=deactivate_panel,
    )


async def resolve_financial_account_erasure(
    db: AsyncSession,
    *,
    user_id: int,
    resolved_by_user_id: int,
    resolution_code: str,
    resolution_note: str,
) -> AccountErasureResult:
    """Record an audited financial resolution and complete account closure.

    This is intentionally an explicit operator action, never a timeout. The
    caller confirms the provider's terminal/refund/chargeback outcome before
    identity redaction. The database callback fences stay enabled forever.
    """
    if resolution_code not in FINANCIAL_RESOLUTION_CODES:
        return AccountErasureResult(state='invalid_resolution', message='Недопустимый код финансового решения.')
    if not resolution_note or len(resolution_note.strip()) < 8:
        return AccountErasureResult(
            state='invalid_resolution', message='Укажите краткое основание финансового решения.'
        )

    context = await _lock_context(db, user_id=user_id)
    if context is None or context.request is None:
        return AccountErasureResult(state='not_found', message='Запрос на закрытие аккаунта не найден.')
    request = context.request
    if context.user.account_erased_at is not None or request.state == ERASURE_COMPLETED:
        return AccountErasureResult(
            state=ERASURE_COMPLETED, message=_message_for_state(ERASURE_COMPLETED, None), completed=True
        )
    current_state, current_reason = _target_state(context)
    requires_settlement = current_state == ERASURE_AWAITING_MANUAL or _legacy_requires_manual_resolution(request)
    if not requires_settlement:
        return AccountErasureResult(
            state='not_required', message='Для этого аккаунта ручная финансовая сверка не требуется.'
        )

    has_balance = int(context.user.balance_kopeks or 0) > 0
    has_active_subscription = _has_live_subscription(context.subscriptions)
    settlement_resolution_codes = {
        'refund_completed',
        'chargeback_resolved',
        'balance_writeoff_approved',
    }
    # ``provider_terminal_verified`` is proof of a negative provider outcome,
    # not settlement for a payment already observed as paid/reviewable.
    if (
        current_reason == 'paid_or_review_payment' or has_balance or has_active_subscription
    ) and resolution_code not in settlement_resolution_codes:
        return AccountErasureResult(
            state='settlement_required',
            message='Сначала зафиксируйте возврат, чарджбэк или согласованное списание баланса.',
        )

    now = datetime.now(UTC)
    # The PostgreSQL safety trigger rejects ordinary post-closure balance
    # credits. This short, transaction-local capability is used only by this
    # audited resolution command to book the compensating withdrawal below.
    await db.execute(text("SET LOCAL app.account_erasure_resolution = 'on'"))
    if has_balance:
        db.add(
            Transaction(
                user_id=context.user.id,
                type=TransactionType.WITHDRAWAL.value,
                amount_kopeks=int(context.user.balance_kopeks),
                description=f'Account closure settlement: {resolution_code}; {resolution_note.strip()}',
                is_completed=True,
                completed_at=now,
            )
        )
        context.user.balance_kopeks = 0
    for subscription in context.subscriptions:
        subscription.status = 'disabled'
        subscription.autopay_enabled = False

    request.financial_resolution_at = now
    request.financial_resolved_by_user_id = resolved_by_user_id
    request.financial_resolution_code = resolution_code
    request.financial_resolution_note = resolution_note.strip()
    request.state = ERASURE_READY
    request.resolution_code = None
    await db.commit()
    return await _complete_ready_financial_account_erasure(db, user_id=user_id, deactivate_panel=True)


async def process_pending_financial_account_erasures(db: AsyncSession, *, limit: int = 20) -> int:
    """Advance requests after canonical payment reconciliation, never by TTL."""
    request_ids = (
        (
            await db.execute(
                select(AccountErasureRequest.id)
                .where(
                    or_(
                        AccountErasureRequest.state.in_(
                            [ERASURE_AWAITING_RECONCILIATION, ERASURE_READY, ERASURE_PANEL_RETRY]
                        ),
                        AccountErasureRequest.panel_state.in_(['pending', 'retry']),
                    )
                )
                .order_by(AccountErasureRequest.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    completed = 0
    for request_id in request_ids:
        request = await db.get(AccountErasureRequest, request_id)
        if request is None:
            continue
        result = await request_financial_account_erasure(
            db,
            user_id=request.user_id,
            requested_by_user_id=request.requested_by_user_id,
            deactivate_panel=True,
        )
        completed += int(result.completed)
    return completed

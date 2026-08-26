"""Durable post-credit work for device-first provider deposits.

The provider deposit and this outbox row are committed together.  Financial
referral rewards are applied in one database transaction and keyed by the
source deposit, so a retry cannot award the same money twice.  Event delivery
is at-least-once and carries the immutable transaction id for consumer-side
deduplication.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_user_campaign_id
from app.database.crud.transaction import emit_transaction_side_effects
from app.database.models import (
    DeviceFirstDepositOutbox,
    PaymentMethod,
    ReferralEarning,
    SubscriptionCheckout,
    Transaction,
    TransactionType,
    User,
)
from app.services.referral_service import (
    _is_commission_limit_reached,
    calculate_referral_commission_percent,
    get_referral_reward_payment_count,
)


logger = structlog.get_logger(__name__)


async def ensure_deposit_outbox(
    db: AsyncSession,
    *,
    transaction_id: int,
    checkout_id: int,
    emit_deposit_event: bool = True,
    pay_referral: bool = True,
) -> DeviceFirstDepositOutbox:
    """Create the unique durable job before the provider settlement commit.

    `emit_deposit_event=False` — для прихода от банка по прямой продаже: событие
    «баланс пополнен» жёстко подписано типом DEPOSIT и способом PLATEGA (`_apply_event_step`),
    а при прямой оплате кошелёк не пополнялся вовсе. Выпустить его значило бы соврать о
    пополнении, которого не было, — вопреки комментарию у самого `PROVIDER_RECEIPT` в модели.

    `pay_referral=False` — аварийный выключатель (РФ-1 п.1.1). Проверяется ЗДЕСЬ, где
    обязательство перед партнёром возникает, а не в `_apply_referral_step`, где оно
    исполняется: выход из шага не помешает внешнему циклу пометить работу выполненной, и
    обратное включение программы ничего бы не доплатило.
    """
    existing = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.transaction_id == transaction_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = DeviceFirstDepositOutbox(
        transaction_id=transaction_id,
        checkout_id=checkout_id,
        event_status='pending' if emit_deposit_event else 'done',
        referral_status='pending' if pay_referral else 'done',
    )
    db.add(row)
    await db.flush()
    return row


async def _add_reward(
    db: AsyncSession,
    *,
    recipient: User,
    source: Transaction,
    amount_kopeks: int,
    ledger_suffix: str,
    description: str,
) -> Transaction | None:
    if amount_kopeks <= 0:
        return None
    ledger_key = f'deposit-side-effect:{source.id}:{ledger_suffix}'
    existing = (
        await db.execute(select(Transaction).where(Transaction.device_first_ledger_key == ledger_key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    recipient.balance_kopeks += amount_kopeks
    recipient.updated_at = datetime.now(UTC)
    reward = Transaction(
        user_id=recipient.id,
        type=TransactionType.REFERRAL_REWARD.value,
        amount_kopeks=amount_kopeks,
        description=description,
        device_first_checkout_id=source.device_first_checkout_id,
        device_first_ledger_key=ledger_key,
        is_completed=True,
        completed_at=datetime.now(UTC),
    )
    db.add(reward)
    await db.flush()
    return reward


async def _add_referral_earning(
    db: AsyncSession,
    *,
    referrer: User,
    referral: User,
    source: Transaction,
    amount_kopeks: int,
    reason: str,
    campaign_id: int | None,
) -> None:
    existing = (
        await db.execute(
            select(ReferralEarning.id).where(
                ReferralEarning.referral_transaction_id == source.id,
                ReferralEarning.reason == reason,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        ReferralEarning(
            user_id=referrer.id,
            referral_id=referral.id,
            amount_kopeks=amount_kopeks,
            reason=reason,
            referral_transaction_id=source.id,
            campaign_id=campaign_id,
        )
    )


async def _apply_referral_step(
    db: AsyncSession,
    *,
    job_id: int,
) -> None:
    """Apply every monetary referral effect and complete the step atomically."""
    job = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.id == job_id).with_for_update()
        )
    ).scalar_one()
    if job.referral_status == 'done':
        return
    source = (
        await db.execute(
            select(Transaction)
            .where(
                Transaction.id == job.transaction_id,
                # РФ-1 п.1.2: источником комиссии стал не только кошельковый депозит, но и
                # приход от банка по прямой продаже (`PROVIDER_RECEIPT`). Это ЕДИНСТВЕННОЕ
                # место, где тип источника проверяется, — всё остальное в шаге универсально.
                # ⛔ Списание за подписку (`SUBSCRIPTION_PAYMENT`) сюда попасть не может и не
                # должно: оно отрицательное и относится к тем же деньгам, что и приход.
                Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
                Transaction.is_completed.is_(True),
            )
            .with_for_update()
        )
    ).scalar_one()
    user_row = await db.get(User, source.user_id)
    if user_row is None:
        raise RuntimeError('credited deposit owner no longer exists')

    owner_ids = [source.user_id]
    if user_row.referred_by_id is not None:
        owner_ids.append(user_row.referred_by_id)
    locked_users = list(
        (
            await db.execute(
                select(User)
                .where(User.id.in_(sorted(set(owner_ids))))
                .order_by(User.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    users_by_id = {item.id: item for item in locked_users}
    user = users_by_id.get(source.user_id)
    if user is None:
        raise RuntimeError('credited deposit owner lock failed')

    if user.referred_by_id is None:
        if not user.has_made_first_topup:
            user.has_made_first_topup = True
        job.referral_status = 'done'
        job.updated_at = datetime.now(UTC)
        await db.commit()
        return

    referrer = users_by_id.get(user.referred_by_id)
    if referrer is None:
        raise RuntimeError('referrer no longer exists')

    campaign_id = await get_user_campaign_id(db, user.id)
    prior_reward_payments = await get_referral_reward_payment_count(db, referrer.id, user.id)
    commission_percent = await calculate_referral_commission_percent(
        db,
        referrer,
        is_first_payment=prior_reward_payments == 0,
    )
    commission_amount = int(source.amount_kopeks * commission_percent / 100) if commission_percent > 0 else 0
    qualifies_for_first_bonus = source.amount_kopeks >= settings.REFERRAL_MINIMUM_TOPUP_KOPEKS

    if not user.has_made_first_topup and qualifies_for_first_bonus:
        user.has_made_first_topup = True
        await db.execute(
            delete(ReferralEarning).where(
                ReferralEarning.user_id == referrer.id,
                ReferralEarning.referral_id == user.id,
                ReferralEarning.reason == 'referral_registration_pending',
            )
        )
        await _add_reward(
            db,
            recipient=user,
            source=source,
            amount_kopeks=settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS,
            ledger_suffix='referred-first-bonus',
            description='Бонус новичка за первую оплату',
        )
        inviter_bonus = settings.REFERRAL_INVITER_BONUS_KOPEKS + commission_amount
        await _add_reward(
            db,
            recipient=referrer,
            source=source,
            amount_kopeks=inviter_bonus,
            ledger_suffix='inviter-first-reward',
            description=(
                f'Награда за первую оплату реферала {user.full_name}: '
                f'{settings.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS)} фикс'
                f' + {commission_percent}% от {settings.format_price(source.amount_kopeks)}'
            ),
        )
        if inviter_bonus > 0:
            await _add_referral_earning(
                db,
                referrer=referrer,
                referral=user,
                source=source,
                amount_kopeks=inviter_bonus,
                reason='referral_first_topup',
                campaign_id=campaign_id,
            )
    elif commission_amount > 0 and not await _is_commission_limit_reached(db, referrer.id, user.id):
        await _add_reward(
            db,
            recipient=referrer,
            source=source,
            amount_kopeks=commission_amount,
            ledger_suffix='inviter-recurring-commission',
            description=(
                f'Комиссия {commission_percent}% с оплаты реферала {user.full_name}'
                f' ({settings.format_price(source.amount_kopeks)})'
            ),
        )
        await _add_referral_earning(
            db,
            referrer=referrer,
            referral=user,
            source=source,
            amount_kopeks=commission_amount,
            reason='referral_commission_topup',
            campaign_id=campaign_id,
        )

    job.referral_status = 'done'
    job.updated_at = datetime.now(UTC)
    await db.commit()


async def _apply_fulfillment_step(db: AsyncSession, *, job_id: int) -> None:
    """Resume exact-payment fulfillment after any post-credit crash."""
    job = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.id == job_id).with_for_update()
        )
    ).scalar_one()
    if job.fulfillment_status != 'pending':
        return
    checkout = await db.get(SubscriptionCheckout, job.checkout_id)
    if checkout is None:
        raise RuntimeError('credited checkout no longer exists')
    public_id = checkout.public_id
    user_id = checkout.user_id
    await db.commit()

    from app.services.device_first_checkout_service import fulfill_checkout

    result = await fulfill_checkout(db, public_id, user_id)

    job = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.id == job_id).with_for_update()
        )
    ).scalar_one()
    if result.fulfillment_state == 'fulfilled' or result.lifecycle_state in {
        'ready',
        'reprice_required',
        'conflict',
        'cancelled',
        'expired',
        'failed',
    }:
        job.fulfillment_status = 'done'
    elif result.lifecycle_state == 'awaiting_funds':
        job.fulfillment_status = 'action_required'
    else:
        raise RuntimeError(f'unexpected fulfillment state: {result.lifecycle_state}')
    job.updated_at = datetime.now(UTC)
    await db.commit()


async def _apply_event_step(db: AsyncSession, *, job_id: int) -> None:
    job = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.id == job_id).with_for_update()
        )
    ).scalar_one()
    if job.event_status == 'done':
        return
    transaction = await db.get(Transaction, job.transaction_id)
    if transaction is None:
        raise RuntimeError('credited deposit transaction no longer exists')
    await db.commit()

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

    job = (
        await db.execute(
            select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.id == job_id).with_for_update()
        )
    ).scalar_one()
    job.event_status = 'done'
    job.updated_at = datetime.now(UTC)
    await db.commit()


async def process_device_first_deposit_outbox(
    db: AsyncSession,
    *,
    limit: int = 20,
    transaction_id: int | None = None,
) -> int:
    """Claim retryable jobs and finish their independent durable steps."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=5)
    query = select(DeviceFirstDepositOutbox).where(
        or_(
            and_(
                DeviceFirstDepositOutbox.status.in_(['pending', 'retry']),
                DeviceFirstDepositOutbox.available_at <= now,
            ),
            and_(
                DeviceFirstDepositOutbox.status == 'processing',
                DeviceFirstDepositOutbox.updated_at <= stale_before,
            ),
        )
    )
    if transaction_id is not None:
        query = query.where(DeviceFirstDepositOutbox.transaction_id == transaction_id)
    rows = list(
        (await db.execute(query.order_by(DeviceFirstDepositOutbox.id).limit(limit).with_for_update(skip_locked=True)))
        .scalars()
        .all()
    )
    for row in rows:
        row.status = 'processing'
        row.attempts += 1
        row.updated_at = now
    await db.commit()

    processed = 0
    for claimed in rows:
        try:
            if claimed.fulfillment_status == 'pending':
                await _apply_fulfillment_step(db, job_id=claimed.id)
            if claimed.referral_status != 'done':
                await _apply_referral_step(db, job_id=claimed.id)
            if claimed.event_status != 'done':
                await _apply_event_step(db, job_id=claimed.id)
            row = await db.get(DeviceFirstDepositOutbox, claimed.id)
            row.status = 'done'
            row.last_error = None
            row.updated_at = datetime.now(UTC)
            await db.commit()
            processed += 1
        except Exception as error:
            await db.rollback()
            row = await db.get(DeviceFirstDepositOutbox, claimed.id)
            if row is None:
                logger.error(
                    'device_first_deposit_outbox_missing_after_error',
                    job_id=claimed.id,
                    error=str(error),
                )
                continue
            row.status = 'retry'
            row.last_error = f'{type(error).__name__}:{error}'
            row.available_at = datetime.now(UTC) + timedelta(minutes=min(60, 2 ** min(row.attempts, 6)))
            row.updated_at = datetime.now(UTC)
            await db.commit()
            logger.error(
                'device_first_deposit_side_effects_failed',
                job_id=row.id,
                transaction_id=row.transaction_id,
                attempts=row.attempts,
                error=row.last_error,
            )
    return processed

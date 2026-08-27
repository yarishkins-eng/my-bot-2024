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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_user_campaign_id
from app.database.crud.transaction import emit_transaction_side_effects
from app.database.models import (
    DeviceFirstDepositOutbox,
    DeviceFirstNotificationOutbox,
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
    settlement_mode: str | None = None,
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
        **({'settlement_mode': settlement_mode} if settlement_mode else {}),
    )
    db.add(row)
    await db.flush()
    return row


def _qualifies_for_first_payment_bonus(user: User, amount_kopeks: int) -> bool:
    """Идёт ли оплата по ветке «первая»: фикс другу и фикс+процент партнёру.

    🔴 Вынесено в общее место после ревью: экран расчёта долга (`plan_referral_debt`)
    повторял это условие своими словами, и разошлись бы они молча — экран показал бы одно,
    движок заплатил другое, а владелец нажал бы, глядя на неверное число.
    """
    return not user.has_made_first_topup and amount_kopeks >= settings.REFERRAL_MINIMUM_TOPUP_KOPEKS


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
    if _qualifies_for_first_payment_bonus(user, source.amount_kopeks):
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
                # Двоеточие несущее: письмо партнёру берёт расшифровку тем, что стоит после
                # него (найдено скептиком — без двоеточия расшифровка была пуста всегда).
                f'Комиссия с оплаты реферала {user.full_name}: '
                f'{commission_percent}% от {settings.format_price(source.amount_kopeks)}'
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

    # РФ-1 п.1.3: сказать партнёру о деньгах. Ставим строку в ту же очередь сообщений, что
    # обслуживает клиентские уведомления: у неё уже есть бот и защита «ровно один раз».
    # ⛔ Слать прямо отсюда нельзя: бота в этой цепочке нет ни в одном из трёх мест вызова —
    # отправка вернула бы «не вышло» молча, и партнёр снова остался бы без сообщения.
    # Строка ставится ВНУТРИ той же транзакции, что и деньги: не заплатили — не обещаем.
    await _queue_referral_reward_notification(db, checkout_id=job.checkout_id)

    job.referral_status = 'done'
    job.updated_at = datetime.now(UTC)
    await db.commit()


async def _queue_referral_reward_notification(db: AsyncSession, *, checkout_id: int) -> None:
    """Поставить сообщение о награде, если по этому заказу действительно платили."""
    from app.services.device_first_checkout_service import REFERRAL_REWARD_NOTIFICATION_TYPE

    paid = (
        await db.execute(
            select(Transaction.id)
            .where(
                Transaction.device_first_checkout_id == checkout_id,
                Transaction.type == TransactionType.REFERRAL_REWARD.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if paid is None:
        return
    existing = (
        await db.execute(
            select(DeviceFirstNotificationOutbox.id)
            .where(
                DeviceFirstNotificationOutbox.checkout_id == checkout_id,
                DeviceFirstNotificationOutbox.notification_type == REFERRAL_REWARD_NOTIFICATION_TYPE,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        DeviceFirstNotificationOutbox(
            checkout_id=checkout_id,
            notification_type=REFERRAL_REWARD_NOTIFICATION_TYPE,
        )
    )


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


# ---------------------------------------------------------------------------
# РФ-2: возврат исторического долга 02.08–26.08.2026
# ---------------------------------------------------------------------------

# 🔴 ПЕРЕЧЕНЬ ЗАМОРОЖЕН, И ЭТО НЕСУЩЕЕ РЕШЕНИЕ, А НЕ ЛЕНЬ.
# Запрос «приходы без наградной строки» подхватил бы ОДНУ лишнюю оплату — 387 от 27.08:
# она прошла при намеренно ВЫКЛЮЧЕННОЙ программе и по правилу владельца не оплачивается
# никогда (мина FO), а от честного долга в базе неотличима. (Оплата 383 того же дня уже
# оплачена и из такого запроса выпадает сама — проверено прогоном запроса на боевом,
# первая редакция этого комментария завышала до «двух».) Каждая строка ниже сверена с боевой базой 27.08.2026 поимённо:
# приход, заказ, покупатель, пригласивший, сумма.
REFERRAL_DEBT_2026_08: tuple[tuple[int, int, int, int, int], ...] = (
    # (приход, заказ, покупатель, пригласивший, сумма прихода в копейках)
    (193, 14, 172, 133, 199000),
    (207, 30, 182, 131, 64900),
    (232, 39, 194, 123, 199000),
    (237, 43, 206, 142, 36900),
    (370, 57, 321, 133, 119900),
)

# Пакет платит ровно эту сумму или не платит ничего. 40 000 бонусов друзьям + 194 925 партнёрам.
REFERRAL_DEBT_2026_08_TOTAL_KOPEKS = 234925

_DEBT_LEDGER_SUFFIXES = ('referred-first-bonus', 'inviter-first-reward', 'inviter-recurring-commission')


async def _debt_credited_kopeks(db: AsyncSession, *, transaction_id: int) -> dict[str, int]:
    """Сколько начислено по этому приходу — по КНИГЕ, врозь получателям.

    Возврат воркера читать нельзя: работу мог перехватить фоновый цикл, и тогда
    `processed=0` означает «уже сделано», а не «не сделано».

    Врозь — потому что общая сумма приписала бы партнёру ещё и бонус новичка: на оплате
    1 990 ₽ экран сказал бы «партнёру 697,50 ₽» вместо 597,50 ₽, и владелец завысил бы
    ответ живому человеку на сотню.
    """
    credited = {'friend': 0, 'referrer': 0}
    keys = {
        f'deposit-side-effect:{transaction_id}:referred-first-bonus': 'friend',
        f'deposit-side-effect:{transaction_id}:inviter-first-reward': 'referrer',
        f'deposit-side-effect:{transaction_id}:inviter-recurring-commission': 'referrer',
    }
    rows = (
        await db.execute(
            select(Transaction.device_first_ledger_key, Transaction.amount_kopeks).where(
                Transaction.device_first_ledger_key.in_(list(keys))
            )
        )
    ).all()
    for key, amount in rows:
        credited[keys[key]] += int(amount or 0)
    return credited


async def plan_referral_debt(db: AsyncSession) -> list[dict]:
    """Посчитать долг, НИЧЕГО не записывая.

    Арифметика берётся из тех же функций, что зовёт `_apply_referral_step`. Иначе экран
    показал бы одно, а движок заплатил другое — и разошлись бы они молча.
    """
    plan: list[dict] = []
    for transaction_id, checkout_id, buyer_id, referrer_id, amount_kopeks in REFERRAL_DEBT_2026_08:
        # 🔴 Приход читаем СТОЛБЦАМИ, а не объектом. Объект осел бы в кэше сессии, и движок,
        # берущий приход `select(...).with_for_update()` без `populate_existing`, получил бы
        # нашу копию, не перечитав её под блокировкой.
        # ⛔ Гасить кэш через `db.expire_all()` — тот способ, которым это чинилось сначала, —
        # НЕЛЬЗЯ: сессия общая с промежуточным слоем, в ней лежит и сам админ, и обращение
        # к нему сразу после выплаты падало бы, не показав владельцу вообще ничего.
        source = (
            await db.execute(
                select(
                    Transaction.id,
                    Transaction.type,
                    Transaction.is_completed,
                    Transaction.amount_kopeks,
                    Transaction.device_first_checkout_id,
                    Transaction.created_at,
                ).where(Transaction.id == transaction_id)
            )
        ).first()
        buyer = await db.get(User, buyer_id)
        referrer = await db.get(User, referrer_id)
        job = (
            await db.execute(
                select(DeviceFirstDepositOutbox).where(DeviceFirstDepositOutbox.transaction_id == transaction_id)
            )
        ).scalar_one_or_none()

        problems: list[str] = []
        if source is None or source.type != TransactionType.PROVIDER_RECEIPT.value:
            problems.append('приход исчез или сменил тип')
        elif not source.is_completed or source.amount_kopeks != amount_kopeks:
            problems.append('сумма или завершённость изменились')
        elif source.device_first_checkout_id != checkout_id:
            problems.append('у прихода другой заказ')
        if buyer is None:
            problems.append('покупатель исчез')
        elif buyer.referred_by_id != referrer_id:
            problems.append('пригласивший изменился')
        if referrer is None:
            problems.append('пригласивший исчез')
        if job is not None:
            # Второй заслон против мины FO: работа уже заведена — платить нечего и нельзя.
            problems.append(f'работа уже заведена ({job.referral_status})')

        to_friend = 0
        to_referrer = 0
        commission_percent = 0
        if not problems:
            prior_reward_payments = await get_referral_reward_payment_count(db, referrer.id, buyer.id)
            commission_percent = await calculate_referral_commission_percent(
                db,
                referrer,
                is_first_payment=prior_reward_payments == 0,
            )
            commission = int(amount_kopeks * commission_percent / 100) if commission_percent > 0 else 0
            if _qualifies_for_first_payment_bonus(buyer, amount_kopeks):
                to_friend = settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
                to_referrer = settings.REFERRAL_INVITER_BONUS_KOPEKS + commission
            elif commission > 0 and not await _is_commission_limit_reached(db, referrer.id, buyer.id):
                to_referrer = commission

        plan.append(
            {
                'transaction_id': transaction_id,
                'checkout_id': checkout_id,
                'buyer': buyer,
                'referrer': referrer,
                'paid_kopeks': amount_kopeks,
                'paid_at': source.created_at if source is not None else None,
                'commission_percent': commission_percent,
                'to_friend': to_friend,
                'to_referrer': to_referrer,
                'problems': problems,
            }
        )
    return plan


async def pay_referral_debt(db: AsyncSession) -> dict:
    """Доплатить долг через тот же движок, что платит живые оплаты.

    🔴 «Остановиться на середине» ЗДЕСЬ НЕВОЗМОЖНО, и делать вид, что можно, опаснее всего.
    Очередь дренирует фоновый цикл каждые 10 секунд без всякого фильтра
    (`device_first_recovery_service.py:47`, `monitoring_service.py:521`): любая
    закоммиченная работа будет исполнена, нравится нам это или нет. Поэтому:

      * единственные настоящие ворота — сверка расчёта ДО первой записи;
      * все пять работ заводятся ОДНОЙ транзакцией, одним коммитом — это и есть точка
        невозврата, одна на весь пакет, а не пять разных;
      * дальше мы лишь доводим до конца сами, чтобы владелец увидел итог сразу. Не вышло —
        доплатит фоновый цикл, и это ПРАВИЛЬНО: долг признан по всем пяти строкам разом.

    Прошлая редакция коммитила построчно и обещала «стоп при расхождении». Обещание было
    ложным дважды: остановка не мешала фону доплатить закоммиченное, а остаток после неё
    не оплачивался уже никогда — у заведённых работ появлялся `problems`, и экран отказывал
    навсегда.
    """
    from app.services.device_first_checkout_service import DIRECT_SETTLEMENT_MODE

    # 🔴 Выключатель здесь — условие ОТКАЗА, а не параметр `pay_referral`.
    # Передать его внутрь `ensure_deposit_outbox` нельзя: при выключенной программе работа
    # завелась бы сразу со статусом 'done', и долг стал бы НЕОПЛАЧИВАЕМЫМ НАВСЕГДА —
    # обратного перевода статуса в коде не существует ни одного (проверено grep по app/).
    # Ровно так 27.08 умерла приёмочная оплата 387.
    if not settings.is_referral_program_enabled():
        return {'paid': False, 'reason': 'реферальная программа выключена', 'rows': []}

    plan = await plan_referral_debt(db)
    total = sum(row['to_friend'] + row['to_referrer'] for row in plan)
    if any(row['problems'] for row in plan) or total != REFERRAL_DEBT_2026_08_TOTAL_KOPEKS:
        # 🔴 Отказ обязан говорить правду о деньгах. Самый частый путь сюда — ВТОРОЕ нажатие
        # после успешной выплаты: работы уже заведены, расчёт даёт ноль. Вернуть голый план
        # значило бы напечатать «ни одна строка не оплачена, деньги не тронуты» поверх уже
        # выплаченных 2 349,25 ₽ — и владелец пошёл бы начислять партнёрам второй раз.
        # Поэтому по каждой строке читаем книгу и отдаём фактически начисленное.
        checked = []
        for row in plan:
            credited = await _debt_credited_kopeks(db, transaction_id=row['transaction_id'])
            checked.append({**row, 'credited_friend': credited['friend'], 'credited_referrer': credited['referrer']})
        return {
            'paid': False,
            'reason': f'расчёт разошёлся: {total} ≠ {REFERRAL_DEBT_2026_08_TOTAL_KOPEKS}',
            'rows': checked,
        }

    # ── точка невозврата: одна транзакция на все пять работ ──────────────────────────
    try:
        for row in plan:
            await ensure_deposit_outbox(
                db,
                transaction_id=row['transaction_id'],
                checkout_id=row['checkout_id'],
                emit_deposit_event=False,
                settlement_mode=DIRECT_SETTLEMENT_MODE,
            )
        await db.commit()
    except IntegrityError:
        # Два нажатия внахлёст: соседняя сессия успела вставить работу первой. Уникальный
        # ключ по приходу держит деньги от повтора — нам остаётся не пугать владельца сырой
        # ошибкой, а сказать по-человечески. Своих записей после отката не остаётся.
        await db.rollback()
        logger.warning('Доплата долга столкнулась со второй попыткой, откатились')
        return {'paid': False, 'reason': 'выплата уже запущена в соседнем окне', 'rows': []}

    for row in plan:
        try:
            # 🔴 Всегда адресно: без `transaction_id` воркер захватит ЧУЖИЕ живые работы.
            await process_device_first_deposit_outbox(db, limit=1, transaction_id=row['transaction_id'])
        except Exception as error:
            await db.rollback()
            logger.warning(
                'Строка долга не доведена нами, останется фоновому циклу',
                transaction_id=row['transaction_id'],
                error=error,
            )

    done = []
    for row in plan:
        credited = await _debt_credited_kopeks(db, transaction_id=row['transaction_id'])
        done.append({**row, 'credited_friend': credited['friend'], 'credited_referrer': credited['referrer']})
    return {'paid': True, 'reason': '', 'rows': done}

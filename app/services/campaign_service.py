from dataclasses import dataclass

import structlog
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.campaign import record_campaign_registration
from app.database.crud.subscription import (
    create_paid_subscription,
    get_subscription_by_user_id,
)
from app.database.crud.tariff import get_tariff_by_id
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.crud.user import add_user_balance
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    CheckoutPaymentAttempt,
    CloudPaymentsPayment,
    DeviceFirstProviderEvent,
    SubscriptionCheckout,
    SubscriptionConversion,
    Transaction,
    TransactionType,
    User,
    YooKassaPayment,
)
from app.services.subscription_service import SubscriptionService


logger = structlog.get_logger(__name__)

_POST_PAID_REVERSAL_PREFIX = 'post_paid_provider_terminal%'
_DURABLE_POST_PAID_REVERSAL_STATUSES = ('CHARGEBACKED',)


def _format_user_log(user: User) -> str:
    """Format user identifier for logging (supports email-only users)."""
    if user.telegram_id:
        return str(user.telegram_id)
    if user.email:
        return f'{user.id} ({user.email})'
    return f'#{user.id}'


@dataclass
class CampaignBonusResult:
    success: bool
    bonus_type: str | None = None
    balance_kopeks: int = 0
    subscription_days: int | None = None
    subscription_traffic_gb: int | None = None
    subscription_device_limit: int | None = None
    subscription_squads: list[str] | None = None
    # Поля для tariff
    tariff_id: int | None = None
    tariff_name: str | None = None
    tariff_duration_days: int | None = None
    # True если запись в advertising_campaign_registrations была создана этим вызовом
    # (а не вернулась как existing). Используется caller'ом, чтобы понять, нужно ли
    # слать админу уведомление о регистрации (один раз на первую успешную).
    is_new_registration: bool = False


@dataclass(frozen=True)
class CampaignAnalytics:
    """Two explicit contracts: legacy fields and first-touch external receipts."""

    campaign_id: int
    registrations: int
    conversion_count: int
    paid_users_count: int
    conversion_rate: float
    total_revenue_kopeks: int
    avg_revenue_per_user_kopeks: int
    leads: int
    paying_leads: int
    payment_conversion_rate: float
    confirmed_receipts_kopeks: int
    avg_confirmed_receipts_per_lead_kopeks: int


def _campaign_analytics_statement(campaign_ids: list[int] | None = None):
    """Build the aggregate without filtering registrations before first-touch ranking."""

    ranked_registrations = select(
        AdvertisingCampaignRegistration.campaign_id,
        AdvertisingCampaignRegistration.user_id,
        AdvertisingCampaignRegistration.created_at,
        func.row_number()
        .over(
            partition_by=AdvertisingCampaignRegistration.user_id,
            order_by=(
                AdvertisingCampaignRegistration.created_at.asc().nulls_last(),
                AdvertisingCampaignRegistration.id.asc(),
            ),
        )
        .label('touch_rank'),
    ).cte('ranked_campaign_registrations')
    first_touch = (
        select(
            ranked_registrations.c.campaign_id,
            ranked_registrations.c.user_id,
            ranked_registrations.c.created_at,
        )
        .where(ranked_registrations.c.touch_rank == 1)
        .cte('campaign_first_touch')
    )

    known_test_payment = or_(
        select(YooKassaPayment.id)
        .where(
            YooKassaPayment.transaction_id == Transaction.id,
            YooKassaPayment.test_mode.is_(True),
        )
        .correlate(Transaction)
        .exists(),
        select(CloudPaymentsPayment.id)
        .where(
            CloudPaymentsPayment.transaction_id == Transaction.id,
            CloudPaymentsPayment.test_mode.is_(True),
        )
        .correlate(Transaction)
        .exists(),
    )
    reversed_attempt = (
        select(CheckoutPaymentAttempt.id)
        .where(
            CheckoutPaymentAttempt.checkout_id == Transaction.device_first_checkout_id,
            CheckoutPaymentAttempt.provider_payment_id == Transaction.external_id,
            CheckoutPaymentAttempt.reconciliation_reason.like(_POST_PAID_REVERSAL_PREFIX),
        )
        .correlate(Transaction)
        .exists()
    )
    reversed_event = (
        select(DeviceFirstProviderEvent.id)
        .where(
            DeviceFirstProviderEvent.checkout_id == Transaction.device_first_checkout_id,
            DeviceFirstProviderEvent.provider_payment_id == Transaction.external_id,
            func.upper(DeviceFirstProviderEvent.provider_status).in_(_DURABLE_POST_PAID_REVERSAL_STATUSES),
        )
        .correlate(Transaction)
        .exists()
    )

    receipts = (
        select(
            first_touch.c.campaign_id,
            first_touch.c.user_id,
            Transaction.id.label('transaction_id'),
            Transaction.amount_kopeks,
        )
        .join(Transaction, Transaction.user_id == first_touch.c.user_id)
        .outerjoin(SubscriptionCheckout, SubscriptionCheckout.id == Transaction.device_first_checkout_id)
        .where(
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
            Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            func.coalesce(Transaction.completed_at, Transaction.created_at) >= first_touch.c.created_at,
            ~known_test_payment,
            or_(
                Transaction.type != TransactionType.PROVIDER_RECEIPT.value,
                and_(
                    ~func.coalesce(SubscriptionCheckout.terminal_reason, '').like(_POST_PAID_REVERSAL_PREFIX),
                    ~reversed_attempt,
                    ~reversed_event,
                ),
            ),
        )
        .cte('campaign_qualified_receipts')
    )

    leads = (
        select(first_touch.c.campaign_id, func.count().label('leads'))
        .group_by(first_touch.c.campaign_id)
        .cte('campaign_leads')
    )
    receipt_totals = (
        select(
            receipts.c.campaign_id,
            func.count(func.distinct(receipts.c.user_id)).label('paying_leads'),
            func.coalesce(func.sum(receipts.c.amount_kopeks), 0).label('confirmed_receipts_kopeks'),
        )
        .group_by(receipts.c.campaign_id)
        .cte('campaign_receipt_totals')
    )
    registrations = (
        select(
            AdvertisingCampaignRegistration.campaign_id,
            func.count(AdvertisingCampaignRegistration.id).label('registrations'),
        )
        .group_by(AdvertisingCampaignRegistration.campaign_id)
        .cte('campaign_registration_totals')
    )
    legacy_registration_users = (
        select(
            AdvertisingCampaignRegistration.campaign_id,
            AdvertisingCampaignRegistration.user_id,
        )
        .distinct()
        .cte('campaign_legacy_registration_users')
    )
    legacy_deposits = (
        select(
            legacy_registration_users.c.campaign_id,
            func.coalesce(func.sum(Transaction.amount_kopeks), 0).label('total_revenue_kopeks'),
        )
        .join(Transaction, Transaction.user_id == legacy_registration_users.c.user_id)
        .where(
            Transaction.type == TransactionType.DEPOSIT.value,
            Transaction.is_completed.is_(True),
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        )
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_deposits')
    )
    legacy_payment_users = (
        select(SubscriptionConversion.user_id.label('user_id'))
        .union(
            select(Transaction.user_id.label('user_id')).where(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed.is_(True),
            )
        )
        .cte('campaign_legacy_payment_users')
    )
    legacy_conversions = (
        select(
            legacy_registration_users.c.campaign_id,
            func.count(func.distinct(legacy_registration_users.c.user_id)).label('conversion_count'),
        )
        .join(legacy_payment_users, legacy_payment_users.c.user_id == legacy_registration_users.c.user_id)
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_conversions')
    )
    legacy_paid_flags = (
        select(
            legacy_registration_users.c.campaign_id,
            func.count(func.distinct(legacy_registration_users.c.user_id)).label('paid_flag_count'),
        )
        .join(User, User.id == legacy_registration_users.c.user_id)
        .where(User.has_had_paid_subscription.is_(True))
        .group_by(legacy_registration_users.c.campaign_id)
        .cte('campaign_legacy_paid_flags')
    )

    statement = (
        select(
            AdvertisingCampaign.id.label('campaign_id'),
            func.coalesce(registrations.c.registrations, 0).label('registrations'),
            func.coalesce(legacy_conversions.c.conversion_count, 0).label('conversion_count'),
            func.coalesce(legacy_paid_flags.c.paid_flag_count, 0).label('paid_flag_count'),
            func.coalesce(legacy_deposits.c.total_revenue_kopeks, 0).label('total_revenue_kopeks'),
            func.coalesce(leads.c.leads, 0).label('leads'),
            func.coalesce(receipt_totals.c.paying_leads, 0).label('paying_leads'),
            func.coalesce(receipt_totals.c.confirmed_receipts_kopeks, 0).label('confirmed_receipts_kopeks'),
        )
        .outerjoin(registrations, registrations.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_conversions, legacy_conversions.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_paid_flags, legacy_paid_flags.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(legacy_deposits, legacy_deposits.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(leads, leads.c.campaign_id == AdvertisingCampaign.id)
        .outerjoin(receipt_totals, receipt_totals.c.campaign_id == AdvertisingCampaign.id)
        .order_by(AdvertisingCampaign.id)
    )
    if campaign_ids is not None:
        statement = statement.where(AdvertisingCampaign.id.in_(campaign_ids))
    return statement


async def get_campaign_analytics(
    db: AsyncSession,
    campaign_ids: list[int] | None = None,
) -> dict[int, CampaignAnalytics]:
    """Return legacy-compatible and honest receipt metrics in one SQL round-trip."""

    if campaign_ids == []:
        return {}
    rows = (await db.execute(_campaign_analytics_statement(campaign_ids))).mappings().all()
    result: dict[int, CampaignAnalytics] = {}
    for row in rows:
        registrations_count = int(row['registrations'] or 0)
        conversion_count = int(row['conversion_count'] or 0)
        paid_users_count = max(conversion_count, int(row['paid_flag_count'] or 0))
        legacy_revenue = int(row['total_revenue_kopeks'] or 0)
        leads_count = int(row['leads'] or 0)
        paying_leads = int(row['paying_leads'] or 0)
        confirmed_receipts = int(row['confirmed_receipts_kopeks'] or 0)
        result[int(row['campaign_id'])] = CampaignAnalytics(
            campaign_id=int(row['campaign_id']),
            registrations=registrations_count,
            conversion_count=conversion_count,
            paid_users_count=paid_users_count,
            conversion_rate=round((paid_users_count / registrations_count) * 100, 1) if registrations_count else 0.0,
            total_revenue_kopeks=legacy_revenue,
            avg_revenue_per_user_kopeks=int(legacy_revenue / registrations_count) if registrations_count else 0,
            leads=leads_count,
            paying_leads=paying_leads,
            payment_conversion_rate=round((paying_leads / leads_count) * 100, 1) if leads_count else 0.0,
            confirmed_receipts_kopeks=confirmed_receipts,
            avg_confirmed_receipts_per_lead_kopeks=(int(confirmed_receipts / leads_count) if leads_count else 0),
        )
    return result


async def delete_campaign_if_unattributed(db: AsyncSession, campaign_id: int) -> bool:
    """Lock the campaign before checking history, fencing concurrent registrations."""

    locked_campaign_id = await db.scalar(
        select(AdvertisingCampaign.id).where(AdvertisingCampaign.id == campaign_id).with_for_update()
    )
    if locked_campaign_id is None:
        await db.rollback()
        return False

    has_registrations = await db.scalar(
        select(
            select(AdvertisingCampaignRegistration.id)
            .where(AdvertisingCampaignRegistration.campaign_id == campaign_id)
            .exists()
        )
    )
    if has_registrations:
        await db.rollback()
        return False

    deleted = await db.execute(delete(AdvertisingCampaign).where(AdvertisingCampaign.id == campaign_id))
    if int(deleted.rowcount or 0) != 1:
        await db.rollback()
        return False
    await db.commit()
    return True


class AdvertisingCampaignService:
    def __init__(self) -> None:
        self.subscription_service = SubscriptionService()

    async def apply_campaign_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        if not campaign.is_active:
            logger.warning('⚠️ Попытка выдать бонус по неактивной кампании', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Prevent partner from being attributed to their own campaign
        if campaign.partner_user_id and campaign.partner_user_id == user.id:
            logger.info(
                'Skipping campaign bonus: user is the campaign partner',
                user_id=user.id,
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(success=False)

        if campaign.is_balance_bonus:
            return await self._apply_balance_bonus(db, user, campaign)

        if campaign.is_subscription_bonus:
            return await self._apply_subscription_bonus(db, user, campaign)

        if campaign.is_none_bonus:
            return await self._apply_none_bonus(db, user, campaign)

        if campaign.is_tariff_bonus:
            return await self._apply_tariff_bonus(db, user, campaign)

        logger.error('❌ Неизвестный тип бонуса кампании', bonus_type=campaign.bonus_type)
        return CampaignBonusResult(success=False)

    async def _apply_balance_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        amount = campaign.balance_bonus_kopeks or 0
        if amount <= 0:
            logger.info('ℹ️ Кампания не имеет бонуса на баланс', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Регистрируем ДО начисления баланса, чтобы при повторном /start (created=False)
        # не накрутить бонус второй раз. UNIQUE constraint в record_campaign_registration
        # + savepoint защищают и от concurrent race conditions.
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='balance',
            balance_bonus_kopeks=amount,
        )

        if not created:
            logger.info(
                'ℹ️ Балансный бонус уже был начислен по этой кампании ранее, пропускаем',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
            )
            return CampaignBonusResult(
                success=True,
                bonus_type='balance',
                balance_kopeks=amount,
                is_new_registration=False,
            )

        # 🔴 РЕК-1: имя кампании в подпись НЕ подставляем. Эта строка ложится в проводку и
        # печатается КЛИЕНТУ дважды: «📊 История операций» бота (`handlers/balance/main.py`)
        # и вкладка «Баланс» кабинета (`cabinet/routes/balance.py` → `Balance.tsx`). У владельца
        # в именах кампаний рекламный бюджет («Кувалда 7000₽»), и человек читал его там же,
        # куда идёт с вопросом «откуда деньги». Причину начисления строка называть обязана —
        # приветствие о ней больше не говорит, и это её единственное объяснение.
        # ⛔ Кампанию владелец различает по АДМИНСКИМ поверхностям: уведомление о регистрации
        # (`admin_notification_service.py`) и экран статистики РК-1/РК-2 — они читают модель.
        description = 'Бонус за регистрацию'
        success = await add_user_balance(
            db,
            user,
            amount,
            description=description,
        )

        if not success:
            # Маркер регистрации остался — баланс не начислился. Это лучше, чем
            # начислить деньги без записи в БД (откатить запись теперь нельзя).
            logger.error(
                '❌ Регистрация записана, но баланс не начислился',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                amount_kopeks=amount,
            )
            return CampaignBonusResult(success=False)

        logger.info(
            '💰 Пользователю начислен бонус ₽ по кампании',
            format_user_log=_format_user_log(user),
            amount=amount / 100,
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='balance',
            balance_kopeks=amount,
            is_new_registration=created,
        )

    async def _apply_subscription_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs:
                # Multi-tariff: extend the best existing subscription instead of blocking
                _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                _pool = _non_daily or active_subs
                existing_subscription = max(_pool, key=lambda s: s.days_left)
            else:
                existing_subscription = None
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        duration_days = campaign.subscription_duration_days or 0
        if duration_days <= 0:
            logger.info('ℹ️ Кампания не содержит корректной длительности подписки', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Legacy campaign-level technical squads have no PublicLocation policy
        # or immutable tariff evidence.  Do not issue them implicitly.
        logger.error(
            'Campaign subscription bonus without tariff entitlement is retired',
            campaign_id=campaign.id,
        )
        return CampaignBonusResult(success=False)

        traffic_limit = campaign.subscription_traffic_gb
        device_limit = campaign.subscription_device_limit
        if device_limit is None:
            device_limit = settings.DEFAULT_DEVICE_LIMIT
        try:
            from app.database.crud.server_squad import get_effective_tariff_squad_uuids

            squads = await get_effective_tariff_squad_uuids(db, campaign.subscription_squads)
        except Exception as error:
            logger.error('Не удалось подобрать сквады для кампании', campaign_id=campaign.id, error=error)
            squads = list(campaign.subscription_squads or [])

        if existing_subscription:
            # Multi-tariff: extend the best existing subscription
            from app.database.crud.subscription import extend_subscription

            await extend_subscription(db, existing_subscription, duration_days)
            try:
                await self.subscription_service.update_remnawave_user(db, existing_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave при продлении кампании', campaign_id=campaign.id, error=error
                )

            logger.info(
                '🎁 Подписка пользователя продлена по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
                subscription_id=existing_subscription.id,
            )
        else:
            new_subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit or 0,
                device_limit=device_limit,
                connected_squads=squads,
                update_server_counters=True,
                is_trial=True,
            )

            try:
                await self.subscription_service.create_remnawave_user(db, new_subscription)
            except Exception as error:
                logger.error('❌ Ошибка синхронизации RemnaWave для кампании', campaign_id=campaign.id, error=error)

            logger.info(
                '🎁 Пользователю выдана подписка по кампании на дней',
                format_user_log=_format_user_log(user),
                campaign_id=campaign.id,
                duration_days=duration_days,
            )

        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='subscription',
            subscription_duration_days=duration_days,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='subscription',
            subscription_days=duration_days,
            subscription_traffic_gb=traffic_limit or 0,
            subscription_device_limit=device_limit,
            subscription_squads=squads,
            is_new_registration=created,
        )

    async def _apply_none_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Обычная ссылка без награды - только регистрация для отслеживания."""
        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='none',
        )

        logger.info(
            '📊 Пользователь зарегистрирован по ссылке кампании (без награды)',
            format_user_log=_format_user_log(user),
            campaign_id=campaign.id,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='none',
            is_new_registration=created,
        )

    async def _apply_tariff_bonus(
        self,
        db: AsyncSession,
        user: User,
        campaign: AdvertisingCampaign,
    ) -> CampaignBonusResult:
        """Выдача тарифа на определённое время."""
        existing_subscription = None
        if settings.is_multi_tariff_enabled():
            from app.database.crud.subscription import get_active_subscriptions_by_user_id

            active_subs = await get_active_subscriptions_by_user_id(db, user.id)
            if active_subs and campaign.tariff_id:
                # Multi-tariff: only check for THIS specific tariff
                same_tariff_subs = [s for s in active_subs if s.tariff_id == campaign.tariff_id]
                if same_tariff_subs:
                    existing_subscription = max(same_tariff_subs, key=lambda s: s.days_left)
                # If no sub for this tariff, existing_subscription stays None -> create new
        else:
            existing_subscription = await get_subscription_by_user_id(db, user.id)
            if existing_subscription:
                logger.warning(
                    '⚠️ У пользователя уже есть подписка, бонус тарифа кампании пропущен',
                    format_user_log=_format_user_log(user),
                    campaign_id=campaign.id,
                )
                return CampaignBonusResult(success=False)

        if not campaign.tariff_id:
            logger.error('❌ Кампания не имеет указанного тарифа для выдачи', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        duration_days = campaign.tariff_duration_days or 0
        if duration_days <= 0:
            logger.error('❌ Кампания не имеет указанной длительности тарифа', campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        # Получаем тариф для извлечения параметров
        tariff = await get_tariff_by_id(db, campaign.tariff_id)
        if not tariff:
            logger.error('❌ Тариф не найден для кампании', tariff_id=campaign.tariff_id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        if not tariff.is_active:
            logger.warning('⚠️ Тариф неактивен, бонус кампании пропущен', tariff_id=tariff.id, campaign_id=campaign.id)
            return CampaignBonusResult(success=False)

        traffic_limit = tariff.traffic_limit_gb
        device_limit = tariff.device_limit
        try:
            from app.services.public_location_entitlement_service import resolve_tariff_entitlement

            squads = list((await resolve_tariff_entitlement(db, tariff)).squad_uuids)
        except Exception as error:
            logger.error('Не удалось разрешить entitlement тарифа кампании', campaign_id=campaign.id, error=error)
            return CampaignBonusResult(success=False)

        if existing_subscription:
            # Multi-tariff: extend the existing subscription for this tariff
            from app.database.crud.subscription import extend_subscription

            await extend_subscription(db, existing_subscription, duration_days, tariff_id=tariff.id)
            try:
                await self.subscription_service.update_remnawave_user(db, existing_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave при продлении тарифа кампании',
                    campaign_id=campaign.id,
                    error=error,
                )

            logger.info(
                '🎁 Подписка пользователя продлена по тарифу кампании на дней',
                format_user_log=_format_user_log(user),
                tariff_name=tariff.name,
                campaign_id=campaign.id,
                duration_days=duration_days,
                subscription_id=existing_subscription.id,
            )
        else:
            # Создаём подписку как платную (не trial) с привязкой к тарифу
            new_subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=duration_days,
                traffic_limit_gb=traffic_limit or 0,
                device_limit=device_limit,
                connected_squads=squads,
                update_server_counters=True,
                is_trial=False,
                tariff_id=tariff.id,
            )

            try:
                await self.subscription_service.create_remnawave_user(db, new_subscription)
            except Exception as error:
                logger.error(
                    '❌ Ошибка синхронизации RemnaWave для тарифа кампании', campaign_id=campaign.id, error=error
                )

            logger.info(
                '🎁 Пользователю выдан тариф по кампании на дней',
                format_user_log=_format_user_log(user),
                tariff_name=tariff.name,
                campaign_id=campaign.id,
                duration_days=duration_days,
            )

        _, created = await record_campaign_registration(
            db,
            campaign_id=campaign.id,
            user_id=user.id,
            bonus_type='tariff',
            tariff_id=tariff.id,
            tariff_duration_days=duration_days,
        )

        return CampaignBonusResult(
            success=True,
            bonus_type='tariff',
            tariff_id=tariff.id,
            tariff_name=tariff.name,
            tariff_duration_days=duration_days,
            subscription_traffic_gb=traffic_limit or 0,
            subscription_device_limit=device_limit,
            subscription_squads=squads,
            is_new_registration=created,
        )

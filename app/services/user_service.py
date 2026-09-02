import asyncio
import os
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot, types
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.promo_group import get_default_promo_group, get_promo_group_by_id
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.transaction import get_user_transactions_count
from app.database.crud.user import (
    add_user_balance,
    get_inactive_users,
    get_referrals,
    get_user_by_id,
    get_users_count,
    get_users_list,
    get_users_spending_stats,
    get_users_statistics,
    subtract_user_balance,
    update_user,
)
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    AntilopayPayment,
    AppleIAPAccount,
    AppleTransaction,
    AuraPayPayment,
    BroadcastHistory,
    CheckoutPaymentAttempt,
    CloudPaymentsPayment,
    CryptoBotPayment,
    DonutPayment,
    EtoplatezhiPayment,
    FreekassaPayment,
    GuestPurchase,
    HeleketPayment,
    JupiterPayment,
    KassaAiPayment,
    LavaPayment,
    MulenPayPayment,
    OverpayPayment,
    Pal24Payment,
    PaymentMethod,
    PayPearPayment,
    PlategaPayment,
    PromoCode,
    PromoCodeUse,
    PromoGroup,
    ReferralEarning,
    RioPayPayment,
    RollyPayPayment,
    SentNotification,
    SeverPayPayment,
    Subscription,
    SubscriptionCheckout,
    SubscriptionConversion,
    SubscriptionServer,
    Ticket,
    Transaction,
    User,
    UserMessage,
    UserStatus,
    WataPayment,
    WelcomeText,
    WithdrawalRequest,
    YooKassaPayment,
)
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)


logger = structlog.get_logger(__name__)


@dataclass
class DeleteUserResult:
    """Результат удаления пользователя."""

    bot_deleted: bool = False
    panel_deleted: bool = False
    panel_error: str | None = None
    # A financial tombstone is deliberately not a physical DB delete, but it
    # does close access and eventually releases the identity for a clean
    # re-registration. Keep that contract explicit for every admin surface.
    account_closed: bool = False
    erasure_state: str | None = None
    message: str | None = None


class UserService:
    @staticmethod
    async def _get_financial_history_kind(db: AsyncSession, user_id: int) -> tuple[bool, bool]:
        """Return ``(has_any_history, has_legacy_history)`` without deleting it.

        Device-First attempts have their own canonical reconciliation model.
        Every other provider row and every non-Device-First ledger transaction
        is financial evidence as well: erase neither records nor their user
        anchor automatically.  The second value sends such accounts to a
        manual financial-resolution state instead of guessing a provider's
        finality.
        """
        has_device_first = bool(
            await db.scalar(
                select(CheckoutPaymentAttempt.id)
                .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                .where(SubscriptionCheckout.user_id == user_id)
                .limit(1)
            )
        )

        legacy_models = (
            AppleTransaction,
            # An Apple account token can receive a delayed StoreKit event even
            # before a local transaction row is materialised. Keep it behind
            # the same manual close/reconciliation policy.
            AppleIAPAccount,
            YooKassaPayment,
            CryptoBotPayment,
            HeleketPayment,
            MulenPayPayment,
            Pal24Payment,
            WataPayment,
            CloudPaymentsPayment,
            FreekassaPayment,
            KassaAiPayment,
            RioPayPayment,
            SeverPayPayment,
            PayPearPayment,
            RollyPayPayment,
            OverpayPayment,
            AuraPayPayment,
            EtoplatezhiPayment,
            AntilopayPayment,
            JupiterPayment,
            DonutPayment,
            LavaPayment,
            WithdrawalRequest,
        )
        has_legacy_provider = False
        for model in legacy_models:
            if await db.scalar(select(model.id).where(model.user_id == user_id).limit(1)) is not None:
                has_legacy_provider = True
                break

        # Landing/guest orders are provider-facing financial obligations. A
        # late payment can be fulfilled for either the buyer or the linked
        # recipient, so both ends retain an auditable manual-close state.
        has_guest_purchase = bool(
            await db.scalar(
                select(GuestPurchase.id)
                .where(or_(GuestPurchase.buyer_user_id == user_id, GuestPurchase.user_id == user_id))
                .limit(1)
            )
        )

        # The Platega model is shared by direct Device-First and historical
        # wallet flows. Only a row with no Device-First attempt is legacy.
        has_legacy_platega = bool(
            await db.scalar(
                select(PlategaPayment.id)
                .outerjoin(
                    CheckoutPaymentAttempt,
                    CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id,
                )
                .where(PlategaPayment.user_id == user_id, CheckoutPaymentAttempt.id.is_(None))
                .limit(1)
            )
        )

        # Direct Device-First receipts are already represented by an attempt;
        # do not make them a separate legacy/manual case.
        has_legacy_transaction = bool(
            await db.scalar(
                select(Transaction.id)
                .where(Transaction.user_id == user_id, Transaction.device_first_checkout_id.is_(None))
                .limit(1)
            )
        )
        has_legacy_history = has_legacy_provider or has_guest_purchase or has_legacy_platega or has_legacy_transaction
        return has_device_first or has_legacy_history, has_legacy_history

    async def send_balance_change_notification(self, bot: Bot, user: User, amount_kopeks: int) -> bool:
        """Сообщает человеку, что его баланс изменили руками.

        Зовётся из ВСЕХ четырёх поверхностей ручной правки баланса: чат-админка,
        кабинетная карточка, кабинетные массовые действия и Web API. До этапа УБ-1
        говорила только чат-админка, остальные три молчали.

        ⛔ Ручное начисление НЕЛЬЗЯ проводить через ``send_cart_notification_after_topup``
        (`app/services/payment/common.py`), хотя он и шлёт похожее письмо: он тянет за собой
        автовозобновление суточной, автопокупку сохранённой корзины и
        ``try_auto_extend_expired_after_topup`` (#629889) — подарок молча ушёл бы на
        продление. Мина **JN**. Здесь только сообщение, без побочных действий.

        Работает для КОГО УГОДНО: подписка, тариф и выбранный язык роли не играют —
        ``notification_delivery_service`` отказывает только забаненным и удалённым.
        Возвращает True, если сообщение реально ушло хотя бы одним каналом.
        """
        texts = get_texts(user.language)
        formatted_balance = settings.format_price(user.balance_kopeks)

        subs = getattr(user, 'subscriptions', None) or []
        has_extendable = any(sub.status in {'active', 'expired', 'trial'} for sub in subs)

        if amount_kopeks > 0:
            message = texts.t(
                'BALANCE_ADMIN_ADDED',
                '💰 <b>Баланс пополнен на {amount}</b>\n\nСейчас на счету: {balance}',
            ).format(amount=settings.format_price(amount_kopeks), balance=formatted_balance)
            # Человеку без подписки говорим главное: деньги на счету — это ещё не VPN.
            # Это единственная полезная мысль из снесённой send_topup_success_to_user;
            # её собственные кнопки были мертвы (`subscription_buy`,
            # `subscription_add_devices` не зарегистрированы нигде в проекте).
            if not has_extendable:
                message += '\n\n' + texts.t(
                    'BALANCE_ADMIN_ADDED_NO_SUBSCRIPTION',
                    'Подписка от этого не включается — её нужно оформить.',
                )
        else:
            message = texts.t(
                'BALANCE_ADMIN_SUBTRACTED',
                '💸 <b>С баланса списано {amount}</b>\n\nСейчас на счету: {balance}',
            ).format(amount=settings.format_price(abs(amount_kopeks)), balance=formatted_balance)
            message += '\n\n' + texts.t(
                'BALANCE_ADMIN_SUBTRACTED_HINT',
                'Если это ошибка — напишите в поддержку.',
            )

        # Кнопка обязана вести в живой обработчик. Оба колбэка проверены на регистрацию:
        # `subscription_extend` — purchase.py, `funnel_tariffs` — tariff_purchase.py.
        if has_extendable:
            button_text = texts.t('SUBSCRIPTION_EXTEND', '💎 Продлить подписку')
            button_callback = 'menu_subscription' if settings.is_multi_tariff_enabled() else 'subscription_extend'
        else:
            button_text = texts.t('FUNNEL_TARIFFS', '💳 Тарифы')
            button_callback = 'funnel_tariffs'

        reply_markup = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=button_text, callback_data=button_callback)]]
        )

        context = {
            'amount_kopeks': amount_kopeks,
            'amount_rubles': amount_kopeks / 100,
            'new_balance_kopeks': user.balance_kopeks,
            'new_balance_rubles': user.balance_kopeks / 100,
            'formatted_amount': settings.format_price(amount_kopeks),
            'formatted_balance': formatted_balance,
            # Имя админа и служебная подпись операции клиенту не показываются.
        }

        return await notification_delivery_service.send_notification(
            user=user,
            notification_type=NotificationType.BALANCE_CHANGE,
            context=context,
            bot=bot,
            telegram_message=message,
            telegram_markup=reply_markup,
        )

    async def get_user_profile(self, db: AsyncSession, user_id: int) -> dict[str, Any] | None:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return None

            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user_id)
                if active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
            else:
                subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            return {
                'user': user,
                'subscription': subscription,
                'transactions_count': transactions_count,
                'is_admin': settings.is_admin(user.telegram_id, user.email),
                'registration_days': (datetime.now(UTC) - user.created_at).days,
            }

        except Exception as e:
            logger.error('Ошибка получения профиля пользователя', user_id=user_id, error=e)
            return None

    async def search_users(self, db: AsyncSession, query: str, page: int = 1, limit: int = 20) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            users = await get_users_list(db, offset=offset, limit=limit, search=query)
            total_count = await get_users_count(db, search=query)

            total_pages = (total_count + limit - 1) // limit

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка поиска пользователей', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def get_users_page(
        self,
        db: AsyncSession,
        page: int = 1,
        limit: int = 20,
        status: UserStatus | None = None,
        order_by_balance: bool = False,
        order_by_traffic: bool = False,
        order_by_last_activity: bool = False,
        order_by_total_spent: bool = False,
        order_by_purchase_count: bool = False,
    ) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            users = await get_users_list(
                db,
                offset=offset,
                limit=limit,
                status=status,
                order_by_balance=order_by_balance,
                order_by_traffic=order_by_traffic,
                order_by_last_activity=order_by_last_activity,
                order_by_total_spent=order_by_total_spent,
                order_by_purchase_count=order_by_purchase_count,
            )
            total_count = await get_users_count(db, status=status)

            total_pages = (total_count + limit - 1) // limit

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка получения страницы пользователей', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def get_users_ready_to_renew(
        self,
        db: AsyncSession,
        min_balance_kopeks: int,
        page: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Возвращает пользователей с истекшей подпиской и достаточным балансом."""
        try:
            offset = (page - 1) * limit
            now = datetime.now(UTC)

            base_filters = [
                User.balance_kopeks >= min_balance_kopeks,
                Subscription.end_date.isnot(None),
                Subscription.end_date <= now,
            ]

            query = (
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .join(Subscription, Subscription.user_id == User.id)
                .where(*base_filters)
                .order_by(User.balance_kopeks.desc(), Subscription.end_date.asc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(query)
            users = result.scalars().unique().all()

            count_query = (
                select(func.count(User.id)).join(Subscription, Subscription.user_id == User.id).where(*base_filters)
            )
            total_count = (await db.execute(count_query)).scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 0

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
            }

        except Exception as e:
            logger.error('Ошибка получения пользователей для продления', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
            }

    async def get_potential_customers(
        self,
        db: AsyncSession,
        min_balance_kopeks: int,
        page: int = 1,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Возвращает пользователей без активной подписки с достаточным балансом."""
        try:
            offset = (page - 1) * limit

            # Фильтры: нет активной подписки И баланс >= порога
            base_filters = [
                User.balance_kopeks >= min_balance_kopeks,
            ]

            # Subquery: user has at least one active/trial subscription
            active_sub_exists = exists().where(
                Subscription.user_id == User.id,
                Subscription.status.in_(['active', 'trial']),
            )

            # Основной запрос: пользователи БЕЗ активных подписок
            query = (
                select(User)
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .where(
                    *base_filters,
                    ~active_sub_exists,
                )
                .order_by(User.balance_kopeks.desc(), User.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(query)
            users = result.scalars().unique().all()

            # Запрос для подсчета общего количества
            count_query = select(func.count(User.id)).where(
                *base_filters,
                ~active_sub_exists,
            )
            total_count = (await db.execute(count_query)).scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 0

            return {
                'users': users,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
            }

        except Exception as e:
            logger.error('Ошибка получения потенциальных клиентов', error=e)
            return {
                'users': [],
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
            }

    async def get_user_spending_stats_map(self, db: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, int]]:
        try:
            return await get_users_spending_stats(db, user_ids)
        except Exception as e:
            logger.error('Ошибка получения статистики трат пользователей', error=e)
            return {}

    async def get_users_by_campaign_page(self, db: AsyncSession, page: int = 1, limit: int = 20) -> dict[str, Any]:
        try:
            offset = (page - 1) * limit

            campaign_ranked = select(
                AdvertisingCampaignRegistration.user_id.label('user_id'),
                AdvertisingCampaignRegistration.campaign_id.label('campaign_id'),
                AdvertisingCampaignRegistration.created_at.label('created_at'),
                func.row_number()
                .over(
                    partition_by=AdvertisingCampaignRegistration.user_id,
                    order_by=AdvertisingCampaignRegistration.created_at.desc(),
                )
                .label('rn'),
            ).cte('campaign_ranked')

            latest_campaign = (
                select(
                    campaign_ranked.c.user_id,
                    campaign_ranked.c.campaign_id,
                    campaign_ranked.c.created_at,
                )
                .where(campaign_ranked.c.rn == 1)
                .subquery()
            )

            query = (
                select(
                    User,
                    AdvertisingCampaign.name.label('campaign_name'),
                    latest_campaign.c.created_at,
                )
                .join(latest_campaign, latest_campaign.c.user_id == User.id)
                .join(
                    AdvertisingCampaign,
                    AdvertisingCampaign.id == latest_campaign.c.campaign_id,
                )
                .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
                .order_by(
                    AdvertisingCampaign.name.asc(),
                    latest_campaign.c.created_at.desc(),
                )
                .offset(offset)
                .limit(limit)
            )

            result = await db.execute(query)
            rows = result.all()

            users = [row[0] for row in rows]
            campaign_map = {
                row[0].id: {
                    'campaign_name': row[1],
                    'registered_at': row[2],
                }
                for row in rows
            }

            total_stmt = select(func.count()).select_from(latest_campaign)
            total_result = await db.execute(total_stmt)
            total_count = total_result.scalar() or 0
            total_pages = (total_count + limit - 1) // limit if total_count else 1

            return {
                'users': users,
                'campaigns': campaign_map,
                'current_page': page,
                'total_pages': total_pages,
                'total_count': total_count,
                'has_next': page < total_pages,
                'has_prev': page > 1,
            }

        except Exception as e:
            logger.error('Ошибка получения пользователей по кампаниям', error=e)
            return {
                'users': [],
                'campaigns': {},
                'current_page': 1,
                'total_pages': 1,
                'total_count': 0,
                'has_next': False,
                'has_prev': False,
            }

    async def update_user_balance(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        admin_id: int,
        bot: Bot | None = None,
    ) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            # Сохраняем старый баланс для уведомления

            if amount_kopeks > 0:
                await add_user_balance(
                    db, user, amount_kopeks, description=description, payment_method=PaymentMethod.MANUAL
                )
                logger.info(
                    'Админ пополнил баланс пользователя на ₽',
                    admin_id=admin_id,
                    user_id=user_id,
                    amount_kopeks=amount_kopeks / 100,
                )
                success = True
            else:
                success = await subtract_user_balance(
                    db,
                    user,
                    abs(amount_kopeks),
                    description,
                    create_transaction=True,
                    payment_method=PaymentMethod.MANUAL,
                )
                if success:
                    logger.info(
                        'Админ списал с баланса пользователя ₽',
                        admin_id=admin_id,
                        user_id=user_id,
                        value=abs(amount_kopeks) / 100,
                    )

            # Отправляем уведомление пользователю, если операция прошла успешно
            if success and bot:
                # Перечитываем человека целиком, а не db.refresh: refresh снимает загруженные
                # связи, а уведомление читает user.subscriptions — в асинхронной сессии
                # доленивиться нечем. get_user_by_id тянет подписки selectinload'ом.
                user = await get_user_by_id(db, user_id) or user

                # Отправляем уведомление (не блокируем операцию если не удалось отправить)
                await self.send_balance_change_notification(bot, user, amount_kopeks)

            return success

        except Exception as e:
            logger.error('Ошибка изменения баланса пользователя', error=e)
            return False

    async def update_user_promo_group(
        self, db: AsyncSession, user_id: int, promo_group_id: int
    ) -> tuple[bool, User | None, PromoGroup | None, PromoGroup | None]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False, None, None, None

            old_group = user.promo_group

            promo_group = await get_promo_group_by_id(db, promo_group_id)
            if not promo_group:
                return False, None, None, old_group

            user.promo_group_id = promo_group.id
            user.promo_group = promo_group
            user.updated_at = datetime.now(UTC)

            await db.commit()
            await db.refresh(user)

            logger.info(
                '👥 Промогруппа пользователя обновлена',
                telegram_id=user.telegram_id,
                promo_group_name=promo_group.name,
            )

            return True, user, promo_group, old_group

        except Exception as e:
            await db.rollback()
            logger.error('Ошибка обновления промогруппы пользователя', user_id=user_id, error=e)
            return False, None, None, None

    async def update_user_referrals(
        self,
        db: AsyncSession,
        user_id: int,
        referral_user_ids: list[int],
        admin_id: int,
    ) -> tuple[bool, dict[str, int]]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False, {'error': 'user_not_found'}

            unique_ids: list[int] = []
            for referral_id in referral_user_ids:
                if referral_id == user_id:
                    continue
                if referral_id not in unique_ids:
                    unique_ids.append(referral_id)

            current_referrals = await get_referrals(db, user_id)
            current_ids = {ref.id for ref in current_referrals}

            to_assign = unique_ids
            to_remove = [rid for rid in current_ids if rid not in unique_ids]
            to_add = [rid for rid in unique_ids if rid not in current_ids]

            if to_assign:
                await db.execute(update(User).where(User.id.in_(to_assign)).values(referred_by_id=user_id))

            if to_remove:
                await db.execute(update(User).where(User.id.in_(to_remove)).values(referred_by_id=None))

            await db.commit()

            logger.info(
                'Админ обновил рефералов пользователя : добавлено , удалено , всего',
                admin_id=admin_id,
                user_id=user_id,
                to_add_count=len(to_add),
                to_remove_count=len(to_remove),
                unique_ids_count=len(unique_ids),
            )

            return True, {
                'added': len(to_add),
                'removed': len(to_remove),
                'total': len(unique_ids),
            }

        except Exception as e:
            await db.rollback()
            logger.error('Ошибка обновления рефералов пользователя', user_id=user_id, e=e)
            return False, {'error': 'update_failed'}

    async def block_user(
        self, db: AsyncSession, user_id: int, admin_id: int, reason: str = 'Заблокирован администратором'
    ) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False

            from app.database.crud.subscription import deactivate_subscription, is_active_paid_subscription

            subs = getattr(user, 'subscriptions', None) or []
            has_active_paid = any(is_active_paid_subscription(sub) for sub in subs)

            if has_active_paid:
                logger.info(
                    '⏭️ Пропуск отключения RemnaWave и подписки: у пользователя активная оплаченная подписка',
                    user_id=user_id,
                    remnawave_uuid=user.remnawave_uuid,
                )
            else:
                from app.services.subscription_service import SubscriptionService

                subscription_service = SubscriptionService()

                if settings.is_multi_tariff_enabled():
                    # In multi-tariff mode, disable each subscription's panel user individually
                    for sub in subs:
                        panel_uuid = sub.remnawave_uuid
                        if panel_uuid:
                            try:
                                await subscription_service.disable_remnawave_user(panel_uuid)
                                logger.info(
                                    '✅ RemnaWave пользователь деактивирован при блокировке',
                                    remnawave_uuid=panel_uuid,
                                    subscription_id=sub.id,
                                )
                            except Exception as e:
                                logger.error(
                                    '❌ Ошибка деактивации RemnaWave при блокировке',
                                    error=e,
                                    subscription_id=sub.id,
                                )
                elif user.remnawave_uuid:
                    try:
                        await subscription_service.disable_remnawave_user(user.remnawave_uuid)
                        logger.info(
                            '✅ RemnaWave пользователь деактивирован при блокировке',
                            remnawave_uuid=user.remnawave_uuid,
                        )
                    except Exception as e:
                        logger.error('❌ Ошибка деактивации RemnaWave пользователя при блокировке', error=e)

                for sub in subs:
                    if sub.status in ['active', 'trial']:
                        await deactivate_subscription(db, sub)

            await update_user(db, user, status=UserStatus.BLOCKED.value)

            logger.info('Админ заблокировал пользователя', admin_id=admin_id, user_id=user_id, reason=reason)
            return True

        except Exception as e:
            logger.error('Ошибка блокировки пользователя', error=e)
            return False

    async def unblock_user(self, db: AsyncSession, user_id: int, admin_id: int) -> bool:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return False
            if getattr(user, 'account_erasure_requested_at', None) is not None:
                logger.warning(
                    'Разблокировка отклонена: идёт финансовое закрытие аккаунта',
                    admin_id=admin_id,
                    user_id=user_id,
                )
                return False

            await update_user(db, user, status=UserStatus.ACTIVE.value)

            from app.database.models import SubscriptionStatus

            now = datetime.now(UTC)
            access_point_reprojection_armed = False
            for sub in getattr(user, 'subscriptions', None) or []:
                if sub.end_date and sub.end_date > now and sub.status != SubscriptionStatus.ACTIVE.value:
                    from app.services.public_access_point_service import (
                        AccessPointPolicyError,
                        assert_no_manual_access_point_grant,
                    )

                    try:
                        await assert_no_manual_access_point_grant(db, sub, action='unblock_reactivation')
                    except AccessPointPolicyError:
                        from app.services.public_access_point_service import requeue_active_access_point_term_projection

                        if await requeue_active_access_point_term_projection(
                            db,
                            sub,
                            reason='account_unblock',
                        ):
                            sub.status = SubscriptionStatus.ACTIVE.value
                            access_point_reprojection_armed = True
                            logger.info('AP subscription rearmed from active captured term', subscription_id=sub.id)
                        continue
                    sub.status = SubscriptionStatus.ACTIVE.value
                    try:
                        from app.services.subscription_service import SubscriptionService

                        subscription_service = SubscriptionService()
                        await subscription_service.update_remnawave_user(db, sub)
                        logger.info(
                            '✅ RemnaWave подписка восстановлена при разблокировке',
                            subscription_id=sub.id,
                            remnawave_uuid=sub.remnawave_uuid
                            if settings.is_multi_tariff_enabled()
                            else user.remnawave_uuid,
                        )
                    except Exception as e:
                        logger.error(
                            '❌ Ошибка восстановления RemnaWave подписки при разблокировке',
                            subscription_id=sub.id,
                            error=e,
                        )
                        from app.services.remnawave_retry_queue import remnawave_retry_queue

                        if hasattr(sub, 'id') and hasattr(sub, 'user_id'):
                            remnawave_retry_queue.enqueue(
                                subscription_id=sub.id,
                                user_id=sub.user_id,
                                action='update',
                            )
            await db.commit()
            if access_point_reprojection_armed:
                from app.services.monitoring_service import monitoring_service

                monitoring_service.wake_access_point_term_projection_scheduler()

            logger.info('Админ разблокировал пользователя', admin_id=admin_id, user_id=user_id)
            return True

        except Exception as e:
            logger.error('Ошибка разблокировки пользователя', error=e)
            return False

    async def delete_user_account(
        self,
        db: AsyncSession,
        user_id: int,
        admin_id: int,
        *,
        force_panel_delete: bool = False,
        soft_delete_if_no_financial_history: bool = False,
    ) -> DeleteUserResult:
        """Удалить обычный аккаунт или безопасно закрыть финансовый.

        force_panel_delete=True: пропускает проверку активной подписки и принудительно
        удаляет (не деактивирует) пользователя из панели RemnaWave. Используется
        при полном удалении через кабинет администратора.

        ``soft_delete_if_no_financial_history`` preserves the legacy inactive
        cleanup behaviour only for users who have never entered checkout. It
        can never select the soft-delete path for financial accounts: they
        always go through the account-closure lifecycle.
        """
        result = DeleteUserResult()
        try:
            # Keep the same payment lock order as Device-First settlement:
            # payment -> user -> attempt -> checkout.  A plain existence
            # check followed by a physical delete could otherwise race a
            # callback which has already locked a provider payment.
            await db.execute(
                select(PlategaPayment)
                .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.platega_payment_id == PlategaPayment.id)
                .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
                .where(SubscriptionCheckout.user_id == user_id)
                .with_for_update(of=PlategaPayment)
            )
            user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
            if not user:
                logger.warning('Пользователь не найден для удаления', user_id=user_id)
                return result

            # Financial checkout evidence is deliberately retained (attempts
            # have RESTRICT FKs). A user with such history takes the single
            # account-erasure path: close access first, reconcile known
            # invoices, then anonymize the identity without breaking audit.
            has_financial_history, has_legacy_financial_history = await self._get_financial_history_kind(db, user_id)
            if has_financial_history:
                from app.services.account_erasure_service import request_financial_account_erasure

                erasure = await request_financial_account_erasure(
                    db,
                    user_id=user_id,
                    requested_by_user_id=admin_id or None,
                    # Financial closure always removes the remote identity.
                    # ``force_panel_delete`` applies only to the legacy path
                    # below and must not leave paid-account access alive.
                    deactivate_panel=True,
                    has_legacy_financial_history=has_legacy_financial_history,
                )
                result.bot_deleted = erasure.completed
                result.panel_deleted = erasure.panel_deactivated
                result.account_closed = erasure.state != 'not_found'
                result.erasure_state = erasure.state
                result.message = erasure.message
                logger.info(
                    'Финансовое закрытие пользователя обработано',
                    user_id=user_id,
                    state=erasure.state,
                    completed=erasure.completed,
                )
                return result

            if soft_delete_if_no_financial_history:
                user.status = UserStatus.DELETED.value
                user.updated_at = datetime.now(UTC)
                await db.commit()
                result.bot_deleted = True
                result.message = 'Пользователь помечен как удалён.'
                return result

            user_id_display = user.telegram_id or user.email or f'#{user.id}'
            logger.info('🗑️ Начинаем полное удаление пользователя', user_id=user_id, user_id_display=user_id_display)

            from app.config import settings
            from app.database.crud.subscription import is_active_paid_subscription

            # Collect all panel UUIDs to process
            subs = getattr(user, 'subscriptions', None) or []
            if settings.is_multi_tariff_enabled():
                panel_uuids = [sub.remnawave_uuid for sub in subs if sub.remnawave_uuid]
            else:
                panel_uuids = [user.remnawave_uuid] if user.remnawave_uuid else []

            if panel_uuids:
                if not force_panel_delete and any(is_active_paid_subscription(sub) for sub in subs):
                    logger.info(
                        '⏭️ Пропуск отключения RemnaWave при удалении: у пользователя активная оплаченная подписка',
                        user_id=user_id,
                    )
                else:
                    delete_mode = 'delete' if force_panel_delete else settings.get_remnawave_user_delete_mode()

                    # Помечаем ВСЕ UUID до цикла, чтобы webhook от первого удаления
                    # не пришёл раньше чем помечены остальные
                    if delete_mode == 'delete':
                        from app.services.remnawave_webhook_service import RemnaWaveWebhookService

                        RemnaWaveWebhookService.mark_intentional_panel_deletion(
                            panel_uuids=panel_uuids,
                            telegram_id=int(user.telegram_id) if user.telegram_id else None,
                        )

                    for panel_uuid in panel_uuids:
                        try:
                            from app.services.remnawave_service import RemnaWaveService

                            remnawave_service = RemnaWaveService()

                            if delete_mode == 'delete':
                                async with remnawave_service.get_api_client() as api:
                                    delete_success = await api.delete_user(panel_uuid)
                                    if delete_success:
                                        result.panel_deleted = True
                                        logger.info(
                                            '✅ RemnaWave пользователь удален из панели',
                                            remnawave_uuid=panel_uuid,
                                        )
                                    else:
                                        result.panel_error = 'Remnawave API вернул ошибку удаления'
                                        logger.warning(
                                            '⚠️ Не удалось удалить пользователя из панели Remnawave',
                                            remnawave_uuid=panel_uuid,
                                        )
                            else:
                                from app.services.subscription_service import SubscriptionService

                                subscription_service = SubscriptionService()
                                disabled = await subscription_service.disable_remnawave_user(panel_uuid)
                                result.panel_deleted = disabled
                                if disabled:
                                    logger.info(
                                        '✅ RemnaWave пользователь деактивирован',
                                        remnawave_uuid=panel_uuid,
                                        delete_mode=delete_mode,
                                    )
                                else:
                                    result.panel_error = 'disable_remnawave_user вернул False'
                                    logger.warning(
                                        '⚠️ Не удалось деактивировать пользователя в RemnaWave',
                                        remnawave_uuid=panel_uuid,
                                        delete_mode=delete_mode,
                                    )

                        except Exception as e:
                            result.panel_error = 'Ошибка обработки пользователя в Remnawave'
                            logger.warning(
                                '⚠️ Ошибка обработки пользователя в Remnawave',
                                delete_mode=delete_mode,
                                remnawave_uuid=panel_uuid,
                                error=e,
                            )
                            if delete_mode == 'delete':
                                try:
                                    from app.services.subscription_service import SubscriptionService

                                    subscription_service = SubscriptionService()
                                    disabled = await subscription_service.disable_remnawave_user(panel_uuid)
                                    if disabled:
                                        result.panel_deleted = True
                                        result.panel_error = 'Удаление не удалось, пользователь деактивирован'
                                        logger.info(
                                            '✅ RemnaWave пользователь деактивирован как fallback',
                                            remnawave_uuid=panel_uuid,
                                        )
                                except Exception as fallback_e:
                                    logger.error('❌ Ошибка деактивации RemnaWave как fallback', fallback_e=fallback_e)

            try:
                async with db.begin_nested():
                    sent_notifications_result = await db.execute(
                        select(SentNotification).where(SentNotification.user_id == user_id)
                    )
                    sent_notifications = sent_notifications_result.scalars().all()

                    if sent_notifications:
                        logger.info('🔄 Удаляем уведомлений', sent_notifications_count=len(sent_notifications))
                        await db.execute(delete(SentNotification).where(SentNotification.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления уведомлений', error=e)

            try:
                async with db.begin_nested():
                    user_messages_result = await db.execute(
                        update(UserMessage).where(UserMessage.created_by == user_id).values(created_by=None)
                    )
                    if user_messages_result.rowcount > 0:
                        logger.info('🔄 Обновлено пользовательских сообщений', rowcount=user_messages_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления пользовательских сообщений', error=e)

            try:
                async with db.begin_nested():
                    promocodes_result = await db.execute(
                        update(PromoCode).where(PromoCode.created_by == user_id).values(created_by=None)
                    )
                    if promocodes_result.rowcount > 0:
                        logger.info('🔄 Обновлено промокодов', rowcount=promocodes_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления промокодов', error=e)

            try:
                async with db.begin_nested():
                    welcome_texts_result = await db.execute(
                        update(WelcomeText).where(WelcomeText.created_by == user_id).values(created_by=None)
                    )
                    if welcome_texts_result.rowcount > 0:
                        logger.info('🔄 Обновлено приветственных текстов', rowcount=welcome_texts_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления приветственных текстов', error=e)

            try:
                async with db.begin_nested():
                    referrals_result = await db.execute(
                        update(User).where(User.referred_by_id == user_id).values(referred_by_id=None)
                    )
                    if referrals_result.rowcount > 0:
                        logger.info('🔗 Очищены реферальные ссылки у рефералов', rowcount=referrals_result.rowcount)
                    await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка очистки реферальных ссылок', error=e)

            try:
                async with db.begin_nested():
                    yookassa_result = await db.execute(
                        select(YooKassaPayment).where(YooKassaPayment.user_id == user_id)
                    )
                    yookassa_payments = yookassa_result.scalars().all()

                    if yookassa_payments:
                        logger.info('🔄 Удаляем YooKassa платежей', yookassa_payments_count=len(yookassa_payments))
                        await db.execute(
                            update(YooKassaPayment)
                            .where(YooKassaPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(YooKassaPayment).where(YooKassaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления YooKassa платежей', error=e)

            try:
                async with db.begin_nested():
                    cryptobot_result = await db.execute(
                        select(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id)
                    )
                    cryptobot_payments = cryptobot_result.scalars().all()

                    if cryptobot_payments:
                        logger.info('🔄 Удаляем CryptoBot платежей', cryptobot_payments_count=len(cryptobot_payments))
                        await db.execute(
                            update(CryptoBotPayment)
                            .where(CryptoBotPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(CryptoBotPayment).where(CryptoBotPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления CryptoBot платежей', error=e)

            try:
                async with db.begin_nested():
                    platega_result = await db.execute(select(PlategaPayment).where(PlategaPayment.user_id == user_id))
                    platega_payments = platega_result.scalars().all()

                    if platega_payments:
                        logger.info('🔄 Удаляем Platega платежей', platega_payments_count=len(platega_payments))
                        await db.execute(
                            update(PlategaPayment).where(PlategaPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(PlategaPayment).where(PlategaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Platega платежей', error=e)

            try:
                async with db.begin_nested():
                    mulenpay_result = await db.execute(
                        select(MulenPayPayment).where(MulenPayPayment.user_id == user_id)
                    )
                    mulenpay_payments = mulenpay_result.scalars().all()

                    if mulenpay_payments:
                        mulenpay_name = settings.get_mulenpay_display_name()
                        logger.info(
                            '🔄 Удаляем платежей',
                            mulenpay_payments_count=len(mulenpay_payments),
                            mulenpay_name=mulenpay_name,
                        )
                        await db.execute(
                            update(MulenPayPayment)
                            .where(MulenPayPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(MulenPayPayment).where(MulenPayPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error(
                    '❌ Ошибка удаления платежей',
                    get_mulenpay_display_name=settings.get_mulenpay_display_name(),
                    error=e,
                )

            try:
                async with db.begin_nested():
                    pal24_result = await db.execute(select(Pal24Payment).where(Pal24Payment.user_id == user_id))
                    pal24_payments = pal24_result.scalars().all()

                    if pal24_payments:
                        logger.info('🔄 Удаляем Pal24 платежей', pal24_payments_count=len(pal24_payments))
                        await db.execute(
                            update(Pal24Payment).where(Pal24Payment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(Pal24Payment).where(Pal24Payment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Pal24 платежей', error=e)

            try:
                async with db.begin_nested():
                    heleket_result = await db.execute(select(HeleketPayment).where(HeleketPayment.user_id == user_id))
                    heleket_payments = heleket_result.scalars().all()

                    if heleket_payments:
                        logger.info('🔄 Удаляем Heleket платежей', heleket_payments_count=len(heleket_payments))
                        await db.execute(
                            update(HeleketPayment).where(HeleketPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(HeleketPayment).where(HeleketPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Heleket платежей', error=e)

            # Удаляем Freekassa платежи
            try:
                async with db.begin_nested():
                    freekassa_payments_result = await db.execute(
                        select(FreekassaPayment).where(FreekassaPayment.user_id == user_id)
                    )
                    freekassa_payments = freekassa_payments_result.scalars().all()

                    if freekassa_payments:
                        logger.info('🔄 Удаляем Freekassa платежей', freekassa_payments_count=len(freekassa_payments))
                        await db.execute(
                            update(FreekassaPayment)
                            .where(FreekassaPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(FreekassaPayment).where(FreekassaPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Freekassa платежей', error=e)

            # Удаляем Wata платежи (до транзакций, т.к. wata_payments.transaction_id -> transactions.id)
            try:
                async with db.begin_nested():
                    wata_payments_result = await db.execute(select(WataPayment).where(WataPayment.user_id == user_id))
                    wata_payments = wata_payments_result.scalars().all()

                    if wata_payments:
                        logger.info('🔄 Удаляем Wata платежей', wata_payments_count=len(wata_payments))
                        await db.execute(
                            update(WataPayment).where(WataPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(WataPayment).where(WataPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления Wata платежей', error=e)

            # Удаляем CloudPayments платежи
            try:
                async with db.begin_nested():
                    cloudpayments_result = await db.execute(
                        select(CloudPaymentsPayment).where(CloudPaymentsPayment.user_id == user_id)
                    )
                    cloudpayments_payments = cloudpayments_result.scalars().all()

                    if cloudpayments_payments:
                        logger.info(
                            '🔄 Удаляем CloudPayments платежей',
                            cloudpayments_payments_count=len(cloudpayments_payments),
                        )
                        await db.execute(
                            update(CloudPaymentsPayment)
                            .where(CloudPaymentsPayment.user_id == user_id)
                            .values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(CloudPaymentsPayment).where(CloudPaymentsPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления CloudPayments платежей', error=e)

            # Удаляем KassaAi платежи
            try:
                async with db.begin_nested():
                    kassa_ai_result = await db.execute(select(KassaAiPayment).where(KassaAiPayment.user_id == user_id))
                    kassa_ai_payments = kassa_ai_result.scalars().all()

                    if kassa_ai_payments:
                        logger.info('🔄 Удаляем KassaAi платежей', kassa_ai_payments_count=len(kassa_ai_payments))
                        await db.execute(
                            update(KassaAiPayment).where(KassaAiPayment.user_id == user_id).values(transaction_id=None)
                        )
                        await db.flush()
                        await db.execute(delete(KassaAiPayment).where(KassaAiPayment.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления KassaAi платежей', error=e)

            # Платёжные провайдеры, которые ссылаются на transactions через FK без ON DELETE,
            # но раньше не очищались в этом блоке. Без них DELETE FROM transactions падал с
            # ForeignKeyViolationError (например, rollypay_payments_transaction_id_fkey).
            from app.database.models import (
                AntilopayPayment,
                AppleTransaction,
                AuraPayPayment,
                DonutPayment,
                EtoplatezhiPayment,
                JupiterPayment,
                LavaPayment,
                OverpayPayment,
                PayPearPayment,
                RioPayPayment,
                RollyPayPayment,
                SeverPayPayment,
            )

            extra_payment_models = (
                RioPayPayment,
                RollyPayPayment,
                SeverPayPayment,
                PayPearPayment,
                OverpayPayment,
                AuraPayPayment,
                EtoplatezhiPayment,
                AntilopayPayment,
                JupiterPayment,
                DonutPayment,
                LavaPayment,
            )
            for model in extra_payment_models:
                try:
                    async with db.begin_nested():
                        await db.execute(update(model).where(model.user_id == user_id).values(transaction_id=None))
                        await db.flush()
                        await db.execute(delete(model).where(model.user_id == user_id))
                        await db.flush()
                except Exception as error:
                    logger.error(
                        '❌ Ошибка удаления платежей провайдера',
                        provider=model.__tablename__,
                        error=str(error),
                    )

            # Apple IAP: FK поле называется transaction_id_fk (не transaction_id),
            # поэтому отдельным блоком. user_id имеет CASCADE на users, но это сработает
            # позже при DELETE User — а DELETE Transaction раньше падал бы из-за FK на apple_transactions.
            try:
                async with db.begin_nested():
                    await db.execute(
                        update(AppleTransaction)
                        .where(AppleTransaction.user_id == user_id)
                        .values(transaction_id_fk=None)
                    )
                    await db.flush()
                    await db.execute(delete(AppleTransaction).where(AppleTransaction.user_id == user_id))
                    await db.flush()
            except Exception as error:
                logger.error('❌ Ошибка удаления Apple IAP платежей', error=str(error))

            try:
                async with db.begin_nested():
                    transactions_result = await db.execute(select(Transaction).where(Transaction.user_id == user_id))
                    transactions = transactions_result.scalars().all()

                    if transactions:
                        logger.info('🔄 Удаляем транзакций', transactions_count=len(transactions))
                        await db.execute(delete(Transaction).where(Transaction.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления транзакций', error=e)

            try:
                async with db.begin_nested():
                    promocode_uses_result = await db.execute(
                        select(PromoCodeUse).where(PromoCodeUse.user_id == user_id)
                    )
                    promocode_uses = promocode_uses_result.scalars().all()

                    if promocode_uses:
                        logger.info('🔄 Удаляем использований промокодов', promocode_uses_count=len(promocode_uses))
                        await db.execute(delete(PromoCodeUse).where(PromoCodeUse.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления использований промокодов', error=e)

            try:
                async with db.begin_nested():
                    referral_earnings_result = await db.execute(
                        select(ReferralEarning).where(ReferralEarning.user_id == user_id)
                    )
                    referral_earnings = referral_earnings_result.scalars().all()

                    if referral_earnings:
                        logger.info('🔄 Удаляем реферальных доходов', referral_earnings_count=len(referral_earnings))
                        await db.execute(delete(ReferralEarning).where(ReferralEarning.user_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления реферальных доходов', error=e)

            try:
                async with db.begin_nested():
                    referral_records_result = await db.execute(
                        select(ReferralEarning).where(ReferralEarning.referral_id == user_id)
                    )
                    referral_records = referral_records_result.scalars().all()

                    if referral_records:
                        logger.info('🔄 Удаляем записей о рефералах', referral_records_count=len(referral_records))
                        await db.execute(delete(ReferralEarning).where(ReferralEarning.referral_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления записей о рефералах', error=e)

            try:
                async with db.begin_nested():
                    conversions_result = await db.execute(
                        select(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id)
                    )
                    conversions = conversions_result.scalars().all()

                    if conversions:
                        logger.info('🔄 Удаляем записей конверсий', conversions_count=len(conversions))
                        await db.execute(
                            delete(SubscriptionConversion).where(SubscriptionConversion.user_id == user_id)
                        )
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления записей конверсий', error=e)

            try:
                async with db.begin_nested():
                    broadcast_history_result = await db.execute(
                        select(BroadcastHistory).where(BroadcastHistory.admin_id == user_id)
                    )
                    broadcast_history = broadcast_history_result.scalars().all()

                    if broadcast_history:
                        logger.info(
                            '🔄 Удаляем записей истории рассылок', broadcast_history_count=len(broadcast_history)
                        )
                        await db.execute(delete(BroadcastHistory).where(BroadcastHistory.admin_id == user_id))
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка удаления истории рассылок', error=e)

            try:
                async with db.begin_nested():
                    campaigns_result = await db.execute(
                        select(AdvertisingCampaign).where(AdvertisingCampaign.created_by == user_id)
                    )
                    campaigns = campaigns_result.scalars().all()

                    if campaigns:
                        logger.info('🔄 Очищаем создателя у рекламных кампаний', campaigns_count=len(campaigns))
                        await db.execute(
                            update(AdvertisingCampaign)
                            .where(AdvertisingCampaign.created_by == user_id)
                            .values(created_by=None)
                        )
                        await db.flush()
            except Exception as e:
                logger.error('❌ Ошибка обновления рекламных кампаний', error=e)

            try:
                async with db.begin_nested():
                    subs = getattr(user, 'subscriptions', None) or []
                    if subs:
                        all_squad_ids: set[str] = set()
                        for sub in subs:
                            logger.info('🔄 Удаляем подписку', subscription_id=sub.id)
                            if sub.connected_squads:
                                all_squad_ids.update(sub.connected_squads)
                            await db.execute(
                                delete(SubscriptionServer).where(SubscriptionServer.subscription_id == sub.id)
                            )

                        # Delete all subscriptions for this user
                        # Lock order: subscriptions -> server_squads (matches webhook order)
                        await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
                        await db.flush()

                        # Decrement server_squads.current_users AFTER subscription delete
                        # to match lock ordering with webhook and avoid deadlocks
                        if all_squad_ids:
                            try:
                                from app.database.crud.server_squad import (
                                    get_server_ids_by_uuids,
                                    remove_user_from_servers,
                                )

                                int_squad_ids = await get_server_ids_by_uuids(db, list(all_squad_ids))
                                if int_squad_ids:
                                    await remove_user_from_servers(db, int_squad_ids)
                            except Exception as sq_err:
                                logger.warning('⚠️ Не удалось уменьшить счётчик серверов', error=sq_err)
            except Exception as e:
                logger.error('❌ Ошибка удаления подписок', error=e)

            try:
                from app.database.models import (
                    AccessPolicy,
                    AdminAuditLog,
                    AdminRole,
                    SavedPaymentMethod,
                    UserRole,
                    WithdrawalRequest,
                )

                await db.execute(delete(SavedPaymentMethod).where(SavedPaymentMethod.user_id == user_id))
                # RioPayPayment удаляется выше в extra_payment_models — здесь дубликат не нужен.
                await db.execute(delete(AdminAuditLog).where(AdminAuditLog.user_id == user_id))
                await db.execute(delete(WithdrawalRequest).where(WithdrawalRequest.user_id == user_id))
                await db.execute(
                    update(WithdrawalRequest).where(WithdrawalRequest.processed_by == user_id).values(processed_by=None)
                )
                await db.execute(update(AdminRole).where(AdminRole.created_by == user_id).values(created_by=None))
                await db.execute(update(UserRole).where(UserRole.assigned_by == user_id).values(assigned_by=None))
                await db.execute(update(AccessPolicy).where(AccessPolicy.created_by == user_id).values(created_by=None))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
                logger.info('✅ Пользователь окончательно удален из базы', user_id=user_id)
            except Exception as e:
                logger.error('❌ Ошибка финального удаления пользователя', error=e)
                await db.rollback()
                return result

            result.bot_deleted = True
            logger.info(
                '✅ Пользователь полностью удалён администратором',
                user_id_display=user_id_display,
                user_id=user_id,
                admin_id=admin_id,
            )
            return result

        except Exception as e:
            logger.error('❌ Критическая ошибка удаления пользователя', user_id=user_id, error=e)
            await db.rollback()
            return result

    async def get_user_statistics(self, db: AsyncSession) -> dict[str, Any]:
        try:
            stats = await get_users_statistics(db)
            return stats

        except Exception as e:
            logger.error('Ошибка получения статистики пользователей', error=e)
            return {
                'total_users': 0,
                'active_users': 0,
                'blocked_users': 0,
                'new_today': 0,
                'new_week': 0,
                'new_month': 0,
            }

    async def cleanup_inactive_users(self, db: AsyncSession, months: int = None) -> tuple[int, int]:
        """Clean up inactive users, skipping those with active subscriptions.

        Returns:
            Tuple of (deleted_count, skipped_active_sub_count).
        """
        try:
            if months is None:
                months = settings.INACTIVE_USER_DELETE_MONTHS

            inactive_users = await get_inactive_users(db, months)
            deleted_count = 0
            skipped_active_sub = 0

            for user in inactive_users:
                # Skip users with active paid subscriptions
                if any(sub.is_active for sub in (getattr(user, 'subscriptions', None) or [])):
                    skipped_active_sub += 1
                    continue

                delete_result = await self.delete_user_account(db, user.id, 0)
                if delete_result.bot_deleted:
                    deleted_count += 1

            if skipped_active_sub > 0:
                logger.info(
                    'Пропущено неактивных пользователей с активной подпиской', skipped_active_sub=skipped_active_sub
                )
            logger.info('Удалено неактивных пользователей', deleted_count=deleted_count)
            return deleted_count, skipped_active_sub

        except Exception as e:
            logger.error('Ошибка очистки неактивных пользователей', e=e)
            return 0, 0

    async def get_user_activity_summary(self, db: AsyncSession, user_id: int) -> dict[str, Any]:
        try:
            user = await get_user_by_id(db, user_id)
            if not user:
                return {}

            if settings.is_multi_tariff_enabled():
                from app.database.crud.subscription import get_active_subscriptions_by_user_id

                active_subs = await get_active_subscriptions_by_user_id(db, user_id)
                if active_subs:
                    _non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
                    _pool = _non_daily or active_subs
                    subscription = max(_pool, key=lambda s: s.days_left)
                else:
                    subscription = None
            else:
                subscription = await get_subscription_by_user_id(db, user_id)
            transactions_count = await get_user_transactions_count(db, user_id)

            days_since_registration = (datetime.now(UTC) - user.created_at).days

            days_since_activity = (datetime.now(UTC) - user.last_activity).days if user.last_activity else None

            return {
                'user_id': user.id,
                'telegram_id': user.telegram_id,
                'username': user.username,
                'full_name': user.full_name,
                'status': user.status,
                'language': user.language,
                'balance_kopeks': user.balance_kopeks,
                'registration_date': user.created_at,
                'last_activity': user.last_activity,
                'days_since_registration': days_since_registration,
                'days_since_activity': days_since_activity,
                'has_subscription': subscription is not None,
                'subscription_active': subscription.is_active if subscription else False,
                'subscription_trial': subscription.is_trial if subscription else False,
                'transactions_count': transactions_count,
                'referrer_id': user.referred_by_id,
                'referral_code': user.referral_code,
            }

        except Exception as e:
            logger.error('Ошибка получения сводки активности пользователя', user_id=user_id, error=e)
            return {}

    async def get_users_by_criteria(self, db: AsyncSession, criteria: dict[str, Any]) -> list[User]:
        try:
            status = criteria.get('status')
            criteria.get('has_subscription')
            criteria.get('is_trial')
            min_balance = criteria.get('min_balance', 0)
            max_balance = criteria.get('max_balance')
            days_inactive = criteria.get('days_inactive')

            registered_after = criteria.get('registered_after')
            registered_before = criteria.get('registered_before')

            users = await get_users_list(db, offset=0, limit=10000, status=status)

            filtered_users = []
            for user in users:
                if user.balance_kopeks < min_balance:
                    continue
                if max_balance and user.balance_kopeks > max_balance:
                    continue

                if registered_after and user.created_at < registered_after:
                    continue
                if registered_before and user.created_at > registered_before:
                    continue

                if days_inactive and user.last_activity:
                    inactive_threshold = datetime.now(UTC) - timedelta(days=days_inactive)
                    if user.last_activity > inactive_threshold:
                        continue

                filtered_users.append(user)

            return filtered_users

        except Exception as e:
            logger.error('Ошибка получения пользователей по критериям', error=e)
            return []


# ============================================================================
# Обнуление тестового аккаунта.
#
# 🔴 ЭТО ИНСТРУМЕНТ, А НЕ ПРОДУКТ. Он нужен, чтобы владелец мог проверять
# правки глазами клиента, не заводя новый Телеграм на каждую проверку.
#
# Действие необратимо и живёт под тремя НЕЗАВИСИМЫМИ заборами. Ни один из них
# не унаследован от соседнего кода — каждый спрашивает своё:
#   1. Телеграм обязан стоять в ``TEST_ACCOUNT_TELEGRAM_IDS`` (окружение).
#   2. У аккаунта не должно быть служебной роли и он не должен быть админом —
#      даже если его вписали в список по ошибке.
#   3. Никаких денег в пути.
#
# 🔴 «Финансовая сверка» (``resolve_financial_account_erasure``) — самый
# короткий путь к тому же результату, и он снимает забор №3 целиком. Здесь она
# НЕ используется намеренно.
# ============================================================================

# Колонка со ссылкой на пользователя означает «эта строка принадлежит человеку»
# только под такими именами. Всё остальное (``created_by``, ``admin_id``,
# ``actor_user_id``, ``referred_by_id`` …) — это ЧУЖАЯ строка, которая на
# человека лишь ссылается, и трогать её нельзя.
# 🔴 `referral_id` и `buyer_user_id` сюда НЕ входят, и это не забывчивость.
# В `referral_earnings` строка принадлежит тому, кто ЗАРАБОТАЛ (`user_id`), а
# `referral_id` — лишь указатель на приглашённого. Снести её по `referral_id`
# значит стереть запись о деньгах ЖИВОГО человека и отнять у него право вывода
# (`referral_withdrawal_service.py`: право = заработано − выведено). То же у
# `guest_purchases.buyer_user_id` — это оплаченный подарок покупателя.
_TEST_RESET_OWNERSHIP_COLUMNS = frozenset({'user_id', 'owner_user_id'})

# Эти таблицы не чистятся никогда. Три из четырёх при этом являются причиной
# ОТКАЗА: если в них есть строка, обнуление не начнётся вовсе.
_TEST_RESET_NEVER_WIPED = frozenset(
    {
        'admin_audit_log',  # несгораемый след админских действий
        'user_roles',  # есть роль → забор №2 отказал раньше
        'account_erasure_requests',  # незакрытая заявка → отказ, в чужую механику не лезем
        'device_first_reconciliation_credits',  # неразобранные деньги → отказ
        # Счётчик победителей раунда не уменьшается нигде, и снос попыток
        # оставил бы раунд с `winners_count`, который больше никто не наберёт.
        # Для «девственности» стенда конкурсы значения не имеют.
        'contest_attempts',
        # Идемпотентность оплаты Telegram Stars держится на `charge_id` этой
        # таблицы: снесём — и повторный колбэк начислит второй раз.
        'wheel_spins',
    }
)

# Положительные списки: всё, чего в них нет, считается «в пути» и запирает
# кнопку. Так новое состояние, о котором мы ещё не знаем, отказывает само.
# Списки положительные: всё, чего в них нет, считается «в пути» и запирает
# кнопку. Наборы взяты у САМОГО проекта, а не написаны заново, — тест
# `test_settled_lists_follow_the_project` краснеет, если проект их изменит.
#
# Заказ: `device_first_checkout_service.TERMINAL_STATES` минус два состояния,
# где деньги на разборе. `failed` и `reprice_required` — это остановившийся
# заказ, денег в нём нет, и снести его ОБЯЗАТЕЛЬНО: пока он жив, клиенту
# закрыт пробный период (`trial_activation_service` пускает только
# `cancelled`, `expired`, `ready`).
_TEST_RESET_MONEY_REVIEW_CHECKOUT_STATES = frozenset({'operator_review', 'conflict'})
_TEST_RESET_FINISHED_CHECKOUT_STATES = frozenset({'ready', 'cancelled', 'expired', 'failed', 'reprice_required'})
# 🔴 `paid_processing` НАМЕРЕННО остаётся у успешной прямой продажи навсегда —
# это запись идемпотентности, а не открытый счёт (`app/database/crud/tariff.py`
# объясняет это прямым текстом). Считать её «деньгами в пути» значит запереть
# кнопку на любом стенде, где хоть раз прошла настоящая покупка, — то есть
# сломать инструмент ровно тогда, когда он впервые понадобился.
_TEST_RESET_SETTLED_ATTEMPT_STATUSES = frozenset({'failed', 'credited', 'paid_processing'})
# Провайдер: `device_first_payment_service.PROVIDER_TERMINAL_STATUSES` плюс
# успех. Без `EXPIRED` брошенный счёт (обычнейшее действие на стенде: открыл
# оплату и закрыл вкладку) запирал бы кнопку навсегда.
_TEST_RESET_SETTLED_PROVIDER_STATUSES = frozenset({'CONFIRMED', 'FAILED', 'CANCELED', 'EXPIRED'})

# Кассы, про состояние которых эта кнопка судить не умеет: у каждой свой набор
# статусов, и разбирать двадцать наборов ради выключенных касс — не работа
# этого инструмента. Строки их обнуление сносит, поэтому наличие такой строки
# = отказ. Сегодня все они пусты и выключены; включат — кнопка честно скажет,
# что не берётся, вместо того чтобы снести молча.
# Список положительный, как и остальные: новый статус заявки запрёт кнопку,
# а не проскочит молча.
_TEST_RESET_FINISHED_WITHDRAWAL_STATUSES = frozenset({'rejected', 'completed', 'cancelled'})
_TEST_RESET_FINISHED_GIFT_STATUSES = frozenset({'delivered', 'failed', 'expired'})

_TEST_RESET_SUBSCRIPTION_STATE_RU = {
    'active': 'действует',
    'expired': 'закончилась',
    'disabled': 'отключена',
    'pending': 'ещё не оплачена',
    'limited': 'трафик исчерпан',
}

_TEST_RESET_UNJUDGED_PAYMENT_TABLES = (
    (YooKassaPayment, 'ЮKassa'),
    (CryptoBotPayment, 'CryptoBot'),
    (HeleketPayment, 'Heleket'),
    (MulenPayPayment, 'MulenPay'),
    (Pal24Payment, 'Pal24'),
    (WataPayment, 'Wata'),
    (CloudPaymentsPayment, 'CloudPayments'),
    (FreekassaPayment, 'FreeKassa'),
    (KassaAiPayment, 'Kassa.ai'),
    (AppleTransaction, 'Apple'),
    # 🔴 У этих одиннадцати `user_id` объявлен `SET NULL`, поэтому обнуление их
    # НЕ сносит — а их ссылка на проводку объявлена без правила удаления, и
    # живая строка запрещает удалить проводку вовсе. Промолчать про них значит
    # и пропустить платёж в пути, и намертво заклинить кнопку.
    (RioPayPayment, 'RioPay'),
    (SeverPayPayment, 'SeverPay'),
    (PayPearPayment, 'PayPear'),
    (RollyPayPayment, 'RollyPay'),
    (OverpayPayment, 'Overpay'),
    (AuraPayPayment, 'AuraPay'),
    (EtoplatezhiPayment, 'Этоплатежи'),
    (AntilopayPayment, 'Antilopay'),
    (JupiterPayment, 'Jupiter'),
    (DonutPayment, 'Donut'),
    (LavaPayment, 'Lava'),
)


@dataclass
class TestAccountResetPlan:
    """Что кнопка снесёт (и снесла) — один и тот же объект до и после."""

    allowed: bool = False
    blocked_reason: str | None = None
    balance_kopeks: int = 0
    subscription: str | None = None
    orders: int = 0
    payments: int = 0
    transactions: int = 0
    invited_users: int = 0
    tickets: int = 0
    panel_linked: bool = False
    done: bool = False
    panel_deleted: bool = False
    deleted_rows: dict[str, int] = dataclass_field(default_factory=dict)


async def notify_balance_change(db: AsyncSession, user_id: int, amount_kopeks: int) -> bool:
    """Сказать человеку, что его баланс изменили руками. Возвращает «дошло ли».

    Общая точка для поверхностей, у которых экземпляра бота нет в руках: кабинетная
    карточка, кабинетные массовые действия и Web API — все три живут в FastAPI. Бот
    создаётся на месте и закрывается, ровно как это уже делают выводы средств
    (`cabinet/routes/admin_withdrawals.py`) и реферальная регистрация.

    🔴 Сбой доставки НЕ роняет начисление: деньги уже на счету, и отказ Телеграма не
    повод возвращать админу ошибку. Поэтому здесь ловится всё и возвращается False.
    """
    if not settings.BOT_TOKEN:
        return False

    try:
        # Перечитываем целиком: нужен и свежий баланс, и подписки (selectinload) —
        # уведомление выбирает по ним кнопку, а доленивиться в асинхронной сессии нечем.
        user = await get_user_by_id(db, user_id)
        if not user:
            return False

        from app.bot_factory import create_bot

        bot = create_bot()
        try:
            # У телеграм-отправки три попытки по 15 с. Без потолка кнопка «Начислить»
            # в кабинете могла бы думать три четверти минуты.
            return await asyncio.wait_for(
                UserService().send_balance_change_notification(bot, user, amount_kopeks),
                timeout=20.0,
            )
        finally:
            await bot.session.close()
    except Exception as error:
        logger.error(
            'Не удалось сообщить человеку о ручной правке баланса',
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            error=error,
        )
        return False


def test_account_telegram_ids() -> frozenset[int]:
    """Разобрать список тестовых аккаунтов. Пусто по умолчанию и при мусоре.

    🔴 Читается ПРЯМО из окружения, а не через `Settings`, и это осознанно.
    Кабинет делает редактируемой каждую настройку класса `Settings`, которой
    нет в списке исключений, и применяет её без рестарта. Список доступа к
    необратимому действию не должен зависеть от того, не забыл ли кто-то
    вписать его в тот список: значения нет среди настроек вовсе, поэтому
    изменить его из кабинета, админки бота или Web API физически нечем.
    """
    result: set[int] = set()
    for raw in os.getenv('TEST_ACCOUNT_TELEGRAM_IDS', '').split(','):
        value = raw.strip()
        if not value:
            continue
        try:
            telegram_id = int(value)
        except ValueError:
            logger.warning('test_account_reset_invalid_allowlist_entry')
            continue
        if telegram_id > 0:
            result.add(telegram_id)
    return frozenset(result)


def is_test_account(user: User) -> bool:
    """Стенд опознаётся ТОЛЬКО по Телеграму из окружения."""
    telegram_id = getattr(user, 'telegram_id', None)
    if not telegram_id:
        return False
    return int(telegram_id) in test_account_telegram_ids()


def _test_reset_delete_plan(scopes: dict[str, list[int]]) -> list[tuple[Any, Any]]:
    """Пары «таблица, условие» в порядке удаления: дети раньше родителей.

    Порядок берётся у самой SQLAlchemy (``sorted_tables`` сортирует родителей
    первыми, здесь он развёрнут), а не пишется руками: рукописный порядок
    слепнет ровно на той таблице, которую добавят завтра.
    """
    from app.database.models import Base

    plan: list[tuple[Any, Any]] = []
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in _TEST_RESET_NEVER_WIPED:
            continue
        clauses = []
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                parent = f'{foreign_key.column.table.name}.{foreign_key.column.name}'
                if parent == 'users.id':
                    # 🔴 `SET NULL` — это прямое указание схемы «строка
                    # переживает человека»: так объявлены подарки, журнал
                    # злоупотреблений Apple и логи промо-предложений. Удалять
                    # их значит спорить со схемой и стирать чужие улики.
                    if (
                        column.name in _TEST_RESET_OWNERSHIP_COLUMNS
                        and (foreign_key.ondelete or '').upper() != 'SET NULL'
                    ):
                        clauses.append(column.in_(scopes['users.id']))
                    continue
                # Ссылка с ``SET NULL`` обнулится сама при удалении родителя —
                # её строка чужая и остаётся жить.
                if parent in scopes and scopes[parent] and (foreign_key.ondelete or '').upper() != 'SET NULL':
                    clauses.append(column.in_(scopes[parent]))
        if clauses:
            plan.append((table, or_(*clauses)))
    return plan


async def _test_reset_blocked_reason(db: AsyncSession, user: User) -> str | None:
    """Первая причина, по которой обнулять нельзя. ``None`` — можно."""
    from app.database.models import AccountErasureRequest, DeviceFirstReconciliationCredit, UserRole

    # Забор №2: служебный аккаунт не стенд, даже если его вписали в список.
    # Снос отвязал бы Телеграм и лишил человека входа в кабинет.
    if settings.is_admin(telegram_id=user.telegram_id, email=user.email):
        return 'Это админский аккаунт. Обнулять его нельзя: он потеряет доступ в кабинет.'
    if await db.scalar(select(UserRole.id).where(UserRole.user_id == user.id).limit(1)):
        return 'У этого человека есть служебная роль в кабинете. Обнулять его нельзя.'
    # Третий, отдельный реестр служебных людей: модераторы поддержки живут не в
    # базе, а в файле настроек. У такого человека нет ни роли, ни строки в
    # ADMIN_IDS — но он каждый день отвечает клиентам в тикетах.
    from app.services.support_settings_service import SupportSettingsService

    if user.telegram_id is not None and SupportSettingsService.is_moderator(int(user.telegram_id)):
        return 'Этот человек — модератор поддержки. Обнулять его нельзя.'

    # Забор №3: деньги. Каждая проверка спрашивает своё.
    #
    # 🔴 Везде спрашивается СОСТОЯНИЕ строки, а не её наличие. Строка заявки на
    # закрытие в этом проекте не удаляется никогда — «есть строка = отказ»
    # означало бы, что первое же нажатие «удалить пользователя» на стенде
    # убивает кнопку навсегда. То же с давно разобранным кредитом и с
    # отклонённой заявкой на вывод.
    if await db.scalar(
        select(AccountErasureRequest.id)
        .where(AccountErasureRequest.user_id == user.id, AccountErasureRequest.state != 'completed')
        .limit(1)
    ):
        return 'Этот аккаунт сейчас закрывается по финансовой сверке. Дождитесь конца — в эту механику не лезем.'
    # 🔴 Здесь спрашивается НАЛИЧИЕ, и это не забывчивость. Строка кредита
    # держит попытку оплаты ссылкой с запретом удаления, а саму строку
    # обнуление не трогает (это деньги). Значит даже разобранный кредит
    # заклинил бы удаление намертво — честнее отказать и позвать человека.
    if await db.scalar(
        select(DeviceFirstReconciliationCredit.id).where(DeviceFirstReconciliationCredit.user_id == user.id).limit(1)
    ):
        return (
            'На аккаунте есть кредит финансовой сверки. Эта кнопка с ним не справится '
            'и трогать его не будет — скажите разработчику.'
        )
    if await db.scalar(
        select(WithdrawalRequest.id)
        .where(
            WithdrawalRequest.user_id == user.id,
            WithdrawalRequest.status.not_in(sorted(_TEST_RESET_FINISHED_WITHDRAWAL_STATUSES)),
        )
        .limit(1)
    ):
        return 'На аккаунте есть незакрытая заявка на вывод денег. Закройте её и вернитесь.'

    money_review = (
        await db.execute(
            select(SubscriptionCheckout.id)
            .where(
                SubscriptionCheckout.user_id == user.id,
                SubscriptionCheckout.lifecycle_state.in_(sorted(_TEST_RESET_MONEY_REVIEW_CHECKOUT_STATES)),
            )
            .limit(1)
        )
    ).first()
    if money_review is not None:
        return (
            f'Заказ №{money_review[0]} лежит на разборе — по нему могли взять деньги. '
            'Разберите его в админ-панели бота, раздел «Заказы на разборе», и вернитесь.'
        )

    live_checkout = (
        await db.execute(
            select(SubscriptionCheckout.id)
            .where(
                SubscriptionCheckout.user_id == user.id,
                SubscriptionCheckout.lifecycle_state.not_in(sorted(_TEST_RESET_FINISHED_CHECKOUT_STATES)),
            )
            .limit(1)
        )
    ).first()
    if live_checkout is not None:
        # 🔴 Не обещаем, что «завершится сам»: протухший заказ гасится только
        # при НОВОЙ покупке того же человека, и даже тогда не гасится, если по
        # нему есть попытка оплаты. Обещание самопроизвольного конца было бы
        # неправдой ровно в том случае, который случается чаще всего.
        return (
            f'Заказ №{live_checkout[0]} не закончен — по нему может идти сверка с банком. '
            'Сам он не закроется. Если висит больше суток — скажите разработчику.'
        )

    unsettled_attempt = (
        await db.execute(
            select(CheckoutPaymentAttempt.id)
            .join(SubscriptionCheckout, SubscriptionCheckout.id == CheckoutPaymentAttempt.checkout_id)
            .where(
                SubscriptionCheckout.user_id == user.id,
                CheckoutPaymentAttempt.status.not_in(sorted(_TEST_RESET_SETTLED_ATTEMPT_STATUSES)),
            )
            .limit(1)
        )
    ).first()
    if unsettled_attempt is not None:
        return (
            f'Оплата по заказу №{unsettled_attempt[0]} ещё сверяется с банком. '
            'Подождите, пока сверка закончится, и вернитесь.'
        )

    unsettled_payment = (
        await db.execute(
            select(PlategaPayment.amount_kopeks)
            .where(
                PlategaPayment.user_id == user.id,
                func.upper(PlategaPayment.status).not_in(sorted(_TEST_RESET_SETTLED_PROVIDER_STATUSES)),
            )
            .limit(1)
        )
    ).first()
    if unsettled_payment is not None:
        amount = (unsettled_payment[0] or 0) / 100
        return f'Платёж на {amount:.2f} ₽ ещё не досверен с банком. По вашему правилу такие деньги ждём, а не удаляем.'

    # 🔴 Забор смотрит на Platega. Остальные кассы у проекта выключены, но их
    # строки обнуление всё равно сносит, поэтому судить о них мы не можем:
    # есть строка чужой кассы — отказываем и зовём человека, а не гадаем.
    # Подарок судим по статусу: завершённый — просто история (строку схема
    # велит сохранить), незавершённый — деньги в пути.
    if await db.scalar(
        select(GuestPurchase.id)
        .where(
            or_(GuestPurchase.user_id == user.id, GuestPurchase.buyer_user_id == user.id),
            GuestPurchase.status.not_in(sorted(_TEST_RESET_FINISHED_GIFT_STATUSES)),
        )
        .limit(1)
    ):
        return 'На аккаунте есть незавершённый подарок — деньги по нему могут быть в пути.'

    for model, human in _TEST_RESET_UNJUDGED_PAYMENT_TABLES:
        if await db.scalar(select(model.id).where(model.user_id == user.id).limit(1)):
            return (
                f'На аккаунте есть платежи через {human}, а про них эта кнопка судить не умеет. '
                'Скажите разработчику — обнулять вслепую не будем.'
            )
    return None


async def _test_reset_delete_panel_identity(user: User, panel_uuids: list[str]) -> bool:
    """Удалить пользователя из панели RemnaWave. ``False`` — не удалось.

    Логика взята у штатного закрытия аккаунта (``account_erasure_service``), а
    не написана рядом заново. Две вещи оттуда критичны:

    * UUID доискивается по Телеграму и почте. POST мог дойти до панели, а его
      ответ потеряться — тогда локального UUID нет, а пользователь в панели
      есть, и «мусора не осталось» было бы неправдой.
    * 404 — это УСПЕХ. Панель могли почистить руками, и на стенде это норма.
      Считать 404 отказом значило бы запереть кнопку навсегда.
    """
    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.remnawave_service import RemnaWaveService
    from app.services.remnawave_webhook_service import RemnaWaveWebhookService

    found = set(panel_uuids)
    try:
        async with RemnaWaveService().get_api_client() as api:
            if user.telegram_id is not None:
                found.update(
                    item.uuid for item in await api.get_user_by_telegram_id(int(user.telegram_id)) if item.uuid
                )
            if user.email:
                found.update(item.uuid for item in await api.get_user_by_email(user.email) if item.uuid)
            if not found:
                return True

            RemnaWaveWebhookService.mark_intentional_panel_deletion(
                panel_uuids=sorted(found),
                telegram_id=int(user.telegram_id) if user.telegram_id is not None else None,
            )
            for panel_uuid in sorted(found):
                try:
                    deleted = await api.delete_user(panel_uuid)
                except RemnaWaveAPIError as error:
                    if error.status_code != 404:
                        raise
                    deleted = True
                if not deleted:
                    logger.warning('test_account_reset_panel_delete_returned_false', remnawave_uuid=panel_uuid)
                    return False
    except Exception as error:
        logger.error('test_account_reset_panel_delete_error', error=error)
        return False
    return True


async def reset_test_account(
    db: AsyncSession,
    user: User,
    admin_id: int | None,
    *,
    confirm: bool,
) -> TestAccountResetPlan:
    """Показать план обнуления стенда, а при ``confirm`` — выполнить его.

    Показ и выполнение идут ОДНИМ путём: то, что владелец прочитал на первом
    нажатии, и то, что снесётся на втором, считается одним и тем же кодом.
    """
    from app.database.crud.subscription import decrement_subscription_server_counts

    # Номер берётся ДО первой записи: любой откат ниже «протухает» ORM-объект,
    # и обращение к его полю после этого попытается сходить в базу вне сессии.
    user_id = user.id
    subscriptions = (await db.execute(select(Subscription).where(Subscription.user_id == user_id))).scalars().all()
    sub_ids = [sub.id for sub in subscriptions]
    checkout_ids = list(
        (await db.execute(select(SubscriptionCheckout.id).where(SubscriptionCheckout.user_id == user_id)))
        .scalars()
        .all()
    )
    attempt_ids = (
        list(
            (
                await db.execute(
                    select(CheckoutPaymentAttempt.id).where(CheckoutPaymentAttempt.checkout_id.in_(checkout_ids))
                )
            )
            .scalars()
            .all()
        )
        if checkout_ids
        else []
    )
    term_ids: list[int] = []
    if sub_ids:
        from app.database.models import SubscriptionEntitlementTerm

        term_ids = list(
            (
                await db.execute(
                    select(SubscriptionEntitlementTerm.id).where(
                        SubscriptionEntitlementTerm.subscription_id.in_(sub_ids)
                    )
                )
            )
            .scalars()
            .all()
        )

    plan = TestAccountResetPlan(
        balance_kopeks=int(user.balance_kopeks or 0),
        orders=len(checkout_ids),
        payments=int(
            await db.scalar(select(func.count(PlategaPayment.id)).where(PlategaPayment.user_id == user_id)) or 0
        ),
        transactions=int(
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user_id)) or 0
        ),
        invited_users=int(await db.scalar(select(func.count(User.id)).where(User.referred_by_id == user_id)) or 0),
        # Обращения в поддержку исчезают вместе с перепиской ВНУТРИ них — в том
        # числе с ответами менеджера. Владелец обязан это видеть до нажатия, а
        # не узнавать постфактум: их удаление ничем на экране не отражалось.
        tickets=int(await db.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == user_id)) or 0),
        panel_linked=bool(user.remnawave_uuid) or any(sub.remnawave_uuid for sub in subscriptions),
    )
    if subscriptions:
        first = subscriptions[0]
        kind = 'пробная' if first.is_trial else 'платная'
        # Владелец читает это перед необратимым действием. Сырое `active` из
        # базы ему ничего не говорит; неизвестное состояние показываем как есть,
        # но не притворяемся, что перевели.
        state = _TEST_RESET_SUBSCRIPTION_STATE_RU.get(str(first.status), str(first.status))
        tail = f' и ещё {len(subscriptions) - 1}' if len(subscriptions) > 1 else ''
        plan.subscription = f'{kind}, {state}, до {first.end_date:%d.%m.%Y}{tail}'

    plan.blocked_reason = await _test_reset_blocked_reason(db, user)
    plan.allowed = plan.blocked_reason is None
    if not plan.allowed or not confirm:
        return plan

    panel_uuids = sorted(
        {value for value in [user.remnawave_uuid, *(sub.remnawave_uuid for sub in subscriptions)] if value}
    )
    scopes = {
        'users.id': [user_id],
        'subscriptions.id': sub_ids,
        'subscription_checkouts.id': checkout_ids,
        'checkout_payment_attempts.id': attempt_ids,
        'subscription_entitlement_terms.id': term_ids,
    }

    # 🔴 Панель — ПЕРВОЙ и ВНЕ транзакции. Обратный порядок держал бы
    # эксклюзивную блокировку на строках ОБЩИХ серверов всё время сетевого
    # вызова (у панели таймаут 60 с и три повтора), и обнуление стенда
    # тормозило бы покупки живых клиентов. Проект пишет это правило прямым
    # текстом в `trial_activation_service`: не держать блокировки через IO.
    #
    # Удаление в панели идемпотентно (404 = успех), поэтому повтор безопасен:
    # если база ниже не дастся, владелец нажмёт ещё раз и дойдёт до конца.
    plan.panel_deleted = await _test_reset_delete_panel_identity(user, panel_uuids)
    if not plan.panel_deleted:
        plan.allowed = False
        plan.blocked_reason = 'Панель RemnaWave не ответила. В базе ничего не тронуто — нажмите ещё раз чуть позже.'
        return plan

    try:
        # Счётчики занятости серверов принадлежат ЧУЖИМ строкам: не уменьшив их
        # до удаления связок, мы испортим общий сервер для всех остальных.
        for subscription in subscriptions:
            await decrement_subscription_server_counts(db, subscription)

        # Использования промокодов удаляются, а счётчик у самого промокода —
        # общий. Не уменьшив его, мы сожгли бы чужому промокоду места: у кода
        # с лимитом 1 после первой же проверки на стенде не осталось бы ни
        # одного. Проект уже умеет это откатывать (`promocode_service`).
        used_promocode_ids = list(
            (await db.execute(select(PromoCodeUse.promocode_id).where(PromoCodeUse.user_id == user_id))).scalars().all()
        )
        for promocode_id in used_promocode_ids:
            await db.execute(
                update(PromoCode)
                .where(PromoCode.id == promocode_id, PromoCode.current_uses > 0)
                .values(current_uses=PromoCode.current_uses - 1)
            )

        # 🔴 Строку заработка реферера мы храним — это ЕГО деньги. Но она
        # ссылается на проводку стенда ссылкой без правила удаления, и живая
        # ссылка запретит удалить проводку вовсе: обнуление падало бы каждый
        # раз, уже ПОСЛЕ удаления пользователя из панели. Тот же шаг делает
        # штатное восстановление при `/start`.
        await db.execute(
            update(ReferralEarning)
            .where(or_(ReferralEarning.user_id == user_id, ReferralEarning.referral_id == user_id))
            .values(referral_transaction_id=None)
        )

        for table, whereclause in _test_reset_delete_plan(scopes):
            result = await db.execute(delete(table).where(whereclause))
            if result.rowcount:
                plan.deleted_rows[table.name] = int(result.rowcount)

        # Единственная строка «про этого человека» без внешнего ключа: она
        # висит на telegram_id, поэтому обход по метаданным её не находит.
        if user.telegram_id is not None:
            from app.database.models import UserChannelSubscription

            result = await db.execute(
                delete(UserChannelSubscription).where(UserChannelSubscription.telegram_id == user.telegram_id)
            )
            if result.rowcount:
                plan.deleted_rows['user_channel_subscriptions'] = int(result.rowcount)

        # Цены, которые увидит «новичок», зависят от промо-группы и от того,
        # пополнял ли он хоть раз. Не сбросив это, стенд показал бы на экране
        # «Тарифы» не те цены, что настоящий новый клиент, — то есть ровно ту
        # ложную картину, из-за которой этот инструмент и понадобился.
        default_group = await get_default_promo_group(db)
        if default_group is not None:
            user.promo_group_id = default_group.id
        user.has_made_first_topup = False
        user.auto_promo_group_assigned = False
        user.auto_promo_group_threshold_kopeks = 0

        # Запреты покупки и пополнения переживают удаление строк — а проверять
        # «что видит ограниченный клиент» на стенде совершенно естественно.
        # Не сняв их, мы вернули бы аккаунт, который не умеет покупать.
        user.restriction_topup = False
        user.restriction_subscription = False
        user.restriction_reason = None
        user.promo_offer_discount_percent = 0
        user.promo_offer_discount_source = None
        user.promo_offer_discount_expires_at = None

        user.balance_kopeks = 0
        user.remnawave_uuid = None
        user.has_had_paid_subscription = False
        user.referred_by_id = None
        user.used_promocodes = 0
        user.status = UserStatus.DELETED.value
        user.account_erasure_requested_at = None
        user.updated_at = datetime.now(UTC)
        await db.commit()
    except Exception as error:
        await db.rollback()
        logger.error('test_account_reset_failed', user_id=user_id, error=error)
        plan.allowed = False
        plan.deleted_rows = {}
        plan.blocked_reason = (
            'Обнулить не удалось: в базе ничего не изменено, но в панели RemnaWave пользователь '
            'уже удалён. Нажмите ещё раз — повтор безопасен.'
        )
        return plan

    plan.done = True
    logger.info(
        'test_account_reset_done',
        user_id=user_id,
        admin_id=admin_id,
        deleted_rows=plan.deleted_rows,
        panel_uuids=len(panel_uuids),
    )
    return plan

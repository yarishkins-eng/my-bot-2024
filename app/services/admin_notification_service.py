import asyncio
import html
import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, NamedTuple

import structlog
from aiogram import Bot, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import select
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.promo_group import get_promo_group_by_id
from app.database.crud.subscription_event import create_subscription_event
from app.database.crud.transaction import get_transaction_by_id
from app.database.crud.user import get_user_by_id
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    GuestPurchase,
    PromoCodeType,
    PromoGroup,
    Subscription,
    Tariff,
    Transaction,
    User,
)
from app.services.manager_alert_service import ManagerAlertTopic, manager_alert_service
from app.utils.formatters import format_days_declension, format_devices_declension
from app.utils.message_patch import caption_exceeds_telegram_limit
from app.utils.pricing_utils import calculate_months_from_days
from app.utils.timezone import format_local_datetime


# Стандартный формат Telegram bot token: `<numeric_id>:<random_35chars>`.
# Может появиться в str(e) от aiogram при сетевых ошибках, если транспорт
# (httpx/aiohttp) сериализует URL `https://api.telegram.org/bot<TOKEN>/...`.
# Не светим токен в логи (структурированные логи могут уехать в Sentry / ELK).
# Trailing — negative lookahead, а не `\b`: иначе токены, оканчивающиеся
# на `-` или `_`, теряли последний символ при редакции (1-char leak).
# Leading `(?<![\w-])` — намеренно НЕ матчит, если перед токеном стоит word/digit
# (например `foo123456789:AAH...`). Это trade-off против false-positive'ов
# на timestamp/UUID-подобных последовательностях. Aiogram и httpx всегда
# префиксят токен либо `bot`, либо URL-границей (`/`, пробел, кавычка),
# так что реальный corpus ошибок не страдает.
_BOT_TOKEN_RE: re.Pattern[str] = re.compile(
    r'(?<![\w-])(?:bot)?\d{6,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])',
)


def _redact_telegram_secrets(text: str) -> str:
    """Replace Telegram bot tokens in an arbitrary string with a placeholder."""
    return _BOT_TOKEN_RE.sub('bot[REDACTED]', text)


class NotificationCategory(StrEnum):
    """Категории уведомлений для маршрутизации по топикам."""

    PURCHASES = 'purchases'  # Покупки подписок, покупки с лендинга
    RENEWALS = 'renewals'  # Продления
    TRIALS = 'trials'  # Триалы
    BALANCE = 'balance'  # Пополнение баланса
    ADDONS = 'addons'  # Докупка трафика/устройств/серверов
    INFRASTRUCTURE = 'infrastructure'  # Ноды, техработы, статус панели, вебхуки
    ERRORS = 'errors'  # Ошибки бота, краши
    PROMO = 'promo'  # Промокоды, кампании, промогруппы
    PARTNERS = 'partners'  # Партнёрки, выводы, админ-действия
    TICKETS = 'tickets'  # Тикеты (уже существует)


logger = structlog.get_logger(__name__)

# Как называть способ оплаты Platega в карточке владельцу — по коду метода из
# settings.get_platega_method_definitions(); код без записи здесь зовётся именем провайдера.
_PLATEGA_METHOD_LABELS = {2: 'по СБП', 11: 'картой', 12: 'зарубежной картой', 13: 'криптой'}


class OwnerCartHint(NamedTuple):
    text: str  # «продление подписки на 30 дней (Базовый)»
    auto: bool  # спишет ли бот сам следом (тогда придёт карточка покупки)
    by_client: bool = False  # списание ждёт нажатия клиента в кабинете (докупка устройств)


def platega_method_label(method_code: int | None) -> str:
    """«по СБП» / «картой» по коду метода Platega; неизвестный код — именем провайдера."""
    try:
        code = int(method_code) if method_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in _PLATEGA_METHOD_LABELS:
        return _PLATEGA_METHOD_LABELS[code]
    return f'через {html.escape(settings.get_platega_display_name())}'


class AdminNotificationService:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.chat_id = getattr(settings, 'ADMIN_NOTIFICATIONS_CHAT_ID', None)
        self.topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TOPIC_ID', None)
        self.ticket_topic_id = getattr(settings, 'ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID', None)
        self.enabled = getattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)

        # Маппинг категорий на topic_id (None = fallback на self.topic_id)
        self.category_topics: dict[NotificationCategory, int | None] = {
            NotificationCategory.PURCHASES: getattr(settings, 'ADMIN_NOTIFICATIONS_PURCHASES_TOPIC_ID', None),
            NotificationCategory.RENEWALS: getattr(settings, 'ADMIN_NOTIFICATIONS_RENEWALS_TOPIC_ID', None),
            NotificationCategory.TRIALS: getattr(settings, 'ADMIN_NOTIFICATIONS_TRIALS_TOPIC_ID', None),
            NotificationCategory.BALANCE: getattr(settings, 'ADMIN_NOTIFICATIONS_BALANCE_TOPIC_ID', None),
            NotificationCategory.ADDONS: getattr(settings, 'ADMIN_NOTIFICATIONS_ADDONS_TOPIC_ID', None),
            NotificationCategory.INFRASTRUCTURE: getattr(settings, 'ADMIN_NOTIFICATIONS_INFRASTRUCTURE_TOPIC_ID', None),
            NotificationCategory.ERRORS: getattr(settings, 'ADMIN_NOTIFICATIONS_ERRORS_TOPIC_ID', None),
            NotificationCategory.PROMO: getattr(settings, 'ADMIN_NOTIFICATIONS_PROMO_TOPIC_ID', None),
            NotificationCategory.PARTNERS: getattr(settings, 'ADMIN_NOTIFICATIONS_PARTNERS_TOPIC_ID', None),
            NotificationCategory.TICKETS: self.ticket_topic_id,
        }

        # Per-category enabled flags (default True — backwards compatible)
        self.category_enabled: dict[NotificationCategory, bool] = {}
        for cat in NotificationCategory:
            key = f'ADMIN_NOTIFICATIONS_{cat.value.upper()}_ENABLED'
            self.category_enabled[cat] = getattr(settings, key, True)

    async def _get_referrer_info(self, db: AsyncSession, referred_by_id: int | None) -> str:
        if not referred_by_id:
            return 'Нет'

        try:
            referrer = await get_user_by_id(db, referred_by_id)
            if not referrer:
                return f'ID {referred_by_id} (не найден)'

            if referrer.username:
                return f'@{html.escape(referrer.username)} (ID: {referred_by_id})'
            if referrer.telegram_id:
                return f'ID {referrer.telegram_id}'
            if referrer.email:
                return f'📧 {html.escape(referrer.email)}'
            return f'User#{referred_by_id}'

        except Exception as e:
            logger.error('Ошибка получения данных рефера', referred_by_id=referred_by_id, error=e)
            return f'ID {referred_by_id}'

    async def _get_user_promo_group(self, db: AsyncSession, user: User) -> PromoGroup | None:
        if promo_group := user.__dict__.get('promo_group'):
            return promo_group

        if not user.promo_group_id:
            return None

        try:
            await db.refresh(user, attribute_names=['promo_group'])
        except Exception:
            # relationship might not be available — fallback to direct fetch
            pass

        if promo_group := user.__dict__.get('promo_group'):
            return promo_group

        try:
            return await get_promo_group_by_id(db, user.promo_group_id)
        except Exception as e:
            logger.error(
                'Ошибка загрузки промогруппы пользователя',
                promo_group_id=user.promo_group_id,
                telegram_id=user.telegram_id,
                e=e,
            )
            return None

    def _get_user_display(self, user: User) -> str:
        first_name = getattr(user, 'first_name', '') or ''
        if first_name:
            return html.escape(first_name)

        username = getattr(user, 'username', '') or ''
        if username:
            return html.escape(username)

        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id is None:
            email = getattr(user, 'email', None)
            if email:
                return html.escape(email)
            return f'User#{getattr(user, "id", "Unknown")}'
        return f'ID{telegram_id}'

    def _get_user_identifier_display(self, user: User) -> str:
        """Get user identifier for display in notifications (telegram_id or email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return f'<code>{telegram_id}</code>'

        email = getattr(user, 'email', None)
        if email:
            return f'📧 {html.escape(email)}'

        return f'User#{getattr(user, "id", "Unknown")}'

    def _get_user_identifier_label(self, user: User) -> str:
        """Get label for user identifier (Telegram ID or Email)."""
        telegram_id = getattr(user, 'telegram_id', None)
        if telegram_id:
            return 'Telegram ID'
        email = getattr(user, 'email', None)
        if email:
            return 'Email'
        return 'ID'

    async def _record_subscription_event(
        self,
        db: AsyncSession,
        *,
        event_type: str,
        user: User,
        subscription: Subscription | None,
        transaction: Transaction | None = None,
        amount_kopeks: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Persist subscription-related event for external dashboards."""

        try:
            await create_subscription_event(
                db,
                user_id=user.id,
                event_type=event_type,
                subscription_id=subscription.id if subscription else None,
                transaction_id=transaction.id if transaction else None,
                amount_kopeks=amount_kopeks,
                currency=None,
                message=message,
                occurred_at=occurred_at,
                extra=extra or None,
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие подписки для пользователя',
                event_type=event_type,
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

            try:
                await db.rollback()
            except Exception:
                logger.error(
                    'Не удалось выполнить rollback после ошибки события подписки пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

    def _format_promo_group_discounts(self, promo_group: PromoGroup) -> list[str]:
        discount_lines: list[str] = []

        discount_map = {
            'servers': ('Серверы', promo_group.server_discount_percent),
            'traffic': ('Трафик', promo_group.traffic_discount_percent),
            'devices': ('Устройства', promo_group.device_discount_percent),
        }

        for title, percent in discount_map.values():
            if percent and percent > 0:
                discount_lines.append(f'• {title}: -{percent}%')

        period_discounts_raw = promo_group.period_discounts or {}
        period_items: list[tuple[int, int]] = []

        if isinstance(period_discounts_raw, dict):
            for raw_days, raw_percent in period_discounts_raw.items():
                try:
                    days = int(raw_days)
                    percent = int(raw_percent)
                except (TypeError, ValueError):
                    continue

                if percent > 0:
                    period_items.append((days, percent))

        period_items.sort(key=lambda item: item[0])

        if period_items:
            formatted_periods = ', '.join(f'{days} д. — -{percent}%' for days, percent in period_items)
            discount_lines.append(f'• Периоды: {formatted_periods}')

        if promo_group.apply_discounts_to_addons:
            discount_lines.append('• Доп. услуги: ✅ скидка действует')
        else:
            discount_lines.append('• Доп. услуги: ❌ без скидки')

        return discount_lines

    def _format_promo_group_block(
        self,
        promo_group: PromoGroup | None,
        *,
        title: str = 'Промогруппа',
        icon: str = '🏷️',
    ) -> str:
        if not promo_group:
            return f'{icon} <b>{title}:</b> —'

        lines = [f'{icon} <b>{title}:</b> {html.escape(promo_group.name)}']

        discount_lines = self._format_promo_group_discounts(promo_group)
        if discount_lines:
            lines.append('💸 <b>Скидки:</b>')
            lines.extend(discount_lines)
        else:
            lines.append('💸 <b>Скидки:</b> отсутствуют')

        return '\n'.join(lines)

    def _get_promocode_type_display(self, promo_type: str | None) -> str:
        mapping = {
            PromoCodeType.BALANCE.value: '💰 Бонус на баланс',
            PromoCodeType.SUBSCRIPTION_DAYS.value: '⏰ Доп. дни подписки',
            PromoCodeType.TRIAL_SUBSCRIPTION.value: '🎁 Триал подписка',
            PromoCodeType.PROMO_GROUP.value: '👥 Промогруппа',
            PromoCodeType.DISCOUNT.value: '💸 Скидка',
        }

        if not promo_type:
            return 'ℹ️ Не указан'

        return mapping.get(promo_type, f'ℹ️ {promo_type}')

    def _format_campaign_bonus(self, campaign: AdvertisingCampaign, *, tariff_name: str | None = None) -> list[str]:
        if campaign.is_balance_bonus:
            return [
                f'💰 Баланс: {settings.format_price(campaign.balance_bonus_kopeks or 0)}',
            ]

        if campaign.is_subscription_bonus:
            default_devices = getattr(settings, 'DEFAULT_DEVICE_LIMIT', 1)
            details = [
                f'📅 {campaign.subscription_duration_days or 0} дн. '
                f'• 📊 {campaign.subscription_traffic_gb or 0} ГБ '
                f'• 📱 {campaign.subscription_device_limit or default_devices} устр.',
            ]
            if campaign.subscription_squads:
                details.append(f'🌐 Сквады: {len(campaign.subscription_squads)} шт.')
            return details

        if campaign.is_tariff_bonus:
            name = tariff_name or f'ID {campaign.tariff_id}'
            details = [f'📦 Тариф: <b>{name}</b>']
            if campaign.tariff_duration_days:
                details.append(f'📅 Период: {campaign.tariff_duration_days} дней')
            return details

        if campaign.is_none_bonus:
            return ['🔗 Только отслеживание']

        return ['ℹ️ Бонусы не предусмотрены']

    async def send_trial_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        *,
        charged_amount_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='activation',
                user=user,
                subscription=subscription,
                transaction=None,
                amount_kopeks=charged_amount_kopeks,
                message='Trial activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'charged_amount_kopeks': charged_amount_kopeks,
                    'trial_duration_days': (
                        max(1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400))
                        if subscription.end_date and subscription.start_date
                        else settings.TRIAL_DURATION_DAYS
                    ),
                    'traffic_limit_gb': (
                        subscription.traffic_limit_gb
                        if subscription.traffic_limit_gb is not None
                        else settings.TRIAL_TRAFFIC_LIMIT_GB
                    ),
                    'device_limit': subscription.device_limit,
                },
            )

            if not self._is_enabled():
                return False

            tariff = await self._get_tariff(db, subscription)
            # Тариф самого пробного («⏰Пробный») дублировал бы заголовок; показываем только чужой —
            # если пробный выдан на платном тарифе, это владельцу как раз важно.
            tariff_name = (
                html.escape(tariff.name) if tariff and not getattr(tariff, 'is_trial_available', False) else None
            )
            device_limit = subscription.device_limit
            if device_limit is None:
                device_limit = settings.get_disabled_mode_device_limit()
                if device_limit is None:
                    device_limit = settings.TRIAL_DEVICE_LIMIT
            duration_days = settings.TRIAL_DURATION_DAYS
            if subscription.end_date and subscription.start_date:
                duration_days = max(1, round((subscription.end_date - subscription.start_date).total_seconds() / 86400))
            traffic_gb = (
                subscription.traffic_limit_gb
                if subscription.traffic_limit_gb is not None
                else settings.TRIAL_TRAFFIC_LIMIT_GB
            )
            what = f'{format_days_declension(duration_days)}, {format_devices_declension(device_limit)}'
            if traffic_gb:
                what += f', {traffic_gb} ГБ'
            what += f', до {self._owner_until(subscription.end_date)}'

            message = self._owner_card(
                self._owner_title('🎁 Пробный период', charged_amount_kopeks),
                self._owner_who(user, tariff_name, verb='Взял(а) пробный'),
                what,
                '⚠️ Раньше уже платил(а) — пробный выдан повторно' if user.has_had_paid_subscription else None,
                await self._owner_referrer_line(db, user),
            )
            return await self._send_message(message, category=NotificationCategory.TRIALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о триале', error=e)
            return False

    async def _get_tariff(self, db: AsyncSession, subscription: Subscription) -> Tariff | None:
        """Тариф подписки, если он есть; объект нужен карточкам, чтобы объяснить цену."""
        if not subscription.tariff_id:
            return None

        try:
            from app.database.crud.tariff import get_tariff_by_id

            return await get_tariff_by_id(db, subscription.tariff_id)
        except Exception as e:
            logger.warning(
                'Не удалось загрузить тариф для карточки владельцу', tariff_id=subscription.tariff_id, error=e
            )
            return None

    async def _get_tariff_name(self, db: AsyncSession, subscription: Subscription) -> str | None:
        """Получает название тарифа подписки, если он есть."""
        tariff = await self._get_tariff(db, subscription)
        return html.escape(tariff.name) if tariff else None

    # ── Единая форма денежных карточек владельцу (этап УВ-2, 13.09.2026) ──────────
    # Строка 1 — что случилось и сколько денег; строка 2 — кто и тариф; строка 3 — что
    # изменилось, с объяснением неочевидной цифры; дальше — только если есть что сказать;
    # дата без секунд нужна, когда владелец пересылает карточку. Telegram ID, ID транзакции,
    # промогруппа, коды серверов и трафик убраны сознательно: решений по ним владелец не
    # принимает, а всё это есть в карточке клиента в кабинете. Исключение — пробный: его
    # лимит трафика (5 ГБ) — параметр самого пробного, и он остаётся в строке 3.

    @staticmethod
    def _owner_until(moment: datetime | None) -> str:
        """«до 16.09»; год дописывается только когда он не текущий — короче некуда, и не врёт."""
        if moment is None:
            return 'N/A'
        same_year = format_local_datetime(moment, '%Y') == format_local_datetime(datetime.now(UTC), '%Y')
        return format_local_datetime(moment, '%d.%m' if same_year else '%d.%m.%Y')

    def _owner_card(self, title: str, who: str, what: str, *extra: str | None) -> str:
        lines = [f'<b>{title}</b>', who, what, *[line for line in extra if line]]
        lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m %H:%M")}</i>')
        return '\n'.join(lines)

    def _owner_who(self, user: User, tariff_name: str | None = None, *, verb: str | None = None) -> str:
        """«Купил(а) nikitaa @lilgaandelf · Базовый»: действие + имя + @ник. Имя оставляем даже
        однобуквенное (владелец 18.09: «хорошо, что имя есть»); «(а)» — Telegram пол не сообщает."""
        name = html.escape((getattr(user, 'first_name', None) or '').strip())
        username = getattr(user, 'username', None)
        telegram_id = getattr(user, 'telegram_id', None)
        if username:
            who = f'{name} @{html.escape(username)}'.strip()
        elif telegram_id:
            # Без @username по имени не найти (у четверти клиентов его нет, тёзок много) —
            # ID здесь единственная справка, ради которой владелец или менеджер откроет кабинет.
            who = f'{name} · ID {telegram_id}' if name else f'ID {telegram_id}'
        else:
            who = name or self._get_user_display(user)
        if verb:
            who = f'{verb} {who}'
        return f'{who} · {tariff_name}' if tariff_name else who

    @staticmethod
    def _owner_title(title: str, amount_kopeks: int | None, pay_label: str = '') -> str:
        if not amount_kopeks or amount_kopeks <= 0:
            return title
        return f'{title} — {settings.format_price(int(amount_kopeks))} {pay_label}'.rstrip()

    def _owner_pay_label(self, transaction: Transaction | None) -> str:
        """Как заплатили — словами владельца («по СБП»), а не именем провайдера.
        Списание с баланса не называем: деньги на него пришли отдельной карточкой."""
        method = getattr(transaction, 'payment_method', None) if transaction else None
        if not method or method == 'balance':
            return ''
        if method == 'platega':
            description = getattr(transaction, 'description', None) or ''
            for code, info in settings.get_platega_method_definitions().items():
                if f'({info["name"]})' in description and code in _PLATEGA_METHOD_LABELS:
                    return _PLATEGA_METHOD_LABELS[code]
            return f'через {html.escape(settings.get_platega_display_name())}'
        if method == 'manual':
            return 'вручную'
        label = re.sub(r'^[^\w(&]+', '', self._get_payment_method_display(method)).strip()
        # у части имён экранирование уже стоит, у части нет — снимаем и ставим ровно один раз
        return f'через {html.escape(html.unescape(label), quote=False)}'

    @staticmethod
    def _owner_balance_line(balance_kopeks: int | None) -> str | None:
        balance = int(balance_kopeks or 0)
        shown = settings.format_price(balance)
        if balance <= 0 or shown.startswith('0 ₽'):
            return None
        return f'На балансе осталось {shown}'

    async def _owner_referrer_line(self, db: AsyncSession, user: User) -> str | None:
        if not getattr(user, 'referred_by_id', None):
            return None
        info = await self._get_referrer_info(db, user.referred_by_id)
        if info == 'Нет':
            return None
        # «По приглашению», а не «пришёл по ссылке»: реферера может назначить и админ рукой,
        # а форма без рода подходит и клиенту, и пригласившему любого пола.
        return f'По приглашению {re.sub(r" \(ID: \d+\)$", "", info)}'

    @staticmethod
    def _owner_subscription_label(subscription: Subscription | None, tariff: Tariff | None) -> str:
        if subscription is None:
            return 'без подписки'
        end_date = getattr(subscription, 'end_date', None)
        if end_date is not None and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        until = AdminNotificationService._owner_until(end_date)
        expired = end_date is not None and end_date <= datetime.now(UTC)
        name = html.escape(tariff.name) if tariff else None
        status = getattr(subscription, 'status', None)
        if status == 'disabled':
            if subscription.is_trial:
                return 'пробный выключен'
            return f'{name}, выключена' if name else 'подписка выключена'
        if subscription.is_trial:
            return f'пробный истёк {until}' if expired else f'пробный до {until}'
        # Без подгруженного тарифа (пополнение под докупку грузит подписку голой) — «активна», а не
        # «подписка»: строка «Подписка сейчас: подписка, 3 устройства» читалась как обрубок
        if subscription.is_active:
            return f'{name or "активна"} до {until}'
        if status == 'limited' and not expired:
            return f'{name or "активна"} до {until}, трафик исчерпан'
        tail = 'ждёт оплаты' if status == 'pending' else f'истекла {until}'
        return f'{name}, {tail}' if name else f'подписка {tail}'

    @staticmethod
    def _owner_device_price(tariff: Tariff | None) -> int:
        if tariff is not None and tariff.device_price_kopeks is not None:
            return int(tariff.device_price_kopeks)
        return int(settings.PRICE_PER_DEVICE)

    @classmethod
    def _owner_price_breakdown(
        cls, tariff: Tariff | None, period_days: int, device_limit: int | None, total_kopeks: int
    ) -> str | None:
        """«Цена: тариф 149 ₽ + устройства 2 × 50 ₽» — только когда сумма сходится до копейки.
        При скидке, промокоде или старой цене объяснение было бы ложью, и его лучше не показывать."""
        if tariff is None or not device_limit or not total_kopeks or total_kopeks <= 0:
            return None
        prices = tariff.period_prices or {}
        base = int(prices.get(str(period_days)) or prices.get(period_days) or 0)
        extra = max(0, int(device_limit) - int(tariff.device_limit or 0))
        if base <= 0 or extra <= 0:
            return None
        per_device = cls._owner_device_price(tariff)
        months = calculate_months_from_days(period_days)
        if base + extra * per_device * months != int(total_kopeks):
            return None
        devices = f'{extra} × {settings.format_price(per_device)}'
        if months > 1:
            devices += f' × {months} мес.'
        return f'Цена: тариф {settings.format_price(base)} + устройства {devices}'

    @classmethod
    def _owner_devices_addon_line(
        cls, subscription: Subscription, tariff: Tariff | None, old_value: Any, new_value: Any, price_paid: int
    ) -> str:
        """«+1 устройство, стало 3 · 5 ₽ — это 50 ₽/мес за 3 дня до конца подписки (16.09)».
        Цену объясняем только когда она сходится с формулой докупки (цена за месяц × дней до
        конца / 30, как в device_addon_service — срок может быть и больше месяца); иначе
        называем лишь срок, но не выдумываем."""
        try:
            old_limit, new_limit, price_paid = int(old_value), int(new_value), int(price_paid or 0)
        except (TypeError, ValueError):
            return f'{old_value} → {new_value}'
        added = new_limit - old_limit
        if added <= 0:
            return f'Устройств: {old_limit} → {new_limit}'
        head = f'+{format_devices_declension(added)}, стало {new_limit}'
        end_date = getattr(subscription, 'end_date', None)
        if not end_date:
            return head
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=UTC)
        until = cls._owner_until(end_date)
        if price_paid <= 0:
            return f'{head}. Подписка до {until}'
        per_device = cls._owner_device_price(tariff)
        seconds_left = (end_date - datetime.now(UTC)).total_seconds()
        days_left = max(1, math.ceil(seconds_left / 86400))
        if seconds_left > 0 and price_paid == int(per_device * added * days_left / 30):
            monthly = (
                settings.format_price(per_device) if added == 1 else f'{added} × {settings.format_price(per_device)}'
            )
            return (
                f'{head} · {settings.format_price(price_paid)} — это {monthly}/мес за '
                f'{format_days_declension(days_left)} до конца подписки ({until})'
            )
        return f'{head}. Подписка до {until}'

    async def send_subscription_purchase_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction | None,
        period_days: int,
        was_trial_conversion: bool = False,
        amount_kopeks: int | None = None,
        purchase_type: str | None = None,  # 'first_purchase', 'renewal', 'tariff_switch', None (auto-detect)
        payment_label: str | None = None,  # К-2: способ оплаты словами от вызывающего («по СБП»), не из описания
        discount_kopeks: int = 0,  # К-2: скидка из замороженной разбивки заказа
    ) -> bool:
        try:
            total_amount = (
                amount_kopeks if amount_kopeks is not None else (abs(transaction.amount_kopeks) if transaction else 0)
            )

            await self._record_subscription_event(
                db,
                event_type='purchase',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=total_amount,
                message='Subscription purchase',
                occurred_at=(transaction.completed_at or transaction.created_at) if transaction else datetime.now(UTC),
                extra={
                    'period_days': period_days,
                    'was_trial_conversion': was_trial_conversion,
                    'payment_method': self._get_payment_method_display(transaction.payment_method)
                    if transaction
                    else 'Баланс',
                },
            )

            if not self._is_enabled():
                return False

            is_renewal = purchase_type == 'renewal' or (
                not was_trial_conversion and purchase_type is None and user.has_had_paid_subscription
            )
            if purchase_type == 'tariff_switch':
                title, verb = '🔄 Смена тарифа', 'Сменил(а) тариф'
            elif was_trial_conversion:
                title, verb = '💎 Покупка после пробного', 'Купил(а)'
            elif is_renewal:
                title, verb = '⏰ Продление', 'Продлил(а)'
            else:
                title, verb = '💎 Первая покупка', 'Купил(а)'

            tariff = await self._get_tariff(db, subscription)
            what = (
                f'{"+" if is_renewal else ""}{format_days_declension(period_days)}, до {self._owner_until(subscription.end_date)}'
                f' · {format_devices_declension(subscription.device_limit or 0)}'
            )
            pay_label = payment_label if payment_label is not None else self._owner_pay_label(transaction)
            breakdown = self._owner_price_breakdown(tariff, period_days, subscription.device_limit, total_amount)
            discount_line = None
            if breakdown is None and int(discount_kopeks or 0) > 0:
                discount_line = f'Со скидкой {settings.format_price(int(discount_kopeks))}'
            message = self._owner_card(
                self._owner_title(title, total_amount, pay_label),
                self._owner_who(user, html.escape(tariff.name) if tariff else None, verb=verb),
                what,
                breakdown or discount_line,
                self._owner_balance_line(user.balance_kopeks),
                await self._owner_referrer_line(db, user) if was_trial_conversion or not is_renewal else None,
            )

            # Маршрутизация по категориям (зеркалит логику заголовков выше)
            cat = NotificationCategory.RENEWALS if is_renewal else NotificationCategory.PURCHASES
            return await self._send_message(message, category=cat)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о покупке', error=e)
            return False

    async def send_version_update_notification(self, current_version: str, latest_version, total_updates: int) -> bool:
        """Отправляет уведомление о новых обновлениях."""
        if not self._is_enabled():
            return False

        try:
            from app.utils.markdown_to_telegram import github_markdown_to_telegram_html, truncate_for_blockquote

            repo = getattr(settings, 'VERSION_CHECK_REPO', 'fr1ngg/remnawave-bedolaga-telegram-bot')
            release_url = f'https://github.com/{repo}/releases/tag/{latest_version.tag_name}'
            repo_url = f'https://github.com/{repo}'
            timestamp = format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')

            if latest_version.prerelease:
                header = '🧪 <b>Pre-release</b>'
            elif latest_version.is_dev:
                header = '🔧 <b>Dev build</b>'
            else:
                header = '🆕 <b>Доступно обновление</b>'

            # -- message prefix (everything before blockquote) --
            prefix_lines = [
                header,
                '',
                f'<code>{current_version}</code>  →  <b><a href="{release_url}">{latest_version.tag_name}</a></b>',
                f'📅 {latest_version.formatted_date}',
                '',
            ]
            message_prefix = '\n'.join(prefix_lines)

            # -- message suffix (everything after blockquote) --
            suffix_lines = ['']
            if total_updates > 1:
                suffix_lines.append(f'Доступно обновлений: <b>{total_updates}</b>')
            suffix_lines.extend(
                [
                    f'<a href="{repo_url}">Репозиторий</a>',
                    '',
                    f'<i>{timestamp}</i>',
                ]
            )
            message_suffix = '\n'.join(suffix_lines)

            # -- description in blockquote --
            raw_description = getattr(latest_version, 'full_description', '') or latest_version.short_description
            description_html = github_markdown_to_telegram_html(raw_description)

            if description_html:
                description_html = truncate_for_blockquote(
                    description_html,
                    message_prefix=message_prefix,
                    message_suffix=message_suffix,
                )
                message = f'{message_prefix}<blockquote expandable>{description_html}</blockquote>{message_suffix}'
            else:
                message = f'{message_prefix}{message_suffix}'

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об обновлении', error=e)
            return False

    async def send_version_check_error_notification(self, error_message: str, current_version: str) -> bool:
        if not self._is_enabled():
            return False

        try:
            message = f"""⚠️ <b>ОШИБКА ПРОВЕРКИ ОБНОВЛЕНИЙ</b>

    📦 <b>Текущая версия:</b> <code>{current_version}</code>
    ❌ <b>Ошибка:</b> {error_message}

    🔄 Следующая попытка через час.
    ⚙️ Проверьте доступность GitHub API и настройки сети.

    ⚙️ <i>Система автоматических обновлений • {format_local_datetime(datetime.now(UTC), '%d.%m.%Y %H:%M:%S')}</i>"""

            return await self._send_message(message, category=NotificationCategory.ERRORS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об ошибке проверки версий', error=e)
            return False

    def _build_balance_topup_message(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        cart_hint: OwnerCartHint | None = None,
    ) -> str:
        # Тариф берём только уже загруженным: ленивая подгрузка здесь падала (УВ-1), и
        # ради названия тарифа ходить в базу из этого сборщика нельзя.
        tariff = subscription.__dict__.get('tariff') if subscription is not None else None
        # «Первое» — только у того, кто раньше не платил вообще (владелец 18.09): клиент,
        # заплативший картой напрямую, для бота «пополняет впервые», а для владельца — нет.
        # И только у ПЕРВОГО пополнения: второе пополнение так и не купившего — уже не первое.
        is_first = (
            not bool(getattr(user, 'has_had_paid_subscription', False)) and 'перво' in (topup_status or '').lower()
        )
        amount = int(transaction.amount_kopeks or 0)
        what = (
            f'На балансе было {settings.format_price(old_balance)}, стало {settings.format_price(user.balance_kopeks)}'
        )
        bonus = int(user.balance_kopeks or 0) - int(old_balance or 0) - amount
        if bonus > 0:
            # Баланс вырос больше, чем пришло денег: бонус новичку за первое пополнение
            what += f' (в т.ч. бонус {settings.format_price(bonus)})'
        # Что будет с деньгами, бот знает по корзине И по тем же трём условиям, по которым
        # автопокупка решит списывать (`_owner_cart_hint`): обещать карточку, которой не будет,
        # нельзя — владелец её ждал бы. Корзины нет — деньги просто остались на балансе.
        if cart_hint and cart_hint.by_client:
            what += f'. Дальше — {cart_hint.text}, когда клиент подтвердит в кабинете'
        elif cart_hint and cart_hint.auto:
            what += f'. Дальше — {cart_hint.text}, карточка придёт следом'
        elif cart_hint:
            what += f'. В корзине — {cart_hint.text}, бот сам не спишет'
        else:
            what += '. Корзины нет — деньги остались на балансе'
        referrer_line = None
        if is_first and referrer_info and referrer_info != 'Нет':
            referrer_line = f'По приглашению {re.sub(r" \(ID: \d+\)$", "", referrer_info)}'
        comment_line = None
        description = (getattr(transaction, 'description', None) or '').strip()
        if getattr(transaction, 'payment_method', None) == 'manual' and description:
            # У ручного начисления описание — это комментарий админа, единственное «за что»
            comment_line = f'Комментарий: {html.escape(description[:120] + ("…" if len(description) > 120 else ""))}'
        return self._owner_card(
            self._owner_title(
                '💰 Первое пополнение' if is_first else '💰 Пополнение', amount, self._owner_pay_label(transaction)
            ),
            self._owner_who(user, verb='Пополнил(а)'),
            what,
            (
                f'Подписка сейчас: {self._owner_subscription_state(subscription, tariff)}'
                if subscription is not None
                else 'Подписки сейчас нет'
            ),
            comment_line,
            referrer_line,
        )

    @staticmethod
    def _owner_subscription_state(subscription: Subscription | None, tariff: Tariff | None) -> str:
        """«Базовый, 5 устройств, до 25.09» — отдельной строкой у пополнения: во второй строке
        «Базовый до 25.09» читалось как «купил до 25.09» (живая карточка 18.09, клиент 291)."""
        if subscription is None:
            return 'нет'
        label = AdminNotificationService._owner_subscription_label(subscription, tariff)
        devices = getattr(subscription, 'device_limit', None)
        if not devices or ' до ' not in label:
            return label
        # с конца: имя тарифа само может содержать « до » («Тариф до 3 устройств»)
        head, _, until = label.rpartition(' до ')
        return f'{head}, {format_devices_declension(int(devices))}, до {until}'

    async def _owner_cart_hint(self, user: User) -> OwnerCartHint | None:
        """Сохранённая корзина («продление подписки на 30 дней (Базовый)») и спишет ли бот сам.
        `auto` повторяет три условия автопокупки после пополнения (`auto_purchase_saved_cart_after_topup`):
        выключатель, свежая метка намерения (живёт короче корзины) и что денег теперь хватает.
        Только чтение; любой сбой хранилища — молчание, карточка важнее подсказки."""
        user_id = int(getattr(user, 'id', 0) or 0)
        try:
            from app.services.user_cart_service import user_cart_service

            cart = await user_cart_service.get_user_cart(user_id)
            if not cart:
                return None
            auto = (
                settings.is_auto_purchase_after_topup_enabled()
                and await user_cart_service.has_topup_intent(user_id)
                and int(user.balance_kopeks or 0) >= int(cart.get('total_price') or 0) > 0
            )
        except Exception as error:
            logger.warning('Не удалось прочитать корзину для карточки пополнения', user_id=user_id, error=error)
            return None
        description = str(cart.get('description') or '').strip()
        text = html.escape(description[:1].lower() + description[1:]) if description else 'покупка из корзины'
        return OwnerCartHint(text=text, auto=auto)

    async def _reload_topup_notification_entities(
        self,
        db: AsyncSession,
        user: User,
        transaction: Transaction,
    ) -> tuple[User, Transaction, Subscription | None, PromoGroup | None]:
        refreshed_user = await get_user_by_id(db, user.id)
        if not refreshed_user:
            raise ValueError(f'Не удалось повторно загрузить пользователя {user.id} для уведомления о пополнении')

        refreshed_transaction = await get_transaction_by_id(db, transaction.id)
        if not refreshed_transaction:
            raise ValueError(f'Не удалось повторно загрузить транзакцию {transaction.id} для уведомления о пополнении')

        subscription = getattr(refreshed_user, 'subscription', None)
        promo_group = await self._get_user_promo_group(db, refreshed_user)

        return refreshed_user, refreshed_transaction, subscription, promo_group

    def _is_lazy_loading_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return (
            isinstance(error, MissingGreenlet)
            or 'greenlet_spawn' in message
            or 'await_only' in message
            or 'missinggreenlet' in message
        )

    async def send_balance_topup_notification(
        self,
        user: User,
        transaction: Transaction,
        old_balance: int,
        *,
        topup_status: str,
        referrer_info: str,
        subscription: Subscription | None,
        promo_group: PromoGroup | None,
        db: AsyncSession | None = None,
        next_step: str | None = None,
    ) -> bool:
        """`next_step` — за что деньги на пути, который корзину не смотрит (пополнение под докупку
        устройств): подсказка по корзине там была бы ложью в обе стороны. Само списание там ждёт
        нажатия клиента в кабинете (`POST /devices/intents/{id}/purchase`), сервер сам не спишет."""
        logger.info('Начинаем отправку уведомления о пополнении баланса')

        if db:
            try:
                await self._record_subscription_event(
                    db,
                    event_type='balance_topup',
                    user=user,
                    subscription=subscription,
                    transaction=transaction,
                    amount_kopeks=transaction.amount_kopeks,
                    message='Balance top-up',
                    occurred_at=transaction.completed_at or transaction.created_at,
                    extra={
                        'status': topup_status,
                        'balance_before': old_balance,
                        'balance_after': user.balance_kopeks,
                        'referrer_info': referrer_info,
                        'promo_group_id': getattr(promo_group, 'id', None),
                        'promo_group_name': getattr(promo_group, 'name', None),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие пополнения баланса пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        if not self._is_enabled():
            return False

        cart_hint = OwnerCartHint(next_step, False, True) if next_step else await self._owner_cart_hint(user)
        try:
            logger.info('Пытаемся создать сообщение уведомления')
            message = self._build_balance_topup_message(
                user,
                transaction,
                old_balance,
                topup_status=topup_status,
                referrer_info=referrer_info,
                subscription=subscription,
                promo_group=promo_group,
                cart_hint=cart_hint,
            )
            logger.info('Сообщение уведомления создано успешно')
        except Exception as error:
            logger.info(
                'Перехвачена ошибка при создании сообщения уведомления', __name__=type(error).__name__, error=error
            )
            if not self._is_lazy_loading_error(error):
                logger.error('Ошибка подготовки уведомления о пополнении', error=error, exc_info=True)
                return False

            if db is None:
                logger.error(
                    'Недостаточно данных для уведомления о пополнении и отсутствует доступ к БД',
                    error=error,
                    exc_info=True,
                )
                return False

            logger.warning(
                'Повторная загрузка данных для уведомления о пополнении после ошибки ленивой загрузки', error=error
            )

            try:
                logger.info('Пытаемся перезагрузить данные для уведомления')
                (
                    user,
                    transaction,
                    subscription,
                    promo_group,
                ) = await self._reload_topup_notification_entities(db, user, transaction)
                logger.info('Данные успешно перезагружены')
            except Exception as reload_error:
                logger.error(
                    'Ошибка повторной загрузки данных для уведомления о пополнении',
                    reload_error=reload_error,
                    exc_info=True,
                )
                return False

            try:
                logger.info('Пытаемся создать сообщение после перезагрузки данных')
                message = self._build_balance_topup_message(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    cart_hint=cart_hint,
                )
                logger.info('Сообщение успешно создано после перезагрузки данных')
            except Exception as rebuild_error:
                logger.error(
                    'Ошибка повторной подготовки уведомления о пополнении после повторной загрузки',
                    rebuild_error=rebuild_error,
                    exc_info=True,
                )
                return False

        try:
            return await self._send_message(message, category=NotificationCategory.BALANCE)
        except Exception as e:
            logger.error('Ошибка отправки уведомления о пополнении', error=e, exc_info=True)
            return False

    async def send_subscription_extension_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        transaction: Transaction,
        extended_days: int,
        old_end_date: datetime,
        *,
        new_end_date: datetime | None = None,
        balance_after: int | None = None,
    ) -> bool:
        try:
            current_end_date = new_end_date or subscription.end_date
            current_balance = balance_after if balance_after is not None else user.balance_kopeks

            await self._record_subscription_event(
                db,
                event_type='renewal',
                user=user,
                subscription=subscription,
                transaction=transaction,
                amount_kopeks=abs(transaction.amount_kopeks),
                message='Subscription renewed',
                occurred_at=transaction.completed_at or transaction.created_at,
                extra={
                    'extended_days': extended_days,
                    'previous_end_date': old_end_date.isoformat(),
                    'new_end_date': current_end_date.isoformat(),
                    'payment_method': transaction.payment_method,
                    'balance_after': current_balance,
                },
            )

            if not self._is_enabled():
                return False

            tariff = await self._get_tariff(db, subscription)
            amount = abs(transaction.amount_kopeks)
            what = (
                f'+{format_days_declension(extended_days)}, до {self._owner_until(current_end_date)}'
                f' · {format_devices_declension(subscription.device_limit or 0)}'
            )
            message = self._owner_card(
                self._owner_title('⏰ Продление', amount, self._owner_pay_label(transaction)),
                self._owner_who(user, html.escape(tariff.name) if tariff else None, verb='Продлил(а)'),
                what,
                self._owner_price_breakdown(tariff, extended_days, subscription.device_limit, amount),
                self._owner_balance_line(current_balance),
            )
            return await self._send_message(message, category=NotificationCategory.RENEWALS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о продлении', error=e)
            return False

    async def send_promocode_activation_notification(
        self,
        db: AsyncSession,
        user: User,
        promocode_data: dict[str, Any],
        effect_description: str,
        balance_before_kopeks: int | None = None,
        balance_after_kopeks: int | None = None,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promocode_activation',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=promocode_data.get('balance_bonus_kopeks'),
                message='Promocode activation',
                occurred_at=datetime.now(UTC),
                extra={
                    'code': promocode_data.get('code'),
                    'type': promocode_data.get('type'),
                    'subscription_days': promocode_data.get('subscription_days'),
                    'balance_bonus_kopeks': promocode_data.get('balance_bonus_kopeks'),
                    'description': effect_description,
                    'valid_until': (
                        promocode_data.get('valid_until').isoformat()
                        if isinstance(promocode_data.get('valid_until'), datetime)
                        else promocode_data.get('valid_until')
                    ),
                    'balance_before_kopeks': balance_before_kopeks,
                    'balance_after_kopeks': balance_after_kopeks,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие активации промокода пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            promo_group = await self._get_user_promo_group(db, user)
            promo_block = self._format_promo_group_block(promo_group)
            type_display = self._get_promocode_type_display(promocode_data.get('type'))
            usage_info = f'{promocode_data.get("current_uses", 0)}/{promocode_data.get("max_uses", 0)}'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🎫 <b>АКТИВАЦИЯ ПРОМОКОДА</b>',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> @{html.escape(getattr(user, "username", None) or "отсутствует")}',
                '',
                promo_block,
                '',
                '🎟️ <b>Промокод:</b>',
                f'🔖 Код: <code>{promocode_data.get("code")}</code>',
                f'🧾 Тип: {type_display}',
                f'📊 Использования: {usage_info}',
            ]

            promo_type = promocode_data.get('type')
            balance_bonus = promocode_data.get('balance_bonus_kopeks', 0)
            subscription_days = promocode_data.get('subscription_days', 0)

            if promo_type == PromoCodeType.DISCOUNT.value:
                message_lines.append(f'💸 Скидка: {balance_bonus}%')
                if subscription_days:
                    message_lines.append(f'⏳ Срок действия скидки: {subscription_days} ч.')
                else:
                    message_lines.append('⏳ Срок действия скидки: до первой покупки')
            else:
                if balance_bonus:
                    message_lines.append(f'💰 Бонус на баланс: {settings.format_price(balance_bonus)}')
                if subscription_days:
                    message_lines.append(f'📅 Доп. дни подписки: {subscription_days}')

            valid_until = promocode_data.get('valid_until')
            if valid_until:
                message_lines.append(
                    f'⏳ Действует до: {format_local_datetime(valid_until, "%d.%m.%Y %H:%M")}'
                    if isinstance(valid_until, datetime)
                    else f'⏳ Действует до: {valid_until}'
                )

            message_lines.extend(
                [
                    '',
                    '💼 <b>Баланс:</b>',
                    (
                        f'{settings.format_price(balance_before_kopeks)} → {settings.format_price(balance_after_kopeks)}'
                        if balance_before_kopeks is not None and balance_after_kopeks is not None
                        else 'ℹ️ Баланс не изменился'
                    ),
                    '',
                    '📝 <b>Эффект:</b>',
                    effect_description.strip() or '✅ Промокод активирован',
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об активации промокода', error=e)
            return False

    async def send_campaign_link_visit_notification(
        self,
        db: AsyncSession,
        telegram_user: types.User,
        campaign: AdvertisingCampaign,
        user: User | None = None,
    ) -> bool:
        # Дедуп: если юзер уже зарегистрирован в этой кампании
        # (AdvertisingCampaignRegistration.UniqueConstraint(campaign_id, user_id))
        # — повторный /start не должен слать новое уведомление в админ-чат, иначе
        # кол-во сообщений в чате превышает реальное число регистраций в БД и
        # вводит админа в заблуждение. Для новых юзеров (user is None) уведомление
        # уходит как раньше — это первичный переход.
        if user:
            existing_registration = await db.execute(
                select(AdvertisingCampaignRegistration.id).where(
                    AdvertisingCampaignRegistration.campaign_id == campaign.id,
                    AdvertisingCampaignRegistration.user_id == user.id,
                )
            )
            if existing_registration.scalar_one_or_none() is not None:
                logger.debug(
                    'Skip campaign visit notification: user already registered in campaign',
                    user_id=user.id,
                    campaign_id=campaign.id,
                )
                return False

            try:
                await self._record_subscription_event(
                    db,
                    event_type='referral_link_visit',
                    user=user,
                    subscription=None,
                    transaction=None,
                    amount_kopeks=None,
                    message='Referral link visit',
                    occurred_at=datetime.now(UTC),
                    extra={
                        'campaign_id': campaign.id,
                        'campaign_name': campaign.name,
                        'start_parameter': campaign.start_parameter,
                        'was_registered': bool(user),
                    },
                )
            except Exception:
                logger.error(
                    'Не удалось сохранить событие перехода по кампании для пользователя',
                    getattr=getattr(user, 'id', 'unknown'),
                    exc_info=True,
                )

        # 🔴 УВ-3б п.1 (решение владельца 14.09, подтверждено 19.09.2026): «📣 ПЕРЕХОД ПО РК» в
        # Telegram больше не уходит — ни владельцу, ни менеджеру. Для нового человека он летел
        # за секунду до «✅ РЕГИСТРАЦИЯ ПО РК» тем же текстом (чистый дубль), а без регистрации
        # следом означал «открыл бота и ушёл» — сделать с этим нечего. Запись события выше и
        # счётчик переходов в карточке кампании остаются: воронка считается как раньше.
        # Возвращаем False, чтобы вызывающие не ставили пометку «отправлено».
        return False

    async def send_campaign_registration_notification(
        self,
        db: AsyncSession,
        telegram_user_id: int,
        telegram_user_name: str,
        telegram_username: str | None,
        campaign: AdvertisingCampaign,
        user: User,
        *,
        bonus_type: str,
        balance_kopeks: int = 0,
        subscription_days: int | None = None,
        subscription_traffic_gb: int | None = None,
        subscription_device_limit: int | None = None,
        tariff_name: str | None = None,
    ) -> bool:
        """Уведомление о СОВЕРШЁННОЙ регистрации по рекламной кампании.

        Шлётся ровно один раз на каждую новую запись в advertising_campaign_registrations
        (caller передаёт is_new_registration=True). Это даёт паритет: число сообщений
        в админ-чате равно числу регистраций в кабинете.
        """
        if not self._is_enabled():
            return False

        try:
            await self._record_subscription_event(
                db,
                event_type='campaign_registration',
                user=user,
                subscription=None,
                transaction=None,
                amount_kopeks=balance_kopeks or None,
                message='Campaign registration completed',
                occurred_at=datetime.now(UTC),
                extra={
                    'campaign_id': campaign.id,
                    'campaign_name': campaign.name,
                    'start_parameter': campaign.start_parameter,
                    'bonus_type': bonus_type,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие регистрации по кампании',
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )

        try:
            message_lines = [
                '✅ <b>РЕГИСТРАЦИЯ ПО РК</b>',
                '',
                f'🧾 {html.escape(campaign.name)} (<code>{html.escape(campaign.start_parameter)}</code>)',
                '',
                f'👤 {html.escape(telegram_user_name)} (<code>{telegram_user_id}</code>)',
            ]
            if telegram_username:
                message_lines.append(f'📱 @{html.escape(telegram_username)}')

            promo_group = await self._get_user_promo_group(db, user)
            if promo_group:
                message_lines.append(f'🏷️ Промогруппа: {html.escape(promo_group.name)}')

            message_lines.append('')

            bonus_lines = self._format_campaign_bonus(campaign, tariff_name=tariff_name)
            message_lines.extend(bonus_lines)

            message_lines.extend(
                [
                    '',
                    f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message(
                '\n'.join(message_lines),
                category=NotificationCategory.PROMO,
                manager_topic=ManagerAlertTopic.MARKETING,
            )

        except Exception as e:
            logger.error(
                'Ошибка отправки уведомления о регистрации по кампании',
                error=str(e),
                user_id=user.id,
                campaign_id=campaign.id,
                exc_info=True,
            )
            return False

    async def send_user_promo_group_change_notification(
        self,
        db: AsyncSession,
        user: User,
        old_group: PromoGroup | None,
        new_group: PromoGroup,
        *,
        reason: str | None = None,
        initiator: User | None = None,
        automatic: bool = False,
    ) -> bool:
        try:
            await self._record_subscription_event(
                db,
                event_type='promo_group_change',
                user=user,
                subscription=None,
                transaction=None,
                message='Promo group change',
                occurred_at=datetime.now(UTC),
                extra={
                    'old_group_id': getattr(old_group, 'id', None),
                    'old_group_name': getattr(old_group, 'name', None),
                    'new_group_id': new_group.id,
                    'new_group_name': new_group.name,
                    'reason': reason,
                    'initiator_id': getattr(initiator, 'id', None),
                    'initiator_telegram_id': getattr(initiator, 'telegram_id', None),
                    'automatic': automatic,
                },
            )
        except Exception:
            logger.error(
                'Не удалось сохранить событие смены промогруппы пользователя',
                getattr=getattr(user, 'id', 'unknown'),
                exc_info=True,
            )

        if not self._is_enabled():
            return False

        try:
            title = '🤖 АВТОМАТИЧЕСКАЯ СМЕНА ПРОМОГРУППЫ' if automatic else '👥 СМЕНА ПРОМОГРУППЫ'
            initiator_line = None
            if initiator:
                initiator_line = (
                    f'👮 <b>Инициатор:</b> {html.escape(initiator.full_name)} (ID: {initiator.telegram_id})'
                )
            elif automatic:
                initiator_line = '🤖 Автоматическое назначение'
            user_display = self._get_user_display(user)
            user_id_label = self._get_user_identifier_label(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                f'{title}',
                '',
                f'👤 <b>Пользователь:</b> {user_display}',
                f'🆔 <b>{user_id_label}:</b> {user_id_display}',
                f'📱 <b>Username:</b> @{html.escape(getattr(user, "username", None) or "отсутствует")}',
                '',
                self._format_promo_group_block(new_group, title='Новая промогруппа', icon='🏆'),
            ]

            if old_group and old_group.id != new_group.id:
                message_lines.extend(
                    [
                        '',
                        self._format_promo_group_block(old_group, title='Предыдущая промогруппа', icon='♻️'),
                    ]
                )

            if initiator_line:
                message_lines.extend(['', initiator_line])

            if reason:
                message_lines.extend(['', f'📝 Причина: {reason}'])

            message_lines.extend(
                [
                    '',
                    f'💰 Баланс пользователя: {settings.format_price(user.balance_kopeks)}',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PROMO)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о смене промогруппы', error=e)
            return False

    def _resolve_topic_id(self, category: NotificationCategory | None = None) -> int | None:
        """Определяет topic_id для сообщения.

        Если указана category и для неё настроен топик — возвращает его.
        Иначе — fallback на self.topic_id (общий топик).
        """
        if category:
            topic = self.category_topics.get(category)
            if topic is not None:
                return topic
        return self.topic_id

    def resolve_recipient_role(self) -> str:
        """Определяет роль получателя уведомления по chat_id.

        В личном чате chat_id совпадает с telegram_id получателя, что позволяет
        проверить права до отправки без I/O (данные читаются из памяти).

        Returns:
            'admin'     — полный набор кнопок;
            'moderator' — набор без «👤 К пользователю» (@admin_required);
            'group'     — групповой/супергруппа/канал админ-чат: только надёжные
                          (не-FSM) кнопки, т.к. конкретного получателя не определить
                          и FSM-ввод в общем чате не работает (privacy mode бота);
            'none'      — без кнопок (посторонний в личке, либо
                          ADMIN_NOTIFICATIONS_CHAT_ID задан строкой @username).
        """
        try:
            chat_id = int(self.chat_id)
        except (TypeError, ValueError):
            return 'none'  # строка @username или None — тип чата не определить
        if chat_id < 0:
            # супергруппа / канал / старая группа — доверенный админ-чат оператора,
            # но конкретного получателя не определить → только надёжные кнопки.
            return 'group'
        if chat_id == 0:
            return 'none'  # невалидный chat_id
        if settings.is_admin(chat_id):
            return 'admin'

        from app.services.support_settings_service import SupportSettingsService

        if SupportSettingsService.is_moderator(chat_id):
            return 'moderator'

        return 'none'  # личка постороннего — не показываем кнопки

    async def _send_message(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
        manager_topic: ManagerAlertTopic | None = None,
    ) -> bool:
        if not self.chat_id:
            logger.warning('ADMIN_NOTIFICATIONS_CHAT_ID не настроен')
            return False

        # Per-category suppression
        if category and not self.category_enabled.get(category, True):
            logger.debug('Уведомление подавлено (категория отключена)', category=category.value)
            return False

        message_kwargs: dict[str, Any] = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        thread_id = self._resolve_topic_id(category)
        if thread_id:
            message_kwargs['message_thread_id'] = thread_id
        if reply_markup is not None:
            message_kwargs['reply_markup'] = reply_markup

        # ВАЖНО: вся ветка ошибок ниже логируется через logger.warning, а не
        # logger.error. Иначе TelegramNotifierProcessor попытается переслать
        # ошибку в этот же админ-чат, упрётся в тот же flood control — петля
        # усиления (баг с node.connection_lost/restored, 7-8 webhook'ов подряд).
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                await self.bot.send_message(**message_kwargs)
                logger.info('Уведомление отправлено в чат', chat_id=self.chat_id, category=category)
                # The manager copy has a strict allow-list and intentionally
                # drops inline keyboards: a group member must not receive admin
                # actions just because they can see the alert.
                #
                # `manager_topic` is the narrow route: one notification opts in
                # by name when its whole admin category must stay admin-only.
                if manager_topic is not None:
                    await manager_alert_service.send(self.bot, manager_topic, text)
                else:
                    await manager_alert_service.mirror_admin_category(
                        self.bot,
                        category.value if category else None,
                        text,
                    )
                return True

            except TelegramForbiddenError:
                logger.warning('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
                return False

            except TelegramBadRequest as e:
                logger.warning(
                    'Ошибка отправки уведомления в админ-чат',
                    error=_redact_telegram_secrets(str(e))[:200],
                )
                return False

            except TelegramRetryAfter as e:
                # Flood control: ждём столько, сколько сказал Telegram (cap 30s),
                # потом ретраим. До фикса исключение проваливалось в bare
                # except → logger.error → петля через TelegramNotifierProcessor.
                requested_retry_after = max(1, int(getattr(e, 'retry_after', 1)))
                retry_after = min(requested_retry_after, 30)
                log_kwargs: dict[str, Any] = {
                    'chat_id': self.chat_id,
                    'retry_after': retry_after,
                    'attempt': attempt,
                }
                if requested_retry_after > retry_after:
                    # Telegram реально просит дольше cap'а — видимый сигнал,
                    # что бот аккаунт перегружен сильнее обычного flood-control'а.
                    log_kwargs['retry_after_requested'] = requested_retry_after
                    log_kwargs['clamped'] = True
                logger.warning('Telegram flood control при отправке в админ-чат', **log_kwargs)
                if attempt < max_attempts:
                    await asyncio.sleep(retry_after)
                    continue
                return False

            except (TelegramNetworkError, TelegramServerError) as e:
                # Транзиентные сетевые/5xx — warning, не error.
                logger.warning(
                    'Транзиентная сетевая ошибка отправки в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                return False

            except Exception as e:
                logger.warning(
                    'Неожиданная ошибка при отправке в админ-чат',
                    chat_id=self.chat_id,
                    error=_redact_telegram_secrets(str(e))[:200],
                    error_type=type(e).__name__,
                )
                return False

        return False

    def _is_enabled(self) -> bool:
        return self.enabled and bool(self.chat_id)

    @property
    def is_enabled(self) -> bool:
        """Public check for whether admin notifications are configured and active."""
        return self._is_enabled()

    async def send_admin_notification(
        self,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
        *,
        category: NotificationCategory | None = None,
    ) -> bool:
        """Send a generic notification to admin chat with optional inline keyboard."""
        if not self._is_enabled():
            return False
        return await self._send_message(text, reply_markup=reply_markup, category=category)

    async def send_guest_purchase_notification(
        self,
        purchase: GuestPurchase,
        tariff_name: str,
        *,
        is_pending_activation: bool = False,
    ) -> bool:
        """Send admin notification for a guest/gift purchase (landing or cabinet)."""
        if not self._is_enabled():
            return False

        try:
            is_cabinet = purchase.source == 'cabinet'

            # Event title
            if is_cabinet and purchase.is_gift:
                event_title = '🎁 ПОДАРОК ИЗ КАБИНЕТА'
            elif is_pending_activation:
                event_title = '⏳ ПОКУПКА С ЛЕНДИНГА (ожидает активации)'
            elif purchase.is_gift:
                event_title = '🎁 ПОКУПКА В ПОДАРОК С ЛЕНДИНГА'
            else:
                event_title = '🛒 ПОКУПКА С ЛЕНДИНГА'

            # Contact info
            contact_display = html.escape(purchase.contact_value or '—')
            contact_icon = '📧' if purchase.contact_type == 'email' else '📱'

            payment_method = self._get_payment_method_display(purchase.payment_method)

            message_lines = [
                f'<b>{event_title}</b>',
                '',
            ]

            if is_cabinet:
                # Cabinet gift: show buyer with link to user profile
                buyer = getattr(purchase, 'buyer', None)
                if buyer:
                    buyer_name = f'@{buyer.username}' if buyer.username else buyer.email or f'id:{buyer.id}'
                    message_lines.append(f'👤 Покупатель: <code>{html.escape(buyer_name)}</code>')
                else:
                    message_lines.append(f'{contact_icon} Покупатель: <code>{contact_display}</code>')
            else:
                # Landing: show page slug and buyer contact
                landing_slug = '—'
                try:
                    landing = purchase.landing
                    if landing:
                        landing_slug = landing.slug
                    elif purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                except Exception:
                    if purchase.landing_id:
                        landing_slug = f'ID:{purchase.landing_id}'
                message_lines.append(f'🌐 Страница: <b>/buy/{html.escape(landing_slug)}</b>')
                message_lines.append(f'{contact_icon} Покупатель: <code>{contact_display}</code>')

            if purchase.is_gift:
                if purchase.gift_recipient_value:
                    recipient_icon = '📧' if purchase.gift_recipient_type == 'email' else '📱'
                    recipient_value = html.escape(purchase.gift_recipient_value)
                    message_lines.append(f'{recipient_icon} Получатель: <code>{recipient_value}</code>')
                else:
                    message_lines.append('🔗 Получатель: <i>по коду активации</i>')
                if purchase.gift_message:
                    raw_msg = purchase.gift_message[:100]
                    suffix = '…' if len(purchase.gift_message) > 100 else ''
                    message_lines.append(f'💬 <i>{html.escape(raw_msg)}{suffix}</i>')

            # Payment details in blockquote
            payment_lines = [
                '<blockquote>',
                f'🏷️ Тариф: <b>{html.escape(tariff_name)}</b>',
                f'📅 Период: {purchase.period_days} дн.',
                f'💵 <b>{settings.format_price(purchase.amount_kopeks)}</b> • {payment_method}',
            ]

            if purchase.payment_id:
                payment_lines.append(f'🆔 {html.escape(str(purchase.payment_id))}')

            payment_lines.append('</blockquote>')
            message_lines.extend(payment_lines)

            message_lines.append(f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PURCHASES)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о гостевой покупке', error=e)
            return False

    async def send_webhook_notification(self, text: str) -> bool:
        """Send a generic webhook/infrastructure notification to admin chat.

        Used by RemnaWaveWebhookService for node, service, and CRM events.
        The caller is responsible for HTML-escaping all untrusted data in `text`.
        """
        if not self._is_enabled():
            return False
        return await self._send_message(text, category=NotificationCategory.INFRASTRUCTURE)

    def _get_payment_method_display(self, payment_method: str | None) -> str:
        if not payment_method:
            return '💰 С баланса'

        method_names: dict[str, str] = {
            'telegram_stars': '⭐ Telegram Stars',
            'yookassa': '💳 YooKassa (карта)',
            'tribute': '💎 Tribute (карта)',
            'mulenpay': f'💳 {settings.get_mulenpay_display_name()} (карта)',
            'pal24': f'🏦 {settings.get_pal24_display_name()} (СБП)',
            'cryptobot': f'🪙 {settings.get_cryptobot_display_name()} (крипто)',
            'heleket': f'🪙 {settings.get_heleket_display_name()} (крипто)',
            'wata': f'💳 {settings.get_wata_display_name()}',
            'platega': f'💳 {settings.get_platega_display_name()}',
            'cloudpayments': f'💳 {settings.get_cloudpayments_display_name()}',
            'freekassa': f'💳 {settings.get_freekassa_display_name()}',
            'kassa_ai': f'💳 {settings.get_kassa_ai_display_name()}',
            'manual': '🛠️ Вручную (админ)',
            'balance': '💰 С баланса',
        }

        return method_names.get(payment_method, f'💳 {html.escape(payment_method)}')

    async def send_maintenance_status_notification(
        self, event_type: str, status: str, details: dict[str, Any] = None
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            if event_type == 'enable':
                if details.get('auto_enabled', False):
                    icon = '⚠️'
                    title = 'АВТОМАТИЧЕСКОЕ ВКЛЮЧЕНИЕ ТЕХРАБОТ'
                else:
                    icon = '🔧'
                    title = 'ВКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'disable':
                icon = '✅'
                title = 'ОТКЛЮЧЕНИЕ ТЕХРАБОТ'

            elif event_type == 'api_status':
                if status == 'online':
                    icon = '🟢'
                    title = 'API REMNAWAVE ВОССТАНОВЛЕНО'
                else:
                    icon = '🔴'
                    title = 'API REMNAWAVE НЕДОСТУПНО'

            elif event_type == 'monitoring':
                if status == 'started':
                    icon = '🔍'
                    title = 'МОНИТОРИНГ ЗАПУЩЕН'
                else:
                    icon = '⏹️'
                    title = 'МОНИТОРИНГ ОСТАНОВЛЕН'
            else:
                icon = 'ℹ️'
                title = 'СИСТЕМА ТЕХРАБОТ'

            message_parts = [f'{icon} <b>{title}</b>', '']

            if event_type == 'enable':
                if details.get('reason'):
                    message_parts.append(f'📋 <b>Причина:</b> {details["reason"]}')

                if details.get('enabled_at'):
                    enabled_at = details['enabled_at']
                    if isinstance(enabled_at, str):
                        enabled_at = datetime.fromisoformat(enabled_at)
                    message_parts.append(
                        f'🕐 <b>Время включения:</b> {format_local_datetime(enabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                message_parts.append(
                    f'🤖 <b>Автоматически:</b> {"Да" if details.get("auto_enabled", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('❗ Обычные пользователи временно не могут использовать бота.')

            elif event_type == 'disable':
                if details.get('disabled_at'):
                    disabled_at = details['disabled_at']
                    if isinstance(disabled_at, str):
                        disabled_at = datetime.fromisoformat(disabled_at)
                    message_parts.append(
                        f'🕐 <b>Время отключения:</b> {format_local_datetime(disabled_at, "%d.%m.%Y %H:%M:%S")}'
                    )

                if details.get('duration'):
                    duration = details['duration']
                    if isinstance(duration, (int, float)):
                        hours = int(duration // 3600)
                        minutes = int((duration % 3600) // 60)
                        if hours > 0:
                            duration_str = f'{hours}ч {minutes}мин'
                        else:
                            duration_str = f'{minutes}мин'
                        message_parts.append(f'⏱️ <b>Длительность:</b> {duration_str}')

                message_parts.append(
                    f'🤖 <b>Было автоматическим:</b> {"Да" if details.get("was_auto", False) else "Нет"}'
                )
                message_parts.append('')
                message_parts.append('✅ Сервис снова доступен для пользователей.')

            elif event_type == 'api_status':
                message_parts.append(f'🔗 <b>API URL:</b> {details.get("api_url", "неизвестно")}')

                if status == 'online':
                    if details.get('response_time'):
                        message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

                    if details.get('consecutive_failures', 0) > 0:
                        message_parts.append(f'🔄 <b>Неудачных попыток было:</b> {details["consecutive_failures"]}')

                    message_parts.append('')
                    message_parts.append('API снова отвечает на запросы.')

                else:
                    if details.get('consecutive_failures'):
                        message_parts.append(f'🔄 <b>Попытка №:</b> {details["consecutive_failures"]}')

                    if details.get('error'):
                        error_msg = str(details['error'])[:100]
                        message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                    message_parts.append('')
                    message_parts.append('⚠️ Началась серия неудачных проверок API.')

            elif event_type == 'monitoring':
                if status == 'started':
                    if details.get('check_interval'):
                        message_parts.append(f'🔄 <b>Интервал проверки:</b> {details["check_interval"]} сек')

                    if details.get('auto_enable_configured') is not None:
                        auto_enable = 'Включено' if details['auto_enable_configured'] else 'Отключено'
                        message_parts.append(f'🤖 <b>Автовключение:</b> {auto_enable}')

                    if details.get('max_failures'):
                        message_parts.append(f'🎯 <b>Порог ошибок:</b> {details["max_failures"]}')

                    message_parts.append('')
                    message_parts.append('Система будет следить за доступностью API.')

                else:
                    message_parts.append('Автоматический мониторинг API остановлен.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о техработах', error=e)
            return False

    async def send_remnawave_panel_status_notification(self, status: str, details: dict[str, Any] = None) -> bool:
        if not self._is_enabled():
            return False

        try:
            details = details or {}

            status_config = {
                'online': {'icon': '🟢', 'title': 'ПАНЕЛЬ REMNAWAVE ДОСТУПНА', 'alert_type': 'success'},
                'offline': {'icon': '🔴', 'title': 'ПАНЕЛЬ REMNAWAVE НЕДОСТУПНА', 'alert_type': 'error'},
                'degraded': {'icon': '🟡', 'title': 'ПАНЕЛЬ REMNAWAVE РАБОТАЕТ СО СБОЯМИ', 'alert_type': 'warning'},
                'maintenance': {'icon': '🔧', 'title': 'ПАНЕЛЬ REMNAWAVE НА ОБСЛУЖИВАНИИ', 'alert_type': 'info'},
            }

            config = status_config.get(status, status_config['offline'])

            message_parts = [f'{config["icon"]} <b>{config["title"]}</b>', '']

            if details.get('api_url'):
                message_parts.append(f'🔗 <b>URL:</b> {details["api_url"]}')

            if details.get('response_time'):
                message_parts.append(f'⚡ <b>Время отклика:</b> {details["response_time"]} сек')

            if details.get('last_check'):
                last_check = details['last_check']
                if isinstance(last_check, str):
                    last_check = datetime.fromisoformat(last_check)
                message_parts.append(f'🕐 <b>Последняя проверка:</b> {format_local_datetime(last_check, "%H:%M:%S")}')

            if status == 'online':
                if details.get('uptime'):
                    message_parts.append(f'⏱️ <b>Время работы:</b> {details["uptime"]}')

                if details.get('users_online'):
                    message_parts.append(f'👥 <b>Пользователей онлайн:</b> {details["users_online"]}')

                message_parts.append('')
                message_parts.append('✅ Все системы работают нормально.')

            elif status == 'offline':
                if details.get('error'):
                    error_msg = str(details['error'])[:150]
                    message_parts.append(f'❌ <b>Ошибка:</b> {error_msg}')

                if details.get('consecutive_failures'):
                    message_parts.append(f'🔄 <b>Неудачных попыток:</b> {details["consecutive_failures"]}')

                message_parts.append('')
                message_parts.append('⚠️ Панель недоступна. Проверьте соединение и статус сервера.')

            elif status == 'degraded':
                if details.get('issues'):
                    issues = details['issues']
                    if isinstance(issues, list):
                        message_parts.append('⚠️ <b>Обнаруженные проблемы:</b>')
                        for issue in issues[:3]:
                            message_parts.append(f'   • {issue}')
                    else:
                        message_parts.append(f'⚠️ <b>Проблема:</b> {issues}')

                message_parts.append('')
                message_parts.append('Панель работает, но возможны задержки или сбои.')

            elif status == 'maintenance':
                if details.get('maintenance_reason'):
                    message_parts.append(f'🔧 <b>Причина:</b> {html.escape(details["maintenance_reason"])}')

                if details.get('estimated_duration'):
                    message_parts.append(f'⏰ <b>Ожидаемая длительность:</b> {details["estimated_duration"]}')

                message_parts.append('')
                message_parts.append('Панель временно недоступна для обслуживания.')

            message_parts.append('')
            message_parts.append(f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>')

            message = '\n'.join(message_parts)

            return await self._send_message(message, category=NotificationCategory.INFRASTRUCTURE)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о статусе панели Remnawave', error=e)
            return False

    async def send_subscription_update_notification(
        self,
        db: AsyncSession,
        user: User,
        subscription: Subscription,
        update_type: str,
        old_value: Any,
        new_value: Any,
        price_paid: int = 0,
    ) -> bool:
        if not self._is_enabled():
            return False

        try:
            price_paid = int(price_paid or 0)
            titles = {
                'traffic': '📊 Докупка трафика',
                'devices': '📱 Докупка устройств',
                'servers': '🌐 Смена серверов',
            }
            title = titles.get(update_type, '⚙️ Изменение подписки')
            if update_type == 'devices' and str(new_value).isdigit() and str(old_value).isdigit():
                if int(new_value) < int(old_value):
                    title = '📱 Устройств стало меньше'
                elif int(new_value) == int(old_value):
                    title = '📱 Устройства не изменились'
            tariff = await self._get_tariff(db, subscription)

            if update_type == 'servers':
                old_servers = await self._format_servers_detailed(old_value)
                new_servers = await self._format_servers_detailed(new_value)
                what = f'{old_servers} → {new_servers}'
            elif update_type == 'devices':
                what = self._owner_devices_addon_line(subscription, tariff, old_value, new_value, price_paid)
            else:
                old_formatted = self._format_update_value(old_value, update_type)
                new_formatted = self._format_update_value(new_value, update_type)
                what = f'{old_formatted} → {new_formatted}'

            if price_paid > 0:
                headline = self._owner_title(title, price_paid)
            elif title in ('📱 Устройств стало меньше', '📱 Устройства не изменились'):
                headline = title
            else:
                headline = f'{title} — бесплатно'
            if title == '📱 Устройства не изменились':
                verb = None  # «Изменил(а)» под таким заголовком противоречил бы ему
            elif title.startswith('📱 Устройств'):
                verb = 'Изменил(а)'
            elif title == '🌐 Смена серверов':
                verb = 'Сменил(а) серверы'
            elif price_paid > 0:
                verb = 'Докупил(а)'
            else:
                verb = 'Получил(а)'
            message = self._owner_card(
                headline,
                self._owner_who(user, html.escape(tariff.name) if tariff else None, verb=verb),
                what,
                self._owner_balance_line(user.balance_kopeks),
            )
            return await self._send_message(message, category=NotificationCategory.ADDONS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления об изменении подписки', error=e)
            return False

    async def _format_servers_detailed(self, server_uuids: list[str]) -> str:
        if not server_uuids:
            return 'Нет серверов'

        try:
            from app.handlers.subscription import get_servers_display_names

            servers_names = await get_servers_display_names(server_uuids)

            if servers_names and servers_names != 'Нет серверов':
                return f'{len(server_uuids)} серверов ({servers_names})'
            return f'{len(server_uuids)} серверов'

        except Exception as e:
            logger.warning('Ошибка получения названий серверов для уведомления', error=e)
            return f'{len(server_uuids)} серверов'

    def _format_update_value(self, value: Any, update_type: str) -> str:
        if update_type == 'traffic':
            if value == 0:
                return '♾ Безлимитный'
            return f'{value} ГБ'
        if update_type == 'devices':
            return f'{value} устройств'
        if update_type == 'servers':
            if isinstance(value, list):
                return f'{len(value)} серверов'
            return str(value)
        return str(value)

    async def send_partner_application_notification(
        self,
        user: User,
        application_data: dict[str, Any],
    ) -> bool:
        """Уведомление о новой заявке на партнёрку."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '🤝 <b>ЗАЯВКА НА ПАРТНЁРКУ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 @{html.escape(username)}')

            message_lines.append('')

            if application_data.get('company_name'):
                message_lines.append(f'🏢 Компания: {html.escape(str(application_data["company_name"]))}')
            if application_data.get('telegram_channel'):
                message_lines.append(f'📢 Канал: {html.escape(str(application_data["telegram_channel"]))}')
            if application_data.get('website_url'):
                message_lines.append(f'🌐 Сайт: {html.escape(str(application_data["website_url"]))}')
            if application_data.get('description'):
                desc = str(application_data['description'])
                if len(desc) > 200:
                    desc = desc[:197] + '...'
                message_lines.append(f'📝 {html.escape(desc)}')
            if application_data.get('expected_monthly_referrals'):
                message_lines.append(f'👥 Ожидаемых рефералов: {application_data["expected_monthly_referrals"]}/мес')
            if application_data.get('desired_commission_percent'):
                message_lines.append(f'💰 Желаемая комиссия: {application_data["desired_commission_percent"]}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о заявке на партнёрку', error=e)
            return False

    async def send_withdrawal_request_notification(
        self,
        user: User,
        amount_kopeks: int,
        payment_details: str | None = None,
    ) -> bool:
        """Уведомление о запросе на вывод средств."""
        if not self._is_enabled():
            return False

        try:
            user_display = self._get_user_display(user)
            user_id_display = self._get_user_identifier_display(user)

            message_lines = [
                '💸 <b>ЗАПРОС НА ВЫВОД СРЕДСТВ</b>',
                '',
                f'👤 {user_display} ({user_id_display})',
            ]

            username = getattr(user, 'username', None)
            if username:
                message_lines.append(f'📱 @{html.escape(username)}')

            message_lines.extend(
                [
                    '',
                    f'💵 <b>Сумма: {settings.format_price(amount_kopeks)}</b>',
                    f'💰 Баланс: {settings.format_price(user.balance_kopeks)}',
                ]
            )

            if payment_details:
                details = str(payment_details)
                if len(details) > 200:
                    details = details[:197] + '...'
                message_lines.extend(['', f'💳 Реквизиты: {html.escape(details)}'])

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            return await self._send_message('\n'.join(message_lines), category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о запросе на вывод', error=e)
            return False

    async def send_bulk_ban_notification(
        self,
        admin_user_id: int,
        successfully_banned: int,
        not_found: int,
        errors: int,
        admin_name: str = 'Администратор',
    ) -> bool:
        """Отправляет уведомление о массовой блокировке пользователей"""
        if not self._is_enabled():
            return False

        try:
            message_lines = [
                '🛑 <b>МАССОВАЯ БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ</b>',
                '',
                f'👮 <b>Администратор:</b> {html.escape(admin_name)}',
                f'🆔 <b>ID администратора:</b> {admin_user_id}',
                '',
                '📊 <b>Результаты:</b>',
                f'✅ Успешно заблокировано: {successfully_banned}',
                f'❌ Не найдено: {not_found}',
                f'💥 Ошибок: {errors}',
            ]

            total_processed = successfully_banned + not_found + errors
            if total_processed > 0:
                success_rate = (successfully_banned / total_processed) * 100
                message_lines.append(f'📈 Успешность: {success_rate:.1f}%')

            message_lines.extend(
                [
                    '',
                    f'⏰ <i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
                ]
            )

            message = '\n'.join(message_lines)
            return await self._send_message(message, category=NotificationCategory.PARTNERS)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о массовой блокировке', error=e)
            return False

    async def send_ticket_event_notification(
        self,
        text: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
        *,
        media_file_id: str | None = None,
        media_type: str | None = None,
    ) -> bool:
        """Публичный метод для отправки уведомлений по тикетам в админ-топик.
        Учитывает настройки включенности в settings.
        Если передан media_file_id, отправляет медиа в тот же топик вместе с текстом.
        """
        # Respect runtime toggle for admin ticket notifications
        try:
            from app.services.support_settings_service import SupportSettingsService

            runtime_enabled = SupportSettingsService.get_admin_ticket_notifications_enabled()
        except Exception:
            runtime_enabled = True
        if not (self._is_enabled() and runtime_enabled):
            logger.info(
                'Ticket notification skipped',
                _is_enabled=self._is_enabled(),
                runtime_enabled=runtime_enabled,
            )
            return False

        # Если есть медиа, отправляем фото с текстом как caption (если влезает) или текст + фото
        if media_file_id and media_type == 'photo':
            return await self._send_ticket_photo_notification(text, media_file_id, keyboard)

        return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def _send_ticket_photo_notification(
        self,
        text: str,
        photo_file_id: str,
        keyboard: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Отправить фото с текстом в тикет-топик.
        Если текст помещается в caption (≤1024 символов после парсинга HTML) — фото с caption.
        Иначе — сначала текст, потом фото в тот же топик.
        """
        if not self.chat_id:
            return False

        thread_id = self._resolve_topic_id(category=NotificationCategory.TICKETS)

        try:
            if not caption_exceeds_telegram_limit(text):
                # Фото с caption — всё в одном сообщении
                photo_kwargs: dict = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                    'caption': text,
                    'parse_mode': 'HTML',
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                if keyboard:
                    photo_kwargs['reply_markup'] = keyboard
                await self.bot.send_photo(**photo_kwargs)
            else:
                # Текст отдельно, фото следом в тот же топик
                await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)
                photo_kwargs = {
                    'chat_id': self.chat_id,
                    'photo': photo_file_id,
                }
                if thread_id:
                    photo_kwargs['message_thread_id'] = thread_id
                await self.bot.send_photo(**photo_kwargs)

            return True
        except Exception as e:
            logger.error('Ошибка отправки фото-уведомления тикета', error=e)
            # Fallback: отправляем хотя бы текст
            return await self._send_message(text, reply_markup=keyboard, category=NotificationCategory.TICKETS)

    async def send_suspicious_traffic_notification(self, message: str, bot: Bot, topic_id: int | None = None) -> bool:
        """
        Отправляет уведомление о подозрительной активности трафика

        Args:
            message: текст уведомления
            bot: экземпляр бота для отправки сообщения
            topic_id: ID топика для отправки уведомления (если не указан, использует стандартный)
        """
        if not self.chat_id:
            logger.warning('ADMIN_NOTIFICATIONS_CHAT_ID не настроен')
            return False

        # Используем специальный топик для подозрительной активности, если он задан
        notification_topic_id = topic_id or self.topic_id

        try:
            message_kwargs = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }

            if notification_topic_id:
                message_kwargs['message_thread_id'] = notification_topic_id

            await bot.send_message(**message_kwargs)
            logger.info(
                'Уведомление о подозрительной активности отправлено в чат топик',
                chat_id=self.chat_id,
                notification_topic_id=notification_topic_id,
            )
            return True

        except TelegramForbiddenError:
            logger.error('Бот не имеет прав для отправки в чат', chat_id=self.chat_id)
            return False
        except TelegramBadRequest as e:
            logger.error('Ошибка отправки уведомления о подозрительной активности', error=e)
            return False
        except Exception as e:
            logger.error('Неожиданная ошибка при отправке уведомления о подозрительной активности', error=e)
            return False

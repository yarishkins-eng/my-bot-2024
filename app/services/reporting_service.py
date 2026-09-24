import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from enum import Enum
from html import escape
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import func, not_, or_, select
from sqlalchemy.sql import true

from app.config import settings
from app.database.crud.tariff import get_all_tariffs, get_trial_tariff
from app.database.crud.transaction import REAL_PAYMENT_METHODS, addon_description_clause
from app.database.database import AsyncSessionLocal
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Subscription,
    SubscriptionEvent,
    SubscriptionStatus,
    Ticket,
    TicketStatus,
    Transaction,
    TransactionType,
    User,
)
from app.utils.user_utils import count_trial_and_paying_users, operational_person_clause


logger = structlog.get_logger(__name__)


class ReportingServiceError(RuntimeError):
    """Base error for the reporting service."""


class ReportPeriod(Enum):
    DAILY = 'daily'
    WEEKLY = 'weekly'
    MONTHLY = 'monthly'


@dataclass(slots=True)
class ReportPeriodRange:
    start_msk: datetime
    end_msk: datetime
    label: str


class ReportingService:
    """Generates admin summary reports (text only, no charts)."""

    def __init__(self) -> None:
        self.bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._moscow_tz = ZoneInfo('Europe/Moscow')

    def set_bot(self, bot: Bot) -> None:
        self.bot = bot

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self.stop()

        if not settings.ADMIN_REPORTS_ENABLED:
            logger.info('Сервис отчетов отключен настройками')
            return

        if not self.bot:
            logger.warning('Невозможно запустить сервис отчетов без экземпляра бота')
            return

        chat_id = settings.get_reports_chat_id()
        if not chat_id:
            logger.warning('Сервис отчетов не запущен: не указан чат для отправки отчетов')
            return

        send_time = settings.get_reports_send_time()
        if not send_time:
            logger.warning('Сервис отчетов не запущен: не указано время ежедневной отправки')
            return

        self._task = asyncio.create_task(self._auto_daily_loop(send_time))
        logger.info('📊 Сервис отчетов запущен: ежедневная отправка в по МСК', send_time=send_time.strftime('%H:%M'))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def send_report(
        self,
        period: ReportPeriod,
        *,
        report_date: date | None = None,
        send_to_topic: bool = False,
    ) -> str:
        report_text = await self._build_report(period, report_date)

        if send_to_topic:
            await self._deliver_report(report_text)

        return report_text

    async def _auto_daily_loop(self, send_time: datetime_time) -> None:
        try:
            next_run_utc, report_date = self._calculate_next_run(send_time)

            while True:
                now_utc = datetime.now(UTC)
                delay = (next_run_utc - now_utc).total_seconds()

                if delay > 0:
                    await asyncio.sleep(delay)

                try:
                    await self.send_report(
                        ReportPeriod.DAILY,
                        report_date=report_date,
                        send_to_topic=True,
                    )
                    logger.info('📊 Автоматический отчет за отправлен', report_date=report_date.strftime('%d.%m.%Y'))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error('Ошибка автоматической отправки отчета', exc=exc)

                next_run_utc, report_date = self._next_run_after(next_run_utc, send_time)

        except asyncio.CancelledError:
            logger.info('Сервис отчетов остановлен')
            raise
        except Exception as exc:
            logger.error('Критическая ошибка в сервисе отчетов', exc=exc)

    def _next_run_after(self, sent_run_utc: datetime, send_time: datetime_time) -> tuple[datetime, date]:
        # Проснулись на миллисекунду раньше срока — `_calculate_next_run` вернул бы ту же минуту,
        # и то же письмо ушло бы дважды
        next_run_utc, report_date = self._calculate_next_run(send_time)
        if next_run_utc <= sent_run_utc:
            next_run_utc += timedelta(days=1)
            report_date += timedelta(days=1)
        return next_run_utc, report_date

    def _calculate_next_run(
        self,
        send_time: datetime_time,
    ) -> tuple[datetime, date]:
        now_msk = datetime.now(self._moscow_tz)
        candidate = datetime.combine(now_msk.date(), send_time, tzinfo=self._moscow_tz)

        if now_msk >= candidate:
            candidate += timedelta(days=1)

        report_date = (candidate - timedelta(days=1)).date()
        return candidate.astimezone(UTC), report_date

    async def _deliver_report(self, report_text: str) -> None:
        if not self.bot:
            raise ReportingServiceError('Бот не инициализирован для отправки отчета')

        chat_id = settings.get_reports_chat_id()
        if not chat_id:
            raise ReportingServiceError('Не задан чат для отправки отчета')

        topic_id = settings.get_reports_topic_id()

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=report_text,
                message_thread_id=topic_id,
                parse_mode='HTML',
            )
            from app.services.manager_alert_service import ManagerAlertTopic, manager_alert_service

            await manager_alert_service.send(self.bot, ManagerAlertTopic.REPORTS, report_text)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.error('Не удалось отправить отчет', exc=exc)
            raise ReportingServiceError('Не удалось отправить отчет в чат') from exc

    # ---------- referral helpers ----------

    def _referral_markers(self) -> list:
        """
        Набор условий, по которым операция помечается как реферальная (если вдруг записана типом DEPOSIT).
        """
        clauses = []

        # Явные флаги
        if hasattr(Transaction, 'is_referral_bonus'):
            clauses.append(Transaction.is_referral_bonus == true())
        if hasattr(Transaction, 'is_bonus'):
            clauses.append(Transaction.is_bonus == true())

        # Источник/причина
        if hasattr(Transaction, 'source'):
            clauses.append(Transaction.source == 'referral')
            clauses.append(Transaction.source == 'referral_bonus')
        if hasattr(Transaction, 'reason'):
            clauses.append(Transaction.reason == 'referral')
            clauses.append(Transaction.reason == 'referral_bonus')
            clauses.append(Transaction.reason == 'referral_reward')

        # Текстовые поля
        like_patterns = ['%реферал%', '%реферальн%', '%referral%']
        if hasattr(Transaction, 'description'):
            for pattern in like_patterns:
                try:
                    clauses.append(Transaction.description.ilike(pattern))
                except Exception:
                    pass
        if hasattr(Transaction, 'comment'):
            for pattern in like_patterns:
                try:
                    clauses.append(Transaction.comment.ilike(pattern))
                except Exception:
                    pass

        return [clause for clause in clauses if clause is not None]

    def _exclude_referral_deposits_condition(self):
        """
        Условие «это НЕ реферальный бонус».
        Если нет ни одного маркера — ничего не исключаем.
        """
        markers = self._referral_markers()
        if not markers:
            return true()
        return not_(or_(*markers))

    # --------------------------------------

    async def _build_report(
        self,
        period: ReportPeriod,
        report_date: date | None,
    ) -> str:
        """Письмо владельцу — 8 строк по его правилам (ОТЧ-7, 20.09.2026).

        Пробный — не подписка; подписка — только когда заплатили деньгами; Team — не клиенты;
        стенды и удалённые — не люди. Числа «Сейчас» считаются той же функцией, что плитки
        кабинета «Пользователи» (`count_trial_and_paying_users`) — письмо и экран не расходятся.
        """
        period_range = self._get_period_range(period, report_date)
        start_utc = period_range.start_msk.astimezone(UTC)
        end_utc = period_range.end_msk.astimezone(UTC)

        async with AsyncSessionLocal() as session:
            stats = await self._collect_period_stats(session, start_utc, end_utc)
            totals = await self._collect_current_totals(session)
            losses = await self._collect_loss_stats(session, start_utc, end_utc)

        header = (
            f'📊 <b>Отчёт за {period_range.label}</b>'
            if period == ReportPeriod.DAILY
            else f'📊 <b>Отчёт за период {period_range.label}</b>'
        )
        split_total = stats['sales_after_trial'] + stats['sales_renewals'] + stats['sales_new']
        # Формулировки — решения владельца 20.09.2026 (записка ОТЧ-7, вопросы после волны 1):
        # «сразу без пробного» вместо «новые», «без пометки N» четвёртым членом, «живых денег».
        split = (
            f'после пробного {stats["sales_after_trial"]} · продления {stats["sales_renewals"]}'
            f' · сразу без пробного {stats["sales_new"]}'
        )
        unmarked = stats['sales_count'] - split_total
        if unmarked > 0:
            # Разбивка идёт по событиям: их нет у продаж до К-2 (18.09.2026), при выключенных
            # уведомлениях (мина MP) и у событий без пометки типа (до РК-3). Число и рубли — по
            # проводкам. Четвёртый член ряда, чтобы сумма сходилась, а не подгонка.
            split += f' · без пометки {unmarked}'
        elif unmarked < 0:
            split += f' (пометок больше, чем продаж: {-unmarked})'
        registrations = stats['campaign_registrations']
        campaign_line = f'по рекламе: {stats["campaign_registrations_total"]}'
        if len(registrations) == 1:
            campaign_line += f' ({escape(registrations[0][0], quote=False)})'  # одна кампания — число не повторяем
        elif registrations:
            campaign_line += (
                ' (' + ', '.join(f'{escape(name, quote=False)} — {count}' for name, count in registrations) + ')'
            )

        def panel(value: int | None) -> str:
            return 'панель не ответила' if value is None else str(value)

        lines = [
            header,
            '',
            '💎 <b>Продажи</b>',
            (
                f'• Купили: <b>{stats["sales_count"]}</b> на <b>{self._format_amount(stats["sales_amount"])}</b>'
                f' — {split}'
            ),
            f'• Докупили устройств и трафика: {stats["addons_count"]} на {self._format_amount(stats["addons_amount"])}',
            (
                f'• Пришло живых денег: <b>{self._format_amount(stats["money_in_amount"])}</b>'
                f' (пополнений баланса {stats["deposits_count"]} · оплат сразу за подписку {stats["receipts_count"]})'
            ),
            '',
            '📌 <b>Сейчас</b>',
            f'• Платят: <b>{totals["paying"]}</b> · на пробном: <b>{totals["on_trial"]}</b>',
            '',
            '🚪 <b>За день</b>' if period == ReportPeriod.DAILY else '🚪 <b>За период</b>',
            f'• Открыли бота: {stats["new_users"]} · {campaign_line} · взяли пробный: {stats["new_trials"]}',
            '',
            # Четыре строки потерь — решение владельца 24.09.2026 (ВК-0, ОТЧ-8); «панель не ответила» вместо
            # числа, когда RemnaWave не отдала первые подключения: ноль здесь был бы ложью
            '📉 <b>Потери за вчера</b>' if period == ReportPeriod.DAILY else '📉 <b>Потери за период</b>',
            f'• Взяли пробный: {losses["trials"]}, подключились к VPN: {panel(losses["connected"])}',
            f'• Не подключились за сутки после пробного: {panel(losses["not_connected_after_day"])}',
            f'• Платная подписка закончилась и не продлена: {losses["paid_expired"]}',
            f'• Пополнили баланс и ничего не купили: {losses["topped_up_idle"]}',
            '',
            f'🎟 Поддержка: {stats["new_tickets"]} новых · {totals["open_tickets"]} открытых',
        ]
        return '\n'.join(lines)

    def _get_period_range(
        self,
        period: ReportPeriod,
        report_date: date | None,
    ) -> ReportPeriodRange:
        now_msk = datetime.now(self._moscow_tz)

        if period == ReportPeriod.DAILY:
            target_date = report_date or (now_msk.date() - timedelta(days=1))
            start = datetime.combine(target_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = start + timedelta(days=1)
        elif period == ReportPeriod.WEEKLY:
            end_date = report_date or now_msk.date()
            start_date = end_date - timedelta(days=7)
            start = datetime.combine(start_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = datetime.combine(end_date, datetime_time.min, tzinfo=self._moscow_tz)
        elif period == ReportPeriod.MONTHLY:
            end_date = report_date or now_msk.date()
            start_date = end_date - timedelta(days=30)
            start = datetime.combine(start_date, datetime_time.min, tzinfo=self._moscow_tz)
            end = datetime.combine(end_date, datetime_time.min, tzinfo=self._moscow_tz)
        else:  # pragma: no cover - defensive branch
            raise ReportingServiceError(f'Неизвестный период отчета: {period}')

        label = self._format_period_label(start, end)
        return ReportPeriodRange(start, end, label)

    async def _collect_current_totals(self, session) -> dict:
        people = await count_trial_and_paying_users(session)
        open_tickets_result = await session.execute(
            select(func.count(Ticket.id))
            .join(User, User.id == Ticket.user_id)
            .where(
                operational_person_clause(),  # тикет стенда — не открытый тикет клиента
                Ticket.status.in_(
                    [
                        TicketStatus.OPEN.value,
                        TicketStatus.ANSWERED.value,
                        TicketStatus.PENDING.value,
                    ]
                ),
            )
        )
        return {
            'paying': int(people.get('paying') or 0),
            'on_trial': int(people.get('on_trial') or 0),
            'open_tickets': int(open_tickets_result.scalar() or 0),
        }

    async def _collect_period_stats(
        self,
        session,
        start_utc: datetime,
        end_utc: datetime,
    ) -> dict:
        # «Человек, а не стенд и не удалённый» — тот же предикат, что у плиток кабинета (галка в базе + `.env`)
        person = operational_person_clause()

        def money(*conditions, everyone: bool = False):
            # `everyone=True` — деньги считаем как в выписке платёжной системы, включая стенды и удалённых:
            # решение владельца 20.09.2026 («Пришло живых денег» = Platega). Люди и продажи — без стендов.
            return (
                select(func.count(Transaction.id), func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0))
                .join(User, User.id == Transaction.user_id)
                .where(
                    Transaction.is_completed == true(),
                    # Нулевая проводка — не деньги: «Смена тарифа администратором» пишется тем же типом на 0 ₽
                    Transaction.amount_kopeks != 0,
                    Transaction.created_at >= start_utc,
                    Transaction.created_at < end_utc,
                    true() if everyone else person,
                    *conditions,
                )
            )

        # Продажи и докупки лежат одним типом проводки; отличает их только описание
        # (`ADDON_DESCRIPTION_PATTERNS`), и на этом же стоит кабинет.
        is_addon = addon_description_clause(Transaction.description)
        sales_count, sales_amount = (
            await session.execute(money(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value, not_(is_addon)))
        ).one()
        addons_count, addons_amount = (
            await session.execute(money(Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value, is_addon))
        ).one()

        # 🔴 Деньги приходят ДВУМЯ типами: пополнение баланса (`deposit`) и оплата картой напрямую с
        # кассы (`provider_receipt`). Прежний отчёт видел только первый и терял треть выручки.
        real_money = (
            Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
            self._exclude_referral_deposits_condition(),
        )
        deposits_count, deposits_amount = (
            await session.execute(money(Transaction.type == TransactionType.DEPOSIT.value, *real_money, everyone=True))
        ).one()
        receipts_count, receipts_amount = (
            await session.execute(
                money(Transaction.type == TransactionType.PROVIDER_RECEIPT.value, *real_money, everyone=True)
            )
        ).one()

        # Разбивка продаж — по тем же признакам, по которым названы карточки в теме «Продажи»:
        # `purchase` + `was_trial_conversion` → после пробного; `renewal` или `purchase_type='renewal'`
        # → продление; остальное — новые. Считаем в Python: `extra` — JSON, и это кроссбазово.
        events = (
            await session.execute(
                select(SubscriptionEvent.event_type, SubscriptionEvent.extra)
                .join(User, User.id == SubscriptionEvent.user_id)
                .where(
                    SubscriptionEvent.event_type.in_(('purchase', 'renewal')),
                    SubscriptionEvent.occurred_at >= start_utc,
                    SubscriptionEvent.occurred_at < end_utc,
                    person,
                )
            )
        ).all()
        after_trial = renewals = new_sales = 0
        for event_type, extra in events:
            extra = extra or {}
            if event_type == 'purchase' and extra.get('was_trial_conversion'):
                after_trial += 1
            elif event_type == 'renewal' or extra.get('purchase_type') == 'renewal':
                renewals += 1
            elif extra.get('purchase_type') == 'first_purchase':
                # только явная пометка «первая покупка» делает продажу «сразу без пробного»; смена тарифа
                # (`tariff_switch`, путь заперт — мина BX) и любая другая пометка идут в «без пометки»
                new_sales += 1
            # событие без `purchase_type` (записано до РК-3 20.09.2026) — не «новое», а «без пометки»:
            # у карточки такого события «Продление» решалось по флагу, письму это неизвестно

        new_users = int(
            (
                await session.execute(
                    select(func.count(User.id)).where(
                        User.created_at >= start_utc,
                        User.created_at < end_utc,
                        person,
                    )
                )
            ).scalar()
            or 0
        )

        # «Взяли пробный» — одно определение с блоком «Потери» (`_trial_taker_rows`)
        trial_tariff = await get_trial_tariff(session)
        new_trials = len(await self._trial_taker_rows(session, start_utc, end_utc, trial_tariff))

        # По рекламе — регистрации (не переходы: их отправка снята 19.09), по имени кампании.
        campaign_rows = (
            await session.execute(
                select(AdvertisingCampaign.name, func.count(AdvertisingCampaignRegistration.id))
                .join(AdvertisingCampaign, AdvertisingCampaign.id == AdvertisingCampaignRegistration.campaign_id)
                .join(User, User.id == AdvertisingCampaignRegistration.user_id)
                .where(
                    AdvertisingCampaignRegistration.created_at >= start_utc,
                    AdvertisingCampaignRegistration.created_at < end_utc,
                    person,
                )
                .group_by(AdvertisingCampaign.name)
                .order_by(func.count(AdvertisingCampaignRegistration.id).desc(), AdvertisingCampaign.name)
            )
        ).all()
        # имена кампаний на боевом с хвостовыми пробелами («Кувалда 7000₽  ») — в письме они лишние
        campaign_registrations = [(str(name).strip(), int(count or 0)) for name, count in campaign_rows]

        new_tickets = int(
            (
                await session.execute(
                    select(func.count(Ticket.id))
                    .join(User, User.id == Ticket.user_id)
                    .where(
                        Ticket.created_at >= start_utc,
                        Ticket.created_at < end_utc,
                        person,
                    )
                )
            ).scalar()
            or 0
        )

        return {
            'sales_count': int(sales_count or 0),
            'sales_amount': int(sales_amount or 0),
            'sales_after_trial': after_trial,
            'sales_renewals': renewals,
            'sales_new': new_sales,
            'addons_count': int(addons_count or 0),
            'addons_amount': int(addons_amount or 0),
            'deposits_count': int(deposits_count or 0),
            'receipts_count': int(receipts_count or 0),
            'money_in_amount': int(deposits_amount or 0) + int(receipts_amount or 0),
            'new_users': new_users,
            'new_trials': new_trials,
            'campaign_registrations': campaign_registrations,
            'campaign_registrations_total': sum(count for _, count in campaign_registrations),
            'new_tickets': new_tickets,
        }

    async def _trial_taker_rows(
        self, session, start_utc: datetime, end_utc: datetime, trial_tariff
    ) -> list[tuple[int, int]]:
        """(id подписки, id человека) взявших пробный в окне — одно определение для «За день» и «Потерь».

        Только канонический пробный тариф: метка `is_trial` стоит и на перекрашенных Team (ОТЧ-4);
        брошенное оформление (`pending`) — не пробный. 🔴 Покупка после пробного ПЕРЕПИСЫВАЕТ ту же
        строку (`is_trial` → False, тариф → платный), и взявший пробный выпадал в тот же день, когда письмо
        считало его «после пробного» (замер 19.09: 44 вместо 47). Добираем по событию покупки с признаком
        конверсии у подписки, заведённой в окне. Признак читаем в Python: `extra` — JSON.
        """
        person = operational_person_clause()
        in_window = (Subscription.created_at >= start_utc, Subscription.created_at < end_utc, person)
        still_trial = [Subscription.is_trial.is_(True), Subscription.status != SubscriptionStatus.PENDING.value]
        if trial_tariff is not None:
            still_trial.append(Subscription.tariff_id == trial_tariff.id)
        rows = (
            await session.execute(
                select(Subscription.id, Subscription.user_id)
                .join(User, User.id == Subscription.user_id)
                .where(*in_window, *still_trial)
            )
        ).all()
        takers = {(int(sub_id), int(user_id)) for sub_id, user_id in rows}
        converted = (
            await session.execute(
                select(Subscription.id, Subscription.user_id, SubscriptionEvent.extra)
                .select_from(SubscriptionEvent)
                .join(Subscription, Subscription.id == SubscriptionEvent.subscription_id)
                .join(User, User.id == Subscription.user_id)
                .where(SubscriptionEvent.event_type == 'purchase', *in_window, Subscription.is_trial.is_not(True))
            )
        ).all()
        takers |= {
            (int(sub_id), int(user_id))
            for sub_id, user_id, extra in converted
            if (extra or {}).get('was_trial_conversion')
        }
        return sorted(takers)

    async def _connected_panel_uuids(self) -> set[str] | None:
        """UUID клиентов панели с первым подключением — тот же читатель, что у монитора «не подключился»
        (`None` — панель не ответила, и это не ноль)."""
        from app.services.monitoring_service import monitoring_service

        return await monitoring_service._fetch_connected_panel_uuids()

    async def _collect_loss_stats(self, session, start_utc: datetime, end_utc: datetime) -> dict:
        """Блок «Потери» — четыре строки по решению владельца 24.09.2026 (ВК-0, ОТЧ-8).

        Люди — как во всём письме (`operational_person_clause`); пробный — как «взяли пробный»; платный
        тариф — как у плиток кабинета (не бесплатный и не пробный); «подключился» — по панели RemnaWave
        (`first_connected_at`) через `users.remnawave_uuid`. Продлённая подписка сюда не попадает: её
        `end_date` уже в будущем. «Не купил» — после ПЕРВОГО пополнения за день ни одной покупки до письма.
        """
        person = operational_person_clause()
        trial_tariff = await get_trial_tariff(session)
        takers = {user_id for _, user_id in await self._trial_taker_rows(session, start_utc, end_utc, trial_tariff)}
        span = end_utc - start_utc  # «за сутки» — окно раньше: к письму у всех прошло больше суток с активации
        earlier = {
            user_id
            for _, user_id in await self._trial_taker_rows(session, start_utc - span, end_utc - span, trial_tariff)
        }
        connected = await self._connected_panel_uuids() if takers or earlier else set()

        async def connected_count(user_ids: set[int]) -> int | None:
            if connected is None:
                return None
            if not user_ids:
                return 0
            uuids = (await session.execute(select(User.remnawave_uuid).where(User.id.in_(user_ids)))).scalars().all()
            return sum(1 for uuid in uuids if uuid in connected)

        took_and_connected = await connected_count(takers)
        earlier_connected = await connected_count(earlier)

        tariffs = await get_all_tariffs(session, include_inactive=True)
        excluded_tariff_ids = [tariff.id for tariff in tariffs if tariff.is_free or tariff.is_trial_available]
        paid_ended = [
            Subscription.is_trial.is_not(True),
            Subscription.end_date >= start_utc,
            Subscription.end_date < end_utc,
            person,
        ]
        if excluded_tariff_ids:
            paid_ended.append(or_(Subscription.tariff_id.is_(None), Subscription.tariff_id.not_in(excluded_tariff_ids)))
        paid_expired = (
            await session.execute(
                select(func.count(Subscription.id)).join(User, User.id == Subscription.user_id).where(*paid_ended)
            )
        ).scalar()

        first_topups = (
            await session.execute(
                select(Transaction.user_id, func.min(Transaction.created_at))
                .join(User, User.id == Transaction.user_id)
                .where(
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed == true(),
                    Transaction.amount_kopeks > 0,
                    Transaction.created_at >= start_utc,
                    Transaction.created_at < end_utc,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                    self._exclude_referral_deposits_condition(),
                    person,
                )
                .group_by(Transaction.user_id)
            )
        ).all()
        last_sales: dict = {}
        if first_topups:
            last_sales = dict(
                (
                    await session.execute(
                        select(Transaction.user_id, func.max(Transaction.created_at))
                        .where(
                            Transaction.user_id.in_([user_id for user_id, _ in first_topups]),
                            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                            Transaction.is_completed == true(),
                            Transaction.amount_kopeks != 0,
                            Transaction.created_at >= start_utc,
                        )
                        .group_by(Transaction.user_id)
                    )
                ).all()
            )
        topped_up_idle = sum(
            1
            for user_id, first_topup in first_topups
            if last_sales.get(user_id) is None or last_sales[user_id] < first_topup
        )
        return {
            'trials': len(takers),
            'connected': took_and_connected,
            'not_connected_after_day': None if earlier_connected is None else len(earlier) - earlier_connected,
            'paid_expired': int(paid_expired or 0),
            'topped_up_idle': topped_up_idle,
        }

    def _format_period_label(self, start: datetime, end: datetime) -> str:
        start_date = start.astimezone(self._moscow_tz).date()
        end_boundary = (end - timedelta(seconds=1)).astimezone(self._moscow_tz)
        end_date = end_boundary.date()

        if start_date == end_date:
            return start_date.strftime('%d.%m.%Y')

        return f'{start_date.strftime("%d.%m.%Y")} - {end_date.strftime("%d.%m.%Y")}'

    def _format_amount(self, amount_kopeks: int) -> str:
        # Как в карточках владельца: `format_price` с настройкой округления (на боевом копейки округляются
        # до рубля: 41.66 → «42 ₽»), чтобы письмо и карточки называли одну сумму одинаково.
        return settings.format_price(int(amount_kopeks or 0))


reporting_service = ReportingService()

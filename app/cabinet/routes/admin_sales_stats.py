"""Admin routes for sales statistics in cabinet."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Integer as SAInteger, and_, case, cast, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.config import settings
from app.database.crud.payment_gateway_stats import get_gateway_success_rates
from app.database.crud.subscription import ALIVE_SUBSCRIPTION_STATUSES
from app.database.crud.tariff import get_all_tariffs, get_trial_tariff
from app.database.crud.transaction import (
    REAL_PAYMENT_METHODS,
    addon_description_clause,
    device_addon_clause,
    traffic_addon_clause,
)
from app.database.models import (
    AdvertisingCampaign,
    GuestPurchase,
    PaymentMethod,
    Subscription,
    SubscriptionConversion,
    SubscriptionEvent,
    SubscriptionStatus,
    Tariff,
    TrafficPurchase,
    Transaction,
    TransactionType,
    User,
)
from app.services.campaign_service import get_campaign_performance
from app.services.reporting_service import reporting_service
from app.utils.user_utils import count_trial_and_paying_users, operational_person_clause, real_payment_user_ids

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/stats/sales', tags=['Cabinet Admin Sales Stats'])


# ============ Helpers ============

MAX_PERIOD_DAYS = 730  # 2 years max


def _parse_period(
    days: int | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime, datetime]:
    """Parse period from preset days or custom date range."""
    now = datetime.now(UTC)
    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid start_date format',
            )
        try:
            end = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid end_date format',
            )
        # Ensure timezone awareness
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        # Validate range
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='start_date must be before end_date',
            )
        if (end - start).days > MAX_PERIOD_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Date range cannot exceed {MAX_PERIOD_DAYS} days',
            )
        return start, end.replace(hour=23, minute=59, second=59)
    if days is not None and days > 0:
        days = min(days, MAX_PERIOD_DAYS)
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    # Default: all time (from epoch)
    return datetime(2020, 1, 1, tzinfo=UTC), now


# ============ СП-1: окно в сутках МСК и определения владельца ============
#
# Правила владельца (27.09.2026): Team не попадает никуда; пробный — только тариф «Пробный»; платит — только
# кто платил деньгами; стенды и удалённые — не люди; деньги — как выписка Platega (со стендами, как утреннее
# письмо). `_parse_period` выше НЕ трогается: его зовут прежние маршруты и старый кабинет (ревью замысла, W2-1).

_MSK = ZoneInfo('Europe/Moscow')
_PERIOD_DAYS = {'7d': 7, '30d': 30, '90d': 90}
SalesPeriod = Literal['yesterday', 'this_month', 'last_month', '7d', '30d', '90d', 'all', 'custom']


@dataclass(frozen=True)
class _SalesWindow:
    start: datetime
    end: datetime
    previous_start: datetime | None
    previous_end: datetime | None


def _msk_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_MSK).astimezone(UTC)


def _sales_window(period: str, start_date: str | None, end_date: str | None, now: datetime) -> _SalesWindow:
    """Окно по кнопке периода: сутки МСК, `[начало, конец)`, конец не позже «сейчас». Кабинет шлёт имя кнопки,
    а не даты по часам телефона. Сравнение — окно той же прошедшей длины перед ним; у месяца — те же числа
    прошлого месяца до того же часа, но не дальше его конца (31-е число, февраль)."""
    today = now.astimezone(_MSK).date()
    if period == 'yesterday':
        start, end = _msk_midnight(today - timedelta(days=1)), _msk_midnight(today)
        return _SalesWindow(start, end, start - timedelta(days=1), start)
    if period in ('this_month', 'last_month'):
        month_start = today.replace(day=1)
        end = now
        if period == 'last_month':
            end = _msk_midnight(month_start)
            month_start = (month_start - timedelta(days=1)).replace(day=1)
        start = _msk_midnight(month_start)
        previous_start = _msk_midnight((month_start - timedelta(days=1)).replace(day=1))
        if period == 'last_month':
            # закрытый месяц сравнивается с ЦЕЛЫМ месяцем перед ним, а не с «теми же числами» (ревью C1-6, C2-1)
            return _SalesWindow(start, end, previous_start, start)
        return _SalesWindow(start, end, previous_start, min(previous_start + (end - start), start))
    if period in _PERIOD_DAYS:
        days = _PERIOD_DAYS[period]
        start = _msk_midnight(today - timedelta(days=days - 1))
        return _SalesWindow(start, now, start - timedelta(days=days), now - timedelta(days=days))
    if period == 'all':
        return _SalesWindow(datetime(2020, 1, 1, tzinfo=UTC), now, None, None)
    if period == 'custom':
        try:
            first, last = date.fromisoformat(start_date or ''), date.fromisoformat(end_date or '')
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid custom period')
        if first > last or (last - first).days > MAX_PERIOD_DAYS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid custom period')
        days = timedelta(days=(last - first).days + 1)
        try:
            start = _msk_midnight(first)
            end = max(start, min(_msk_midnight(last + timedelta(days=1)), now))
            # сравнение — то же окно на целые сутки раньше, как у 7/30/90: окно, кончающееся «сейчас», сравнивается с
            # тем же часом N суток назад, а не с отрезком, начатым посреди суток (ревью C1-2)
            return _SalesWindow(start, end, start - days, end - days)
        except (OverflowError, ValueError):
            # год 0001 или 9999 из подделанной ссылки — отказ, а не голая 500 (ревью C5-2)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid custom period')
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown period')


@dataclass(frozen=True)
class _OwnerRules:
    """Тарифы по правилам владельца. Team — бесплатный тариф, который НЕ пробный: голый `is_free` нельзя, у
    «Пробного» цены тоже нулевые (`{"5": 0}` на боевом). Платный — ни бесплатный, ни пробный (как «Пользователи»)."""

    team_tariff_ids: tuple[int, ...]
    not_paid_tariff_ids: tuple[int, ...]

    @property
    def people(self):
        """«Человек» для чисел людей: не удалён и не стенд (как письмо и «Пользователи») и не друг из Team — ни
        одной подписки на Team, ни живой, ни прошлой."""
        if not self.team_tariff_ids:
            return operational_person_clause()
        # своя копия таблицы подписок: внешний запрос сам может идти по подпискам, и без неё подзапрос
        # сросся бы с ним вместо того, чтобы искать ЛЮБУЮ подписку человека на Team
        team_subscription = aliased(Subscription)
        in_team = select(team_subscription.id).where(
            team_subscription.user_id == User.id, team_subscription.tariff_id.in_(self.team_tariff_ids)
        )
        return and_(operational_person_clause(), ~in_team.exists())

    @property
    def paid_tariff(self):
        if not self.not_paid_tariff_ids:
            return true()
        return or_(Subscription.tariff_id.is_(None), Subscription.tariff_id.not_in(self.not_paid_tariff_ids))


async def _owner_rules(db: AsyncSession) -> _OwnerRules:
    tariffs = await get_all_tariffs(db, include_inactive=True)
    return _OwnerRules(
        team_tariff_ids=tuple(t.id for t in tariffs if t.is_free and not t.is_trial_available),
        not_paid_tariff_ids=tuple(t.id for t in tariffs if t.is_free or t.is_trial_available),
    )


@dataclass(frozen=True)
class _Purchase:
    id: int
    user_id: int
    at: datetime
    amount_kopeks: int
    is_addon: bool


async def _payer_purchases(db: AsyncSession, rules: _OwnerRules) -> tuple[list[_Purchase], dict[int, _Purchase]]:
    """Покупки подписок и докупки людей, плативших деньгами, — за всю историю, и первая покупка подписки каждого
    (по времени, затем по номеру). Проводки те же, что «Купили» утреннего письма: `subscription_payment` ≠ 0,
    докупка — по описанию. Покупка на бонусы у того, кто деньгами не платил, — не продажа."""
    rows = (
        await db.execute(
            select(
                Transaction.id,
                Transaction.user_id,
                Transaction.created_at,
                func.abs(Transaction.amount_kopeks),
                addon_description_clause(Transaction.description),
            )
            .join(User, User.id == Transaction.user_id)
            .where(
                Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                Transaction.is_completed == true(),
                Transaction.amount_kopeks != 0,
                rules.people,
            )
        )
    ).all()
    payers = await real_payment_user_ids(db, {row[1] for row in rows})
    purchases = sorted(
        (
            _Purchase(int(row[0]), int(row[1]), row[2], int(row[3] or 0), bool(row[4]))
            for row in rows
            if row[1] in payers
        ),
        key=lambda purchase: (purchase.at, purchase.id),
    )
    first: dict[int, _Purchase] = {}
    for purchase in purchases:
        if not purchase.is_addon:
            first.setdefault(purchase.user_id, purchase)
    return purchases, first


async def _trial_starts(db: AsyncSession) -> dict[int, datetime]:
    """Начало пробного по человеку. Покупка переписывает строку подписки (флаг и тариф — урок ОТЧ-7), поэтому
    главный признак — событие активации, оно переживает покупку; строка пробного тарифа страхует тех, у кого
    события нет. Пробный из чат-админки события не пишет — после покупки он «сразу без пробного» (предел, L1-5)."""
    queries = [
        select(SubscriptionEvent.user_id, func.min(SubscriptionEvent.occurred_at))
        .where(SubscriptionEvent.event_type == 'activation', SubscriptionEvent.message == 'Trial activation')
        .group_by(SubscriptionEvent.user_id)
    ]
    trial_tariff = await get_trial_tariff(db)
    if trial_tariff is not None:
        queries.append(
            select(Subscription.user_id, func.min(Subscription.created_at))
            .where(Subscription.tariff_id == trial_tariff.id, Subscription.status != SubscriptionStatus.PENDING.value)
            .group_by(Subscription.user_id)
        )
    starts: dict[int, datetime] = {}
    for query in queries:
        for user_id, started_at in (await db.execute(query)).all():
            if started_at is not None and (user_id not in starts or started_at < starts[user_id]):
                starts[user_id] = started_at
    return starts


async def _live_trial_user_ids(db: AsyncSession, now: datetime) -> set[int]:
    trial_tariff = await get_trial_tariff(db)
    if trial_tariff is None:
        return set()
    rows = await db.execute(
        select(Subscription.user_id).where(
            Subscription.tariff_id == trial_tariff.id,
            Subscription.is_trial.is_(True),
            Subscription.status.in_(sorted(ALIVE_SUBSCRIPTION_STATUSES)),
            Subscription.end_date > now,
        )
    )
    return set(rows.scalars().all())


def _money_in_filter():
    """«Пришло живых денег» — ровно как в утреннем письме (`reporting_service._collect_period_stats`): пополнения
    и оплаты сразу за подписку платёжной системой, без реферальных пометок, ВСЕ аккаунты — как выписка Platega
    (решение владельца 20.09.2026: со стендами и друзьями)."""
    return and_(
        Transaction.type.in_((TransactionType.DEPOSIT.value, TransactionType.PROVIDER_RECEIPT.value)),
        Transaction.is_completed == true(),
        Transaction.amount_kopeks != 0,
        Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
        reporting_service._exclude_referral_deposits_condition(),
    )


async def _money_in(db: AsyncSession, start: datetime, end: datetime) -> dict[str, int]:
    rows = (
        await db.execute(
            select(
                Transaction.type,
                func.count(Transaction.id),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0),
            )
            .join(User, User.id == Transaction.user_id)
            .where(_money_in_filter(), Transaction.created_at >= start, Transaction.created_at < end)
            .group_by(Transaction.type)
        )
    ).all()
    counts = {row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in rows}
    deposits = counts.get(TransactionType.DEPOSIT.value, (0, 0))
    receipts = counts.get(TransactionType.PROVIDER_RECEIPT.value, (0, 0))
    return {'deposits': deposits[0], 'receipts': receipts[0], 'kopeks': deposits[1] + receipts[1]}


async def dashboard_money(db: AsyncSession, now: datetime) -> dict:
    """Деньги для экрана «Статистика» (СП-1б): те же проводки, что «Пришло живых денег» (как выписка Platega), по
    суткам и месяцам Москвы — сегодня, этот месяц, всё время, 30 дней, все месяцы. Проводок с деньгами единицы в
    день, поэтому раскладываем в Python, а не часовыми поясами базы: одно и то же и на боевом, и в SQLite тестов."""
    rows = (
        await db.execute(
            select(Transaction.created_at, func.abs(Transaction.amount_kopeks))
            .join(User, User.id == Transaction.user_id)
            .where(_money_in_filter(), Transaction.created_at < now)
        )
    ).all()
    today = now.astimezone(_MSK).date()
    by_day = {today - timedelta(days=offset): 0 for offset in range(29, -1, -1)}
    by_month: dict[date, int] = {}
    for created_at, kopeks in rows:
        at = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        day = at.astimezone(_MSK).date()
        by_month[day.replace(day=1)] = by_month.get(day.replace(day=1), 0) + int(kopeks)
        if day in by_day:
            by_day[day] += int(kopeks)
    months = []
    month = min(by_month, default=today.replace(day=1))
    while month <= today:
        # месяц без денег — строка с нулём, а не дыра в ряду
        months.append({'month': month.strftime('%Y-%m'), 'kopeks': by_month.get(month, 0)})
        month = (month + timedelta(days=32)).replace(day=1)
    return {
        'today_kopeks': by_day[today],
        'month_kopeks': by_month.get(today.replace(day=1), 0),
        'total_kopeks': sum(by_month.values()),
        'days': [{'date': day.isoformat(), 'kopeks': kopeks} for day, kopeks in by_day.items()],
        'months': months,
    }


def _share_pct(part: int, whole: int) -> int | None:
    """Доля целым процентом, половина — вверх; нечего делить — `None` (на экране «—»), а не ноль."""
    return int(100 * part / whole + 0.5) if whole > 0 else None


async def referral_period_totals(db: AsyncSession, now: datetime) -> dict:
    """Итоги под «Топом рефералов» (РЕФ-2.4б): начислено ВСЕМ пригласившим за сегодня, 7 суток и календарный месяц
    Москвы до «сейчас» — тем же счётчиком, что плитка «Начислено пригласившим»: «Этот месяц» под «Топом» и плитка —
    одно число (прежде экран складывал десять строк вкладки по скользящим суткам UTC — находка D-3)."""
    today = now.astimezone(_MSK).date()
    starts = {
        'today_kopeks': _msk_midnight(today),
        'week_kopeks': _msk_midnight(today - timedelta(days=6)),
        'month_kopeks': _msk_midnight(today.replace(day=1)),
    }
    return {key: await reporting_service.referral_rewards_kopeks(db, start, now) for key, start in starts.items()}


async def dashboard_referrals(db: AsyncSession, now: datetime) -> dict:
    """«Приглашения» на «Статистике» (РЕФ-2, 30.09.2026): каждый месяц Москвы считает ТОТ ЖЕ счётчик, что утреннее
    письмо (`reporting_service.referral_numbers`), — письмо и экран не расходятся (мина NW). Каждое событие — в своём
    месяце (решение владельца 29.09). Месяц без событий — строка с нулями; текущий месяц — до «сейчас». Плитки экран
    берёт из последней строки: одно число из одного места. Доли: «пришли» — от всех новых людей месяца (Team и стенды
    не люди, как на экране продаж), «деньги от приглашённых» — от денег месяца как в выписке Platega (со стендами)."""
    today = now.astimezone(_MSK).date()
    this_month = today.replace(day=1)
    month = this_month
    first = await reporting_service.referral_first_arrival(db)
    if first is not None:
        first = first if first.tzinfo else first.replace(tzinfo=UTC)
        month = min(month, first.astimezone(_MSK).date().replace(day=1))
    months = []
    while month <= this_month:
        next_month = (month + timedelta(days=32)).replace(day=1)
        numbers = await reporting_service.referral_numbers(
            db, _msk_midnight(month), min(_msk_midnight(next_month), now)
        )
        months.append(
            {
                'month': month.strftime('%Y-%m'),
                **{key: numbers[key] for key in ('came', 'trial', 'paid_first', 'money_kopeks', 'rewards_kopeks')},
            }
        )
        month = next_month
    month_start = _msk_midnight(this_month)
    rules = await _owner_rules(db)
    new_people = int(
        (
            await db.execute(
                select(func.count(User.id)).where(rules.people, User.created_at >= month_start, User.created_at < now)
            )
        ).scalar()
        or 0
    )
    money_month = (await _money_in(db, month_start, now))['kopeks']
    current = months[-1]
    return {
        'months': months,
        'new_people_month': new_people,
        'money_month_kopeks': money_month,
        'came_pct': _share_pct(current['came'], new_people),
        'money_pct': _share_pct(current['money_kopeks'], money_month),
    }


async def _subscription_people(db: AsyncSession, rules: _OwnerRules, *conditions) -> list:
    """Живые люди, плательщики, с подпиской на платном тарифе (не пробной) под условием — по строке на человека,
    с ближайшим по времени концом. Одно определение для числа на плитке и для списка под ней."""
    rows = (
        await db.execute(
            select(
                User.id,
                User.first_name,
                User.last_name,
                User.username,
                User.telegram_id,
                User.balance_kopeks,
                Subscription.end_date,
                Subscription.autopay_enabled,
                Tariff.name,
            )
            .join(User, User.id == Subscription.user_id)
            .join(Tariff, Tariff.id == Subscription.tariff_id, isouter=True)
            .where(Subscription.is_trial.is_not(True), rules.people, rules.paid_tariff, *conditions)
            .order_by(Subscription.end_date)
        )
    ).all()
    payers = await real_payment_user_ids(db, {row[0] for row in rows})
    people: dict[int, object] = {}
    for row in rows:
        if row[0] in payers:
            people.setdefault(row[0], row)
    return list(people.values())


async def _not_renewed(db: AsyncSession, rules: _OwnerRules, start: datetime, end: datetime) -> list:
    """«Не продлили» — как «Платная подписка закончилась и не продлена» письма: срок кончился в окне. Продление
    уводит срок вперёд, и такой человек сюда не попадает. Свежие — сверху."""
    rows = await _subscription_people(db, rules, Subscription.end_date >= start, Subscription.end_date < end)
    return sorted(rows, key=lambda row: row[6], reverse=True)


async def _ending_soon(db: AsyncSession, rules: _OwnerRules, now: datetime) -> list:
    """«Кончится в ближайшие 7 дней» — живая платная подписка, срок в `(сейчас, сейчас + 7 суток]`."""
    return await _subscription_people(
        db,
        rules,
        Subscription.status.in_(sorted(ALIVE_SUBSCRIPTION_STATUSES)),
        Subscription.end_date > now,
        Subscription.end_date <= now + timedelta(days=7),
    )


async def owner_people_tiles(db: AsyncSession, now: datetime) -> dict[str, int]:
    """Плитки верха панели администратора: «Платят / На пробном» — функцией экрана «Пользователи», «+N сегодня» —
    новые покупатели с 00:00 МСК тем же определением первой покупки, что экран продаж (ревью L4-7)."""
    people_now = await count_trial_and_paying_users(db)
    _, first = await _payer_purchases(db, await _owner_rules(db))
    today_start = _msk_midnight(now.astimezone(_MSK).date())
    return {
        'on_trial': int(people_now.get('on_trial') or 0),
        'paying': int(people_now.get('paying') or 0),
        'new_buyers_today': sum(1 for purchase in first.values() if today_start <= purchase.at <= now),
    }


# ============ Summary Schemas ============


class SalesSummary(BaseModel):
    """Summary stats for the top cards."""

    total_revenue_kopeks: int
    manual_topup_kopeks: int
    active_subscriptions: int
    active_trials: int
    new_trials: int
    new_paid_subscriptions: int
    expired_subscriptions: int
    trial_to_paid_conversion: float
    renewals_count: int
    addon_revenue_kopeks: int


# ============ Summary Endpoint ============


@router.get('/summary', response_model=SalesSummary)
async def get_sales_summary(
    days: int | None = Query(default=30, description='Preset period in days (7, 30, 90, 0=all)'),
    start_date: str | None = Query(default=None, description='Custom start date ISO format'),
    end_date: str | None = Query(default=None, description='Custom end date ISO format'),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesSummary:
    """Get summary statistics for sales dashboard cards."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        # Total revenue (deposits + direct subscription payments with real payment methods)
        revenue_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                    Transaction.is_completed == True,
                    Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        total_revenue = revenue_result.scalar() or 0

        # Gateway-funded gifts never create a Transaction (the recipient "didn't
        # pay"), so the buyer's real payment was otherwise invisible to revenue.
        # Count it from GuestPurchase. Balance-funded gifts carry payment_method
        # 'balance' and are excluded here — they're already counted via the deposit
        # that funded the balance.
        gift_revenue_result = await db.execute(
            select(func.coalesce(func.sum(GuestPurchase.amount_kopeks), 0)).where(
                and_(
                    GuestPurchase.is_gift.is_(True),
                    GuestPurchase.payment_method.in_(REAL_PAYMENT_METHODS),
                    GuestPurchase.paid_at >= period_start,
                    GuestPurchase.paid_at <= period_end,
                )
            )
        )
        total_revenue += gift_revenue_result.scalar() or 0

        # Manual top-ups by admins
        manual_topup_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type == TransactionType.DEPOSIT.value,
                    Transaction.is_completed == True,
                    Transaction.payment_method == PaymentMethod.MANUAL.value,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        manual_topup = manual_topup_result.scalar() or 0

        # Consolidated subscription counts: active paid, active trial, new trials in period
        sub_counts_result = await db.execute(
            select(
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.status == SubscriptionStatus.ACTIVE.value, Subscription.is_trial.is_(False)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('active_paid'),
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.status == SubscriptionStatus.ACTIVE.value, Subscription.is_trial.is_(True)
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('active_trial'),
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.is_trial.is_(True),
                                Subscription.created_at >= period_start,
                                Subscription.created_at <= period_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('new_trials'),
                # New PAID subscriptions started in the period.
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.is_trial.is_(False),
                                Subscription.created_at >= period_start,
                                Subscription.created_at <= period_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('new_paid'),
                # Paid subscriptions that ENDED in the period (for net active growth).
                func.sum(
                    case(
                        (
                            and_(
                                Subscription.is_trial.is_(False),
                                Subscription.end_date >= period_start,
                                Subscription.end_date <= period_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label('expired_paid'),
            )
        )
        row = sub_counts_result.one()
        active_subs = row.active_paid or 0
        active_trials = row.active_trial or 0
        new_trials = row.new_trials or 0
        new_paid_subs = row.new_paid or 0
        expired_paid_subs = row.expired_paid or 0

        # Trial-to-paid conversion in period
        # Method 1: SubscriptionConversion records (only created by some purchase flows)
        conversions_result = await db.execute(
            select(func.count(SubscriptionConversion.id)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                )
            )
        )
        conversion_records = conversions_result.scalar() or 0

        # Method 2: Users registered in period who have paid (catches all purchase flows)
        converted_users_result = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                    User.has_had_paid_subscription.is_(True),
                )
            )
        )
        converted_users = converted_users_result.scalar() or 0

        # Use the higher count to catch conversions from all purchase flows
        conversions = max(conversion_records, converted_users)

        # new_trials only counts REMAINING trials (is_trial=True), but converted users
        # had is_trial flipped to False. Add conversions back to get total trial starters.
        total_trial_starters = new_trials + conversions
        conversion_rate = (
            min(round((conversions / total_trial_starters * 100), 1), 100.0) if total_trial_starters > 0 else 0.0
        )

        # Renewals count
        renewals_subquery = (
            select(Transaction.user_id)
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at < period_start,
                )
            )
            .distinct()
        )
        renewals_result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    # A renewal is a repeat subscription payment — NOT a traffic/device
                    # top-up (those are add-ons with their own tab); exclude them so
                    # renewals don't double-count add-on purchases.
                    ~addon_description_clause(Transaction.description),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                    Transaction.user_id.in_(renewals_subquery),
                )
            )
        )
        renewals_count = renewals_result.scalar() or 0

        # Add-on revenue for the summary card = ALL add-ons (traffic + devices),
        # so "Доп. услуги" matches the sum of the Add-ons tab. (Previously this was
        # traffic-only and silently dropped device revenue.)
        addon_revenue_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    addon_description_clause(Transaction.description),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        addon_revenue = addon_revenue_result.scalar() or 0

        return SalesSummary(
            # Gateway revenue only — manual admin top-ups are reported separately
            # (manual_topup_kopeks) so the headline "Доход" isn't muddied by them.
            total_revenue_kopeks=total_revenue,
            manual_topup_kopeks=manual_topup,
            active_subscriptions=active_subs,
            active_trials=active_trials,
            new_trials=new_trials,
            new_paid_subscriptions=new_paid_subs,
            expired_subscriptions=expired_paid_subs,
            trial_to_paid_conversion=conversion_rate,
            renewals_count=renewals_count,
            addon_revenue_kopeks=addon_revenue,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get sales summary', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load sales summary',
        )


# ============ Trials Schemas ============


class ProviderBreakdownItem(BaseModel):
    provider: str
    count: int


class DailyTrialItem(BaseModel):
    date: str
    registrations: int
    trials: int


class TrialsStatsResponse(BaseModel):
    total_trials: int
    total_registrations: int
    conversion_rate: float
    avg_trial_duration_days: float
    by_provider: list[ProviderBreakdownItem]
    daily: list[DailyTrialItem]


# ============ Trials Endpoint ============


@router.get('/trials', response_model=TrialsStatsResponse)
async def get_trials_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> TrialsStatsResponse:
    """Get trial registration statistics with provider breakdown."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        total_result = await db.execute(
            select(func.count(Subscription.id)).where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
        )
        total_trials = total_result.scalar() or 0

        # Conversion: SubscriptionConversion records + fallback to has_had_paid_subscription
        conversions_result = await db.execute(
            select(func.count(SubscriptionConversion.id)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                )
            )
        )
        conversion_records = conversions_result.scalar() or 0

        converted_users_result = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                    User.has_had_paid_subscription.is_(True),
                )
            )
        )
        converted_users = converted_users_result.scalar() or 0
        conversions = max(conversion_records, converted_users)

        # total_trials only counts remaining is_trial=True; add conversions for total starters
        total_trial_starters = total_trials + conversions
        conversion_rate = (
            min(round((conversions / total_trial_starters * 100), 1), 100.0) if total_trial_starters > 0 else 0.0
        )

        avg_duration_result = await db.execute(
            select(func.avg(SubscriptionConversion.trial_duration_days)).where(
                and_(
                    SubscriptionConversion.converted_at >= period_start,
                    SubscriptionConversion.converted_at <= period_end,
                    SubscriptionConversion.trial_duration_days.isnot(None),
                )
            )
        )
        avg_duration = float(avg_duration_result.scalar() or 0.0)

        provider_case = case(
            (User.vk_id.isnot(None), 'vk'),
            (User.yandex_id.isnot(None), 'yandex'),
            (User.google_id.isnot(None), 'google'),
            (User.discord_id.isnot(None), 'discord'),
            (User.auth_type == 'email', 'email'),
            else_='telegram',
        )
        provider_query = await db.execute(
            select(
                provider_case.label('provider'),
                func.count(Subscription.id).label('count'),
            )
            .join(User, Subscription.user_id == User.id)
            .where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
            .group_by(provider_case)
        )
        by_provider = [ProviderBreakdownItem(provider=row.provider, count=row.count) for row in provider_query]

        # Total registrations (all user signups in period)
        reg_total_result = await db.execute(
            select(func.count(User.id)).where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                )
            )
        )
        total_registrations = reg_total_result.scalar() or 0

        # Daily registrations (user signups per day)
        daily_reg_query = await db.execute(
            select(
                func.date(User.created_at).label('date'),
                func.count(User.id).label('count'),
            )
            .where(
                and_(
                    User.created_at >= period_start,
                    User.created_at <= period_end,
                )
            )
            .group_by(func.date(User.created_at))
            .order_by(func.date(User.created_at))
        )
        reg_by_date: dict[str, int] = {}
        for row in daily_reg_query:
            date_str = row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date)
            reg_by_date[date_str] = row.count

        # Daily trials (trial subscriptions per day)
        daily_trial_query = await db.execute(
            select(
                func.date(Subscription.created_at).label('date'),
                func.count(Subscription.id).label('count'),
            )
            .where(
                and_(
                    Subscription.is_trial == True,
                    Subscription.created_at >= period_start,
                    Subscription.created_at <= period_end,
                )
            )
            .group_by(func.date(Subscription.created_at))
            .order_by(func.date(Subscription.created_at))
        )
        trial_by_date: dict[str, int] = {}
        for row in daily_trial_query:
            date_str = row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date)
            trial_by_date[date_str] = row.count

        # Merge both series by date union
        all_dates = sorted(set(reg_by_date.keys()) | set(trial_by_date.keys()))
        daily = [
            DailyTrialItem(
                date=d,
                registrations=reg_by_date.get(d, 0),
                trials=trial_by_date.get(d, 0),
            )
            for d in all_dates
        ]

        return TrialsStatsResponse(
            total_trials=total_trials,
            total_registrations=total_registrations,
            conversion_rate=conversion_rate,
            avg_trial_duration_days=round(avg_duration, 1),
            by_provider=by_provider,
            daily=daily,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get trials stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load trials statistics',
        )


# ============ Sales Schemas ============


class SalesByTariffItem(BaseModel):
    tariff_id: int
    tariff_name: str
    count: int


class SalesByPeriodItem(BaseModel):
    period_days: int
    count: int


class DailySalesItem(BaseModel):
    date: str
    count: int
    revenue_kopeks: int


class DailyTariffSalesItem(BaseModel):
    date: str
    tariff_name: str
    count: int


class SalesStatsResponse(BaseModel):
    total_sales: int
    total_revenue_kopeks: int
    avg_order_kopeks: int
    top_tariff_name: str
    by_tariff: list[SalesByTariffItem]
    by_period: list[SalesByPeriodItem]
    daily: list[DailySalesItem]
    daily_by_tariff: list[DailyTariffSalesItem]


# ============ Sales Endpoint ============


@router.get('/subscriptions', response_model=SalesStatsResponse)
async def get_sales_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesStatsResponse:
    """Get subscription sales statistics."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        base_filter = and_(
            Subscription.is_trial == False,
            Subscription.created_at >= period_start,
            Subscription.created_at <= period_end,
        )

        totals_result = await db.execute(select(func.count(Subscription.id).label('count')).where(base_filter))
        totals = totals_result.one()
        total_sales = totals.count

        # Revenue and the number of payments that make it up, so the average is
        # money-per-payment. (Previously divided by the count of *new* subscriptions,
        # while the sum included renewals/add-ons too — that inflated the average.)
        revenue_result = await db.execute(
            select(
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                func.count(Transaction.id).label('payments'),
            ).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        rev_row = revenue_result.one()
        total_revenue = rev_row.revenue or 0
        sub_payment_count = rev_row.payments or 0
        avg_order = total_revenue // sub_payment_count if sub_payment_count > 0 else 0

        by_tariff_query = await db.execute(
            select(
                Tariff.id.label('tariff_id'),
                Tariff.name.label('tariff_name'),
                func.count(Subscription.id).label('count'),
            )
            .join(Tariff, Subscription.tariff_id == Tariff.id, isouter=True)
            .where(base_filter)
            .group_by(Tariff.id, Tariff.name)
            .order_by(func.count(Subscription.id).desc())
        )
        by_tariff = []
        top_tariff_name = '-'
        for i, row in enumerate(by_tariff_query):
            name = row.tariff_name or 'Unknown'
            by_tariff.append(
                SalesByTariffItem(
                    tariff_id=row.tariff_id or 0,
                    tariff_name=name,
                    count=row.count,
                )
            )
            if i == 0:
                top_tariff_name = name

        # Use epoch extraction / 86400 for correct total days (EXTRACT(day) only returns the day component)
        period_days_expr = cast(
            func.extract('epoch', Subscription.end_date - Subscription.start_date) / 86400,
            SAInteger,
        )
        by_period_query = await db.execute(
            select(
                period_days_expr.label('period_days'),
                func.count(Subscription.id).label('count'),
            )
            .where(base_filter)
            .group_by(period_days_expr)
            .order_by(period_days_expr)
        )
        by_period = [
            SalesByPeriodItem(period_days=int(row.period_days or 0), count=row.count) for row in by_period_query
        ]

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
            )
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailySalesItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                revenue_kopeks=row.revenue,
            )
            for row in daily_query
        ]

        # Daily sales grouped by tariff
        tariff_name_col = func.coalesce(Tariff.name, 'Unknown')
        daily_by_tariff_query = await db.execute(
            select(
                func.date(Subscription.created_at).label('date'),
                tariff_name_col.label('tariff_name'),
                func.count(Subscription.id).label('count'),
            )
            .join(Tariff, Subscription.tariff_id == Tariff.id, isouter=True)
            .where(base_filter)
            .group_by(func.date(Subscription.created_at), tariff_name_col)
            .order_by(func.date(Subscription.created_at), tariff_name_col)
        )
        daily_by_tariff = [
            DailyTariffSalesItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                tariff_name=row.tariff_name,
                count=row.count,
            )
            for row in daily_by_tariff_query
        ]

        return SalesStatsResponse(
            total_sales=total_sales,
            total_revenue_kopeks=total_revenue,
            avg_order_kopeks=avg_order,
            top_tariff_name=top_tariff_name,
            by_tariff=by_tariff,
            by_period=by_period,
            daily=daily,
            daily_by_tariff=daily_by_tariff,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get sales stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load sales statistics',
        )


# ============ Renewals Schemas ============


class DailyRenewalItem(BaseModel):
    date: str
    count: int


class RenewalPeriodStats(BaseModel):
    count: int
    revenue_kopeks: int


class RenewalChange(BaseModel):
    absolute: int
    percent: float
    trend: str


class RenewalsStatsResponse(BaseModel):
    total_renewals: int
    total_revenue_kopeks: int
    renewal_rate: float
    current_period: RenewalPeriodStats
    previous_period: RenewalPeriodStats
    change: RenewalChange
    daily: list[DailyRenewalItem]


# ============ Renewals Endpoint ============


@router.get('/renewals', response_model=RenewalsStatsResponse)
async def get_renewals_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RenewalsStatsResponse:
    """Get renewal statistics with period comparison."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)
        is_all_time = days is not None and days == 0

        # Renewals must NOT include traffic/device top-ups (they share the
        # SUBSCRIPTION_PAYMENT type but belong to the Add-ons tab).
        not_addon = ~addon_description_clause(Transaction.description)

        if is_all_time:
            # For "all time": renewals = users with more than 1 real subscription payment
            repeat_users_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        not_addon,
                    )
                )
                .group_by(Transaction.user_id)
                .having(func.count(Transaction.id) > 1)
            )
            existing_users_subquery = repeat_users_subquery

            current_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        not_addon,
                        Transaction.user_id.in_(repeat_users_subquery),
                    )
                )
            )
            current = current_result.one()
            current_count = current.count
            current_revenue = current.revenue

            # No meaningful previous period for "all time"
            prev = type('Row', (), {'count': 0, 'revenue': 0})()
        else:
            period_length = period_end - period_start
            prev_start = period_start - period_length
            prev_end = period_start

            existing_users_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at < period_start,
                    )
                )
                .distinct()
            )

            current_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        not_addon,
                        Transaction.created_at >= period_start,
                        Transaction.created_at <= period_end,
                        Transaction.user_id.in_(existing_users_subquery),
                    )
                )
            )
            current = current_result.one()
            current_count = current.count
            current_revenue = current.revenue

            prev_existing_subquery = (
                select(Transaction.user_id)
                .where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        Transaction.created_at < prev_start,
                    )
                )
                .distinct()
            )
            prev_result = await db.execute(
                select(
                    func.count(Transaction.id).label('count'),
                    func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
                ).where(
                    and_(
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed == True,
                        not_addon,
                        Transaction.created_at >= prev_start,
                        Transaction.created_at <= prev_end,
                        Transaction.user_id.in_(prev_existing_subquery),
                    )
                )
            )
            prev = prev_result.one()

        if prev.count > 0:
            change_percent = round(((current_count - prev.count) / prev.count) * 100, 1)
        else:
            change_percent = 100.0 if current_count > 0 else 0.0

        if change_percent > 0:
            trend = 'up'
        elif change_percent < 0:
            trend = 'down'
        else:
            trend = 'stable'

        # Denominator for renewal_rate excludes add-ons too, so the rate is
        # renewals / (new + renewals), not diluted by traffic/device top-ups.
        total_sub_payments_result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    not_addon,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        total_sub_payments = total_sub_payments_result.scalar() or 0
        renewal_rate = round((current_count / total_sub_payments * 100), 1) if total_sub_payments > 0 else 0.0

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
            )
            .where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    not_addon,
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                    Transaction.user_id.in_(existing_users_subquery),
                )
            )
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailyRenewalItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
            )
            for row in daily_query
        ]

        return RenewalsStatsResponse(
            total_renewals=current_count,
            total_revenue_kopeks=current_revenue,
            renewal_rate=renewal_rate,
            current_period=RenewalPeriodStats(count=current_count, revenue_kopeks=current_revenue),
            previous_period=RenewalPeriodStats(count=prev.count, revenue_kopeks=prev.revenue),
            change=RenewalChange(
                absolute=current_count - prev.count,
                percent=change_percent,
                trend=trend,
            ),
            daily=daily,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get renewals stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load renewals statistics',
        )


# ============ Add-ons Schemas ============


class AddonByPackageItem(BaseModel):
    traffic_gb: int
    count: int


class DailyAddonItem(BaseModel):
    date: str
    count: int
    total_gb: int


class DailyDeviceItem(BaseModel):
    date: str
    count: int


class AddonsStatsResponse(BaseModel):
    total_purchases: int
    total_gb_purchased: int
    addon_revenue_kopeks: int
    device_purchases: int
    device_revenue_kopeks: int
    by_package: list[AddonByPackageItem]
    daily: list[DailyAddonItem]
    daily_devices: list[DailyDeviceItem]


# ============ Add-ons Endpoint ============


@router.get('/addons', response_model=AddonsStatsResponse)
async def get_addons_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AddonsStatsResponse:
    """Get add-on purchase statistics."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        base_filter = and_(
            TrafficPurchase.created_at >= period_start,
            TrafficPurchase.created_at <= period_end,
        )

        totals_result = await db.execute(
            select(
                func.count(TrafficPurchase.id).label('count'),
                func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0).label('total_gb'),
            ).where(base_filter)
        )
        totals = totals_result.one()

        addon_revenue_result = await db.execute(
            select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
                and_(
                    Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                    Transaction.is_completed == True,
                    traffic_addon_clause(Transaction.description),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        addon_revenue = addon_revenue_result.scalar() or 0

        by_package_query = await db.execute(
            select(
                TrafficPurchase.traffic_gb.label('traffic_gb'),
                func.count(TrafficPurchase.id).label('count'),
            )
            .where(base_filter)
            .group_by(TrafficPurchase.traffic_gb)
            .order_by(TrafficPurchase.traffic_gb)
        )
        by_package = [AddonByPackageItem(traffic_gb=row.traffic_gb, count=row.count) for row in by_package_query]

        daily_query = await db.execute(
            select(
                func.date(TrafficPurchase.created_at).label('date'),
                func.count(TrafficPurchase.id).label('count'),
                func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0).label('total_gb'),
            )
            .where(base_filter)
            .group_by(func.date(TrafficPurchase.created_at))
            .order_by(func.date(TrafficPurchase.created_at))
        )
        daily = [
            DailyAddonItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                total_gb=row.total_gb,
            )
            for row in daily_query
        ]

        # Device purchases (transactions whose description looks like a devices add-on)
        device_filter = and_(
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed == True,
            device_addon_clause(Transaction.description),
            Transaction.created_at >= period_start,
            Transaction.created_at <= period_end,
        )
        device_result = await db.execute(
            select(
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('revenue'),
            ).where(device_filter)
        )
        device_row = device_result.one()

        # Daily device purchases
        daily_device_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
            )
            .where(device_filter)
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily_devices = [
            DailyDeviceItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
            )
            for row in daily_device_query
        ]

        return AddonsStatsResponse(
            total_purchases=totals.count,
            total_gb_purchased=totals.total_gb,
            addon_revenue_kopeks=addon_revenue,
            device_purchases=device_row.count,
            device_revenue_kopeks=device_row.revenue,
            by_package=by_package,
            daily=daily,
            daily_devices=daily_devices,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get addons stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load add-ons statistics',
        )


# ============ Deposits Schemas ============


class DepositByMethodItem(BaseModel):
    method: str
    count: int
    amount_kopeks: int


class DailyDepositItem(BaseModel):
    date: str
    count: int
    amount_kopeks: int


class DailyDepositByMethodItem(BaseModel):
    date: str
    method: str
    amount_kopeks: int


class DepositsStatsResponse(BaseModel):
    total_deposits: int
    total_amount_kopeks: int
    avg_deposit_kopeks: int
    by_method: list[DepositByMethodItem]
    daily: list[DailyDepositItem]
    daily_by_method: list[DailyDepositByMethodItem]


# ============ Deposits Endpoint ============


@router.get('/deposits', response_model=DepositsStatsResponse)
async def get_deposits_stats(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> DepositsStatsResponse:
    """Get deposit statistics with payment method breakdown."""
    try:
        period_start, period_end = _parse_period(days, start_date, end_date)

        methods_with_manual = [*REAL_PAYMENT_METHODS, PaymentMethod.MANUAL.value]
        base_filter = and_(
            Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
            Transaction.is_completed == True,
            Transaction.payment_method.in_(methods_with_manual),
            Transaction.created_at >= period_start,
            Transaction.created_at <= period_end,
        )

        totals_result = await db.execute(
            select(
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('amount'),
            ).where(base_filter)
        )
        totals = totals_result.one()
        total_deposits = totals.count
        total_amount = totals.amount
        avg_deposit = total_amount // total_deposits if total_deposits > 0 else 0

        by_method_query = await db.execute(
            select(
                Transaction.payment_method.label('method'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(Transaction.payment_method)
            .order_by(func.sum(func.abs(Transaction.amount_kopeks)).desc())
        )
        by_method = [
            DepositByMethodItem(method=row.method or 'unknown', count=row.count, amount_kopeks=row.amount)
            for row in by_method_query
        ]

        daily_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                func.count(Transaction.id).label('count'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        daily = [
            DailyDepositItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                count=row.count,
                amount_kopeks=row.amount,
            )
            for row in daily_query
        ]

        # Daily deposits grouped by payment method
        # base_filter already excludes NULLs via .in_(methods_with_manual), no coalesce needed
        daily_by_method_query = await db.execute(
            select(
                func.date(Transaction.created_at).label('date'),
                Transaction.payment_method.label('method'),
                func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0).label('amount'),
            )
            .where(base_filter)
            .group_by(func.date(Transaction.created_at), Transaction.payment_method)
            .order_by(func.date(Transaction.created_at), Transaction.payment_method)
        )
        daily_by_method = [
            DailyDepositByMethodItem(
                date=row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
                method=row.method or 'unknown',
                amount_kopeks=row.amount,
            )
            for row in daily_by_method_query
        ]

        return DepositsStatsResponse(
            total_deposits=total_deposits,
            total_amount_kopeks=total_amount,
            avg_deposit_kopeks=avg_deposit,
            by_method=by_method,
            daily=daily,
            daily_by_method=daily_by_method,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get deposits stats', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load deposits statistics',
        )


# ============ Payment Health Schemas ============


class GatewaySuccessItem(BaseModel):
    method: str
    total: int
    paid: int
    success_rate: float


class PaymentHealthResponse(BaseModel):
    total_attempts: int
    total_paid: int
    success_rate: float
    failed_purchases: int
    by_gateway: list[GatewaySuccessItem]


# ============ Payment Health Endpoint ============


@router.get('/payment-health', response_model=PaymentHealthResponse)
async def get_payment_health(
    days: int | None = Query(default=30),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    # СП-1: новый экран шлёт имя кнопки — окно в сутках МСК; без него — как раньше (старый кабинет)
    period: SalesPeriod | None = Query(default=None),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> PaymentHealthResponse:
    """Payment reliability: per-gateway success-rate + failed-purchase rollbacks.

    success-rate = paid / created per gateway (rows are inserted at initiation).
    failed_purchases = internal balance rollbacks after a failed/guarded purchase
    (REFUND with no payment_method) — a signal of how often purchases error out,
    NOT money returned to customers.
    """
    if period is not None:
        window = _sales_window(period, start_date, end_date, datetime.now(UTC))
        period_start, period_end = window.start, window.end
    else:
        period_start, period_end = _parse_period(days, start_date, end_date)
    try:
        gateways = await get_gateway_success_rates(db, period_start, period_end)
        total_attempts = sum(g['total'] for g in gateways)
        total_paid = sum(g['paid'] for g in gateways)
        success_rate = round(total_paid / total_attempts * 100, 1) if total_attempts > 0 else 0.0

        failed_result = await db.execute(
            select(func.count(Transaction.id)).where(
                and_(
                    Transaction.type == TransactionType.REFUND.value,
                    Transaction.is_completed == True,
                    Transaction.payment_method.is_(None),
                    Transaction.created_at >= period_start,
                    Transaction.created_at <= period_end,
                )
            )
        )
        failed_purchases = failed_result.scalar() or 0

        return PaymentHealthResponse(
            total_attempts=total_attempts,
            total_paid=total_paid,
            success_rate=success_rate,
            failed_purchases=failed_purchases,
            by_gateway=[GatewaySuccessItem(**g) for g in gateways],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get payment health', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load payment health',
        )


# ============ СП-1: обзор продаж по правилам владельца ============


class SalesWindowInfo(BaseModel):
    start: datetime
    end: datetime
    previous_start: datetime | None = None
    previous_end: datetime | None = None


class SalesNowStats(BaseModel):
    """Сейчас — от выбранного периода не зависит."""

    paying: int
    on_trial: int
    ending_soon: int


class SalesMoneyStats(BaseModel):
    received_kopeks: int
    deposits_count: int
    receipts_count: int
    previous_received_kopeks: int | None = None
    # процент к прошлому окну честен, только если там были деньги и оно не начинается раньше первых живых денег
    previous_comparable: bool = False


class SalesPurchaseStats(BaseModel):
    count: int
    amount_kopeks: int
    first_count: int
    first_amount_kopeks: int
    first_after_trial: int
    first_direct: int
    renewal_count: int
    renewal_amount_kopeks: int
    addon_count: int
    addon_amount_kopeks: int
    previous_first_count: int | None = None
    not_renewed: int


class SalesTrialStats(BaseModel):
    """Когорта: люди, впервые открывшие бота в окне."""

    came: int
    took_trial: int
    trial_finished: int
    bought_after_trial: int


class SalesOverviewResponse(BaseModel):
    generated_at: datetime
    window: SalesWindowInfo
    now: SalesNowStats
    money: SalesMoneyStats
    purchases: SalesPurchaseStats
    trial: SalesTrialStats


async def _build_overview(db: AsyncSession, window: _SalesWindow, now: datetime) -> SalesOverviewResponse:
    rules = await _owner_rules(db)
    people_now = await count_trial_and_paying_users(db)
    ending_soon = await _ending_soon(db, rules, now)
    money = await _money_in(db, window.start, window.end)
    previous_kopeks, comparable = None, False
    if window.previous_start is not None and window.previous_end is not None:
        previous_kopeks = (await _money_in(db, window.previous_start, window.previous_end))['kopeks']
        first_money_at = (
            await db.execute(
                select(func.min(Transaction.created_at))
                .join(User, User.id == Transaction.user_id)
                .where(_money_in_filter())
            )
        ).scalar()
        comparable = previous_kopeks > 0 and first_money_at is not None and first_money_at <= window.previous_start

    purchases, first = await _payer_purchases(db, rules)
    trial_starts = await _trial_starts(db)

    def is_first(purchase: _Purchase) -> bool:
        return not purchase.is_addon and first[purchase.user_id].id == purchase.id

    in_window = [p for p in purchases if window.start <= p.at < window.end]
    firsts = [p for p in in_window if is_first(p)]
    renewals = [p for p in in_window if not p.is_addon and not is_first(p)]
    addons = [p for p in in_window if p.is_addon]
    after_trial = sum(1 for p in firsts if p.user_id in trial_starts and trial_starts[p.user_id] < p.at)
    previous_first = None
    if window.previous_start is not None and window.previous_end is not None:
        previous_first = sum(1 for p in first.values() if window.previous_start <= p.at < window.previous_end)
    not_renewed = await _not_renewed(db, rules, window.start, min(window.end, now))

    came = set(
        (
            await db.execute(
                select(User.id).where(User.created_at >= window.start, User.created_at < window.end, rules.people)
            )
        )
        .scalars()
        .all()
    )
    took = {user_id for user_id in came if user_id in trial_starts}
    trial_length = timedelta(days=int(settings.TRIAL_DURATION_DAYS))
    live_trial = await _live_trial_user_ids(db, now)
    # продлённый пробный ещё идёт — человек не «закончил», хотя три дня прошли (ревью C3-1, id 378 до 10.10)
    finished = {
        user_id for user_id in took if trial_starts[user_id] + trial_length <= now and user_id not in live_trial
    }
    bought = {user_id for user_id in finished if user_id in first and first[user_id].at >= trial_starts[user_id]}

    return SalesOverviewResponse(
        generated_at=now,
        window=SalesWindowInfo(
            start=window.start,
            end=window.end,
            previous_start=window.previous_start,
            previous_end=window.previous_end,
        ),
        now=SalesNowStats(
            paying=int(people_now.get('paying') or 0),
            on_trial=int(people_now.get('on_trial') or 0),
            ending_soon=len(ending_soon),
        ),
        money=SalesMoneyStats(
            received_kopeks=money['kopeks'],
            deposits_count=money['deposits'],
            receipts_count=money['receipts'],
            previous_received_kopeks=previous_kopeks,
            previous_comparable=comparable,
        ),
        purchases=SalesPurchaseStats(
            count=len(firsts) + len(renewals),
            amount_kopeks=sum(p.amount_kopeks for p in firsts + renewals),
            first_count=len(firsts),
            first_amount_kopeks=sum(p.amount_kopeks for p in firsts),
            first_after_trial=after_trial,
            first_direct=len(firsts) - after_trial,
            renewal_count=len(renewals),
            renewal_amount_kopeks=sum(p.amount_kopeks for p in renewals),
            addon_count=len(addons),
            addon_amount_kopeks=sum(p.amount_kopeks for p in addons),
            previous_first_count=previous_first,
            not_renewed=len(not_renewed),
        ),
        trial=SalesTrialStats(
            came=len(came), took_trial=len(took), trial_finished=len(finished), bought_after_trial=len(bought)
        ),
    )


@router.get('/overview', response_model=SalesOverviewResponse)
async def get_sales_overview(
    period: SalesPeriod = Query(default='this_month'),
    start_date: str | None = Query(default=None, description='Для period=custom: первый день, YYYY-MM-DD (МСК)'),
    end_date: str | None = Query(default=None, description='Для period=custom: последний день, YYYY-MM-DD (МСК)'),
    admin: User = Depends(require_permission('sales_stats:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesOverviewResponse:
    """Экран «Статистика продаж» по правилам владельца (СП-1): сейчас, деньги, покупки, пробный."""
    now = datetime.now(UTC)
    window = _sales_window(period, start_date, end_date, now)
    try:
        return await _build_overview(db, window, now)
    except Exception as e:
        logger.error('Failed to get sales overview', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load sales overview',
        )


class SalesPersonItem(BaseModel):
    user_id: int
    name: str | None = None
    username: str | None = None
    telegram_id: int | None = None
    tariff_name: str | None = None
    end_date: datetime
    autopay_enabled: bool = False
    balance_kopeks: int = 0


class SalesPeopleResponse(BaseModel):
    kind: str
    total: int
    items: list[SalesPersonItem]


async def _people_items(db: AsyncSession, kind: str, window: _SalesWindow, now: datetime) -> list[SalesPersonItem]:
    """Список под плиткой — тем же помощником, что число на ней: длина списка = число (ревью L2-7)."""
    rules = await _owner_rules(db)
    if kind == 'not_renewed':
        rows = await _not_renewed(db, rules, window.start, min(window.end, now))
    else:
        rows = await _ending_soon(db, rules, now)
    return [
        SalesPersonItem(
            user_id=row[0],
            name=' '.join(part for part in (row[1], row[2]) if part) or None,
            username=row[3],
            telegram_id=row[4],
            balance_kopeks=int(row[5] or 0),
            end_date=row[6],
            autopay_enabled=bool(row[7]),
            tariff_name=row[8],
        )
        for row in rows
    ]


@router.get('/people', response_model=SalesPeopleResponse)
async def get_sales_people(
    kind: Literal['not_renewed', 'ending_soon'],
    period: SalesPeriod = Query(default='this_month'),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    # имена, ники и балансы клиентов — только тем, кому и так видна карточка пользователя
    admin: User = Depends(require_permission('sales_stats:read', 'users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesPeopleResponse:
    """Кто за плитками «Не продлили» (за выбранный период) и «Кончится в ближайшие 7 дней» (сейчас)."""
    now = datetime.now(UTC)
    # «Кончится» считается от «сейчас» и от периода не зависит: «Свой» без дат не должен ронять этот список (C6-4)
    window = (
        _sales_window(period, start_date, end_date, now)
        if kind == 'not_renewed'
        else _SalesWindow(now, now, None, None)
    )
    try:
        items = await _people_items(db, kind, window, now)
        return SalesPeopleResponse(kind=kind, total=len(items), items=items)
    except Exception as e:
        logger.error('Failed to get sales people', kind=kind, error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load people list',
        )


class SalesAdCampaign(BaseModel):
    campaign_id: int
    name: str
    ad_spend_kopeks: int
    buyers: int
    cost_per_buyer_kopeks: int | None = None
    receipts_kopeks: int
    # все лиды моложе 7 суток (или лидов нет и кампания заведена меньше 7 суток назад) — судить рано
    fresh: bool


class SalesAdsResponse(BaseModel):
    campaigns_total: int
    campaigns_with_spend: int
    mature_spend_kopeks: int
    mature_buyers: int
    mature_cost_per_buyer_kopeks: int | None = None
    mature_receipts_kopeks: int
    fresh_spend_kopeks: int
    fresh_buyers: int
    campaigns: list[SalesAdCampaign]


async def _ads(db: AsyncSession, now: datetime) -> SalesAdsResponse:
    """Реклама за всё время — определения экрана кампаний (`get_campaign_performance`, РК-3: покупатель — первая
    оплата после первого касания, двойного счёта между кампаниями нет). Только кампании с указанным расходом.
    Свежие кампании отдельно: без этого «покупатель ≈» сам вырастет вдвое, пока они дозревают (ревью, W2-4)."""
    rows = (
        await db.execute(
            select(
                AdvertisingCampaign.id,
                AdvertisingCampaign.name,
                AdvertisingCampaign.ad_spend_kopeks,
                AdvertisingCampaign.created_at,
            ).order_by(AdvertisingCampaign.id)
        )
    ).all()
    campaigns: list[SalesAdCampaign] = []
    for campaign_id, name, spend, created_at in rows:
        # расход не указан или 0 ₽ («осознанно без расхода») — это не покупная реклама: такие покупатели
        # занизили бы «покупатель ≈» у платных кампаний (ревью C1-4)
        if not spend:
            continue
        performance = await get_campaign_performance(db, campaign_id, now=now)
        if performance is None:
            continue
        leads = int(performance['leads'] or 0)
        fresh = (
            int(performance['immature_leads_count'] or 0) == leads
            if leads
            else created_at is not None and created_at > now - timedelta(days=7)
        )
        campaigns.append(
            SalesAdCampaign(
                campaign_id=campaign_id,
                name=str(name).strip(),
                ad_spend_kopeks=int(spend),
                buyers=int(performance['paid_subscription_users_count'] or 0),
                cost_per_buyer_kopeks=performance['customer_acquisition_cost_kopeks'],
                receipts_kopeks=int(performance['confirmed_receipts_kopeks'] or 0),
                fresh=fresh,
            )
        )
    mature = [campaign for campaign in campaigns if not campaign.fresh]
    fresh_ones = [campaign for campaign in campaigns if campaign.fresh]
    mature_spend = sum(campaign.ad_spend_kopeks for campaign in mature)
    mature_buyers = sum(campaign.buyers for campaign in mature)
    return SalesAdsResponse(
        campaigns_total=len(rows),
        campaigns_with_spend=len(campaigns),
        mature_spend_kopeks=mature_spend,
        mature_buyers=mature_buyers,
        mature_cost_per_buyer_kopeks=round(mature_spend / mature_buyers) if mature_buyers else None,
        mature_receipts_kopeks=sum(campaign.receipts_kopeks for campaign in mature),
        fresh_spend_kopeks=sum(campaign.ad_spend_kopeks for campaign in fresh_ones),
        fresh_buyers=sum(campaign.buyers for campaign in fresh_ones),
        campaigns=sorted(campaigns, key=lambda campaign: (campaign.fresh, -campaign.ad_spend_kopeks)),
    )


@router.get('/ads', response_model=SalesAdsResponse)
async def get_sales_ads(
    # расход на рекламу виден тем же, кому видна статистика кампаний
    admin: User = Depends(require_permission('sales_stats:read', 'campaigns:stats')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SalesAdsResponse:
    """Реклама за всё время: расход, покупатели и «во сколько обошёлся покупатель» по кампаниям."""
    try:
        return await _ads(db, datetime.now(UTC))
    except Exception as e:
        logger.error('Failed to get sales ads', error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load ads economics',
        )

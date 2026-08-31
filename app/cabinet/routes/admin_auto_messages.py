"""Раздел кабинета «Автосообщения» — то, что бот отправляет клиентам сам.

Экран читающий: он показывает ВСЕ автоматические сообщения бота, но менять умеет
только те пять, у которых выключатель уже существует в коде
(``NotificationSettingsService._DEFAULTS``). Для остальных пятнадцати ``control``
честно говорит «настроек нет», а ``PATCH`` по ним отвечает 409 — интерфейс не
обещает того, чего не может.

🔴 Забор на деньги (``_assert_discount_is_safe``) не формальность: менять процент
может не только владелец, но и роль Marketer, а нулевая итоговая цена ведёт себя
по-разному в двух кассах — старая пропускает нулевое списание и выдаёт платную
подписку бесплатно, новая отказывается продавать вовсе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    DiscountOffer,
    MonitoringLog,
    PromoGroup,
    SentNotification,
    Subscription,
    User,
)
from app.services.notification_settings_service import NotificationSettingsService
from app.services.pricing_engine import PricingEngine

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/auto-messages', tags=['Admin Auto Messages'])


# ---------------------------------------------------------------------------
# Заборы на деньги
# ---------------------------------------------------------------------------

# Потолок скидки сообщения. Скидка промогруппы и скидка сообщения ПЕРЕМНОЖАЮТСЯ
# (PricingEngine.apply_stacked_discounts), поэтому опасно не само число, а итог.
MAX_OFFER_DISCOUNT_PERCENT = 50

# Самая дешёвая мыслимая цена — 1 ₽. Если на ней итог остаётся положительным,
# на любой реальной цене он тем более положителен.
_GUARD_REFERENCE_PRICE_KOPEKS = 100

MIN_VALID_HOURS = 1
MAX_VALID_HOURS = 168
MIN_TRIGGER_DAYS = 1
MAX_TRIGGER_DAYS = 60


async def _max_promo_group_percent(db: AsyncSession) -> int:
    """Наибольшая скидка, которую сегодня даёт хоть одна промогруппа.

    Берём максимум по трём колонкам процентов и по значениям ``period_discounts``:
    забор должен считать худший случай, а не удобный.
    """
    result = await db.execute(
        select(
            PromoGroup.server_discount_percent,
            PromoGroup.traffic_discount_percent,
            PromoGroup.device_discount_percent,
            PromoGroup.period_discounts,
        )
    )
    worst = 0
    for server_pct, traffic_pct, device_pct, period_discounts in result.all():
        worst = max(worst, int(server_pct or 0), int(traffic_pct or 0), int(device_pct or 0))
        if isinstance(period_discounts, dict):
            for value in period_discounts.values():
                try:
                    worst = max(worst, int(value))
                except (TypeError, ValueError):
                    continue
    return worst


async def _assert_discount_is_safe(db: AsyncSession, percent: int) -> None:
    """Отбить процент, при котором цена может уйти в ноль. 422 с внятной причиной."""
    if percent < 0 or percent > MAX_OFFER_DISCOUNT_PERCENT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'Скидка сообщения не может быть больше {MAX_OFFER_DISCOUNT_PERCENT}%: '
                'вместе со скидкой промогруппы цена может уйти в ноль.'
            ),
        )

    group_percent = await _max_promo_group_percent(db)
    final_price, _, _ = PricingEngine.apply_stacked_discounts(_GUARD_REFERENCE_PRICE_KOPEKS, group_percent, percent)
    if final_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'При скидке {percent}% вместе со скидкой промогруппы ({group_percent}%) '
                'цена станет нулевой. Уменьшите процент.'
            ),
        )


def _assert_in_range(value: int, low: int, high: int, what: str) -> None:
    if value < low or value > high:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'{what}: допустимо от {low} до {high}.',
        )


# ---------------------------------------------------------------------------
# Каталог автосообщений
# ---------------------------------------------------------------------------
#
# Это данные, а не логика: список того, что бот умеет отправлять сам.
#   control  — 'toggle' (выключатель есть) | 'locked' (настроек нет)
#              | 'server' (включается только настройкой окружения)
#   settings_key — ключ в data/notification_settings.json, если он есть
#   sent_type / sent_days — чем строка помечается в sent_notifications
#   claim_type   — notification_type скидочного оффера, если сообщение его создаёт
#
# 🔴 Полнота каталога закреплена тестом: каждый record_notification из
# monitoring_service обязан иметь здесь соответствие.

AUTO_MESSAGE_CATALOG: list[dict[str, Any]] = [
    {
        'id': 'trial-2h',
        'group': 'trial',
        'title': 'Пробный истекает через 2 часа',
        'when': 'За 2 часа до конца — каждому, у кого идёт пробный',
        'control': 'locked',
        'sent_type': 'trial_2h',
        'buttons': [
            {'label': '💎 Купить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
            {'label': '💰 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
        ],
    },
    {
        'id': 'trial-expired',
        'group': 'trial',
        'title': 'Пробный истёк',
        'when': 'Сразу, как только закончился пробный период',
        'control': 'locked',
        'buttons': [
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
            {'label': '💳 Тарифы', 'target': 'Список тарифов в боте', 'tracked': True},
        ],
    },
    {
        'id': 'trial-discount',
        'group': 'trial',
        'title': 'Скидка на первую подписку',
        'when': 'Через {trigger_days} дн. после пробного — тем, кто ни разу не платил',
        'control': 'toggle',
        'settings_key': 'trial_expired_discount',
        'params': ('discount_percent', 'valid_hours', 'trigger_days'),
        'sent_type': 'trial_expired_discount',
        'claim_type': 'trial_expired_discount',
        'buttons': [
            {'label': '🎁 Получить скидку', 'target': 'Выдаёт скидку', 'tracked': True},
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
        ],
    },
    {
        'id': 'paid-3d',
        'group': 'paid',
        'title': 'Подписка истекает через 3 дня',
        'when': 'За 3 дня до конца — тем, у кого активна платная подписка',
        'control': 'locked',
        'sent_type': 'expiring',
        'sent_days': 3,
        'buttons': [
            {'label': '⏰ Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Кабинет, экран подписки', 'tracked': False},
        ],
    },
    {
        'id': 'paid-1d',
        'group': 'paid',
        'title': 'Подписка истекает завтра',
        'when': 'За 1 день до конца · если сообщение «за 3 дня» уже ушло, второй раз не отправляется',
        'control': 'locked',
        'sent_type': 'expiring',
        'sent_days': 1,
        'buttons': [
            {'label': '⏰ Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Кабинет, экран подписки', 'tracked': False},
        ],
    },
    {
        'id': 'paid-expired',
        'group': 'paid',
        'title': 'Подписка истекла',
        'when': 'В момент отключения доступа',
        'control': 'locked',
        'buttons': [
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
        ],
    },
    {
        'id': 'return-day1',
        'group': 'return',
        'title': 'Первый день без подписки',
        'when': 'Через 1 день после окончания подписки · с ценой продления',
        'control': 'toggle',
        'settings_key': 'expired_1d',
        'params': (),
        'sent_type': 'expired_1d',
        'buttons': [
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '🆘 Поддержка', 'target': 'Кабинет, экран поддержки', 'tracked': False},
        ],
    },
    {
        'id': 'return-wave2',
        'group': 'return',
        'title': 'Скидка на продление, вторая волна',
        'when': 'На 2-й или 3-й день после окончания — точный день зависит от часа проверки',
        'control': 'toggle',
        'settings_key': 'expired_second_wave',
        'params': ('discount_percent', 'valid_hours'),
        'sent_type': 'expired_discount_wave2',
        'claim_type': 'expired_discount_wave2',
        'buttons': [
            {'label': '🎁 Получить скидку', 'target': 'Выдаёт скидку', 'tracked': True},
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '🆘 Поддержка', 'target': 'Кабинет, экран поддержки', 'tracked': False},
        ],
    },
    {
        'id': 'return-wave3',
        'group': 'return',
        'title': 'Скидка на продление, третья волна',
        'when': 'Через {trigger_days} дн. после окончания подписки',
        'control': 'toggle',
        'settings_key': 'expired_third_wave',
        'params': ('discount_percent', 'valid_hours', 'trigger_days'),
        'sent_type': 'expired_discount_wave3',
        'claim_type': 'expired_discount_wave3',
        'buttons': [
            {'label': '🎁 Получить скидку', 'target': 'Выдаёт скидку', 'tracked': True},
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '🆘 Поддержка', 'target': 'Кабинет, экран поддержки', 'tracked': False},
        ],
    },
    {
        'id': 'traffic-80',
        'group': 'other',
        'title': 'Израсходовано много трафика',
        'when': 'При достижении порога — но только там, где вообще есть лимит гигабайт',
        'control': 'locked',
        'quiet_check': 'traffic_limits',
        'buttons': [],
    },
    {
        'id': 'channel-left',
        'group': 'other',
        'title': 'Отписка от канала: доступ закрыт',
        'when': 'Сразу после отписки от обязательного канала',
        'control': 'toggle',
        'settings_key': 'trial_channel_unsubscribed',
        'params': (),
        'quiet_check': 'channel_required',
        'buttons': [{'label': '✅ Я подписался', 'target': 'Повторная проверка в боте', 'tracked': True}],
    },
    {
        'id': 'grace-2d',
        'group': 'other',
        'title': 'Бонус: VPN работает ещё 2 дня',
        'when': 'Сразу после окончания платной подписки от месяца',
        'control': 'server',
        'quiet_check': 'grace_enabled',
        'buttons': [
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, экран подписки', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
        ],
    },
    {
        'id': 'low-balance',
        'group': 'other',
        'title': 'Низкий баланс',
        'when': 'Когда баланс становится низким — но только тем, кто сам включил это в своих настройках',
        'control': 'locked',
        'quiet_check': 'client_opt_in',
        'buttons': [{'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False}],
    },
    {
        'id': 'autopay-ok',
        'group': 'other',
        'title': 'Автоплатёж прошёл',
        'when': 'После успешного списания с баланса',
        'control': 'locked',
        'quiet_check': 'autopay',
        'buttons': [],
    },
    {
        'id': 'autopay-fail',
        'group': 'other',
        'title': 'Автоплатёж не прошёл',
        'when': 'Когда на балансе не хватило денег',
        'control': 'locked',
        'quiet_check': 'autopay',
        'buttons': [
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Кабинет, экран подписки', 'tracked': False},
        ],
    },
    {
        'id': 'autopay-final',
        'group': 'other',
        'title': 'Последнее напоминание об автоплатеже',
        'when': 'За несколько часов до отключения, если денег так и не хватило',
        'control': 'locked',
        'quiet_check': 'autopay',
        'buttons': [
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Кабинет, экран подписки', 'tracked': False},
        ],
    },
    {
        'id': 'autopay-legacy',
        'group': 'other',
        'title': 'Автоплатёж приостановлен: подписка без тарифа',
        'when': 'Один раз в неделю тем, чья подписка создана до введения тарифов',
        'control': 'locked',
        'quiet_check': 'autopay',
        'buttons': [],
    },
    {
        'id': 'daily-charge',
        'group': 'other',
        'title': 'Суточное списание',
        'when': 'Каждые сутки на суточном тарифе',
        'control': 'locked',
        'quiet_check': 'daily_tariffs',
        'buttons': [],
    },
    {
        'id': 'daily-paused',
        'group': 'other',
        'title': 'Подписка приостановлена: не хватило на сутки',
        'when': 'Когда на суточное списание не хватило денег',
        'control': 'locked',
        'quiet_check': 'daily_tariffs',
        'buttons': [
            {'label': '💳 Пополнить баланс', 'target': 'Экран баланса в боте', 'tracked': True},
            {'label': '📱 Моя подписка', 'target': 'Экран подписки в боте', 'tracked': True},
        ],
    },
    {
        'id': 'traffic-reset',
        'group': 'other',
        'title': 'Сброс докупленного трафика',
        'when': 'Через 30 дней после первой докупки гигабайтов',
        'control': 'locked',
        'quiet_check': 'traffic_topups',
        'buttons': [],
    },
]

CATALOG_BY_ID = {entry['id']: entry for entry in AUTO_MESSAGE_CATALOG}

# Писать настройки только через ИМЕНОВАННЫЕ сеттеры сервиса: в каждом сидит свой
# ограничитель диапазона. Общего set_field у сервиса нет, и обходить его записью
# в файл нельзя — потеряются именно эти ограничители.
SETTER_NAMES: dict[tuple[str, str], str] = {
    ('expired_second_wave', 'discount_percent'): 'set_second_wave_discount_percent',
    ('expired_second_wave', 'valid_hours'): 'set_second_wave_valid_hours',
    ('expired_third_wave', 'discount_percent'): 'set_third_wave_discount_percent',
    ('expired_third_wave', 'valid_hours'): 'set_third_wave_valid_hours',
    ('expired_third_wave', 'trigger_days'): 'set_third_wave_trigger_days',
    ('trial_expired_discount', 'discount_percent'): 'set_trial_expired_discount_percent',
    ('trial_expired_discount', 'valid_hours'): 'set_trial_expired_discount_valid_hours',
    ('trial_expired_discount', 'trigger_days'): 'set_trial_expired_discount_trigger_days',
}

# Сообщения, которые гасит общий выключатель ENABLE_NOTIFICATIONS. Он покрывает
# НЕ всё: проверено по monitoring_service — are_notifications_globally_enabled()
# стоит только на трёх проверках из десяти.
GLOBALLY_SWITCHED_IDS = frozenset({'trial-discount', 'return-day1', 'return-wave2', 'return-wave3', 'channel-left'})


# ---------------------------------------------------------------------------
# Схемы ответов
# ---------------------------------------------------------------------------


class AutoMessageButton(BaseModel):
    label: str
    target: str
    tracked: bool


class AutoMessageItem(BaseModel):
    id: str
    group: str
    title: str
    when: str
    control: str
    enabled: bool | None = None
    state: str
    quiet_reason: str | None = None
    params: dict[str, int] | None = None
    sent_count: int | None = None
    claimed_count: int | None = None
    claim_tracked: bool = False


class AutoMessageSummary(BaseModel):
    total_count: int
    live_count: int
    configurable_count: int
    sent_total: int
    claimed_total: int
    global_enabled: bool
    global_affects: int
    last_cycle_at: datetime | None = None


class AutoMessageListResponse(BaseModel):
    summary: AutoMessageSummary
    items: list[AutoMessageItem]


class AutoMessageHistoryRow(BaseModel):
    sent_at: datetime | None
    user_ref: str
    claimed: bool | None


class AutoMessageDetail(AutoMessageItem):
    buttons: list[AutoMessageButton]
    history: list[AutoMessageHistoryRow]
    history_note: str


class AutoMessagePatch(BaseModel):
    enabled: bool | None = None
    discount_percent: int | None = Field(default=None, ge=0, le=100)
    valid_hours: int | None = Field(default=None, ge=0, le=1000)
    trigger_days: int | None = Field(default=None, ge=0, le=1000)


class GlobalPatch(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Состояние: идёт или молчит
# ---------------------------------------------------------------------------


async def _quiet_reasons(db: AsyncSession) -> dict[str, str | None]:
    """Почему то или иное сообщение сегодня никому не уходит.

    Считаем по фактам системы, а не по догадкам: чтобы менеджер видел причину,
    а не пустой счётчик.
    """
    from app.config import settings

    reasons: dict[str, str | None] = {}

    with_limit = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status.in_(['active', 'trial']), Subscription.traffic_limit_gb > 0)
    )
    total_active = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.status.in_(['active', 'trial']))
    )
    reasons['traffic_limits'] = None if (with_limit or 0) > 0 else 'ни у одной активной подписки нет лимита гигабайтов'
    if (with_limit or 0) > 0:
        reasons['traffic_limits_note'] = f'лимит есть у {with_limit} подписок из {total_active or 0}'

    reasons['channel_required'] = None if settings.CHANNEL_IS_REQUIRED_SUB else 'подписка на канал не обязательна'
    reasons['grace_enabled'] = None if settings.GRACE_ENABLED else 'бонусные дни выключены на сервере'
    reasons['client_opt_in'] = 'уходит только тем, кто сам включил это в своих настройках'

    autopay_count = await db.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.autopay_enabled.is_(True))
    )
    reasons['autopay'] = None if (autopay_count or 0) > 0 else 'автоплатёж не включён ни у одной подписки'
    if (autopay_count or 0) > 0:
        reasons['autopay_note'] = f'автоплатёж включён у {autopay_count} подписок'

    from app.services.daily_subscription_service import daily_subscription_service

    reasons['daily_tariffs'] = None if daily_subscription_service.is_enabled() else 'суточные тарифы выключены'
    reasons['traffic_topups'] = None
    return reasons


def _params_for(entry: dict[str, Any]) -> dict[str, int] | None:
    """Текущие значения настраиваемых полей сообщения."""
    key = entry.get('settings_key')
    names = entry.get('params')
    if not key or not names:
        return None
    section = NotificationSettingsService.get_config().get(key, {})
    return {name: int(section.get(name, 0)) for name in names if name in section}


def _resolve_when(entry: dict[str, Any], params: dict[str, int] | None) -> str:
    when = entry['when']
    if '{trigger_days}' in when:
        days = (params or {}).get('trigger_days')
        return when.replace('{trigger_days}', str(days) if days else '—')
    return when


def _is_enabled(entry: dict[str, Any]) -> bool | None:
    key = entry.get('settings_key')
    if not key:
        return None
    return bool(NotificationSettingsService.is_enabled(key))


def _state_of(entry: dict[str, Any], reasons: dict[str, str | None]) -> tuple[str, str | None]:
    """Идёт сообщение или молчит, и почему. Выключатель — только одна из причин."""
    enabled = _is_enabled(entry)
    if enabled is False:
        return 'quiet', 'выключено в этом разделе'
    if not NotificationSettingsService.are_notifications_globally_enabled() and entry['id'] in GLOBALLY_SWITCHED_IDS:
        return 'quiet', 'выключено общим переключателем'
    check = entry.get('quiet_check')
    if check:
        reason = reasons.get(check)
        if reason:
            return 'quiet', reason
        note = reasons.get(f'{check}_note')
        if note:
            return 'live', note
    return 'live', None


# ---------------------------------------------------------------------------
# Счётчики
# ---------------------------------------------------------------------------


async def _sent_counts(db: AsyncSession) -> dict[tuple[str, int | None], int]:
    result = await db.execute(
        select(SentNotification.notification_type, SentNotification.days_before, func.count()).group_by(
            SentNotification.notification_type, SentNotification.days_before
        )
    )
    return {(row[0], row[1]): row[2] for row in result.all()}


async def _claimed_counts(db: AsyncSession) -> dict[str, int]:
    result = await db.execute(
        select(DiscountOffer.notification_type, func.count())
        .where(DiscountOffer.claimed_at.isnot(None))
        .group_by(DiscountOffer.notification_type)
    )
    return {row[0]: row[1] for row in result.all()}


def _sent_for(entry: dict[str, Any], counts: dict[tuple[str, int | None], int]) -> int | None:
    sent_type = entry.get('sent_type')
    if not sent_type:
        return None
    days = entry.get('sent_days')
    if days is not None:
        return counts.get((sent_type, days), 0)
    return sum(value for (key, _), value in counts.items() if key == sent_type)


def _build_item(
    entry: dict[str, Any],
    reasons: dict[str, str | None],
    sent_counts: dict[tuple[str, int | None], int],
    claimed_counts: dict[str, int],
) -> AutoMessageItem:
    params = _params_for(entry)
    state, quiet_reason = _state_of(entry, reasons)
    claim_type = entry.get('claim_type')
    return AutoMessageItem(
        id=entry['id'],
        group=entry['group'],
        title=entry['title'],
        when=_resolve_when(entry, params),
        control=entry['control'],
        enabled=_is_enabled(entry),
        state=state,
        quiet_reason=quiet_reason,
        params=params,
        sent_count=_sent_for(entry, sent_counts),
        claimed_count=claimed_counts.get(claim_type, 0) if claim_type else None,
        claim_tracked=bool(claim_type),
    )


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------


@router.get('', response_model=AutoMessageListResponse)
async def list_auto_messages(
    db: AsyncSession = Depends(get_cabinet_db),
    _: User = Depends(require_permission('auto_messages:read')),
) -> AutoMessageListResponse:
    reasons = await _quiet_reasons(db)
    sent_counts = await _sent_counts(db)
    claimed_counts = await _claimed_counts(db)

    items = [_build_item(entry, reasons, sent_counts, claimed_counts) for entry in AUTO_MESSAGE_CATALOG]

    last_cycle_at = await db.scalar(
        select(func.max(MonitoringLog.created_at)).where(MonitoringLog.event_type == 'monitoring_cycle_completed')
    )

    summary = AutoMessageSummary(
        total_count=len(items),
        live_count=sum(1 for item in items if item.state == 'live'),
        configurable_count=sum(1 for item in items if item.control == 'toggle'),
        sent_total=sum(item.sent_count or 0 for item in items),
        claimed_total=sum(item.claimed_count or 0 for item in items),
        global_enabled=NotificationSettingsService.are_notifications_globally_enabled(),
        global_affects=len(GLOBALLY_SWITCHED_IDS),
        last_cycle_at=last_cycle_at,
    )
    return AutoMessageListResponse(summary=summary, items=items)


@router.get('/{message_id}', response_model=AutoMessageDetail)
async def get_auto_message(
    message_id: str,
    request: Request,
    db: AsyncSession = Depends(get_cabinet_db),
    user: User = Depends(require_permission('auto_messages:read')),
) -> AutoMessageDetail:
    entry = CATALOG_BY_ID.get(message_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Сообщение не найдено')

    reasons = await _quiet_reasons(db)
    item = _build_item(entry, reasons, await _sent_counts(db), await _claimed_counts(db))

    history = await _history_for(db, entry, user)
    return AutoMessageDetail(
        **item.model_dump(),
        buttons=[AutoMessageButton(**button) for button in entry.get('buttons', [])],
        history=history,
        history_note=(
            'Строки исчезают, когда клиент продлевает подписку: так сегодня устроена защита от повторной отправки.'
        ),
    )


async def _history_for(db: AsyncSession, entry: dict[str, Any], viewer: User) -> list[AutoMessageHistoryRow]:
    """Последние 30 дней отправок. Имена показываем только тем, кто и так видит клиентскую базу."""
    sent_type = entry.get('sent_type')
    if not sent_type:
        return []

    from app.services.permission_service import PermissionService

    may_see_names, _ = await PermissionService.check_permission(db, viewer, 'users:read')

    since = datetime.now(UTC) - timedelta(days=30)
    conditions = [SentNotification.notification_type == sent_type, SentNotification.created_at >= since]
    days = entry.get('sent_days')
    if days is not None:
        conditions.append(SentNotification.days_before == days)

    claim_type = entry.get('claim_type')
    columns = [
        SentNotification.created_at,
        SentNotification.user_id,
        User.telegram_id,
        User.username,
        User.first_name,
    ]
    if claim_type:
        columns.append(DiscountOffer.claimed_at)

    query = select(*columns).join(User, User.id == SentNotification.user_id)
    if claim_type:
        # Склейка по (клиент, подписка, тип) — иначе чужой оффер того же типа
        # засчитался бы этой строке как «забрал скидку».
        query = query.outerjoin(
            DiscountOffer,
            (DiscountOffer.user_id == SentNotification.user_id)
            & (DiscountOffer.subscription_id == SentNotification.subscription_id)
            & (DiscountOffer.notification_type == claim_type),
        )
    query = query.where(*conditions).order_by(SentNotification.created_at.desc()).limit(50)

    history: list[AutoMessageHistoryRow] = []
    for row in (await db.execute(query)).all():
        created_at, user_id, telegram_id, username, first_name = row[:5]
        claimed_at = row[5] if claim_type else None
        if may_see_names:
            label = f'@{username}' if username else (first_name or f'id {telegram_id or user_id}')
        else:
            label = f'Клиент #{user_id}'
        history.append(
            AutoMessageHistoryRow(
                sent_at=created_at,
                user_ref=label,
                claimed=(claimed_at is not None) if claim_type else None,
            )
        )
    return history


@router.patch('/global')
async def patch_global_switch(
    payload: GlobalPatch,
    db: AsyncSession = Depends(get_cabinet_db),
    _: User = Depends(require_permission('auto_messages:edit')),
) -> dict[str, Any]:
    """Общий выключатель. Гасит ПЯТЬ сообщений из двадцати, а не всё — так в коде бота."""
    from app.services.system_settings_service import BotConfigurationService

    try:
        await BotConfigurationService.set_value(db, 'ENABLE_NOTIFICATIONS', payload.enabled)
    except Exception as error:
        logger.error('auto_messages_global_switch_failed', error=str(error))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось сохранить настройку. Значение осталось прежним.',
        ) from error

    # 🔴 Отвечаем ДЕЙСТВУЮЩИМ значением, а не запрошенным. Если ключ задан в
    # окружении, запись уходит в базу, но на живого бота не влияет — тогда экран
    # обязан сказать правду, а не отрапортовать успех.
    effective = NotificationSettingsService.are_notifications_globally_enabled()
    logger.info('auto_messages_global_switch', requested=payload.enabled, effective=effective)
    return {
        'enabled': effective,
        'applied': effective == payload.enabled,
        'affects': len(GLOBALLY_SWITCHED_IDS),
    }


@router.patch('/{message_id}', response_model=AutoMessageItem)
async def patch_auto_message(
    message_id: str,
    payload: AutoMessagePatch,
    db: AsyncSession = Depends(get_cabinet_db),
    _: User = Depends(require_permission('auto_messages:edit')),
) -> AutoMessageItem:
    entry = CATALOG_BY_ID.get(message_id)
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Сообщение не найдено')

    settings_key = entry.get('settings_key')
    if entry['control'] != 'toggle' or not settings_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Этим сообщением из кабинета управлять нельзя: у него нет настроек в боте.',
        )

    allowed = set(entry.get('params') or ())
    changes = payload.model_dump(exclude_none=True)

    for field in ('discount_percent', 'valid_hours', 'trigger_days'):
        if field in changes and field not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'У этого сообщения нет настройки «{field}».',
            )

    # Заборы ДО записи: половина применённых изменений хуже, чем ни одного.
    if 'discount_percent' in changes:
        await _assert_discount_is_safe(db, changes['discount_percent'])
    if 'valid_hours' in changes:
        _assert_in_range(changes['valid_hours'], MIN_VALID_HOURS, MAX_VALID_HOURS, 'Срок действия скидки, часов')
    if 'trigger_days' in changes:
        _assert_in_range(changes['trigger_days'], MIN_TRIGGER_DAYS, MAX_TRIGGER_DAYS, 'Через сколько дней')

    if 'enabled' in changes:
        NotificationSettingsService.set_enabled(settings_key, changes['enabled'])
    for field in ('discount_percent', 'valid_hours', 'trigger_days'):
        if field not in changes:
            continue
        setter_name = SETTER_NAMES.get((settings_key, field))
        if not setter_name:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'У этого сообщения нет настройки «{field}».',
            )
        getattr(NotificationSettingsService, setter_name)(changes[field])

    logger.info('auto_messages_patched', message_id=message_id, changes=sorted(changes))

    reasons = await _quiet_reasons(db)
    return _build_item(entry, reasons, await _sent_counts(db), await _claimed_counts(db))

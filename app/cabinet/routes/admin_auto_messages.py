"""Раздел кабинета «Автосообщения» — то, что бот отправляет клиентам по расписанию.

🔴 ГРАНИЦА КАТАЛОГА, не расширять молча. Здесь только сообщения, которые рождает
СЛУЖБА МОНИТОРИНГА и служба суточных подписок — то есть те, что уходят сами, по
времени. Подтверждения после действия клиента (авто-продление и авто-покупка
после пополнения баланса — ``subscription_auto_purchase_service``, восемь отправок)
в каталог НЕ входят: у них другая природа, другие условия и другой владелец. Если
их когда-нибудь добавят, надо править и обещание на экране.

После АС-2 выключатель есть у девятнадцати сообщений из двадцати. Двадцатое —
бонусные дни: там рычаг гасит не письмо, а сам бонус, поэтому ``control`` честно
говорит «настроек нет», а ``PATCH`` по нему отвечает 409. Обратное правило важнее:
нарисованный выключатель обязан правда запирать отправку, иначе экран врёт.

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
    SubscriptionStatus,
    Tariff,
    User,
)
from app.services.notification_settings_service import NotificationSettingsService
from app.services.pricing_engine import PricingEngine
from app.utils.formatters import format_hours_declension, format_subscriptions_declension

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/auto-messages', tags=['Admin Auto Messages'])


# ---------------------------------------------------------------------------
# Заборы на деньги
# ---------------------------------------------------------------------------

# Потолок скидки сообщения. Скидка промогруппы и скидка сообщения ПЕРЕМНОЖАЮТСЯ
# (PricingEngine.apply_stacked_discounts), поэтому опасно не само число, а итог.
MAX_OFFER_DISCOUNT_PERCENT = 50

# Опорная цена для проверки итога — 1 ₽.
# 🔴 Осторожно с рассуждением «меньшая цена опаснее»: оно НЕВЕРНО. `apply_discount`
# округляет вниз саму СКИДКУ (`amount * percent // 100`), поэтому при проценте ниже
# 100 цена не может стать нулём ни на рубле, ни на копейке. Ноль рождается ровно от
# ста процентов. Опорная цена здесь — не «худший случай», а просто общий знаменатель,
# на котором видно, кто именно обнулил цену: промогруппа или наше сообщение.
_GUARD_REFERENCE_PRICE_KOPEKS = 100

# Ноль процентов — не «выключено», а письмо «Скидка 0% на продление». Выключают
# тумблером, а не обнулением.
# 🔴 Не меньше часа. Служба мониторинга обходит всех раз в час, а условие отправки —
# «до конца осталось не больше N». Окно уже часа цикл перешагнёт, и большинство
# клиентов не получит предупреждения вовсе, причём молча.
#
# 🔴 Ровно час НЕ подходит, хотя владелец выбрал именно его как самое малое: цикл спит
# час ПОСЛЕ работы, значит шаг между обходами — час плюс длительность обхода. Окно
# шириной ровно в час уже шага, и в каждом обороте остаётся слепая полоса. Самое малое
# работающее — два часа; это ровно то, что стоит на боевом сегодня.
MIN_WARN_HOURS = 2
MAX_WARN_HOURS = 48
MIN_OFFER_DISCOUNT_PERCENT = 1
MIN_VALID_HOURS = 1
MAX_VALID_HOURS = 168
# 🔴 29, и не 30. Выборка «кто остался без подписки» смотрит назад ровно на 30 дней, а
# отправка требует, чтобы прошло от N до N+1 дня. При 30 эти два условия пересекаются в
# единственной точке «ровно 30,000 суток», куда часовой обход не попадает. Работает
# последнее полное окно — 29. Мина из АС-1: сначала стояло 60, потом 30 — оба мёртвые.
MAX_TRIGGER_DAYS = NotificationSettingsService.MAX_TRIGGER_DAYS

# Человеческие имена полей: в отказ уходят они, а не латиница из кода.
# Все числовые поля раздела в одном месте: обработчик перебирает именно этот набор,
# и добавление нового поля мимо него молча ничего не сохранит.
_NUMERIC_FIELDS: tuple[str, ...] = ('warn_hours', 'discount_percent', 'valid_hours', 'trigger_days')

FIELD_TITLES: dict[str, str] = {
    'warn_hours': 'За сколько предупредить',
    'discount_percent': 'Размер скидки',
    'valid_hours': 'Сколько действует',
    'trigger_days': 'Через сколько дней',
}


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
    """Отбить процент, от которого цена уходит в ноль. 422 с внятной причиной."""
    if percent < MIN_OFFER_DISCOUNT_PERCENT or percent > MAX_OFFER_DISCOUNT_PERCENT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'Размер скидки: допустимо от {MIN_OFFER_DISCOUNT_PERCENT} '
                f'до {MAX_OFFER_DISCOUNT_PERCENT}%. Чтобы сообщение перестало уходить, '
                'выключите его, а не ставьте ноль.'
            ),
        )

    group_percent = await _max_promo_group_percent(db)
    after_group = PricingEngine.apply_discount(_GUARD_REFERENCE_PRICE_KOPEKS, group_percent)
    if after_group <= 0:
        # Цену обнулила промогруппа, а не мы. Запрещать тут правку нельзя: иначе
        # раздел запирается целиком, и сбить сбежавшую скидку станет невозможно
        # ровно тогда, когда это нужнее всего.
        return

    final_price, _, _ = PricingEngine.apply_stacked_discounts(_GUARD_REFERENCE_PRICE_KOPEKS, group_percent, percent)
    if final_price <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(f'При скидке {percent}% вместе со скидкой промогруппы цена станет нулевой. Уменьшите процент.'),
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
        'title': 'Пробный скоро истекает',
        'when': 'За {warn_hours} до конца — каждому, у кого идёт пробный',
        'control': 'toggle',
        'settings_key': 'trial_2h',
        'params': ('warn_hours',),
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
        'control': 'toggle',
        'settings_key': 'subscription_expired',
        'params': (),
        'shares_switch_with': 'Подписка истекла',
        'warning': 'Выключите — и человек, у которого кончился пробный, просто увидит, что VPN перестал работать. Бот ему ничего не напишет.',
        'buttons': [
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
            {'label': '💳 Тарифы', 'target': 'Список тарифов в боте', 'tracked': False},
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
        'when': 'За 3 дня до конца — тем, у кого активна платная подписка и кто не отключил это у себя',
        'control': 'toggle',
        'settings_key': 'subscription_expiring',
        'params': (),
        'shares_switch_with': 'Подписка истекает завтра',
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
        'when': (
            'За 1 день до конца — тем, кто не отключил это у себя · более срочное вытесняет трёхдневное, а не наоборот'
        ),
        'control': 'toggle',
        'settings_key': 'subscription_expiring',
        'params': (),
        'shares_switch_with': 'Подписка истекает через 3 дня',
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
        'control': 'toggle',
        'settings_key': 'subscription_expired',
        'params': (),
        'shares_switch_with': 'Пробный истёк',
        'warning': 'Это единственное сообщение, из которого клиент узнаёт, что подписка кончилась и доступ закрыт. Выключите — люди будут молча терять связь.',
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
            {'label': '🆘 Поддержка', 'target': 'Экран поддержки в боте', 'tracked': False},
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
            {'label': '🆘 Поддержка', 'target': 'Экран поддержки в боте', 'tracked': False},
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
            {'label': '🆘 Поддержка', 'target': 'Экран поддержки в боте', 'tracked': False},
        ],
    },
    {
        'id': 'traffic-80',
        'group': 'other',
        'title': 'Израсходовано много трафика',
        'when': (
            'Когда израсходована та доля гигабайт, которую клиент выставил себе сам — и только '
            'там, где лимит вообще есть'
        ),
        'control': 'toggle',
        'settings_key': 'traffic_warning',
        'params': (),
        'warning': 'Это единственное предупреждение перед тем, как гигабайты закончатся. Выключите — интернет у клиента остановится без предупреждения.',
        'quiet_check': 'traffic_limits',
        'buttons': [],
    },
    {
        'id': 'channel-left',
        'group': 'other',
        'title': 'Отписка от канала: доступ закрыт',
        'when': 'После отписки от обязательного канала: сразу и повторно на ближайшей проверке',
        'control': 'toggle',
        'settings_key': 'trial_channel_unsubscribed',
        'params': (),
        'sent_type': 'trial_channel_unsubscribed',
        'quiet_check': 'channel_required',
        'buttons': [
            {'label': '🔗 <название канала>', 'target': 'Ссылка на канал — своя кнопка на каждый', 'tracked': False},
            {'label': '✅ Я подписался', 'target': 'Повторная проверка в боте', 'tracked': False},
        ],
    },
    {
        'id': 'channel-back',
        'group': 'other',
        'title': 'Вернулся в канал: доступ открыт',
        'when': 'Сразу после того, как клиент снова подписался на обязательный канал',
        'control': 'toggle',
        'settings_key': 'channel_restored',
        'params': (),
        'quiet_check': 'channel_required',
        'buttons': [],
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
        'when': (
            'Когда баланс низкий — и только если клиент сам включил это у себя, автоплатёж у него '
            'включён, подписка кончается в ближайшие дни и сейчас не ночь'
        ),
        'control': 'toggle',
        'settings_key': 'low_balance',
        'params': (),
        'quiet_check': 'client_opt_in',
        'buttons': [{'label': '💳 Пополнить баланс', 'target': 'Кабинет, главный экран', 'tracked': False}],
    },
    {
        'id': 'autopay-ok',
        'group': 'other',
        'title': 'Автоплатёж прошёл',
        'when': 'После успешного списания с баланса',
        'control': 'toggle',
        'settings_key': 'autopay_success',
        'params': (),
        'quiet_check': 'autopay',
        'buttons': [],
    },
    {
        'id': 'autopay-fail',
        'group': 'other',
        'title': 'Автоплатёж не прошёл',
        'when': 'Когда на балансе не хватило денег',
        'control': 'toggle',
        'settings_key': 'autopay_failed',
        'params': (),
        'shares_switch_with': 'Последнее напоминание об автоплатеже',
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
        'control': 'toggle',
        'settings_key': 'autopay_failed',
        'params': (),
        'shares_switch_with': 'Автоплатёж не прошёл',
        'warning': 'Последнее предупреждение перед отключением за неуплату. Выключите — подписка оборвётся без напоминания.',
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
        'control': 'toggle',
        'settings_key': 'autopay_legacy',
        'params': (),
        'quiet_check': 'legacy_subscriptions',
        'buttons': [],
    },
    {
        'id': 'daily-charge',
        'group': 'other',
        'title': 'Суточное списание',
        'when': 'Каждые сутки на суточном тарифе',
        'control': 'toggle',
        'settings_key': 'daily_charge',
        'params': (),
        'quiet_check': 'daily_tariffs',
        'buttons': [],
    },
    {
        'id': 'daily-paused',
        'group': 'other',
        'title': 'Подписка приостановлена: не хватило на сутки',
        'when': 'Когда на суточное списание не хватило денег',
        'control': 'toggle',
        'settings_key': 'daily_paused',
        'params': (),
        'warning': 'Гасит только сообщение. Подписка всё равно приостановится — клиент останется без VPN и не будет знать почему.',
        'quiet_check': 'daily_tariffs',
        'buttons': [
            {'label': '💳 Пополнить баланс', 'target': 'Экран баланса в боте', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Экран подписки в боте', 'tracked': False},
        ],
    },
    {
        'id': 'traffic-reset',
        'group': 'other',
        'title': 'Сброс докупленного трафика',
        'when': 'Когда истекает срок докупленного пакета гигабайтов',
        'control': 'toggle',
        'settings_key': 'traffic_reset',
        'params': (),
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
    ('trial_2h', 'warn_hours'): 'set_trial_warn_hours',
}

# Читаем тоже через геттеры, а не из сырого файла: в них сидят те же ограничители,
# и экран показывает ровно то число, которое применит бот. Чтение сырого JSON
# показывало бы 0 там, где бот подставляет 2.
GETTER_NAMES: dict[tuple[str, str], str] = {
    ('expired_second_wave', 'discount_percent'): 'get_second_wave_discount_percent',
    ('expired_second_wave', 'valid_hours'): 'get_second_wave_valid_hours',
    ('expired_third_wave', 'discount_percent'): 'get_third_wave_discount_percent',
    ('expired_third_wave', 'valid_hours'): 'get_third_wave_valid_hours',
    ('expired_third_wave', 'trigger_days'): 'get_third_wave_trigger_days',
    ('trial_expired_discount', 'discount_percent'): 'get_trial_expired_discount_percent',
    ('trial_expired_discount', 'valid_hours'): 'get_trial_expired_discount_valid_hours',
    ('trial_expired_discount', 'trigger_days'): 'get_trial_expired_discount_trigger_days',
    ('trial_2h', 'warn_hours'): 'get_trial_warn_hours',
}

# Нижняя граница «через сколько дней» у сообщений РАЗНАЯ: у третьей волны сеттер
# поднимает единицу до двойки, у скидки после пробного двойка не нужна. Общая
# константа врала бы одному из двух: приняли бы 1, а записалось бы 2, и ответ
# молча вернул бы не то, что просили.
TRIGGER_DAYS_MIN: dict[str, int] = {
    'expired_third_wave': 2,
    'trial_expired_discount': 1,
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
    # Уточнение к РАБОТАЮЩЕМУ сообщению («кому именно уходит»). Отдельно от причины
    # молчания намеренно: одно поле на два смысла уже сделало живое сообщение «молчащим».
    note: str | None = None
    # Имя сообщения, которое гасится ТЕМ ЖЕ выключателем. Три пары в коде бота пишет
    # одно и то же место, разделить их — отдельная работа (решение владельца 01.09.2026).
    shares_switch_with: str | None = None
    # Что случится с клиентом, если это выключить. Только там, где последствие настоящее.
    warning: str | None = None
    params: dict[str, int] | None = None
    sent_count: int | None = None
    claimed_count: int | None = None
    claim_tracked: bool = False
    # Границы для каждого поля. Экран берёт их отсюда, а не зашивает у себя: пол
    # «через сколько дней» у разных сообщений разный, и зашитая единица врала бы.
    limits: dict[str, list[int]] | None = None


class AutoMessageSummary(BaseModel):
    total_count: int
    live_count: int
    configurable_count: int
    sent_total: int
    claimed_total: int
    # Только для показа. Этот переключатель живёт в настройках бота и отсюда не меняется:
    # он пишет общий ключ конфигурации, который вдобавок глушит уведомления клиентам об
    # ответах поддержки. Раздел про автосообщения такой радиус обещать не может.
    global_enabled: bool
    global_affects: int
    global_editable_here: bool = False
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
    # extra='forbid': опечатка в имени поля должна быть отказом, а не тихим 200,
    # после которого менеджер уверен, что скидку поменял.
    model_config = {'extra': 'forbid'}

    enabled: bool | None = None
    warn_hours: int | None = Field(default=None, ge=MIN_WARN_HOURS, le=MAX_WARN_HOURS)
    discount_percent: int | None = Field(default=None, ge=0, le=100)
    valid_hours: int | None = Field(default=None, ge=MIN_VALID_HOURS, le=MAX_VALID_HOURS)
    trigger_days: int | None = Field(default=None, ge=1, le=MAX_TRIGGER_DAYS)


# ---------------------------------------------------------------------------
# Состояние: идёт или молчит
# ---------------------------------------------------------------------------


async def _quiet_facts(db: AsyncSession) -> tuple[dict[str, str], dict[str, str]]:
    """Факты о системе для колонки «идёт или молчит».

    Возвращает ДВА разных словаря, и это принципиально:
      * ``reasons`` — почему сообщение сегодня никому не уходит;
      * ``notes``   — уточнение к работающему сообщению («кому именно уходит»).

    В одном словаре они уже путались: заметка про адресность делала живое
    сообщение вечно «молчащим» и занижала счётчик работающих.
    """
    from app.config import settings
    from app.services.daily_subscription_service import daily_subscription_service

    reasons: dict[str, str] = {}
    notes: dict[str, str] = {}

    # Ровно те статусы, которые видит отправитель (monitoring_service: выборка по
    # traffic_limit_gb). `limited` он не берёт: у такой подписки трафик уже кончился,
    # предупреждать поздно. Считать её здесь значило бы обещать письма, которых не будет.
    live_statuses = ['active', 'trial']
    with_limit = (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status.in_(live_statuses), Subscription.traffic_limit_gb > 0)
        )
        or 0
    )
    total_live = (
        await db.scalar(select(func.count()).select_from(Subscription).where(Subscription.status.in_(live_statuses)))
        or 0
    )
    if with_limit:
        notes['traffic_limits'] = f'лимит есть у {format_subscriptions_declension(with_limit)} из {total_live}'
    else:
        reasons['traffic_limits'] = 'ни у одной живой подписки нет лимита гигабайтов'

    if not settings.CHANNEL_IS_REQUIRED_SUB:
        reasons['channel_required'] = 'подписка на канал не обязательна'
    if not settings.GRACE_ENABLED:
        reasons['grace_enabled'] = 'бонусные дни выключены на сервере'

    # Это НЕ причина молчания: сообщение работает, просто адресно.
    notes['client_opt_in'] = 'уходит только тем, кто сам включил это в своих настройках'

    # Считаем ровно ту выборку, которую берёт сам автоплатёж: не триальные и живые.
    # Счёт «по всем подпискам» преувеличивал бы — там и триалы, и давно истёкшие.
    autopay_count = (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.autopay_enabled.is_(True),
                Subscription.is_trial.is_(False),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
        )
        or 0
    )
    if autopay_count:
        notes['autopay'] = f'автоплатёж включён у {format_subscriptions_declension(autopay_count)}'
    else:
        reasons['autopay'] = 'автоплатёж не включён ни у одной активной платной подписки'

    # Флага мало: он включён по умолчанию, а суточных тарифов может не быть вовсе.
    # Причина должна называть факт, а не настройку.
    if not daily_subscription_service.is_enabled():
        reasons['daily_tariffs'] = 'суточные тарифы выключены настройкой'
    else:
        daily_tariffs = await db.scalar(select(func.count()).select_from(Tariff).where(Tariff.is_daily.is_(True))) or 0
        if not daily_tariffs:
            reasons['daily_tariffs'] = 'суточных тарифов не заведено'

    legacy_count = (
        await db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.tariff_id.is_(None),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
            )
        )
        or 0
    )
    if not legacy_count:
        reasons['legacy_subscriptions'] = 'подписок без тарифа не осталось'

    return reasons, notes


def _params_for(entry: dict[str, Any]) -> dict[str, int] | None:
    """Текущие значения настраиваемых полей — через геттеры сервиса.

    Сырой файл читать нельзя: там может лежать мусор (упадёт весь список из-за
    одной записи) или значение вне допустимого диапазона, которое бот всё равно
    подтянет к границе. Геттеры делают и то и другое.
    """
    key = entry.get('settings_key')
    if not key:
        return None
    names = entry.get('params')
    if not names:
        # Пустой словарь, а НЕ None: у сообщения есть выключатель, просто нет числовых
        # полей. None экран читал как «управлять нельзя» и говорил это про управляемое.
        return {}
    values: dict[str, int] = {}
    for name in names:
        getter = GETTER_NAMES.get((key, name))
        if getter:
            values[name] = int(getattr(NotificationSettingsService, getter)())
    return values


def _resolve_when(entry: dict[str, Any], params: dict[str, int] | None) -> str:
    """Подставить в подпись живые значения настроек.

    🔴 Подпись обязана следовать за настройкой. Пока число было зашито в текст,
    можно было поменять момент отправки и оставить на экране старую цифру — ровно
    то враньё, за которое владелец и зацепился.
    """
    when = entry['when']
    values = params or {}
    if '{trigger_days}' in when:
        days = values.get('trigger_days')
        when = when.replace('{trigger_days}', str(days) if days else '—')
    if '{warn_hours}' in when:
        hours = values.get('warn_hours')
        when = when.replace('{warn_hours}', format_hours_declension(hours) if hours else '—')
    return when


def _is_enabled(entry: dict[str, Any]) -> bool | None:
    key = entry.get('settings_key')
    if not key:
        return None
    return bool(NotificationSettingsService.is_enabled(key))


def _state_of(
    entry: dict[str, Any], reasons: dict[str, str], notes: dict[str, str]
) -> tuple[str, str | None, str | None]:
    """Идёт сообщение или молчит, почему молчит и что уточнить, если идёт.

    Возвращает ``(state, quiet_reason, note)``. Причина и уточнение — разные поля:
    класть уточнение в поле причины значит объяснять тишину там, где её нет.
    """
    if _is_enabled(entry) is False:
        return 'quiet', 'выключено в этом разделе', None
    if not NotificationSettingsService.are_notifications_globally_enabled() and entry['id'] in GLOBALLY_SWITCHED_IDS:
        return 'quiet', 'выключено общим переключателем в настройках бота', None
    check = entry.get('quiet_check')
    if check:
        reason = reasons.get(check)
        if reason:
            return 'quiet', reason, None
        return 'live', None, notes.get(check)
    return 'live', None, None


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


def _limits_for(entry: dict[str, Any]) -> dict[str, list[int]] | None:
    """Границы полей ИМЕННО этого сообщения — экран не должен их угадывать."""
    key = entry.get('settings_key')
    names = entry.get('params')
    if not key or not names:
        return None
    bounds: dict[str, list[int]] = {}
    for name in names:
        if name == 'discount_percent':
            bounds[name] = [MIN_OFFER_DISCOUNT_PERCENT, MAX_OFFER_DISCOUNT_PERCENT]
        elif name == 'valid_hours':
            bounds[name] = [MIN_VALID_HOURS, MAX_VALID_HOURS]
        elif name == 'trigger_days':
            bounds[name] = [TRIGGER_DAYS_MIN.get(key, 1), MAX_TRIGGER_DAYS]
        elif name == 'warn_hours':
            bounds[name] = [MIN_WARN_HOURS, MAX_WARN_HOURS]
    return bounds


def _build_item(
    entry: dict[str, Any],
    reasons: dict[str, str],
    notes: dict[str, str],
    sent_counts: dict[tuple[str, int | None], int],
    claimed_counts: dict[str, int],
) -> AutoMessageItem:
    params = _params_for(entry)
    state, quiet_reason, note = _state_of(entry, reasons, notes)
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
        note=note,
        shares_switch_with=entry.get('shares_switch_with'),
        warning=entry.get('warning'),
        params=params,
        sent_count=_sent_for(entry, sent_counts),
        claimed_count=claimed_counts.get(claim_type, 0) if claim_type else None,
        claim_tracked=bool(claim_type),
        limits=_limits_for(entry),
    )


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------


@router.get('', response_model=AutoMessageListResponse)
async def list_auto_messages(
    db: AsyncSession = Depends(get_cabinet_db),
    _: User = Depends(require_permission('auto_messages:read')),
) -> AutoMessageListResponse:
    reasons, notes = await _quiet_facts(db)
    sent_counts = await _sent_counts(db)
    claimed_counts = await _claimed_counts(db)

    items = [_build_item(entry, reasons, notes, sent_counts, claimed_counts) for entry in AUTO_MESSAGE_CATALOG]

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
        global_editable_here=False,
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

    reasons, notes = await _quiet_facts(db)
    item = _build_item(entry, reasons, notes, await _sent_counts(db), await _claimed_counts(db))

    history = await _history_for(db, entry, user, request)
    return AutoMessageDetail(
        **item.model_dump(),
        buttons=[AutoMessageButton(**button) for button in entry.get('buttons', [])],
        history=history,
        history_note=(
            'Строки исчезают, когда клиент продлевает подписку: так сегодня устроена защита от повторной отправки.'
        ),
    )


async def _history_for(
    db: AsyncSession, entry: dict[str, Any], viewer: User, request: Request
) -> list[AutoMessageHistoryRow]:
    """Последние 30 дней отправок. Имена показываем только тем, кто и так видит клиентскую базу.

    🔴 IP передаётся обязательно: без него политика запрета «не показывать клиентов вне
    офисной сети» молча не применяется, и маска снимается там, где не должна.
    """
    sent_type = entry.get('sent_type')
    if not sent_type:
        return []

    from app.services.permission_service import PermissionService

    from ..ip_utils import get_client_ip

    try:
        client_ip = get_client_ip(request)
    except HTTPException:
        client_ip = None
    may_see_names, _ = await PermissionService.check_permission(db, viewer, 'users:read', ip_address=client_ip)

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
        # 🔴 Подзапрос, а НЕ outerjoin. Пара (клиент, подписка, тип) не уникальна:
        # забранный или истёкший оффер не переиспользуется, рядом создаётся новый.
        # Join размножил бы одну отправку на несколько строк, а `limit` считает
        # строки ПОСЛЕ склейки — история молча теряла бы часть событий.
        columns.append(
            select(func.max(DiscountOffer.claimed_at))
            .where(
                DiscountOffer.user_id == SentNotification.user_id,
                DiscountOffer.subscription_id == SentNotification.subscription_id,
                DiscountOffer.notification_type == claim_type,
            )
            .correlate(SentNotification)
            .scalar_subquery()
            .label('claimed_at')
        )

    query = (
        select(*columns)
        .join(User, User.id == SentNotification.user_id)
        .where(*conditions)
        .order_by(SentNotification.created_at.desc())
        .limit(50)
    )

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
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Нечего сохранять: в запросе нет ни одного изменения.',
        )

    for field in _NUMERIC_FIELDS:
        if field in changes and field not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'У этого сообщения нет настройки «{FIELD_TITLES[field]}».',
            )

    # Заборы ДО записи: половина применённых изменений хуже, чем ни одного.
    if 'discount_percent' in changes:
        await _assert_discount_is_safe(db, changes['discount_percent'])
    if 'warn_hours' in changes:
        _assert_in_range(changes['warn_hours'], MIN_WARN_HOURS, MAX_WARN_HOURS, FIELD_TITLES['warn_hours'])
    if 'valid_hours' in changes:
        _assert_in_range(changes['valid_hours'], MIN_VALID_HOURS, MAX_VALID_HOURS, FIELD_TITLES['valid_hours'])
    if 'trigger_days' in changes:
        min_days = TRIGGER_DAYS_MIN.get(settings_key, 1)
        _assert_in_range(changes['trigger_days'], min_days, MAX_TRIGGER_DAYS, FIELD_TITLES['trigger_days'])

    # 🔴 Включение — тоже опасное действие. Процент мог быть выставлен в сто из
    # чат-админки бота, где потолка нет; без этой проверки тумблер выпускал бы его
    # в бой мимо забора.
    if changes.get('enabled') and 'discount_percent' in (entry.get('params') or ()):
        stored = (_params_for(entry) or {}).get('discount_percent')
        if stored is not None and 'discount_percent' not in changes:
            await _assert_discount_is_safe(db, stored)

    # Каждая запись возвращает bool. Выбросить его значило бы ответить «сохранено» на
    # том, что не легло на диск: значения живут в памяти процесса и исчезнут при рестарте.
    written: list[bool] = []
    if 'enabled' in changes:
        written.append(bool(NotificationSettingsService.set_enabled(settings_key, changes['enabled'])))
    for field in _NUMERIC_FIELDS:
        if field not in changes:
            continue
        setter_name = SETTER_NAMES.get((settings_key, field))
        if not setter_name:
            # Сюда попасть можно только при рассинхроне каталога и таблицы сеттеров —
            # это наша поломка, а не ошибка пользователя.
            logger.error('auto_messages_setter_missing', settings_key=settings_key, field=field)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Не удалось сохранить из-за ошибки в настройках раздела. Значение не изменено.',
            )
        written.append(bool(getattr(NotificationSettingsService, setter_name)(changes[field])))

    if written and not all(written):
        # 🔴 Сервис меняет значение в памяти ДО записи на диск и не откатывает его при
        # отказе. Кабинет и мониторинг — один процесс, поэтому без отката бот применял бы
        # к живым клиентам значение, про которое нам только что сказали «не сохранено».
        # Сбрасываем кэш, чтобы память вернулась к тому, что реально лежит на диске.
        NotificationSettingsService._loaded = False
        NotificationSettingsService._load()
        logger.error('auto_messages_save_failed', message_id=message_id, changes=sorted(changes))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось сохранить: сервер не принял изменение. Значение осталось прежним.',
        )

    logger.info('auto_messages_patched', message_id=message_id, changes=sorted(changes))

    reasons, notes = await _quiet_facts(db)
    return _build_item(entry, reasons, notes, await _sent_counts(db), await _claimed_counts(db))

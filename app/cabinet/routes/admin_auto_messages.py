"""Раздел кабинета «Автосообщения» — то, что бот отправляет клиентам сам, без их нажатия.

🔴 ГРАНИЦА КАТАЛОГА, не расширять молча. Здесь сообщения, которые рождает СЛУЖБА
МОНИТОРИНГА и служба суточных подписок, первое письмо после активации пробного (АП-2а),
и письмо в момент конца подписки: его с ВК-4
шлёт бот по событию панели «срок вышел» той же функцией монитора. Пока НЕ входят:
письма самой панели, которые бот пересылает («новое устройство», «настройки VPN получены»
и другие; письма о конце — только подпискам с флагом пробной на непробном тарифе), подтверждения после действия
клиента (квитанции об оплате; авто-продление и авто-покупка после пополнения —
``subscription_auto_purchase_service``, восемь отправок) и сообщения рефералки. Добавляя
их, править и обещание на экране (`admin.autoMessages.scope` в кабинете).

После АП-2б выключатель есть у двадцати четырёх сообщений из двадцати пяти. Двадцать пятое —
бонусные дни: там рычаг гасит не письмо, а сам бонус, поэтому ``control`` честно
говорит «настроек нет», а ``PATCH`` по нему отвечает 409. Обратное правило важнее:
нарисованный выключатель обязан правда запирать отправку, иначе экран врёт.

🔴 Забор на деньги (``_assert_discount_is_safe``) не формальность: менять процент
может не только владелец, но и роль Marketer, а нулевая итоговая цена ведёт себя
по-разному в двух кассах — старая пропускает нулевое списание и выдаёт платную
подписку бесплатно, новая отказывается продавать вовсе.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_serializer
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    DiscountOffer,
    MonitoringLog,
    PromoGroup,
    SentNotification,
    Subscription,
    SubscriptionCheckout,
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
# Границы живут в службе, а не здесь: продублируй их литералами — и экран однажды
# пообещает то, чего бот не применит.
MIN_NOT_CONNECTED_HOURS = NotificationSettingsService.MIN_NOT_CONNECTED_HOURS
MAX_NOT_CONNECTED_HOURS = NotificationSettingsService.MAX_NOT_CONNECTED_HOURS

# Человеческие имена полей: в отказ уходят они, а не латиница из кода.
# Все числовые поля раздела в одном месте: обработчик перебирает именно этот набор,
# и добавление нового поля мимо него молча ничего не сохранит.
_NUMERIC_FIELDS: tuple[str, ...] = (
    'warn_hours',
    'discount_percent',
    'valid_hours',
    'trigger_days',
    'not_connected_after_hours',
    'connect_reminder_minutes',
    'carrier',
    'audience',
    'post_trial_hours',
)

FIELD_TITLES: dict[str, str] = {
    'warn_hours': 'За сколько предупредить',
    'discount_percent': 'Размер скидки',
    'valid_hours': 'Сколько действует',
    'trigger_days': 'Через сколько дней',
    'not_connected_after_hours': 'Через сколько часов после начала',
    'connect_reminder_minutes': 'Через сколько минут после появления устройства',
    'carrier': 'В каком письме',
    'audience': 'Кому',
    'post_trial_hours': 'Скидка действует после пробного',
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


async def _assert_discount_is_safe(
    db: AsyncSession, percent: int, *, max_percent: int = MAX_OFFER_DISCOUNT_PERCENT
) -> None:
    """Отбить процент, от которого цена уходит в ноль. 422 с внятной причиной."""
    if percent < MIN_OFFER_DISCOUNT_PERCENT or percent > max_percent:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f'Размер скидки: допустимо от {MIN_OFFER_DISCOUNT_PERCENT} '
                f'до {max_percent}%. Чтобы сообщение перестало уходить, '
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
        'id': 'trial-started',
        'group': 'trial',
        'title': 'Пробный начался',
        'when': (
            'Сразу после включения пробного, текстом без картинки; дата окончания по Москве. '
            'С 00:00 до 08:00 МСК без звука.'
        ),
        'control': 'toggle',
        'settings_key': 'trial_started',
        'params': (),
        'quiet_check': 'trial_start_available',
        'warning': (
            'Выключите — пробный включится, но это сообщение не придёт. Старое меню новичка удаляется; '
            'новое меню клиент откроет командой /start. Включение не рассылает прежние активации заново.\n\n'
            'Дата и лимит берутся из выданной пробной подписки. '
            'Отправки пока не записываются, счётчика и истории у этого сообщения нет.'
        ),
        'buttons': [
            {'label': 'Подключить VPN', 'target': 'Кабинет, экран подключения', 'tracked': False},
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
            {'label': '👤 Личный кабинет', 'target': 'Кабинет, главная', 'tracked': False},
            {'label': '💳 Тарифы', 'target': 'Бот, сроки подписки', 'tracked': False},
            {
                'label': '🔗 Моя ссылка для подключения',
                'target': 'Бот, ссылка; только при готовой ссылке',
                'tracked': False,
            },
            {
                'label': '🎁 Привести друга — получить бонус',
                'target': 'Приглашения; только если программа включена',
                'tracked': False,
            },
        ],
    },
    {
        'id': 'trial-connect-reminder',
        'group': 'trial',
        'title': 'Пробный: настройки есть, VPN не включён',
        'when': (
            'Через {connect_reminder_minutes} мин после появления устройства, если накопленный расход меньше 1 МБ. '
            'С 00:00 до 08:00 МСК без звука. '
            'Действует для устройств, добавленных после включения.'
        ),
        'control': 'toggle',
        'settings_key': 'trial_connect_reminder',
        'params': ('connect_reminder_minutes',),
        'sent_type': 'trial_connect_reminder',
        'quiet_check': 'trial_tariff_marked',
        'buttons': [
            {'label': '📲 Подключиться', 'target': 'Кабинет, экран подключения', 'tracked': False},
            {'label': '💬 Написать в поддержку', 'target': 'Кабинет, экран поддержки', 'tracked': False},
        ],
    },
    {
        'id': 'trial-not-connected',
        'group': 'trial',
        'title': 'Пробный идёт, а VPN не подключён',
        'when': (
            'Через {not_connected_after_hours} после начала пробного — при накопленном расходе меньше 1 МБ. '
            'Получившим напоминание «Остался один шаг» письмо не приходит. '
            'С 00:00 до 08:00 МСК без звука.'
        ),
        'control': 'toggle',
        'settings_key': 'trial_not_connected',
        'params': ('not_connected_after_hours',),
        'sent_type': 'trial_not_connected',
        'quiet_check': 'trial_tariff_marked',
        'warning': (
            'Человек включил пробный и не настроил VPN. Пока сообщение выключено, бот молчит '
            'до самого конца теста, а напомнит только за пару часов до истечения — настраивать '
            'уже поздно.\n\n'
            'Включите — и письмо сразу уйдёт всем, кто сидит без подключения дольше выбранного '
            'срока. Дальше по одному-двум в сутки.\n\n'
            'При расходе от 1 МБ (1 000 000 байт) письмо не придёт: бот читает накопленный трафик панели, '
            'который не обнуляется каждую ночь. Автопроверка серверов приложением не считается использованием. '
            'Нет достоверного счётчика или профиля — бот промолчит до следующей проверки.'
        ),
        'buttons': [
            {'label': '📲 Подключиться', 'target': 'Кабинет, экран подключения', 'tracked': False},
            {'label': '💬 Написать в поддержку', 'target': 'Кабинет, экран поддержки', 'tracked': False},
        ],
    },
    {
        'id': 'trial-2h',
        'group': 'trial',
        'title': 'Пробный скоро истекает',
        'when': (
            'За {warn_hours} до конца — каждому на пробном тарифе, включая исчерпавших трафик. '
            'С 00:00 до 08:00 МСК без звука.'
        ),
        'control': 'toggle',
        'settings_key': 'trial_2h',
        'params': ('warn_hours',),
        'sent_type': 'trial_2h',
        'quiet_check': 'trial_tariff_marked',
        'warning': 'Цены и доплата из кассы клиента добавляются после текста автоматически; метки в шаблон не нужны.',
        'buttons': [
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, касса', 'tracked': False},
        ],
    },
    {
        'id': 'trial-2d',
        'group': 'trial',
        'title': 'Пробный закончится через 2 дня',
        'when': 'За 2 дня до конца пробного, по событию панели. С 00:00 до 08:00 МСК без звука.',
        'control': 'toggle',
        'settings_key': 'trial_expiring_48h',
        'params': (),
        'sent_type': 'trial_expiring_48h',
        'quiet_check': 'trial_warning_available',
        'stops_reminder': True,
        'warning': 'Если выключить, предупреждения за 2 дня не будет — панель его не заменит. Пропущенные письма потом не досылаются.',
        'buttons': [{'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False}],
    },
    {
        'id': 'trial-1d',
        'group': 'trial',
        'title': 'Пробный закончится завтра',
        'when': 'За день до конца пробного, по событию панели. С 00:00 до 08:00 МСК без звука.',
        'control': 'toggle',
        'settings_key': 'trial_expiring_24h',
        'params': (),
        'sent_type': 'trial_expiring_24h',
        'quiet_check': 'trial_warning_available',
        'stops_reminder': True,
        'warning': 'Если выключить, предупреждения за день не будет — панель его не заменит. Пропущенные письма потом не досылаются.',
        'buttons': [{'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False}],
    },
    {
        'id': 'trial-expired',
        'group': 'trial',
        'title': 'Пробный истёк',
        'when': (
            'Сразу после конца пробного, а если панель промолчала — при ближайшей часовой проверке. '
            'С 00:00 до 08:00 по Москве без звука.'
        ),
        'control': 'toggle',
        'settings_key': 'trial_expired',
        'params': (),
        'sent_type': 'trial_expired',
        # Письмо выбирает «пробный» по помеченному пробному тарифу: без метки пробные получили бы платный текст.
        'quiet_check': 'trial_tariff_marked',
        'warning': (
            'Если выключить, в минуту конца пробного человек ничего не получит: письмо панели «Подписка истекла» '
            'бот больше не пересылает, человек увидит только, что VPN отключился. До конца ему придёт «Пробный '
            'скоро истекает», через сутки — «Скидка на первую подписку», если они включены. Если включить обратно, '
            'часовая проверка допишет письмо тем, чей пробный кончился за последние 6 часов.\n\n'
            'Цена в письме — из кассы. Если касса цену не назвала, письмо уйдёт без строк цены; метку '
            '{price_lines} держите отдельной строкой в конце текста.'
        ),
        'buttons': [
            {'label': '💎 Оформить подписку', 'target': 'Кабинет, экран покупки', 'tracked': False},
            {'label': '💳 Тарифы', 'target': 'Список тарифов в боте', 'tracked': False},
        ],
    },
    {
        'id': 'trial-ending-discount',
        'group': 'trial',
        'title': 'Скидка до конца пробного',
        'when': 'Приходит в письме «{carrier_title}».',
        'control': 'toggle',
        'settings_key': 'trial_ending_discount',
        'params': ('discount_percent', 'carrier', 'audience', 'post_trial_hours'),
        'warning': 'Если выключить — скидку перестанут выдавать.',
        'buttons': [{'label': '🎁 Оформить со скидкой', 'target': 'Кабинет, касса', 'tracked': False}],
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
        'quiet_check': 'trial_tariff_marked',
        'claim_type': 'trial_expired_discount',
        'warning': 'Если выключить, через сутки после конца пробного клиенту ничего не придёт.',
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
        'warning': 'Если выключить, платным не придут оба предупреждения: за 3 дня и за день. '
        'Письма панели их больше не заменяют. Клиент увидит только конец подписки и последующие письма, '
        'если они включены. Настройка клиента «Окончание подписки» тоже может отключить эти предупреждения.',
        'sent_type': 'expiring',
        'sent_days': 3,
        'buttons': [
            {'label': '⏰ Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
        'warning': 'Если выключить, платным не придут оба предупреждения: за день и за 3 дня. '
        'Подстраховки письмом панели больше нет. Письмо о конце и последующие письма управляются отдельно; '
        'настройка клиента «Окончание подписки» тоже может отключить предупреждения.',
        'sent_type': 'expiring',
        'sent_days': 1,
        'buttons': [
            {'label': '⏰ Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
            {'label': '💳 Пополнить баланс', 'target': 'Кабинет, экран пополнения', 'tracked': False},
            {'label': '📱 Моя подписка', 'target': 'Кабинет, экран подписки', 'tracked': False},
        ],
    },
    {
        'id': 'paid-expired',
        'group': 'paid',
        'title': 'Подписка истекла',
        'when': 'Сразу после конца подписки, а если панель промолчала — при ближайшей часовой проверке.',
        'control': 'toggle',
        'settings_key': 'subscription_expired',
        'params': (),
        'sent_type': 'subscription_expired',
        'warning': (
            'Если выключить, в минуту конца клиент ничего не получит: письмо панели «Подписка истекла» бот больше '
            'не пересылает. Платному напомнит через сутки «Первый день без подписки», если оно включено; '
            'панель его больше не подстрахует. Подпискам с флагом пробной на обычном тарифе панель по-прежнему '
            'пишет «Подписка истекла вчера». Если включить обратно, часовая проверка допишет письмо тем, чья '
            'подписка кончилась за последние 6 часов.'
        ),
        'buttons': [
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
        'warning': 'Если выключить, платному через сутки после конца ничего не напомнит: письмо панели '
        '«Подписка истекла вчера» больше не пересылается. Следующее письмо — скидка на продление второй волны, '
        'если она включена. Письмо в минуту конца управляется отдельно.',
        'buttons': [
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
            {'label': '💎 Продлить подписку', 'target': 'Кабинет, касса продления', 'tracked': False},
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
    ('trial_connect_reminder', 'connect_reminder_minutes'): 'set_connect_reminder_minutes',
    ('expired_second_wave', 'discount_percent'): 'set_second_wave_discount_percent',
    ('expired_second_wave', 'valid_hours'): 'set_second_wave_valid_hours',
    ('expired_third_wave', 'discount_percent'): 'set_third_wave_discount_percent',
    ('expired_third_wave', 'valid_hours'): 'set_third_wave_valid_hours',
    ('expired_third_wave', 'trigger_days'): 'set_third_wave_trigger_days',
    ('trial_expired_discount', 'discount_percent'): 'set_trial_expired_discount_percent',
    ('trial_expired_discount', 'valid_hours'): 'set_trial_expired_discount_valid_hours',
    ('trial_expired_discount', 'trigger_days'): 'set_trial_expired_discount_trigger_days',
    ('trial_2h', 'warn_hours'): 'set_trial_warn_hours',
    ('trial_not_connected', 'not_connected_after_hours'): 'set_trial_not_connected_after_hours',
}

# Читаем тоже через геттеры, а не из сырого файла: в них сидят те же ограничители,
# и экран показывает ровно то число, которое применит бот. Чтение сырого JSON
# показывало бы 0 там, где бот подставляет 2.
GETTER_NAMES: dict[tuple[str, str], str] = {
    ('trial_connect_reminder', 'connect_reminder_minutes'): 'get_connect_reminder_minutes',
    ('expired_second_wave', 'discount_percent'): 'get_second_wave_discount_percent',
    ('expired_second_wave', 'valid_hours'): 'get_second_wave_valid_hours',
    ('expired_third_wave', 'discount_percent'): 'get_third_wave_discount_percent',
    ('expired_third_wave', 'valid_hours'): 'get_third_wave_valid_hours',
    ('expired_third_wave', 'trigger_days'): 'get_third_wave_trigger_days',
    ('trial_expired_discount', 'discount_percent'): 'get_trial_expired_discount_percent',
    ('trial_expired_discount', 'valid_hours'): 'get_trial_expired_discount_valid_hours',
    ('trial_expired_discount', 'trigger_days'): 'get_trial_expired_discount_trigger_days',
    ('trial_2h', 'warn_hours'): 'get_trial_warn_hours',
    ('trial_not_connected', 'not_connected_after_hours'): 'get_trial_not_connected_after_hours',
}

for _field, _suffix in (
    ('discount_percent', 'percent'),
    ('carrier', 'carrier'),
    ('audience', 'audience'),
    ('post_trial_hours', 'post_trial_hours'),
):
    SETTER_NAMES['trial_ending_discount', _field] = f'set_trial_ending_discount_{_suffix}'
    GETTER_NAMES['trial_ending_discount', _field] = f'get_trial_ending_discount_{_suffix}'

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
DISCOUNT_RELATED_IDS = frozenset({'trial-2d', 'trial-1d', 'trial-2h', 'trial-discount', 'trial-ending-discount'})


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
    # Имя сообщения, которое гасится ТЕМ ЖЕ выключателем. Две пары в коде бота пишет
    # одно и то же место (третью ВК-4 развёл), разделить их — отдельная работа (решение владельца 01.09.2026).
    shares_switch_with: str | None = None
    # Что случится с клиентом, если это выключить. Только там, где последствие настоящее.
    warning: str | None = None
    stops_reminder: bool = False
    params: dict[str, int] | None = None
    sent_count: int | None = None
    claimed_count: int | None = None
    claim_tracked: bool = False
    # Границы для каждого поля. Экран берёт их отсюда, а не зашивает у себя: пол
    # «через сколько дней» у разных сообщений разный, и зашитая единица врала бы.
    limits: dict[str, list[int]] | None = None
    # Заполняется только ответом на сохранение текста: «сохранено, но письмо уйдёт без логотипа».
    text_warning: str | None = None
    choices: dict[str, list[dict[str, Any]]] | None = None
    discount_prices: dict[str, list[dict[str, int]]] | None = None
    discount_context: dict[str, Any] | None = None

    @model_serializer(mode='wrap')
    def omit_absent_discount_fields(self, serializer):
        data = serializer(self)
        for field in ('choices', 'discount_prices', 'discount_context'):
            if data.get(field) is None:
                data.pop(field, None)
        return data


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


class AutoMessageInsertVariant(BaseModel):
    """Одна из фраз, которая может встать вместо метки, и условие, при котором встаёт."""

    text: str
    when: str


class AutoMessageInsert(BaseModel):
    """Метка, вместо которой подставляется не число, а другой текст."""

    name: str
    variants: list[AutoMessageInsertVariant]


class AutoMessageMarker(BaseModel):
    """Метка простыми словами: что это и чем она станет у клиента."""

    name: str
    what: str
    example: str


class AutoMessageDetail(AutoMessageItem):
    buttons: list[AutoMessageButton]
    history: list[AutoMessageHistoryRow]
    history_note: str
    # Текст письма, прочитанный оттуда же, откуда его берёт отправитель.
    text: str | None = None
    # Куски, которые отправитель ДОПИСЫВАЕТ к тексту сам (ссылка на кабинет, строка тарифа).
    text_suffixes: list[str] = []
    # Метки, вместо которых подставляется другой текст, — со списком вариантов.
    text_inserts: list[AutoMessageInsert] = []
    # Имя сообщения, у которого ТОТ ЖЕ текст. Выводится из общего ключа, не из руки.
    shares_text_with: str | None = None
    # 'code' — текст как в коде, 'custom' — владелец его правил.
    text_source: str = 'code'
    # Каждая метка показанного текста простыми словами. Без этого значок остаётся загадкой,
    # и человек боится трогать текст (прямое замечание владельца 06.09.2026).
    text_markers: list[AutoMessageMarker] = []
    # Есть ли у письма английская версия. У восьми писем её нет вовсе — они и сегодня
    # уходят по-русски всем, и правка их изменит: обещать обратное было бы неправдой.
    text_has_english: bool = False
    # Уходит ли письмо фотографией с логотипом. Только у таких потолок подписи что-то значит.
    text_with_logo: bool = False
    text_only: bool = False
    # Пределы длины приезжают с сервера, а не зашиты в экране: правило раздела с этапа АС-1.
    text_limits: dict[str, int] = {}


class AutoMessagePatch(BaseModel):
    # extra='forbid': опечатка в имени поля должна быть отказом, а не тихим 200,
    # после которого менеджер уверен, что скидку поменял.
    model_config = {'extra': 'forbid'}

    enabled: bool | None = None
    warn_hours: int | None = Field(default=None, ge=MIN_WARN_HOURS, le=MAX_WARN_HOURS)
    discount_percent: int | None = Field(default=None, strict=True)
    valid_hours: int | None = Field(default=None, ge=MIN_VALID_HOURS, le=MAX_VALID_HOURS)
    trigger_days: int | None = Field(default=None, ge=1, le=MAX_TRIGGER_DAYS)
    connect_reminder_minutes: int | None = Field(default=None, ge=5, le=60, strict=True)
    not_connected_after_hours: int | None = Field(default=None, ge=MIN_NOT_CONNECTED_HOURS, le=MAX_NOT_CONNECTED_HOURS)
    carrier: int | None = Field(default=None, ge=0, le=48, strict=True)
    audience: int | None = Field(default=None, ge=0, le=1, strict=True)
    post_trial_hours: int | None = Field(default=None, ge=1, le=48, strict=True)
    # Новый текст письма. Пустая строка не принимается — чтобы стереть правку, есть reset_text.
    text: str | None = None
    # Вернуть текст, который лежит в коде.
    reset_text: bool = False


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

    # Письмо о неподключении отбирает людей по ПОМЕЧЕННОМУ пробному тарифу. Метки нет —
    # отбирать не по чему, и служба выходит молча. Без этой строки экран показал бы
    # «работает» сообщению, которое не может уйти никогда.
    trial_tariff_marked = await db.scalar(
        select(func.count()).select_from(Tariff).where(Tariff.is_trial_available.is_(True))
    )
    if not trial_tariff_marked:
        reasons['trial_tariff_marked'] = 'ни один тариф не помечен пробным — отбирать не по чему'
        fallback_id = settings.get_trial_tariff_id()
        fallback_exists = fallback_id and await db.scalar(
            select(func.count())
            .select_from(Tariff)
            .where(Tariff.id == fallback_id, Tariff.entitlement_mode != 'access_point_managed')
        )
        if not fallback_exists:
            reasons['trial_start_available'] = reasons['trial_tariff_marked']
    if not settings.FUNNEL_MENU_ENABLED or not settings.is_cabinet_mode():
        reasons['trial_start_available'] = 'меню воронки или кабинетный режим выключены на сервере'
    if not trial_tariff_marked:
        reasons['trial_warning_available'] = reasons['trial_tariff_marked']
    elif not settings.WEBHOOK_NOTIFY_USER_ENABLED or not settings.WEBHOOK_NOTIFY_SUB_EXPIRING:
        reasons['trial_warning_available'] = 'предупреждения панели выключены на сервере: WEBHOOK_NOTIFY_*'

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
    if '{carrier_title}' in when:
        when = when.replace('{carrier_title}', CATALOG_BY_ID[_discount_context()['carrier_id']]['title'])
    values = params or {}
    if '{trigger_days}' in when:
        days = values.get('trigger_days')
        when = when.replace('{trigger_days}', str(days) if days else '—')
    if '{warn_hours}' in when:
        hours = values.get('warn_hours')
        when = when.replace('{warn_hours}', format_hours_declension(hours) if hours else '—')
    when = when.replace('{connect_reminder_minutes}', str(values.get('connect_reminder_minutes', '—')))
    if '{not_connected_after_hours}' in when:
        hours = values.get('not_connected_after_hours')
        when = when.replace('{not_connected_after_hours}', format_hours_declension(hours) if hours else '—')
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
    if entry['id'] == 'trial-ending-discount':
        context = _discount_context()
        carrier = context['carrier_id']
        state, reason, _ = _state_of(CATALOG_BY_ID[carrier], reasons, notes)
        if state == 'quiet':
            action = 'выключено' if not _is_enabled(CATALOG_BY_ID[carrier]) else 'молчит'
            return 'quiet', f'{action} письмо «{CATALOG_BY_ID[carrier]["title"]}»', None
        if not NotificationSettingsService.are_notifications_globally_enabled():
            return 'quiet', 'уведомления выключены в настройках бота', None
        return 'live', None, context['rollout_note']
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
            bounds[name] = [
                MIN_OFFER_DISCOUNT_PERCENT,
                30 if key == 'trial_ending_discount' else MAX_OFFER_DISCOUNT_PERCENT,
            ]
        elif name in ('carrier', 'audience', 'post_trial_hours'):
            bounds[name] = {'carrier': [0, 48], 'audience': [0, 1], 'post_trial_hours': [1, 48]}[name]
        elif name == 'valid_hours':
            bounds[name] = [MIN_VALID_HOURS, MAX_VALID_HOURS]
        elif name == 'trigger_days':
            bounds[name] = [TRIGGER_DAYS_MIN.get(key, 1), MAX_TRIGGER_DAYS]
        elif name == 'warn_hours':
            bounds[name] = [MIN_WARN_HOURS, MAX_WARN_HOURS]
        elif name == 'connect_reminder_minutes':
            bounds[name] = [5, 60]
        elif name == 'not_connected_after_hours':
            bounds[name] = [MIN_NOT_CONNECTED_HOURS, MAX_NOT_CONNECTED_HOURS]
    return bounds


def _choices_for(entry: dict[str, Any]) -> dict[str, list[dict[str, Any]]] | None:
    if entry['id'] != 'trial-ending-discount':
        return None
    hours = NotificationSettingsService.get_trial_warn_hours()
    saved_hours = NotificationSettingsService.get_trial_ending_discount_post_trial_hours()
    return {
        'carrier': [
            {'value': 0, 'label': f'За {format_hours_declension(hours)}'},
            {'value': 24, 'label': 'За сутки'},
            {'value': 48, 'label': 'За двое суток'},
        ],
        'audience': [{'value': 1, 'label': 'Включавшим VPN'}, {'value': 0, 'label': 'Всем на пробном'}],
        'post_trial_hours': [
            {'value': hours, 'label': format_hours_declension(hours)} for hours in sorted({6, 12, 24, 48, saved_hours})
        ],
    }


def _discount_context() -> dict[str, Any]:
    from app.services.trial_ending_discount_service import TRIAL_ENDING_DISCOUNT_ROLLOUT

    config = NotificationSettingsService.get_trial_ending_discount_settings()
    carrier_id = {0: 'trial-2h', 24: 'trial-1d', 48: 'trial-2d'}[config['carrier']]
    return {
        'carrier_id': carrier_id,
        'carrier_title': CATALOG_BY_ID[carrier_id]['title'],
        'percent': config['discount_percent'],
        'enabled': config['enabled'],
        'legacy_enabled': NotificationSettingsService.is_enabled('trial_expired_discount'),
        'rollout_note': 'Пока только тестовым аккаунтам' if TRIAL_ENDING_DISCOUNT_ROLLOUT == 'stands' else None,
    }


async def _discount_examples(db: AsyncSession, *, lock: bool = False) -> tuple[dict, dict]:
    from app.database.crud.promo_group import get_default_promo_group
    from app.database.crud.tariff import get_tariffs_for_user, get_trial_tariff
    from app.services.device_first_checkout_service import device_first_new_checkouts_enabled
    from app.services.device_first_eligibility import resolve_single_eligible_tariff
    from app.services.pricing_engine import pricing_engine
    from app.services.trial_ending_discount_service import _discount_expiry

    if not device_first_new_checkouts_enabled():
        return {}, {}
    group = await get_default_promo_group(db)
    if lock and group is not None:
        await db.refresh(group, with_for_update=True)
        if not group.is_default:
            return {}, {}
    tariffs = await get_tariffs_for_user(db, group.id if group else None)
    if lock:
        for tariff in tariffs:
            await db.refresh(tariff, with_for_update=True)
            await db.refresh(tariff, attribute_names=['allowed_promo_groups'])
        tariffs = [
            tariff
            for tariff in tariffs
            if not tariff.allowed_promo_groups
            or (group is not None and any(allowed.id == group.id for allowed in tariff.allowed_promo_groups))
        ]
    eligible = resolve_single_eligible_tariff(tariffs, subscription=None)
    if not eligible.eligible or 30 not in eligible.period_options:
        return {}, {}
    config = NotificationSettingsService.get_trial_ending_discount_settings()
    end = datetime.now(UTC) + timedelta(hours=config['carrier'] or NotificationSettingsService.get_trial_warn_hours())
    expiry = _discount_expiry(end, config['post_trial_hours'])
    trial = await get_trial_tariff(db)
    tariff = eligible.tariff
    person = SimpleNamespace(
        promo_group=group,
        promo_offer_discount_source='trial_ending_discount',
        promo_offer_discount_expires_at=expiry,
        has_had_paid_subscription=False,
    )
    prices, options = {}, {}
    for percent in range(1, 31):
        person.promo_offer_discount_percent = percent
        rows, matrix = [], []
        for days in eligible.period_options:
            quote = await pricing_engine.calculate_tariff_purchase_price(tariff, days, user=person)
            if type(quote.final_total) is not int or not 0 < quote.final_total <= 2**53 - 1:
                return {}, {}
            rows.append({'period_days': days, 'price_kopeks': quote.final_total})
            matrix.append(
                {
                    'period_days': days,
                    'prices': [
                        {
                            'device_limit': tariff.device_limit,
                            'price_kopeks': quote.final_total,
                            'breakdown': {
                                'promo_offer_discount_kopeks': quote.promo_offer_discount,
                                'promo_offer': {'discount_percent': percent, 'expires_at': expiry.isoformat()},
                            },
                        }
                    ],
                }
            )
        if not any(row['period_days'] == 30 for row in rows):
            return {}, {}
        prices[str(percent)] = rows
        if percent == config['discount_percent']:
            options = {
                'eligible': True,
                'tariff': {'id': tariff.id, 'base_device_limit': tariff.device_limit},
                'current_subscription': {'is_trial': True, 'tariff_id': trial.id} if trial else {},
                'price_matrix': matrix,
            }
    return prices, options


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
        stops_reminder=entry.get('stops_reminder', False),
        params=params,
        sent_count=_sent_for(entry, sent_counts),
        claimed_count=claimed_counts.get(claim_type, 0) if claim_type else None,
        claim_tracked=bool(claim_type),
        limits=_limits_for(entry),
        choices=_choices_for(entry),
        discount_context=_discount_context() if entry['id'] in DISCOUNT_RELATED_IDS else None,
    )


# ---------------------------------------------------------------------------
# Текст письма
# ---------------------------------------------------------------------------
#
# 🔴 Карточка показывает текст, ПРОЧИТАННЫЙ ОТТУДА ЖЕ, откуда его берёт отправитель.
# Копию текста здесь заводить нельзя ни под каким видом: экран показывал бы то, что
# не уходит, и ни один тест этого бы не поймал (прямой запрет владельца 05.09.2026).
#
# Источников ровно два:
#   * словарь локалей `ru.json` — 14 сообщений, ключ ниже;
#   * константа рядом с отправителем — 8 сообщений, которых в словаре нет вовсе.

_LOCALE_TEXT_KEYS: dict[str, str] = {
    'trial-connect-reminder': 'TRIAL_CONNECT_REMINDER',
    'trial-2h': 'TRIAL_ENDING_TEXT',
    'trial-2d': 'TRIAL_EXPIRING_48H',
    'trial-1d': 'TRIAL_EXPIRING_24H',
    'trial-started': 'FUNNEL_TRIAL_ACTIVATED',
    'trial-expired': 'TRIAL_EXPIRED_NOTIFICATION',
    'trial-discount': 'TRIAL_EXPIRED_DISCOUNT',
    'paid-3d': 'SUBSCRIPTION_EXPIRING_PAID',
    'paid-1d': 'SUBSCRIPTION_EXPIRING_PAID',
    'return-day1': 'SUBSCRIPTION_EXPIRED_1D',
    'return-wave2': 'SUBSCRIPTION_EXPIRED_SECOND_WAVE',
    'return-wave3': 'SUBSCRIPTION_EXPIRED_THIRD_WAVE',
    'traffic-80': 'TRAFFIC_WARNING_ALERT',
    'channel-left': 'TRIAL_CHANNEL_UNSUBSCRIBED',
    'channel-back': 'SUBSCRIPTION_REACTIVATED_CHANNEL_SUBSCRIBE',
    'low-balance': 'LOW_BALANCE_ALERT',
    'autopay-ok': 'AUTOPAY_SUCCESS',
    'autopay-fail': 'AUTOPAY_FAILED',
    'autopay-final': 'AUTOPAY_FAILED_FINAL',
}

# Метки, вместо которых подставляется не число, а ДРУГОЙ ТЕКСТ. Показать шаблон и
# промолчать про них — значит показать предложение с невидимыми дырами.
# 🔴 В письмо встаёт ОДНА из фраз, а не все. Показывать их списком без условия —
# значит дать владельцу собрать в голове письмо, которого не бывает: прогон сценария
# 05.09.2026 показал, что он прочитает сверху вниз и решит, будто платящим клиентам
# уходит спокойное «автоплатёж включён», тогда как на боевом автоплатёж включён у
# ОДНОЙ активной платной подписки из 60 (замер 05.09.2026).
# Условия списаны с ветвлений отправителя `_send_subscription_expiring_notification`.
_INSERT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    'traffic_text': (
        ('FUNNEL_TRIAL_TRAFFIC_DAILY', 'лимит больше нуля и сбрасывается каждый день'),
        ('FUNNEL_TRIAL_TRAFFIC_TOTAL', 'лимит больше нуля, сброс не ежедневный'),
        ('FUNNEL_TRIAL_TRAFFIC_UNLIMITED', 'лимит равен нулю — трафик не ограничен'),
    ),
    'autopay_status': (
        ('AUTOPAY_STATUS_CARD_ACTIVE', 'автоплатёж включён, карта привязана'),
        ('AUTOPAY_STATUS_NO_CARD', 'автоплатёж включён, карты нет'),
        ('AUTOPAY_STATUS_OFF', 'автоплатёж выключен'),
    ),
    'action_text': (
        ('AUTOPAY_ACTION_CHECK_BALANCE', 'автоплатёж включён'),
        ('AUTOPAY_ACTION_ENABLE', 'автоплатёж выключен, а в настройках бота он разрешён'),
        ('AUTOPAY_ACTION_RENEW', 'автоплатёж выключен, и в настройках бота он тоже выключен'),
    ),
    'check_button': (('CHANNEL_CHECK_BUTTON', 'всегда'),),
    # ВК-4: строки цены в письме о конце пробного. Условия списаны с `_trial_price_lines`; не назвала касса
    # цену — строк нет вовсе (это сказано в «когда» карточки).
    'price_lines': (
        (
            'TRIAL_EXPIRED_PRICE_LINES',
            'в кассе есть цены на 1 и на 3 месяца, а на балансе меньше рубля или касса не предлагает доплату',
        ),
        ('TRIAL_EXPIRED_PRICE_LINES_BALANCE', 'в кассе есть цена месяца, а на балансе от рубля, но меньше этой цены'),
        ('TRIAL_EXPIRED_PRICE_LINES_FUNDED', 'в кассе есть цена месяца и денег на балансе уже хватает'),
    ),
}

# Пара, которую отправитель выбирает НЕ по клиенту, а по настройке бота: показывать
# обе — показывать фразу, которой сегодня не бывает ни у кого.
_ONLY_WITH_AUTOPAY_ALLOWED = 'AUTOPAY_ACTION_ENABLE'
_ONLY_WITHOUT_AUTOPAY_ALLOWED = 'AUTOPAY_ACTION_RENEW'

# Кому отправитель дописывает ссылку на кабинет, а кому — строку с тарифом.
_CABINET_LINK_IDS = frozenset({'grace-2d', 'paid-expired', 'paid-3d', 'paid-1d'})
_TARIFF_LINE_IDS = frozenset({'autopay-ok', 'autopay-fail', 'autopay-final'})


_CONST_SOURCE_NAMES: dict[str, str] = {
    'grace-2d': 'GRACE_STARTED_TEXT',
    'autopay-legacy': 'AUTOPAY_LEGACY_TEXT',
    'paid-expired': 'SUBSCRIPTION_EXPIRED_TEXT',
    'trial-not-connected': 'TRIAL_NOT_CONNECTED_TEXT',
    'daily-charge': 'DAILY_CHARGE_TEXT',
    'daily-paused': 'DAILY_PAUSED_TEXT',
    'traffic-reset': 'TRAFFIC_RESET_TEXT',
}


def _const_texts() -> dict[str, str]:
    """Тексты, которых в словаре нет: берутся у самих отправителей, не копируются."""
    from app.services.daily_subscription_service import (
        DAILY_CHARGE_TEXT,
        DAILY_PAUSED_TEXT,
        TRAFFIC_RESET_TEXT,
    )
    from app.services.monitoring_service import (
        AUTOPAY_LEGACY_TEXT,
        GRACE_STARTED_TEXT,
        SUBSCRIPTION_EXPIRED_TEXT,
        TRIAL_NOT_CONNECTED_TEXT,
    )

    return {
        'grace-2d': GRACE_STARTED_TEXT,
        'autopay-legacy': AUTOPAY_LEGACY_TEXT,
        'paid-expired': SUBSCRIPTION_EXPIRED_TEXT,
        'trial-not-connected': TRIAL_NOT_CONNECTED_TEXT,
        'daily-charge': DAILY_CHARGE_TEXT,
        'daily-paused': DAILY_PAUSED_TEXT,
        'traffic-reset': TRAFFIC_RESET_TEXT,
    }


# Метка простыми словами. Владелец не программист: без этого значок остаётся загадкой, и он
# боится трогать текст — прямое замечание 06.09.2026. Примеры НЕ выдуманы: это форма значения,
# которую подставляет отправитель. Числа, зависящие от настроек, названы местом, а не цифрой,
# чтобы не повторить историю «число из фикстуры ушло владельцу».
# Кому отправитель прикладывает логотип: только эти письма уходят фотографией с подписью,
# и только у них потолок подписи в 1024 символа что-то значит. Остальным обещать «уйдёт без
# логотипа» — обещать потерю того, чего не бывает. Список закреплён сторожем по исходникам
# отправителей, а не написан на глаз.
_WITH_LOGO_IDS = frozenset(
    {
        'grace-2d',
        'paid-expired',
        'trial-2h',
        'trial-not-connected',
        'channel-left',
        'return-day1',
        'return-wave2',
        'return-wave3',
        'autopay-ok',
        'autopay-fail',
        'autopay-final',
        'paid-3d',
        'paid-1d',
    }
)


_MARKER_HINTS: dict[str, tuple[str, str]] = {
    'app': ('приложение новейшего устройства: INCY или Happ', 'INCY'),
    'amount': ('сумма списания', '199.00'),
    'balance': ('остаток на счету клиента', '340.00'),
    'days': ('на сколько дней продлили', '30'),
    'days_text': ('сколько осталось до конца подписки', '3 дня'),
    'end_date': ('когда заканчивается подписка', '09.09.2026 14:30'),
    'expires_at': ('до какого момента действует предложение', '07.09.2026 12:00'),
    'hours_text': ('сколько часов осталось до конца пробного', '2 часа'),
    'limit': ('сколько гигабайт всего в тарифе', '100'),
    'limit_gb': ('текущий лимит трафика в гигабайтах', '200'),
    'percent': ('размер скидки', '10'),
    'percent:.0f': ('сколько процентов трафика израсходовано', '82'),
    'price': ('цена продления', '299 ₽'),
    'required': ('сколько нужно списать целиком, а не сколько не хватает', '199.00'),
    'reset_gb': ('сколько докупленных гигабайт сбросили', '50'),
    'threshold': ('порог низкого баланса, который клиент выставил себе сам', '100.00'),
    'trigger_days': ('через сколько дней уходит письмо', '7'),
    'until_str': ('до какого числа VPN ещё работает', '08.09'),
    'used:.1f': ('сколько гигабайт израсходовано', '82.4'),
}

# 🔴 Одна и та же метка выглядит по-РАЗНОМУ в разных письмах: у автоплатежа деньги идут
# через `format_price` («199 ₽», рубль внутри значения), у суточных — как «199.00», а у
# низкого баланса — вовсе без копеек. Показать одну форму на всех значило бы подтолкнуть
# владельца дописать «₽» там, где он уже есть, и клиент получил бы «199 ₽ ₽».
# Нашла линза текстов, сверив КАЖДУЮ метку с её отправителем.
_MARKER_HINTS_BY_MESSAGE: dict[tuple[str, str], tuple[str, str]] = {
    ('trial-started', 'end_date'): ('дата и время окончания выданного пробного, по Москве', '14.10.2026 14:35'),
    ('trial-started', 'limit_gb'): ('только в строке лимита: гигабайты выданной подписки', '5'),
    ('autopay-ok', 'amount'): ('сумма списания вместе с рублём', '199 ₽'),
    ('autopay-fail', 'balance'): ('остаток на счету вместе с рублём', '340 ₽'),
    ('autopay-fail', 'required'): ('сколько нужно списать целиком, вместе с рублём', '199 ₽'),
    ('autopay-final', 'balance'): ('остаток на счету вместе с рублём', '340 ₽'),
    ('autopay-final', 'required'): ('сколько нужно списать целиком, вместе с рублём', '199 ₽'),
    ('low-balance', 'balance'): ('остаток на счету, без копеек', '340'),
    ('low-balance', 'threshold'): ('порог, который клиент выставил себе сам, без копеек', '100'),
    # ВК-4: метки внутри строк цены письма о конце пробного. Цены — те, что клиент увидит в кассе.
    # Живут только внутри строк цены: вписать их в сам текст письма сохранение не даст.
    ('trial-expired', 'month_price'): ('только в строках цены ниже: цена месяца в кассе, с рублём', '149 ₽'),
    ('trial-expired', 'quarter_price'): ('только в строках цены ниже: цена трёх месяцев в кассе, с рублём', '399 ₽'),
    ('trial-expired', 'balance'): (
        'только в строках цены ниже: деньги на балансе в целых рублях, округлённых вниз, с рублём',
        '50 ₽',
    ),
    ('trial-expired', 'top_up'): ('только в строках цены ниже: сколько доплатить за месяц, с рублём', '99 ₽'),
}

_MARKER_HINTS_BY_MESSAGE.update(
    {
        (message_id, name): hint
        for (source_id, name), hint in list(_MARKER_HINTS_BY_MESSAGE.items())
        if source_id == 'trial-expired'
        for message_id in ('trial-2d', 'trial-1d', 'trial-2h')
    }
)

# Метки, значение которых владелец задаёт ЗДЕСЬ ЖЕ, полем на этой карточке. Для них
# показываем не пример, а его сегодняшнее число — прямая просьба владельца 06.09.2026.
_MARKER_FROM_PARAM: dict[str, str] = {
    'percent': 'discount_percent',
    'trigger_days': 'trigger_days',
}


def _markers_of(message_id: str, body: str | None, params: dict[str, int] | None) -> list[AutoMessageMarker]:
    """Метки показанного текста с расшифровкой. Порядок — как в самом письме."""
    if not body:
        return []
    # Тем же разбором, что и забор сохранения: регулярка считает `{{метка}}` меткой, а
    # разбор — литералом, и расшифровка расходилась бы с проверкой. Порядок сохраняем
    # по тексту, чтобы метки шли так же, как в письме.
    known = _marker_set(body)
    seen: list[str] = []
    for match in re.finditer(r'\{([^{}]+)\}', body):
        name = match.group(1)
        if name in known and name not in seen and name not in _INSERT_KEYS:
            seen.append(name)

    markers: list[AutoMessageMarker] = []
    for name in seen:
        hint = _MARKER_HINTS_BY_MESSAGE.get((message_id, name)) or _MARKER_HINTS.get(name)
        if not hint:
            # Метка без расшифровки — это значок-загадка на экране. Сторож обязан
            # покраснеть здесь, а не оставить владельца гадать.
            continue
        what, example = hint
        field = _MARKER_FROM_PARAM.get(name)
        if field and params and params.get(field) is not None:
            what = f'{what} — берётся из поля на этой карточке'
            example = str(params[field])
        markers.append(AutoMessageMarker(name=name, what=what, example=example))
    return markers


# Выше какой длины письмо уходит без логотипа. Не запрет, а предупреждение: отправитель сам
# снимает картинку, когда подпись не влезает (`caption_exceeds_telegram_limit`).
_CAPTION_LIMIT = 1024
# Жёсткий предел Телеграма 4096; конвенция проекта для набранных руками текстов — 4000.
_TEXT_LIMIT = 4000


# Метка тарифа — единственная, чьё присутствие зависит от настройки бота, а не от текста.
# Из сравнения она исключена намеренно: иначе включённый многотарифный режим запер бы
# повторную правку уже правленого письма отказом «пропала метка», которой в поле не видно.
_OPTIONAL_MARKER = 'tariff_label'


def _marker_set(text: str) -> set[str]:
    """Метки текста вместе со спецификатором и конверсией. Одна реализация на проект.

    🔴 Разбором format-строки, а НЕ регуляркой. Регулярка не знает грамматики: она молча
    пропускала `{{метка}}` (клиент получал буквальные скобки вместо числа), одиночную `{`
    или `}` (письмо не уходило вовсе) и конверсию `!r` (клиент получал сумму в кавычках).
    Все три поймали живыми прогонами отправки, каждую — своя проверка.
    Метку тарифа исключаем ТОЛЬКО в голом виде: со спецификатором она обязана считаться
    обычной, иначе `{tariff_label:d}` проходит сохранение и падает при отправке.
    """
    return NotificationSettingsService.marker_names(text)


def _filtered(body: str | None, multi_tariff: bool) -> str | None:
    """Текст в том виде, в каком он УХОДИТ при сегодняшних настройках.

    🔴 Метка тарифа рождается у отправителя только в многотарифном режиме; пока он выключен,
    она разворачивается в пустоту у каждого клиента. Владелец правит текст именно в этом виде
    (его решение 06.09.2026: «меньше значков на экране — меньше поводов их задеть»), поэтому
    и разрешённый набор меток считается отсюда же — иначе экран показывал бы одно, а требовал
    другое.
    """
    if body and not multi_tariff:
        return body.replace('{tariff_label}', '')
    return body


def _raw_source_text_of(message_id: str) -> str | None:
    """Исходный текст из кода БЕЗ фильтра по настройкам — чтобы знать, бывает ли в нём тариф."""
    from app.localization.loader import DEFAULT_LANGUAGE, load_locale

    key = _LOCALE_TEXT_KEYS.get(message_id)
    return load_locale(DEFAULT_LANGUAGE).get(key) if key else _const_texts().get(message_id)


def _source_text_of(message_id: str) -> str | None:
    """Исходный текст из КОДА — без правки владельца, но в том же отфильтрованном виде."""
    from app.config import settings
    from app.localization.loader import DEFAULT_LANGUAGE, load_locale

    # 🔴 Читаем словарь НАПРЯМУЮ, минуя `Texts`: внутри него стоит крючок правки владельца,
    # и через него «исходным» оказалась бы его же прошлая правка — набор меток поехал бы
    # вслед за ней, и вторая правка могла бы стереть метку без единого отказа.
    key = _LOCALE_TEXT_KEYS.get(message_id)
    raw = load_locale(DEFAULT_LANGUAGE).get(key) if key else _const_texts().get(message_id)
    return _filtered(raw, settings.is_multi_tariff_enabled())


def _describe(name: str) -> str:
    """Метка словами, чтобы отказ говорил на языке владельца, а не на языке кода."""
    hint = _MARKER_HINTS.get(name, _MARKER_HINTS.get(name.split(':')[0]))
    return f'{{{name}}} ({hint[0]})' if hint else '{' + name + '}'


def _validate_new_text(
    source: str, incoming: str, *, suffix_length: int = 0, tariff_optional: bool = False
) -> tuple[str, str | None]:
    """Проверяет правку владельца и возвращает (что сохранить, предупреждение).

    🔴 Набор меток обязан совпасть с исходным — ни стереть, ни добавить. Стёртая метка
    даёт клиенту письмо с дырой («Скидка % на продление»), а незнакомая или непарная
    скобка роняет отправку целиком: письмо не уходит вовсе и молча. Владелец сказал, что
    выдумывать метки менеджер не станет, — забор не про выдумки, а про то, что метку
    легко задеть, правя соседнее слово.
    """
    from app.utils.telegram_html import prepare_telegram_broadcast, telegram_visible_length

    body = (incoming or '').strip('\n')
    if not body.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Текст письма не может быть пустым. Наберите текст или закройте правку кнопкой «Отмена».',
        )

    # 🔴 Ведущие и хвостовые переносы восстанавливаются РОВНО такими, какие набрал человек.
    # `prepare_telegram_broadcast` их срезает, и у девяти писем из 22 «открыл и сохранил,
    # ничего не меняя» тихо меняло письмо: у трёх — прямо в середине, потому что бот
    # дописывает ссылку на кабинет, начинающуюся с пустой строки.
    lead = incoming[: len(incoming) - len(incoming.lstrip('\n'))]
    trail = incoming[len(incoming.rstrip('\n')) :]

    try:
        prepared = lead + prepare_telegram_broadcast(body) + trail
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    try:
        wanted, given = _marker_set(source), _marker_set(prepared)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                'В тексте осталась непарная фигурная скобка — с ней письмо не уйдёт никому. '
                'Проверьте, что у каждой метки есть и открывающая, и закрывающая скобка.'
            ),
        ) from error

    # Метку тарифа терпим в обе стороны — но ТОЛЬКО у писем, где она есть в исходнике.
    # Экран её прячет (многотарифный режим выключен), поэтому владелец не может ни вернуть
    # её, ни увидеть; а добавить её в чужое письмо нельзя: отправитель такого не подставит,
    # и письмо не уйдёт вовсе. Первая редакция вычёркивала метку у всех — скептик показал,
    # что так `{tariff_label}` пролезает в любое письмо.
    if tariff_optional:
        wanted = wanted - {_OPTIONAL_MARKER}
        given = given - {_OPTIONAL_MARKER}

    lost = sorted(wanted - given)
    if lost:
        names = ', '.join(_describe(name) for name in lost)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Не хватает метки {names}. Верните её в текст — без неё бот не подставит это значение.',
        )
    extra = sorted(given - wanted)
    if extra:
        names = ', '.join('{' + name + '}' for name in extra)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Метку {names} бот не знает — оставьте только те метки, что были в тексте. Ничего не сохранено.',
        )

    # Последний забор: текст обязан СОБРАТЬСЯ. Совпадения набора мало — форма метки может
    # быть такой, что подстановка всё равно падает, а падает она уже у живого клиента.
    # Значения подставляем ТАКИМИ, какими их даёт отправитель: почти всё это строки, и
    # числовой ноль скрывал бы поломки вроде `{tariff_label:d}`. Числом подставляем только
    # там, где спецификатор его требует.
    sample: dict[str, Any] = {}
    for name in _marker_set(prepared) | {_OPTIONAL_MARKER}:
        field = name.split('!')[0].split(':')[0]
        spec = name.split(':', 1)[1] if ':' in name else ''
        sample[field] = 0.0 if spec and spec[-1] in 'eEfFgGn%' else (0 if spec and spec[-1] in 'bcdoxX' else '')
    try:
        prepared.format(**sample)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Бот не смог собрать письмо с этим текстом. Проверьте фигурные скобки и метки.',
        ) from error

    warning = None
    if telegram_visible_length(prepared) + suffix_length > _CAPTION_LIMIT:
        warning = f'Сохранено. Текст длиннее {_CAPTION_LIMIT} символов, поэтому письмо уйдёт без логотипа.'
    return prepared, warning


def _source_name_of(message_id: str) -> str | None:
    """Имя ИСТОЧНИКА текста — ключ словаря или имя константы. Ключ хранения правок.

    🔴 Не `message_id`: у пары «за 3 дня / завтра» текст ОДИН, и ключ по письму заставил бы
    их разъехаться — а карточка прямо говорит человеку, что текст у них общий.
    """
    if message_id in _LOCALE_TEXT_KEYS:
        return _LOCALE_TEXT_KEYS[message_id]
    return _CONST_SOURCE_NAMES.get(message_id)


def _shares_text_with(message_id: str) -> str | None:
    """Кто ещё шлёт ТОТ ЖЕ текст. Выводится из общего ключа, а не пишется рукой."""
    key = _LOCALE_TEXT_KEYS.get(message_id)
    if not key:
        return None
    for other_id, other_key in _LOCALE_TEXT_KEYS.items():
        if other_key == key and other_id != message_id:
            twin = CATALOG_BY_ID.get(other_id)
            if twin:
                return twin['title']
    return None


def _text_facts(message_id: str, params: dict[str, int] | None = None) -> dict[str, Any]:
    """Текст письма и всё, без чего он на экране был бы неправдой."""
    from app.config import settings
    from app.localization.loader import DEFAULT_LANGUAGE, load_locale
    from app.localization.texts import get_texts
    from app.services.monitoring_service import AUTOPAY_TARIFF_LINE, cabinet_link_suffix

    texts = get_texts(DEFAULT_LANGUAGE)
    key = _LOCALE_TEXT_KEYS.get(message_id)
    # Правку владельца отдаёт `texts.get` сам — крючок стоит внутри `Texts` (этап АС-11),
    # поэтому словарные письма приходят уже с ней. У писем-констант источник читаем через
    # ту же службу, чтобы карточка и отправитель брали текст из ОДНОГО места.
    source_name = _source_name_of(message_id)
    if key:
        body = texts.get(key)
    else:
        raw = _const_texts().get(message_id)
        body = NotificationSettingsService.text_for(source_name, raw) if (source_name and raw) else raw
    # 🔴 «Изменён» — это когда показанный текст ОТЛИЧАЕТСЯ от кодового, а не когда правка
    # просто лежит в файле. Если она перестала применяться (метки разошлись с кодом после
    # обновления), карточка обязана показать кодовый текст и НЕ называть его изменённым:
    # иначе владелец видит свои слова исчезнувшими, а значок «изменён» висит. Нашёл скептик.

    # 🔴 Строка тарифа и метка {tariff_label} живут ТОЛЬКО в многотарифном режиме: у
    # отправителей КАЖДОЕ место, где они рождаются, стоит за `is_multi_tariff_enabled()`
    # (проверено поимённо 05.09.2026 и перепроверено скептиком). Пока режим выключен, они разворачиваются в
    # пустоту у КАЖДОГО клиента — и показать их владельцу значило бы показать кусок
    # письма, которого никто не получит. Ровно то, ради чего этап и делается.
    multi_tariff = settings.is_multi_tariff_enabled()
    body = _filtered(body, multi_tariff)
    # Считается ПОСЛЕ фильтра: до него показанный и кодовый тексты различаются меткой
    # тарифа у каждого письма, и значок «изменён» горел бы всегда.
    edited = bool(source_name) and body is not None and body != _source_text_of(message_id)

    suffixes: list[str] = []
    if message_id in _CABINET_LINK_IDS:
        link = cabinet_link_suffix()
        if link:
            suffixes.append(link)
    if message_id in _TARIFF_LINE_IDS and multi_tariff:
        suffixes.append(AUTOPAY_TARIFF_LINE)

    autopay_allowed = bool(getattr(settings, 'ENABLE_AUTOPAY', False))

    def _reachable(variant_key: str) -> bool:
        if variant_key == _ONLY_WITH_AUTOPAY_ALLOWED:
            return autopay_allowed
        if variant_key == _ONLY_WITHOUT_AUTOPAY_ALLOWED:
            return not autopay_allowed
        return True

    inserts = [
        AutoMessageInsert(
            name=name,
            variants=[
                AutoMessageInsertVariant(text=text, when=when)
                for variant_key, when in variants
                if _reachable(variant_key) and (text := texts.get(variant_key))
            ],
        )
        for name, variants in _INSERT_KEYS.items()
        if body and ('{' + name + '}' in body or (message_id == 'trial-2h' and name == 'price_lines'))
    ]

    markers = _markers_of(message_id, body, params)
    # Метки внутри фраз-вставок — только те, что расшифрованы именно для этого письма (строки цены письма о конце
    # пробного, ВК-4). Общая расшифровка чужой вставки могла бы показать не ту форму значения.
    shown = {marker.name for marker in markers}
    for marker in _markers_of(message_id, '\n'.join(v.text for insert in inserts for v in insert.variants), params):
        if (message_id, marker.name) in _MARKER_HINTS_BY_MESSAGE and marker.name not in shown:
            markers.append(marker)
            shown.add(marker.name)

    return {
        'text': body,
        'text_suffixes': suffixes,
        'text_inserts': inserts,
        'shares_text_with': _shares_text_with(message_id),
        'text_source': 'custom' if edited else 'code',
        'text_markers': markers,
        'text_has_english': bool(key and load_locale('en').get(key)),
        'text_with_logo': message_id in _WITH_LOGO_IDS,
        'text_only': message_id == 'trial-started',
        'text_limits': {'max': _TEXT_LIMIT, 'caption': _CAPTION_LIMIT},
    }


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
    for item in items:
        await _discount_item(db, item)

    last_cycle_at = await db.scalar(
        select(func.max(MonitoringLog.created_at)).where(MonitoringLog.event_type == 'monitoring_cycle_completed')
    )

    summary = AutoMessageSummary(
        total_count=len(items),
        live_count=sum(1 for item in items if item.state == 'live'),
        configurable_count=sum(1 for item in items if item.control == 'toggle'),
        sent_total=sum(item.sent_count or 0 for item in items if item.id != 'trial-ending-discount'),
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
    item = await _discount_item(
        db, _build_item(entry, reasons, notes, await _sent_counts(db), await _claimed_counts(db)), with_count=False
    )

    history = await _history_for(db, entry, user, request)
    history_note = (
        'Строки исчезают, когда клиент продлевает подписку или покупает её после пробного: так сегодня '
        'устроена защита от повторной отправки. Показаны последние 50 отправок.'
    )
    if message_id == 'trial-ending-discount':
        item.sent_count, history_note, history = await _trial_discount_stats(
            db, may_see_names=await _may_see_names(db, user, request)
        )
    text_facts = _text_facts(message_id, item.params)
    buttons = [AutoMessageButton(**button) for button in entry.get('buttons', [])]
    context = _discount_context()
    if context['enabled'] and message_id == context['carrier_id']:
        _, options = await _discount_examples(db)
        if options:
            text_facts['text_inserts'] = [
                insert for insert in text_facts['text_inserts'] if insert.name != 'price_lines'
            ] + [AutoMessageInsert(name='price_lines', variants=_discount_previews(options))]
            text_facts['text_markers'] = _markers_of(message_id, text_facts['text'], item.params)
            for button in buttons:
                button.target += '; без личной скидки'
            buttons.insert(
                0,
                AutoMessageButton(
                    label='🎁 Оформить со скидкой', target='Кабинет, касса; при личной скидке', tracked=False
                ),
            )
    order = ['trial-2d', 'trial-1d', 'trial-2h', 'trial-expired']
    if context['enabled'] and message_id in order and order.index(message_id) > order.index(context['carrier_id']):
        _, options = await _discount_examples(db)
        if options:
            for insert in text_facts['text_inserts']:
                if insert.name == 'price_lines':
                    insert.variants.extend(_discount_previews(options, short=True))
    return AutoMessageDetail(
        **item.model_dump(),
        **text_facts,
        buttons=buttons,
        history=history,
        history_note=history_note,
    )


async def _may_see_names(db: AsyncSession, viewer: User, request: Request) -> bool:
    if viewer is None or request is None:
        return False
    from app.services.permission_service import PermissionService

    from ..ip_utils import get_client_ip

    try:
        client_ip = get_client_ip(request)
    except HTTPException:
        client_ip = None
    return (await PermissionService.check_permission(db, viewer, 'users:read', ip_address=client_ip))[0]


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

    may_see_names = await _may_see_names(db, viewer, request)

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


def _has_numeric_change(payload: AutoMessagePatch) -> bool:
    """Есть ли в запросе что-то, кроме правки текста."""
    if payload.enabled is not None:
        return True
    return any(getattr(payload, field, None) is not None for field in _NUMERIC_FIELDS)


async def _trial_discount_stats(
    db: AsyncSession, *, may_see_names: bool = False
) -> tuple[int, str, list[AutoMessageHistoryRow]]:
    from app.cabinet.routes.admin_sales_stats import _owner_rules
    from app.config import settings
    from app.services.trial_ending_discount_service import TRIAL_ENDING_DISCOUNT_ROLLOUT
    from app.services.user_service import test_account_telegram_ids

    since = NotificationSettingsService.get_trial_ending_discount_stats_since()
    if since is None:
        note = (
            'Дата начала не записана; итог пока недоступен.'
            if NotificationSettingsService.is_trial_ending_discount_enabled()
            else 'Итог появится после первого включения.'
        )
        return 0, note, []
    rules = await _owner_rules(db)
    stands = TRIAL_ENDING_DISCOUNT_ROLLOUT == 'stands'
    test_account = case(
        (User.telegram_id.is_(None), False),
        (User.telegram_id == 0, False),
        else_=func.coalesce(User.test_account_enabled, User.telegram_id.in_(tuple(test_account_telegram_ids()))),
    )
    people = test_account if stands else rules.people
    now = datetime.now(UTC)
    offers = (
        await db.execute(
            select(
                DiscountOffer.user_id,
                DiscountOffer.created_at,
                DiscountOffer.extra_data,
                User.telegram_id,
                User.username,
                User.first_name,
            )
            .join(User, User.id == DiscountOffer.user_id)
            .where(
                DiscountOffer.notification_type == 'trial_ending_discount',
                DiscountOffer.created_at >= since,
                DiscountOffer.created_at <= now,
                people,
                User.status.is_distinct_from('deleted'),
            )
            .order_by(DiscountOffer.created_at.desc())
        )
    ).all()
    delivered, history = set(), []
    for user_id, created, extra, telegram_id, username, first_name in offers:
        try:
            value = (extra if isinstance(extra, dict) else {}).get('delivered_at')
            at = datetime.fromisoformat(value) if isinstance(value, str) else None
            if at is None or at.utcoffset() is None or not since <= created <= at <= now:
                continue
        except ValueError:
            continue
        delivered.add(user_id)
        if at >= now - timedelta(days=30):
            label = f'@{username}' if username else (first_name or f'id {telegram_id or user_id}')
            history.append(
                AutoMessageHistoryRow(
                    sent_at=at, user_ref=label if may_see_names else f'Клиент #{user_id}', claimed=None
                )
            )
    sales = (
        (
            await db.execute(
                select(SubscriptionCheckout.sale_snapshot)
                .join(User, User.id == SubscriptionCheckout.user_id)
                .where(
                    people,
                    User.status.is_distinct_from('deleted'),
                    SubscriptionCheckout.fulfilled_at >= since,
                    SubscriptionCheckout.fulfilled_at <= now,
                    SubscriptionCheckout.sale_snapshot['promo_offer_source'].as_string() == 'trial_ending_discount',
                    SubscriptionCheckout.funding_state == 'funded',
                    SubscriptionCheckout.fulfillment_state == 'fulfilled',
                    SubscriptionCheckout.lifecycle_state.in_(('fulfilling', 'ready')),
                    SubscriptionCheckout.tariff_id.not_in(rules.not_paid_tariff_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    savings, totals = [], []
    for snapshot in sales:
        if not isinstance(snapshot, dict) or snapshot.get('promo_offer_source') != 'trial_ending_discount':
            continue
        breakdown = snapshot.get('price_breakdown')
        saving = breakdown.get('promo_offer_discount_kopeks') if isinstance(breakdown, dict) else None
        total = snapshot.get('tariff_total_kopeks')
        if type(saving) is int and saving > 0 and type(total) is int and total > 0:
            savings.append(saving)
            totals.append(total)

    def money(value):
        return settings.format_price(value, round_kopeks=False)

    first = min((row[1] for row in offers), default=since)
    prefix = 'Тестовые аккаунты' if stands else f'С {first.astimezone(ZoneInfo("Europe/Moscow")):%d.%m.%Y}'
    note = f'{prefix}: получили {len(delivered)} · купили со скидкой {len(totals)}'
    if totals:
        note += f' · заплатили {money(sum(totals))} · скидок на {money(sum(savings))}'
    note += '.'
    if not stands:
        if not offers:
            note = 'Клиенты скидку ещё не получали.'
        elif NotificationSettingsService.get_trial_ending_discount_audience() == 1:
            note += ' Раньше из включавших VPN платили около 27 из 100'
            note += ' — пока данных мало, сравнивать рано.' if len(delivered) < 30 else ', считая покупки без скидки.'
    history.sort(key=lambda row: row.sent_at, reverse=True)
    return len(delivered), note, history[:50]


async def _patch_trial_discount(db: AsyncSession, changes: dict[str, Any]) -> None:
    before = NotificationSettingsService.get_config()['trial_ending_discount']
    candidate = {**before, **changes} if isinstance(before, dict) else {}
    if type(candidate.get('discount_percent')) is int and not 1 <= candidate['discount_percent'] <= 30:
        raise HTTPException(422, 'Размер скидки: от 1 до 30%.')
    if not NotificationSettingsService._valid_trial_ending_discount_settings(candidate):
        raise HTTPException(
            422, 'Проверьте поля «Размер скидки», «В каком письме», «Кому», «Скидка действует после пробного».'
        )
    if 'discount_percent' in changes or candidate['enabled']:
        await _assert_discount_is_safe(db, candidate['discount_percent'], max_percent=30)
    if candidate['enabled']:
        prices, _ = await _discount_examples(db, lock=True)
        month = next(
            (row for row in prices.get(str(candidate['discount_percent']), []) if row['period_days'] == 30), None
        )
        if month is None or type(month['price_kopeks']) is not int or month['price_kopeks'] <= 0:
            raise HTTPException(422, 'Касса не назвала цену месяца. Скидка не включена — проверьте тариф.')
    # Awaited price checks must never overwrite another request's successful update.
    if NotificationSettingsService.get_config()['trial_ending_discount'] != before:
        raise HTTPException(409, 'Настройки изменились во время проверки. Обновите карточку и проверьте их снова.')
    if not NotificationSettingsService.set_trial_ending_discount_settings(
        **{field: value for field, value in candidate.items() if field != 'stats_since'}
    ):
        raise HTTPException(500, 'Не удалось сохранить настройки. Прежние значения сохранены.')


async def _discount_item(db: AsyncSession, item: AutoMessageItem, *, with_count: bool = True) -> AutoMessageItem:
    if item.id == 'trial-ending-discount':
        try:
            item.discount_prices, _ = await _discount_examples(db)
        except Exception:
            logger.warning('auto_message_discount_price_unavailable', exc_info=True)
            item.discount_prices = {}
            item.state, item.quiet_reason = 'quiet', 'касса не назвала цену месяца'
        if item.enabled and not item.discount_prices and item.state == 'live':
            item.state, item.quiet_reason = 'quiet', 'касса не назвала цену месяца'
        if with_count:
            item.sent_count, _, _ = await _trial_discount_stats(db)
    return item


def _discount_previews(options: dict, *, short: bool = False) -> list[AutoMessageInsertVariant]:
    from copy import deepcopy

    from app.localization.texts import get_texts
    from app.services.monitoring_service import format_trial_discount_lines, format_trial_regular_price_lines

    config = NotificationSettingsService.get_trial_ending_discount_settings()
    if short:
        return [
            AutoMessageInsertVariant(
                text=format_trial_discount_lines(
                    options, SimpleNamespace(balance_kopeks=0), get_texts('ru'), short=True
                ),
                when=f'скидку уже получил в письме «{_discount_context()["carrier_title"]}»',
            )
        ]
    result = []
    examples = [(0, True, 'баланса нет'), (5000, True, 'есть баланс')]
    if config['audience'] == 1:
        examples = [(balance, used, f'включал VPN и {label}') for balance, used, label in examples]
        examples.append((0, False, 'не включал VPN'))
    for balance, used, label in examples:
        person = SimpleNamespace(balance_kopeks=balance)
        sample = deepcopy(options)
        if not used and config['audience'] == 1:
            for row in sample['price_matrix']:
                for cell in row['prices']:
                    cell['price_kopeks'] += cell['breakdown']['promo_offer_discount_kopeks']
                    cell['breakdown'] = {}
        text = format_trial_discount_lines(sample, person, get_texts('ru'))
        if not text:
            text = format_trial_regular_price_lines(sample, person, get_texts('ru'))
        result.append(AutoMessageInsertVariant(text=text, when=label))
    return result


def _apply_text_change(message_id: str, payload: AutoMessagePatch) -> str | None:
    """Сохраняет или снимает правку текста. Возвращает предупреждение для экрана."""
    if payload.text is None and not payload.reset_text:
        return None

    source_name = _source_name_of(message_id)
    if not source_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='У этого сообщения нет текста, который можно править.',
        )

    if payload.reset_text:
        if payload.text is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Нельзя одновременно сохранить новый текст и вернуть исходный.',
            )
        if not NotificationSettingsService.clear_text_override(source_name):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Не удалось вернуть исходный текст: настройки не записались на диск.',
            )
        return None

    # Сравниваем с ИСХОДНЫМ текстом из кода, а не с показанным: показанный уже может быть
    # чужой правкой, и тогда набор меток поехал бы вслед за ней.
    source = _source_text_of(message_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='У этого сообщения нет текста, который можно править.',
        )
    from app.services.monitoring_service import cabinet_link_suffix

    # Хвост со ссылкой на кабинет бот дописывает ПОСЛЕ текста: без него потолок считался бы
    # по телу, а Телеграм режет письмо целиком.
    suffix = len(cabinet_link_suffix()) if message_id in _CABINET_LINK_IDS else 0
    prepared, warning = _validate_new_text(
        source,
        payload.text,
        suffix_length=suffix,
        tariff_optional=_OPTIONAL_MARKER in _marker_set(_raw_source_text_of(message_id) or ''),
    )
    if message_id not in _WITH_LOGO_IDS:
        # Обещать «уйдёт без логотипа» письму, которое логотипа не знает, — обещать потерю
        # того, чего не бывает.
        warning = None
    if not NotificationSettingsService.set_text_override(source_name, prepared):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Не удалось сохранить текст: настройки не записались на диск.',
        )
    return warning


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

    # 🔴 Текст и настройки сохраняются ПО ОТДЕЛЬНОСТИ. Иначе запрос, упавший на числовой
    # половине, оставлял бы текст уже записанным, а владелец видел бы отказ на изменение,
    # которое легло. Кабинет их вместе и не шлёт — забор закрывает путь через API.
    if (payload.text is not None or payload.reset_text) and _has_numeric_change(payload):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Текст письма и настройки сохраняются по отдельности.',
        )

    # Текст правится ДО забора «нет настроек в боте»: у бонусных дней выключателя нет
    # вовсе (`control: 'server'`), но текст письма у них есть, и править его можно.
    text_warning = _apply_text_change(message_id, payload)

    settings_key = entry.get('settings_key')
    if payload.text is not None or payload.reset_text:
        reasons, notes = await _quiet_facts(db)
        item = _build_item(entry, reasons, notes, await _sent_counts(db), await _claimed_counts(db))
        item.text_warning = text_warning
        # Правка уходит живым клиентам — в журнале обязана остаться строка. Ранний возврат
        # не доходит до общей записи ниже, и без этой строки след оставался бы только в
        # журнале прав кабинета.
        logger.info(
            'auto_message_text_changed',
            message_id=message_id,
            reset=payload.reset_text,
            length=len(payload.text or ''),
        )
        return item

    if entry['control'] != 'toggle' or not settings_key:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Этим сообщением из кабинета управлять нельзя: у него нет настроек в боте.',
        )

    allowed = set(entry.get('params') or ())
    # Правка текста уже применена выше и в числовые изменения не входит. Без этого
    # `reset_text=False` (это НЕ None) всегда попадал бы в `changes`, и пустой запрос
    # переставал бы отбиваться — то есть экран говорил бы «сохранено» ни на чём.
    changes = {
        field: value
        for field, value in payload.model_dump(exclude_none=True).items()
        if field not in ('text', 'reset_text')
    }
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

    if settings_key == 'trial_ending_discount':
        await _patch_trial_discount(db, changes)
        logger.info('auto_messages_patched', message_id=message_id, changes=sorted(changes))
        reasons, notes = await _quiet_facts(db)
        return await _discount_item(
            db, _build_item(entry, reasons, notes, await _sent_counts(db), await _claimed_counts(db))
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
    if 'connect_reminder_minutes' in changes:
        _assert_in_range(changes['connect_reminder_minutes'], 5, 60, FIELD_TITLES['connect_reminder_minutes'])
    if 'not_connected_after_hours' in changes:
        _assert_in_range(
            changes['not_connected_after_hours'],
            MIN_NOT_CONNECTED_HOURS,
            MAX_NOT_CONNECTED_HOURS,
            FIELD_TITLES['not_connected_after_hours'],
        )

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

"""Классификатор состояния пользователя в воронке (для меню по состояниям).

Единый источник истины: клавиатура главного меню и текст-статус опираются на ОДНУ
подписку (``user.subscription`` через ``get_subscriber_state``), чтобы кнопки и подпись
не расходились.

ВАЖНО: активный триал в БД хранится со status=ACTIVE (не TRIAL), поэтому
``Subscription.actual_status`` для него возвращает 'active'. Различать триал и платную
можно ТОЛЬКО по флагу ``is_trial``. Платное под-состояние (PAID_*) считается по
``actual_status`` выбранной подписки; триал-ветки — по списку ``user.subscriptions``.
"""

from enum import Enum

from app.config import settings


class FunnelState(str, Enum):
    NEWBIE = 'newbie'  # совсем новый: ни триала, ни покупки
    TRIAL_ACTIVE = 'trial_active'  # активный триал (включая limited — исчерпан трафик)
    TRIAL_EXPIRED = 'trial_expired'  # триал закончился, платной не было
    # Платный подписчик — своё меню (флаг FUNNEL_SUBSCRIBER_MENU_ENABLED):
    PAID_ACTIVE = 'paid_active'  # активная платная, до конца больше порога
    PAID_EXPIRING = 'paid_expiring'  # активная платная, до конца <= порога
    PAID_EXPIRED = 'paid_expired'  # платная закончилась
    OTHER = 'other'  # прочее — funnel-меню НЕ применяется


# Активные статусы (подписка ещё «живая»)
_ALIVE_STATUSES = {'active', 'trial', 'limited'}
# Статусы «закончилась». ВАЖНО: 'disabled' сюда НЕ входит — это обратимое
# состояние (напр. временное отключение за отписку от канала), подписка ещё жива
# и реактивируется. Считать его «триал закончился» нельзя.
_DEAD_STATUSES = {'expired'}


def get_subscriber_state(user):
    """Платное под-состояние подписчика → (state, subscription).

    Возвращает (FunnelState.PAID_*, sub) для активного/истекающего/истёкшего платника,
    либо (None, sub|None) — тогда меню подписчика НЕ применяется (вызывающий отдаёт
    обычное меню). ЕДИНЫЙ ИСТОЧНИК: смотрим на ОДНУ подписку ``user.subscription`` —
    ту же, что использует текст-статус (_get_subscription_status), чтобы кнопки и
    подпись не расходились.

    Гейты: свой флаг FUNNEL_SUBSCRIBER_MENU_ENABLED + защита от мультитарифа
    (одна «Моя ссылка» не отражает несколько подписок).
    Тонкости: триал (is_trial) — не наша зона; НЕ-триальную подписку классифицируем
    ПО СТАТУСУ (флаг has_had_paid не требуется — активная/истёкшая платная = подписчик):
    'disabled' обратимо → обычное меню; 'expired' → PAID_EXPIRED;
    'active' обычного тарифа → PAID_ACTIVE/PAID_EXPIRING по порогу дней;
    'limited' (исчерпан трафик) и СУТОЧНЫЙ тариф → всегда PAID_ACTIVE (без форс-CTA «Продлить»).
    """
    if user is None:
        return None, None
    if not settings.is_funnel_subscriber_menu_enabled():
        return None, None
    if settings.is_multi_tariff_enabled():
        return None, None

    sub = getattr(user, 'subscription', None)
    if sub is None:
        return None, None
    if bool(getattr(sub, 'is_trial', False)):
        return None, sub

    status = (getattr(sub, 'actual_status', '') or '').lower()

    if status == 'disabled':
        return None, sub
    if status == 'expired':
        return FunnelState.PAID_EXPIRED, sub
    if status in ('active', 'limited'):
        # 'limited' (исчерпан трафик) и СУТОЧНЫЙ тариф (списывается ежедневно, days_left≈0)
        # → активное меню без форс-CTA «Продлить»: продление периода тут не по адресу
        # (для суточного это совпадает с текстом-статусом SUB_STATUS_DAILY_ACTIVE).
        if status == 'limited' or bool(getattr(sub, 'is_daily_tariff', False)):
            return FunnelState.PAID_ACTIVE, sub
        threshold = settings.get_subscriber_menu_renew_threshold_days()
        days_left = int(getattr(sub, 'days_left', 0) or 0)
        if days_left <= threshold:
            return FunnelState.PAID_EXPIRING, sub
        return FunnelState.PAID_ACTIVE, sub
    return None, sub


def classify_funnel_state(user) -> FunnelState:
    """Определяет состояние воронки по подпискам пользователя.

    Триал-ветки считаем по ``user.subscriptions`` (всему списку), а платное
    под-состояние — через ``get_subscriber_state`` (по ``user.subscription``,
    тот же ряд, что и текст-статус), чтобы кнопки и подпись не расходились.
    """
    if user is None:
        return FunnelState.OTHER

    has_had_paid = bool(getattr(user, 'has_had_paid_subscription', False))
    subscriptions = list(getattr(user, 'subscriptions', None) or [])

    # Совсем новый: нет ни одной подписки и никогда не платил
    if not subscriptions and not has_had_paid:
        return FunnelState.NEWBIE

    # Платный подписчик: своё меню по под-состоянию (если фича включена)
    paid_state, _ = get_subscriber_state(user)
    if paid_state is not None:
        return paid_state

    # Платил когда-либо → платный путь, funnel-меню (триал) не для него
    if has_had_paid:
        return FunnelState.OTHER

    trial_active = False
    trial_expired = False
    has_any_alive = False

    for sub in subscriptions:
        status = (getattr(sub, 'actual_status', '') or '').lower()
        is_trial = bool(getattr(sub, 'is_trial', False))
        if status in _ALIVE_STATUSES:
            has_any_alive = True
            if is_trial:
                trial_active = True
        elif status in _DEAD_STATUSES and is_trial:
            trial_expired = True

    # Активный триал имеет приоритет
    if trial_active:
        return FunnelState.TRIAL_ACTIVE
    # Активная НЕ-триальная подписка доходит сюда лишь когда меню подписчика выключено
    # (при включённом флаге её раньше перехватывает get_subscriber_state). Отдаём обычное меню.
    if has_any_alive:
        return FunnelState.OTHER
    # Только закончившийся триал
    if trial_expired:
        return FunnelState.TRIAL_EXPIRED

    return FunnelState.OTHER

"""Классификатор состояния пользователя в воронке (для меню по состояниям).

Единый источник истины: и клавиатура главного меню, и (в будущем) текст-баннер
должны опираться на эту функцию, чтобы кнопки и подпись не расходились.

ВАЖНО: активный триал в БД хранится со status=ACTIVE (не TRIAL), поэтому
``Subscription.actual_status`` для него возвращает 'active'. Различать триал и
платную можно ТОЛЬКО по флагу ``is_trial``. Поэтому классификация идёт по флагу
``is_trial`` + ``actual_status`` + ``has_had_paid_subscription``.
"""

from enum import Enum


class FunnelState(str, Enum):
    NEWBIE = 'newbie'  # совсем новый: ни триала, ни покупки
    TRIAL_ACTIVE = 'trial_active'  # активный триал (включая limited — исчерпан трафик)
    TRIAL_EXPIRED = 'trial_expired'  # триал закончился, платной не было
    OTHER = 'other'  # платная/прочее — funnel-меню НЕ применяется


# Активные статусы (подписка ещё «живая»)
_ALIVE_STATUSES = {'active', 'trial', 'limited'}
# Статусы «закончилась». ВАЖНО: 'disabled' сюда НЕ входит — это обратимое
# состояние (напр. временное отключение за отписку от канала), подписка ещё жива
# и реактивируется. Считать его «триал закончился» нельзя.
_DEAD_STATUSES = {'expired'}


def classify_funnel_state(user) -> FunnelState:
    """Определяет состояние воронки по всем подпискам пользователя.

    Считаем по ``user.subscriptions`` (всему списку), а не по property
    ``user.subscription``, потому что у пользователя может быть несколько рядов
    (например истёкший триал + что-то), и property может вернуть «не тот».
    """
    if user is None:
        return FunnelState.OTHER

    has_had_paid = bool(getattr(user, 'has_had_paid_subscription', False))
    subscriptions = list(getattr(user, 'subscriptions', None) or [])

    # Совсем новый: нет ни одной подписки и никогда не платил
    if not subscriptions and not has_had_paid:
        return FunnelState.NEWBIE

    # Платил когда-либо → это платный путь, funnel-меню не для него
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
    # Есть активная НЕ-триальная подписка без paid-флага — редкий случай, не трогаем
    if has_any_alive:
        return FunnelState.OTHER
    # Только закончившийся триал
    if trial_expired:
        return FunnelState.TRIAL_EXPIRED

    return FunnelState.OTHER

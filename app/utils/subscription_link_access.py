"""Единая проверка, можно ли показать пользователю прямую ссылку подписки.

Это UI- и handler-гейт, а не проверка оплаты: бесплатные подписки и триалы
тоже могут давать VPN-доступ. Правило намеренно опирается только на текущий
доступ, готовность ссылки и глобальную настройку её скрытия.
"""

from datetime import UTC, datetime

from app.config import settings
from app.database.models import _aware
from app.utils.grace import is_in_grace
from app.utils.subscription_utils import get_display_subscription_link


def has_active_subscription_connection(subscription) -> bool:
    """Возвращает True, когда подписка ещё вправе получить конфигурацию.

    Это гейт фактического VPN-доступа для bot/mini-app путей. Он намеренно НЕ
    учитывает ``HIDE_SUBSCRIPTION_LINK``: эта настройка скрывает сырой URL, но
    штатный экран подключения может продолжать отдавать crypt/deep-link.

    ``limited`` требует неистёкший ``end_date``: ``actual_status`` модели
    возвращает limited раньше проверки срока. Grace допустим только для реально
    истёкшей НЕ-триальной подписки; отключенная подписка, даже с устаревшим
    флагом ``in_grace``, доступа не получает.
    """
    if subscription is None:
        return False

    actual_status = str(getattr(subscription, 'actual_status', '') or '').lower()
    if actual_status in {'active', 'trial'}:
        has_vpn_access = True
    elif actual_status == 'limited':
        end_date = _aware(getattr(subscription, 'end_date', None))
        has_vpn_access = end_date is not None and end_date > datetime.now(UTC)
    else:
        has_vpn_access = (
            actual_status == 'expired'
            and getattr(subscription, 'is_trial', True) is False
            and is_in_grace(subscription)
        )
    if not has_vpn_access:
        return False

    return bool(get_display_subscription_link(subscription))


def has_available_subscription_link(subscription) -> bool:
    """Возвращает True, когда открыть прямую ссылку подписки безопасно.

    Ограничение ``HIDE_SUBSCRIPTION_LINK`` применяется не только при рендере
    кнопки, но и при обработке старого callback. Это не позволяет показать URL,
    если настройку скрытия включили уже после отправки меню.

    Это более узкое условие, чем доступ к экрану подключения:
    ``HIDE_SUBSCRIPTION_LINK`` скрывает именно прямой URL и старые direct-callback.
    """
    if settings.should_hide_subscription_link():
        return False
    return has_active_subscription_connection(subscription)


def get_user_subscription_with_available_link(user):
    """Возвращает единственную подписку пользователя, пригодную для прямой ссылки.

    В multi-tariff режиме намеренно возвращает ``None``: одна bare-кнопка не
    может безопасно выбрать нужный тариф. Для такого режима нужен отдельный
    экран выбора подписки с callback, содержащим её ID.
    """
    if user is None or settings.is_multi_tariff_enabled():
        return None

    subscription = getattr(user, 'subscription', None)
    return subscription if has_available_subscription_link(subscription) else None

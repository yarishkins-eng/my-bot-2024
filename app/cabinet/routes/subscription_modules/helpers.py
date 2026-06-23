"""Shared helper functions for subscription modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database.models import Tariff

from app.database.models import Subscription, User

from ...schemas.subscription import (
    ServerInfo,
    SubscriptionResponse,
)


logger = structlog.get_logger(__name__)


async def resolve_subscription(
    db: AsyncSession,
    user: User,
    subscription_id: int | None,
) -> Subscription | None:
    """Resolve target subscription: by ID in multi-tariff mode, or legacy fallback.

    Args:
        db: Database session.
        user: Current user.
        subscription_id: Optional subscription ID (from query param).

    Returns:
        Target Subscription or None if not found.

    Raises:
        HTTPException: If subscription_id provided but not found for this user.
    """
    from fastapi import HTTPException

    from app.database.crud.subscription import get_subscription_by_id_for_user

    if subscription_id and settings.is_multi_tariff_enabled():
        subscription = await get_subscription_by_id_for_user(db, subscription_id, user.id)
        if not subscription:
            raise HTTPException(status_code=404, detail='Subscription not found')
        return subscription

    if settings.is_multi_tariff_enabled() and not subscription_id:
        from app.database.crud.subscription import get_active_subscriptions_by_user_id

        active_subs = await get_active_subscriptions_by_user_id(db, user.id)
        if active_subs:
            non_daily = [s for s in active_subs if not getattr(s, 'is_daily_tariff', False)]
            pool = non_daily or active_subs
            return max(pool, key=lambda s: s.days_left)
        return None

    await db.refresh(user, ['subscriptions'])
    return user.subscription


def _get_addon_discount_percent(
    user: User | None,
    category: str,
    period_days_hint: int | None = None,
) -> int:
    """Get addon discount percent for user — delegates to PricingEngine."""
    from app.services.pricing_engine import PricingEngine

    return PricingEngine.get_addon_discount_percent(user, category, period_days_hint)


def _apply_addon_discount(
    user: User,
    category: str,
    amount: int,
    period_days: int | None = None,
) -> dict[str, int]:
    """Apply addon discount to amount.

    Returns dict with keys: discounted, discount, percent
    """
    from app.utils.pricing_utils import apply_percentage_discount

    percent = _get_addon_discount_percent(user, category, period_days)
    if percent <= 0 or amount <= 0:
        return {'discounted': amount, 'discount': 0, 'percent': 0}

    discounted_amount, discount_value = apply_percentage_discount(amount, percent)
    return {
        'discounted': discounted_amount,
        'discount': discount_value,
        'percent': percent,
    }


def _resolve_device_addon_price(tariff: Tariff | None) -> tuple[int, int | None]:
    """Effective per-device add-on price (kopeks) + max device limit.

    Single source shared by ``GET /subscription/devices/price`` and the
    subscription-response top-up gate so the two never drift: a tariff's own
    ``device_price_kopeks`` when set, otherwise the global
    ``settings.PRICE_PER_DEVICE``; max limit from the tariff or the global
    ``settings.MAX_DEVICES_LIMIT`` (0 → no limit).
    """
    if tariff is not None and tariff.device_price_kopeks is not None:
        return tariff.device_price_kopeks, tariff.max_device_limit
    max_limit = settings.MAX_DEVICES_LIMIT if settings.MAX_DEVICES_LIMIT > 0 else None
    return settings.PRICE_PER_DEVICE, max_limit


def compute_device_topup_gate(subscription: Subscription, tariff: Tariff | None) -> tuple[bool, int]:
    """Whether the user may buy more device slots, plus the effective per-device price.

    Mirrors the availability gates of ``GET /subscription/devices/price``:

    * effective price ``<= 0`` → top-up disabled for this plan (hide the button);
    * ``device_limit`` already at the tariff/global max → no room to add more.

    Returns ``(can_topup, device_price_kopeks)``. In the at-max case the price is
    still returned (so the caller has context) but the gate is closed.
    """
    device_price, max_device_limit = _resolve_device_addon_price(tariff)
    if not device_price or device_price <= 0:
        return False, 0
    current_devices = subscription.device_limit or 1
    if max_device_limit and current_devices >= max_device_limit:
        return False, device_price
    return True, device_price


def compute_traffic_topup_gate(subscription: Subscription, tariff: Tariff | None) -> bool:
    """Whether traffic top-up is available — tracks ``GET /subscription/traffic-packages``
    so the screen's gate matches what that endpoint would return.

    * Tariff mode (tariffs sales-mode AND the sub has a tariff): the tariff's own
      :meth:`Tariff.can_topup_traffic` (topup enabled AND packages exist AND not
      unlimited), per the Чат-1 directive to reuse that helper rather than raw
      columns. (It checks that packages *exist*, not that any is priced > 0 — a
      degenerate all-zero-price tariff package would show the button; that is a
      misconfiguration the shared helper deliberately doesn't distinguish.)
    * Classic mode (classic sales-mode, or any sub WITHOUT a tariff): the global
      ``TRAFFIC_TOPUP_ENABLED`` toggle plus a configured global package (priced > 0)
      — and, when a tariff is attached, its ``allow_traffic_topup`` flag.
      ``TRAFFIC_TOPUP_ENABLED`` defaults to True, so a no-tariff sub CAN top up
      traffic; returning ``False`` here would wrongly hide a working button.
    """
    if settings.is_tariffs_mode() and subscription.tariff_id:
        return bool(tariff.can_topup_traffic()) if tariff is not None else False
    if not settings.is_traffic_topup_enabled():
        return False
    if subscription.tariff_id and tariff is not None and not getattr(tariff, 'allow_traffic_topup', True):
        return False
    return any(pkg.get('enabled', True) and pkg.get('price', 0) > 0 for pkg in settings.get_traffic_topup_packages())


def _subscription_to_response(
    subscription: Subscription,
    servers: list[ServerInfo] | None = None,
    tariff_name: str | None = None,
    traffic_purchases: list[dict[str, Any]] | None = None,
    user: User | None = None,
    disabled_reason_hint: str | None = None,
) -> SubscriptionResponse:
    """Convert Subscription model to response."""
    now = datetime.now(UTC)

    # Use actual_status property for correct status (same as bot uses)
    actual_status = subscription.actual_status
    is_expired = actual_status == 'expired'
    is_active = actual_status in ('active', 'trial')
    is_limited = actual_status == 'limited'

    # Calculate time remaining
    days_left = 0
    hours_left = 0
    minutes_left = 0
    time_left_display = ''

    if subscription.end_date and not is_expired:
        time_delta = subscription.end_date - now
        total_seconds = max(0, int(time_delta.total_seconds()))

        days_left = total_seconds // 86400  # 86400 seconds in a day
        remaining_seconds = total_seconds % 86400
        hours_left = remaining_seconds // 3600
        minutes_left = (remaining_seconds % 3600) // 60

        # Create human-readable display
        if days_left > 0:
            time_left_display = f'{days_left}d {hours_left}h'
        elif hours_left > 0:
            time_left_display = f'{hours_left}h {minutes_left}m'
        elif minutes_left > 0:
            time_left_display = f'{minutes_left}m'
        else:
            time_left_display = '0m'
    else:
        time_left_display = '0m'

    traffic_limit_gb = subscription.traffic_limit_gb or 0
    traffic_used_gb = subscription.traffic_used_gb or 0.0

    if traffic_limit_gb > 0:
        traffic_used_percent = min(100, (traffic_used_gb / traffic_limit_gb) * 100)
    else:
        traffic_used_percent = 0

    # Check if this is a daily tariff
    is_daily_paused = getattr(subscription, 'is_daily_paused', False) or False
    tariff_id = getattr(subscription, 'tariff_id', None)

    # Use subscription's is_daily_tariff property if available
    is_daily = False
    daily_price_kopeks = None

    if hasattr(subscription, 'is_daily_tariff'):
        is_daily = subscription.is_daily_tariff
    elif tariff_id and hasattr(subscription, 'tariff') and subscription.tariff:
        is_daily = getattr(subscription.tariff, 'is_daily', False)

    # Get daily_price_kopeks, tariff_name, traffic_reset_mode from tariff
    traffic_reset_mode = None
    if tariff_id and hasattr(subscription, 'tariff') and subscription.tariff:
        daily_price_kopeks = getattr(subscription.tariff, 'daily_price_kopeks', None)
        # Применяем скидку промогруппы + promo-offer для отображения
        if daily_price_kopeks and daily_price_kopeks > 0 and user:
            from app.services.pricing_engine import PricingEngine
            from app.utils.promo_offer import get_user_active_promo_discount_percent

            _promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
            _group_pct = _promo_group.get_discount_percent('period', 1) if _promo_group else 0
            _offer_pct = get_user_active_promo_discount_percent(user)
            if _group_pct > 0 or _offer_pct > 0:
                daily_price_kopeks, _, _ = PricingEngine.apply_stacked_discounts(
                    daily_price_kopeks, _group_pct, _offer_pct
                )
        if not tariff_name:  # Only set if not passed as parameter
            tariff_name = getattr(subscription.tariff, 'name', None)
        traffic_reset_mode = (
            getattr(subscription.tariff, 'traffic_reset_mode', None) or settings.DEFAULT_TRAFFIC_RESET_STRATEGY
        )

    # Calculate next daily charge time (24 hours after last charge)
    next_daily_charge_at = None
    if is_daily and not is_daily_paused:
        last_charge = getattr(subscription, 'last_daily_charge_at', None)
        if last_charge:
            next_charge = last_charge + timedelta(days=1)
            # Если время списания уже прошло — не показываем (DailySubscriptionService обработает)
            if next_charge > datetime.now(UTC):
                next_daily_charge_at = next_charge

    # Проверяем настройку скрытия ссылки (скрывается только текст, кнопки работают)
    hide_link = settings.should_hide_subscription_link()

    # Redesigned-cabinet fields. Read the tariff from the already-loaded
    # relationship via __dict__ (NEVER lazy-load here: this is a sync builder
    # called from several async handlers; status.py sets subscription.tariff,
    # classic/unloaded paths yield None → gates hidden, the safe default).
    _tariff = subscription.__dict__.get('tariff')
    can_topup_devices, device_addon_price_kopeks = compute_device_topup_gate(subscription, _tariff)
    can_topup_traffic = compute_traffic_topup_gate(subscription, _tariff)
    restriction_subscription = bool(getattr(user, 'restriction_subscription', False))

    return SubscriptionResponse(
        id=subscription.id,
        status=actual_status,  # Use actual_status instead of raw status
        is_trial=subscription.is_trial or actual_status == 'trial',
        start_date=subscription.start_date,
        end_date=subscription.end_date,
        days_left=days_left,
        hours_left=hours_left,
        minutes_left=minutes_left,
        time_left_display=time_left_display,
        traffic_limit_gb=traffic_limit_gb,
        traffic_used_gb=round(traffic_used_gb, 2),
        traffic_used_percent=round(traffic_used_percent, 1),
        device_limit=subscription.device_limit or 0,
        connected_squads=subscription.connected_squads or [],
        servers=servers or [],
        autopay_enabled=subscription.autopay_enabled or False,
        autopay_days_before=subscription.autopay_days_before or 3,
        subscription_url=subscription.subscription_url,
        hide_subscription_link=hide_link,
        is_active=is_active,
        is_expired=is_expired,
        is_limited=is_limited,
        traffic_purchases=traffic_purchases or [],
        is_daily=is_daily,
        is_daily_paused=is_daily_paused,
        daily_price_kopeks=daily_price_kopeks,
        next_daily_charge_at=next_daily_charge_at,
        tariff_id=tariff_id,
        tariff_name=tariff_name,
        traffic_reset_mode=traffic_reset_mode,
        can_topup_devices=can_topup_devices,
        device_addon_price_kopeks=device_addon_price_kopeks,
        can_topup_traffic=can_topup_traffic,
        restriction_subscription=restriction_subscription,
        disabled_reason_hint=disabled_reason_hint,
    )

"""Strict eligibility and option normalization for device-first checkout."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.database.models import Subscription, Tariff


MAX_DEVICE_FIRST_DEVICE_LIMIT = 10


class DeviceFirstConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DeviceFirstEligibility:
    eligible: bool
    reason: str
    tariff: Tariff | None = None
    device_options: tuple[int, ...] = ()
    period_options: tuple[int, ...] = ()
    default_period_days: int | None = None


def normalize_device_purchase_options(
    raw: object,
    *,
    base_device_limit: int,
    max_device_limit: int | None,
    device_price_kopeks: int | None,
) -> list[int] | None:
    """Validate the explicit admin contract; NULL is the only legacy value."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise DeviceFirstConfigurationError('device_purchase_options must be NULL or a non-empty list')
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw):
        raise DeviceFirstConfigurationError('device_purchase_options must contain integers only')
    if any(value <= 0 for value in raw):
        raise DeviceFirstConfigurationError('device_purchase_options must contain positive limits')
    if base_device_limit > MAX_DEVICE_FIRST_DEVICE_LIMIT or any(
        value > MAX_DEVICE_FIRST_DEVICE_LIMIT for value in raw
    ):
        raise DeviceFirstConfigurationError(
            f'device-first supports no more than {MAX_DEVICE_FIRST_DEVICE_LIMIT} devices'
        )
    if raw != sorted(raw) or len(raw) != len(set(raw)):
        raise DeviceFirstConfigurationError('device_purchase_options must be unique and strictly increasing')
    if base_device_limit not in raw:
        raise DeviceFirstConfigurationError('device_purchase_options must include the tariff base device limit')
    if any(value < base_device_limit for value in raw):
        raise DeviceFirstConfigurationError(
            'device_purchase_options cannot be below the tariff base device limit'
        )
    if max_device_limit is not None and any(value > max_device_limit for value in raw):
        raise DeviceFirstConfigurationError('device_purchase_options cannot exceed max_device_limit')
    if any(value > base_device_limit for value in raw) and (device_price_kopeks is None or device_price_kopeks <= 0):
        raise DeviceFirstConfigurationError(
            'options above the base limit require this tariff own positive device_price_kopeks'
        )
    return list(raw)


def period_options(tariff: Tariff) -> tuple[int, ...]:
    values: list[int] = []
    for raw_days, raw_price in (tariff.period_prices or {}).items():
        try:
            days, price = int(raw_days), int(raw_price)
        except (TypeError, ValueError):
            continue
        if days > 0 and price >= 0:
            values.append(days)
    return tuple(sorted(set(values)))


def device_options_for_subscription(tariff: Tariff, subscription: Subscription | None) -> tuple[int, ...]:
    configured = normalize_device_purchase_options(
        tariff.device_purchase_options,
        base_device_limit=int(tariff.device_limit or 0),
        max_device_limit=tariff.max_device_limit,
        device_price_kopeks=tariff.device_price_kopeks,
    )
    if configured is None:
        return ()
    if subscription is None or subscription.is_trial:
        return tuple(configured)

    current = int(subscription.device_limit or tariff.device_limit or 0)
    if current > MAX_DEVICE_FIRST_DEVICE_LIMIT:
        raise DeviceFirstConfigurationError(
            f'device-first supports no more than {MAX_DEVICE_FIRST_DEVICE_LIMIT} devices'
        )
    # A paid subscription can be extended onto a different tariff. Its current
    # device limit is still the floor: device-first must never quietly turn an
    # extension into a device-limit downgrade just because the previous tariff
    # differs from the one currently offered.
    #
    # Paid users are grandfathered: the current limit remains selectable even
    # if an admin later removes it or lowers max_device_limit.
    allowed = {current}
    effective_max = tariff.max_device_limit
    allowed.update(
        value for value in configured if value >= current and (effective_max is None or value <= effective_max)
    )
    return tuple(sorted(allowed))


def tariff_eligibility(
    tariff: Tariff,
    *,
    subscription: Subscription | None = None,
    multi_tariff_enabled: bool | None = None,
) -> DeviceFirstEligibility:
    if settings.SALES_MODE != 'tariffs':
        return DeviceFirstEligibility(False, 'legacy_sales_mode')
    if multi_tariff_enabled if multi_tariff_enabled is not None else settings.is_multi_tariff_enabled():
        return DeviceFirstEligibility(False, 'multi_tariff_enabled')
    if not tariff.is_active:
        return DeviceFirstEligibility(False, 'tariff_inactive')
    if tariff.is_daily:
        return DeviceFirstEligibility(False, 'daily_tariff')
    if tariff.custom_days_enabled:
        return DeviceFirstEligibility(False, 'custom_days_tariff')
    if tariff.custom_traffic_enabled:
        return DeviceFirstEligibility(False, 'custom_traffic_tariff')
    periods = period_options(tariff)
    if not periods:
        return DeviceFirstEligibility(False, 'no_fixed_periods')
    try:
        devices = device_options_for_subscription(tariff, subscription)
    except DeviceFirstConfigurationError:
        return DeviceFirstEligibility(False, 'invalid_device_purchase_options')
    if not devices:
        return DeviceFirstEligibility(False, 'device_purchase_options_not_configured')
    default = 30 if 30 in periods else periods[0]
    return DeviceFirstEligibility(True, 'eligible', tariff, devices, periods, default)


def resolve_single_eligible_tariff(
    tariffs: list[Tariff],
    *,
    subscription: Subscription | None = None,
) -> DeviceFirstEligibility:
    matches = [tariff_eligibility(t, subscription=subscription) for t in tariffs]
    eligible = [item for item in matches if item.eligible]
    if len(eligible) != 1:
        return DeviceFirstEligibility(False, 'eligible_tariff_count_not_one')
    return eligible[0]

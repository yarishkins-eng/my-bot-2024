"""One-shot trial offers; their transaction never owns a caller's notification lock."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.discount_offer import get_latest_claimed_offer_for_user
from app.database.crud.promo_offer_log import log_promo_offer_action
from app.database.crud.tariff import get_trial_tariff
from app.database.database import AsyncSessionLocal
from app.database.models import DiscountOffer, SentNotification, Subscription, User, UserPromoGroup
from app.services.device_first_checkout_service import build_purchase_options
from app.services.notification_settings_service import NotificationSettingsService
from app.services.user_service import is_test_account


logger = structlog.get_logger(__name__)
TRIAL_ENDING_DISCOUNT_SOURCE = 'trial_ending_discount'
TRIAL_ENDING_DISCOUNT_ROLLOUT = 'stands'
_MOSCOW = ZoneInfo('Europe/Moscow')
_PROMO_FIELDS = ('promo_offer_discount_percent', 'promo_offer_discount_source', 'promo_offer_discount_expires_at')


@dataclass(frozen=True)
class TrialEndingDiscountResult:
    offer_id: int
    user_id: int
    subscription_id: int
    panel_uuid: str
    panel_end: datetime
    reset_started_at: datetime | None
    discount_percent: int
    expires_at: datetime
    lifetime_bytes: int | None = None


def _aware_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        return parsed.astimezone(UTC) if isinstance(parsed, datetime) and parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _settings_snapshot(carrier: int) -> tuple | None:
    config = NotificationSettingsService.get_trial_ending_discount_settings()
    if not config.get('enabled') or config.get('carrier') != carrier:
        return None
    key = {0: 'trial_2h', 24: 'trial_expiring_24h', 48: 'trial_expiring_48h'}.get(carrier)
    if key is None or not NotificationSettingsService.is_enabled(key):
        return None
    if not NotificationSettingsService.are_notifications_globally_enabled():
        return None
    if carrier and not (settings.WEBHOOK_NOTIFY_USER_ENABLED and settings.WEBHOOK_NOTIFY_SUB_EXPIRING):
        return None
    return (
        tuple(sorted(config.items())),
        NotificationSettingsService.get_trial_warn_hours(),
        key,
    )


def _eligible_person(user: User | None, reset_started_at) -> bool:
    return bool(
        user is not None
        and user.status == 'active'
        and user.telegram_id
        and not user.has_had_paid_subscription
        and not user.restriction_subscription
        and user.account_erasure_requested_at is None
        and user.account_erased_at is None
        and user.test_reset_state not in ('resetting', 'failed')
        and user.test_reset_started_at == reset_started_at
    )


def _matches_term(user, subscription, panel_uuid, panel_end) -> bool:
    return bool(
        subscription is not None
        and subscription.user_id == user.id
        and subscription.is_trial
        and subscription.status in ('active', 'limited')
        and (subscription.remnawave_uuid or user.remnawave_uuid) == panel_uuid
        and subscription.end_date == panel_end
        and panel_end > datetime.now(UTC)
    )


def _event_window(end: datetime, carrier: int, warn_hours: int) -> bool:
    now = datetime.now(UTC)
    if carrier == 0:
        return now < end <= now + timedelta(hours=warn_hours)
    return now + timedelta(hours=carrier - 1) <= end <= now + timedelta(hours=carrier, minutes=1)


def _discount_expiry(end: datetime, hours: int) -> datetime:
    expiry = (end + timedelta(hours=hours)).astimezone(_MOSCOW)
    if expiry.hour < 9:
        expiry = expiry.replace(hour=9, minute=0, second=0, microsecond=0)
    return expiry.astimezone(UTC)


def _positive_options(options: dict) -> bool:
    prices = [cell.get('price_kopeks') for row in options.get('price_matrix', []) for cell in row.get('prices', [])]
    return (
        options.get('eligible') is True and bool(prices) and all(type(price) is int and price > 0 for price in prices)
    )


async def prepare_trial_ending_discount(
    *, user_id, subscription_id, carrier, panel_uuid, panel_end, lifetime_bytes, reset_started_at
) -> TrialEndingDiscountResult | None:
    """Call before the outer lifecycle User lock; all HTTP evidence is already bound to this UUID."""
    if type(carrier) is not int or carrier not in (0, 24, 48) or not isinstance(panel_uuid, str) or not panel_uuid:
        return None
    panel_end = _aware_time(panel_end)
    snapshot = _settings_snapshot(carrier)
    if panel_end is None or snapshot is None:
        return None
    config = dict(snapshot[0])
    if config['audience'] == 1 and (type(lifetime_bytes) is not int or lifetime_bytes < 1_000_000):
        logger.info('trial_ending_discount_skipped', user_id=user_id, reason='unknown_or_unused_panel_traffic')
        return None
    try:
        async with AsyncSessionLocal() as db, db.begin():
            user = await db.scalar(
                select(User)
                .where(User.id == user_id)
                .options(
                    selectinload(User.promo_group),
                    selectinload(User.user_promo_groups).selectinload(UserPromoGroup.promo_group),
                )
                .with_for_update()
            )
            rolled_out = TRIAL_ENDING_DISCOUNT_ROLLOUT == 'all' or (
                TRIAL_ENDING_DISCOUNT_ROLLOUT == 'stands' and user is not None and is_test_account(user)
            )
            if not rolled_out or not _eligible_person(user, reset_started_at):
                return None
            subscription = await db.scalar(
                select(Subscription).where(Subscription.id == subscription_id).with_for_update()
            )
            if subscription is None or abs((subscription.end_date - panel_end).total_seconds()) > 1:
                return None
            current_id = await db.scalar(
                select(Subscription.id)
                .where(Subscription.user_id == user_id)
                .order_by(
                    case((Subscription.status == 'active', 0), (Subscription.status == 'trial', 1), else_=2),
                    Subscription.end_date.desc().nulls_last(),
                    Subscription.created_at.desc(),
                )
                .limit(1)
            )
            if current_id != subscription_id:
                return None
            panel_end = subscription.end_date  # Freeze the precise local term, not the panel's rounded timestamp.
            trial = await get_trial_tariff(db)
            if (
                not _matches_term(user, subscription, panel_uuid, panel_end)
                or trial is None
                or subscription.tariff_id != trial.id
                or not _event_window(panel_end, carrier, snapshot[1])
            ):
                return None
            now = datetime.now(UTC)
            if user.promo_offer_discount_percent > 0 and (
                user.promo_offer_discount_expires_at is None or user.promo_offer_discount_expires_at > now
            ):
                return None
            if await db.scalar(
                select(DiscountOffer.id)
                .where(
                    DiscountOffer.user_id == user_id, DiscountOffer.notification_type == TRIAL_ENDING_DISCOUNT_SOURCE
                )
                .limit(1)
            ):
                return None
            if await db.scalar(
                select(SentNotification.id)
                .where(
                    SentNotification.subscription_id == subscription_id,
                    SentNotification.notification_type == snapshot[2],
                    SentNotification.created_at >= panel_end - timedelta(hours=(carrier or snapshot[1]) + 1),
                )
                .limit(1)
            ):
                return None
            if not _positive_options(await build_purchase_options(db, user, subscription=subscription)):
                return None
            if _settings_snapshot(carrier) != snapshot:
                return None
            expiry = _discount_expiry(panel_end, config['post_trial_hours'])
            previous = tuple(getattr(user, field) for field in _PROMO_FIELDS)
            async with db.begin_nested():
                try:
                    user.promo_offer_discount_percent = config['discount_percent']
                    user.promo_offer_discount_source = TRIAL_ENDING_DISCOUNT_SOURCE
                    user.promo_offer_discount_expires_at = expiry
                    candidate = await build_purchase_options(db, user, subscription=subscription)
                finally:
                    for field, value in zip(_PROMO_FIELDS, previous, strict=True):
                        setattr(user, field, value)
            if (
                not _positive_options(candidate)
                or _settings_snapshot(carrier) != snapshot
                or not _event_window(panel_end, carrier, snapshot[1])
            ):
                return None
            offer = DiscountOffer(
                user_id=user_id,
                subscription_id=subscription_id,
                notification_type=TRIAL_ENDING_DISCOUNT_SOURCE,
                discount_percent=config['discount_percent'],
                bonus_amount_kopeks=0,
                claimed_at=now,
                is_active=False,
                effect_type='percent_discount',
                expires_at=expiry,
                extra_data={
                    'carrier': carrier,
                    'lifetime_bytes': lifetime_bytes if type(lifetime_bytes) is int and lifetime_bytes >= 0 else None,
                    'panel_uuid': panel_uuid,
                    'panel_end': panel_end.isoformat(),
                    'reset_started_at': reset_started_at.isoformat() if reset_started_at else None,
                },
            )
            db.add(offer)
            for field, value in zip(
                _PROMO_FIELDS, (config['discount_percent'], TRIAL_ENDING_DISCOUNT_SOURCE, expiry), strict=True
            ):
                setattr(user, field, value)
            await db.flush()
            if _settings_snapshot(carrier) != snapshot:
                raise ValueError('Notification settings changed before issue commit')
            result = TrialEndingDiscountResult(
                offer.id,
                user_id,
                subscription_id,
                panel_uuid,
                panel_end,
                reset_started_at,
                config['discount_percent'],
                expiry,
                lifetime_bytes if type(lifetime_bytes) is int and lifetime_bytes >= 0 else None,
            )
        return result
    except Exception as error:
        logger.warning('trial_ending_discount_issue_failed', user_id=user_id, error=type(error).__name__)
        return None


def validate_prepared(result: TrialEndingDiscountResult | None, user: User, subscription: Subscription) -> bool:
    """A committed issue is not permission to send across a later reset, sale or changed term."""
    return bool(
        result is not None
        and user.id == result.user_id
        and subscription is not None
        and subscription.id == result.subscription_id
        and _eligible_person(user, result.reset_started_at)
        and _matches_term(user, subscription, result.panel_uuid, result.panel_end)
        and user.promo_offer_discount_source == TRIAL_ENDING_DISCOUNT_SOURCE
        and user.promo_offer_discount_percent == result.discount_percent
        and user.promo_offer_discount_expires_at == result.expires_at
        and result.expires_at > datetime.now(UTC)
    )


async def mark_trial_discount_delivered(db: AsyncSession, *, offer_id: int, user_id: int) -> bool:
    """Merge the first positive delivery into the caller's sent-mark transaction; never commit it here."""
    offer = await db.scalar(
        select(DiscountOffer)
        .where(
            DiscountOffer.id == offer_id,
            DiscountOffer.user_id == user_id,
            DiscountOffer.notification_type == TRIAL_ENDING_DISCOUNT_SOURCE,
        )
        .with_for_update()
    )
    if offer is None or (offer.extra_data or {}).get('delivered_at'):
        return False
    offer.extra_data = {**(offer.extra_data or {}), 'delivered_at': datetime.now(UTC).isoformat()}
    await db.flush()
    return True


async def cleanup_expired_discounts_safely(db: AsyncSession) -> int:
    """Compare all three stale slot fields before clearing; preserve ordinary offer expiration/logging."""
    now = datetime.now(UTC)
    candidates = (
        await db.execute(
            select(
                User.id,
                User.promo_offer_discount_percent,
                User.promo_offer_discount_source,
                User.promo_offer_discount_expires_at,
            ).where(User.promo_offer_discount_percent > 0, User.promo_offer_discount_expires_at <= now)
        )
    ).all()
    payloads = []
    for user_id, percent, source, expiry in candidates:
        cleared = await db.execute(
            update(User)
            .where(
                User.id == user_id,
                User.promo_offer_discount_percent == percent,
                User.promo_offer_discount_source.is_not_distinct_from(source),
                User.promo_offer_discount_expires_at == expiry,
                User.promo_offer_discount_expires_at <= now,
            )
            .values(
                promo_offer_discount_percent=0,
                promo_offer_discount_source=None,
                promo_offer_discount_expires_at=None,
                updated_at=now,
            )
            .returning(User.id)
            .execution_options(synchronize_session=False)
        )
        if cleared.scalar_one_or_none() is None:
            continue
        offer = None
        if source:
            try:
                offer = await get_latest_claimed_offer_for_user(db, user_id, source)
            except Exception as lookup_error:
                logger.warning(
                    'Failed to fetch latest claimed promo offer for user during expiration cleanup',
                    user_id=user_id,
                    lookup_error=lookup_error,
                )
        payloads.append(
            dict(
                user_id=user_id,
                offer_id=offer.id if offer else None,
                source=source,
                percent=percent,
                effect_type=offer.effect_type if offer else None,
            )
        )
    if candidates:
        await db.commit()
    for payload in payloads:
        try:
            await log_promo_offer_action(db, **payload, action='disabled', details={'reason': 'offer_expired'})
        except Exception as log_error:
            logger.warning(
                'Failed to log promo offer expiration for user', user_id=payload['user_id'], log_error=log_error
            )
            try:
                await db.rollback()
            except Exception as rollback_error:
                logger.warning(
                    'Failed to rollback session after promo offer expiration log failure', rollback_error=rollback_error
                )
    return len(payloads)

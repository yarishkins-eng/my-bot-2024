"""Grace ("бонус 2 дня после конца") — pure decision helpers.

Internally we call it *grace*; the user never sees that word (the screen shows
"бонус 2 дня"). Rules approved by the owner 22.06.2026 (ЗАДАЧИ-grace-и-доступ-без-VPN.md):

  • only for users who REALLY paid (trial → never; #629889: is_trial is never flipped);
  • only for paid periods of at least one month (GRACE_MIN_PERIOD_DAYS);
  • given even without a card/balance (autopay simply won't charge).

These functions are side-effect-free so the central expiry loop, the cabinet
response builder and every RemnaWave-sync path can share one source of truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.database.models import SubscriptionStatus, _aware


def is_grace_eligible(subscription, user, *, min_period_days: int | None = None) -> bool:
    """Whether a subscription qualifies for the post-expiry grace bonus.

    Pure predicate — does NOT look at balance/card (owner: grace is given even when
    there is nothing to charge). The money-gate is ``user.has_had_paid_subscription``.
    """
    if subscription is None or user is None:
        return False
    # Trial → never. NULL-safe: only an explicit False (real paid sub) passes.
    if getattr(subscription, 'is_trial', True) is not False:
        return False
    if not bool(getattr(user, 'has_had_paid_subscription', False)):
        return False
    min_days = settings.GRACE_MIN_PERIOD_DAYS if min_period_days is None else min_period_days
    period = getattr(subscription, 'grace_eligible_period_days', None) or 0
    return period >= min_days


def grace_period_for_term(days: int, *, min_period_days: int | None = None) -> int | None:
    """Value to store in ``grace_eligible_period_days`` for a purchased/extended term.

    Returns ``days`` only for month+ terms; ``None`` for shorter terms so a free
    7-day promo/campaign/admin bonus never clobbers a paying user's eligibility.
    """
    min_days = settings.GRACE_MIN_PERIOD_DAYS if min_period_days is None else min_period_days
    return days if days is not None and days >= min_days else None


def is_in_grace(subscription, now: datetime | None = None) -> bool:
    """True while the subscription's grace window is open (VPN kept alive)."""
    if not getattr(subscription, 'in_grace', False):
        return False
    grace_until = _aware(getattr(subscription, 'grace_until', None))
    if grace_until is None:
        return False
    now = now or datetime.now(UTC)
    return grace_until > now


def resolve_panel_active_and_expiry(subscription, now: datetime) -> tuple[bool, datetime]:
    """Compute ``(is_active, expire_at)`` to push to RemnaWave for one subscription.

    Grace-aware: while in grace the panel user stays ACTIVE with ``expire_at =
    grace_until`` so the VPN keeps working even though the DB status is EXPIRED.
    For a truly-dead subscription the expire is clamped to the near future, matching
    the long-standing ``max(end_date, now + 1min)`` behaviour.
    """
    end = _aware(getattr(subscription, 'end_date', None))
    normal_active = (
        getattr(subscription, 'status', None) in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
        and end is not None
        and end > now
    )
    if normal_active:
        return True, end
    if is_in_grace(subscription, now):
        return True, _aware(subscription.grace_until)
    fallback = now + timedelta(minutes=1)
    return False, (max(end, fallback) if end is not None else fallback)

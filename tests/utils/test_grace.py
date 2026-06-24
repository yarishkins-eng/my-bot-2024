"""Unit tests for the grace ("бонус 2 дня после конца") helpers.

Grace rules (owner-approved 22.06.2026, see ЗАДАЧИ-grace-и-доступ-без-VPN.md):
  • only for users who REALLY paid (trial → never);
  • only for paid periods of at least one month (short periods → no grace);
  • given even without a card/balance (autopay just won't charge);
  • #629889: is_trial is never flipped anywhere.

These tests pin the pure decision logic so the central expiry loop can rely on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.database.models import SubscriptionStatus
from app.utils.grace import (
    grace_period_for_term,
    is_grace_eligible,
    is_in_grace,
    resolve_panel_active_and_expiry,
)


MIN = 30  # GRACE_MIN_PERIOD_DAYS used explicitly to keep the tests config-independent


def _user(*, has_had_paid: bool):
    return SimpleNamespace(id=1, has_had_paid_subscription=has_had_paid)


def _sub(*, is_trial: bool, period_days: int | None, status=SubscriptionStatus.EXPIRED.value):
    return SimpleNamespace(
        id=1,
        status=status,
        is_trial=is_trial,
        grace_eligible_period_days=period_days,
        end_date=datetime.now(UTC) - timedelta(hours=1),
        in_grace=False,
        grace_until=None,
    )


# ──────────────────────────── eligibility ────────────────────────────


def test_paid_monthly_subscriber_is_eligible():
    assert is_grace_eligible(_sub(is_trial=False, period_days=30), _user(has_had_paid=True), min_period_days=MIN)


def test_trial_is_never_eligible_even_if_long_period():
    # A trial relabelled to a paid tariff keeps is_trial=True (#629889) → must NOT get grace.
    assert not is_grace_eligible(_sub(is_trial=True, period_days=365), _user(has_had_paid=True), min_period_days=MIN)


def test_short_paid_period_is_not_eligible():
    assert not is_grace_eligible(_sub(is_trial=False, period_days=7), _user(has_had_paid=True), min_period_days=MIN)


def test_null_period_is_not_eligible():
    assert not is_grace_eligible(_sub(is_trial=False, period_days=None), _user(has_had_paid=True), min_period_days=MIN)


def test_never_paid_user_is_not_eligible_even_with_long_period():
    # Free admin-granted subscription (is_trial=False, never paid) → must NOT get grace,
    # otherwise it could later be auto-charged after a top-up (#629889-adjacent).
    assert not is_grace_eligible(_sub(is_trial=False, period_days=30), _user(has_had_paid=False), min_period_days=MIN)


def test_eligibility_does_not_depend_on_balance_or_card():
    # Owner rule: give grace even when there is no money to charge. The predicate
    # must not look at balance/card at all — exactly the paid+month+ subscriber.
    user = _user(has_had_paid=True)  # no balance/card attributes set
    assert is_grace_eligible(_sub(is_trial=False, period_days=30), user, min_period_days=MIN)


def test_exactly_min_period_is_eligible():
    assert is_grace_eligible(_sub(is_trial=False, period_days=MIN), _user(has_had_paid=True), min_period_days=MIN)


def test_none_inputs_are_safe():
    assert not is_grace_eligible(None, _user(has_had_paid=True), min_period_days=MIN)
    assert not is_grace_eligible(_sub(is_trial=False, period_days=30), None, min_period_days=MIN)


# ──────────────────────────── is_in_grace ────────────────────────────


def test_is_in_grace_true_when_flag_set_and_window_open():
    sub = _sub(is_trial=False, period_days=30)
    sub.in_grace = True
    sub.grace_until = datetime.now(UTC) + timedelta(days=1)
    assert is_in_grace(sub)


def test_is_in_grace_false_when_window_closed():
    sub = _sub(is_trial=False, period_days=30)
    sub.in_grace = True
    sub.grace_until = datetime.now(UTC) - timedelta(minutes=1)
    assert not is_in_grace(sub)


def test_is_in_grace_false_without_flag():
    sub = _sub(is_trial=False, period_days=30)
    sub.in_grace = False
    sub.grace_until = datetime.now(UTC) + timedelta(days=1)
    assert not is_in_grace(sub)


# ─────────────────────── panel active/expiry resolution ───────────────────────


def test_panel_active_subscription_keeps_real_end_date():
    now = datetime.now(UTC)
    end = now + timedelta(days=5)
    sub = SimpleNamespace(
        status=SubscriptionStatus.ACTIVE.value, end_date=end, in_grace=False, grace_until=None
    )
    active, expire_at = resolve_panel_active_and_expiry(sub, now)
    assert active is True
    assert expire_at == end


def test_panel_expired_non_grace_is_disabled():
    now = datetime.now(UTC)
    end = now - timedelta(days=1)
    sub = SimpleNamespace(
        status=SubscriptionStatus.EXPIRED.value, end_date=end, in_grace=False, grace_until=None
    )
    active, expire_at = resolve_panel_active_and_expiry(sub, now)
    assert active is False
    # disabled users get a near-future expire (existing behaviour: max(end, now+1min))
    assert expire_at >= now


def test_panel_grace_keeps_vpn_alive_until_grace_until():
    """During grace the panel user stays ACTIVE with expireAt = grace_until,
    even though the DB status is EXPIRED — this is what keeps the VPN working."""
    now = datetime.now(UTC)
    end = now - timedelta(hours=2)
    grace_until = now + timedelta(days=2)
    sub = SimpleNamespace(
        status=SubscriptionStatus.EXPIRED.value, end_date=end, in_grace=True, grace_until=grace_until
    )
    active, expire_at = resolve_panel_active_and_expiry(sub, now)
    assert active is True
    assert expire_at == grace_until


def test_panel_grace_window_passed_is_disabled():
    now = datetime.now(UTC)
    end = now - timedelta(days=3)
    sub = SimpleNamespace(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=end,
        in_grace=True,
        grace_until=now - timedelta(minutes=5),  # grace already over
    )
    active, expire_at = resolve_panel_active_and_expiry(sub, now)
    assert active is False


# ─────────────────── period recording (anti-clobber) ───────────────────


def test_grace_period_recorded_for_month_plus_term():
    assert grace_period_for_term(30, min_period_days=MIN) == 30
    assert grace_period_for_term(365, min_period_days=MIN) == 365


def test_short_bonus_does_not_record_a_period():
    # A free 7-day promo/campaign/admin bonus must NOT overwrite a paying user's
    # eligibility period — returns None so the caller leaves the column untouched.
    assert grace_period_for_term(7, min_period_days=MIN) is None
    assert grace_period_for_term(0, min_period_days=MIN) is None
    assert grace_period_for_term(-5, min_period_days=MIN) is None

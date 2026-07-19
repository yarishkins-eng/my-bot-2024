"""CRUD-level guards for the grace ("бонус 2 дня") feature.

Covers:
  • get_expired_subscriptions EXCLUDES in_grace rows — otherwise the 30-min
    monitoring cycle would keep re-entering grace and shift grace_until forever
    (the loop guard, ЗАДАЧИ §4).
  • extend_subscription records the purchased period into grace_eligible_period_days
    for month+ terms, never lets a short free bonus clobber it, and clears the
    grace flags when a graced subscription is renewed back to life.
  • _revive_paid_subscription records the period too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.database.crud import subscription as sub_crud
from app.database.crud.subscription import get_expired_subscriptions, get_subscriptions_grace_ended
from app.database.models import SubscriptionStatus


MIN = 30  # matches GRACE_MIN_PERIOD_DAYS default


# ──────────────────────── loop guard: get_expired excludes in_grace ────────────────────────


async def test_get_expired_subscriptions_excludes_in_grace():
    """The query that feeds the central expiry loop must filter out in_grace rows."""
    captured = {}

    async def fake_execute(stmt):
        captured['stmt'] = stmt
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = fake_execute

    await get_expired_subscriptions(db)

    sql = str(captured['stmt'].compile(compile_kwargs={'literal_binds': False})).lower()
    assert 'in_grace' in sql, 'get_expired_subscriptions must filter on in_grace to avoid the grace loop'


async def test_get_subscriptions_grace_ended_only_finalizes_expired():
    """The grace-end finalizer must require status=EXPIRED, so a sub that was renewed/
    reactivated (status=ACTIVE) while still flagged in_grace is NEVER force-disabled."""
    captured = {}

    async def fake_execute(stmt):
        captured['stmt'] = stmt
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = fake_execute

    await get_subscriptions_grace_ended(db)

    sql = str(captured['stmt'].compile(compile_kwargs={'literal_binds': False})).lower()
    assert 'in_grace' in sql
    assert 'status' in sql, 'grace-end finalizer must guard on status=EXPIRED (not finalize a revived sub)'


# ──────────────────────── period recording in extend_subscription ────────────────────────


def _extend_env(monkeypatch):
    monkeypatch.setattr('app.database.crud.subscription._lock_subscription_row', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription._housekeep_expired_purchases', AsyncMock())
    monkeypatch.setattr('app.database.crud.subscription.clear_notifications', AsyncMock())
    monkeypatch.setattr(
        'app.database.crud.subscription._apply_base_limit_preserving_active_purchases',
        AsyncMock(return_value=(0, 0)),
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.deactivate_user_trial_subscriptions', AsyncMock(return_value=[])
    )
    db = AsyncMock()
    db.flush = AsyncMock()
    return db


def _paid_sub(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=1,
        user_id=7,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=10),
        end_date=now + timedelta(days=5),
        tariff_id=None,
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=1,
        connected_squads=[],
        purchased_traffic_gb=0,
        updated_at=now,
        in_grace=False,
        grace_until=None,
        grace_eligible_period_days=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def test_extend_records_period_for_month_plus(monkeypatch):
    db = _extend_env(monkeypatch)
    sub = _paid_sub(grace_eligible_period_days=None)

    await sub_crud.extend_subscription(db, sub, 30, commit=False)

    assert sub.grace_eligible_period_days == 30


async def test_extend_short_bonus_does_not_clobber_period(monkeypatch):
    """A paying user with a 30-day eligibility who redeems a 7-day promo keeps eligibility."""
    db = _extend_env(monkeypatch)
    sub = _paid_sub(grace_eligible_period_days=30)

    await sub_crud.extend_subscription(db, sub, 7, commit=False)

    assert sub.grace_eligible_period_days == 30  # untouched by the short bonus


async def test_extend_clears_grace_flags_on_renewal(monkeypatch):
    """Renewing a graced (EXPIRED + in_grace) subscription brings it back to life —
    the grace flags must be cleared so the screen stops showing «бонус 2 дня»."""
    db = _extend_env(monkeypatch)
    now = datetime.now(UTC)
    sub = _paid_sub(
        status=SubscriptionStatus.EXPIRED.value,
        end_date=now - timedelta(hours=2),
        in_grace=True,
        grace_until=now + timedelta(days=1),
        grace_eligible_period_days=30,
    )

    await sub_crud.extend_subscription(db, sub, 30, commit=False)

    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.in_grace is False
    assert sub.grace_until is None


# ──────────────────────── period recording in _revive_paid_subscription ────────────────────────


async def test_revive_records_period(monkeypatch):
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    past = datetime.now(UTC) - timedelta(days=5)
    sub = SimpleNamespace(
        id=1,
        user_id=7,
        tariff_id=3,
        status=SubscriptionStatus.EXPIRED.value,
        end_date=past,
        start_date=past - timedelta(days=30),
        is_trial=False,
        traffic_used_gb=0.0,
        connected_squads=['sq'],
        device_limit=2,
        traffic_limit_gb=100,
        in_grace=False,
        grace_until=None,
        grace_eligible_period_days=None,
    )

    await sub_crud._revive_paid_subscription(
        db,
        sub,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['sq'],
        update_server_counters=False,
        commit=False,
    )

    assert sub.grace_eligible_period_days == 30

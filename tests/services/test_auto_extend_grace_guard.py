"""Regression test: auto-extend-after-topup must NOT charge during the grace bonus.

Background — grace ("бонус 2 дня после конца")
----------------------------------------------
When a paid month+ subscription expires it may enter *grace*: the DB status is set
to EXPIRED, ``in_grace=True`` and ``grace_until = end + GRACE_PERIOD_DAYS``, while the
RemnaWave panel keeps the VPN alive until ``grace_until``. The grace module documents
the money rule explicitly: *"given even without a card/balance (autopay simply won't
charge)"*.

The regular autopay cycle never touches grace subs (it filters ``status=ACTIVE``), but
``try_auto_extend_expired_after_topup`` — which fires when the user tops up their
balance — only checked ``status==EXPIRED`` + ``is_trial`` + ``has_had_paid`` +
``autopay_enabled``. A grace sub passes all four, so a top-up made for any reason during
the bonus window would silently charge a full renewal while the gifted days were still
running. This test pins the ``is_in_grace`` guard so that regression cannot return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus


def _chargeable_grace_sub():
    """An expired sub that — apart from being in grace — would otherwise be charged:
    paid, autopay on, active tariff, recently expired. Only ``in_grace`` should stop it."""
    now = datetime.now(UTC)
    active_tariff = SimpleNamespace(
        id=10,
        name='Премиум',
        is_active=True,
        get_shortest_period=lambda: 30,
    )
    return SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        autopay_enabled=True,
        end_date=now - timedelta(hours=2),
        in_grace=True,
        grace_until=now + timedelta(days=2),  # окно бонуса ещё открыто
        tariff=active_tariff,
    )


def _paid_user():
    user = MagicMock()
    user.id = 7
    user.balance_kopeks = 1_000_000
    user.has_had_paid_subscription = True
    return user


@pytest.mark.asyncio
async def test_try_auto_extend_skips_during_grace_single_tariff(monkeypatch) -> None:
    """Single-tariff: top-up during the bonus window must NOT trigger a charge."""
    from app.services import subscription_auto_purchase_service as svc

    subscription = _chargeable_grace_sub()
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)

    async def fake_get_subscription_by_user_id(_db, _user_id):
        return subscription

    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        fake_get_subscription_by_user_id,
    )

    # If the grace guard fails to trip, the function reaches the pricing / balance path.
    pricing_engine_spy = AsyncMock()
    subtract_balance_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract_balance_spy)

    result = await svc.try_auto_extend_expired_after_topup(AsyncMock(), _paid_user(), bot=None)

    assert result is False, 'must NOT charge while the grace bonus window is open'
    pricing_engine_spy.assert_not_called()
    subtract_balance_spy.assert_not_called()


@pytest.mark.asyncio
async def test_try_auto_extend_skips_during_grace_multi_tariff(monkeypatch) -> None:
    """Multi-tariff selection branch reaches the same shared guard — pin it too."""
    from app.services import subscription_auto_purchase_service as svc

    subscription = _chargeable_grace_sub()
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: True)

    async def fake_get_all_subs(_db, _user_id):
        return [subscription]

    monkeypatch.setattr(
        'app.database.crud.subscription.get_all_subscriptions_by_user_id',
        fake_get_all_subs,
    )

    pricing_engine_spy = AsyncMock()
    subtract_balance_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract_balance_spy)

    result = await svc.try_auto_extend_expired_after_topup(AsyncMock(), _paid_user(), bot=None)

    assert result is False, 'must NOT charge while the grace bonus window is open (multi-tariff)'
    pricing_engine_spy.assert_not_called()
    subtract_balance_spy.assert_not_called()


@pytest.mark.asyncio
async def test_try_auto_extend_not_blocked_when_grace_window_already_closed(monkeypatch) -> None:
    """Control: once the bonus window has passed (grace_until in the past), the grace guard
    must NOT trip — is_in_grace() returns False, so the sub flows on down the normal path
    (here it stops harmlessly at the inactive-tariff guard). NOTE: this control test on its
    own cannot isolate the grace guard — it would also pass if the guard were deleted. The
    guard itself is pinned by tests 1 and 2 (active tariff + OPEN window → a charge would
    happen without it). This case only confirms a *past* window does not wrongly block."""
    from app.services import subscription_auto_purchase_service as svc

    now = datetime.now(UTC)
    inactive_tariff = SimpleNamespace(id=99, name='СТАРЫЙ', is_active=False, get_shortest_period=lambda: 30)
    subscription = SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        autopay_enabled=True,
        end_date=now - timedelta(days=3),
        in_grace=True,  # флаг ещё стоит, но окно прошло
        grace_until=now - timedelta(minutes=5),  # бонус уже закончился → is_in_grace == False
        tariff=inactive_tariff,
    )
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)

    async def fake_get_subscription_by_user_id(_db, _user_id):
        return subscription

    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        fake_get_subscription_by_user_id,
    )
    pricing_engine_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_engine_spy)

    result = await svc.try_auto_extend_expired_after_topup(AsyncMock(), _paid_user(), bot=None)

    # Stopped by the inactive-tariff guard, NOT the grace guard — but crucially it got
    # past grace. No charge either way (inactive tariff is its own protection).
    assert result is False
    pricing_engine_spy.assert_not_called()

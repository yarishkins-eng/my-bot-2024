"""#629889-adjacent guard: try_auto_extend_expired_after_topup must also require
that the user has ACTUALLY paid before, on top of the existing is_trial guard.

Without this, a free admin-granted subscription (is_trial=False, autopay_enabled=True,
never paid) would be silently charged for a full period the first time the user tops
up their balance for something else. The is_trial guard alone does not catch it because
such a subscription is not a trial.

The new check must be ADDED, not replace the is_trial guard (both must remain).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import SubscriptionStatus


def _expired_paid_like_sub():
    return SimpleNamespace(
        id=1,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,  # NOT a trial → old guard does not catch it
        autopay_enabled=True,  # user (or admin default) left autopay on
        end_date=datetime.now(UTC) - timedelta(hours=2),  # recently expired (in 30-day window)
        tariff=SimpleNamespace(id=5, name='Премиум', is_active=True, get_shortest_period=lambda: 30),
    )


@pytest.mark.asyncio
async def test_never_paid_user_is_not_auto_charged(monkeypatch):
    """has_had_paid_subscription=False → must refuse before touching pricing/balance."""
    from app.services import subscription_auto_purchase_service as svc

    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        AsyncMock(return_value=_expired_paid_like_sub()),
    )

    pricing_spy = AsyncMock()
    subtract_spy = AsyncMock()
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_spy)
    monkeypatch.setattr(svc, 'subtract_user_balance', subtract_spy)

    user = MagicMock()
    user.id = 7
    user.balance_kopeks = 1_000_000
    user.has_had_paid_subscription = False  # free admin grant — never actually paid

    result = await svc.try_auto_extend_expired_after_topup(AsyncMock(), user, bot=None)

    assert result is False
    pricing_spy.assert_not_called()
    subtract_spy.assert_not_called()


@pytest.mark.asyncio
async def test_real_paying_user_passes_the_has_paid_guard(monkeypatch):
    """has_had_paid_subscription=True → the new guard must let the flow proceed
    (it reaches the pricing engine). We make pricing raise so the function returns
    early, which still proves the guard was passed."""
    from app.services import subscription_auto_purchase_service as svc

    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id',
        AsyncMock(return_value=_expired_paid_like_sub()),
    )

    user = MagicMock()
    user.id = 7
    user.telegram_id = 777
    user.balance_kopeks = 1_000_000
    user.has_had_paid_subscription = True

    # lock_user_for_pricing returns the (locked) user; keep it a real user-like object.
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))

    # Pricing reached → guard passed. Raise to stop the flow cleanly afterwards.
    pricing_spy = AsyncMock(side_effect=RuntimeError('stop after guard'))
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', pricing_spy)

    result = await svc.try_auto_extend_expired_after_topup(AsyncMock(), user, bot=None)

    assert result is False  # stopped at pricing
    pricing_spy.assert_called()  # but the has-paid guard let us get there

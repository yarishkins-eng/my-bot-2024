"""Regression tests for per-referral earnings in the cabinet admin list."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import SortByEnum, UserListItem
from app.database.models import ReferralEarning


@pytest.mark.asyncio
async def test_referral_list_groups_earnings_once_for_current_page(monkeypatch: pytest.MonkeyPatch) -> None:
    referrals = [SimpleNamespace(id=129), SimpleNamespace(id=130)]
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=SimpleNamespace(id=123)))
    monkeypatch.setattr(admin_users, 'get_referrals', AsyncMock(return_value=referrals))
    monkeypatch.setattr(admin_users, 'get_users_spending_stats', AsyncMock(return_value={}))
    monkeypatch.setattr(
        admin_users,
        '_build_user_list_item',
        lambda user, _stats: UserListItem.model_construct(id=user.id, referral_earned_kopeks=0),
    )

    earnings = [
        ReferralEarning(user_id=123, referral_id=129, amount_kopeks=20_000, reason='test'),
        ReferralEarning(user_id=123, referral_id=129, amount_kopeks=6_575, reason='test'),
        ReferralEarning(user_id=123, referral_id=131, amount_kopeks=99_999, reason='off_page'),
        ReferralEarning(user_id=999, referral_id=129, amount_kopeks=88_888, reason='other_referrer'),
    ]

    async def execute(statement):
        params = statement.compile().params
        referrer_id = next(value for key, value in params.items() if key.startswith('user_id'))
        page_ids = next(value for key, value in params.items() if key.startswith('referral_id'))
        totals = {
            referral_id: sum(
                earning.amount_kopeks
                for earning in earnings
                if earning.user_id == referrer_id and earning.referral_id == referral_id
            )
            for referral_id in page_ids
        }
        return SimpleNamespace(
            all=lambda: [
                SimpleNamespace(referral_id=referral_id, referral_earned_kopeks=amount)
                for referral_id, amount in totals.items()
                if amount
            ]
        )

    db = AsyncMock()
    db.execute.side_effect = execute

    response = await admin_users.get_user_referrals(user_id=123, offset=0, limit=50, admin=SimpleNamespace(id=1), db=db)

    assert [(item.id, item.referral_earned_kopeks) for item in response.users] == [(129, 26_575), (130, 0)]
    assert db.execute.await_count == 1
    sql = str(db.execute.await_args.args[0])
    assert 'sum(referral_earnings.amount_kopeks)' in sql
    assert 'GROUP BY referral_earnings.referral_id' in sql
    assert 'referral_earnings.user_id' in sql


@pytest.mark.asyncio
async def test_general_user_list_keeps_zero_without_earnings_query(monkeypatch: pytest.MonkeyPatch) -> None:
    users = [SimpleNamespace(id=129)]
    monkeypatch.setattr(admin_users, 'get_users_list', AsyncMock(return_value=users))
    monkeypatch.setattr(admin_users, 'get_users_count', AsyncMock(return_value=1))
    monkeypatch.setattr(admin_users, 'get_users_spending_stats', AsyncMock(return_value={}))
    monkeypatch.setattr(
        admin_users,
        '_build_user_list_item',
        lambda user, _stats: UserListItem.model_construct(id=user.id, referral_earned_kopeks=0),
    )
    db = AsyncMock()

    response = await admin_users.list_users(
        offset=0,
        limit=50,
        search=None,
        email=None,
        status=None,
        subscription_status=None,
        tariff_id=None,
        promo_group_id=None,
        campaign_id=None,
        partner_id=None,
        sort_by=SortByEnum.CREATED_AT,
        admin=SimpleNamespace(id=1),
        db=db,
    )

    assert response.users[0].referral_earned_kopeks == 0
    db.execute.assert_not_awaited()

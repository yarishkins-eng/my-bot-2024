"""Верх панели администратора (СП-1.5) и вкладка «Оплаты»: новые поля не ломают прежний ответ `/dashboard`,
а их сбой не роняет экран нод; «Оплаты» с новым периодом считают сутки МСК, без него — как раньше."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import admin_sales_stats, admin_stats


def _dashboard_patches(tiles):
    return (
        patch.object(
            admin_stats,
            '_get_nodes_overview',
            AsyncMock(
                return_value=admin_stats.NodesOverview(
                    total=0, online=0, offline=0, disabled=0, total_users_online=0, nodes=[]
                )
            ),
        ),
        patch.object(
            admin_stats,
            'get_subscriptions_statistics',
            AsyncMock(
                return_value={
                    'total_subscriptions': 140,
                    'active_subscriptions': 139,
                    'trial_subscriptions': 58,
                    'paid_subscriptions': 81,
                }
            ),
        ),
        patch.object(admin_stats, 'get_transactions_statistics', AsyncMock(return_value={})),
        patch.object(admin_stats, 'get_revenue_by_period', AsyncMock(return_value=[])),
        patch.object(admin_stats, 'get_server_statistics', AsyncMock(return_value={})),
        patch.object(admin_stats, '_get_tariff_stats', AsyncMock(return_value=None)),
        patch.object(admin_stats, 'owner_people_tiles', tiles),
    )


async def _dashboard(tiles):
    db = SimpleNamespace(rollback=AsyncMock())
    patches = _dashboard_patches(tiles)
    for item in patches:
        item.start()
    try:
        return await admin_stats.get_dashboard_stats(admin=None, db=db), db
    finally:
        for item in patches:
            item.stop()


@pytest.mark.asyncio
async def test_dashboard_carries_owner_tiles_next_to_the_old_fields() -> None:
    stats, db = await _dashboard(AsyncMock(return_value={'on_trial': 35, 'paying': 68, 'new_buyers_today': 2}))

    subscriptions = stats.subscriptions.model_dump()
    assert (subscriptions['trial'], subscriptions['paid']) == (
        58,
        81,
    )  # прежние поля не тронуты — их читает «Статистика»
    assert (subscriptions['people_on_trial'], subscriptions['people_paying'], subscriptions['new_buyers_today']) == (
        35,
        68,
        2,
    )
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rollback_does_not_take_the_dashboard_down() -> None:
    db = SimpleNamespace(rollback=AsyncMock(side_effect=RuntimeError('connection lost')))
    patches = _dashboard_patches(AsyncMock(side_effect=RuntimeError('database went away')))
    for item in patches:
        item.start()
    try:
        stats = await admin_stats.get_dashboard_stats(admin=None, db=db)
    finally:
        for item in patches:
            item.stop()

    assert stats.subscriptions.people_paying is None and stats.subscriptions.trial == 58


@pytest.mark.asyncio
async def test_owner_tiles_failure_leaves_the_dashboard_alive_with_dashes() -> None:
    stats, db = await _dashboard(AsyncMock(side_effect=RuntimeError('database went away')))

    subscriptions = stats.subscriptions.model_dump()
    assert (subscriptions['people_on_trial'], subscriptions['people_paying'], subscriptions['new_buyers_today']) == (
        None,
        None,
        None,
    )
    assert subscriptions['trial'] == 58
    db.rollback.assert_awaited_once()  # общий сеанс не остаётся в прерванной транзакции


def test_response_contracts_with_the_cabinet() -> None:
    """Договор с кабинетом (`cabinet-code/src/api/adminSalesStats.ts`): имена полей — ровно такие."""
    assert set(admin_stats.SubscriptionStats.model_fields) >= {
        'total',
        'active',
        'trial',
        'paid',
        'expired',
        'purchased_today',
        'people_on_trial',
        'people_paying',
        'new_buyers_today',
    }
    assert set(admin_sales_stats.SalesOverviewResponse.model_fields) == {
        'generated_at',
        'window',
        'now',
        'money',
        'purchases',
        'trial',
    }
    assert set(admin_sales_stats.SalesNowStats.model_fields) == {'paying', 'on_trial', 'ending_soon'}
    assert set(admin_sales_stats.SalesMoneyStats.model_fields) == {
        'received_kopeks',
        'deposits_count',
        'receipts_count',
        'previous_received_kopeks',
        'previous_comparable',
    }
    assert set(admin_sales_stats.SalesPurchaseStats.model_fields) == {
        'count',
        'amount_kopeks',
        'first_count',
        'first_amount_kopeks',
        'first_after_trial',
        'first_direct',
        'renewal_count',
        'renewal_amount_kopeks',
        'addon_count',
        'addon_amount_kopeks',
        'previous_first_count',
        'not_renewed',
    }
    assert set(admin_sales_stats.SalesTrialStats.model_fields) == {
        'came',
        'took_trial',
        'trial_finished',
        'bought_after_trial',
    }
    assert set(admin_sales_stats.SalesPersonItem.model_fields) == {
        'user_id',
        'name',
        'username',
        'telegram_id',
        'tariff_name',
        'end_date',
        'autopay_enabled',
        'balance_kopeks',
    }
    assert set(admin_sales_stats.SalesWindowInfo.model_fields) == {'start', 'end', 'previous_start', 'previous_end'}
    assert set(admin_sales_stats.SalesPeopleResponse.model_fields) == {'kind', 'total', 'items'}
    assert set(admin_sales_stats.SalesAdsResponse.model_fields) == {
        'campaigns_total',
        'campaigns_with_spend',
        'mature_spend_kopeks',
        'mature_buyers',
        'mature_cost_per_buyer_kopeks',
        'mature_receipts_kopeks',
        'fresh_spend_kopeks',
        'fresh_buyers',
        'campaigns',
    }
    # кабинет рисует «—» ровно там, где бот может прислать null — остальные поля обязаны быть всегда
    nullable = {
        (model.__name__, name)
        for model in (
            admin_sales_stats.SalesWindowInfo,
            admin_sales_stats.SalesMoneyStats,
            admin_sales_stats.SalesPurchaseStats,
            admin_sales_stats.SalesPersonItem,
            admin_sales_stats.SalesAdCampaign,
            admin_sales_stats.SalesAdsResponse,
        )
        for name, field in model.model_fields.items()
        if not field.is_required()
    }
    assert nullable == {
        ('SalesWindowInfo', 'previous_start'),
        ('SalesWindowInfo', 'previous_end'),
        ('SalesMoneyStats', 'previous_received_kopeks'),
        ('SalesMoneyStats', 'previous_comparable'),
        ('SalesPurchaseStats', 'previous_first_count'),
        ('SalesPersonItem', 'name'),
        ('SalesPersonItem', 'username'),
        ('SalesPersonItem', 'telegram_id'),
        ('SalesPersonItem', 'tariff_name'),
        ('SalesPersonItem', 'autopay_enabled'),
        ('SalesPersonItem', 'balance_kopeks'),
        ('SalesAdCampaign', 'cost_per_buyer_kopeks'),
        ('SalesAdsResponse', 'mature_cost_per_buyer_kopeks'),
    }
    assert set(admin_sales_stats.SalesAdCampaign.model_fields) == {
        'campaign_id',
        'name',
        'ad_spend_kopeks',
        'buyers',
        'cost_per_buyer_kopeks',
        'receipts_kopeks',
        'fresh',
    }


@pytest.mark.asyncio
async def test_payments_tab_with_a_period_counts_moscow_days_and_without_it_stays_as_before() -> None:
    seen = []

    async def rates(db, start, end):
        seen.append((start, end))
        return []

    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar=lambda: 0)))
    with patch.object(admin_sales_stats, 'get_gateway_success_rates', rates):
        await admin_sales_stats.get_payment_health(
            days=30, start_date=None, end_date=None, period='yesterday', admin=None, db=db
        )
        await admin_sales_stats.get_payment_health(
            days=30, start_date=None, end_date=None, period=None, admin=None, db=db
        )

    yesterday, legacy = seen
    assert yesterday[1] - yesterday[0] == datetime(2026, 1, 2) - datetime(2026, 1, 1)
    assert yesterday[1].astimezone(admin_sales_stats._MSK).hour == 0  # граница — полночь по Москве
    assert (legacy[0].tzinfo, legacy[0].hour) == (UTC, 0)  # прежний путь: полночь UTC, как до СП-1

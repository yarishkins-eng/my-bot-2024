import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql

from app.services.campaign_service import (
    CampaignAnalytics,
    _campaign_analytics_statement,
    delete_campaign_if_unattributed,
    get_campaign_analytics,
)


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


def _row(**overrides):
    values = {
        'campaign_id': 4,
        'registrations': 107,
        'conversion_count': 4,
        'paid_flag_count': 4,
        'total_revenue_kopeks': 0,
        'leads': 107,
        'paying_leads': 4,
        'confirmed_receipts_kopeks': 203_600,
    }
    values.update(overrides)
    return values


def test_aggregate_sql_encodes_the_money_and_attribution_contract() -> None:
    sql = str(
        _campaign_analytics_statement().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={'literal_binds': True},
        )
    ).lower()

    assert 'row_number() over (partition by advertising_campaign_registrations.user_id' in sql
    assert 'advertising_campaign_registrations.created_at asc' in sql
    assert 'nulls last' in sql
    assert 'advertising_campaign_registrations.id asc' in sql
    assert "transactions.type in ('deposit', 'provider_receipt')" in sql
    assert 'transactions.is_completed is true' in sql
    assert 'transactions.amount_kopeks > 0' in sql
    assert 'transactions.payment_method in (' in sql
    assert 'coalesce(transactions.completed_at, transactions.created_at) >=' in sql
    assert 'post_paid_provider_terminal%' in sql
    assert 'checkout_payment_attempts.provider_payment_id = transactions.external_id' in sql
    assert 'device_first_provider_events.provider_payment_id = transactions.external_id' in sql
    assert "upper(device_first_provider_events.provider_status) in ('chargebacked')" in sql
    assert 'yookassa_payments.test_mode is true' in sql
    assert 'cloudpayments_payments.test_mode is true' in sql
    assert 'campaign_legacy_registration_users as' in sql
    assert 'select distinct advertising_campaign_registrations.campaign_id' in sql
    assert "transactions.type = 'deposit'" in sql  # legacy compatibility remains deposit-only


@pytest.mark.asyncio
async def test_metrics_keep_legacy_zero_but_report_confirmed_receipts() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=_MappingsResult([_row()])))

    metrics = (await get_campaign_analytics(db))[4]

    assert metrics.total_revenue_kopeks == 0
    assert metrics.confirmed_receipts_kopeks == 203_600
    assert metrics.paying_leads == 4
    assert metrics.payment_conversion_rate == 3.7
    assert metrics.avg_confirmed_receipts_per_lead_kopeks == 1902
    assert metrics.registrations == 107
    assert metrics.conversion_rate == 3.7


@pytest.mark.asyncio
async def test_paid_flag_is_legacy_only_and_does_not_invent_external_receipts() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_MappingsResult(
                [
                    _row(
                        campaign_id=2,
                        registrations=1,
                        conversion_count=0,
                        paid_flag_count=1,
                        leads=1,
                        paying_leads=0,
                        confirmed_receipts_kopeks=0,
                    )
                ]
            )
        )
    )

    metrics = (await get_campaign_analytics(db, [2]))[2]

    assert metrics.paid_users_count == 1
    assert metrics.conversion_rate == 100.0
    assert metrics.paying_leads == 0
    assert metrics.payment_conversion_rate == 0.0


def test_aggregate_executes_first_touch_receipt_filters_and_durable_reversals() -> None:
    """Run the real statement, not a mocked aggregate row, over adversarial fixtures."""

    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        for ddl in (
            'CREATE TABLE advertising_campaigns (id INTEGER PRIMARY KEY)',
            'CREATE TABLE advertising_campaign_registrations ('
            'id INTEGER PRIMARY KEY, campaign_id INTEGER, user_id INTEGER, created_at DATETIME)',
            'CREATE TABLE users (id INTEGER PRIMARY KEY, has_had_paid_subscription BOOLEAN)',
            'CREATE TABLE subscription_conversions (user_id INTEGER)',
            'CREATE TABLE subscription_checkouts (id INTEGER PRIMARY KEY, terminal_reason TEXT)',
            'CREATE TABLE checkout_payment_attempts ('
            'id INTEGER PRIMARY KEY, checkout_id INTEGER, provider_payment_id TEXT, reconciliation_reason TEXT)',
            'CREATE TABLE device_first_provider_events ('
            'id INTEGER PRIMARY KEY, checkout_id INTEGER, provider_payment_id TEXT, provider_status TEXT)',
            'CREATE TABLE yookassa_payments (id INTEGER PRIMARY KEY, transaction_id INTEGER, test_mode BOOLEAN)',
            'CREATE TABLE cloudpayments_payments (id INTEGER PRIMARY KEY, transaction_id INTEGER, test_mode BOOLEAN)',
            'CREATE TABLE transactions ('
            'id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount_kopeks INTEGER, '
            'payment_method TEXT, is_completed BOOLEAN, completed_at DATETIME, created_at DATETIME, '
            'device_first_checkout_id INTEGER, external_id TEXT)',
        ):
            connection.exec_driver_sql(ddl)

        connection.exec_driver_sql('INSERT INTO advertising_campaigns (id) VALUES (1), (2), (3), (4)')
        connection.exec_driver_sql(
            'INSERT INTO advertising_campaign_registrations (id, campaign_id, user_id, created_at) VALUES '
            "(1, 1, 10, '2026-01-01 00:00:00'), "
            "(2, 2, 10, '2026-01-02 00:00:00'), "
            "(3, 2, 11, '2026-01-01 00:00:00'), "
            "(4, 1, 11, '2026-01-01 00:00:00'), "
            '(5, 3, 12, NULL), '
            "(6, 1, 13, '2026-01-03 00:00:00'), "
            "(7, 1, 13, '2026-01-03 00:00:00'), "
            "(8, 2, 14, '2026-01-01 00:00:00'), "
            "(9, 2, 15, '2026-01-01 00:00:00')"
        )
        connection.exec_driver_sql(
            'INSERT INTO users (id, has_had_paid_subscription) VALUES '
            '(10, 1), (11, 1), (12, 0), (13, 1), (14, 1), (15, 1)'
        )
        connection.exec_driver_sql(
            'INSERT INTO subscription_checkouts (id, terminal_reason) VALUES '
            "(100, NULL), (101, 'cancelled_by_operator_review'), (102, 'cancelled_by_operator_review'), "
            "(103, 'post_paid_provider_terminal:chargebacked')"
        )
        connection.exec_driver_sql(
            'INSERT INTO checkout_payment_attempts '
            '(id, checkout_id, provider_payment_id, reconciliation_reason) VALUES '
            "(1, 101, 'attempt-reversed', 'post_paid_provider_terminal:chargebacked'), "
            "(2, 102, 'event-reversed', 'operator_refund_wallet_credit')"
        )
        connection.exec_driver_sql(
            'INSERT INTO device_first_provider_events '
            '(id, checkout_id, provider_payment_id, provider_status) VALUES '
            "(1, 102, 'event-reversed', 'CHARGEBACKED')"
        )

        rows = [
            (1, 10, 'deposit', 10_000, 'platega', 1, '2026-01-01 01:00:00', '2026-01-01 01:00:00', None, None),
            (
                2,
                10,
                'subscription_payment',
                -10_000,
                'balance',
                1,
                '2026-01-01 01:00:01',
                '2026-01-01 01:00:01',
                None,
                None,
            ),
            (
                3,
                10,
                'provider_receipt',
                20_000,
                'platega',
                1,
                '2026-01-02 00:00:00',
                '2026-01-02 00:00:00',
                100,
                'valid',
            ),
            (4, 10, 'deposit', 30_000, 'manual', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (5, 10, 'deposit', 40_000, 'platega', 0, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (6, 10, 'deposit', -50_000, 'platega', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (7, 10, 'deposit', 60_000, 'platega', 1, '2025-12-31 23:59:59', '2025-12-31 23:59:59', None, None),
            (8, 10, 'deposit', 70_000, 'yookassa', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (
                9,
                11,
                'provider_receipt',
                90_000,
                'platega',
                1,
                '2026-01-02 00:00:00',
                '2026-01-02 00:00:00',
                101,
                'attempt-reversed',
            ),
            (10, 11, 'deposit', 5_000, 'platega', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (11, 12, 'deposit', 8_000, 'platega', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
            (12, 13, 'deposit', 11_000, 'platega', 1, '2026-01-04 00:00:00', '2026-01-04 00:00:00', None, None),
            (13, 13, 'deposit', 12_000, 'platega', 1, '2026-01-05 00:00:00', '2026-01-05 00:00:00', None, None),
            (
                14,
                14,
                'provider_receipt',
                99_000,
                'platega',
                1,
                '2026-01-02 00:00:00',
                '2026-01-02 00:00:00',
                102,
                'event-reversed',
            ),
            (15, 13, 'deposit', 1_000, 'platega', 1, None, '2026-01-06 00:00:00', None, None),
            (
                16,
                15,
                'provider_receipt',
                100_000,
                'platega',
                1,
                '2026-01-02 00:00:00',
                '2026-01-02 00:00:00',
                103,
                'terminal-reversed',
            ),
            (17, 10, 'deposit', 80_000, 'cloudpayments', 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', None, None),
        ]
        connection.exec_driver_sql(
            'INSERT INTO transactions '
            '(id, user_id, type, amount_kopeks, payment_method, is_completed, completed_at, created_at, '
            'device_first_checkout_id, external_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            rows,
        )
        connection.exec_driver_sql('INSERT INTO yookassa_payments (id, transaction_id, test_mode) VALUES (1, 8, 1)')
        connection.exec_driver_sql(
            'INSERT INTO cloudpayments_payments (id, transaction_id, test_mode) VALUES (1, 17, 1)'
        )

        result = {
            row['campaign_id']: row for row in connection.execute(_campaign_analytics_statement()).mappings().all()
        }
        filtered_result = connection.execute(_campaign_analytics_statement([2])).mappings().all()

    assert result[1]['leads'] == 2  # users 10 and 13; duplicate same-campaign touch is one lead
    assert result[1]['paying_leads'] == 2
    assert result[1]['confirmed_receipts_kopeks'] == 54_000
    assert result[1]['registrations'] == 4  # legacy raw-row field includes the later cross-campaign touch
    assert result[1]['total_revenue_kopeks'] == 199_000  # duplicate same-campaign row does not multiply deposits
    assert result[2]['leads'] == 3  # same-time tie goes to lower registration id; later touch is ignored
    assert result[2]['paying_leads'] == 1
    assert result[2]['confirmed_receipts_kopeks'] == 5_000
    assert result[3]['leads'] == 1
    assert result[3]['paying_leads'] == 0  # unknown touch time is conservative: no money attribution
    assert result[3]['confirmed_receipts_kopeks'] == 0
    assert result[4]['leads'] == 0
    assert result[4]['confirmed_receipts_kopeks'] == 0
    assert [row['campaign_id'] for row in filtered_result] == [2]
    assert filtered_result[0]['leads'] == 3  # filtering campaigns never re-ranks global first-touch


@pytest.mark.asyncio
async def test_empty_campaign_id_page_does_not_query() -> None:
    db = SimpleNamespace(execute=AsyncMock())
    assert await get_campaign_analytics(db, []) == {}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_campaign_delete_locks_parent_then_refuses_existing_attribution() -> None:
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[4, True]),
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    assert await delete_campaign_if_unattributed(db, 4) is False
    lock_sql = str(
        db.scalar.await_args_list[0]
        .args[0]
        .compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    ).lower()
    history_sql = str(
        db.scalar.await_args_list[1]
        .args[0]
        .compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    ).lower()
    assert 'for update' in lock_sql
    assert 'exists (select advertising_campaign_registrations.id' in history_sql
    assert 'advertising_campaign_registrations.campaign_id = 4' in history_sql
    db.execute.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_campaign_delete_after_locked_empty_history_commits_exact_row() -> None:
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[4, False]),
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    assert await delete_campaign_if_unattributed(db, 4) is True
    delete_sql = str(
        db.execute.await_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})
    ).lower()
    assert 'delete from advertising_campaigns' in delete_sql
    assert 'advertising_campaigns.id = 4' in delete_sql
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_campaign_delete_missing_row_rolls_back_without_delete() -> None:
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    assert await delete_campaign_if_unattributed(db, 404) is False
    db.execute.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_every_live_delete_entrypoint_uses_the_attribution_guard() -> None:
    project_root = Path(__file__).resolve().parents[2]
    offenders = []
    for path in (project_root / 'app').rglob('*.py'):
        source = path.read_text()
        relative_path = path.relative_to(project_root)
        tree = ast.parse(source)
        imports_legacy_delete = any(
            isinstance(node, ast.ImportFrom)
            and node.module == 'app.database.crud.campaign'
            and any(alias.name == 'delete_campaign' for alias in node.names)
            for node in ast.walk(tree)
        )
        calls_legacy_delete = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == 'delete_campaign')
                or (isinstance(node.func, ast.Attribute) and node.func.attr == 'delete_campaign')
            )
            for node in ast.walk(tree)
        )
        raw_parent_delete_outside_approved_files = 'delete(AdvertisingCampaign)' in source and str(
            relative_path
        ) not in {'app/database/crud/campaign.py', 'app/services/campaign_service.py'}
        if imports_legacy_delete or calls_legacy_delete or raw_parent_delete_outside_approved_files:
            offenders.append(str(relative_path))
    assert offenders == []


@pytest.mark.asyncio
async def test_top_campaign_api_exposes_new_totals_in_two_fixed_queries() -> None:
    from app.cabinet.routes import admin_stats

    campaigns = [
        SimpleNamespace(
            id=4,
            name='Кувалда',
            start_parameter='teplo2',
            bonus_type='none',
            is_active=True,
            created_at=None,
        ),
        SimpleNamespace(
            id=3,
            name='Киношная',
            start_parameter='teplovpn1',
            bonus_type='none',
            is_active=True,
            created_at=None,
        ),
    ]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _ScalarsResult(campaigns),
                _MappingsResult(
                    [
                        _row(),
                        _row(
                            campaign_id=3,
                            registrations=7,
                            conversion_count=1,
                            paid_flag_count=1,
                            leads=7,
                            paying_leads=1,
                            confirmed_receipts_kopeks=24_900,
                        ),
                    ]
                ),
            ]
        )
    )

    response = await admin_stats.get_top_campaigns(limit=10, admin=SimpleNamespace(id=1), db=db)

    assert response.total_campaigns == 2
    assert response.total_registrations == 114
    assert response.total_revenue_kopeks == 0
    assert response.total_leads == 114
    assert response.total_paying_leads == 5
    assert response.payment_conversion_rate == 4.4
    assert response.confirmed_receipts_kopeks == 228_500
    assert [item.id for item in response.campaigns] == [4, 3]
    assert response.campaigns[0].confirmed_receipts_kopeks == 203_600
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_campaign_list_route_exposes_first_touch_fields(monkeypatch) -> None:
    from app.cabinet.routes import admin_campaigns

    campaign = SimpleNamespace(
        id=4,
        name='Кувалда',
        start_parameter='teplo2',
        bonus_type='none',
        is_active=True,
        partner_user_id=None,
        partner=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    metrics = CampaignAnalytics(
        campaign_id=4,
        registrations=107,
        conversion_count=4,
        paid_users_count=4,
        conversion_rate=3.7,
        total_revenue_kopeks=0,
        avg_revenue_per_user_kopeks=0,
        leads=107,
        paying_leads=4,
        payment_conversion_rate=3.7,
        confirmed_receipts_kopeks=203_600,
        avg_confirmed_receipts_per_lead_kopeks=1902,
    )
    monkeypatch.setattr(admin_campaigns, 'get_campaigns_list', AsyncMock(return_value=[campaign]))
    monkeypatch.setattr(admin_campaigns, 'get_campaigns_count', AsyncMock(return_value=1))
    monkeypatch.setattr(admin_campaigns, 'get_campaign_analytics', AsyncMock(return_value={4: metrics}))

    response = await admin_campaigns.list_campaigns(
        include_inactive=True,
        offset=0,
        limit=50,
        admin=SimpleNamespace(id=1),
        db=SimpleNamespace(),
    )

    assert response.total == 1
    assert response.campaigns[0].leads == 107
    assert response.campaigns[0].paying_leads == 4
    assert response.campaigns[0].payment_conversion_rate == 3.7
    assert response.campaigns[0].confirmed_receipts_kopeks == 203_600
    assert response.campaigns[0].total_revenue_kopeks == 0


@pytest.mark.asyncio
async def test_campaign_detail_route_exposes_first_touch_fields(monkeypatch) -> None:
    from app.cabinet.routes import admin_campaigns

    campaign = SimpleNamespace(
        id=4,
        name='Кувалда',
        start_parameter='teplo2',
        bonus_type='none',
        is_active=True,
    )
    legacy = {
        'registrations': 107,
        'balance_issued': 0,
        'subscription_issued': 0,
        'last_registration': None,
        'total_revenue_kopeks': 0,
        'avg_revenue_per_user_kopeks': 0,
        'avg_first_payment_kopeks': 0,
        'trial_users_count': 0,
        'active_trials_count': 0,
        'conversion_count': 4,
        'paid_users_count': 4,
        'conversion_rate': 3.7,
        'trial_conversion_rate': 0.0,
    }
    metrics = CampaignAnalytics(
        campaign_id=4,
        registrations=107,
        conversion_count=4,
        paid_users_count=4,
        conversion_rate=3.7,
        total_revenue_kopeks=0,
        avg_revenue_per_user_kopeks=0,
        leads=107,
        paying_leads=4,
        payment_conversion_rate=3.7,
        confirmed_receipts_kopeks=203_600,
        avg_confirmed_receipts_per_lead_kopeks=1902,
    )
    monkeypatch.setattr(admin_campaigns, 'get_campaign_by_id', AsyncMock(return_value=campaign))
    monkeypatch.setattr(admin_campaigns, 'get_campaign_statistics', AsyncMock(return_value=legacy))
    monkeypatch.setattr(admin_campaigns, 'get_campaign_analytics', AsyncMock(return_value={4: metrics}))

    response = await admin_campaigns.get_campaign_stats(
        campaign_id=4,
        admin=SimpleNamespace(id=1),
        db=SimpleNamespace(),
    )

    assert response.leads == 107
    assert response.paying_leads == 4
    assert response.payment_conversion_rate == 3.7
    assert response.confirmed_receipts_kopeks == 203_600
    assert response.avg_confirmed_receipts_per_lead_kopeks == 1902
    assert response.total_revenue_kopeks == 0

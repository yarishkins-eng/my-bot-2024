from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.admin_broadcasts import (
    ARCHIVED_BROADCAST_TARIFF_IDS,
    CUSTOM_FILTER_GROUPS,
    CUSTOM_FILTER_LABELS,
    FILTER_GROUPS,
    FILTER_LABELS,
    _broadcast_target_label,
)
from app.database.models import SubscriptionStatus
from app.handlers.admin import messages as admin_messages
from app.utils import user_utils


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _RowResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _PaymentDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _ScalarResult([1, 2])
        return _RowResult(
            [
                (3, 90, True, 'platega_2'),  # gift: buyer 3 paid, recipient 90 did not
                (None, 4, False, 'yookassa_sbp'),  # landing buyer linked only as recipient
                (None, 5, True, 'platega'),  # anonymous gift recipient is not the payer
                (6, 91, True, 'balance'),  # wallet/manual money is not an external payment
            ]
        )


@pytest.mark.asyncio
async def test_real_payment_evidence_combines_ledger_and_guest_purchase_fallback() -> None:
    db = _PaymentDB()

    assert await user_utils.real_payment_user_ids(db, range(1, 100)) == {1, 2, 3, 4}
    assert len(db.statements) == 2  # batch queries, never one query per user

    ledger_sql = str(db.statements[0])
    assert 'transactions.is_completed IS true' in ledger_sql
    assert 'transactions.amount_kopeks !=' in ledger_sql
    assert 'transactions.type IN' in ledger_sql
    assert 'transactions.payment_method IN' in ledger_sql
    ledger_params = db.statements[0].compile().params.values()
    transaction_types = next(value for value in ledger_params if isinstance(value, list) and 'deposit' in value)
    assert set(transaction_types) == {
        'deposit',
        'provider_receipt',
        'subscription_payment',
        'gift_payment',
    }
    payment_methods = next(value for value in ledger_params if isinstance(value, list) and 'platega' in value)
    assert 'manual' not in payment_methods
    assert 'balance' not in payment_methods

    guest_sql = str(db.statements[1])
    assert 'guest_purchases.paid_at IS NOT NULL' in guest_sql
    assert 'guest_purchases.amount_kopeks >' in guest_sql
    assert 'guest_purchases.buyer_user_id IN' in guest_sql
    assert 'guest_purchases.is_gift IS false' in guest_sql


def _subscription(
    *,
    now: datetime,
    is_trial: bool,
    active: bool = False,
    in_grace: bool = False,
    status: str | None = None,
    end_date: datetime | None = None,
):
    return SimpleNamespace(
        is_trial=is_trial,
        is_active=active,
        status=status or (SubscriptionStatus.ACTIVE.value if active else SubscriptionStatus.EXPIRED.value),
        end_date=end_date or (now + timedelta(days=5) if active else now - timedelta(days=1)),
        in_grace=in_grace,
        grace_until=now + timedelta(days=1) if in_grace else None,
    )


def _user(
    user_id: int,
    subscriptions: list,
    *,
    now: datetime | None = None,
    paid_before: bool = False,
):
    moment = now or datetime.now(UTC)
    return SimpleNamespace(
        id=user_id,
        subscriptions=subscriptions,
        has_had_paid_subscription=paid_before,
        created_at=moment,
        last_activity=moment,
    )


@pytest.mark.asyncio
async def test_new_expired_segments_use_real_payment_and_exclude_current_or_bonus_access(monkeypatch) -> None:
    now = datetime.now(UTC)
    users = [
        _user(1, [_subscription(now=now, is_trial=True)]),
        _user(2, [_subscription(now=now, is_trial=True)]),  # trial followed by a real payment
        _user(3, [_subscription(now=now, is_trial=False)]),  # free non-trial admin grant
        _user(4, [], paid_before=True),  # old №6 fallback whose subscription row was removed
        _user(5, [_subscription(now=now, is_trial=False, active=True)]),
        _user(6, [_subscription(now=now, is_trial=False, in_grace=True)]),
        _user(
            7,
            [
                _subscription(now=now, is_trial=True),
                _subscription(now=now, is_trial=False, active=True),
            ],
        ),
        _user(
            8,
            [
                _subscription(
                    now=now,
                    is_trial=False,
                    status=SubscriptionStatus.LIMITED.value,
                    end_date=now + timedelta(days=5),
                )
            ],
        ),
        _user(
            9,
            [
                _subscription(
                    now=now,
                    is_trial=True,
                    status=SubscriptionStatus.TRIAL.value,
                    end_date=now + timedelta(days=2),
                )
            ],
        ),
    ]
    monkeypatch.setattr(
        admin_messages,
        'real_payment_user_ids',
        AsyncMock(return_value={2, 4, 5, 6, 8, 9}),
    )

    expired_trials = await admin_messages.get_target_users(
        object(),
        'expired_trial_unpaid',
        preloaded_users=users,
    )
    former_payers = await admin_messages.get_target_users(
        object(),
        'former_payer_no_subscription',
        preloaded_users=users,
    )

    assert [user.id for user in expired_trials] == [1]
    assert [user.id for user in former_payers] == [2, 4]


@pytest.mark.parametrize(
    ('criteria', 'included_days', 'excluded_days'),
    [
        ('registered_0_7_unpaid', [0, 7], [-1, 7.00001, 30]),
        ('registered_8_30_unpaid', [7.00001, 30], [-1, 7, 30.00001]),
        ('inactive_7_29', [7, 29.99999], [-1, 6.99999, 30]),
        ('inactive_30_89', [30, 89.99999], [-1, 29.99999, 90]),
        ('inactive_90_plus', [90, 120], [-1, 89.99999]),
    ],
)
def test_new_time_segments_are_non_overlapping_at_boundaries(
    criteria: str,
    included_days: list[float],
    excluded_days: list[float],
) -> None:
    now = datetime(2026, 9, 2, 12, tzinfo=UTC)
    date_field = 'created_at' if criteria.startswith('registered_') else 'last_activity'

    for days in included_days:
        user = _user(1, [], now=now)
        setattr(user, date_field, now - timedelta(days=days))
        assert admin_messages._matches_new_time_segment(user, criteria, now)

    for days in excluded_days:
        user = _user(1, [], now=now)
        setattr(user, date_field, now - timedelta(days=days))
        assert not admin_messages._matches_new_time_segment(user, criteria, now)

    if date_field == 'last_activity':
        user = _user(1, [], now=now)
        user.last_activity = None
        assert not admin_messages._matches_new_time_segment(user, criteria, now)


def test_catalog_keeps_old_keys_in_archive_and_new_keys_active() -> None:
    assert FILTER_GROUPS['expired_trial_unpaid'] == 'subscription'
    assert FILTER_GROUPS['former_payer_no_subscription'] == 'subscription'
    assert CUSTOM_FILTER_GROUPS['custom_registered_0_7_unpaid'] == 'registration'
    assert CUSTOM_FILTER_GROUPS['custom_registered_8_30_unpaid'] == 'registration'
    assert CUSTOM_FILTER_GROUPS['custom_inactive_7_29'] == 'activity'
    assert CUSTOM_FILTER_GROUPS['custom_inactive_30_89'] == 'activity'
    assert CUSTOM_FILTER_GROUPS['custom_inactive_90_plus'] == 'activity'

    assert {key for key, group in FILTER_GROUPS.items() if group == 'archive'} == {
        'trial',
        'expiring',
        'expired',
        'zero',
        'active_zero',
        'trial_zero',
    }
    assert {key for key, group in CUSTOM_FILTER_GROUPS.items() if group == 'archive'} == {
        'custom_today',
        'custom_week',
        'custom_month',
        'custom_active_today',
        'custom_inactive_week',
        'custom_inactive_month',
    }
    assert {4} == ARCHIVED_BROADCAST_TARIFF_IDS

    # Old history remains truthful: no old key was renamed or reused.
    assert FILTER_LABELS['expired'] == 'Закончилась (включая пробные)'
    assert CUSTOM_FILTER_LABELS['custom_week'] == 'Регистрация за неделю'
    assert _broadcast_target_label('expired') == 'Закончилась (включая пробные)'
    assert _broadcast_target_label('former_payer_no_subscription') == ('Раньше платил — сейчас без подписки')


@pytest.mark.asyncio
async def test_filter_catalog_archives_team_but_keeps_basic_tariff_active(monkeypatch) -> None:
    async def fake_get_target_users(db, target):
        return []

    async def fake_recipients(db, target, category, admin, **kwargs):
        return []

    async def fake_tariff_counts(db, category, *, preloaded_users=None):
        return {3: 51, 4: 30}

    class _TariffResult:
        def scalars(self):
            return self

        def all(self):
            return [
                SimpleNamespace(id=3, name='Базовый'),
                SimpleNamespace(id=4, name='Team'),
            ]

    class _DB:
        async def execute(self, statement):
            return _TariffResult()

    monkeypatch.setattr(admin_messages, 'get_target_users', fake_get_target_users)
    monkeypatch.setattr(
        'app.cabinet.routes.admin_broadcasts.get_target_users',
        fake_get_target_users,
    )
    monkeypatch.setattr(
        'app.cabinet.routes.admin_broadcasts._resolve_cabinet_telegram_recipients',
        fake_recipients,
    )
    monkeypatch.setattr(
        'app.cabinet.routes.admin_broadcasts._get_tariff_user_counts',
        fake_tariff_counts,
    )

    from app.cabinet.routes import admin_broadcasts

    response = await admin_broadcasts.get_filters(
        category='system',
        admin=SimpleNamespace(id=7),
        db=_DB(),
    )
    groups = {item.tariff_id: item.group for item in response.tariff_filters}
    assert groups == {3: 'tariff', 4: 'archive'}

"""Guards for the truthful trial and paying cards on the cabinet users screen."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_users


def _scalar_result(value: int) -> SimpleNamespace:
    return SimpleNamespace(scalar=lambda: value)


def _scalars_result(values: list[int]) -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))


def _sql(statement) -> str:
    return str(statement.compile(compile_kwargs={'literal_binds': True}))


def _tariff(tariff_id: int, *, free: bool = False, trial: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=tariff_id, is_free=free, is_trial_available=trial)


@pytest.mark.asyncio
async def test_truthful_cards_use_canonical_trial_and_real_payment_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7454290913,7749231125')
    monkeypatch.setattr(admin_users, 'get_trial_tariff', AsyncMock(return_value=_tariff(5, trial=True)))
    monkeypatch.setattr(
        admin_users,
        'get_all_tariffs',
        AsyncMock(return_value=[_tariff(3), _tariff(4, free=True), _tariff(5, trial=True)]),
    )
    real_payment_ids = AsyncMock(return_value={11, 12})
    monkeypatch.setattr(admin_users, 'real_payment_user_ids', real_payment_ids)
    db = AsyncMock()
    db.execute.side_effect = [_scalar_result(21), _scalars_result([11, 12, 13])]

    result = await admin_users._count_trial_and_paying_users(db)

    assert result == {'on_trial': 21, 'paying': 2}
    trial_sql = _sql(db.execute.await_args_list[0].args[0])
    assert 'count(distinct(subscriptions.user_id))' in trial_sql.lower()
    assert "subscriptions.status IN ('active', 'limited', 'trial')" in trial_sql
    assert 'subscriptions.end_date >' in trial_sql
    assert 'subscriptions.is_trial IS true' in trial_sql
    assert 'subscriptions.tariff_id = 5' in trial_sql

    paying_sql = _sql(db.execute.await_args_list[1].args[0])
    assert 'SELECT DISTINCT subscriptions.user_id' in paying_sql
    assert 'subscriptions.is_trial IS NOT true' in paying_sql
    assert 'subscriptions.tariff_id IS NULL' in paying_sql
    assert 'subscriptions.tariff_id NOT IN (4, 5)' in paying_sql
    assert "users.status != 'deleted'" in paying_sql
    assert 'users.test_account_enabled IS false' in paying_sql
    assert 'users.telegram_id IS NULL' in paying_sql
    assert 'users.telegram_id NOT IN (7454290913, 7749231125)' in paying_sql
    real_payment_ids.assert_awaited_once_with(db, {11, 12, 13})


@pytest.mark.asyncio
async def test_missing_trial_tariff_skips_only_the_trial_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '')
    monkeypatch.setattr(admin_users, 'get_trial_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(admin_users, 'get_all_tariffs', AsyncMock(return_value=[]))
    real_payment_ids = AsyncMock(return_value={7})
    monkeypatch.setattr(admin_users, 'real_payment_user_ids', real_payment_ids)
    db = AsyncMock()
    db.execute.return_value = _scalars_result([7])

    result = await admin_users._count_trial_and_paying_users(db)

    assert result == {'on_trial': 0, 'paying': 1}
    assert db.execute.await_count == 1
    assert 'subscriptions.is_trial IS NOT true' in _sql(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_no_paying_candidates_skips_payment_ledger_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '')
    monkeypatch.setattr(admin_users, 'get_trial_tariff', AsyncMock(return_value=_tariff(5, trial=True)))
    monkeypatch.setattr(admin_users, 'get_all_tariffs', AsyncMock(return_value=[]))
    real_payment_ids = AsyncMock()
    monkeypatch.setattr(admin_users, 'real_payment_user_ids', real_payment_ids)
    db = AsyncMock()
    db.execute.side_effect = [_scalar_result(0), _scalars_result([])]

    result = await admin_users._count_trial_and_paying_users(db)

    assert result == {'on_trial': 0, 'paying': 0}
    real_payment_ids.assert_not_awaited()

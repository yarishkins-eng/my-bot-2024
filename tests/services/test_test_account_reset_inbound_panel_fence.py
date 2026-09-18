"""Regression tests for stale panel snapshots across a test-account reset."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.database.crud.subscription as subscription_crud
from app.config import Settings
from app.services.remnawave_service import RemnaWaveService


class _FreshUserDb:
    """The reset happened in another session; this is its durable User row."""

    def __init__(self, user) -> None:
        self.user = user
        self.scalar = AsyncMock(return_value=user)


def _single_tariff(monkeypatch) -> None:
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


def _retired_generation() -> tuple[SimpleNamespace, SimpleNamespace]:
    # `captured` is what the status sync had before it fetched P1.  `current`
    # is the same row after reset A/P1 and subsequent new trial B/P2.
    captured = SimpleNamespace(id=41, telegram_id=7749231125, test_reset_started_at=None)
    current = SimpleNamespace(
        id=41,
        telegram_id=7749231125,
        remnawave_uuid='P2-current',
        test_reset_started_at=datetime.now(UTC),
        test_reset_state='ready',
        test_reset_panel_uuids=['P1-retired'],
    )
    return captured, current


async def test_stale_status_snapshot_cannot_overwrite_new_trial_url(monkeypatch) -> None:
    """P1 list entry must not be applied to B/P2 after reset completed."""
    _single_tariff(monkeypatch)
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    captured, current = _retired_generation()
    db = _FreshUserDb(current)
    service = RemnaWaveService()

    new_trial = SimpleNamespace(
        id=99,
        subscription_url='https://panel.example/P2-current',
        end_date=datetime.now(UTC) + timedelta(days=3),
    )
    current_subscription = AsyncMock(return_value=new_trial)
    monkeypatch.setattr(subscription_crud, 'get_subscription_by_user_id', current_subscription)

    await service._update_subscription_from_panel_data(
        db,
        captured,
        {
            'uuid': 'P1-retired',
            'status': 'ACTIVE',
            'expireAt': '2030-01-01T00:00:00Z',
            'subscriptionUrl': 'https://panel.example/P1-retired',
            'usedTrafficBytes': 987654321,
        },
    )

    # The inbound helper locks and re-reads the User before the pre-reset
    # `get_subscription_by_user_id` fallback can select B.
    db.scalar.assert_awaited_once()
    current_subscription.assert_not_awaited()
    assert new_trial.subscription_url == 'https://panel.example/P2-current'


async def test_current_generation_snapshot_is_still_accepted(monkeypatch) -> None:
    """The fence rejects P1, not the legitimate P2 status webhook/sync."""
    _single_tariff(monkeypatch)
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    captured, current = _retired_generation()
    db = _FreshUserDb(current)

    accepted = await RemnaWaveService()._accept_test_account_panel_snapshot(db, captured, {'uuid': 'P2-current'})

    assert accepted is True
    db.scalar.assert_awaited_once()


async def test_configured_test_account_without_reset_history_keeps_normal_sync(monkeypatch) -> None:
    """The fresh lock is a fence, not a ban on an untouched test fixture."""
    _single_tariff(monkeypatch)
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    captured = SimpleNamespace(id=41, telegram_id=7749231125, test_reset_started_at=None)
    current = SimpleNamespace(
        id=41,
        telegram_id=7749231125,
        remnawave_uuid='P1-first',
        test_reset_started_at=None,
        test_reset_state=None,
        test_reset_panel_uuids=None,
    )
    db = _FreshUserDb(current)

    accepted = await RemnaWaveService()._accept_test_account_panel_snapshot(db, captured, {'uuid': 'P1-first'})

    assert accepted is True
    db.scalar.assert_awaited_once()


async def test_stale_snapshot_cannot_create_fallback_subscription(monkeypatch) -> None:
    """A failed import path must not recreate a retired P1 subscription."""
    _single_tariff(monkeypatch)
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    captured, current = _retired_generation()
    db = _FreshUserDb(current)
    create_subscription = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'create_subscription_no_commit', create_subscription)

    await RemnaWaveService()._create_subscription_from_panel_data(
        db,
        captured,
        {
            'uuid': 'P1-retired',
            'status': 'ACTIVE',
            'expireAt': '2030-01-01T00:00:00Z',
            'subscriptionUrl': 'https://panel.example/P1-retired',
        },
    )

    create_subscription.assert_not_awaited()

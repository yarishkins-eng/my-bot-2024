"""Real PostgreSQL concurrency checks for device-first financial constraints."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


if not hasattr(asyncpg, 'connect'):
    # The global unit-test fixture installs a lightweight asyncpg import stub.
    # This explicitly restores the real driver only for the opt-in PostgreSQL test.
    sys.modules.pop('asyncpg', None)
    asyncpg = importlib.import_module('asyncpg')

DATABASE_URL = os.getenv('DEVICE_FIRST_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason='DEVICE_FIRST_TEST_DATABASE_URL is required for PostgreSQL constraint tests',
    ),
]


async def _seed_owner_and_tariff(connection: asyncpg.Connection) -> tuple[int, int]:
    user_id = await connection.fetchval(
        """
        INSERT INTO users (
            auth_type,
            has_had_paid_subscription,
            email_verified,
            auto_promo_group_assigned,
            auto_promo_group_threshold_kopeks,
            promo_offer_discount_percent,
            has_made_first_topup,
            restriction_topup,
            restriction_subscription,
            partner_status
        )
        VALUES ('telegram', false, false, false, 0, 0, false, false, false, 'none')
        RETURNING id
        """
    )
    tariff_id = await connection.fetchval(
        """
        INSERT INTO tariffs (
            name,
            display_order,
            is_active,
            traffic_limit_gb,
            device_limit,
            period_prices,
            tier_level,
            is_trial_available,
            allow_traffic_topup,
            traffic_topup_enabled,
            max_topup_traffic_gb,
            is_daily,
            daily_price_kopeks,
            custom_days_enabled,
            price_per_day_kopeks,
            min_days,
            max_days,
            custom_traffic_enabled,
            traffic_price_per_gb_kopeks,
            min_traffic_gb,
            max_traffic_gb
        )
        VALUES (
            $1, 0, true, 0, 2, '{"30": 30000}'::json, 1, false,
            false, false, 0, false, 0, false, 0, 1, 365, false, 0, 0, 0
        )
        RETURNING id
        """,
        f'device-first-test-{uuid.uuid4()}',
    )
    return user_id, tariff_id


async def _insert_checkout(
    connection: asyncpg.Connection,
    *,
    user_id: int,
    tariff_id: int,
) -> None:
    await connection.execute(
        """
        INSERT INTO subscription_checkouts (
            public_id,
            user_id,
            source,
            tariff_id,
            expect_no_subscription,
            target_snapshot,
            period_days,
            selected_device_limit,
            price_breakdown,
            quoted_price_kopeks,
            max_price_kopeks,
            pricing_revision,
            quote_expires_at,
            expires_at
        )
        VALUES (
            $1, $2, 'cabinet', $3, true, '{}'::json, 30, 2, '{}'::json,
            30000, 30000, 1, now() + interval '30 minutes', now() + interval '24 hours'
        )
        """,
        str(uuid.uuid4()),
        user_id,
        tariff_id,
    )


async def test_only_one_concurrent_open_checkout_per_user() -> None:
    setup = await asyncpg.connect(DATABASE_URL)
    first = await asyncpg.connect(DATABASE_URL)
    second = await asyncpg.connect(DATABASE_URL)
    try:
        user_id, tariff_id = await _seed_owner_and_tariff(setup)

        results = await asyncio.gather(
            _insert_checkout(first, user_id=user_id, tariff_id=tariff_id),
            _insert_checkout(second, user_id=user_id, tariff_id=tariff_id),
            return_exceptions=True,
        )

        assert sum(result is None for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], asyncpg.UniqueViolationError)
    finally:
        await first.close()
        await second.close()
        await setup.close()


async def test_duplicate_financial_ledger_key_is_rejected_concurrently() -> None:
    setup = await asyncpg.connect(DATABASE_URL)
    first = await asyncpg.connect(DATABASE_URL)
    second = await asyncpg.connect(DATABASE_URL)
    try:
        user_id, _ = await _seed_owner_and_tariff(setup)
        ledger_key = f'device-first-test:{uuid.uuid4()}'

        async def insert(connection: asyncpg.Connection) -> None:
            await connection.execute(
                """
                INSERT INTO transactions (
                    user_id,
                    type,
                    amount_kopeks,
                    device_first_ledger_key
                )
                VALUES ($1, 'deposit', 10000, $2)
                """,
                user_id,
                ledger_key,
            )

        results = await asyncio.gather(
            insert(first),
            insert(second),
            return_exceptions=True,
        )

        assert sum(result is None for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], asyncpg.UniqueViolationError)
    finally:
        await first.close()
        await second.close()
        await setup.close()


async def _seed_concurrent_first_topups(
    connection: asyncpg.Connection,
) -> tuple[int, int, tuple[int, int]]:
    referrer_id = await connection.fetchval(
        """
        INSERT INTO users (
            auth_type,
            first_name,
            balance_kopeks,
            has_had_paid_subscription,
            email_verified,
            auto_promo_group_assigned,
            auto_promo_group_threshold_kopeks,
            promo_offer_discount_percent,
            has_made_first_topup,
            restriction_topup,
            restriction_subscription,
            partner_status
        )
        VALUES ('telegram', 'Inviter', 0, false, false, false, 0, 0, false, false, false, 'none')
        RETURNING id
        """
    )
    referral_id = await connection.fetchval(
        """
        INSERT INTO users (
            auth_type,
            first_name,
            balance_kopeks,
            referred_by_id,
            has_had_paid_subscription,
            email_verified,
            auto_promo_group_assigned,
            auto_promo_group_threshold_kopeks,
            promo_offer_discount_percent,
            has_made_first_topup,
            restriction_topup,
            restriction_subscription,
            partner_status
        )
        VALUES ('telegram', 'Referral', 0, $1, false, false, false, 0, 0, false, false, false, 'none')
        RETURNING id
        """,
        referrer_id,
    )
    _, tariff_id = await _seed_owner_and_tariff(connection)
    checkout_id = await connection.fetchval(
        """
        INSERT INTO subscription_checkouts (
            public_id,
            user_id,
            source,
            tariff_id,
            expect_no_subscription,
            target_snapshot,
            period_days,
            selected_device_limit,
            price_breakdown,
            quoted_price_kopeks,
            max_price_kopeks,
            pricing_revision,
            quote_expires_at,
            expires_at
        )
        VALUES (
            $1, $2, 'cabinet', $3, true, '{}'::json, 30, 2, '{}'::json,
            30000, 30000, 1, now() + interval '30 minutes', now() + interval '24 hours'
        )
        RETURNING id
        """,
        str(uuid.uuid4()),
        referral_id,
        tariff_id,
    )
    job_ids: list[int] = []
    for suffix in ('first', 'second'):
        transaction_id = await connection.fetchval(
            """
            INSERT INTO transactions (
                user_id,
                type,
                amount_kopeks,
                description,
                device_first_checkout_id,
                device_first_ledger_key,
                is_completed,
                completed_at
            )
            VALUES ($1, 'deposit', 10000, $2, $3, $4, true, now())
            RETURNING id
            """,
            referral_id,
            f'concurrent {suffix} first-topup candidate',
            checkout_id,
            f'postgres-first-topup:{uuid.uuid4()}',
        )
        job_ids.append(
            await connection.fetchval(
                """
                INSERT INTO device_first_deposit_outbox (
                    transaction_id,
                    checkout_id,
                    status,
                    event_status,
                    referral_status,
                    fulfillment_status,
                    attempts,
                    available_at
                )
                VALUES ($1, $2, 'pending', 'pending', 'pending', 'not_required', 0, now())
                RETURNING id
                """,
                transaction_id,
                checkout_id,
            )
        )
    return referrer_id, referral_id, (job_ids[0], job_ids[1])


async def test_two_concurrent_first_topups_award_only_one_bonus_pair(monkeypatch) -> None:
    """Exercise the real ORM lock/refresh path against PostgreSQL."""
    from app.services import device_first_deposit_outbox_service as service

    setup = await asyncpg.connect(DATABASE_URL)
    engine_url = DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://', 1)
    engine = create_async_engine(engine_url, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first = session_factory()
    second = session_factory()
    original_get = AsyncSession.get
    both_loaded = asyncio.Event()
    load_count = 0
    load_count_lock = asyncio.Lock()

    async def synchronized_get(session, entity, ident, **kwargs):
        nonlocal load_count
        row = await original_get(session, entity, ident, **kwargs)
        async with load_count_lock:
            load_count += 1
            if load_count == 2:
                both_loaded.set()
        await asyncio.wait_for(both_loaded.wait(), timeout=5)
        return row

    monkeypatch.setattr(AsyncSession, 'get', synchronized_get)
    monkeypatch.setattr(service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(service, 'calculate_referral_commission_percent', AsyncMock(return_value=0))
    monkeypatch.setattr(service, '_is_commission_limit_reached', AsyncMock(return_value=False))

    try:
        referrer_id, referral_id, job_ids = await _seed_concurrent_first_topups(setup)
        await asyncio.gather(
            service._apply_referral_step(first, job_id=job_ids[0]),
            service._apply_referral_step(second, job_id=job_ids[1]),
        )

        assert await setup.fetchval(
            'SELECT has_made_first_topup FROM users WHERE id = $1',
            referral_id,
        )
        assert (
            await setup.fetchval(
                """
                SELECT count(*)
                FROM transactions
                WHERE device_first_ledger_key LIKE 'deposit-side-effect:%:referred-first-bonus'
                  AND user_id = $1
                """,
                referral_id,
            )
            == 1
        )
        assert (
            await setup.fetchval(
                """
                SELECT count(*)
                FROM transactions
                WHERE device_first_ledger_key LIKE 'deposit-side-effect:%:inviter-first-reward'
                  AND user_id = $1
                """,
                referrer_id,
            )
            == 1
        )
        assert (
            await setup.fetchval(
                """
                SELECT count(*)
                FROM referral_earnings
                WHERE user_id = $1
                  AND referral_id = $2
                  AND reason = 'referral_first_topup'
                """,
                referrer_id,
                referral_id,
            )
            == 1
        )
    finally:
        await first.close()
        await second.close()
        await engine.dispose()
        await setup.close()


async def _seed_terminal_direct_erasure_graph(connection: asyncpg.Connection) -> tuple[int, int, int, int]:
    """Create one exact unpaid direct invoice that is safe to anonymize."""
    user_id, tariff_id = await _seed_owner_and_tariff(connection)
    checkout_id = await connection.fetchval(
        """
        INSERT INTO subscription_checkouts (
            public_id, user_id, source, tariff_id, expect_no_subscription,
            target_snapshot, period_days, selected_device_limit, price_breakdown,
            quoted_price_kopeks, max_price_kopeks, pricing_revision,
            quote_expires_at, expires_at, lifecycle_state, quote_state,
            settlement_mode, funding_state, fulfillment_state, terminal_reason
        )
        VALUES (
            $1, $2, 'cabinet', $3, true, '{}'::json, 30, 2, '{}'::json,
            35000, 35000, 1, now() - interval '1 minute', now() + interval '24 hours',
            'cancelled', 'expired', 'direct_purchase_v2', 'invoice_terminal', 'not_started',
            'provider_terminal:canceled'
        )
        RETURNING id
        """,
        str(uuid.uuid4()),
        user_id,
        tariff_id,
    )
    payment_id = await connection.fetchval(
        """
        INSERT INTO platega_payments (
            user_id, platega_transaction_id, correlation_id, amount_kopeks,
            currency, payment_method_code, status, is_paid, metadata_json,
            callback_payload
        )
        VALUES ($1, $2, $3, 35000, 'RUB', 2, 'CANCELED', false, $4::json, '{}'::json)
        RETURNING id
        """,
        user_id,
        f'erasure-provider-{uuid.uuid4()}',
        str(uuid.uuid4()),
        '{"device_first_attempt_id": 0, "settlement_mode": "direct_purchase_v2"}',
    )
    attempt_id = await connection.fetchval(
        """
        INSERT INTO checkout_payment_attempts (
            checkout_id, merchant_order_key, provider, method_key,
            provider_method_code, currency, requested_amount_kopeks,
            settlement_mode, status, provider_payment_id, platega_payment_id,
            reconciliation_reason, terminal_observations, next_reconcile_at
        )
        VALUES (
            $1, $2, 'platega', 'sbp', 2, 'RUB', 35000,
            'direct_purchase_v2', 'failed', $3, $4,
            'provider_terminal:canceled', 0, now()
        )
        RETURNING id
        """,
        checkout_id,
        f'erasure-attempt-{uuid.uuid4()}',
        await connection.fetchval('SELECT platega_transaction_id FROM platega_payments WHERE id = $1', payment_id),
        payment_id,
    )
    await connection.execute(
        """
        UPDATE platega_payments
        SET metadata_json = jsonb_build_object(
            'device_first_attempt_id', $2::int, 'settlement_mode', 'direct_purchase_v2'
        )
        WHERE id = $1
        """,
        payment_id,
        attempt_id,
    )
    await connection.execute(
        """
        INSERT INTO account_erasure_requests (user_id, state, panel_state, has_legacy_financial_history)
        VALUES ($1, 'ready_for_anonymization', 'deactivated', false)
        """,
        user_id,
    )
    return user_id, checkout_id, attempt_id, payment_id


async def test_final_erasure_wins_lock_race_against_late_terminal_reconciliation() -> None:
    """The late poll waits for redaction, then performs a no-op on fresh rows."""
    from app.services.account_erasure_service import _complete_ready_financial_account_erasure
    from app.services.device_first_payment_service import _release_direct_terminal_invoice

    setup = await asyncpg.connect(DATABASE_URL)
    engine_url = DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://', 1)
    engine = create_async_engine(engine_url, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    erasure_db = session_factory()
    reconciliation_db = session_factory()
    final_commit_reached = asyncio.Event()
    allow_final_commit = asyncio.Event()
    original_commit = erasure_db.commit

    async def commit_after_reconciliation_is_waiting() -> None:
        final_commit_reached.set()
        await asyncio.wait_for(allow_final_commit.wait(), timeout=5)
        await original_commit()

    try:
        user_id, checkout_id, attempt_id, payment_id = await _seed_terminal_direct_erasure_graph(setup)
        erasure_db.commit = commit_after_reconciliation_is_waiting  # type: ignore[method-assign]
        erasure_task = asyncio.create_task(
            _complete_ready_financial_account_erasure(erasure_db, user_id=user_id, deactivate_panel=True)
        )
        await asyncio.wait_for(final_commit_reached.wait(), timeout=5)

        reconciliation_task = asyncio.create_task(
            _release_direct_terminal_invoice(
                reconciliation_db,
                attempt_id=attempt_id,
                payment_id=payment_id,
                payload={
                    'id': await setup.fetchval(
                        'SELECT platega_transaction_id FROM platega_payments WHERE id = $1', payment_id
                    ),
                    'status': 'CANCELED',
                    'paymentMethod': 'SBPQR',
                    'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
                },
                provider_status='CANCELED',
                source='poll',
            )
        )
        await asyncio.sleep(0.05)
        assert not reconciliation_task.done()

        allow_final_commit.set()
        completed = await asyncio.wait_for(erasure_task, timeout=5)
        released = await asyncio.wait_for(reconciliation_task, timeout=5)
        assert completed.completed is True
        assert released is False
        assert await setup.fetchval('SELECT account_erased_at IS NOT NULL FROM users WHERE id = $1', user_id)
        assert (
            await setup.fetchval('SELECT state FROM account_erasure_requests WHERE user_id = $1', user_id)
            == 'completed'
        )
        assert await setup.fetchval('SELECT metadata_json FROM platega_payments WHERE id = $1', payment_id) == '{}'
        assert await setup.fetchval('SELECT status FROM platega_payments WHERE id = $1', payment_id) == 'CANCELED'
        assert (
            await setup.fetchval('SELECT status FROM checkout_payment_attempts WHERE id = $1', attempt_id) == 'failed'
        )
        assert (
            await setup.fetchval('SELECT lifecycle_state FROM subscription_checkouts WHERE id = $1', checkout_id)
            == 'cancelled'
        )
    finally:
        await reconciliation_db.rollback()
        await erasure_db.rollback()
        await reconciliation_db.close()
        await erasure_db.close()
        await engine.dispose()
        await setup.close()


async def test_two_concurrent_fused_pay_clicks_birth_exactly_one_checkout(monkeypatch) -> None:
    """The unique open-checkout index decides: the loser resumes the winner."""
    from app.services import device_first_checkout_service as service

    setup = await asyncpg.connect(DATABASE_URL)
    engine_url = DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://', 1)
    engine = create_async_engine(engine_url, pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first = session_factory()
    second = session_factory()

    try:
        user_id, tariff_id = await _seed_owner_and_tariff(setup)
        options = {
            'eligible': True,
            'version': 2,
            'tariff': {
                'id': tariff_id,
                'name': 'Test',
                'traffic_limit_gb': 0,
                'base_device_limit': 2,
                'pricing_revision': 1,
            },
            'period_options': [30],
            'device_options': [2],
            'default_period_days': 30,
            'current_subscription': None,
            'balance_kopeks': 0,
            'price_matrix': [
                {
                    'period_days': 30,
                    'prices': [
                        {
                            'device_limit': 2,
                            'price_kopeks': 30000,
                            'breakdown': {
                                'base_price_kopeks': 30000,
                                'devices_price_kopeks': 0,
                                'promo_group_discount_kopeks': 0,
                                'promo_offer_discount_kopeks': 0,
                            },
                        }
                    ],
                }
            ],
        }
        monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
        monkeypatch.setattr(service, 'build_purchase_options', AsyncMock(return_value=options))
        user = SimpleNamespace(id=user_id, balance_kopeks=0)

        results = await asyncio.gather(
            service.create_or_resume_direct_checkout(
                first,
                user=user,
                period_days=30,
                selected_device_limit=2,
                expected_tariff_total_kopeks=30000,
                funding_mode='platega',
                method_key='sbp',
                source='cabinet',
            ),
            service.create_or_resume_direct_checkout(
                second,
                user=user,
                period_days=30,
                selected_device_limit=2,
                expected_tariff_total_kopeks=30000,
                funding_mode='platega',
                method_key='sbp',
                source='cabinet',
            ),
            return_exceptions=True,
        )

        failures = [result for result in results if isinstance(result, Exception)]
        assert failures == []
        assert [result.proceed_to_payment for result in results] == [True, True]
        assert results[0].checkout.public_id == results[1].checkout.public_id
        assert (
            await setup.fetchval(
                'SELECT count(*) FROM subscription_checkouts WHERE user_id = $1',
                user_id,
            )
            == 1
        )
        assert (
            await setup.fetchval(
                'SELECT lifecycle_state FROM subscription_checkouts WHERE user_id = $1',
                user_id,
            )
            == 'confirmed'
        )
    finally:
        await first.close()
        await second.close()
        await engine.dispose()
        await setup.close()

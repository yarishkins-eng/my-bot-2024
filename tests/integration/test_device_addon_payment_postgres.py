"""Real PostgreSQL regressions for add-on Platega settlement."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PaymentMethod,
    PlategaPayment,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
)
from app.services import device_addon_payment_service as payments
from app.services.device_addon_payment_service import (
    create_device_addon_topup,
    reconcile_device_addon_payment,
)
from app.services.device_addon_service import DeviceAddonError
from app.services.device_first_deposit_outbox_service import apply_deposit_referral_money
from app.services.payment.platega import PlategaPaymentMixin
from app.services.platega_service import PlategaService
from tests.integration.test_device_addon_lifecycle_postgres import DATABASE_URL, sessions  # noqa: F401


pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE_URL, reason='Requires isolated addon PostgreSQL')]


async def _late_payment_graph(db):
    user = User(
        telegram_id=7_781_000_000 + int(uuid.uuid4().hex[:5], 16),
        balance_kopeks=0,
        status='active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        remnawave_uuid=str(uuid.uuid4()),
    )
    db.add(user)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        status='active',
        is_trial=False,
        device_limit=2,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(subscription)
    await db.flush()
    intent = DeviceAddonIntent(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        subscription_id=subscription.id,
        target_subscription_id=subscription.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash='a' * 64,
        devices_to_add=1,
        original_device_limit=2,
        end_date=subscription.end_date,
        device_addon_generation=0,
        days_left=30,
        monthly_price_kopeks=10_000,
        base_price_kopeks=10_000,
        quoted_price_kopeks=10_000,
    )
    db.add(intent)
    await db.flush()

    async def add_attempt(*, status: str, holds_slot: bool):
        correlation = uuid.uuid4().hex
        provider_id = str(uuid.uuid4())
        payment = PlategaPayment(
            user_id=user.id,
            correlation_id=correlation,
            amount_kopeks=10_000,
            currency='RUB',
            payment_method_code=2,
            status='CANCELED' if status == 'terminal' else 'PENDING',
            platega_transaction_id=provider_id,
            payload=f'platega:{correlation}',
            metadata_json={},
        )
        db.add(payment)
        await db.flush()
        attempt = DeviceAddonTopupAttempt(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            intent_id=intent.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash=uuid.uuid4().hex * 2,
            method_key='2',
            provider_method_code=2,
            expected_amount_kopeks=10_000,
            requested_amount_kopeks=10_000,
            status=status,
            holds_invoice_slot=holds_slot,
            platega_payment_id=payment.id,
            provider_payment_id=provider_id,
            correlation_id=correlation,
        )
        db.add(attempt)
        await db.flush()
        return payment, attempt

    old_payment, old_attempt = await add_attempt(status='terminal', holds_slot=False)
    _, current_attempt = await add_attempt(status='pending', holds_slot=True)
    await db.commit()
    return user, intent, old_payment, old_attempt, current_attempt


async def _active_intent_graph(db):
    user = User(
        telegram_id=7_783_000_000 + int(uuid.uuid4().hex[:5], 16),
        balance_kopeks=0,
        status='active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        remnawave_uuid=str(uuid.uuid4()),
    )
    db.add(user)
    await db.flush()
    tariff = Tariff(
        name='addon payment race',
        device_limit=1,
        max_device_limit=10,
        device_price_kopeks=5000,
    )
    db.add(tariff)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id,
        end_date=datetime.now(UTC) + timedelta(days=30),
        status='active',
        is_trial=False,
        device_limit=1,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(subscription)
    await db.flush()
    intent = DeviceAddonIntent(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        subscription_id=subscription.id,
        target_subscription_id=subscription.id,
        tariff_id=tariff.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash='a' * 64,
        devices_to_add=1,
        original_device_limit=1,
        panel_uuid=user.remnawave_uuid,
        end_date=subscription.end_date,
        device_addon_generation=0,
        days_left=30,
        monthly_price_kopeks=5000,
        base_price_kopeks=5000,
        quoted_price_kopeks=5000,
    )
    db.add(intent)
    await db.commit()
    return user, subscription, intent


async def test_terminal_old_invoice_can_settle_after_new_invoice_without_reclaiming_slot(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    async with sessions() as db:
        user, _, _, old_attempt, current_attempt = await _late_payment_graph(db)
        payload = {
            'id': old_attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=old_attempt.id, payload=payload)
        await reconcile_device_addon_payment(db, attempt_id=old_attempt.id, payload=payload)
        await db.refresh(user)
        await db.refresh(old_attempt)
        await db.refresh(current_attempt)
        assert user.balance_kopeks == 10_000
        assert old_attempt.status == 'paid'
        assert old_attempt.holds_invoice_slot is False
        assert current_attempt.status == 'pending'
        assert current_attempt.holds_invoice_slot is True
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1


async def test_wrong_currency_on_old_invoice_holds_only_that_invoice(sessions):
    async with sessions() as db:
        user, _, old_payment, old_attempt, current_attempt = await _late_payment_graph(db)
        payload = {
            'id': old_attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'USD'},
        }
        await reconcile_device_addon_payment(db, attempt_id=old_attempt.id, payload=payload)
        await db.refresh(user)
        await db.refresh(old_payment)
        await db.refresh(old_attempt)
        await db.refresh(current_attempt)
        assert user.balance_kopeks == 0
        assert old_payment.status == 'OPERATOR_REVIEW'
        assert old_attempt.status == 'operator_review'
        assert old_attempt.holds_invoice_slot is False
        assert current_attempt.status == 'pending'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize(
    'mutation',
    [
        {'id': 'wrong-provider-id'},
        {'paymentDetails': {'amount': '99.99', 'currency': 'RUB'}},
        {'payload': 'platega:wrong-correlation'},
    ],
    ids=['provider-id', 'amount', 'correlation-payload'],
)
async def test_canonical_identity_amount_and_present_payload_must_all_match(sessions, mutation):
    async with sessions() as db:
        user, _, old_payment, old_attempt, current_attempt = await _late_payment_graph(db)
        payload = {
            'id': old_attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            'payload': f'platega:{old_attempt.correlation_id}',
        }
        payload.update(mutation)
        await reconcile_device_addon_payment(db, attempt_id=old_attempt.id, payload=payload)
        await db.refresh(user)
        await db.refresh(old_payment)
        await db.refresh(old_attempt)
        await db.refresh(current_attempt)
        assert user.balance_kopeks == 0
        assert old_payment.status == 'OPERATOR_REVIEW'
        assert old_attempt.status == 'operator_review'
        assert old_attempt.holds_invoice_slot is False
        assert current_attempt.status == 'pending'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_early_exact_webhook_during_lost_create_response_never_uses_generic_finalizer(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test')
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 10_000)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 10_000_000)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(
        payments,
        'available_platega_methods_for_db',
        AsyncMock(return_value=[{'provider_code': 2}]),
    )
    provider_id = str(uuid.uuid4())
    provider_posts = 0

    class EarlyWebhookThenLostResponse(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_payment(self, **kwargs):
            nonlocal provider_posts
            provider_posts += 1
            assert self._max_retries == 1
            async with sessions() as callback_db:
                handled = await PlategaPaymentMixin().process_platega_webhook(
                    callback_db,
                    {
                        'id': provider_id,
                        'status': 'CONFIRMED',
                        'paymentMethod': 'SBPQR',
                        'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
                        'payload': kwargs['payload'],
                    },
                )
                assert handled is True
            # The invoice was created remotely, but the POST response was lost.

    monkeypatch.setattr(payments, 'PlategaService', EarlyWebhookThenLostResponse)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='b' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        await db.refresh(user)
        await db.refresh(attempt)
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        assert provider_posts == 1
        assert attempt.status == 'reconciling'
        assert attempt.provider_payment_id == provider_id
        assert local_payment.is_paid is False
        assert local_payment.transaction_id is None
        assert user.balance_kopeks == 0
        assert await db.scalar(select(func.count(DeviceAddonTopupAttempt.id))) == 1
        assert await db.scalar(select(func.count(PlategaPayment.id))) == 1
        assert await db.scalar(select(func.count(Transaction.id))) == 0

        with pytest.raises(DeviceAddonError) as active_error:
            await create_device_addon_topup(
                db,
                intent_public_id=intent.public_id,
                user_id=user.id,
                idempotency_key=uuid.uuid4().hex,
                request_hash='c' * 64,
                method_key='2',
                expected_amount_kopeks=10_000,
                return_url=None,
                failed_url=None,
            )
        assert active_error.value.code == 'payment_attempt_active'
        assert provider_posts == 1

        canonical = {
            'id': provider_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await db.refresh(user)
        assert user.balance_kopeks == 10_000
        assert await db.scalar(select(func.count(Transaction.id))) == 1


async def test_post_paid_provider_regressions_preserve_receipt_and_effect_progress(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    async with sessions() as db:
        user, _, payment, attempt, _ = await _late_payment_graph(db)
        confirmed = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=confirmed)
        attempt.referral_status = 'done'
        attempt.event_status = 'done'
        await db.commit()
        deposit_id = attempt.deposit_transaction_id
        paid_at = attempt.paid_at

        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=None)
        pending = {**confirmed, 'status': 'PENDING'}
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=pending)
        wrong = {
            **confirmed,
            'paymentDetails': {'amount': '101.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=wrong)
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=confirmed)
        await db.refresh(user)
        await db.refresh(payment)
        await db.refresh(attempt)
        assert user.balance_kopeks == 10_000
        assert attempt.status == 'paid'
        assert attempt.holds_invoice_slot is False
        assert attempt.deposit_transaction_id == deposit_id
        assert attempt.paid_at == paid_at
        assert attempt.referral_status == 'done'
        assert attempt.event_status == 'done'
        assert payment.is_paid is True
        assert payment.transaction_id == deposit_id
        assert await db.scalar(select(func.count(Transaction.id))) == 1


async def test_paid_effect_recovery_respects_next_reconcile_backoff(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    run_effects = AsyncMock()
    monkeypatch.setattr(payments, '_run_paid_effects', run_effects)
    async with sessions() as db:
        _, _, _, attempt, current_attempt = await _late_payment_graph(db)
        confirmed = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=confirmed)
        future = datetime.now(UTC) + timedelta(minutes=5)
        attempt.event_status = 'pending'
        attempt.next_reconcile_at = future
        current_attempt.next_reconcile_at = future
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0
        run_effects.assert_not_awaited()


async def test_redacted_erasure_binding_still_fences_late_confirmed_money(sessions):
    async with sessions() as db:
        user, _, payment, attempt, _ = await _late_payment_graph(db)
        now = datetime.now(UTC)
        user.account_erasure_requested_at = now
        payment.payload = None
        payment.metadata_json = {}
        request = AccountErasureRequest(
            user_id=user.id,
            requested_by_user_id=user.id,
            state='ready_for_anonymization',
            financial_resolution_at=now,
            financial_resolved_by_user_id=user.id,
            financial_resolution_code='owner_approved',
            financial_resolution_note='resolved before late provider observation',
        )
        db.add(request)
        await db.commit()
        canonical = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await db.refresh(user)
        await db.refresh(payment)
        await db.refresh(attempt)
        await db.refresh(request)
        assert user.balance_kopeks == 0
        assert payment.status == 'OPERATOR_REVIEW'
        assert attempt.status == 'operator_review'
        assert attempt.reconciliation_reason == 'paid_after_account_lifecycle_change'
        assert request.state == 'awaiting_manual_resolution'
        assert request.resolution_code == 'late_device_first_payment_callback'
        assert request.financial_resolution_at is None
        assert request.financial_resolved_by_user_id is None
        assert request.financial_resolution_code is None
        assert request.last_late_payment_blocked_at is not None
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_redacted_payload_does_not_relax_internal_correlation_binding(sessions):
    async with sessions() as db:
        user, _, payment, attempt, _ = await _late_payment_graph(db)
        payment.payload = None
        payment.correlation_id = uuid.uuid4().hex
        await db.commit()
        canonical = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await db.refresh(user)
        await db.refresh(payment)
        await db.refresh(attempt)
        assert user.balance_kopeks == 0
        assert payment.status == 'OPERATOR_REVIEW'
        assert attempt.status == 'operator_review'
        assert attempt.reconciliation_reason == 'durable_payment_binding_mismatch'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_referral_money_helper_replay_cannot_turn_first_payment_into_recurring_commission(sessions, monkeypatch):
    from app.services import device_first_deposit_outbox_service as referral_money

    monkeypatch.setattr(referral_money, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_money, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_money, 'calculate_referral_commission_percent', AsyncMock(return_value=10))
    monkeypatch.setattr(referral_money, '_is_commission_limit_reached', AsyncMock(return_value=False))
    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 1)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 100)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 200)

    async with sessions() as db:
        referrer = User(
            telegram_id=7_782_000_001,
            balance_kopeks=0,
            status='active',
            language='ru',
            referral_code=uuid.uuid4().hex[:12],
        )
        db.add(referrer)
        await db.flush()
        buyer = User(
            telegram_id=7_782_000_002,
            balance_kopeks=10_000,
            status='active',
            language='ru',
            referral_code=uuid.uuid4().hex[:12],
            referred_by_id=referrer.id,
            has_made_first_topup=False,
        )
        db.add(buyer)
        await db.flush()
        source = Transaction(
            user_id=buyer.id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=10_000,
            payment_method=PaymentMethod.PLATEGA.value,
            external_id=str(uuid.uuid4()),
            device_first_ledger_key=f'device-addon-deposit:{uuid.uuid4()}',
            is_completed=True,
            completed_at=datetime.now(UTC),
        )
        db.add(source)
        await db.commit()

        first = await apply_deposit_referral_money(db, source_transaction_id=source.id)
        await db.commit()
        second = await apply_deposit_referral_money(db, source_transaction_id=source.id)
        await db.commit()
        await db.refresh(buyer)
        await db.refresh(referrer)
        rewards = list(
            (
                await db.execute(
                    select(Transaction).where(
                        Transaction.device_first_ledger_key.like(f'deposit-side-effect:{source.id}:%')
                    )
                )
            )
            .scalars()
            .all()
        )
        assert first == second == [buyer.id, referrer.id]
        assert len(rewards) == 2
        assert buyer.balance_kopeks == 10_100
        assert referrer.balance_kopeks == 1_200

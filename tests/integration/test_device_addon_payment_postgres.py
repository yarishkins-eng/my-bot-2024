"""Real PostgreSQL regressions for add-on Platega settlement."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.cabinet.routes import admin_payments
from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    AdminAuditLog,
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
from app.services.device_addon_service import DeviceAddonError, serialize_intent
from app.services.device_first_deposit_outbox_service import apply_deposit_referral_money
from app.services.payment.platega import PlategaPaymentMixin
from app.services.payment_search_service import (
    PeriodPreset,
    SearchParams,
    StatusFilter,
    _classify_status,
    search_payments,
)
from app.services.payment_verification_service import (
    attach_device_addon_payment_metadata,
    get_payment_record,
    list_recent_pending_payments,
)
from app.services.platega_service import PlategaCreateRejected, PlategaService
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


def _configure_addon_topup(monkeypatch):
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


async def _closing_referred_paid_graph(db, *, referrer_erased: bool = False):
    now = datetime.now(UTC)
    referrer = User(
        telegram_id=7_784_000_000 + int(uuid.uuid4().hex[:5], 16),
        balance_kopeks=0,
        status='deleted' if referrer_erased else 'active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        account_erasure_requested_at=now if referrer_erased else None,
        account_erased_at=now if referrer_erased else None,
    )
    db.add(referrer)
    await db.flush()
    buyer = User(
        telegram_id=7_785_000_000 + int(uuid.uuid4().hex[:5], 16),
        balance_kopeks=0,
        status='deleted',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        referred_by_id=referrer.id,
        has_made_first_topup=False,
        account_erasure_requested_at=now,
    )
    db.add(buyer)
    await db.flush()
    subscription = Subscription(
        user_id=buyer.id,
        end_date=now + timedelta(days=30),
        status='disabled',
        is_trial=False,
        device_limit=2,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(subscription)
    await db.flush()
    intent = DeviceAddonIntent(
        public_id=str(uuid.uuid4()),
        user_id=buyer.id,
        subscription_id=subscription.id,
        target_subscription_id=subscription.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash='d' * 64,
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
    source = Transaction(
        user_id=buyer.id,
        type=TransactionType.DEPOSIT.value,
        amount_kopeks=10_000,
        payment_method=PaymentMethod.PLATEGA.value,
        external_id=str(uuid.uuid4()),
        device_first_ledger_key=f'device-addon-deposit:{uuid.uuid4()}',
        is_completed=True,
        completed_at=now,
    )
    db.add(source)
    await db.flush()
    correlation = uuid.uuid4().hex
    payment = PlategaPayment(
        user_id=buyer.id,
        correlation_id=correlation,
        amount_kopeks=10_000,
        currency='RUB',
        payment_method_code=2,
        status='CONFIRMED',
        is_paid=True,
        transaction_id=source.id,
        platega_transaction_id=source.external_id,
        payload=f'platega:{correlation}',
    )
    db.add(payment)
    await db.flush()
    attempt = DeviceAddonTopupAttempt(
        public_id=str(uuid.uuid4()),
        user_id=buyer.id,
        intent_id=intent.id,
        idempotency_key=uuid.uuid4().hex,
        request_hash='e' * 64,
        method_key='2',
        provider_method_code=2,
        expected_amount_kopeks=10_000,
        requested_amount_kopeks=10_000,
        credited_amount_kopeks=10_000,
        status='paid',
        holds_invoice_slot=False,
        platega_payment_id=payment.id,
        provider_payment_id=source.external_id,
        correlation_id=correlation,
        deposit_transaction_id=source.id,
        referral_status='pending',
        event_status='pending',
        next_reconcile_at=now,
        paid_at=now,
    )
    db.add(attempt)
    db.add(
        AccountErasureRequest(
            user_id=buyer.id,
            requested_by_user_id=buyer.id,
            state='awaiting_reconciliation',
        )
    )
    if referrer_erased:
        db.add(
            AccountErasureRequest(
                user_id=referrer.id,
                requested_by_user_id=referrer.id,
                state='completed',
                finalized_at=now,
            )
        )
    await db.commit()
    return buyer, referrer, source, payment, attempt


def _configure_referral_money(monkeypatch):
    from app.services import device_first_deposit_outbox_service as referral_money

    monkeypatch.setattr(referral_money, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_money, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_money, 'calculate_referral_commission_percent', AsyncMock(return_value=10))
    monkeypatch.setattr(referral_money, '_is_commission_limit_reached', AsyncMock(return_value=False))
    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 1)
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 100)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 200)


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


async def test_paid_addon_topup_creates_one_deposit_and_one_customer_receipt(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    bot = MagicMock()
    bot.send_message = AsyncMock()

    async with sessions() as db:
        user, intent, _, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)

        assert await payments.recover_device_addon_payments(db, limit=1, bot=bot) == 1
        await db.refresh(user)
        await db.refresh(attempt)
        assert user.balance_kopeks == 10_000
        assert attempt.event_status == 'done'
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.args[0] == user.telegram_id
        assert 'Пополнение успешно!' in bot.send_message.await_args.args[1]
        button = bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        assert button.web_app.url == f'https://cabinet.example/subscription/device-topup/{intent.public_id}'

        assert await payments.recover_device_addon_payments(db, limit=1, bot=bot) == 0
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1
        bot.send_message.assert_awaited_once()


async def test_customer_receipt_failure_keeps_the_single_credit_retryable(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    failing_bot = MagicMock()
    failing_bot.send_message = AsyncMock(side_effect=RuntimeError('telegram unavailable'))

    async with sessions() as db:
        user, _, _, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)

        assert await payments.recover_device_addon_payments(db, limit=1, bot=failing_bot) == 0
        await db.refresh(user)
        await db.refresh(attempt)
        assert user.balance_kopeks == 10_000
        assert attempt.event_status == 'processing'
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1

        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        retry_bot = MagicMock()
        retry_bot.send_message = AsyncMock()
        assert await payments.recover_device_addon_payments(db, limit=1, bot=retry_bot) == 1
        await db.refresh(attempt)
        assert attempt.event_status == 'done'
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1
        retry_bot.send_message.assert_awaited_once()


async def test_manual_canonical_get_failure_logs_payment_attempt_and_exception_type(sessions, monkeypatch):
    class FailingProvider:
        def __init__(self):
            self._max_retries = 3

        async def get_transaction(self, transaction_id):
            assert transaction_id
            raise TimeoutError('provider timed out')

    monkeypatch.setattr(payments, 'PlategaService', FailingProvider)
    async with sessions() as db:
        _, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        await db.commit()

        with capture_logs() as logs:
            await payments.check_device_addon_payment_now(db, platega_payment_id=int(payment.id))

        failure = next(entry for entry in logs if entry.get('event') == 'device_addon_manual_canonical_get_failed')
        assert failure['platega_payment_id'] == payment.id
        assert failure['attempt_id'] == attempt.id
        assert failure['error_type'] == 'TimeoutError'


@pytest.mark.parametrize('error_type', [TelegramForbiddenError, TelegramBadRequest])
async def test_customer_receipt_terminal_failure_finishes_without_retry(sessions, monkeypatch, error_type):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=error_type(method=MagicMock(), message='recipient unavailable'))

    async with sessions() as db:
        user, _, _, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)

        assert await payments.recover_device_addon_payments(db, limit=1, bot=bot) == 1
        await db.refresh(attempt)
        assert attempt.event_status == 'done'
        assert attempt.reconciliation_reason == 'customer_receipt_undeliverable'
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 1

        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1, bot=bot) == 0
        bot.send_message.assert_awaited_once()


async def test_paid_effect_attempt_ceiling_finishes_without_another_delivery(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', False)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    async with sessions() as db:
        _, _, _, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': attempt.provider_payment_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
        }
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)
        attempt.referral_status = 'done'
        attempt.event_status = 'pending'
        attempt.effects_attempts = payments._MAX_PAID_EFFECT_ATTEMPTS
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        assert await payments.recover_device_addon_payments(db, limit=1, bot=bot) == 1
        await db.refresh(attempt)
        assert attempt.event_status == 'done'
        assert attempt.reconciliation_reason == 'paid_effects_attempts_exhausted'
        bot.send_message.assert_not_awaited()


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
        assert old_payment.status == 'CANCELED'
        assert old_attempt.status == 'terminal'
        assert old_attempt.holds_invoice_slot is False
        assert old_attempt.reconciliation_reason == 'terminal_recheck_canonical_invoice_mismatch'
        assert current_attempt.status == 'pending'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_first_terminal_observation_with_amount_mismatch_releases_invoice_slot(sessions):
    async with sessions() as db:
        _, _, _, old_attempt, attempt = await _late_payment_graph(db)
        payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        old_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': attempt.provider_payment_id,
            'status': 'CANCELED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '108.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
        }

        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)
        await db.refresh(payment)
        await db.refresh(attempt)

        assert payment.status == 'CANCELED'
        assert attempt.status == 'terminal'
        assert attempt.holds_invoice_slot is False
        assert attempt.reconciliation_reason == 'provider_terminal:canceled:canonical_invoice_mismatch'
        assert (
            await db.scalar(
                select(func.count(DeviceAddonTopupAttempt.id)).where(
                    DeviceAddonTopupAttempt.holds_invoice_slot.is_(True)
                )
            )
            == 0
        )


async def test_first_terminal_observation_with_wrong_provider_id_keeps_invoice_for_review(sessions):
    async with sessions() as db:
        _, _, _, old_attempt, attempt = await _late_payment_graph(db)
        payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        old_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        payload = {
            'id': str(uuid.uuid4()),
            'status': 'CANCELED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '108.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
        }

        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=payload)
        await db.refresh(payment)
        await db.refresh(attempt)

        assert payment.status == 'OPERATOR_REVIEW'
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        assert attempt.reconciliation_reason == 'canonical_invoice_mismatch'


@pytest.mark.parametrize(
    ('mutation', 'expected_attempt_status', 'expected_payment_status'),
    [
        ({'id': 'wrong-provider-id'}, 'operator_review', 'OPERATOR_REVIEW'),
        ({'paymentDetails': {'amount': '99.99', 'currency': 'RUB'}}, 'terminal', 'CANCELED'),
        ({'payload': 'platega:wrong-correlation'}, 'operator_review', 'OPERATOR_REVIEW'),
    ],
    ids=['provider-id', 'amount', 'correlation-payload'],
)
async def test_canonical_identity_amount_and_present_payload_must_all_match(
    sessions,
    mutation,
    expected_attempt_status,
    expected_payment_status,
):
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
        assert old_payment.status == expected_payment_status
        assert old_attempt.status == expected_attempt_status
        assert old_attempt.holds_invoice_slot is False
        assert current_attempt.status == 'pending'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize('payment_provider_id', [None, 'conflicting-provider-id'], ids=['missing', 'conflicting'])
async def test_automatic_recovery_requires_matching_provider_identity_mirrors(
    sessions,
    monkeypatch,
    payment_provider_id,
):
    async with sessions() as db:
        user, _, _, old_attempt, attempt = await _late_payment_graph(db)
        payment = await db.get(PlategaPayment, attempt.platega_payment_id)
        attempt_provider_id = str(attempt.provider_payment_id)
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        payment.status = 'RECONCILING'
        payment.platega_transaction_id = payment_provider_id
        old_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        await db.commit()

        class ConfirmedProvider(PlategaService):
            def __init__(self):
                self._max_retries = 1

            async def get_transaction(self, transaction_id):
                assert transaction_id == attempt_provider_id
                return {
                    'id': attempt_provider_id,
                    'status': 'CONFIRMED',
                    'paymentMethod': 'SBPQR',
                    'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
                    'payload': f'platega:{attempt.correlation_id}',
                }

        monkeypatch.setattr(payments, 'PlategaService', ConfirmedProvider)
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(user)
        await db.refresh(payment)
        await db.refresh(attempt)
        assert user.balance_kopeks == 0
        assert payment.status == 'OPERATOR_REVIEW'
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        assert attempt.reconciliation_reason == 'durable_provider_identity_mismatch'
        assert attempt.reconcile_attempts == 1
        assert await db.scalar(select(func.count(Transaction.id))) == 0

        attempt.reconcile_attempts = 23
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        assert attempt.reconcile_attempts == 24

        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0


async def test_unproven_crypto_method_is_rejected_before_any_financial_write(sessions, monkeypatch):
    globally_available = AsyncMock(return_value=[{'key': 'crypto', 'provider_code': 13}])
    provider_factory = MagicMock()
    monkeypatch.setattr(payments, 'available_platega_methods_for_db', globally_available)
    monkeypatch.setattr(payments, 'PlategaService', provider_factory)

    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        with pytest.raises(DeviceAddonError) as error:
            await create_device_addon_topup(
                db,
                intent_public_id=intent.public_id,
                user_id=user.id,
                idempotency_key=uuid.uuid4().hex,
                request_hash='f' * 64,
                method_key='13',
                expected_amount_kopeks=5_000,
                return_url=None,
                failed_url=None,
            )

        assert error.value.code == 'payment_method_unavailable'
        globally_available.assert_awaited_once()
        provider_factory.assert_not_called()
        assert await db.scalar(select(func.count(DeviceAddonTopupAttempt.id))) == 0
        assert await db.scalar(select(func.count(PlategaPayment.id))) == 0
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

        async def create_device_addon_payment(self, **kwargs):
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


async def test_early_mismatched_webhook_cannot_hide_provider_identity_from_create_response(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)
    provider_id = str(uuid.uuid4())

    class EarlyMismatchThenSuccessfulResponse(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            async with sessions() as callback_db:
                handled = await PlategaPaymentMixin().process_platega_webhook(
                    callback_db,
                    {
                        'id': provider_id,
                        'status': 'PENDING',
                        'paymentMethod': 'SBPQR',
                        'paymentDetails': {'amount': '99.99', 'currency': 'RUB'},
                        'payload': kwargs['payload'],
                    },
                )
                assert handled is True
            return {'id': provider_id, 'redirect': 'https://pay.example.test/invoice'}

    monkeypatch.setattr(payments, 'PlategaService', EarlyMismatchThenSuccessfulResponse)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='d' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)

        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        assert attempt.reconciliation_reason == 'callback_correlation_or_invoice_mismatch'
        assert attempt.provider_payment_id == provider_id
        assert attempt.payment_url == 'https://pay.example.test/invoice'
        assert payment.platega_transaction_id == provider_id
        assert payment.status == 'OPERATOR_REVIEW'
        with pytest.raises(DeviceAddonError) as error:
            await payments.close_device_addon_attempt_without_credit(
                db,
                platega_payment_id=int(attempt.platega_payment_id),
            )
        assert error.value.code == 'attempt_cannot_be_closed'


async def test_trusted_create_rejection_frees_slot_and_allows_a_new_invoice(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)

    class RejectedProvider:
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            del kwargs
            raise PlategaCreateRejected(422)

    monkeypatch.setattr(payments, 'PlategaService', RejectedProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        first = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='e' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        second = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='f' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        await db.refresh(first)
        await db.refresh(second)
        assert first.status == second.status == 'terminal'
        assert first.holds_invoice_slot is second.holds_invoice_slot is False
        assert first.reconciliation_reason == second.reconciliation_reason == 'provider_create_rejected:422'
        assert await db.scalar(select(func.count(DeviceAddonTopupAttempt.id))) == 2
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_uninformative_create_response_keeps_the_only_invoice_slot(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)

    class UninformativeProvider:
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            # PlategaService maps an empty/non-error JSON body to this ambiguous
            # result: the HTTP response does not prove that no invoice exists.
            del kwargs

    monkeypatch.setattr(payments, 'PlategaService', UninformativeProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='0' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        assert attempt.status == 'creation_unknown'
        assert attempt.holds_invoice_slot is True
        assert attempt.reconciliation_reason == 'provider_create_missing_identity'
        with pytest.raises(DeviceAddonError) as error:
            await create_device_addon_topup(
                db,
                intent_public_id=intent.public_id,
                user_id=user.id,
                idempotency_key=uuid.uuid4().hex,
                request_hash='9' * 64,
                method_key='2',
                expected_amount_kopeks=10_000,
                return_url=None,
                failed_url=None,
            )
        assert error.value.code == 'payment_attempt_active'


async def test_exact_mismatched_callback_binds_provider_identity_before_review(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)

    class UninformativeProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            del kwargs

    monkeypatch.setattr(payments, 'PlategaService', UninformativeProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='d' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        provider_id = str(uuid.uuid4())
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)

        assert await payments.handle_device_addon_platega_callback(
            db,
            payment=local_payment,
            payload={
                'id': provider_id,
                'payload': f'platega:{attempt.correlation_id}',
                'status': 'PENDING',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '99.99', 'currency': 'RUB'},
            },
        )

        await db.refresh(attempt)
        await db.refresh(local_payment)
        assert attempt.status == 'operator_review'
        assert attempt.provider_payment_id == provider_id
        assert local_payment.platega_transaction_id == provider_id
        record = await get_payment_record(db, PaymentMethod.PLATEGA, int(local_payment.id))
        assert record is not None
        await attach_device_addon_payment_metadata(db, [record])
        assert record.device_addon_can_check is True
        assert record.device_addon_can_close is False
        with pytest.raises(DeviceAddonError) as error:
            await payments.close_device_addon_attempt_without_credit(db, platega_payment_id=int(local_payment.id))
        assert error.value.code == 'attempt_cannot_be_closed'
        assert user.balance_kopeks == 0
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_sparse_exact_callback_binds_identity_then_credits_only_from_canonical_get(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)

    class UninformativeProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            del kwargs

    monkeypatch.setattr(payments, 'PlategaService', UninformativeProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='e' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        provider_id = str(uuid.uuid4())
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
        assert await payments.handle_device_addon_platega_callback(
            db,
            payment=local_payment,
            payload={
                'id': provider_id,
                'payload': f'platega:{attempt.correlation_id}',
                'status': 'PENDING',
            },
        )
        await db.refresh(attempt)
        await db.refresh(local_payment)
        assert attempt.status == 'reconciling'
        assert attempt.provider_payment_id == provider_id
        assert local_payment.platega_transaction_id == provider_id
        assert user.balance_kopeks == 0

        canonical = {
            'id': provider_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
        }

        class CanonicalProvider(PlategaService):
            def __init__(self):
                self._max_retries = 1

            async def get_transaction(self, transaction_id):
                assert transaction_id == provider_id
                return canonical

        monkeypatch.setattr(payments, 'PlategaService', CanonicalProvider)
        await payments.check_device_addon_payment_now(db, platega_payment_id=int(local_payment.id))
        await payments.check_device_addon_payment_now(db, platega_payment_id=int(local_payment.id))
        await db.refresh(user)
        await db.refresh(attempt)
        assert user.balance_kopeks == 10_000
        assert attempt.status == 'paid'
        assert await db.scalar(select(func.count(Transaction.id))) == 1


async def test_unknown_create_requires_logged_review_then_audited_close_without_credit(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)

    class UnknownProvider:
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            del kwargs
            raise TimeoutError('response lost after POST')

    monkeypatch.setattr(payments, 'PlategaService', UnknownProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='1' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        assert attempt.status == 'creation_unknown'
        assert attempt.holds_invoice_slot is True
        assert attempt.provider_payment_id is None

        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        with capture_logs() as logs:
            assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        review_log = next(
            entry for entry in logs if str(entry.get('event', '')).startswith('device_addon_payment_operator_review ')
        )
        assert f'user_id={user.id}' in review_log['event']
        assert 'reason=provider_identity_unknown_no_retry' in review_log['event']
        assert 'amount_kopeks=10000' in review_log['event']
        assert review_log['user_id'] == user.id
        assert review_log['reason'] == 'provider_identity_unknown_no_retry'
        assert review_log['intent_public_id'] == intent.public_id
        assert review_log['attempt_public_id'] == attempt.public_id
        assert review_log['amount_kopeks'] == 10_000

        with pytest.raises(DeviceAddonError, match='уже проверяется'):
            await create_device_addon_topup(
                db,
                intent_public_id=intent.public_id,
                user_id=user.id,
                idempotency_key=uuid.uuid4().hex,
                request_hash='2' * 64,
                method_key='2',
                expected_amount_kopeks=10_000,
                return_url=None,
                failed_url=None,
            )

        record = await get_payment_record(db, PaymentMethod.PLATEGA, int(attempt.platega_payment_id))
        assert record is not None
        await attach_device_addon_payment_metadata(db, [record])
        assert record.is_device_addon is True
        assert record.device_addon_can_check is False
        assert record.device_addon_can_close is True
        assert record.device_addon_reason_text

        with monkeypatch.context() as audit_patch:
            audit_patch.setattr(
                admin_payments.PermissionService,
                'log_action',
                AsyncMock(side_effect=RuntimeError('audit unavailable')),
            )
            with pytest.raises(RuntimeError, match='audit unavailable'):
                await admin_payments.close_device_addon_attempt(
                    PaymentMethod.PLATEGA.value,
                    int(attempt.platega_payment_id),
                    admin=user,
                    db=db,
                )
        await db.rollback()
        await db.refresh(attempt)
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        assert local_payment.status == 'OPERATOR_REVIEW'

        response = await admin_payments.close_device_addon_attempt(
            PaymentMethod.PLATEGA.value,
            int(attempt.platega_payment_id),
            admin=user,
            db=db,
        )
        await db.refresh(user)
        await db.refresh(attempt)
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
        audit = await db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.action == 'device_addon.payment_attempt_closed',
                AdminAuditLog.resource_id == str(attempt.platega_payment_id),
            )
        )
        assert response.success is True
        assert response.payment is not None and response.payment.is_device_addon is True
        assert attempt.status == 'terminal'
        assert attempt.holds_invoice_slot is False
        assert attempt.reconciliation_reason == 'closed_by_operator'
        assert local_payment.status == 'CLOSED_BY_OPERATOR'
        assert audit is not None and audit.details == {'resolution': 'closed_by_operator', 'credited': False}
        assert user.balance_kopeks == 0
        assert await db.scalar(select(func.count(Transaction.id))) == 0

        replacement = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='3' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        assert replacement.id != attempt.id
        assert replacement.status == 'creation_unknown'


async def test_operator_close_rejects_attempt_with_provider_identity(sessions):
    async with sessions() as db:
        _, _, _, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=1)
        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = False
        attempt.provider_payment_id = None
        await db.commit()
        record = await get_payment_record(db, PaymentMethod.PLATEGA, int(attempt.platega_payment_id))
        assert record is not None
        await attach_device_addon_payment_metadata(db, [record])
        assert record.device_addon_can_check is False
        assert record.device_addon_can_close is False
        with pytest.raises(DeviceAddonError) as error:
            await payments.close_device_addon_attempt_without_credit(
                db,
                platega_payment_id=int(attempt.platega_payment_id),
            )
        assert error.value.code == 'attempt_cannot_be_closed'


async def test_late_confirmed_callback_after_operator_close_still_credits_exactly_once(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)

    class UnknownProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            del kwargs
            raise TimeoutError('response lost after POST')

    monkeypatch.setattr(payments, 'PlategaService', UnknownProvider)
    async with sessions() as db:
        user, _, intent = await _active_intent_graph(db)
        attempt = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='4' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await payments.close_device_addon_attempt_without_credit(
            db,
            platega_payment_id=int(attempt.platega_payment_id),
        )
        provider_id = str(uuid.uuid4())
        canonical = {
            'id': provider_id,
            'status': 'CONFIRMED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
        }
        local_payment = await db.get(PlategaPayment, attempt.platega_payment_id, populate_existing=True)
        assert await payments.handle_device_addon_platega_callback(
            db,
            payment=local_payment,
            payload=canonical,
        )

        class CanonicalProvider(PlategaService):
            def __init__(self):
                self._max_retries = 3

            async def get_transaction(self, transaction_id):
                assert transaction_id == provider_id
                return canonical

        monkeypatch.setattr(payments, 'PlategaService', CanonicalProvider)
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await payments.check_device_addon_payment_now(
            db,
            platega_payment_id=int(attempt.platega_payment_id),
        )
        await db.refresh(user)
        await db.refresh(intent)
        await db.refresh(attempt)
        assert user.balance_kopeks == 10_000
        assert attempt.status == 'paid'
        assert attempt.holds_invoice_slot is False
        assert intent.purchase_state == 'draft'
        assert await db.scalar(select(func.count(Transaction.id))) == 1


async def test_terminal_and_operator_review_rechecks_are_bounded_and_delayed(sessions, monkeypatch):
    class MissingCanonicalProvider:
        def __init__(self):
            self._max_retries = 3

        async def get_transaction(self, transaction_id):
            assert transaction_id

    monkeypatch.setattr(payments, 'PlategaService', MissingCanonicalProvider)
    async with sessions() as db:
        _, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        attempt.reconcile_attempts = 3
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        before_terminal = datetime.now(UTC)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        assert attempt.status == 'terminal'
        assert attempt.reconcile_attempts == 4
        assert before_terminal + timedelta(hours=5, minutes=59) < attempt.next_reconcile_at
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0

        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = False
        attempt.reconcile_attempts = 23
        attempt.created_at = datetime.now(UTC) - timedelta(days=30)
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        payment.status = 'OPERATOR_REVIEW'
        before_review = datetime.now(UTC)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        assert attempt.status == 'operator_review'
        assert attempt.reconcile_attempts == 24
        assert before_review + timedelta(minutes=59) < attempt.next_reconcile_at
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0


async def test_terminal_live_regression_stays_bounded_and_allows_replacement(sessions, monkeypatch):
    _configure_addon_topup(monkeypatch)
    canonical_calls = 0
    replacement_provider_id = None
    replacement_payload = None

    class PendingProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def get_transaction(self, transaction_id):
            nonlocal canonical_calls
            canonical_calls += 1
            if transaction_id == provider_id:
                return canonical
            assert transaction_id == replacement_provider_id
            return {
                'id': replacement_provider_id,
                'status': 'PENDING',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
                'payload': replacement_payload,
                'redirect': 'https://pay.example.test/replacement',
            }

        async def create_device_addon_payment(self, **kwargs):
            nonlocal replacement_provider_id, replacement_payload
            replacement_provider_id = str(uuid.uuid4())
            replacement_payload = kwargs['payload']
            return {
                'id': replacement_provider_id,
                'redirect': 'https://pay.example.test/replacement',
            }

    monkeypatch.setattr(payments, 'PlategaService', PendingProvider)
    async with sessions() as db:
        user, intent, payment, attempt, current_attempt = await _late_payment_graph(db)
        subscription = await db.get(Subscription, intent.subscription_id)
        tariff = Tariff(
            name='terminal regression replacement',
            device_limit=2,
            max_device_limit=10,
            device_price_kopeks=10_000,
        )
        db.add(tariff)
        await db.flush()
        subscription.tariff_id = tariff.id
        current_attempt.status = 'terminal'
        current_attempt.holds_invoice_slot = False
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        provider_id = str(attempt.provider_payment_id)
        canonical = {
            'id': provider_id,
            'status': 'PENDING',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            'payload': f'platega:{attempt.correlation_id}',
            'redirect': 'https://pay.example.test/old-terminal',
        }
        await db.commit()

        before_first = datetime.now(UTC)
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await db.refresh(attempt)
        await db.refresh(payment)
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is False
        assert attempt.reconcile_attempts == 1
        assert attempt.reconciliation_reason == 'provider_terminal_status_regressed'
        assert attempt.next_reconcile_at >= before_first + timedelta(minutes=59)
        assert payment.status == 'OPERATOR_REVIEW'

        before_second = datetime.now(UTC)
        await reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=canonical)
        await db.refresh(attempt)
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is False
        assert attempt.reconcile_attempts == 2
        assert attempt.next_reconcile_at >= before_second + timedelta(minutes=59)

        attempt.reconcile_attempts = 23
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is False
        assert attempt.reconcile_attempts == 24
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0
        assert canonical_calls == 1

        serialized = await serialize_intent(db, intent=intent, user=user, include_quote=True)
        old_attempt = next(item for item in serialized['topup_attempts'] if item['id'] == attempt.public_id)
        assert old_attempt['action_required'] is True
        assert old_attempt['can_open_payment'] is False
        assert old_attempt['can_create_new_attempt'] is True

        replacement = await create_device_addon_topup(
            db,
            intent_public_id=intent.public_id,
            user_id=user.id,
            idempotency_key=uuid.uuid4().hex,
            request_hash='9' * 64,
            method_key='2',
            expected_amount_kopeks=10_000,
            return_url=None,
            failed_url=None,
        )
        assert replacement.status == 'pending'
        assert replacement.holds_invoice_slot is True


async def test_live_poll_without_safe_redirect_preserves_operator_review_retry_budget(sessions, monkeypatch):
    class LiveWithoutRedirectProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def get_transaction(self, transaction_id):
            return {
                'id': transaction_id,
                'status': 'PENDING',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            }

    monkeypatch.setattr(payments, 'PlategaService', LiveWithoutRedirectProvider)
    async with sessions() as db:
        _, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = False
        attempt.reconcile_attempts = 23
        attempt.payment_url = None
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        payment.status = 'OPERATOR_REVIEW'
        payment.redirect_url = None
        await db.commit()

        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(attempt)
        await db.refresh(payment)
        assert attempt.status == 'operator_review'
        assert attempt.reconcile_attempts == 24
        assert attempt.reconciliation_reason == 'provider_terminal_status_regressed'
        assert payment.status == 'OPERATOR_REVIEW'

        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0


async def test_old_operator_review_remains_visible_after_automatic_polling_stops(sessions):
    async with sessions() as db:
        _, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        attempt.status = 'operator_review'
        attempt.reconcile_attempts = 24
        attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        attempt.created_at = datetime.now(UTC) - timedelta(days=30)
        payment.status = 'OPERATOR_REVIEW'
        payment.created_at = datetime.now(UTC) - timedelta(days=30)
        await db.commit()

        recent = await list_recent_pending_payments(db)
        page, total = await search_payments(
            db,
            SearchParams(
                period=PeriodPreset.H24,
                method_filter=PaymentMethod.PLATEGA,
                per_page=100,
            ),
        )

        assert int(payment.id) in {record.local_id for record in recent}
        assert int(payment.id) in {record.local_id for record in page}
        assert total >= 1


async def test_admin_manual_check_credits_confirmed_addon_exactly_once(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)

    class CanonicalProvider(PlategaService):
        def __init__(self):
            self._max_retries = 1

        async def get_transaction(self, transaction_id):
            return {
                'id': transaction_id,
                'status': 'CONFIRMED',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            }

    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(payments, 'PlategaService', CanonicalProvider)
    monkeypatch.setattr(admin_payments, 'create_bot', lambda: bot)

    async with sessions() as db:
        user, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        current_attempt.status = 'terminal'
        current_attempt.holds_invoice_slot = False
        await db.commit()
        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = True
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()

        first = await admin_payments.check_payment_status(
            PaymentMethod.PLATEGA.value,
            int(payment.id),
            admin=user,
            db=db,
        )
        second = await admin_payments.check_payment_status(
            PaymentMethod.PLATEGA.value,
            int(payment.id),
            admin=user,
            db=db,
        )
        await db.refresh(user)
        await db.refresh(attempt)

        assert first.success is True and first.payment is not None
        assert second.success is True and second.payment is not None
        assert user.balance_kopeks == 10_000
        assert attempt.status == 'paid'
        assert attempt.deposit_transaction_id is not None
        assert await db.scalar(select(func.count(Transaction.id))) == 1
        assert (
            await db.scalar(
                select(func.count(AdminAuditLog.id)).where(
                    AdminAuditLog.action == 'device_addon.payment_checked',
                    AdminAuditLog.resource_id == str(payment.id),
                )
            )
            == 2
        )
        assert bot.session.close.await_count == 2


async def test_admin_manual_check_does_not_credit_when_audit_insert_fails(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(admin_payments, 'create_bot', lambda: bot)
    monkeypatch.setattr(
        admin_payments.PermissionService,
        'log_action',
        AsyncMock(side_effect=RuntimeError('audit unavailable')),
    )

    async with sessions() as db:
        user, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        current_attempt.status = 'terminal'
        current_attempt.holds_invoice_slot = False
        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = True
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()

        with pytest.raises(RuntimeError, match='audit unavailable'):
            await admin_payments.check_payment_status(
                PaymentMethod.PLATEGA.value,
                int(payment.id),
                admin=user,
                db=db,
            )
        await db.rollback()
        await db.refresh(user)
        await db.refresh(attempt)
        await db.refresh(payment)

        assert user.balance_kopeks == 0
        assert attempt.status == 'operator_review'
        assert attempt.holds_invoice_slot is True
        assert attempt.deposit_transaction_id is None
        assert payment.status == 'OPERATOR_REVIEW'
        assert await db.scalar(select(func.count(Transaction.id))) == 0
        assert bot.session.close.await_count == 0


async def test_admin_manual_check_keeps_amount_mismatch_for_operator(sessions, monkeypatch):
    class CanonicalProvider(PlategaService):
        def __init__(self):
            self._max_retries = 1

        async def get_transaction(self, transaction_id):
            return {
                'id': transaction_id,
                'status': 'CONFIRMED',
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '101.00', 'currency': 'RUB'},
            }

    bot = MagicMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(payments, 'PlategaService', CanonicalProvider)
    monkeypatch.setattr(admin_payments, 'create_bot', lambda: bot)

    async with sessions() as db:
        user, _, payment, attempt, current_attempt = await _late_payment_graph(db)
        current_attempt.next_reconcile_at = datetime.now(UTC) + timedelta(days=2)
        current_attempt.status = 'terminal'
        current_attempt.holds_invoice_slot = False
        await db.commit()
        attempt.status = 'operator_review'
        attempt.holds_invoice_slot = True
        payment.status = 'OPERATOR_REVIEW'
        await db.commit()

        response = await admin_payments.check_payment_status(
            PaymentMethod.PLATEGA.value,
            int(payment.id),
            admin=user,
            db=db,
        )
        await db.refresh(user)
        await db.refresh(attempt)

        assert response.success is True and response.payment is not None
        assert response.payment.device_addon_reason_text == (
            'Сумма у провайдера не совпала — проверьте настройку комиссии Platega.'
        )
        assert user.balance_kopeks == 0
        assert attempt.status == 'operator_review'
        assert attempt.reconciliation_reason == 'canonical_invoice_mismatch'
        assert await db.scalar(select(func.count(Transaction.id))) == 0


@pytest.mark.parametrize('provider_status', ['CLOSED_BY_OPERATOR', 'REJECTED_400', 'REJECTED_422'])
async def test_addon_terminal_payment_statuses_are_cancelled_not_pending(sessions, provider_status):
    async with sessions() as db:
        _, _, payment, _, _ = await _late_payment_graph(db)
        payment.status = provider_status
        await db.commit()
        record = await get_payment_record(db, PaymentMethod.PLATEGA, int(payment.id))
        assert record is not None
        assert _classify_status(record) == StatusFilter.CANCELLED
        assert admin_payments._get_status_info(record)[0] == '❌'


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


async def test_closing_owner_referral_is_credited_once_then_event_is_skipped(sessions, monkeypatch):
    _configure_referral_money(monkeypatch)
    async with sessions() as db:
        buyer, referrer, source, _, attempt = await _closing_referred_paid_graph(db)
        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(buyer)
        await db.refresh(referrer)
        await db.refresh(attempt)
        rewards = list(
            (
                await db.execute(
                    select(Transaction)
                    .where(Transaction.device_first_ledger_key.like(f'deposit-side-effect:{source.id}:%'))
                    .order_by(Transaction.id)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.user_id, row.amount_kopeks) for row in rewards] == [
            (buyer.id, 100),
            (referrer.id, 1_200),
        ]
        assert buyer.balance_kopeks == 100
        assert referrer.balance_kopeks == 1_200
        assert attempt.referral_status == 'done'
        assert attempt.event_status == 'done'

        # Replays find no work and cannot award either wallet twice.
        assert await payments.recover_device_addon_payments(db, limit=1) == 0
        await db.refresh(buyer)
        await db.refresh(referrer)
        assert buyer.balance_kopeks == 100
        assert referrer.balance_kopeks == 1_200
        assert (
            await db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.device_first_ledger_key.like(f'deposit-side-effect:{source.id}:%')
                )
            )
            == 2
        )

        # The evidence is single-use. A later ordinary increase with the same
        # delta is still suppressed by the production 0098/0106 fence.
        buyer.balance_kopeks += 100
        await db.commit()
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 100


async def test_trigger_consumes_pending_reward_evidence_before_second_same_delta_update(sessions, monkeypatch):
    _configure_referral_money(monkeypatch)
    async with sessions() as db:
        buyer, referrer, source, _, attempt = await _closing_referred_paid_graph(db)
        recipients = await apply_deposit_referral_money(db, source_transaction_id=source.id)
        await db.refresh(buyer)
        await db.refresh(referrer)
        await db.refresh(attempt)
        assert recipients == [buyer.id, referrer.id]
        assert attempt.referral_status == 'processing'
        assert buyer.balance_kopeks == 100

        buyer.balance_kopeks += 100
        await db.flush()
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 100
        await db.rollback()


async def test_erased_referrer_never_gets_false_reward_ledger(sessions, monkeypatch):
    _configure_referral_money(monkeypatch)
    async with sessions() as db:
        buyer, referrer, source, _, attempt = await _closing_referred_paid_graph(db, referrer_erased=True)
        source_id = source.id
        request = await db.scalar(select(AccountErasureRequest).where(AccountErasureRequest.user_id == buyer.id))
        request.state = 'ready_for_anonymization'
        request.financial_resolution_at = datetime.now(UTC)
        request.financial_resolved_by_user_id = buyer.id
        request.financial_resolution_code = 'balance_writeoff_approved'
        request.financial_resolution_note = 'Previously approved before deferred referral processing.'
        await db.commit()

        assert await payments.recover_device_addon_payments(db, limit=1) == 1
        await db.refresh(buyer)
        await db.refresh(referrer)
        await db.refresh(attempt)
        await db.refresh(request)
        assert buyer.balance_kopeks == 0
        assert referrer.balance_kopeks == 0
        assert attempt.referral_status == 'operator_review'
        assert attempt.reconciliation_reason == 'referral_reward_recipient_account_closed'
        assert request.state == 'awaiting_manual_resolution'
        assert request.resolution_code == 'late_device_first_payment_callback'
        assert request.last_late_payment_blocked_at is not None
        assert request.financial_resolution_at is None
        assert request.financial_resolved_by_user_id is None
        assert request.financial_resolution_code is None
        assert request.financial_resolution_note is None
        assert (
            await db.scalar(
                select(func.count(Transaction.id)).where(
                    Transaction.device_first_ledger_key.like(f'deposit-side-effect:{source_id}:%')
                )
            )
            == 0
        )
        # A held liability is not retried by a timer, even if its event is
        # pending. Explicit audited resolution must not restart it either.
        attempt.next_reconcile_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0
        attempt.referral_status = 'resolved_manually'
        await db.commit()
        assert await payments.recover_device_addon_payments(db, limit=1) == 0


async def test_referral_money_helper_replay_cannot_turn_first_payment_into_recurring_commission(sessions, monkeypatch):
    _configure_referral_money(monkeypatch)

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

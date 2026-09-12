"""Money-boundary regressions for manual device add-on purchase."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.cabinet.routes import admin_users
from app.config import settings
from app.database.models import (
    AdminAuditLog,
    DeviceAddonIntent,
    DeviceAddonTopupAttempt,
    PlategaPayment,
    Subscription,
    Tariff,
    Transaction,
    User,
)
from app.services import device_addon_worker as worker_module
from app.services.device_addon_service import (
    DeviceAddonError,
    calculate_device_addon,
    create_intent,
    purchase_intent,
    quote_for_calculation,
    serialize_intent,
)
from tests.integration.test_device_addon_lifecycle_postgres import sessions  # noqa: F401


pytestmark = pytest.mark.asyncio


async def _active_target(db, *, balance: int = 10_000, limit: int = 1, tariff: Tariff | None = None):
    user = User(
        telegram_id=7_790_000_000 + int(uuid.uuid4().int % 100_000_000),
        balance_kopeks=balance,
        status='active',
        language='ru',
        referral_code=uuid.uuid4().hex[:12],
        remnawave_uuid=str(uuid.uuid4()),
    )
    db.add(user)
    await db.flush()
    if tariff is not None:
        db.add(tariff)
        await db.flush()
    subscription = Subscription(
        user_id=user.id,
        tariff_id=tariff.id if tariff is not None else None,
        end_date=datetime.now(UTC) + timedelta(days=30),
        status='active',
        is_trial=False,
        device_limit=limit,
        remnawave_short_id=uuid.uuid4().hex[:16],
    )
    db.add(subscription)
    await db.commit()
    return user, subscription


async def _intent_for(db, user, subscription, *, devices: int, key: str):
    quote = quote_for_calculation(
        await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=devices),
        user_id=user.id,
    )
    intent = await create_intent(db, user=user, quote_token=quote['quote_token'], idempotency_key=key)
    return intent, quote


async def _add_topup_attempt(db, *, intent, user, status: str, holds_slot: bool):
    correlation = uuid.uuid4().hex
    payment = PlategaPayment(
        user_id=user.id,
        correlation_id=correlation,
        amount_kopeks=100,
        currency='RUB',
        payment_method_code=2,
        status='PENDING',
        payload=f'platega:{correlation}',
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
        expected_amount_kopeks=100,
        requested_amount_kopeks=100,
        status=status,
        holds_invoice_slot=holds_slot,
        platega_payment_id=payment.id,
        correlation_id=correlation,
    )
    db.add(attempt)
    await db.flush()
    return attempt


async def test_commit_failure_rolls_back_balance_ledger_and_limit(sessions):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='commit-failure')
        user_id, subscription_id, intent_id = user.id, subscription.id, intent.id
        original_commit = db.commit
        db.commit = AsyncMock(side_effect=RuntimeError('injected commit failure'))
        with pytest.raises(RuntimeError, match='injected commit failure'):
            await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        db.commit = original_commit
        await db.rollback()

    async with sessions() as verify:
        stored_user = await verify.get(User, user_id)
        stored_subscription = await verify.get(Subscription, subscription_id)
        stored_intent = await verify.get(DeviceAddonIntent, intent_id)
        assert stored_user.balance_kopeks == 10_000
        assert stored_subscription.device_limit == 1
        assert stored_intent.purchase_state == 'draft'
        assert await verify.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user_id)) == 0


async def test_old_quote_rejects_but_explicit_fresh_requote_can_purchase(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, old_quote = await _intent_for(db, user, subscription, devices=1, key='reprice')
        monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', settings.PRICE_PER_DEVICE + 1000)
        fresh_quote = quote_for_calculation(
            await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=1),
            user_id=user.id,
        )
        with pytest.raises(DeviceAddonError) as error:
            await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=old_quote['quote_token'])
        assert error.value.code == 'quote_changed'
        bought = await purchase_intent(
            db, user=user, public_id=intent.public_id, quote_token=fresh_quote['quote_token']
        )
        assert bought.purchase_state == 'purchased'
        assert bought.quoted_price_kopeks == fresh_quote['price_kopeks']


async def test_same_intent_key_with_different_selection_conflicts(sessions):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        await _intent_for(db, user, subscription, devices=1, key='same-key')
        different_quote = quote_for_calculation(
            await calculate_device_addon(db, user=user, subscription_id=subscription.id, devices_to_add=2),
            user_id=user.id,
        )
        with pytest.raises(DeviceAddonError) as error:
            await create_intent(db, user=user, quote_token=different_quote['quote_token'], idempotency_key='same-key')
        assert error.value.code == 'idempotency_conflict'


async def test_concurrent_different_intents_cannot_overrun_max_limit(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 2)
    async with sessions() as db:
        user, subscription = await _active_target(db, balance=20_000)
        first, first_quote = await _intent_for(db, user, subscription, devices=1, key='max-first')
        second, second_quote = await _intent_for(db, user, subscription, devices=1, key='max-second')
        user_id, subscription_id = user.id, subscription.id

    async def buy(intent, quote):
        async with sessions() as worker:
            worker_user = await worker.get(User, user_id)
            return await purchase_intent(
                worker, user=worker_user, public_id=intent.public_id, quote_token=quote['quote_token']
            )

    results = await asyncio.gather(buy(first, first_quote), buy(second, second_quote), return_exceptions=True)
    assert sum(isinstance(item, DeviceAddonIntent) for item in results) == 1
    assert any(
        isinstance(item, DeviceAddonError) and item.code in {'quote_changed', 'max_device_limit'} for item in results
    )
    async with sessions() as verify:
        assert (await verify.get(Subscription, subscription_id)).device_limit == 2
        assert await verify.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user_id)) == 1


async def test_free_allowance_changes_limit_without_a_wallet_ledger_row(sessions):
    async with sessions() as db:
        tariff = Tariff(name=f'free-device-{uuid.uuid4().hex}', device_limit=2)
        user, subscription = await _active_target(db, balance=0, limit=1, tariff=tariff)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='free-slot')
        assert quote['price_kopeks'] == 0
        bought = await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        assert bought.transaction_id is None
        assert subscription.device_limit == 2
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 0


async def test_hundred_percent_discount_changes_limit_without_a_wallet_ledger_row(sessions, monkeypatch):
    from app.cabinet.routes.subscription_modules import helpers

    monkeypatch.setattr(
        helpers,
        '_apply_addon_discount',
        lambda user, category, amount, period_days: {'discounted': 0, 'discount': amount, 'percent': 100},
    )
    async with sessions() as db:
        user, subscription = await _active_target(db, balance=0)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='discount-free')
        assert quote['price_kopeks'] == 0
        bought = await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        assert bought.transaction_id is None
        assert subscription.device_limit == 2
        assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id)) == 0


async def test_worker_marks_deleted_target_for_review_without_any_panel_call(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='deleted-worker-target')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = intent.id
        await db.delete(subscription)
        await db.commit()

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    await worker._fulfill_claim(*claim)
    assert calls == []

    async with sessions() as verify:
        stored = await verify.get(DeviceAddonIntent, intent_id)
        assert stored.subscription_id is None
        assert stored.fulfillment_state == 'needs_attention'
        assert stored.fulfillment_error_code == 'subscription_target_changed'
        owner = await verify.get(User, stored.user_id)
        replay = await purchase_intent(
            verify, user=owner, public_id=stored.public_id, quote_token='expired-or-lost-original-quote'
        )
        assert replay.receipt_json == stored.receipt_json
        assert await verify.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == owner.id)) == 1


class _FakePanelClient:
    def __init__(self, calls, result, returned_uuid=None):
        self._calls = calls
        self._result = result
        self._returned_uuid = returned_uuid

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def update_user(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is None:
            return None
        return type(
            'PanelUser',
            (),
            {'uuid': self._returned_uuid or kwargs['uuid'], 'hwid_device_limit': kwargs['hwid_device_limit']},
        )()


def _fake_remnawave_service(monkeypatch, *, calls, result, returned_uuid=None):
    from app.services import remnawave_service

    class FakeRemnaWaveService:
        def get_api_client(self):
            return _FakePanelClient(calls, result, returned_uuid)

    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', FakeRemnaWaveService)


async def test_worker_retries_panel_exception_then_restarts_with_narrow_hwid_patch(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='worker-restart')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id, panel_uuid = intent.id, user.remnawave_uuid

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=RuntimeError('panel unavailable'))
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    first_worker = worker_module.DeviceAddonWorker()
    first_claim = await first_worker._claim_one()
    assert first_claim is not None
    await first_worker._fulfill_claim(*first_claim)

    async with sessions() as db:
        failed = await db.get(DeviceAddonIntent, intent_id)
        assert failed.fulfillment_state == 'pending'
        assert failed.fulfillment_error_code == 'panel_patch_failed'
        assert failed.lease_token is None
        # A restarted worker may reclaim only after the durable retry time.
        failed.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    restarted_worker = worker_module.DeviceAddonWorker()
    second_claim = await restarted_worker._claim_one()
    assert second_claim is not None
    await restarted_worker._fulfill_claim(*second_claim)

    assert calls == [
        {'uuid': panel_uuid, 'hwid_device_limit': 2},
        {'uuid': panel_uuid, 'hwid_device_limit': 2},
    ]
    async with sessions() as db:
        fulfilled = await db.get(DeviceAddonIntent, intent_id)
        assert fulfilled.fulfillment_state == 'ready'
        assert fulfilled.lease_token is None


async def test_worker_does_not_patch_for_a_stale_lease(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='worker-stale-lease')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = intent.id

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    async with sessions() as db:
        replacement = await db.get(DeviceAddonIntent, intent_id)
        replacement.lease_token = 'replacement-lease'
        replacement.lease_epoch += 1
        await db.commit()

    await worker._fulfill_claim(*claim)
    assert calls == []
    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'pending'
        assert stored.lease_token == 'replacement-lease'


async def test_worker_rejects_panel_response_for_a_different_uuid(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='worker-wrong-uuid')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = intent.id

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True, returned_uuid=str(uuid.uuid4()))
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    await worker._fulfill_claim(*claim)

    assert len(calls) == 1
    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'pending'
        assert stored.fulfillment_error_code == 'panel_patch_failed'


async def test_worker_keeps_retrying_after_three_panel_failures(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='three-panel-failures')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = int(intent.id)

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=RuntimeError('panel unavailable'))
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    for index in range(3):
        claim = await worker._claim_one()
        assert claim is not None
        await worker._fulfill_claim(*claim)
        async with sessions() as db:
            stored = await db.get(DeviceAddonIntent, intent_id)
            assert stored.fulfillment_state == 'pending'
            assert stored.fulfillment_attempts == index + 1
            assert stored.fulfillment_error_code == 'panel_patch_failed'
            stored.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
    assert len(calls) == 3


@pytest.mark.parametrize(
    ('temporary_state', 'expected_reason'),
    [('limited', 'subscription_limited'), ('resetting', 'account_reset_busy')],
)
async def test_worker_reschedules_temporary_target_states(
    sessions,
    monkeypatch,
    temporary_state,
    expected_reason,
):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key=f'temporary-{temporary_state}')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        if temporary_state == 'limited':
            subscription.status = 'limited'
        else:
            user.test_reset_state = 'resetting'
        intent_id = int(intent.id)
        await db.commit()

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    if temporary_state == 'resetting':
        claim = await worker._claim_one()
        assert claim is None
    else:
        for index in range(3):
            claim = await worker._claim_one()
            assert claim is not None
            await worker._fulfill_claim(*claim)
            async with sessions() as db:
                stored = await db.get(DeviceAddonIntent, intent_id)
                assert stored.fulfillment_state == 'pending'
                assert stored.fulfillment_attempts == 0
                if index < 2:
                    stored.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
                    await db.commit()

    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'pending'
        if temporary_state == 'resetting':
            assert stored.fulfillment_attempts == 0
            assert stored.fulfillment_error_code is None
        else:
            assert stored.fulfillment_attempts == 0
            assert stored.fulfillment_error_code == expected_reason
        assert stored.lease_token is None
        if temporary_state != 'resetting':
            now = datetime.now(UTC)
            assert stored.next_attempt_at >= now + timedelta(minutes=2)
            assert stored.next_attempt_at <= now + timedelta(minutes=5)
    assert calls == []


@pytest.mark.parametrize(
    ('permanent_state', 'expected_reason'),
    [
        ('deleted_subscription', 'subscription_target_changed'),
        ('expired_subscription', 'subscription_expired'),
        ('generation_changed', 'device_addon_generation_changed'),
        ('anonymization', 'account_anonymization_started'),
        ('panel_identity_changed', 'panel_identity_changed'),
    ],
)
async def test_worker_reports_distinct_permanent_target_failures(
    sessions,
    monkeypatch,
    permanent_state,
    expected_reason,
):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key=f'permanent-{permanent_state}')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = int(intent.id)
        intent_public_id = str(intent.public_id)
        user_id = int(user.id)
        if permanent_state == 'deleted_subscription':
            await db.delete(subscription)
        elif permanent_state == 'expired_subscription':
            subscription.end_date = datetime.now(UTC) - timedelta(seconds=1)
        elif permanent_state == 'generation_changed':
            user.device_addon_generation = int(user.device_addon_generation or 0) + 1
        elif permanent_state == 'anonymization':
            user.account_erasure_requested_at = datetime.now(UTC)
        else:
            user.remnawave_uuid = str(uuid.uuid4())
        await db.commit()

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    with capture_logs() as logs:
        await worker._fulfill_claim(*claim)

    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'needs_attention'
        assert stored.fulfillment_error_code == expected_reason
    assert calls == []
    alert = next(entry for entry in logs if entry.get('event') == 'device_addon_fulfillment_needs_attention')
    assert alert['intent_public_id'] == intent_public_id
    assert alert['user_id'] == user_id
    assert alert['reason'] == expected_reason


async def test_exhausted_intent_preserves_reason_and_does_not_block_next_claim(sessions, monkeypatch):
    async with sessions() as db:
        first_user, first_subscription = await _active_target(db, balance=100_000)
        first, first_quote = await _intent_for(
            db,
            first_user,
            first_subscription,
            devices=1,
            key='exhausted-first',
        )
        await purchase_intent(
            db,
            user=first_user,
            public_id=first.public_id,
            quote_token=first_quote['quote_token'],
        )
        second_user, second_subscription = await _active_target(db, balance=100_000)
        second, second_quote = await _intent_for(
            db,
            second_user,
            second_subscription,
            devices=1,
            key='ready-second',
        )
        await purchase_intent(
            db,
            user=second_user,
            public_id=second.public_id,
            quote_token=second_quote['quote_token'],
        )
        first.fulfillment_attempts = worker_module._MAX_AUTOMATIC_ATTEMPTS
        first.fulfillment_error_code = 'panel_patch_failed'
        first.next_attempt_at = datetime.now(UTC) - timedelta(seconds=2)
        second.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        first_id, second_id = int(first.id), int(second.id)
        await db.commit()

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    with capture_logs() as logs:
        claim = await worker._claim_one()
    assert claim is not None and claim[0] == second_id
    await worker._fulfill_claim(*claim)

    async with sessions() as db:
        exhausted = await db.get(DeviceAddonIntent, first_id)
        fulfilled = await db.get(DeviceAddonIntent, second_id)
        assert exhausted.fulfillment_state == 'needs_attention'
        assert exhausted.fulfillment_error_code == 'panel_patch_failed|automatic_attempts_exhausted'
        assert fulfilled.fulfillment_state == 'ready'
    assert len(calls) == 1
    alert = next(entry for entry in logs if entry.get('event') == 'device_addon_fulfillment_needs_attention')
    assert alert['reason'] == 'panel_patch_failed|automatic_attempts_exhausted'


async def test_worker_reschedules_unexpected_error_outside_panel_patch(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='unexpected-db-error')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = int(intent.id)

    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    monkeypatch.setattr(worker, '_load_target', AsyncMock(side_effect=RuntimeError('db unavailable once')))
    await worker._fulfill_claim(*claim)

    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'pending'
        assert stored.fulfillment_error_code == 'fulfillment_unexpected:RuntimeError'
        assert stored.lease_token is None


async def test_worker_finalizes_after_successful_compensation(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='worker-compensation')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        intent_id = int(intent.id)
        subscription_id = int(subscription.id)
        panel_uuid = str(user.remnawave_uuid)

    calls: list[dict] = []

    class CompensatingPanelClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def update_user(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                async with sessions() as admin_db:
                    current_subscription = await admin_db.get(Subscription, subscription_id)
                    current_subscription.device_limit = 1
                    await admin_db.commit()
            return type(
                'PanelUser',
                (),
                {'uuid': kwargs['uuid'], 'hwid_device_limit': kwargs['hwid_device_limit']},
            )()

    class CompensatingRemnaWaveService:
        def get_api_client(self):
            return CompensatingPanelClient()

    from app.services import remnawave_service

    monkeypatch.setattr(remnawave_service, 'RemnaWaveService', CompensatingRemnaWaveService)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None
    await worker._fulfill_claim(*claim)

    assert calls == [
        {'uuid': panel_uuid, 'hwid_device_limit': 2},
        {'uuid': panel_uuid, 'hwid_device_limit': 1},
    ]
    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'ready'
        assert stored.fulfillment_error_code is None


async def test_admin_retry_requeues_only_owned_needs_attention_without_money_changes(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db)
        intent, quote = await _intent_for(db, user, subscription, devices=1, key='admin-retry')
        await purchase_intent(db, user=user, public_id=intent.public_id, quote_token=quote['quote_token'])
        await _add_topup_attempt(db, intent=intent, user=user, status='terminal', holds_slot=False)
        intent.fulfillment_state = 'needs_attention'
        intent.fulfillment_attempts = worker_module._MAX_AUTOMATIC_ATTEMPTS
        intent.fulfillment_error_code = 'panel_patch_failed|automatic_attempts_exhausted'
        intent.lease_token = 'stale-lease'
        intent.lease_expires_at = datetime.now(UTC) + timedelta(days=1)
        other_user, _ = await _active_target(db)
        user_id = int(user.id)
        other_user_id = int(other_user.id)
        intent_id = int(intent.id)
        public_id = str(intent.public_id)
        balance_before = int(user.balance_kopeks)
        limit_before = int(subscription.device_limit)
        transaction_count = await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id))
        await db.commit()

        history = await admin_users.get_user_device_addons(user_id, admin=user, db=db)
        assert history['items'][0]['public_id'] == public_id
        assert history['items'][0]['devices_to_add'] == 1
        assert history['items'][0]['fulfillment_state'] == 'needs_attention'
        assert history['items'][0]['attempts'][0]['status'] == 'terminal'

        with pytest.raises(Exception) as not_owned:
            await admin_users.retry_user_device_addon_fulfillment(
                other_user_id,
                public_id,
                admin=other_user,
                db=db,
            )
        assert getattr(not_owned.value, 'status_code', None) == 404

        wake = MagicMock()
        monkeypatch.setattr(worker_module.device_addon_worker, 'wake', wake)
        response = await admin_users.retry_user_device_addon_fulfillment(
            user_id,
            public_id,
            admin=user,
            db=db,
        )
        assert response['fulfillment_state'] == 'pending'
        wake.assert_called_once_with()

        await db.refresh(user)
        await db.refresh(subscription)
        await db.refresh(intent)
        assert user.balance_kopeks == balance_before
        assert subscription.device_limit == limit_before
        assert (
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user.id))
            == transaction_count
        )
        assert intent.fulfillment_attempts == 0
        assert intent.fulfillment_error_code is None
        assert intent.lease_token is None
        audit = await db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.action == 'retry_device_addon_fulfillment',
                AdminAuditLog.resource_id == public_id,
            )
        )
        assert audit is not None

    calls: list[dict] = []
    _fake_remnawave_service(monkeypatch, calls=calls, result=True)
    monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
    worker = worker_module.DeviceAddonWorker()
    claim = await worker._claim_one()
    assert claim is not None and claim[0] == intent_id
    await worker._fulfill_claim(*claim)
    async with sessions() as db:
        stored = await db.get(DeviceAddonIntent, intent_id)
        assert stored.fulfillment_state == 'ready'
        assert (
            await db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user_id))
            == transaction_count
        )


async def test_worker_window_lease_and_stop_contract():
    retry_window = sum(min(300, 2 ** min(epoch, 8)) for epoch in range(1, worker_module._MAX_AUTOMATIC_ATTEMPTS + 1))
    assert retry_window >= 90 * 60
    assert worker_module._PANEL_PATCH_TIMEOUT_SECONDS > settings.REMNAWAVE_API_TOTAL_TIMEOUT * 4
    assert worker_module._LEASE_SECONDS > worker_module._PANEL_PATCH_TIMEOUT_SECONDS * 2

    worker = worker_module.DeviceAddonWorker()
    task = asyncio.create_task(asyncio.Event().wait())
    worker._running = True
    worker._task = task
    await worker.stop()
    assert task.cancelled()
    assert worker._task is None


async def test_topup_action_flags_use_full_graph_and_live_guards(sessions, monkeypatch):
    async with sessions() as db:
        user, subscription = await _active_target(db, balance=0)
        intent, _ = await _intent_for(db, user, subscription, devices=1, key='topup-action-flags')
        terminal = await _add_topup_attempt(db, intent=intent, user=user, status='terminal', holds_slot=False)
        await _add_topup_attempt(db, intent=intent, user=user, status='pending', holds_slot=True)
        await db.commit()

        serialized = await serialize_intent(db, intent=intent, user=user)
        old = next(item for item in serialized['topup_attempts'] if item['id'] == terminal.public_id)
        assert old['can_create_new_attempt'] is False
        assert old['can_open_payment'] is False
        assert old['action_required'] is False

    async with sessions() as db:
        user, subscription = await _active_target(db, balance=0)
        intent, _ = await _intent_for(db, user, subscription, devices=1, key='topup-paid-shortage')
        paid = await _add_topup_attempt(db, intent=intent, user=user, status='paid', holds_slot=False)
        await db.commit()
        serialized = await serialize_intent(db, intent=intent, user=user)
        paid_payload = next(item for item in serialized['topup_attempts'] if item['id'] == paid.public_id)
        assert paid_payload['can_create_new_attempt'] is True

        await db.delete(subscription)
        await db.commit()
        unavailable = await serialize_intent(db, intent=intent, user=user)
        assert unavailable['subscription_id'] == intent.target_subscription_id
        assert unavailable['quote'] is None
        assert unavailable['topup_attempts'][0]['can_create_new_attempt'] is False

    async with sessions() as db:
        user, subscription = await _active_target(db, balance=0)
        intent, _ = await _intent_for(db, user, subscription, devices=1, key='topup-disabled-flag')
        terminal = await _add_topup_attempt(db, intent=intent, user=user, status='terminal', holds_slot=False)
        await db.commit()
        monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', False)
        serialized = await serialize_intent(db, intent=intent, user=user)
        terminal_payload = next(item for item in serialized['topup_attempts'] if item['id'] == terminal.public_id)
        assert terminal_payload['can_create_new_attempt'] is False

"""Safety contract for financial account erasure.

These tests intentionally exercise the state machine without a provider or a
real panel.  The optional PostgreSQL integration suite verifies FK behaviour;
here we pin the product decisions that prevent a late payment from becoming a
credit on a newly registered Telegram account.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import User
from app.services import account_erasure_service as service
from app.services.device_first_payment_service import _fence_account_erasure_payment
from app.services.monitoring_service import MonitoringService
from app.services.recurrent_payment_service import _find_subscriptions_needing_topup, _reload_subscription_with_user
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.services.subscription_service import SubscriptionService
from app.services.user_service import UserService


def _context(*, attempt_status: str = 'failed', reason: str | None = 'provider_terminal:canceled', paid=False):
    payment = SimpleNamespace(id=51, is_paid=paid, status='CANCELED')
    attempt = SimpleNamespace(
        id=41,
        platega_payment_id=51,
        status=attempt_status,
        reconciliation_reason=reason,
    )
    return SimpleNamespace(
        user=SimpleNamespace(balance_kopeks=0),
        subscriptions=[],
        attempts=[attempt],
        payments=[payment],
    )


def test_only_canonical_provider_terminal_state_is_safe_to_anonymize() -> None:
    state, reason = service._target_state(_context())
    assert (state, reason) == (service.ERASURE_READY, None)

    state, reason = service._target_state(_context(attempt_status='pending', reason=None))
    assert (state, reason) == (service.ERASURE_AWAITING_RECONCILIATION, 'provider_invoice_unresolved')


def test_paid_or_reviewed_money_never_allows_automatic_erasure() -> None:
    state, reason = service._target_state(_context(attempt_status='operator_review', reason='anything'))
    assert (state, reason) == (service.ERASURE_AWAITING_MANUAL, 'paid_or_review_payment')


def test_user_balance_debit_contract_remains_on_user_model() -> None:
    """Schema additions must not move money methods onto the tombstone model."""
    user = User(balance_kopeks=500)

    assert user.subtract_balance(300) is True
    assert user.balance_kopeks == 200
    assert user.subtract_balance(201) is False

    state, reason = service._target_state(_context(paid=True))
    assert (state, reason) == (service.ERASURE_AWAITING_MANUAL, 'paid_or_review_payment')

    context = _context()
    context.payments[0].status = 'CONFIRMED'
    state, reason = service._target_state(context)
    assert (state, reason) == (service.ERASURE_AWAITING_MANUAL, 'paid_or_review_payment')


@pytest.mark.asyncio
async def test_completion_scrubs_identity_but_keeps_the_financial_anchor(monkeypatch) -> None:
    user = SimpleNamespace(
        id=7,
        account_erased_at=None,
        account_erasure_requested_at=datetime.now(UTC),
        balance_kopeks=0,
        telegram_id=12345,
        auth_type='telegram',
        username='old',
        first_name='Old',
        last_name='Name',
        email='old@example.test',
        email_verified=True,
        email_verified_at=datetime.now(UTC),
        email_verification_source='cabinet',
        password_hash='hash',
        email_verification_token='verify',
        email_verification_expires=datetime.now(UTC),
        password_reset_token='reset',
        password_reset_expires=datetime.now(UTC),
        email_change_new='new@example.test',
        email_change_code='123456',
        email_change_expires=datetime.now(UTC),
        google_id='google',
        yandex_id='yandex',
        discord_id='discord',
        vk_id=1,
        referral_code='ref',
        referred_by_id=3,
        remnawave_uuid='panel-id',
        trojan_password='trojan',
        vless_uuid='vless',
        ss_password='ss',
        pending_campaign_slug='campaign',
        notification_settings={'x': 1},
        last_pinned_message_id=9,
        status='deleted',
        restriction_subscription=True,
        restriction_topup=True,
        restriction_reason='account_erasure_requested',
    )
    request = SimpleNamespace(
        id=10,
        state=service.ERASURE_READY,
        panel_state='deactivated',
        has_legacy_financial_history=False,
        resolution_code=None,
    )
    subscription = SimpleNamespace(
        id=0xFFFFF,
        status='expired',
        autopay_enabled=True,
        subscription_url='https://old.example/sub',
        subscription_crypto_link='crypto://old',
        remnawave_uuid='panel-subscription-id',
        remnawave_short_uuid='short-uuid',
        remnawave_short_id='abcdef',
        connected_squads=['eu'],
    )
    context = SimpleNamespace(
        user=user,
        request=request,
        attempts=[],
        checkouts=[],
        payments=[],
        subscriptions=[subscription],
    )
    lock_context = AsyncMock(side_effect=[context, context])
    monkeypatch.setattr(service, '_lock_context', lock_context)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await service._complete_ready_financial_account_erasure(db, user_id=7, deactivate_panel=True)

    assert result.completed is True
    assert request.state == service.ERASURE_COMPLETED
    assert user.telegram_id is None
    assert user.email is None
    assert user.google_id is None
    assert user.remnawave_uuid is None
    assert user.account_erased_at is not None
    assert subscription.remnawave_short_id == 'erased-fffff'
    assert subscription.remnawave_uuid is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_financial_closure_removes_panel_even_when_legacy_caller_passes_false(monkeypatch) -> None:
    user = SimpleNamespace(
        id=7,
        account_erased_at=None,
        account_erasure_requested_at=None,
        balance_kopeks=0,
        status='active',
        restriction_subscription=False,
        restriction_topup=False,
        restriction_reason=None,
    )
    context = SimpleNamespace(user=user, request=None, attempts=[], checkouts=[], payments=[], subscriptions=[])
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    def add_request(request):
        context.request = request

    db.add.side_effect = add_request
    monkeypatch.setattr(service, '_lock_context', AsyncMock(return_value=context))
    remove_panel = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_remove_panel_identity', remove_panel)

    result = await service.request_financial_account_erasure(
        db,
        user_id=7,
        requested_by_user_id=1,
        deactivate_panel=False,
        has_legacy_financial_history=True,
    )

    assert result.state == service.ERASURE_AWAITING_MANUAL
    remove_panel.assert_awaited_once_with(context)
    assert context.request.panel_state == 'deactivated'
    assert user.account_erasure_requested_at is not None


@pytest.mark.asyncio
async def test_initial_closure_disables_autopay_and_removes_saved_payment_methods(monkeypatch) -> None:
    user = SimpleNamespace(
        id=7,
        account_erased_at=None,
        account_erasure_requested_at=None,
        balance_kopeks=0,
        status='active',
        restriction_subscription=False,
        restriction_topup=False,
        restriction_reason=None,
    )
    subscription = SimpleNamespace(status='active', autopay_enabled=True)
    context = SimpleNamespace(user=user, request=None, attempts=[], checkouts=[], payments=[], subscriptions=[subscription])
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())

    def add_request(request):
        context.request = request

    db.add.side_effect = add_request
    monkeypatch.setattr(service, '_lock_context', AsyncMock(return_value=context))
    monkeypatch.setattr(service, '_remove_panel_identity', AsyncMock(return_value=True))

    await service.request_financial_account_erasure(
        db,
        user_id=7,
        requested_by_user_id=1,
        deactivate_panel=True,
        has_legacy_financial_history=True,
    )

    assert subscription.status == 'disabled'
    assert subscription.autopay_enabled is False
    # The first transaction contains an explicit DELETE from saved_payment_methods.
    assert any('saved_payment_methods' in str(call.args[0]) for call in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_repeated_legacy_delete_never_bypasses_manual_financial_resolution(monkeypatch) -> None:
    """A duplicate delete must not turn an unresolved old invoice into a safe one."""
    user = SimpleNamespace(
        id=7,
        account_erased_at=None,
        account_erasure_requested_at=datetime.now(UTC),
        balance_kopeks=0,
        status='deleted',
        restriction_subscription=True,
        restriction_topup=True,
        restriction_reason='account_erasure_requested',
    )
    request = SimpleNamespace(
        state=service.ERASURE_AWAITING_MANUAL,
        panel_state='deactivated',
        has_legacy_financial_history=True,
        financial_resolution_at=None,
        resolution_code='legacy_financial_history',
    )
    context = SimpleNamespace(user=user, request=request, attempts=[], checkouts=[], payments=[], subscriptions=[])
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())
    monkeypatch.setattr(service, '_lock_context', AsyncMock(return_value=context))
    complete = AsyncMock()
    monkeypatch.setattr(service, '_complete_ready_financial_account_erasure', complete)

    result = await service.request_financial_account_erasure(
        db,
        user_id=7,
        requested_by_user_id=1,
        deactivate_panel=True,
        has_legacy_financial_history=True,
    )

    assert result.state == service.ERASURE_AWAITING_MANUAL
    assert request.state == service.ERASURE_AWAITING_MANUAL
    assert request.resolution_code == 'legacy_financial_history'
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurrent_worker_query_excludes_financial_closure() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))))

    await _find_subscriptions_needing_topup(db)

    statement = db.execute.await_args.args[0]
    compiled = str(statement)
    assert 'JOIN users ON subscriptions.user_id = users.id' in compiled
    assert 'users.account_erasure_requested_at IS NULL' in compiled


@pytest.mark.asyncio
async def test_recurrent_worker_reload_rechecks_financial_closure_before_charge() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))

    await _reload_subscription_with_user(db, 17)

    compiled = str(db.execute.await_args.args[0])
    assert 'JOIN users ON subscriptions.user_id = users.id' in compiled
    assert 'users.account_erasure_requested_at IS NULL' in compiled


@pytest.mark.asyncio
async def test_monitoring_autopay_query_joins_user_before_excluding_closed_accounts() -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))))

    await MonitoringService()._process_autopayments(db)

    compiled = str(db.execute.await_args.args[0])
    assert 'JOIN users ON subscriptions.user_id = users.id' in compiled
    assert 'users.account_erasure_requested_at IS NULL' in compiled


@pytest.mark.asyncio
async def test_manual_resolution_debits_balance_disables_access_and_then_completes(monkeypatch) -> None:
    user = SimpleNamespace(id=7, account_erased_at=None, account_erasure_requested_at=datetime.now(UTC), balance_kopeks=500)
    subscription = SimpleNamespace(status='active', autopay_enabled=True)
    request = SimpleNamespace(
        state=service.ERASURE_AWAITING_MANUAL,
        has_legacy_financial_history=False,
        financial_resolution_at=None,
        financial_resolved_by_user_id=None,
        financial_resolution_code=None,
        financial_resolution_note=None,
        resolution_code='positive_balance',
    )
    context = SimpleNamespace(
        user=user,
        request=request,
        attempts=[],
        checkouts=[],
        payments=[],
        subscriptions=[subscription],
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), add=MagicMock())
    monkeypatch.setattr(service, '_lock_context', AsyncMock(return_value=context))
    complete = AsyncMock(
        return_value=service.AccountErasureResult(
            state=service.ERASURE_COMPLETED,
            message='closed',
            completed=True,
        )
    )
    monkeypatch.setattr(service, '_complete_ready_financial_account_erasure', complete)

    result = await service.resolve_financial_account_erasure(
        db,
        user_id=7,
        resolved_by_user_id=99,
        resolution_code='balance_writeoff_approved',
        resolution_note='Owner-approved reconciliation.',
    )

    assert result.completed is True
    assert user.balance_kopeks == 0
    assert subscription.status == 'disabled'
    assert subscription.autopay_enabled is False
    assert request.financial_resolved_by_user_id == 99
    assert request.financial_resolution_code == 'balance_writeoff_approved'
    assert db.add.call_args.args[0].amount_kopeks == 500
    complete.assert_awaited_once_with(db, user_id=7, deactivate_panel=True)


def test_late_callback_invalidates_operator_resolution() -> None:
    request = SimpleNamespace(
        state=service.ERASURE_READY,
        financial_resolution_at=datetime.now(UTC),
        financial_resolved_by_user_id=1,
        financial_resolution_code='provider_terminal_verified',
        financial_resolution_note='Provider record checked.',
    )

    service._invalidate_financial_resolution(request, 'late_legacy_payment_callback')

    assert request.state == service.ERASURE_AWAITING_MANUAL
    assert request.resolution_code == 'late_legacy_payment_callback'
    assert request.financial_resolution_at is None


@pytest.mark.asyncio
async def test_device_first_platega_is_not_misclassified_as_legacy_history() -> None:
    """One Platega row is shared by old wallet and Device-First flows.

    The exact D1 attempt must keep a terminally cancelled checkout eligible for
    automatic redaction instead of forcing a needless manual account hold.
    """
    # one D1 attempt, then every legacy provider check, then no legacy
    # Platega-only row and no non-D1 ledger transaction
    scalar_results = [1] + [None] * 24 + [None, None]
    db = SimpleNamespace(scalar=AsyncMock(side_effect=scalar_results))

    assert await UserService._get_financial_history_kind(db, 7) == (True, False)
    legacy_platega_query = db.scalar.await_args_list[-2].args[0]
    assert 'checkout_payment_attempts.id IS NULL' in str(legacy_platega_query)


@pytest.mark.asyncio
async def test_guest_purchase_is_legacy_financial_history_for_buyer_and_recipient() -> None:
    # D1 + 23 providers return no row, then a guest order is found; Platega
    # and ledger lookups still happen so the result stays an auditable manual
    # financial path.
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[None] * 24 + [1, None, None]))

    assert await UserService._get_financial_history_kind(db, 7) == (True, True)
    guest_query = db.scalar.await_args_list[24].args[0]
    compiled = str(guest_query)
    assert 'guest_purchases.buyer_user_id' in compiled
    assert 'guest_purchases.user_id' in compiled


@pytest.mark.asyncio
async def test_completion_redacts_guest_order_credentials_but_not_financial_rows(monkeypatch) -> None:
    user = SimpleNamespace(
        id=7,
        account_erased_at=None,
        account_erasure_requested_at=datetime.now(UTC),
        balance_kopeks=0,
        telegram_id=123,
        auth_type='telegram', username='old', first_name='Old', last_name='Name', email='old@example.test',
        email_verified=True, email_verified_at=None, email_verification_source=None,
        password_hash=None, email_verification_token=None, email_verification_expires=None,
        password_reset_token=None, password_reset_expires=None, email_change_new=None, email_change_code=None,
        email_change_expires=None, google_id=None, yandex_id=None, discord_id=None, vk_id=None,
        referral_code=None, referred_by_id=None, remnawave_uuid=None, trojan_password=None,
        vless_uuid=None, ss_password=None, pending_campaign_slug=None, notification_settings={},
        last_pinned_message_id=None, status='deleted', restriction_subscription=True,
        restriction_topup=True, restriction_reason='account_erasure_requested',
    )
    request = SimpleNamespace(id=10, state=service.ERASURE_READY, panel_state='deactivated', has_legacy_financial_history=False, resolution_code=None)
    context = SimpleNamespace(user=user, request=request, attempts=[], checkouts=[], payments=[], subscriptions=[])
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(service, '_lock_context', AsyncMock(side_effect=[context, context]))

    result = await service._complete_ready_financial_account_erasure(db, user_id=7, deactivate_panel=True)

    assert result.completed is True
    guest_redaction = next(call.args[0] for call in db.execute.await_args_list if 'guest_purchases' in str(call.args[0]))
    compiled = str(guest_redaction)
    for protected_field in ('contact_value', 'cabinet_password', 'auto_login_token', 'subscription_url', 'subscription_crypto_link'):
        assert protected_field in compiled
    assert 'amount_kopeks' not in compiled
    assert 'payment_id' not in compiled


@pytest.mark.asyncio
async def test_completion_redacts_payment_links_and_purges_nonfinancial_account_data(monkeypatch) -> None:
    """Final erasure retains money facts, not payment URLs or user-content."""
    user = SimpleNamespace(
        id=7, account_erased_at=None, account_erasure_requested_at=datetime.now(UTC), balance_kopeks=0,
        telegram_id=123, auth_type='telegram', username='old', first_name='Old', last_name='Name', email='old@test',
        email_verified=True, email_verified_at=None, email_verification_source=None,
        password_hash=None, email_verification_token=None, email_verification_expires=None,
        password_reset_token=None, password_reset_expires=None, email_change_new=None, email_change_code=None,
        email_change_expires=None, google_id=None, yandex_id=None, discord_id=None, vk_id=None,
        referral_code=None, referred_by_id=None, remnawave_uuid=None, trojan_password=None,
        vless_uuid=None, ss_password=None, pending_campaign_slug=None, notification_settings={},
        last_pinned_message_id=None, status='deleted', restriction_subscription=True,
        restriction_topup=True, restriction_reason='account_erasure_requested',
    )
    payment = SimpleNamespace(
        id=51, is_paid=False, status='CANCELED',
        redirect_url='https://provider/pay/secret', return_url='https://return/secret', failed_url='https://fail/secret',
        payload='tg=123', metadata_json={'email': 'old@test'}, callback_payload={'token': 'secret'}, description='Old',
    )
    attempt = SimpleNamespace(
        id=41, platega_payment_id=51, status='failed', reconciliation_reason='provider_terminal:canceled',
        redirect_url='https://provider/attempt/secret',
    )
    request = SimpleNamespace(
        id=10, state=service.ERASURE_READY, panel_state='deactivated', panel_cleanup_uuids=[],
        has_legacy_financial_history=False, resolution_code=None,
    )
    context = SimpleNamespace(
        user=user, request=request, attempts=[attempt], checkouts=[SimpleNamespace(id=9)],
        payments=[payment], subscriptions=[],
    )
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(service, '_lock_context', AsyncMock(side_effect=[context, context]))

    result = await service._complete_ready_financial_account_erasure(db, user_id=7, deactivate_panel=True)

    assert result.completed is True
    assert payment.redirect_url is payment.return_url is payment.failed_url is None
    assert payment.payload is None
    assert payment.metadata_json == payment.callback_payload == {}
    assert payment.description == 'account erased; financial evidence retained'
    assert attempt.redirect_url is None
    all_sql = '\n'.join(str(call.args[0]) for call in db.execute.await_args_list)
    for table in (
        'device_first_outbox', 'device_first_deposit_outbox', 'device_first_notification_outbox',
        'device_first_mutations', 'ticket_notifications', 'tickets', 'user_device_aliases',
    ):
        assert table in all_sql
    assert 'platega_payments' not in all_sql


@pytest.mark.asyncio
async def test_late_payment_after_closure_is_reviewed_never_credited() -> None:
    checkout = SimpleNamespace(id=9, public_id='checkout-9', lifecycle_state='cancelled', terminal_reason='provider_terminal:canceled')
    attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        credited_amount_kopeks=0,
        status='failed',
        reconciliation_reason='provider_terminal:canceled',
    )
    payment = SimpleNamespace(id=51, user_id=7, metadata_json={}, status='CANCELED', is_paid=False)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        add=MagicMock(),
        commit=AsyncMock(),
    )

    returned = await _fence_account_erasure_payment(
        db,
        checkout=checkout,
        attempt=attempt,
        payment=payment,
        provider_payment_id='provider-1',
        verified=(36_900, 'RUB'),
    )

    assert returned is payment
    assert attempt.status == 'operator_review'
    assert attempt.reconciliation_reason == 'account_erasure_requested'
    assert payment.status == 'OPERATOR_REVIEW'
    assert payment.is_paid is True
    assert checkout.lifecycle_state == 'operator_review'
    credit = db.add.call_args.args[0]
    assert credit.user_id == 7
    assert credit.amount_kopeks == 36_900
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_erasure_removes_panel_identity_with_webhook_fence(monkeypatch) -> None:
    context = SimpleNamespace(
        user=SimpleNamespace(id=7, telegram_id=12345, email=None, remnawave_uuid='primary-panel-id'),
        subscriptions=[SimpleNamespace(remnawave_uuid='subscription-panel-id')],
        request=SimpleNamespace(panel_cleanup_uuids=[]),
    )
    mark = MagicMock()
    api = SimpleNamespace(
        get_user_by_telegram_id=AsyncMock(return_value=[]),
        get_user_by_email=AsyncMock(return_value=[]),
        delete_user=AsyncMock(return_value=True),
    )

    @asynccontextmanager
    async def fake_api_client(_self):
        yield api

    monkeypatch.setattr(RemnaWaveWebhookService, 'mark_intentional_panel_deletion', mark)
    monkeypatch.setattr(SubscriptionService, 'get_api_client', fake_api_client)

    assert await service._remove_panel_identity(context) is True
    mark.assert_called_once_with(panel_uuids=['primary-panel-id', 'subscription-panel-id'], telegram_id=12345)
    assert api.delete_user.await_count == 2


@pytest.mark.asyncio
async def test_panel_cleanup_discovers_response_lost_identity_and_retries_failed_delete(monkeypatch) -> None:
    """A provider POST with a lost response is still found by stable Telegram ID."""
    context = SimpleNamespace(
        user=SimpleNamespace(id=7, telegram_id=12345, email=None, remnawave_uuid=None),
        subscriptions=[],
        request=SimpleNamespace(panel_cleanup_uuids=[]),
    )
    discovered = SimpleNamespace(uuid='unknown-created-during-timeout')
    api = SimpleNamespace(
        get_user_by_telegram_id=AsyncMock(return_value=[discovered]),
        get_user_by_email=AsyncMock(return_value=[]),
        delete_user=AsyncMock(return_value=False),
    )

    @asynccontextmanager
    async def fake_api_client(_self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', fake_api_client)
    monkeypatch.setattr(RemnaWaveWebhookService, 'mark_intentional_panel_deletion', MagicMock())

    assert await service._remove_panel_identity(context) is False
    assert context.request.panel_cleanup_uuids == ['unknown-created-during-timeout']


@pytest.mark.asyncio
async def test_guarded_panel_write_locks_user_before_provider_request() -> None:
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[SimpleNamespace(id=7, account_erasure_requested_at=None), None]),
    )
    api = SimpleNamespace()
    result = await SubscriptionService().run_guarded_panel_write(
        db,
        user_id=7,
        api=api,
        operation=AsyncMock(return_value=SimpleNamespace(uuid='panel-7')),
    )

    assert result.uuid == 'panel-7'
    preflight = db.scalar.await_args_list[0].args[0]
    assert 'FOR UPDATE' in str(preflight)

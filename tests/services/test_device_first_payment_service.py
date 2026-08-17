from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.services.device_first_checkout_service import (
    DeviceFirstError,
    expire_checkout_quote_if_needed,
    prepare_direct_external_checkout,
)
from app.services.device_first_payment_service import (
    _apply_direct_pending_provider_observation,
    _checkout_return_url,
    _create_direct_platega_attempt,
    _direct_checkout_return_url,
    _has_durable_direct_payment_binding,
    _provider_method_code,
    _provider_transaction_id,
    _queue_direct_callback_for_canonical_reconciliation,
    _release_direct_terminal_invoice,
    _safe_provider_redirect_url,
    _verified_amount,
    abandon_direct_checkout_for_new_calculation,
    available_platega_methods,
    create_platega_attempt,
    reconcile_device_first_payments,
    settle_device_first_platega_payment,
)
from app.services.payment.platega import PlategaPaymentMixin
from app.services.platega_service import PlategaService


def test_verified_amount_uses_actual_rub_provider_value():
    assert _verified_amount({'paymentDetails': {'amount': '123.45', 'currency': 'RUB'}}) == (12345, 'RUB')


def test_documented_canonical_sbpqr_method_is_an_exact_allowlisted_match():
    assert _provider_method_code({'paymentMethod': 'SBPQR'}) == 2
    assert _provider_method_code({'paymentMethod': 'definitely-not-a-provider-method'}) is None


def test_verified_amount_rejects_untrusted_currency_or_missing_amount():
    assert _verified_amount({'paymentDetails': {'amount': '10', 'currency': 'USD'}}) is None
    assert _verified_amount({'paymentDetails': {'currency': 'RUB'}}) is None
    assert _verified_amount({'paymentDetails': {'amount': '349.995', 'currency': 'RUB'}}) is None


@pytest.mark.parametrize('amount', ['249.001', '248.995'])
def test_verified_callback_amount_rejects_fractional_kopeks(amount):
    assert _verified_amount({'paymentDetails': {'amount': amount, 'currency': 'RUB'}}) is None


def test_semantic_method_mapping_excludes_unapproved_numeric_12(monkeypatch):
    monkeypatch.setattr('app.services.device_first_payment_service.settings.PLATEGA_ENABLED', True)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.PLATEGA_MERCHANT_ID', 'merchant')
    monkeypatch.setattr('app.services.device_first_payment_service.settings.PLATEGA_SECRET', 'secret')
    monkeypatch.setattr('app.services.device_first_payment_service.settings.PLATEGA_ACTIVE_METHODS', '2,11,12,13')
    methods = available_platega_methods()
    assert {item['key'] for item in methods} == {'sbp', 'cards_ru', 'crypto'}
    assert {item['provider_code'] for item in methods} == {2, 11, 13}


def test_checkout_return_url_keeps_only_configured_origin(monkeypatch):
    monkeypatch.setattr(
        'app.services.device_first_payment_service.settings.PLATEGA_RETURN_URL',
        'https://cabinet.example/balance/top-up/result?old=1',
    )
    assert _checkout_return_url('checkout-1') == ('https://cabinet.example/subscription/purchase?checkout=checkout-1')


def test_direct_return_url_never_uses_telegram_or_generic_topup_return(monkeypatch):
    monkeypatch.setattr(
        'app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example/anything'
    )
    assert (
        _direct_checkout_return_url('checkout-1') == 'https://cabinet.example/subscription/purchase?checkout=checkout-1'
    )
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://t.me/teplo_bot')
    assert _direct_checkout_return_url('checkout-1') is None


@pytest.mark.parametrize(
    ('response', 'expected'),
    [
        ({'paymentDetails': {'amount': '249.00', 'currency': 'RUB'}}, (24900, 'RUB')),
        ({'paymentDetails': {'amount': '249.001', 'currency': 'RUB'}}, None),
        ({'paymentDetails': {'amount': '249.00', 'currency': 'USD'}}, None),
    ],
)
def test_provider_create_response_requires_exact_integer_kopeks(response, expected):
    assert PlategaService.parse_amount_currency(response) == expected


@pytest.mark.parametrize(
    'response',
    [
        {
            'transactionId': 'v1-invoice',
            'redirect': 'https://pay.example/v1-invoice',
            # Documented v1 create responses expose this as a display string,
            # so it must never be used as the financial source of truth.
            'paymentDetails': '450 RUB',
        },
        {
            'id': 'v2-invoice',
            'url': 'https://pay.example/v2-invoice',
            # v2 creation does not include a canonical amount at all.
        },
    ],
)
def test_direct_create_shapes_keep_identity_and_redirect_but_require_canonical_get(response):
    assert _provider_transaction_id(response)
    assert _safe_provider_redirect_url(response)
    assert PlategaService.parse_amount_currency(response) is None


def _terminal_direct_rows(*, lifecycle_state: str = 'awaiting_funds'):
    payment = SimpleNamespace(
        id=51,
        user_id=7,
        is_paid=False,
        status='PENDING',
        expires_at=None,
        updated_at=None,
        platega_transaction_id='provider-1',
        amount_kopeks=35_000,
        currency='RUB',
        payment_method_code=2,
        metadata_json={'device_first_attempt_id': 41, 'settlement_mode': 'direct_purchase_v2'},
    )
    user = SimpleNamespace(id=7)
    attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        platega_payment_id=51,
        provider='platega',
        settlement_mode='direct_purchase_v2',
        currency='RUB',
        provider_payment_id='provider-1',
        provider_method_code=2,
        requested_amount_kopeks=35_000,
        method_key='sbp',
        status='pending',
        reconciliation_reason=None,
        terminal_observations=0,
    )
    checkout = SimpleNamespace(
        id=9,
        public_id='checkout-1',
        user_id=7,
        settlement_mode='direct_purchase_v2',
        lifecycle_state=lifecycle_state,
        quote_state='valid',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        terminal_reason=None,
        updated_at=None,
    )
    return payment, user, attempt, checkout


def _finalized_erased_direct_rows():
    payment, user, attempt, checkout = _terminal_direct_rows(lifecycle_state='cancelled')
    payment.metadata_json = {}
    payment.redirect_url = payment.return_url = payment.failed_url = payment.payload = None
    payment.callback_payload = {}
    payment.status = 'CANCELED'
    user.account_erasure_requested_at = datetime.now(UTC)
    user.account_erased_at = datetime.now(UTC)
    user.balance_kopeks = 0
    attempt.status = 'failed'
    attempt.reconciliation_reason = 'provider_terminal:canceled'
    attempt.redirect_url = None
    checkout.quote_state = 'expired'
    checkout.funding_state = 'invoice_terminal'
    checkout.terminal_reason = 'provider_terminal:canceled'
    request = SimpleNamespace(state='completed')
    return payment, user, attempt, checkout, request


@pytest.mark.asyncio
async def test_exact_terminal_provider_result_archives_only_that_invoice_and_releases_next_quote():
    payment, user, attempt, checkout = _terminal_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(),
        commit=AsyncMock(),
    )

    released = await _release_direct_terminal_invoice(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={
            'id': 'provider-1',
            'status': 'CANCELED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
        provider_status='CANCELED',
        source='canonical_get',
    )

    assert released is True
    assert payment.status == 'CANCELED'
    assert attempt.status == 'failed'
    assert attempt.reconciliation_reason == 'provider_terminal:canceled'
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.quote_state == 'expired'
    assert checkout.terminal_reason == 'provider_terminal:canceled'
    event = db.add.call_args.args[0]
    assert event.provider_payment_id == 'provider-1'
    assert event.amount_kopeks == 35_000
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sparse_signed_terminal_callback_is_journaled_then_requires_canonical_get():
    payment, user, attempt, checkout = _terminal_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(),
        commit=AsyncMock(),
    )

    released = await _release_direct_terminal_invoice(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={'id': 'provider-1', 'status': 'EXPIRED'},
        provider_status='EXPIRED',
        source='callback',
    )

    assert released is False
    assert payment.status == 'VERIFYING'
    assert attempt.status == 'reconciliation'
    assert attempt.reconciliation_reason == 'provider_terminal_callback_awaiting_canonical'
    assert checkout.lifecycle_state == 'awaiting_funds'
    event = db.add.call_args.args[0]
    assert event.amount_kopeks is None
    assert event.currency is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalized_erasure_canonical_terminal_unpaid_is_a_redacted_noop(monkeypatch):
    payment, user, attempt, checkout, request = _finalized_erased_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout), Result(request)]
        ),
        scalar=AsyncMock(),
        add=MagicMock(),
        commit=AsyncMock(),
    )
    logger = MagicMock()
    monkeypatch.setattr('app.services.device_first_payment_service.logger', logger)

    released = await _release_direct_terminal_invoice(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={
            'id': 'provider-1',
            'status': 'CANCELED',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
        provider_status='CANCELED',
        source='poll',
    )

    assert released is False
    assert payment.metadata_json == payment.callback_payload == {}
    assert payment.redirect_url is payment.return_url is payment.failed_url is payment.payload is None
    assert payment.status == 'CANCELED'
    assert attempt.status == 'failed'
    assert attempt.reconciliation_reason == 'provider_terminal:canceled'
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == 'provider_terminal:canceled'
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_finalized_erasure_signed_terminal_callback_leaves_redacted_graph_untouched():
    payment, user, attempt, checkout, request = _finalized_erased_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout), Result(request)]
        ),
        scalar=AsyncMock(),
        add=MagicMock(),
        commit=AsyncMock(),
    )

    queued = await _queue_direct_callback_for_canonical_reconciliation(
        db,
        payment_id=payment.id,
        attempt_id=attempt.id,
        payload={'id': 'provider-1', 'status': 'EXPIRED'},
        reason='provider_terminal_callback_awaiting_canonical',
    )

    assert queued is True
    assert payment.metadata_json == payment.callback_payload == {}
    assert payment.status == 'CANCELED'
    assert attempt.status == 'failed'
    assert checkout.lifecycle_state == 'cancelled'
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_terminal_callback_routes_redacted_direct_payment_away_from_generic_platega_flow(monkeypatch):
    payment, _user, attempt, _checkout, _request = _finalized_erased_direct_rows()
    payment_service = SimpleNamespace(get_platega_payment_by_transaction_id=AsyncMock(return_value=payment))
    platega_crud = SimpleNamespace(get_platega_payment_by_id_for_update=AsyncMock(return_value=payment))
    queue = AsyncMock(return_value=True)

    def import_module(name):
        if name == 'app.services.payment_service':
            return payment_service
        if name == 'app.database.crud.platega':
            return platega_crud
        raise AssertionError(f'unexpected module: {name}')

    monkeypatch.setattr('app.services.payment.platega.import_module', import_module)
    monkeypatch.setattr(PlategaPaymentMixin, '_get_durable_direct_attempt', AsyncMock(return_value=attempt))
    monkeypatch.setattr(
        'app.services.device_first_payment_service._queue_direct_callback_for_canonical_reconciliation', queue
    )

    handled = await PlategaPaymentMixin().process_platega_webhook(
        SimpleNamespace(),
        {'id': 'provider-1', 'status': 'CANCELED'},
    )

    assert handled is True
    queue.assert_awaited_once_with(
        ANY,
        payment_id=payment.id,
        payload={'id': 'provider-1', 'status': 'CANCELED'},
        reason='provider_terminal_callback_awaiting_canonical',
        attempt_id=attempt.id,
    )


@pytest.mark.asyncio
async def test_finalized_erasure_late_confirmed_creates_only_manual_financial_hold(monkeypatch):
    payment, user, attempt, checkout, request = _finalized_erased_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
                Result(request),
                Result(checkout),
            ]
        ),
        scalar=AsyncMock(return_value=attempt.id),
        add=MagicMock(),
        commit=AsyncMock(),
    )
    append_event = AsyncMock()
    store_credit = AsyncMock()
    invalidate = AsyncMock()
    fulfill = AsyncMock()
    monkeypatch.setattr('app.services.device_first_payment_service._append_direct_provider_event', append_event)
    monkeypatch.setattr('app.services.device_first_payment_service._store_reconciliation_credit', store_credit)
    monkeypatch.setattr(
        'app.services.account_erasure_service.invalidate_financial_resolution_for_late_payment', invalidate
    )
    monkeypatch.setattr('app.services.device_first_payment_service.fulfill_direct_external_checkout', fulfill)

    settled = await settle_device_first_platega_payment(
        db,
        payment=payment,
        payload={
            'id': 'provider-1',
            'status': 'CONFIRMED',
            'paymentMethod': 2,
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
    )

    assert settled is payment
    assert payment.status == 'OPERATOR_REVIEW'
    assert payment.is_paid is True
    assert payment.metadata_json == payment.callback_payload == {}
    assert attempt.status == 'operator_review'
    assert attempt.reconciliation_reason == 'account_erased_late_confirmed'
    assert checkout.lifecycle_state == 'operator_review'
    assert checkout.terminal_reason == 'account_erased_late_confirmed'
    assert user.balance_kopeks == 0
    store_credit.assert_awaited_once()
    fulfill.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_active_missing_direct_marker_stays_fail_closed_without_a_balance_mutation():
    payment, user, attempt, checkout = _terminal_direct_rows()
    payment.metadata_json = {}
    user.balance_kopeks = 0
    user.account_erased_at = None
    user.account_erasure_requested_at = None
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        scalar=AsyncMock(return_value=attempt.id),
        commit=AsyncMock(),
    )

    settled = await settle_device_first_platega_payment(
        db,
        payment=payment,
        payload={
            'id': 'provider-1',
            'paymentMethod': 2,
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
    )

    assert settled is payment
    assert payment.status == 'OPERATOR_REVIEW'
    assert attempt.status == 'operator_review'
    assert attempt.reconciliation_reason == 'direct_payment_attempt_mode_or_binding_mismatch'
    assert checkout.lifecycle_state == 'operator_review'
    assert user.balance_kopeks == 0
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    ('attribute', 'value'),
    [
        ('checkout_user_id', 99),
        ('payment_transaction_id', 'other-provider-id'),
        ('payment_amount_kopeks', 34_999),
        ('payment_currency', 'USD'),
        ('payment_method_code', 13),
        ('checkout_settlement_mode', 'legacy_deposit'),
    ],
)
def test_durable_erased_binding_rejects_every_immutable_mismatch(attribute, value):
    payment, _user, attempt, checkout = _terminal_direct_rows()
    payment.metadata_json = {}
    setattr(
        {
            'checkout_user_id': checkout,
            'payment_transaction_id': payment,
            'payment_amount_kopeks': payment,
            'payment_currency': payment,
            'payment_method_code': payment,
            'checkout_settlement_mode': checkout,
        }[attribute],
        {
            'checkout_user_id': 'user_id',
            'payment_transaction_id': 'platega_transaction_id',
            'payment_amount_kopeks': 'amount_kopeks',
            'payment_currency': 'currency',
            'payment_method_code': 'payment_method_code',
            'checkout_settlement_mode': 'settlement_mode',
        }[attribute],
        value,
    )

    assert _has_durable_direct_payment_binding(payment, attempt, checkout) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('attribute', 'value'),
    [
        ('checkout_user_id', 99),
        ('payment_transaction_id', 'other-provider-id'),
        ('payment_amount_kopeks', 34_999),
        ('payment_currency', 'USD'),
        ('payment_method_code', 13),
        ('checkout_settlement_mode', 'legacy_deposit'),
    ],
)
async def test_metadata_marked_direct_payment_rejects_every_immutable_mismatch_without_fulfillment(attribute, value):
    """A live JSON route marker cannot bypass the retained financial graph."""
    payment, user, attempt, checkout = _terminal_direct_rows()
    user.balance_kopeks = 0
    setattr(
        {
            'checkout_user_id': checkout,
            'payment_transaction_id': payment,
            'payment_amount_kopeks': payment,
            'payment_currency': payment,
            'payment_method_code': payment,
            'checkout_settlement_mode': checkout,
        }[attribute],
        {
            'checkout_user_id': 'user_id',
            'payment_transaction_id': 'platega_transaction_id',
            'payment_amount_kopeks': 'amount_kopeks',
            'payment_currency': 'currency',
            'payment_method_code': 'payment_method_code',
            'checkout_settlement_mode': 'settlement_mode',
        }[attribute],
        value,
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    with patch(
        'app.services.device_first_payment_service.fulfill_direct_external_checkout',
        AsyncMock(),
    ) as fulfill:
        settled = await settle_device_first_platega_payment(
            db,
            payment=payment,
            payload={
                'id': 'provider-1',
                'paymentMethod': 2,
                'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
            },
        )

    assert settled is payment
    assert payment.status == 'OPERATOR_REVIEW'
    assert payment.is_paid is False
    assert attempt.status == 'operator_review'
    assert attempt.reconciliation_reason == 'direct_payment_attempt_mode_or_binding_mismatch'
    assert checkout.lifecycle_state == 'operator_review'
    assert checkout.fulfillment_state == 'not_started'
    assert user.balance_kopeks == 0
    fulfill.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_abandon_archives_only_a_fully_bound_canonical_pending_invoice():
    payment, user, attempt, checkout = _terminal_direct_rows()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=payment.id),
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    archived = await abandon_direct_checkout_for_new_calculation(
        db,
        checkout_public_id=checkout.public_id,
        user_id=user.id,
    )

    assert archived is checkout
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.quote_state == 'expired'
    assert checkout.funding_state == 'invoice_abandoned'
    assert checkout.terminal_reason == 'cancelled_by_user_after_invoice'
    assert attempt.status == 'reconciliation'
    assert attempt.reconciliation_reason == 'provider_invoice_abandoned_by_user'
    assert payment.status == 'VERIFYING'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_exact_confirmation_after_explicit_abandon_credits_wallet_once_without_old_fulfilment():
    """The customer-facing abandon promise: old money never activates old VPN."""
    payment, user, attempt, checkout = _terminal_direct_rows()
    user.balance_kopeks = 0
    payment.platega_transaction_id = 'provider-1'
    payment.transaction_id = None
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[payment.id, None]),
        execute=AsyncMock(
            side_effect=[
                # Explicit customer abandon: Payment -> User -> Attempt -> Checkout.
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
                # First exact late CONFIRMED: same lock order, then checkout
                # and the absent one-time ledger row.
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
                Result(None),
                # A duplicate callback stops at the credited idempotency fence.
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }

    abandoned = await abandon_direct_checkout_for_new_calculation(
        db,
        checkout_public_id=checkout.public_id,
        user_id=user.id,
    )
    assert abandoned is checkout
    assert checkout.terminal_reason == 'cancelled_by_user_after_invoice'

    with patch(
        'app.services.device_first_payment_service.fulfill_direct_external_checkout',
        AsyncMock(),
    ) as fulfill:
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.fulfillment_state == 'not_started'
    assert payment.is_paid is True
    assert attempt.status == 'credited'
    assert attempt.reconciliation_reason == 'late_paid_wallet_credit'
    assert user.balance_kopeks == 35_000
    ledger_rows = [
        call.args[0]
        for call in db.add.call_args_list
        if getattr(call.args[0], 'device_first_ledger_key', None) == 'direct_late_invoice:41'
    ]
    assert len(ledger_rows) == 1
    assert ledger_rows[0].amount_kopeks == 35_000
    fulfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_abandon_refuses_an_unbound_or_ambiguous_invoice_creation():
    payment, user, attempt, checkout = _terminal_direct_rows()
    payment.status = 'CREATING'
    attempt.status = 'creating'
    attempt.provider_payment_id = None
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=payment.id),
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    with pytest.raises(DeviceFirstError) as raised:
        await abandon_direct_checkout_for_new_calculation(
            db,
            checkout_public_id=checkout.public_id,
            user_id=user.id,
        )

    assert raised.value.code == 'reconciliation_required'
    assert checkout.lifecycle_state == 'awaiting_funds'
    assert attempt.status == 'creating'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_canonical_pending_cannot_demote_a_newer_paid_transition():
    payment, user, attempt, checkout = _terminal_direct_rows(lifecycle_state='ready')
    now = datetime.now(UTC)
    payment.is_paid = True
    payment.status = 'CONFIRMED'
    payment.updated_at = now
    payment.expires_at = now + timedelta(minutes=10)
    attempt.status = 'paid_processing'
    checkout.fulfillment_state = 'fulfilled'
    checkout.updated_at = now
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={
            'id': 'provider-1',
            'status': 'PENDING',
            'expiresIn': '00:15:00',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
        observed_after=now - timedelta(seconds=1),
    )

    assert applied is False
    assert payment.status == 'CONFIRMED'
    assert attempt.status == 'paid_processing'
    assert checkout.lifecycle_state == 'ready'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_pending_invoice_without_provider_deadline_stays_payable():
    payment, user, attempt, checkout = _terminal_direct_rows()
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={
            'id': 'provider-1',
            'status': 'PENDING',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
        observed_after=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert applied is True
    assert payment.status == 'PENDING'
    assert payment.expires_at is None
    assert attempt.status == 'pending'
    assert attempt.reconciliation_reason is None
    assert attempt.next_reconcile_at > datetime.now(UTC)
    assert checkout.lifecycle_state == 'awaiting_funds'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_pending_invoice_reopens_only_the_prior_missing_deadline_hold():
    payment, user, attempt, checkout = _terminal_direct_rows(lifecycle_state='operator_review')
    payment.status = 'OPERATOR_REVIEW'
    attempt.status = 'operator_review'
    attempt.reconciliation_reason = 'provider_invoice_missing_or_elapsed_expiry'
    checkout.terminal_reason = 'provider_invoice_missing_or_elapsed_expiry'
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload={
            'id': 'provider-1',
            'status': 'PENDING',
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
        observed_after=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert applied is True
    assert attempt.status == 'pending'
    assert checkout.lifecycle_state == 'awaiting_funds'
    assert checkout.terminal_reason is None


def _expired_deadline_payload():
    """Точный живой счёт, который провайдер всё ещё держит «в ожидании»."""

    return {
        'id': 'provider-1',
        'status': 'PENDING',
        'paymentMethod': 'SBPQR',
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }


@pytest.mark.asyncio
async def test_expired_provider_deadline_closes_the_cart_instead_of_calling_an_operator():
    """🔴 Мина F. Срок счёта вышел, денег нет — это брошенная корзина, а не авария.

    До этой правки такой заказ уходил в `operator_review` и запирал человеку новую покупку,
    запирал смену серверов тарифа и слал владельцу тревогу «ЗАКАЗ ЗАВИС». Разбирать было
    нечего: оба живых случая (заказы 32 и 34) закрылись сами, когда Platega через ~6 часов
    отчиталась отменой.
    """
    from app.services.device_first_checkout_service import _owner_alert_is_obsolete

    payment, user, attempt, checkout = _terminal_direct_rows()
    payment.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload=_expired_deadline_payload(),
        observed_after=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert applied is False
    # Пара «состояние + причина» выбрана не по смыслу, а по единственному условию возврата
    # поздних денег на баланс (`device_first_payment_service.py:1944-1952`).
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == 'cancelled_by_user_after_invoice'
    assert checkout.quote_state == 'expired'
    assert checkout.funding_state == 'invoice_abandoned'
    # 🔴 Требование 3: забор тарифа — `or_` из двух ветвей, и отмена заказа размыкает только
    # первую. Вторая смотрит на статус попытки мимо заказа. Сравниваем с САМИМ набором забора,
    # а не со списком-копией: иначе тест переживёт добавление `reconciliation` в тот набор и
    # тариф останется заперт молча, до первой попытки владельца сменить серверы.
    from app.database.crud.tariff import _CHECKOUT_TERMINAL_STATES, _DIRECT_PROVIDER_ATTEMPT_OPEN_STATES

    assert attempt.status == 'reconciliation'
    assert attempt.status not in _DIRECT_PROVIDER_ATTEMPT_OPEN_STATES
    assert checkout.lifecycle_state in _CHECKOUT_TERMINAL_STATES
    assert attempt.reconciliation_reason == 'provider_invoice_abandoned_after_expiry'
    assert payment.status == 'VERIFYING'
    # Ни одного признака аварии: `operator_hold` не запирает покупку, тревога владельцу гаснет.
    assert _owner_alert_is_obsolete(checkout) is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_payment_after_the_abandoned_cart_closes_lands_on_the_balance():
    """🔴 Отрицательный сценарий, которого прямо требует план: деньги не должны потеряться.

    Ссылка Platega после закрытия корзины остаётся живой — на этом стоит вся посылка мины F.
    Заплатит человек позже — сумма обязана лечь ему на баланс, а не повиснуть кредитом сверки
    на ручной разбор. Возврат работает ТОЛЬКО на паре `cancelled` + одна из двух причин, то
    есть этот тест и есть проверка, что мина F закрывает корзину правильно.
    """
    payment, user, attempt, checkout = _terminal_direct_rows()
    payment.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    payment.transaction_id = None
    user.balance_kopeks = 0
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(
            side_effect=[
                # Мина F закрывает корзину: Payment -> User -> Attempt -> Checkout.
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
                # Поздняя точная оплата: тот же порядок замков и отсутствующая строка ledger.
                Result(payment),
                Result(user),
                Result(attempt),
                Result(checkout),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )

    closed = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload=_expired_deadline_payload(),
        observed_after=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert closed is False
    assert checkout.lifecycle_state == 'cancelled'

    with patch(
        'app.services.device_first_payment_service.fulfill_direct_external_checkout',
        AsyncMock(),
    ) as fulfill:
        await settle_device_first_platega_payment(
            db,
            payment=payment,
            payload={'id': 'provider-1', 'paymentMethod': 2, 'paymentDetails': {'amount': '350.00', 'currency': 'RUB'}},
        )

    assert user.balance_kopeks == 35_000
    assert attempt.status == 'credited'
    assert attempt.reconciliation_reason == 'late_paid_wallet_credit'
    # Старый заказ не оживает и подписку не выдаёт — деньги стали балансом, и только им.
    assert checkout.fulfillment_state == 'not_started'
    fulfill.assert_not_awaited()
    ledger_rows = [
        call.args[0]
        for call in db.add.call_args_list
        if getattr(call.args[0], 'device_first_ledger_key', None) == 'direct_late_invoice:41'
    ]
    assert len(ledger_rows) == 1
    assert ledger_rows[0].amount_kopeks == 35_000


@pytest.mark.asyncio
async def test_a_live_invoice_without_an_elapsed_deadline_is_never_closed_by_mine_f():
    """Обратная половина: мина F не смеет закрывать счёт, срок которого ещё идёт."""
    payment, user, attempt, checkout = _terminal_direct_rows()
    payment.expires_at = datetime.now(UTC) + timedelta(minutes=5)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    applied = await _apply_direct_pending_provider_observation(
        db,
        attempt_id=attempt.id,
        payment_id=payment.id,
        payload=_expired_deadline_payload(),
        observed_after=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert applied is True
    assert checkout.lifecycle_state == 'awaiting_funds'
    assert checkout.terminal_reason is None
    assert attempt.status == 'pending'


def _stub_direct_create_identity_binding(monkeypatch, added):
    """Keep POST orchestration tests focused; lock fencing is tested below."""

    async def bind(_db, *, attempt_id, payment_id, provider_payment_id, redirect_url):
        attempt, payment = added
        assert (attempt.id, payment.id) == (attempt_id, payment_id)
        attempt.provider_payment_id = provider_payment_id
        attempt.redirect_url = redirect_url
        attempt.status = 'reconciliation'
        payment.platega_transaction_id = provider_payment_id
        payment.redirect_url = redirect_url
        payment.status = 'VERIFYING'
        return attempt

    monkeypatch.setattr('app.services.device_first_payment_service._bind_direct_provider_identity', bind)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'create_response',
    [
        {
            'transactionId': 'v1-invoice',
            'redirect': 'https://pay.example/v1-invoice',
            'paymentDetails': '450 RUB',
        },
        {'id': 'v2-invoice', 'url': 'https://pay.example/v2-invoice'},
    ],
)
async def test_direct_invoice_persists_create_identity_then_verifies_canonical_amount_once(
    monkeypatch, create_response
):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        external_payable_kopeks=45_000,
        tariff_total_kopeks=45_000,
        wallet_applied_kopeks=0,
    )
    added = []

    def add(model):
        added.append(model)

    async def flush():
        for model in added:
            if getattr(model, 'id', None) is None:
                model.id = 41 if hasattr(model, 'merchant_order_key') else 51

    db = SimpleNamespace(
        add=MagicMock(side_effect=add),
        flush=AsyncMock(side_effect=flush),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    create_payment = AsyncMock(return_value=create_response)

    class FakePlategaService:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 0

        async def create_payment(self, **_kwargs):
            return await create_payment()

        async def get_transaction(self, transaction_id):
            attempt, payment = added
            # The durable identity and protected redirect are committed before
            # the canonical GET. A retry therefore never needs another POST.
            assert transaction_id == attempt.provider_payment_id
            assert payment.platega_transaction_id == transaction_id
            assert payment.redirect_url == _safe_provider_redirect_url(create_response)
            assert attempt.status == 'reconciliation'
            assert payment.status == 'VERIFYING'
            return {
                'id': transaction_id,
                'status': 'PENDING',
                'expiresIn': '00:15:00',
                'paymentMethod': 2,
                'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
            }

    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', FakePlategaService)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')
    _stub_direct_create_identity_binding(monkeypatch, added)

    async def apply_pending(_db, *, attempt_id, payment_id, payload, observed_after):
        attempt, payment = added
        assert (attempt.id, payment.id) == (attempt_id, payment_id)
        assert payload['paymentMethod'] == 2
        attempt.provider_returned_amount_kopeks = 45_000
        attempt.provider_returned_currency = 'RUB'
        attempt.status = 'pending'
        payment.status = 'PENDING'
        payment.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        checkout.expires_at = payment.expires_at
        return True

    monkeypatch.setattr(
        'app.services.device_first_payment_service._apply_direct_pending_provider_observation', apply_pending
    )

    attempt = await _create_direct_platega_attempt(
        db,
        checkout_public_id=checkout.public_id,
        user_id=7,
        method_key='sbp',
        method_code=2,
        was_financially_committed=False,
    )

    assert create_payment.await_count == 1
    assert attempt.provider_payment_id == _provider_transaction_id(create_response)
    assert attempt.provider_returned_amount_kopeks == 45_000
    assert attempt.provider_returned_currency == 'RUB'
    assert attempt.status == 'pending'
    assert added[1].status == 'PENDING'
    assert added[1].expires_at > datetime.now(UTC)
    assert checkout.expires_at == added[1].expires_at


@pytest.mark.asyncio
async def test_direct_invoice_without_a_provider_deadline_opens_the_verified_single_invoice(monkeypatch):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        external_payable_kopeks=45_000,
        tariff_total_kopeks=45_000,
        wallet_applied_kopeks=0,
    )
    added = []

    def add(model):
        added.append(model)

    async def flush():
        for model in added:
            if getattr(model, 'id', None) is None:
                model.id = 41 if hasattr(model, 'merchant_order_key') else 51

    class FakePlategaService:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 0

        async def create_payment(self, **_kwargs):
            return {'id': 'no-deadline', 'url': 'https://pay.example/no-deadline'}

        async def get_transaction(self, _transaction_id):
            return {
                'id': 'no-deadline',
                'status': 'PENDING',
                'paymentMethod': 2,
                'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
            }

    db = SimpleNamespace(
        add=MagicMock(side_effect=add),
        flush=AsyncMock(side_effect=flush),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', FakePlategaService)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')
    _stub_direct_create_identity_binding(monkeypatch, added)
    monkeypatch.setattr(
        'app.services.device_first_payment_service._apply_direct_pending_provider_observation',
        AsyncMock(return_value=True),
    )

    attempt = await _create_direct_platega_attempt(
        db,
        checkout_public_id=checkout.public_id,
        user_id=7,
        method_key='sbp',
        method_code=2,
        was_financially_committed=False,
    )

    assert attempt.provider_payment_id == 'no-deadline'
    assert attempt.redirect_url == 'https://pay.example/no-deadline'


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_status', ['FAILED', 'CANCELED', 'EXPIRED'])
@pytest.mark.parametrize(
    'create_response',
    [
        {
            'transactionId': 'terminal-v1-invoice',
            'redirect': 'https://pay.example/terminal-v1',
            'paymentDetails': '450 RUB',
        },
        {'id': 'terminal-v2-invoice', 'url': 'https://pay.example/terminal-v2'},
    ],
)
async def test_direct_invoice_never_exposes_a_canonically_terminal_provider_status(
    monkeypatch, provider_status, create_response
):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        external_payable_kopeks=45_000,
        tariff_total_kopeks=45_000,
        wallet_applied_kopeks=0,
    )
    added = []

    def add(model):
        added.append(model)

    async def flush():
        for model in added:
            if getattr(model, 'id', None) is None:
                model.id = 41 if hasattr(model, 'merchant_order_key') else 51

    class FakePlategaService:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 0

        async def create_payment(self, **_kwargs):
            return create_response

        async def get_transaction(self, _transaction_id):
            return {
                'id': _provider_transaction_id(create_response),
                'status': provider_status,
                'paymentMethod': 'SBPQR',
                'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
            }

    db = SimpleNamespace(
        add=MagicMock(side_effect=add),
        flush=AsyncMock(side_effect=flush),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    release_terminal = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.device_first_payment_service._release_direct_terminal_invoice',
        release_terminal,
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', FakePlategaService)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')
    _stub_direct_create_identity_binding(monkeypatch, added)

    with pytest.raises(DeviceFirstError) as raised:
        await _create_direct_platega_attempt(
            db,
            checkout_public_id=checkout.public_id,
            user_id=7,
            method_key='sbp',
            method_code=2,
            was_financially_committed=False,
        )

    assert raised.value.code == 'invoice_terminal'
    release_terminal.assert_awaited_once_with(
        db,
        attempt_id=41,
        payment_id=51,
        payload={
            'id': _provider_transaction_id(create_response),
            'status': provider_status,
            'paymentMethod': 'SBPQR',
            'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
        },
        provider_status=provider_status,
        source='canonical_get',
    )


@pytest.mark.asyncio
async def test_direct_invoice_with_confirmed_canonical_status_uses_strict_settlement(monkeypatch):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        external_payable_kopeks=45_000,
        tariff_total_kopeks=45_000,
        wallet_applied_kopeks=0,
    )
    added = []

    def add(model):
        added.append(model)

    async def flush():
        for model in added:
            if getattr(model, 'id', None) is None:
                model.id = 41 if hasattr(model, 'merchant_order_key') else 51

    class FakePlategaService:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 0

        async def create_payment(self, **_kwargs):
            return {'id': 'confirmed-invoice', 'url': 'https://pay.example/confirmed'}

        async def get_transaction(self, _transaction_id):
            return {
                'id': 'confirmed-invoice',
                'status': 'CONFIRMED',
                'paymentMethod': 2,
                'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
            }

    db = SimpleNamespace(
        add=MagicMock(side_effect=add),
        flush=AsyncMock(side_effect=flush),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    settle = AsyncMock()
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', FakePlategaService)
    monkeypatch.setattr('app.services.device_first_payment_service.settle_device_first_platega_payment', settle)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')
    _stub_direct_create_identity_binding(monkeypatch, added)

    await _create_direct_platega_attempt(
        db,
        checkout_public_id=checkout.public_id,
        user_id=7,
        method_key='sbp',
        method_code=2,
        was_financially_committed=False,
    )

    settle.assert_awaited_once_with(
        db,
        payment=added[1],
        payload={
            'id': 'confirmed-invoice',
            'status': 'CONFIRMED',
            'paymentMethod': 2,
            'paymentDetails': {'amount': '450.00', 'currency': 'RUB'},
        },
    )


@pytest.mark.asyncio
async def test_direct_reconciler_escalates_an_old_identityless_invoice_without_another_provider_post(monkeypatch):
    attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        settlement_mode='direct_purchase_v2',
        status='reconciliation',
        provider_payment_id=None,
        platega_payment_id=51,
        lease_epoch=3,
    )

    class AttemptsResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [attempt])

    class FakePlategaService:
        def __init__(self):
            pass

        async def get_transaction(self, _transaction_id):
            raise AssertionError('identityless direct attempts must not be polled or recreated')

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[AttemptsResult(), SimpleNamespace(rowcount=1)]),
        get=AsyncMock(return_value=attempt),
        commit=AsyncMock(),
    )
    hold = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', FakePlategaService)
    monkeypatch.setattr('app.services.device_first_payment_service._hold_direct_invoice_for_review', hold)
    monkeypatch.setattr('app.services.device_first_payment_service._release_direct_attempt_lease', AsyncMock())

    assert await reconcile_device_first_payments(db) == 0
    hold.assert_awaited_once_with(
        db,
        attempt_id=41,
        payment_id=51,
        reason='provider_invoice_creation_incomplete',
        lease_token=ANY,
        lease_epoch=3,
    )


@pytest.mark.asyncio
async def test_payment_attempt_requires_explicitly_armed_checkout(monkeypatch):
    user = SimpleNamespace(id=7)
    checkout = SimpleNamespace(lifecycle_state='confirmed', armed_at=None, settlement_mode='legacy_deposit')
    db = SimpleNamespace(get=AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.device_first_payment_service.available_platega_methods_for_db',
        AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_owned_checkout',
        AsyncMock(return_value=checkout),
    )

    with pytest.raises(Exception) as error:
        await create_platega_attempt(
            db,
            checkout_public_id='checkout-1',
            user_id=7,
            method_key='sbp',
        )

    assert error.value.code == 'invalid_state'


@pytest.mark.asyncio
async def test_payment_attempt_rejects_replay_after_exact_payment_started_fulfillment(monkeypatch):
    """A settled invoice cannot be replaced with a second invoice while its outbox runs."""
    user = SimpleNamespace(id=7)
    checkout = SimpleNamespace(
        lifecycle_state='armed',
        armed_at=object(),
        fulfillment_state='in_progress',
        quote_state='committed',
        settlement_mode='legacy_deposit',
    )
    db = SimpleNamespace(get=AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.device_first_payment_service.available_platega_methods_for_db',
        AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_owned_checkout',
        AsyncMock(return_value=checkout),
    )

    with pytest.raises(DeviceFirstError) as raised:
        await create_platega_attempt(
            db,
            checkout_public_id='checkout-1',
            user_id=7,
            method_key='sbp',
        )

    assert raised.value.code == 'invalid_state'


@pytest.mark.asyncio
async def test_payment_attempt_requests_the_whole_ruble_top_up_shown_to_the_customer(monkeypatch):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        user_id=7,
        lifecycle_state='awaiting_funds',
        armed_at=object(),
        fulfillment_state='not_started',
        max_price_kopeks=40_100,
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        settlement_mode='legacy_deposit',
    )
    user = SimpleNamespace(id=7, balance_kopeks=30_050)
    db = SimpleNamespace(
        get=AsyncMock(return_value=user),
        execute=AsyncMock(side_effect=[Result(None), Result(user)]),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    provider = MagicMock()
    provider.create_payment = AsyncMock(
        return_value={'id': 'provider-91', 'redirectUrl': 'https://pay.example/91', 'status': 'PENDING'}
    )
    provider_class = MagicMock(return_value=provider)
    provider_class.parse_redirect_url.return_value = 'https://pay.example/91'
    monkeypatch.setattr(
        'app.services.device_first_payment_service.available_platega_methods_for_db',
        AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_owned_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', provider_class)
    monkeypatch.setattr('app.services.device_first_payment_service.settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)

    attempt = await create_platega_attempt(
        db,
        checkout_public_id=checkout.public_id,
        user_id=user.id,
        method_key='sbp',
    )

    assert attempt.requested_amount_kopeks == 10_100
    assert provider.create_payment.await_args.kwargs['amount'] == 101.0


@pytest.mark.asyncio
async def test_expired_quote_never_returns_or_creates_a_provider_invoice(monkeypatch):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        user_id=7,
        lifecycle_state='awaiting_funds',
        armed_at=object(),
        fulfillment_state='not_started',
        max_price_kopeks=40_100,
        quote_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        quote_state='valid',
        terminal_reason=None,
        settlement_mode='legacy_deposit',
    )
    user = SimpleNamespace(id=7, balance_kopeks=0)
    db = SimpleNamespace(get=AsyncMock(return_value=user), commit=AsyncMock())
    provider_class = MagicMock()
    monkeypatch.setattr(
        'app.services.device_first_payment_service.available_platega_methods_for_db',
        AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_owned_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', provider_class)

    with pytest.raises(DeviceFirstError, match='expired') as raised:
        await create_platega_attempt(
            db,
            checkout_public_id=checkout.public_id,
            user_id=user.id,
            method_key='sbp',
        )

    assert raised.value.code == 'quote_expired'
    assert checkout.lifecycle_state == 'reprice_required'
    provider_class.assert_not_called()


@pytest.mark.asyncio
async def test_settlement_rejects_provider_identity_mismatch_before_credit():
    attempt = SimpleNamespace(
        provider_payment_id='expected-provider-id',
        provider_method_code=2,
        settlement_mode='legacy_deposit',
        status='pending',
        reconciliation_reason=None,
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: attempt)
    db = AsyncMock()
    db.execute.return_value = result
    payment = SimpleNamespace(
        id=51,
        metadata_json={'device_first_attempt_id': 41},
        callback_payload=None,
    )
    db.execute.side_effect = [Result(payment), result]

    returned = await settle_device_first_platega_payment(
        db,
        payment=payment,
        payload={
            'id': 'different-provider-id',
            'paymentMethod': 2,
            'paymentDetails': {'amount': '10.00', 'currency': 'RUB'},
        },
    )

    assert returned is payment
    assert attempt.status == 'reconciliation'
    assert attempt.reconciliation_reason == 'provider_identity_mismatch'
    assert payment.callback_payload['id'] == 'different-provider-id'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_direct_reconciliation_lease_cannot_settle_payment():
    payment = SimpleNamespace(
        id=51,
        metadata_json={'device_first_attempt_id': 41},
        status='PENDING',
        is_paid=False,
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(None)]),
        commit=AsyncMock(),
    )

    settled = await settle_device_first_platega_payment(
        db,
        payment=payment,
        payload={'id': 'provider-1'},
        lease_token='superseded-token',
        lease_epoch=3,
    )

    assert settled is None
    assert payment.status == 'PENDING'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_late_exact_confirmation_keeps_wallet_credit_idempotent():
    payment = SimpleNamespace(
        id=51,
        user_id=7,
        platega_transaction_id='provider-1',
        amount_kopeks=35_000,
        currency='RUB',
        payment_method_code=2,
        metadata_json={'device_first_attempt_id': 41, 'settlement_mode': 'direct_purchase_v2'},
        status='CONFIRMED',
        is_paid=True,
    )
    user = SimpleNamespace(id=7, balance_kopeks=35_000)
    attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        platega_payment_id=51,
        provider='platega',
        provider_payment_id='provider-1',
        provider_method_code=2,
        requested_amount_kopeks=35_000,
        currency='RUB',
        settlement_mode='direct_purchase_v2',
        status='credited',
        reconciliation_reason='late_paid_wallet_credit',
    )
    checkout = SimpleNamespace(id=9, user_id=7, settlement_mode='direct_purchase_v2')
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(payment), Result(user), Result(attempt), Result(checkout)]),
        commit=AsyncMock(),
    )

    settled = await settle_device_first_platega_payment(
        db,
        payment=payment,
        payload={
            'id': 'provider-1',
            'paymentMethod': 2,
            'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
        },
    )

    assert settled is payment
    assert user.balance_kopeks == 35_000
    assert attempt.status == 'credited'
    assert attempt.reconciliation_reason == 'late_paid_wallet_credit'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_terminal_checkout_cannot_open_another_provider_invoice(monkeypatch):
    checkout = SimpleNamespace(
        financial_committed_at=datetime.now(UTC),
        funding_mode='platega',
        lifecycle_state='operator_review',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        created_subscription_id=None,
        debit_transaction_id=None,
    )
    monkeypatch.setattr(
        'app.services.device_first_checkout_service._lock_direct_context',
        AsyncMock(return_value=(checkout, SimpleNamespace(id=7), None, SimpleNamespace())),
    )
    monkeypatch.setattr('app.services.device_first_checkout_service.settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr('app.services.device_first_checkout_service.is_device_first_canary_user', lambda _user: True)

    with pytest.raises(DeviceFirstError) as raised:
        await prepare_direct_external_checkout(db=SimpleNamespace(), public_id='checkout-1', user_id=7)

    assert raised.value.code == 'invalid_state'


@pytest.mark.asyncio
async def test_normal_direct_commit_does_not_recreate_a_missing_pre_attempt_invoice(monkeypatch):
    checkout = SimpleNamespace(
        id=91,
        public_id='checkout-91',
        financial_committed_at=datetime.now(UTC),
        funding_mode='platega',
    )
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=7)))
    monkeypatch.setattr(
        'app.services.device_first_payment_service.available_platega_methods_for_db',
        AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_owned_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')

    with pytest.raises(DeviceFirstError) as raised:
        await _create_direct_platega_attempt(
            db,
            checkout_public_id=checkout.public_id,
            user_id=7,
            method_key='sbp',
            method_code=2,
            was_financially_committed=True,
        )

    assert raised.value.code == 'invalid_state'


@pytest.mark.asyncio
async def test_direct_post_paid_provider_reversal_stays_in_operator_review():
    user = SimpleNamespace(id=7)
    payment = SimpleNamespace(
        id=51,
        user_id=7,
        is_paid=True,
        status='CONFIRMED',
        metadata_json={'device_first_attempt_id': 41, 'settlement_mode': 'direct_purchase_v2'},
    )
    attempt = SimpleNamespace(id=41, checkout_id=9, settlement_mode='direct_purchase_v2', status='paid_processing')
    checkout = SimpleNamespace(id=9, lifecycle_state='fulfilling', terminal_reason=None)
    outbox = SimpleNamespace(status='processing', last_error=None)

    class OutboxResult:
        def scalars(self):
            return iter([outbox])

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(user), Result(attempt), Result(checkout), OutboxResult()]),
        commit=AsyncMock(),
    )

    await PlategaPaymentMixin()._mark_direct_post_paid_reversal(
        db,
        payment=payment,
        provider_status='CANCELED',
    )

    assert payment.is_paid is True
    assert payment.status == 'OPERATOR_REVIEW'
    assert attempt.status == 'operator_review'
    assert checkout.lifecycle_state == 'operator_review'
    assert outbox.status == 'operator_review'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_reconciler_fences_a_post_paid_provider_terminal_for_operator_review(monkeypatch):
    attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        settlement_mode='direct_purchase_v2',
        status='pending',
        lease_epoch=3,
        provider_payment_id='provider-1',
        platega_payment_id=51,
        reconcile_attempts=0,
    )
    payment = SimpleNamespace(id=51, is_paid=True, status='PENDING')

    class AttemptsResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [attempt])

    provider = SimpleNamespace(get_transaction=AsyncMock(return_value={'status': 'CANCELED'}))
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[AttemptsResult(), SimpleNamespace(rowcount=1), Result(payment)]),
        get=AsyncMock(side_effect=[attempt, payment]),
        commit=AsyncMock(),
    )
    fence = AsyncMock()
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', lambda: provider)
    monkeypatch.setattr(
        'app.services.device_first_payment_service._lock_owned_direct_attempt_lease',
        AsyncMock(side_effect=[attempt, attempt]),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service._release_direct_attempt_lease',
        AsyncMock(),
    )
    monkeypatch.setattr('app.services.payment.platega.PlategaPaymentMixin._mark_direct_post_paid_reversal', fence)

    processed = await reconcile_device_first_payments(db)

    assert processed == 0
    fence.assert_awaited_once_with(
        db,
        payment=payment,
        provider_status='CANCELED',
        lease_token=ANY,
        lease_epoch=3,
    )


@pytest.mark.asyncio
async def test_direct_reconciler_refreshes_webhook_first_payment_before_confirmed_recovery(monkeypatch):
    """A worker's stale pre-webhook objects must not turn a ready sale into review."""
    stale_attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        settlement_mode='direct_purchase_v2',
        status='pending',
        lease_epoch=3,
        provider_payment_id='provider-1',
        platega_payment_id=51,
        reconcile_attempts=0,
        reconciliation_reason=None,
    )
    stale_payment = SimpleNamespace(id=51, is_paid=False, status='PENDING')
    webhook_payment = SimpleNamespace(
        id=51,
        user_id=7,
        is_paid=True,
        status='CONFIRMED',
        platega_transaction_id='provider-1',
        amount_kopeks=10_000,
        currency='RUB',
        payment_method_code=2,
        metadata_json={'device_first_attempt_id': 41, 'settlement_mode': 'direct_purchase_v2'},
    )
    webhook_attempt = SimpleNamespace(
        id=41,
        checkout_id=9,
        provider='platega',
        settlement_mode='direct_purchase_v2',
        status='paid_processing',
        provider_payment_id='provider-1',
        provider_method_code=2,
        requested_amount_kopeks=10_000,
        currency='RUB',
        platega_payment_id=51,
        reconciliation_reason=None,
    )
    webhook_checkout = SimpleNamespace(id=9, user_id=7, settlement_mode='direct_purchase_v2')

    class AttemptsResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [stale_attempt])

    provider = SimpleNamespace(
        get_transaction=AsyncMock(
            return_value={
                'id': 'provider-1',
                'status': 'CONFIRMED',
                'paymentMethod': 2,
                'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
            }
        )
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                AttemptsResult(),
                SimpleNamespace(rowcount=1),
                Result(webhook_payment),
                Result(SimpleNamespace(id=7)),
                Result(webhook_attempt),
                Result(webhook_checkout),
            ]
        ),
        get=AsyncMock(side_effect=[stale_attempt, stale_payment]),
        commit=AsyncMock(),
    )
    fulfillment = AsyncMock()
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', lambda: provider)
    monkeypatch.setattr(
        'app.services.device_first_payment_service._lock_owned_direct_attempt_lease',
        AsyncMock(return_value=stale_attempt),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service._release_direct_attempt_lease',
        AsyncMock(),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.fulfill_direct_external_checkout', fulfillment)

    processed = await reconcile_device_first_payments(db)

    assert processed == 1
    assert webhook_payment.status == 'CONFIRMED'
    assert webhook_attempt.status == 'paid_processing'
    assert webhook_attempt.reconciliation_reason is None
    fulfillment.assert_not_awaited()


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


def _payment():
    return SimpleNamespace(
        id=51,
        user_id=7,
        correlation_id='correlation',
        platega_transaction_id='provider-1',
        metadata_json={'device_first_attempt_id': 41},
        callback_payload=None,
        transaction_id=None,
        status='PENDING',
        is_paid=False,
    )


def _attempt():
    return SimpleNamespace(
        id=41,
        checkout_id=9,
        provider_payment_id='provider-1',
        provider_method_code=2,
        settlement_mode='legacy_deposit',
        requested_amount_kopeks=35000,
        credited_amount_kopeks=0,
        status='pending',
        reconciliation_reason=None,
    )


def _checkout(state='armed'):
    return SimpleNamespace(
        id=9,
        public_id='checkout-1',
        user_id=7,
        lifecycle_state=state,
        armed_at=object(),
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        quote_state='valid',
        terminal_reason=None,
        settlement_mode='legacy_deposit',
    )


@pytest.mark.asyncio
async def test_duplicate_webhook_credits_balance_once_and_reuses_durable_outbox():
    attempt = _attempt()
    checkout = _checkout()
    user = SimpleNamespace(id=7, balance_kopeks=10000)
    existing = SimpleNamespace(id=88, device_first_checkout_id=9)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment := _payment()),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(existing),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(),
        ) as ensure,
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ) as side_effects,
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert user.balance_kopeks == 10000
    db.add.assert_not_called()
    assert payment.transaction_id == 88
    ensure.assert_awaited_once_with(db, transaction_id=88, checkout_id=9)
    side_effects.assert_awaited_once_with(db, transaction_id=88, limit=1)


@pytest.mark.asyncio
async def test_late_cancelled_payment_is_credited_but_never_fulfills_checkout():
    attempt = _attempt()
    checkout = _checkout('cancelled')
    user = SimpleNamespace(id=7, balance_kopeks=10000)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment := _payment()),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(return_value=SimpleNamespace(fulfillment_status='pending')),
        ) as ensure,
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ),
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert user.balance_kopeks == 45000
    assert payment.is_paid is True
    assert attempt.reconciliation_reason == 'late_paid_credited_to_balance_only'
    assert ensure.return_value.fulfillment_status == 'not_required'


@pytest.mark.asyncio
async def test_partial_provider_amount_records_reconciliation_and_uses_actual_credit():
    attempt = _attempt()
    checkout = _checkout()
    user = SimpleNamespace(id=7, balance_kopeks=0)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment := _payment()),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '100.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(return_value=SimpleNamespace(fulfillment_status='pending')),
        ) as ensure,
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ),
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert user.balance_kopeks == 10000
    assert attempt.credited_amount_kopeks == 10000
    assert attempt.reconciliation_reason == 'amount_mismatch:requested=35000:actual=10000'
    assert checkout.lifecycle_state == 'conflict'
    assert checkout.terminal_reason == 'payment_amount_mismatch'
    assert ensure.return_value.fulfillment_status == 'not_required'
    created = db.add.call_args.args[0]
    assert created.amount_kopeks == 10000


@pytest.mark.asyncio
async def test_exact_provider_amount_queues_durable_fulfillment():
    attempt = _attempt()
    checkout = _checkout()
    user = SimpleNamespace(id=7, balance_kopeks=0)
    payment = _payment()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    job = SimpleNamespace(fulfillment_status='not_required')
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(return_value=job),
        ),
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ) as process,
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert checkout.lifecycle_state == 'armed'
    assert checkout.fulfillment_state == 'in_progress'
    assert checkout.funding_state == 'funded'
    assert checkout.quote_state == 'committed'
    assert job.fulfillment_status == 'pending'
    process.assert_awaited_once()

    # A payment accepted before expiry keeps this exact approved quote while a
    # later outbox worker resumes after the 30-minute display window.
    checkout.quote_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    follow_up_db = SimpleNamespace(commit=AsyncMock())
    assert not await expire_checkout_quote_if_needed(follow_up_db, checkout)
    follow_up_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_overpayment_is_credited_to_balance_but_never_auto_fulfills():
    attempt = _attempt()
    checkout = _checkout()
    user = SimpleNamespace(id=7, balance_kopeks=0)
    payment = _payment()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    job = SimpleNamespace(fulfillment_status='pending')
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '400.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(return_value=job),
        ),
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ) as process,
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert user.balance_kopeks == 40_000
    assert checkout.lifecycle_state == 'conflict'
    assert checkout.terminal_reason == 'payment_amount_mismatch'
    assert attempt.reconciliation_reason == 'amount_mismatch:requested=35000:actual=40000'
    assert job.fulfillment_status == 'not_required'
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_quote_payment_is_credited_to_balance_but_never_auto_fulfills():
    attempt = _attempt()
    checkout = _checkout()
    checkout.quote_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    user = SimpleNamespace(id=7, balance_kopeks=0)
    payment = _payment()
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(payment),
                Result(attempt),
                Result(checkout),
                Result(user),
                Result(None),
            ]
        ),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    job = SimpleNamespace(fulfillment_status='pending')
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '350.00', 'currency': 'RUB'},
    }

    with (
        patch(
            'app.services.device_first_payment_service.ensure_deposit_outbox',
            AsyncMock(return_value=job),
        ),
        patch(
            'app.services.device_first_payment_service.process_device_first_deposit_outbox',
            AsyncMock(),
        ) as process,
    ):
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert user.balance_kopeks == 35_000
    assert checkout.lifecycle_state == 'reprice_required'
    assert checkout.quote_state == 'expired'
    assert checkout.terminal_reason == 'quote_expired'
    assert attempt.reconciliation_reason == 'quote_expired_paid_credited_to_balance_only'
    assert job.fulfillment_status == 'not_required'
    process.assert_awaited_once()

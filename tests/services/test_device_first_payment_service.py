from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.device_first_checkout_service import DeviceFirstError, expire_checkout_quote_if_needed
from app.services.device_first_payment_service import (
    _checkout_return_url,
    _direct_checkout_return_url,
    _verified_amount,
    available_platega_methods,
    create_platega_attempt,
    settle_device_first_platega_payment,
)
from app.services.platega_service import PlategaService


def test_verified_amount_uses_actual_rub_provider_value():
    assert _verified_amount({'paymentDetails': {'amount': '123.45', 'currency': 'RUB'}}) == (12345, 'RUB')


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
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example/anything')
    assert _direct_checkout_return_url('checkout-1') == 'https://cabinet.example/subscription/purchase?checkout=checkout-1'
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


@pytest.mark.asyncio
async def test_payment_attempt_requires_explicitly_armed_checkout(monkeypatch):
    user = SimpleNamespace(id=7)
    checkout = SimpleNamespace(lifecycle_state='confirmed', armed_at=None)
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

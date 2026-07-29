from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.device_first_payment_service import (
    _checkout_return_url,
    _verified_amount,
    available_platega_methods,
    create_platega_attempt,
    settle_device_first_platega_payment,
)


def test_verified_amount_uses_actual_rub_provider_value():
    assert _verified_amount({'paymentDetails': {'amount': '123.45', 'currency': 'RUB'}}) == (12345, 'RUB')


def test_verified_amount_rejects_untrusted_currency_or_missing_amount():
    assert _verified_amount({'paymentDetails': {'amount': '10', 'currency': 'USD'}}) is None
    assert _verified_amount({'paymentDetails': {'currency': 'RUB'}}) is None


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
    assert checkout.lifecycle_state == 'awaiting_funds'
    assert ensure.return_value.fulfillment_status == 'action_required'
    created = db.add.call_args.args[0]
    assert created.amount_kopeks == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_amount', ['350.00', '400.00'])
async def test_exact_or_overpayment_queues_durable_fulfillment(provider_amount):
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
        'paymentDetails': {'amount': provider_amount, 'currency': 'RUB'},
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
    assert job.fulfillment_status == 'pending'
    process.assert_awaited_once()

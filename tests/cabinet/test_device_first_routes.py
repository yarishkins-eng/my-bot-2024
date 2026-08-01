from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.device_first import (
    CheckoutCommitRequest,
    PaymentAttemptRequest,
    _checkout_command,
    _mutation,
    _rehydrate_owned_direct_redirect,
    checkout_commit,
    checkout_get,
    checkout_open,
    checkout_pending_payment,
    checkout_resume_invoice,
)
from app.services.device_first_checkout_service import DeviceFirstError, checkout_ui_state


@pytest.mark.asyncio
async def test_foreign_checkout_get_preserves_safe_404() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    with patch(
        'app.cabinet.routes.device_first.get_owned_checkout',
        AsyncMock(side_effect=DeviceFirstError('not_found', 'Checkout not found', status_code=404)),
    ):
        with pytest.raises(HTTPException) as raised:
            await checkout_get('foreign-id', user=user, db=AsyncMock())
    assert raised.value.status_code == 404
    assert raised.value.detail['code'] == 'not_found'


@pytest.mark.asyncio
async def test_open_checkout_endpoint_returns_only_the_authenticated_users_checkout() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=1250)
    checkout = SimpleNamespace(public_id='owned-checkout')
    db = AsyncMock()
    with (
        patch(
            'app.cabinet.routes.device_first.get_open_checkout_for_user',
            AsyncMock(return_value=checkout),
        ) as get_open,
        patch(
            'app.cabinet.routes.device_first.serialize_checkout',
            return_value={'id': 'owned-checkout'},
        ),
    ):
        response = await checkout_open(user=user, db=db)

    assert response == {'id': 'owned-checkout'}
    get_open.assert_awaited_once_with(db, user_id=17)


@pytest.mark.asyncio
async def test_incomplete_mutation_is_reentered_for_canonical_recovery() -> None:
    existing = SimpleNamespace(request_hash='same', response_json=None)
    result = SimpleNamespace(scalar_one_or_none=lambda: existing)
    db = AsyncMock()
    db.execute.return_value = result

    with patch('app.cabinet.routes.device_first.request_hash', return_value='same'):
        mutation, replay = await _mutation(
            db,
            user_id=7,
            action='confirm',
            key='stable-intent',
            payload={'checkout_id': 'checkout-id'},
        )

    assert mutation is existing
    assert replay is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_recovers_canonical_response_after_lost_http_response() -> None:
    mutation = SimpleNamespace(checkout_id=41)
    recovered = SimpleNamespace(
        id=41,
        public_id='public-checkout',
        lifecycle_state='armed',
    )
    fulfilled = SimpleNamespace(
        id=41,
        public_id='public-checkout',
        lifecycle_state='fulfilling',
    )
    user = SimpleNamespace(id=7, balance_kopeks=1250)
    db = AsyncMock()
    db.get.return_value = recovered
    canonical = {'id': 'public-checkout', 'ui_state': 'processing'}

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch(
            'app.cabinet.routes.device_first._mutation',
            AsyncMock(return_value=(mutation, None)),
        ),
        patch(
            'app.cabinet.routes.device_first.serialize_checkout',
            return_value=canonical,
        ),
        patch(
            'app.cabinet.routes.device_first.store_mutation_result',
            AsyncMock(),
        ) as store,
        patch(
            'app.cabinet.routes.device_first.confirm_checkout',
            AsyncMock(),
        ) as confirm,
        patch(
            'app.cabinet.routes.device_first.fulfill_checkout',
            AsyncMock(return_value=fulfilled),
        ) as fulfill,
    ):
        response = await _checkout_command(
            action='arm',
            checkout_id='public-checkout',
            idempotency_key='stable-intent',
            user=user,
            db=db,
        )

    assert response == canonical
    confirm.assert_not_awaited()
    fulfill.assert_awaited_once_with(db, 'public-checkout', user.id)
    store.assert_awaited_once_with(db, mutation, response=canonical)


def test_fulfilled_checkout_is_not_ready_before_provisioning() -> None:
    checkout = SimpleNamespace(
        lifecycle_state='fulfilling',
        fulfillment_state='fulfilled',
        provisioning_state='pending',
        quote_state='valid',
    )
    assert checkout_ui_state(checkout) == 'provisioning'
    checkout.lifecycle_state = 'ready'
    checkout.provisioning_state = 'ready'
    assert checkout_ui_state(checkout) == 'ready'


@pytest.mark.asyncio
async def test_resume_invoice_rejects_paid_or_operator_review_direct_checkout() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        funding_mode='platega',
        financial_committed_at=object(),
        lifecycle_state='operator_review',
        funding_state='funded',
        fulfillment_state='fulfilled',
        created_subscription_id=42,
        debit_transaction_id=77,
    )
    db = AsyncMock()
    with (
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=checkout)),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        with pytest.raises(HTTPException) as raised:
            await checkout_resume_invoice(
                'checkout-91',
                PaymentAttemptRequest(method_key='sbp'),
                idempotency_key='resume-operator-review',
                user=user,
                db=db,
            )

    assert raised.value.status_code == 409
    assert raised.value.detail['code'] == 'invoice_resume_unavailable'
    create_attempt.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_payment_never_offers_resume_for_direct_operator_review() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        funding_mode='platega',
        financial_committed_at=object(),
        lifecycle_state='operator_review',
        funding_state='funded',
        fulfillment_state='fulfilled',
        created_subscription_id=42,
        debit_transaction_id=77,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(None), Result(41)]))
    with patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=checkout)):
        response = await checkout_pending_payment('checkout-91', user=user, db=db)

    assert response == {'redirect_url': None, 'status': 'missing', 'resume_allowed': False}


@pytest.mark.asyncio
async def test_direct_redirect_replay_is_rehydrated_from_owned_provider_payment_not_mutation_json() -> None:
    user = SimpleNamespace(id=17)
    checkout = SimpleNamespace(id=91, settlement_mode='direct_purchase_v2')
    attempt = SimpleNamespace(platega_payment_id=51)
    payment = SimpleNamespace(redirect_url='https://pay.example/live')
    result = SimpleNamespace(scalar_one_or_none=lambda: attempt)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=payment))
    stored_response = {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/stale'}

    with patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=checkout)):
        response = await _rehydrate_owned_direct_redirect(db, user=user, stored_response=stored_response)

    assert response == {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/live'}
    assert stored_response['redirect_url'] == 'https://pay.example/stale'


@pytest.mark.asyncio
async def test_direct_commit_stores_a_redacted_idempotency_response() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(id=91, settlement_mode='direct_purchase_v2')
    attempt = SimpleNamespace(platega_payment_id=51)
    payment = SimpleNamespace(redirect_url='https://pay.example/live')
    db = SimpleNamespace(get=AsyncMock(return_value=payment))

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.get_owned_checkout',
            AsyncMock(side_effect=[checkout, checkout]),
        ),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock(return_value=attempt)),
        patch('app.cabinet.routes.device_first.serialize_checkout', return_value={'id': 'owned-checkout'}),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await checkout_commit(
            'owned-checkout',
            CheckoutCommitRequest(funding_mode='platega', method_key='sbp'),
            idempotency_key='commit-redacted',
            user=user,
            db=db,
        )

    assert response == {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/live'}
    assert store.await_args.kwargs['response'] == {'checkout': {'id': 'owned-checkout'}}

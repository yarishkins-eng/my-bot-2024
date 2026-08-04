from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.dependencies import get_current_native_launch_user
from app.cabinet.routes.device_first import (
    CheckoutCommitRequest,
    NativeCheckoutLaunchRequest,
    PaymentAttemptRequest,
    _checkout_command,
    _mutation,
    _rehydrate_owned_direct_redirect,
    checkout_abandon,
    checkout_commit,
    checkout_create,
    checkout_get,
    checkout_native_launch,
    checkout_open,
    checkout_pending_payment,
    checkout_resume_invoice,
    direct_checkout_commit,
    direct_native_launch,
)
from app.cabinet.schemas.device_first import DirectCheckoutCommitRequest
from app.services.device_first_checkout_service import DeviceFirstError, checkout_ui_state


@pytest.mark.asyncio
async def test_native_launch_requires_valid_telegram_identity_before_any_checkout_lookup() -> None:
    user = SimpleNamespace(id=17, telegram_id=7001)
    request = SimpleNamespace(headers={})

    with pytest.raises(HTTPException) as raised:
        await get_current_native_launch_user(request, user)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_native_launch_rejects_a_signed_identity_for_another_telegram_account() -> None:
    user = SimpleNamespace(id=17, telegram_id=7001)
    request = SimpleNamespace(headers={'X-Telegram-Init-Data': 'signed-but-other-user'})

    with patch('app.cabinet.dependencies.validate_telegram_init_data', return_value={'id': 7002}):
        with pytest.raises(HTTPException) as raised:
            await get_current_native_launch_user(request, user)

    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_native_launch_delegates_to_the_same_direct_payment_state_machine_and_cannot_debit_wallet() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0, telegram_id=7001)
    db = AsyncMock()
    expected = {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/live'}

    with patch('app.cabinet.routes.device_first._commit_checkout', AsyncMock(return_value=expected)) as commit:
        response = await checkout_native_launch(
            'owned-checkout',
            NativeCheckoutLaunchRequest(method_key='sbp'),
            idempotency_key='native-launch-owned-checkout-sbp',
            user=user,
            db=db,
        )

    assert response == expected
    request = commit.await_args.kwargs['request']
    assert request.funding_mode == 'platega'
    assert request.method_key == 'sbp'
    assert commit.await_args.kwargs['action'] == 'native_launch'


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
async def test_new_different_cabinet_selection_archives_only_the_old_direct_invoice() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    old_invoice = SimpleNamespace(
        id=41,
        public_id='old-checkout',
        settlement_mode='direct_purchase_v2',
        period_days=30,
        selected_device_limit=4,
    )
    archived = SimpleNamespace(lifecycle_state='cancelled')
    fresh = SimpleNamespace(id=92, public_id='fresh-checkout')
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.get_open_checkout_for_user', AsyncMock(return_value=old_invoice)),
        patch(
            'app.cabinet.routes.device_first.abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=archived),
        ) as abandon,
        patch('app.cabinet.routes.device_first.create_checkout', AsyncMock(return_value=fresh)) as create,
        patch('app.cabinet.routes.device_first.serialize_checkout', return_value={'id': 'fresh-checkout'}),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        response = await checkout_create(
            SimpleNamespace(
                period_days=90,
                selected_device_limit=2,
                model_dump=lambda: {'period_days': 90, 'selected_device_limit': 2},
            ),
            idempotency_key='new-different-choice',
            user=user,
            db=db,
        )

    assert response == {'id': 'fresh-checkout'}
    abandon.assert_awaited_once_with(db, checkout_public_id='old-checkout', user_id=17)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_different_cabinet_selection_discards_a_quote_without_invoice() -> None:
    """A quote is disposable until the customer actually selects payment."""
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    old_quote = SimpleNamespace(
        public_id='old-quote',
        settlement_mode='direct_purchase_v2',
        period_days=30,
        selected_device_limit=2,
    )
    fresh = SimpleNamespace(id=93, public_id='fresh-checkout')
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.get_open_checkout_for_user', AsyncMock(return_value=old_quote)),
        patch(
            'app.cabinet.routes.device_first.abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=None),
        ) as abandon,
        patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=old_quote)) as locked,
        patch(
            'app.cabinet.routes.device_first.cancel_checkout_for_new_calculation',
            AsyncMock(return_value=old_quote),
        ) as cancel_quote,
        patch('app.cabinet.routes.device_first.create_checkout', AsyncMock(return_value=fresh)) as create,
        patch('app.cabinet.routes.device_first.serialize_checkout', return_value={'id': 'fresh-checkout'}),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        response = await checkout_create(
            SimpleNamespace(
                period_days=90,
                selected_device_limit=4,
                model_dump=lambda: {'period_days': 90, 'selected_device_limit': 4},
            ),
            idempotency_key='replace-quote-without-invoice',
            user=user,
            db=db,
        )

    assert response == {'id': 'fresh-checkout'}
    abandon.assert_awaited_once_with(db, checkout_public_id='old-quote', user_id=17)
    locked.assert_awaited_once_with(db, public_id='old-quote', user_id=17, for_update=True)
    cancel_quote.assert_awaited_once_with(db, old_quote)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_cabinet_selection_resumes_a_live_invoice_instead_of_abandoning_it() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    old_invoice = SimpleNamespace(
        id=41,
        public_id='old-checkout',
        settlement_mode='direct_purchase_v2',
        period_days=30,
        selected_device_limit=4,
    )
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.get_open_checkout_for_user', AsyncMock(return_value=old_invoice)),
        patch('app.cabinet.routes.device_first.abandon_direct_checkout_for_new_calculation', AsyncMock()) as abandon,
        patch('app.cabinet.routes.device_first.create_checkout', AsyncMock()) as create,
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'old-checkout'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await checkout_create(
            SimpleNamespace(
                period_days=30,
                selected_device_limit=4,
                model_dump=lambda: {'period_days': 30, 'selected_device_limit': 4},
            ),
            idempotency_key='same-choice',
            user=user,
            db=db,
        )

    assert response == {'id': 'old-checkout'}
    abandon.assert_not_awaited()
    create.assert_not_awaited()
    assert mutation.checkout_id == 41
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_selection_keeps_the_current_checkout_when_abandon_loses_to_settlement() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    old_invoice = SimpleNamespace(
        id=91,
        public_id='old-checkout',
        settlement_mode='direct_purchase_v2',
        period_days=30,
        selected_device_limit=4,
        lifecycle_state='processing',
    )
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.get_open_checkout_for_user', AsyncMock(return_value=old_invoice)),
        patch(
            'app.cabinet.routes.device_first.abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=old_invoice),
        ) as abandon,
        patch('app.cabinet.routes.device_first.create_checkout', AsyncMock()) as create,
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'old-checkout'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await checkout_create(
            SimpleNamespace(
                period_days=90,
                selected_device_limit=2,
                model_dump=lambda: {'period_days': 90, 'selected_device_limit': 2},
            ),
            idempotency_key='changed-choice-after-settlement',
            user=user,
            db=db,
        )

    assert response == {'id': 'old-checkout'}
    abandon.assert_awaited_once_with(db, checkout_public_id='old-checkout', user_id=user.id)
    create.assert_not_awaited()
    store.assert_awaited_once_with(db, mutation, response={'id': 'old-checkout'})


@pytest.mark.asyncio
async def test_explicit_abandon_uses_its_own_idempotent_owner_scoped_action() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    archived = SimpleNamespace(id=91, public_id='old-checkout', lifecycle_state='cancelled')
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.abandon_direct_checkout_for_new_calculation',
            AsyncMock(return_value=archived),
        ) as abandon,
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'old-checkout'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await checkout_abandon(
            'old-checkout',
            idempotency_key='abandon-old-checkout',
            user=user,
            db=db,
        )

    assert response == {'id': 'old-checkout'}
    abandon.assert_awaited_once_with(db, checkout_public_id='old-checkout', user_id=user.id)
    assert mutation.checkout_id == archived.id
    store.assert_awaited_once_with(db, mutation, response={'id': 'old-checkout'})


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
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt = SimpleNamespace(platega_payment_id=51, status='pending')
    payment = SimpleNamespace(
        redirect_url='https://pay.example/live',
        status='PENDING',
        is_paid=False,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: attempt)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=payment))
    stored_response = {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/stale'}

    with patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=checkout)):
        response = await _rehydrate_owned_direct_redirect(db, user=user, stored_response=stored_response)

    assert response == {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/live'}
    assert stored_response['redirect_url'] == 'https://pay.example/stale'


@pytest.mark.asyncio
async def test_failed_direct_invoice_never_rehydrates_or_reappears_in_pending_payment() -> None:
    user = SimpleNamespace(id=17)
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt = SimpleNamespace(platega_payment_id=51, status='pending')
    # A failed webhook can arrive before the attempt reconciler writes its
    # terminal state. The protected provider state wins over that stale row.
    payment = SimpleNamespace(
        redirect_url='https://pay.example/failed',
        status='FAILED',
        is_paid=False,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: attempt)
    db = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=payment))
    stored_response = {'checkout': {'id': 'owned-checkout'}, 'redirect_url': 'https://pay.example/stale'}

    with patch('app.cabinet.routes.device_first.get_owned_checkout', AsyncMock(return_value=checkout)):
        replay = await _rehydrate_owned_direct_redirect(db, user=user, stored_response=stored_response)
        pending = await checkout_pending_payment('owned-checkout', user=user, db=db)

    assert replay == {'checkout': {'id': 'owned-checkout'}}
    assert pending == {'redirect_url': None, 'status': 'pending', 'resume_allowed': False}


@pytest.mark.asyncio
async def test_direct_commit_stores_a_redacted_idempotency_response() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt = SimpleNamespace(platega_payment_id=51, status='pending')
    payment = SimpleNamespace(
        redirect_url='https://pay.example/live',
        is_paid=False,
        status='PENDING',
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
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


@pytest.mark.asyncio
async def test_direct_commit_returns_settled_owned_checkout_without_a_provider_redirect() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    initial_checkout = SimpleNamespace(id=91, settlement_mode='direct_purchase_v2')
    settled_checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='fulfilling',
        funding_state='paid',
        fulfillment_state='fulfilled',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt = SimpleNamespace(platega_payment_id=51, status='paid_processing')
    payment = SimpleNamespace(
        redirect_url='https://pay.example/confirmed',
        is_paid=True,
        status='CONFIRMED',
        expires_at=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=payment))

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.get_owned_checkout',
            AsyncMock(side_effect=[initial_checkout, settled_checkout]),
        ),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock(return_value=attempt)),
        patch('app.cabinet.routes.device_first.serialize_checkout', return_value={'id': 'owned-checkout'}),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await checkout_commit(
            'owned-checkout',
            CheckoutCommitRequest(funding_mode='platega', method_key='sbp'),
            idempotency_key='commit-confirmed',
            user=user,
            db=db,
        )

    assert response == {'checkout': {'id': 'owned-checkout'}}
    assert store.await_args.kwargs['response'] == {'checkout': {'id': 'owned-checkout'}}


@pytest.mark.asyncio
@pytest.mark.parametrize('payment_status', ['VERIFYING', 'CONFIRMED', 'FAILED', 'CANCELED', 'EXPIRED'])
async def test_direct_commit_never_exposes_a_non_live_provider_redirect(payment_status: str) -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(
        id=91,
        settlement_mode='direct_purchase_v2',
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    attempt = SimpleNamespace(platega_payment_id=51, status='pending')
    payment = SimpleNamespace(
        redirect_url='https://pay.example/unverified',
        is_paid=False,
        status=payment_status,
        expires_at=None,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=payment))

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.get_owned_checkout',
            AsyncMock(side_effect=[checkout, checkout]),
        ),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock(return_value=attempt)),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        with pytest.raises(HTTPException) as raised:
            await checkout_commit(
                'owned-checkout',
                CheckoutCommitRequest(funding_mode='platega', method_key='sbp'),
                idempotency_key='commit-unverified',
                user=user,
                db=db,
            )

    assert raised.value.detail['code'] == 'reconciliation_required'
    assert store.await_args.kwargs['response']['code'] == 'reconciliation_required'


def _fused_request(**overrides):
    payload = {
        'period_days': 30,
        'selected_device_limit': 2,
        'funding_mode': 'platega',
        'method_key': 'sbp',
        'expected_tariff_total_kopeks': 36_900,
    }
    return DirectCheckoutCommitRequest(**{**payload, **overrides})


@pytest.mark.asyncio
async def test_fused_commit_wallet_births_and_debits_only_at_pay_time() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=100_000)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(id=91, public_id='fused-checkout')
    resolved = SimpleNamespace(checkout=checkout, proceed_to_payment=True)
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ) as fused,
        patch(
            'app.cabinet.routes.device_first.commit_direct_wallet_checkout',
            AsyncMock(return_value=checkout),
        ) as commit,
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'fused-checkout'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await direct_checkout_commit(
            _fused_request(funding_mode='wallet', method_key=None),
            idempotency_key='pay:30:2:wallet:36900',
            user=user,
            db=db,
        )

    assert response == {'checkout': {'id': 'fused-checkout'}}
    assert fused.await_args.kwargs['funding_mode'] == 'wallet'
    assert fused.await_args.kwargs['expected_tariff_total_kopeks'] == 36_900
    assert fused.await_args.kwargs['source'] == 'cabinet'
    assert fused.await_args.kwargs['mutation'] is mutation
    commit.assert_awaited_once_with(db, public_id='fused-checkout', user_id=17)
    assert mutation.checkout_id == 91
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_fused_commit_wallet_rejects_a_provider_method() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch('app.cabinet.routes.device_first.create_or_resume_direct_checkout', AsyncMock()) as fused,
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        with pytest.raises(HTTPException) as raised:
            await direct_checkout_commit(
                _fused_request(funding_mode='wallet', method_key='sbp'),
                idempotency_key='pay:30:2:wallet:36900',
                user=user,
                db=db,
            )

    assert raised.value.status_code == 422
    assert raised.value.detail['code'] == 'invalid_funding_request'
    fused.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_commit_platega_returns_a_live_redirect_and_stores_it_redacted() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    checkout = SimpleNamespace(
        id=91,
        public_id='fused-checkout',
        settlement_mode='direct_purchase_v2',
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    resolved = SimpleNamespace(checkout=checkout, proceed_to_payment=True)
    attempt = SimpleNamespace(platega_payment_id=51, status='pending')
    payment = SimpleNamespace(
        redirect_url='https://pay.example/live',
        is_paid=False,
        status='PENDING',
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    db = SimpleNamespace(get=AsyncMock(return_value=payment), refresh=AsyncMock())

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ) as fused,
        patch(
            'app.cabinet.routes.device_first.create_platega_attempt',
            AsyncMock(return_value=attempt),
        ) as create_attempt,
        patch(
            'app.cabinet.routes.device_first.get_owned_checkout',
            AsyncMock(return_value=checkout),
        ),
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'fused-checkout'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        response = await direct_checkout_commit(
            _fused_request(),
            idempotency_key='pay:30:2:platega:sbp:36900',
            user=user,
            db=db,
        )

    assert response == {'checkout': {'id': 'fused-checkout'}, 'redirect_url': 'https://pay.example/live'}
    assert fused.await_args.kwargs['method_key'] == 'sbp'
    create_attempt.assert_awaited_once_with(db, checkout_public_id='fused-checkout', user_id=17, method_key='sbp')
    assert store.await_args.kwargs['response'] == {'checkout': {'id': 'fused-checkout'}}


@pytest.mark.asyncio
async def test_fused_commit_reprice_leaves_no_row_no_post_and_no_cancel() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    reprice = DeviceFirstError('reprice_required', 'The price changed')
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=reprice),
        ),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch('app.cabinet.routes.device_first.commit_direct_wallet_checkout', AsyncMock()) as commit,
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()) as store,
    ):
        with pytest.raises(HTTPException) as raised:
            await direct_checkout_commit(
                _fused_request(expected_tariff_total_kopeks=35_000),
                idempotency_key='pay:30:2:platega:sbp:35000',
                user=user,
                db=db,
            )

    assert raised.value.status_code == 409
    assert raised.value.detail['code'] == 'reprice_required'
    create_attempt.assert_not_awaited()
    commit.assert_not_awaited()
    assert store.await_args.kwargs['response']['code'] == 'reprice_required'
    assert store.await_args.kwargs['status_code'] == 409


@pytest.mark.asyncio
async def test_fused_commit_funding_locked_demands_the_explicit_abandon_screen() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=100_000)
    mutation = SimpleNamespace(checkout_id=None)
    locked = DeviceFirstError('funding_mode_locked', 'Funding method is already fixed')
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.create_or_resume_direct_checkout',
            AsyncMock(side_effect=locked),
        ),
        patch('app.cabinet.routes.device_first.commit_direct_wallet_checkout', AsyncMock()) as commit,
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        with pytest.raises(HTTPException) as raised:
            await direct_checkout_commit(
                _fused_request(funding_mode='wallet', method_key=None),
                idempotency_key='pay:30:2:wallet:36900',
                user=user,
                db=db,
            )

    assert raised.value.status_code == 409
    assert raised.value.detail['code'] == 'funding_mode_locked'
    commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_commit_resume_of_a_paid_race_returns_the_canonical_checkout() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    mutation = SimpleNamespace(checkout_id=None)
    settled = SimpleNamespace(id=91, public_id='settled-checkout')
    resolved = SimpleNamespace(checkout=settled, proceed_to_payment=False)
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(mutation, None))),
        patch(
            'app.cabinet.routes.device_first.create_or_resume_direct_checkout',
            AsyncMock(return_value=resolved),
        ),
        patch('app.cabinet.routes.device_first.create_platega_attempt', AsyncMock()) as create_attempt,
        patch(
            'app.cabinet.routes.device_first._serialize_cabinet_checkout',
            AsyncMock(return_value={'id': 'settled-checkout', 'ui_state': 'processing'}),
        ),
        patch('app.cabinet.routes.device_first.store_mutation_result', AsyncMock()),
    ):
        response = await direct_checkout_commit(
            _fused_request(),
            idempotency_key='pay:30:2:platega:sbp:36900',
            user=user,
            db=db,
        )

    assert response == {'checkout': {'id': 'settled-checkout', 'ui_state': 'processing'}}
    create_attempt.assert_not_awaited()
    assert mutation.checkout_id == 91


@pytest.mark.asyncio
async def test_fused_commit_replay_rehydrates_the_owned_redirect() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=0)
    replay = {'checkout': {'id': 'fused-checkout'}}
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.device_first._rate_limit', AsyncMock()),
        patch('app.cabinet.routes.device_first._mutation', AsyncMock(return_value=(SimpleNamespace(), replay))),
        patch(
            'app.cabinet.routes.device_first._rehydrate_owned_direct_redirect',
            AsyncMock(return_value={'checkout': {'id': 'fused-checkout'}, 'redirect_url': 'https://pay.example/live'}),
        ) as rehydrate,
        patch('app.cabinet.routes.device_first.create_or_resume_direct_checkout', AsyncMock()) as fused,
    ):
        response = await direct_checkout_commit(
            _fused_request(),
            idempotency_key='pay:30:2:platega:sbp:36900',
            user=user,
            db=db,
        )

    assert response['redirect_url'] == 'https://pay.example/live'
    rehydrate.assert_awaited_once()
    fused.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_native_launch_delegates_with_its_own_action_and_never_debits_wallet() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=100_000, telegram_id=7001)
    db = AsyncMock()
    expected = {'checkout': {'id': 'fused-checkout'}, 'redirect_url': 'https://pay.example/live'}

    with patch(
        'app.cabinet.routes.device_first._fused_commit_checkout', AsyncMock(return_value=expected)
    ) as fused_commit:
        response = await direct_native_launch(
            _fused_request(),
            idempotency_key='native-pay:30:2:platega:sbp:36900',
            user=user,
            db=db,
        )

    assert response == expected
    assert fused_commit.await_args.kwargs['action'] == 'fused_native_launch'
    assert fused_commit.await_args.kwargs['request'].funding_mode == 'platega'


@pytest.mark.asyncio
async def test_fused_native_launch_rejects_wallet_funding_before_any_mutation() -> None:
    user = SimpleNamespace(id=17, balance_kopeks=100_000, telegram_id=7001)
    db = AsyncMock()

    with patch('app.cabinet.routes.device_first._fused_commit_checkout', AsyncMock()) as fused_commit:
        with pytest.raises(HTTPException) as raised:
            await direct_native_launch(
                _fused_request(funding_mode='wallet', method_key=None),
                idempotency_key='native-pay:30:2:wallet:36900',
                user=user,
                db=db,
            )

    assert raised.value.status_code == 422
    assert raised.value.detail['code'] == 'invalid_funding_request'
    fused_commit.assert_not_awaited()

"""16в-3: точное согласие на повтор покупки, replay и реальное время денег.

SQL-выборки исполняются на SQLite. Блокировки SQLite не доказывает: порядок
финальной проверки и денег проверяется на существующей границе User lock.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.cabinet.routes import balance as balance_route, device_first as route
from app.cabinet.schemas.device_first import DirectCheckoutCommitRequest
from app.database.models import SubscriptionCheckout, Transaction
from app.services import device_first_checkout_service as dfc, device_first_payment_service as payments
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401 -- настоящий SQL-стенд
    _intent_payment,
    _options,
    _purchase,
    _user,
    db,
    session,
)
from tests.cabinet.test_vk16_topup_screen_contract import _replaced_then_paid


def _sale(session, *, transaction_id=901, user_id=1, at=None, kind='subscription_payment'):
    at = at or datetime.now(UTC) - timedelta(minutes=2)
    session.add(Transaction(id=transaction_id, user_id=user_id, type=kind, amount_kopeks=14_900, created_at=at))
    session.commit()
    return at


def _request(**changes):
    return DirectCheckoutCommitRequest(
        **{
            'period_days': 30,
            'selected_device_limit': 1,
            'funding_mode': 'platega',
            'method_key': 'cards_ru',
            'expected_tariff_total_kopeks': 14_900,
            'purchase_context': 'chat_autostart',
            **changes,
        }
    )


async def test_options_expose_real_latest_purchase_identity_and_ignore_an_unpaid_invoice(db, session, monkeypatch):
    _purchase(session, checkout_public_id='unpaid-card', minutes_ago=2, transaction=False, funding_mode='platega')
    monkeypatch.setattr(route, 'build_purchase_options', AsyncMock(return_value=_options()))
    assert (await route.purchase_options(user=_user(session), db=db))['recent_purchase'] is None
    at = _sale(session)
    _sale(session, transaction_id=902, kind='deposit', at=at + timedelta(seconds=1))
    _sale(session, transaction_id=903, user_id=2, at=at + timedelta(seconds=2))
    _sale(session, transaction_id=904, at=datetime.now(UTC) - timedelta(hours=2))
    result = await route.purchase_options(user=_user(session), db=db)
    assert result['recent_purchase'] == {
        'transaction_id': 901,
        'purchased_at': at.isoformat(),
        'checkout_public_id': None,
    }


async def test_purchase_identity_includes_its_owned_checkout(db, session):
    _purchase(session, checkout_public_id='paid-owned', minutes_ago=2)
    summary = await dfc.recent_purchase_summary(db, user_id=1)
    assert summary['checkout_public_id'] == 'paid-owned'
    assert summary['transaction_id'] == session.scalar(select(Transaction.id))


async def test_yes_is_specific_to_a_transaction_even_when_another_purchase_has_the_same_timestamp(db, session):
    at = _sale(session)
    await dfc.assert_recent_purchase_confirmed(
        db, user_id=1, purchase_context='chat_autostart', confirmed_purchase_id=901
    )
    _sale(session, transaction_id=905, at=at)
    with pytest.raises(dfc.DeviceFirstError) as error:
        await dfc.assert_recent_purchase_confirmed(
            db, user_id=1, purchase_context='chat_autostart', confirmed_purchase_id=901
        )
    assert error.value.code == 'recent_purchase_confirmation_required'
    assert error.value.detail['recent_purchase']['transaction_id'] == 905


async def test_legacy_create_opt_in_is_checked_under_its_user_lock_before_inserting(db, session, monkeypatch):
    _sale(session)
    monkeypatch.setattr(route, '_rate_limit', AsyncMock())
    monkeypatch.setattr(route, '_mutation', AsyncMock(return_value=(SimpleNamespace(checkout_id=None), None)))
    monkeypatch.setattr(route, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(route, 'store_mutation_result', AsyncMock())
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    with pytest.raises(HTTPException) as error:
        await route.checkout_create(
            route.CheckoutCreateRequest(period_days=30, selected_device_limit=1, purchase_context='chat_autostart'),
            idempotency_key='legacy-create',
            user=_user(session),
            db=db,
        )
    assert error.value.detail['recent_purchase']['transaction_id'] == 901
    assert session.scalar(select(SubscriptionCheckout.id)) is None


async def test_confirmation_allows_the_guarded_money_path_to_reach_normal_validation(db, session, monkeypatch):
    _sale(session)
    monkeypatch.setattr(
        dfc,
        '_lock_direct_context',
        AsyncMock(return_value=(SimpleNamespace(financial_committed_at=None), _user(session), None, None)),
    )
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(dfc, 'is_device_first_canary_user', lambda _: True)
    validate = AsyncMock(side_effect=RuntimeError('normal validation'))
    monkeypatch.setattr(dfc, '_validate_direct_pre_commit', validate)
    with pytest.raises(RuntimeError, match='normal validation'):
        await dfc.commit_direct_wallet_checkout(
            db, public_id='quote', user_id=1, purchase_context='chat_autostart', confirmed_purchase_id=901
        )
    validate.assert_awaited_once()


@pytest.mark.parametrize('function', [dfc.prepare_direct_external_checkout, dfc.commit_direct_wallet_checkout])
async def test_a_purchase_that_arrives_at_the_user_lock_blocks_money_before_validation(
    db, session, monkeypatch, function
):
    events = []
    checkout = SimpleNamespace(financial_committed_at=None)

    async def lock(*args, **kwargs):
        events.append('user_lock')
        _sale(session)
        return checkout, _user(session), None, SimpleNamespace()

    monkeypatch.setattr(dfc, '_lock_direct_context', lock)
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(dfc, 'is_device_first_canary_user', lambda _: True)
    validate = AsyncMock(side_effect=AssertionError('money validation happened'))
    monkeypatch.setattr(dfc, '_validate_direct_pre_commit', validate)
    with pytest.raises(dfc.DeviceFirstError) as error:
        await function(db, public_id='quote', user_id=1, purchase_context='chat_autostart')
    assert events == ['user_lock']
    assert error.value.detail['recent_purchase']['transaction_id'] == 901
    assert checkout.financial_committed_at is None
    validate.assert_not_awaited()


@pytest.mark.parametrize('funding', ['wallet', 'platega'])
async def test_financially_committed_repeat_returns_the_same_checkout_before_guard(monkeypatch, funding):
    checkout = SimpleNamespace(
        financial_committed_at=datetime.now(UTC),
        funding_mode=funding,
        lifecycle_state='awaiting_funds',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        created_subscription_id=None,
        debit_transaction_id=None,
    )
    monkeypatch.setattr(dfc, '_lock_direct_context', AsyncMock(return_value=(checkout, None, None, None)))
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(dfc, 'is_device_first_canary_user', lambda _: True)
    summary = AsyncMock(side_effect=AssertionError('repeat asked a fresh purchase question'))
    monkeypatch.setattr(dfc, 'recent_purchase_summary', summary)
    function = dfc.commit_direct_wallet_checkout if funding == 'wallet' else dfc.prepare_direct_external_checkout
    assert await function(SimpleNamespace(), public_id='same', user_id=1, purchase_context='chat_autostart') is checkout
    summary.assert_not_awaited()


@pytest.mark.parametrize('function', [dfc.prepare_direct_external_checkout, dfc.commit_direct_wallet_checkout])
async def test_old_client_without_context_keeps_its_normal_payment_path(monkeypatch, function):
    checkout = SimpleNamespace(financial_committed_at=None)
    monkeypatch.setattr(dfc, '_lock_direct_context', AsyncMock(return_value=(checkout, None, None, None)))
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    monkeypatch.setattr(dfc, 'is_device_first_canary_user', lambda _: True)
    monkeypatch.setattr(dfc, 'recent_purchase_summary', AsyncMock(side_effect=AssertionError('old client was guarded')))
    sentinel = RuntimeError('reached normal validation')
    monkeypatch.setattr(dfc, '_validate_direct_pre_commit', AsyncMock(side_effect=sentinel))
    with pytest.raises(RuntimeError, match='reached normal validation'):
        await function(SimpleNamespace(), public_id='old-client', user_id=1)


@pytest.mark.parametrize('endpoint', [route.direct_native_launch, route.direct_checkout_commit])
async def test_fused_opt_in_rejects_before_business_rows_and_returns_the_current_identity(
    db, session, monkeypatch, endpoint
):
    _sale(session)
    monkeypatch.setattr(route, '_rate_limit', AsyncMock())
    monkeypatch.setattr(route, '_mutation', AsyncMock(return_value=(SimpleNamespace(checkout_id=None), None)))
    store = AsyncMock()
    monkeypatch.setattr(route, 'store_mutation_result', store)
    monkeypatch.setattr(dfc, 'build_purchase_options', AsyncMock(return_value=_options()))
    monkeypatch.setattr(dfc, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    create = AsyncMock(side_effect=AssertionError('unexpected checkout'))
    monkeypatch.setattr(dfc, 'create_checkout', create)
    with pytest.raises(HTTPException) as error:
        await endpoint(_request(), idempotency_key='one', user=_user(session), db=db)
    assert error.value.status_code == 409
    assert error.value.detail['recent_purchase']['transaction_id'] == 901
    assert error.value.detail['code'] == 'recent_purchase_confirmation_required'
    assert store.await_args.kwargs['response'] == error.value.detail
    assert session.scalar(select(SubscriptionCheckout.id)) is None
    create.assert_not_awaited()


@pytest.mark.parametrize('endpoint', [route.direct_native_launch, route.direct_checkout_commit])
async def test_cached_replay_is_returned_before_resolver_or_purchase_guard(monkeypatch, endpoint):
    response = {'checkout': {'id': 'already-owned'}}
    monkeypatch.setattr(route, '_rate_limit', AsyncMock())
    monkeypatch.setattr(route, '_mutation', AsyncMock(return_value=(SimpleNamespace(checkout_id=41), response)))
    monkeypatch.setattr(route, '_rehydrate_owned_direct_redirect', AsyncMock(return_value=response))
    resolver = AsyncMock(side_effect=AssertionError('replay created another order'))
    monkeypatch.setattr(route, 'create_or_resume_direct_checkout', resolver)
    result = await endpoint(_request(), idempotency_key='same', user=SimpleNamespace(id=1), db=AsyncMock())
    assert result == response
    resolver.assert_not_awaited()


async def test_missing_http_response_recovers_its_committed_checkout_before_guard(monkeypatch):
    bound = SimpleNamespace(id=41, user_id=1, financial_committed_at=datetime.now(UTC))
    response = {'checkout': {'id': 'paid-owned', 'ui_state': 'ready'}}
    db = SimpleNamespace(get=AsyncMock(return_value=bound))
    monkeypatch.setattr(route, '_rate_limit', AsyncMock())
    monkeypatch.setattr(route, '_mutation', AsyncMock(return_value=(SimpleNamespace(checkout_id=41), None)))
    monkeypatch.setattr(route, '_serialize_cabinet_checkout', AsyncMock(return_value=response['checkout']))
    monkeypatch.setattr(route, '_rehydrate_owned_direct_redirect', AsyncMock(return_value=response))
    monkeypatch.setattr(route, 'store_mutation_result', AsyncMock())
    resolver = AsyncMock(side_effect=AssertionError('recovery created a new order'))
    monkeypatch.setattr(route, 'create_or_resume_direct_checkout', resolver)
    assert (
        await route.direct_native_launch(
            _request(), idempotency_key='same', user=SimpleNamespace(id=1, balance_kopeks=0), db=db
        )
        == response
    )
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    'payload_model',
    [
        route.CheckoutCreateRequest(period_days=30, selected_device_limit=1),
        route.CheckoutCommitRequest(funding_mode='platega', method_key='cards_ru'),
        route.NativeCheckoutLaunchRequest(method_key='cards_ru'),
        _request(purchase_context=None),
    ],
)
def test_optional_fields_keep_pre_deploy_idempotency_payloads(payload_model):
    old = payload_model.model_dump(exclude={'purchase_context', 'confirmed_purchase_id'})
    assert dfc.request_hash(route._purchase_payload(payload_model)) == dfc.request_hash(old)


async def test_legacy_native_forwards_opt_in_identity_without_changing_funding(monkeypatch):
    commit = AsyncMock(return_value={'checkout': {'id': 'same'}})
    monkeypatch.setattr(route, '_commit_checkout', commit)
    await route.checkout_native_launch(
        'same',
        route.NativeCheckoutLaunchRequest(
            method_key='cards_ru', purchase_context='chat_autostart', confirmed_purchase_id=901
        ),
        idempotency_key='native',
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    forwarded = commit.await_args.kwargs['request']
    assert (forwarded.funding_mode, forwarded.purchase_context, forwarded.confirmed_purchase_id) == (
        'platega',
        'chat_autostart',
        901,
    )


async def test_provider_chain_forwards_guard_to_the_existing_money_lock(monkeypatch):
    monkeypatch.setattr(payments, 'available_platega_methods_for_db', AsyncMock(return_value=[{'provider_code': 11}]))
    monkeypatch.setattr(
        payments,
        'get_owned_checkout',
        AsyncMock(
            return_value=SimpleNamespace(settlement_mode=dfc.DIRECT_SETTLEMENT_MODE, financial_committed_at=None)
        ),
    )
    monkeypatch.setattr(payments, '_direct_checkout_return_url', lambda *args, **kwargs: 'https://cabinet.test/return')
    sentinel = dfc.DeviceFirstError('recent_purchase_confirmation_required', 'stop')
    prepare = AsyncMock(side_effect=sentinel)
    monkeypatch.setattr(payments, 'prepare_direct_external_checkout', prepare)
    with pytest.raises(dfc.DeviceFirstError):
        await payments.create_platega_attempt(
            AsyncMock(),
            checkout_public_id='same',
            user_id=1,
            method_key='cards_ru',
            purchase_context='chat_autostart',
            confirmed_purchase_id=901,
        )
    assert prepare.await_args.kwargs['purchase_context'] == 'chat_autostart'
    assert prepare.await_args.kwargs['confirmed_purchase_id'] == 901


@pytest.mark.parametrize('entry', ['id', 'latest', 'check'])
async def test_money_timestamp_is_completed_at_for_the_paid_predecessor_in_every_single_response(
    db, session, entry, monkeypatch
):
    _replaced_then_paid(session, decided_at=(datetime.now(UTC) + timedelta(days=2)).isoformat())
    expected = session.get(Transaction, 970).completed_at.replace(tzinfo=UTC)
    if entry == 'id':
        result = await balance_route.get_pending_payment_details('platega', 71, user=_user(session), db=db)
    elif entry == 'latest':
        result = await balance_route.get_latest_payment_by_method('platega', user=_user(session), db=db)
    else:
        monkeypatch.setattr(balance_route, '_is_checkable', lambda _: False)
        result = (await balance_route.check_payment_status('platega', 71, user=_user(session), db=db)).payment
    assert result.intent_payment_id == 70
    assert result.intent_paid_at == expected


async def test_decision_after_new_invoice_does_not_relabel_money_that_arrived_before_it(db, session):
    _replaced_then_paid(
        session, paid_ago=timedelta(minutes=8), decided_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    )
    response = await balance_route.get_pending_payment_details('platega', 71, user=_user(session), db=db)
    assert (response.intent_payment_id, response.intent_paid_at) == (71, None)


@pytest.mark.parametrize('deposit', [False, True])
async def test_unknown_completion_time_stays_null_without_substitute_dates(db, session, deposit):
    if deposit:
        session.add(Transaction(id=950, user_id=1, type='deposit', created_at=datetime.now(UTC), completed_at=None))
    _intent_payment(
        session,
        payment_id=50,
        status='fulfilled',
        is_paid=True,
        transaction_id=950 if deposit else None,
        decided_at=datetime.now(UTC).isoformat(),
    )
    response = await balance_route.get_pending_payment_details('platega', 50, user=_user(session), db=db)
    assert response.intent_paid_at is None

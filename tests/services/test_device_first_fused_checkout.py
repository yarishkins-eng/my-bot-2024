"""Fused pay-time checkout resolver: birth/resume/supersede only at «Pay»."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import device_first_checkout_service as service, device_first_payment_service as payment_service
from app.services.public_location_entitlement_service import ResolvedEntitlement


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


def _user(*, balance_kopeks: int = 0):
    return SimpleNamespace(id=7, balance_kopeks=balance_kopeks)


def _options(*, price_kopeks: int = 36_900, period_days: int = 30, device_limit: int = 2):
    return {
        'eligible': True,
        'tariff': {'id': 7, 'name': 'Базовый'},
        'period_options': [period_days],
        'device_options': [device_limit],
        'price_matrix': [
            {'period_days': period_days, 'prices': [{'device_limit': device_limit, 'price_kopeks': price_kopeks}]}
        ],
        'current_subscription': {},
    }


def _checkout(**overrides):
    base = {
        'id': 51,
        'public_id': 'open-checkout',
        'user_id': 7,
        'settlement_mode': 'direct_purchase_v2',
        'lifecycle_state': 'confirmed',
        'fulfillment_state': 'not_started',
        'period_days': 30,
        'selected_device_limit': 2,
        'financial_committed_at': None,
        'funding_mode': None,
    }
    return SimpleNamespace(**{**base, **overrides})


def _access_point_sale_checkout(*, entitlement: ResolvedEntitlement):
    checkout = _checkout(
        tariff_id=7,
        tariff_total_kopeks=36_900,
        wallet_applied_kopeks=0,
        external_payable_kopeks=36_900,
        funding_mode='platega',
        expect_no_subscription=True,
        target_snapshot={},
        price_breakdown={},
        pricing_revision=3,
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    tariff = SimpleNamespace(id=7, name='AP tariff', traffic_limit_gb=100)
    checkout.sale_snapshot = service._direct_sale_snapshot(
        checkout,
        tariff,
        funding_mode='platega',
        entitlement=entitlement,
    )
    return checkout, tariff


class _Savepoint:
    def __init__(self):
        self.exit_exception = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, _traceback):
        self.exit_exception = exc
        return False


def _db_with_user(user):
    return SimpleNamespace(execute=AsyncMock(side_effect=[ScalarResult(user)]), commit=AsyncMock())


def _patch_options(monkeypatch, options):
    monkeypatch.setattr(service, 'build_purchase_options', AsyncMock(return_value=options))


@pytest.mark.asyncio
async def test_fused_birth_creates_one_confirmed_checkout_with_matching_price(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    created = _checkout(public_id='fused-checkout', lifecycle_state='confirmed')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='telegram',
    )

    assert resolved.checkout is created
    assert resolved.proceed_to_payment is True
    assert create.await_args.kwargs['initial_lifecycle_state'] == 'confirmed'
    assert create.await_args.kwargs['source'] == 'telegram'


@pytest.mark.asyncio
async def test_access_point_policy_drift_forces_requote_before_any_funding_transition(monkeypatch):
    quoted = ResolvedEntitlement(('point-pl',), ('squad-pl',), 4, 'access_point_policy', 'fp-before')
    checkout = _checkout(
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        expect_no_subscription=True,
        target_snapshot={},
        pricing_revision=7,
        tariff_total_kopeks=36_900,
        period_days=30,
        selected_device_limit=2,
        quote_state='valid',
        terminal_reason=None,
        entitlement_quote_snapshot=service._entitlement_quote_snapshot(quoted),
    )
    user = _user()
    tariff = SimpleNamespace(id=7, pricing_revision=7)
    # Final quote validation must take a real tariff-row lock before it checks
    # the access-point evidence.  The fake returns the locked, current row.
    db = SimpleNamespace(commit=AsyncMock(), scalar=AsyncMock(return_value=tariff))
    monkeypatch.setattr(service, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda *_args, **_kwargs: SimpleNamespace(eligible=True, period_options=(30,), device_options=(2,)),
    )
    monkeypatch.setattr(
        service.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=36_900)),
    )
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement(('point-pl',), ('squad-pl',), 5, 'access_point_policy', 'fp-after')),
    )

    result = await service._validate_direct_pre_commit(
        db,
        checkout=checkout,
        user=user,
        target=None,
        tariff=tariff,
    )

    assert result is None
    assert checkout.lifecycle_state == 'reprice_required'
    assert checkout.terminal_reason == 'entitlement_changed'
    db.commit.assert_awaited_once()
    resolver = __import__(
        'app.services.public_location_entitlement_service',
        fromlist=['resolve_tariff_entitlement'],
    ).resolve_tariff_entitlement
    assert resolver.await_args.kwargs['lock_access_point_evidence'] is True


@pytest.mark.asyncio
async def test_paid_access_point_drift_enters_operator_review_before_term_or_ledger_mutation(monkeypatch):
    quoted = ResolvedEntitlement(('point-pl',), ('squad-pl',), 4, 'access_point_policy', 'fp-before')
    changed = ResolvedEntitlement(('point-pl',), ('squad-pl',), 5, 'access_point_policy', 'fp-after')
    checkout, tariff = _access_point_sale_checkout(entitlement=quoted)
    user = _user()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=tariff),
        execute=AsyncMock(),
        add=Mock(),
    )
    monkeypatch.setattr(service, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=changed),
    )
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_paid_subscription', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service._complete_direct_sale_locked(
            db,
            checkout=checkout,
            user=user,
            target=None,
            provider_payment_id='provider-paid-id',
        )

    assert raised.value.code == 'operator_review_required'
    assert checkout.lifecycle_state == 'operator_review'
    assert checkout.terminal_reason == 'captured_entitlement_changed_after_payment'
    create.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_access_point_term_capture_failure_rolls_back_before_ledger_mutation(monkeypatch):
    quoted = ResolvedEntitlement(('point-pl',), ('squad-pl',), 4, 'access_point_policy', 'fp-before')
    checkout, tariff = _access_point_sale_checkout(entitlement=quoted)
    user = _user()
    savepoint = _Savepoint()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=tariff),
        execute=AsyncMock(),
        add=Mock(),
        begin_nested=Mock(return_value=savepoint),
    )
    monkeypatch.setattr(service, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=quoted),
    )
    create = AsyncMock(side_effect=ValueError('captured term source conflict'))
    monkeypatch.setattr(service, 'create_paid_subscription', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service._complete_direct_sale_locked(
            db,
            checkout=checkout,
            user=user,
            target=None,
            provider_payment_id='provider-paid-id',
        )

    assert raised.value.code == 'operator_review_required'
    assert checkout.lifecycle_state == 'operator_review'
    assert checkout.terminal_reason == 'captured_entitlement_term_unavailable'
    assert isinstance(savepoint.exit_exception, ValueError)
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_fused_stale_expected_price_is_rejected_before_any_insert(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    _patch_options(monkeypatch, _options(price_kopeks=39_900))
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reprice_required'
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_same_config_resume_ignores_the_expected_price(monkeypatch):
    """The immutable quote/invoice owns its approved price; stale tokens resume."""
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(lifecycle_state='confirmed')
    _patch_options(monkeypatch, _options(price_kopeks=41_000))
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    get_owned = AsyncMock(return_value=existing)
    monkeypatch.setattr(service, 'get_owned_checkout', get_owned)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)
    confirm = AsyncMock()
    monkeypatch.setattr(service, 'confirm_checkout', confirm)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is existing
    assert resolved.proceed_to_payment is True
    # The optimistic resume must not hold a Checkout row lock: the commit
    # chain takes User -> Checkout itself, and a held C lock inverts it.
    assert get_owned.await_args.kwargs['for_update'] is False
    confirm.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_same_config_legacy_draft_is_confirmed_before_payment(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    draft = _checkout(lifecycle_state='draft')
    confirmed = _checkout(lifecycle_state='confirmed')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=draft))
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=draft))
    confirm = AsyncMock(return_value=confirmed)
    monkeypatch.setattr(service, 'confirm_checkout', confirm)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    confirm.assert_awaited_once_with(db, draft)
    assert resolved.checkout is confirmed
    assert resolved.proceed_to_payment is True
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_resume_of_an_expired_legacy_draft_gets_the_reprice_flow(monkeypatch):
    """A quote whose 30-minute TTL died comes back from confirm as reprice."""
    user = _user()
    db = _db_with_user(user)
    draft = _checkout(lifecycle_state='draft')
    expired = _checkout(lifecycle_state='reprice_required')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=draft))
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=draft))
    monkeypatch.setattr(service, 'confirm_checkout', AsyncMock(return_value=expired))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reprice_required'
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_resume_of_a_pre_commit_crash_row_gets_the_reprice_flow(monkeypatch):
    """awaiting_funds without a financial commit can never pass pre-commit."""
    user = _user()
    db = _db_with_user(user)
    crash_row = _checkout(lifecycle_state='awaiting_funds', financial_committed_at=None)
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=crash_row))
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=crash_row))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reprice_required'
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_wallet_click_over_a_live_invoice_is_funding_locked_never_superseded(monkeypatch):
    user = _user(balance_kopeks=100_000)
    db = _db_with_user(user)
    existing = _checkout(
        lifecycle_state='awaiting_funds',
        financial_committed_at=datetime.now(UTC),
        funding_mode='platega',
    )
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=existing))
    abandon = AsyncMock()
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='wallet',
            method_key=None,
            source='cabinet',
        )

    assert raised.value.code == 'funding_mode_locked'
    abandon.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_state', ['armed', 'fulfilling', 'operator_review'])
async def test_fused_same_config_paid_or_held_checkout_is_its_own_canonical_answer(monkeypatch, lifecycle_state):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(lifecycle_state=lifecycle_state)
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=existing))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is existing
    assert resolved.proceed_to_payment is False
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_different_config_supersedes_through_the_whole_abandon_path(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4)
    abandoned = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='cancelled')
    created = _checkout(public_id='fused-checkout')
    _patch_options(monkeypatch, _options())
    # Phase 3 reads the old invoice; phase 4 re-reads under the fence after
    # the abandon and must find nothing open.
    monkeypatch.setattr(
        service,
        'get_open_checkout_for_user',
        AsyncMock(side_effect=[existing, None]),
    )
    abandon = AsyncMock(return_value=abandoned)
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    abandon.assert_awaited_once_with(db, checkout_public_id='open-checkout', user_id=7)
    assert resolved.checkout is created
    assert resolved.proceed_to_payment is True
    assert create.await_args.kwargs['initial_lifecycle_state'] == 'confirmed'


@pytest.mark.asyncio
async def test_fused_different_config_without_an_invoice_cancels_the_disposable_quote(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='draft')
    cancelled_quote = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='cancelled')
    created = _checkout(public_id='fused-checkout')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(
        service,
        'get_open_checkout_for_user',
        AsyncMock(side_effect=[existing, None]),
    )
    abandon = AsyncMock(return_value=None)
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    get_owned = AsyncMock(return_value=existing)
    monkeypatch.setattr(service, 'get_owned_checkout', get_owned)
    cancel_quote = AsyncMock(return_value=cancelled_quote)
    monkeypatch.setattr(service, 'cancel_checkout_for_new_calculation', cancel_quote)
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    get_owned.assert_awaited_once_with(db, public_id='open-checkout', user_id=7, for_update=True)
    cancel_quote.assert_awaited_once_with(db, existing)
    assert resolved.checkout is created


@pytest.mark.asyncio
async def test_fused_cancel_refusal_never_falls_through_to_a_second_insert(monkeypatch):
    """A recovery transition winning the row must answer, not IntegrityError."""
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='draft')
    recovered = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='fulfilling')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    abandon = AsyncMock(return_value=None)
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    monkeypatch.setattr(service, 'get_owned_checkout', AsyncMock(return_value=existing))
    monkeypatch.setattr(service, 'cancel_checkout_for_new_calculation', AsyncMock(return_value=recovered))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is recovered
    assert resolved.proceed_to_payment is False
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_different_config_keeps_the_checkout_when_abandon_loses_the_money_race(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4)
    settled = _checkout(period_days=90, selected_device_limit=4, lifecycle_state='fulfilling')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    abandon = AsyncMock(return_value=settled)
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is settled
    assert resolved.proceed_to_payment is False
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_different_config_propagates_the_abandon_reconciliation_refusal(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4)
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    abandon = AsyncMock(side_effect=service.DeviceFirstError('reconciliation_required', 'Ambiguous invoice'))
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reconciliation_required'
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_different_config_with_a_stale_price_never_cancels_the_live_invoice(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    existing = _checkout(period_days=90, selected_device_limit=4)
    _patch_options(monkeypatch, _options(price_kopeks=41_000))
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=existing))
    abandon = AsyncMock()
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reprice_required'
    abandon.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_wallet_birth_with_insufficient_balance_leaves_no_row(monkeypatch):
    user = _user(balance_kopeks=10_000)
    db = _db_with_user(user)
    _patch_options(monkeypatch, _options())
    get_open = AsyncMock(return_value=None)
    monkeypatch.setattr(service, 'get_open_checkout_for_user', get_open)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='wallet',
            method_key=None,
            source='telegram',
        )

    assert raised.value.code == 'wallet_insufficient'
    assert raised.value.status_code == 422
    # The balance pre-check runs before any checkout row is even read.
    get_open.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_wallet_insufficient_never_abandons_a_live_invoice(monkeypatch):
    """The funding pre-check precedes the supersede branch entirely."""
    user = _user(balance_kopeks=10_000)
    db = _db_with_user(user)
    live_invoice = _checkout(
        period_days=90,
        selected_device_limit=4,
        lifecycle_state='awaiting_funds',
        financial_committed_at=datetime.now(UTC),
        funding_mode='platega',
    )
    _patch_options(monkeypatch, _options())
    get_open = AsyncMock(return_value=live_invoice)
    monkeypatch.setattr(service, 'get_open_checkout_for_user', get_open)
    abandon = AsyncMock()
    monkeypatch.setattr(payment_service, 'abandon_direct_checkout_for_new_calculation', abandon)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='wallet',
            method_key=None,
            source='cabinet',
        )

    assert raised.value.code == 'wallet_insufficient'
    get_open.assert_not_awaited()
    abandon.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_platega_out_of_range_price_is_rejected_before_any_row(monkeypatch):
    """An amount Platega can never invoice must not become a zombie checkout."""
    user = _user()
    db = _db_with_user(user)
    _patch_options(monkeypatch, _options(price_kopeks=5_000))
    monkeypatch.setattr(service.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 10_000)
    monkeypatch.setattr(service.settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 100_000_000)
    get_open = AsyncMock(return_value=None)
    monkeypatch.setattr(service, 'get_open_checkout_for_user', get_open)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=5_000,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'provider_amount_out_of_range'
    assert raised.value.status_code == 422
    get_open.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_wallet_birth_with_a_covering_balance_reaches_creation(monkeypatch):
    user = _user(balance_kopeks=100_000)
    db = _db_with_user(user)
    created = _checkout(public_id='fused-checkout')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='wallet',
        method_key=None,
        source='telegram',
    )

    assert resolved.checkout is created
    assert resolved.proceed_to_payment is True


@pytest.mark.asyncio
async def test_fused_fenced_race_resumes_the_concurrent_same_config_winner(monkeypatch):
    """The authoritative re-read under the fence catches a winner's INSERT."""
    user = _user()
    db = _db_with_user(user)
    winner = _checkout(public_id='winner-checkout', lifecycle_state='confirmed')
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(
        service,
        'get_open_checkout_for_user',
        AsyncMock(side_effect=[None, winner]),
    )
    get_owned = AsyncMock(return_value=winner)
    monkeypatch.setattr(service, 'get_owned_checkout', get_owned)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is winner
    assert resolved.proceed_to_payment is True
    # The fence is held here, so the row lock follows the canonical U -> C.
    assert get_owned.await_args.kwargs['for_update'] is True
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_fenced_race_with_a_different_config_answers_canonically(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    other = _checkout(public_id='other-checkout', period_days=90, selected_device_limit=4)
    _patch_options(monkeypatch, _options())
    monkeypatch.setattr(
        service,
        'get_open_checkout_for_user',
        AsyncMock(side_effect=[None, other]),
    )
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    resolved = await service.create_or_resume_direct_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        expected_tariff_total_kopeks=36_900,
        funding_mode='platega',
        method_key='sbp',
        source='cabinet',
    )

    assert resolved.checkout is other
    assert resolved.proceed_to_payment is False
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_fenced_reprice_when_the_price_drifts_under_the_lock(monkeypatch):
    """The authoritative price under the fence is the one that is compared."""
    user = _user()
    db = _db_with_user(user)
    monkeypatch.setattr(
        service,
        'build_purchase_options',
        AsyncMock(side_effect=[_options(price_kopeks=36_900), _options(price_kopeks=39_900)]),
    )
    monkeypatch.setattr(service, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'reprice_required'
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_rejects_an_unsupported_selection_before_any_money_step(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    _patch_options(monkeypatch, _options())
    get_open = AsyncMock(return_value=None)
    monkeypatch.setattr(service, 'get_open_checkout_for_user', get_open)
    create = AsyncMock()
    monkeypatch.setattr(service, 'create_checkout', create)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=9,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'invalid_selection'
    assert raised.value.status_code == 422
    get_open.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_fused_ineligible_user_gets_the_legacy_only_answer(monkeypatch):
    user = _user()
    db = _db_with_user(user)
    _patch_options(monkeypatch, {'eligible': False, 'reason': 'feature_disabled'})
    get_open = AsyncMock()
    monkeypatch.setattr(service, 'get_open_checkout_for_user', get_open)

    with pytest.raises(service.DeviceFirstError) as raised:
        await service.create_or_resume_direct_checkout(
            db,
            user=user,
            period_days=30,
            selected_device_limit=2,
            expected_tariff_total_kopeks=36_900,
            funding_mode='platega',
            method_key='sbp',
            source='cabinet',
        )

    assert raised.value.code == 'legacy_only'
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_fused_birth_carries_a_thirty_minute_quote_expiry_and_confirmed_birth(monkeypatch):
    """The fused row is born confirmed with the post-invoice price fence intact."""
    user = SimpleNamespace(id=7, restriction_subscription=False, balance_kopeks=0)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ScalarResult(user),
                SimpleNamespace(),
                SimpleNamespace(scalar_one_or_none=lambda: None),
                ScalarResult(None),
                SimpleNamespace(scalars=list),
            ]
        ),
        commit=AsyncMock(),
        get=AsyncMock(return_value=SimpleNamespace(id=7, pricing_revision=1)),
        add=lambda *_args, **_kwargs: None,
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(
        service,
        'build_purchase_options',
        AsyncMock(
            return_value={
                'eligible': True,
                'tariff': {'id': 7},
                'current_subscription': {},
                'period_options': [30],
                'device_options': [2],
                'price_matrix': [
                    {'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900, 'breakdown': {}}]}
                ],
            }
        ),
    )
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement(('point-1',), ('squad-1',), 1, 'access_point_policy', 'fp-1')),
    )

    before = datetime.now(UTC)
    checkout = await service.create_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        source='telegram',
        initial_lifecycle_state='confirmed',
    )
    after = datetime.now(UTC)

    assert checkout.lifecycle_state == 'confirmed'
    assert checkout.confirmed_at is not None
    assert before + timedelta(minutes=30) <= checkout.quote_expires_at <= after + timedelta(minutes=30)
    assert checkout.expires_at > checkout.quote_expires_at
    assert checkout.entitlement_quote_snapshot['entitlement']['location_ids'] == ['point-1']
    assert checkout.entitlement_quote_snapshot['entitlement_hash']


@pytest.mark.asyncio
async def test_legacy_create_checkout_default_birth_stays_a_draft(monkeypatch):
    """Deprecated showcase callers keep the exact pre-refactor behaviour."""
    user = SimpleNamespace(id=7, restriction_subscription=False, balance_kopeks=0)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                ScalarResult(user),
                SimpleNamespace(),
                SimpleNamespace(scalar_one_or_none=lambda: None),
                ScalarResult(None),
                SimpleNamespace(scalars=list),
            ]
        ),
        commit=AsyncMock(),
        get=AsyncMock(return_value=SimpleNamespace(id=7, pricing_revision=1)),
        add=lambda *_args, **_kwargs: None,
        flush=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(
        service,
        'build_purchase_options',
        AsyncMock(
            return_value={
                'eligible': True,
                'tariff': {'id': 7},
                'current_subscription': {},
                'period_options': [30],
                'device_options': [2],
                'price_matrix': [
                    {'period_days': 30, 'prices': [{'device_limit': 2, 'price_kopeks': 36_900, 'breakdown': {}}]}
                ],
            }
        ),
    )
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=ResolvedEntitlement(('point-1',), ('squad-1',), 1, 'access_point_policy', 'fp-1')),
    )

    checkout = await service.create_checkout(
        db,
        user=user,
        period_days=30,
        selected_device_limit=2,
        source='cabinet',
    )

    assert checkout.lifecycle_state == 'draft'
    assert checkout.confirmed_at is None


@pytest.mark.asyncio
async def test_create_checkout_rejects_an_unknown_initial_state(monkeypatch):
    user = SimpleNamespace(id=7)
    with pytest.raises(ValueError, match='Unsupported initial checkout lifecycle state'):
        await service.create_checkout(
            SimpleNamespace(),
            user=user,
            period_days=30,
            selected_device_limit=2,
            source='cabinet',
            initial_lifecycle_state='awaiting_funds',
        )

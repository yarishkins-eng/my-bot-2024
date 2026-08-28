from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_payment_service as payment_service
from app.services.device_first_checkout_service import DIRECT_SETTLEMENT_MODE
from app.services.device_first_payment_service import settle_device_first_platega_payment
from app.services.trial_activation_service import (
    TrialCheckoutResolutionError,
    _LockedDirectContext,
    activate_trial_with_checkout_resolution,
)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class Row:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


def _pending_context():
    user = SimpleNamespace(id=7, auth_type='telegram', restriction_subscription=False, balance_kopeks=0)
    checkout = SimpleNamespace(
        id=9,
        user_id=user.id,
        public_id='checkout-9',
        tariff_id=3,
        period_days=90,
        selected_device_limit=3,
        tariff_total_kopeks=64_900,
        quoted_price_kopeks=64_900,
        lifecycle_state='awaiting_funds',
        quote_state='valid',
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        financial_committed_at=None,
        terminal_reason=None,
        updated_at=None,
    )
    attempt = SimpleNamespace(
        id=41,
        checkout_id=checkout.id,
        provider='platega',
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        platega_payment_id=51,
        provider_payment_id='provider-1',
        provider_method_code=2,
        requested_amount_kopeks=64_900,
        currency='RUB',
        credited_amount_kopeks=0,
        method_key='sbp',
        status='pending',
        reconciliation_reason=None,
        next_reconcile_at=None,
    )
    payment = SimpleNamespace(
        id=51,
        user_id=user.id,
        is_paid=False,
        status='PENDING',
        platega_transaction_id='provider-1',
        amount_kopeks=64_900,
        currency='RUB',
        payment_method_code=2,
        transaction_id=None,
        metadata_json={'device_first_attempt_id': attempt.id, 'settlement_mode': DIRECT_SETTLEMENT_MODE},
    )
    return (
        _LockedDirectContext(
            user=user,
            checkouts=[checkout],
            attempts_by_checkout_id={checkout.id: [attempt]},
            payments_by_id={payment.id: payment},
        ),
        checkout,
        attempt,
        payment,
    )


@pytest.mark.asyncio
async def test_existing_trial_replay_commits_before_post_commit_recovery():
    """Idempotent recovery must release coordinator locks before RemnaWave IO."""

    user = SimpleNamespace(id=7, auth_type='telegram', restriction_subscription=False)
    subscription = SimpleNamespace(id=111, is_trial=True)
    locked = _LockedDirectContext(
        user=user,
        checkouts=[],
        attempts_by_checkout_id={},
        payments_by_id={},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=Rows([subscription])), commit=AsyncMock())

    with patch(
        'app.services.trial_activation_service._lock_direct_context_for_trial',
        AsyncMock(return_value=locked),
    ):
        result = await activate_trial_with_checkout_resolution(db, user_id=user.id)

    assert result.subscription is subscription
    assert result.already_active is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_direct_invoice_requires_explicit_trial_resolution():
    locked, checkout, _attempt, _payment = _pending_context()
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Rows([]), Rows([])]))

    with (
        patch('app.services.trial_activation_service._lock_direct_context_for_trial', AsyncMock(return_value=locked)),
        patch('app.services.trial_activation_service.preview_trial_activation_charge', return_value=0),
        patch(
            'app.services.trial_activation_service._tariff_for_checkout',
            AsyncMock(return_value=SimpleNamespace(name='Базовый')),
        ),
    ):
        with pytest.raises(TrialCheckoutResolutionError) as error:
            await activate_trial_with_checkout_resolution(db, user_id=locked.user.id)

    assert error.value.code == 'pending_checkout_requires_resolution'
    assert error.value.context['checkout']['id'] == checkout.public_id
    assert checkout.lifecycle_state == 'awaiting_funds'


@pytest.mark.asyncio
async def test_explicit_trial_resolution_fences_old_invoice_and_creates_trial_atomically():
    locked, checkout, attempt, payment = _pending_context()
    subscription = SimpleNamespace(id=111)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    with (
        patch('app.services.trial_activation_service._lock_direct_context_for_trial', AsyncMock(return_value=locked)),
        patch('app.services.trial_activation_service.preview_trial_activation_charge', return_value=0),
        patch(
            'app.services.trial_activation_service._tariff_for_checkout',
            AsyncMock(return_value=SimpleNamespace(name='Базовый')),
        ),
        patch(
            'app.services.trial_activation_service._trial_parameters',
            AsyncMock(
                return_value={
                    'duration_days': 3,
                    'traffic_limit_gb': 100,
                    'device_limit': 2,
                    'connected_squads': [],
                    'tariff_id': None,
                }
            ),
        ),
        patch(
            'app.services.trial_activation_service.create_trial_subscription',
            AsyncMock(return_value=subscription),
        ) as create_trial,
    ):
        result = await activate_trial_with_checkout_resolution(
            db,
            user_id=locked.user.id,
            resolution='abandon_pending_invoice',
            expected_checkout_public_id=checkout.public_id,
        )

    assert result.subscription is subscription
    assert result.abandoned_checkout is not None
    assert result.abandoned_checkout.public_id == checkout.public_id
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == 'cancelled_by_user_after_invoice'
    assert attempt.status == 'reconciliation'
    assert attempt.reconciliation_reason == 'provider_invoice_abandoned_by_user'
    assert payment.status == 'VERIFYING'
    create_trial.assert_awaited_once()
    assert create_trial.await_args.kwargs['commit'] is False
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_trial_resolution_then_late_exact_callback_credits_once_without_touching_new_trial(monkeypatch):
    """Exercise both services in the customer-visible abandonment sequence."""

    # РФ-3: поздняя оплата теперь заводит работу в очередь выплат и будит её. Этот тест
    # считает обращения к базе поимённо, поэтому очередь подменяем — она проверена отдельно.
    monkeypatch.setattr(payment_service, 'ensure_deposit_outbox', AsyncMock())
    monkeypatch.setattr(payment_service, 'process_device_first_deposit_outbox', AsyncMock(return_value=0))

    locked, checkout, attempt, payment = _pending_context()
    trial_subscription = SimpleNamespace(id=111, is_trial=True, status='trial')
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Rows([]), Rows([])]),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )

    with (
        patch('app.services.trial_activation_service._lock_direct_context_for_trial', AsyncMock(return_value=locked)),
        patch('app.services.trial_activation_service.preview_trial_activation_charge', return_value=0),
        patch(
            'app.services.trial_activation_service._tariff_for_checkout',
            AsyncMock(return_value=SimpleNamespace(name='Базовый')),
        ),
        patch(
            'app.services.trial_activation_service._trial_parameters',
            AsyncMock(
                return_value={
                    'duration_days': 3,
                    'traffic_limit_gb': 100,
                    'device_limit': 2,
                    'connected_squads': [],
                    'tariff_id': None,
                }
            ),
        ),
        patch(
            'app.services.trial_activation_service.create_trial_subscription',
            AsyncMock(return_value=trial_subscription),
        ),
    ):
        result = await activate_trial_with_checkout_resolution(
            db,
            user_id=locked.user.id,
            resolution='abandon_pending_invoice',
            expected_checkout_public_id=checkout.public_id,
        )

    assert result.subscription is trial_subscription
    assert checkout.lifecycle_state == 'cancelled'
    # The old direct payment callback uses its real settlement service after
    # the coordinator commit.  It must credit wallet only and never fulfil the
    # terminated checkout or modify the newly created trial.
    db.execute.side_effect = [
        Row(payment),
        Row(locked.user),
        Row(attempt),
        Row(checkout),
        Row(None),
        Row(payment),
        Row(locked.user),
        Row(attempt),
        Row(checkout),
    ]
    payload = {
        'id': 'provider-1',
        'paymentMethod': 2,
        'paymentDetails': {'amount': '649.00', 'currency': 'RUB'},
    }
    with patch('app.services.device_first_payment_service.fulfill_direct_external_checkout', AsyncMock()) as fulfill:
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)
        await settle_device_first_platega_payment(db, payment=payment, payload=payload)

    assert locked.user.balance_kopeks == 64_900
    assert attempt.status == 'credited'
    assert checkout.fulfillment_state == 'not_started'
    assert trial_subscription.is_trial is True
    assert trial_subscription.status == 'trial'
    ledger_rows = [
        call.args[0]
        for call in db.add.call_args_list
        if getattr(call.args[0], 'device_first_ledger_key', None) == 'direct_late_invoice:41'
    ]
    assert len(ledger_rows) == 1
    fulfill.assert_not_awaited()


# ---------------------------------------------------------------------------
# Пункт 4.5. Застрявший заказ и пробный период — ЕДИНСТВЕННЫЙ доказанный вред
# состояний `conflict` / `failed` / `reprice_required`.
#
# Тариф они не запирают (входят в `_CHECKOUT_TERMINAL_STATES`, `crud/tariff.py`), новую
# покупку не блокируют (свип их не выбирает), тревогу владельцу не поднимают. Они делают
# ровно одно: навсегда отнимают у человека право на пробный период. Мест, где это
# решается, ДВА, и оба обязаны отпустить его после закрытия заказа.
# ---------------------------------------------------------------------------

STOPPED_STATES = ('conflict', 'failed', 'reprice_required')


def _stopped_checkout(state: str):
    """Заказ, который сам никуда не поедет: свип его не выбирает, повторов у него нет."""
    return SimpleNamespace(
        id=9,
        user_id=7,
        public_id='checkout-stuck-9',
        tariff_id=3,
        period_days=90,
        selected_device_limit=3,
        tariff_total_kopeks=64_900,
        quoted_price_kopeks=64_900,
        lifecycle_state=state,
        quote_state='valid',
        settlement_mode=DIRECT_SETTLEMENT_MODE,
        funding_state='invoice_terminal',
        fulfillment_state='not_started',
        financial_committed_at=None,
        terminal_reason='payment_amount_mismatch',
        updated_at=None,
    )


class _ReadOnlyTrialDb:
    """Сессия классификатора: заказы, попытки, платежи — в том порядке, как он их просит."""

    def __init__(self, checkouts):
        self._answers = [Rows(list(checkouts)), Rows([])]

    async def execute(self, _stmt):
        return self._answers.pop(0)

    async def get(self, _model, _ident):
        return SimpleNamespace(name='Базовый')


async def _close_with_the_operator_button(checkout) -> None:
    """Ровно та кнопка, которую жмёт оператор, — без её обхода тест ничего не доказывает."""
    from app.services.device_first_checkout_service import close_operator_review_checkout

    done, _message = await close_operator_review_checkout(
        SimpleNamespace(commit=AsyncMock()), checkout=checkout, admin_user_id=1
    )
    assert done is True


@pytest.mark.parametrize('state', STOPPED_STATES)
@pytest.mark.asyncio
async def test_a_stopped_order_takes_the_trial_away_and_closing_gives_it_back(state):
    """Классификатор (`/trial` и мини-апп). До закрытия — отказ, после — снова можно."""
    from app.services.trial_activation_service import get_trial_checkout_context

    checkout = _stopped_checkout(state)
    blocked = await get_trial_checkout_context(_ReadOnlyTrialDb([checkout]), user_id=7)
    assert blocked.state == 'reconciliation_required'
    assert blocked.checkout.public_id == checkout.public_id

    await _close_with_the_operator_button(checkout)

    released = await get_trial_checkout_context(_ReadOnlyTrialDb([checkout]), user_id=7)
    assert released.state == 'ready'


class _TrialFenceReached(Exception):
    """Управление дошло дальше забора — значит забор пропустил."""


@pytest.mark.parametrize('state', STOPPED_STATES)
@pytest.mark.asyncio
async def test_the_mutating_path_also_lets_the_trial_through_after_closing(state):
    """🔴 Второе место, и правка одного оставила бы второе врать.

    Классификатор только рассказывает; выдаёт триал этот путь, и у него свой список
    состояний. Проверяем поведением: до закрытия — отказ с кодом блокировки, после —
    управление уходит за забор (ловим его собственным исключением).
    """
    checkout = _stopped_checkout(state)
    locked = _LockedDirectContext(
        user=SimpleNamespace(id=7, auth_type='telegram', restriction_subscription=False, balance_kopeks=0),
        checkouts=[checkout],
        attempts_by_checkout_id={},
        payments_by_id={},
    )
    patches = (
        patch('app.services.trial_activation_service._lock_direct_context_for_trial', AsyncMock(return_value=locked)),
        patch('app.services.trial_activation_service.preview_trial_activation_charge', return_value=0),
        patch(
            'app.services.trial_activation_service._tariff_for_checkout',
            AsyncMock(return_value=SimpleNamespace(name='Базовый')),
        ),
    )

    with patches[0], patches[1], patches[2]:
        db = SimpleNamespace(execute=AsyncMock(side_effect=[Rows([]), Rows([])]))
        with pytest.raises(TrialCheckoutResolutionError) as blocked:
            await activate_trial_with_checkout_resolution(db, user_id=7)
    assert blocked.value.code == 'trial_blocked_by_reconciliation'

    await _close_with_the_operator_button(checkout)

    with (
        patches[0],
        patches[1],
        patches[2],
        patch(
            'app.services.trial_activation_service._trial_parameters',
            AsyncMock(side_effect=_TrialFenceReached),
        ),
    ):
        db = SimpleNamespace(execute=AsyncMock(side_effect=[Rows([]), Rows([])]))
        with pytest.raises(_TrialFenceReached):
            await activate_trial_with_checkout_resolution(db, user_id=7)

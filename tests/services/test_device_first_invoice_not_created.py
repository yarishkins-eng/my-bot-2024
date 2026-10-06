"""ВК-15. Platega не выставила счёт — заказ отпускается сразу, а не запирает человека.

Было: ответ без номера счёта (502, обрыв связи, таймаут — `PlategaService._request` во всех
этих случаях отдаёт `None`) → попытка «на сверке» → через 5 минут заказ на разборе → любая
новая покупка, даже с баланса, отбита до ручного закрытия владельцем. Так 28.09.2026 сорок
две минуты просидела новая клиентка (заказ 112), 21.08 — трое суток клиент 207 (заказ 48).

Стало: без номера счёта ссылку на оплату человек не получал ни разу — платить нечем, —
поэтому попытка закрывается, заказ отменяется с причиной «счёт не создан», а человеку
отвечают своим кодом. Сторожа зовут НАСТОЯЩИЕ `_create_direct_platega_attempt` и
`_hold_direct_invoice_for_review` на подделке сессии, которая отдаёт ровно те объекты,
что код создал сам, — как карта идентичности настоящей сессии.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from app.database.models import CheckoutPaymentAttempt, PlategaPayment, SubscriptionCheckout, User
from app.services.device_first_checkout_service import DIRECT_SETTLEMENT_MODE, DeviceFirstError, checkout_money_state
from app.services.device_first_payment_service import (
    _create_direct_platega_attempt,
    _hold_direct_invoice_for_review,
    _queue_direct_callback_for_canonical_reconciliation,
)
from app.services.platega_service import PlategaService


class _Session:
    """Выборки по сущности отдают объекты графа; запись — только в эти же объекты."""

    def __init__(self, checkout, *rows):
        self.checkout = checkout
        self.added = list(rows)
        self.commit = AsyncMock()
        self.refresh = AsyncMock()
        self.rollback = AsyncMock()
        # Единственный вопрос к базе в этих сторожах вне выборок по сущности — «сколько зачислено».
        self.scalar = AsyncMock(return_value=0)

    def add(self, model):
        self.added.append(model)

    async def flush(self):
        for model in self.added:
            if getattr(model, 'id', None) is None:
                model.id = 41 if isinstance(model, CheckoutPaymentAttempt) else 51

    def one(self, model_type):
        (row,) = [model for model in self.added if isinstance(model, model_type)]
        return row

    async def execute(self, statement):
        entity = statement.column_descriptions[0]['entity']
        if entity is SubscriptionCheckout:
            row = self.checkout
        elif entity is User:
            row = SimpleNamespace(id=7)
        else:
            row = self.one(entity)
        return SimpleNamespace(scalar_one_or_none=lambda: row, scalar_one=lambda: row)


def _checkout(**overrides):
    values = {
        'id': 91,
        'public_id': 'checkout-91',
        'user_id': 7,
        'external_payable_kopeks': 19_900,
        'tariff_total_kopeks': 19_900,
        'wallet_applied_kopeks': 0,
        'lifecycle_state': 'awaiting_funds',
        'quote_state': 'valid',
        'funding_state': 'invoice_pending',
        'fulfillment_state': 'not_started',
        'debit_transaction_id': None,
        'terminal_reason': None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _platega(outcome, posts):
    class FakePlatega:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 3

        async def create_payment(self, **_kwargs):
            posts.append(self._max_retries)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def get_transaction(self, _transaction_id):
            raise AssertionError('у счёта без номера спрашивать нечего')

    return FakePlatega


def _patch_create(monkeypatch, checkout, outcome, posts):
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', _platega(outcome, posts))
    monkeypatch.setattr('app.services.device_first_payment_service.settings.CABINET_URL', 'https://cabinet.example')


async def _pay(db, checkout):
    return await _create_direct_platega_attempt(
        db,
        checkout_public_id=checkout.public_id,
        user_id=7,
        method_key='sbp',
        method_code=2,
        was_financially_committed=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('outcome', 'failure'),
    [
        # 502 и любой код ≥ 400, таймаут, обрыв связи — `_request` отдаёт `None`.
        (None, 'provider_response_missing_identity'),
        # Ответ пришёл, но без номера счёта: ссылка в нём есть, а привязать её не к чему.
        ({'url': 'https://pay.example/orphan', 'status': 'PENDING'}, 'provider_response_missing_identity'),
        (RuntimeError('провайдер упал'), 'create_exception:RuntimeError'),
    ],
)
async def test_platega_without_an_invoice_number_releases_the_order_at_once(monkeypatch, outcome, failure):
    checkout = _checkout()
    db = _Session(checkout)
    posts = []
    _patch_create(monkeypatch, checkout, outcome, posts)

    with capture_logs() as logs, pytest.raises(DeviceFirstError) as raised:
        await _pay(db, checkout)

    attempt, payment = db.one(CheckoutPaymentAttempt), db.one(PlategaPayment)
    # По этой строке после выкладки принимается живое доказательство: первый сбой Platega.
    assert [log for log in logs if log['event'] == 'device_first_direct_invoice_not_created'] == [
        {
            'event': 'device_first_direct_invoice_not_created',
            'log_level': 'warning',
            'checkout_id': 'checkout-91',
            'attempt_id': 41,
            'failure': failure,
        }
    ]
    assert raised.value.code == 'provider_invoice_not_created'
    # 4xx, а не 5xx: на 4xx кабинет сам меняет ключ повтора, и следующее нажатие —
    # новый запрос. Ответ 5xx он повторил бы тем же ключом и получил бы тот же отказ.
    assert raised.value.status_code == 409
    assert posts == [1], 'в одной попытке — ровно один запрос к Platega'
    assert attempt.status == 'failed'
    assert attempt.reconciliation_reason == f'provider_invoice_not_created:{failure}'
    assert payment.status == 'FAILED'
    assert (checkout.lifecycle_state, checkout.quote_state, checkout.funding_state, checkout.terminal_reason) == (
        'cancelled',
        'expired',
        'invoice_not_created',
        'provider_invoice_not_created',
    )
    if isinstance(outcome, Exception):
        assert raised.value.__cause__ is outcome


@pytest.mark.asyncio
async def test_an_order_that_moved_on_meanwhile_stays_with_the_operator(monkeypatch):
    """Отпускаем только заказ, который всё ещё ждёт денег. Остальное — прежний разбор."""
    checkout = _checkout(lifecycle_state='fulfilling')
    db = _Session(checkout)
    _patch_create(monkeypatch, checkout, None, [])

    with pytest.raises(DeviceFirstError) as raised:
        await _pay(db, checkout)

    assert raised.value.code == 'reconciliation_required'
    assert db.one(CheckoutPaymentAttempt).status == 'operator_review'
    assert (checkout.lifecycle_state, checkout.terminal_reason) == (
        'operator_review',
        'provider_invoice_creation_incomplete',
    )


def _graph(*, payment_is_paid=False, metadata_attempt_id=41, **attempt_overrides):
    attempt_values = {
        'id': 41,
        'checkout_id': 91,
        'status': 'creating',
        'provider_payment_id': None,
        'platega_payment_id': 51,
        'settlement_mode': DIRECT_SETTLEMENT_MODE,
        'credited_amount_kopeks': 0,
        'reconciliation_reason': None,
    }
    attempt_values.update(attempt_overrides)
    attempt = CheckoutPaymentAttempt(**attempt_values)
    payment = PlategaPayment(
        id=51,
        user_id=7,
        correlation_id='corr-51',
        amount_kopeks=19_900,
        status='CREATING',
        is_paid=payment_is_paid,
        metadata_json={'device_first_attempt_id': metadata_attempt_id},
    )
    return attempt, payment


async def _hold(db, **kwargs):
    return await _hold_direct_invoice_for_review(
        db,
        attempt_id=41,
        payment_id=51,
        reason='provider_invoice_creation_incomplete',
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['creating', 'reconciliation'])
async def test_the_worker_releases_an_interrupted_invoice_without_a_number(status):
    """Обрыв процесса между записью попытки и ответом Platega: через 5 минут — отпустить."""
    checkout = _checkout()
    attempt, payment = _graph(status=status)
    db = _Session(checkout, attempt, payment)

    assert await _hold(db, release_failure='creation_interrupted') is True

    assert (attempt.status, attempt.reconciliation_reason) == (
        'failed',
        'provider_invoice_not_created:creation_interrupted',
    )
    assert payment.status == 'FAILED'
    assert (checkout.lifecycle_state, checkout.terminal_reason) == ('cancelled', 'provider_invoice_not_created')
    db.commit.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('graph', 'checkout_overrides'),
    [
        ({'payment_is_paid': True}, {}),
        ({'credited_amount_kopeks': 19_900}, {}),
        ({'provider_payment_id': 'inv-1'}, {}),
        # Уведомление Platega обогнало ответ: попытка уже на разборе по проверке привязки.
        ({'status': 'operator_review'}, {}),
        ({}, {'lifecycle_state': 'fulfilling'}),
        ({}, {'fulfillment_state': 'fulfilled'}),
        ({}, {'debit_transaction_id': 77}),
    ],
)
async def test_any_sign_of_money_or_progress_keeps_the_order_on_review(graph, checkout_overrides):
    checkout = _checkout(**checkout_overrides)
    attempt, payment = _graph(**graph)
    db = _Session(checkout, attempt, payment)

    assert await _hold(db, release_failure='provider_response_missing_identity') is True

    assert (attempt.status, attempt.reconciliation_reason) == (
        'operator_review',
        'provider_invoice_creation_incomplete',
    )
    assert payment.status == 'OPERATOR_REVIEW'
    assert (checkout.lifecycle_state, checkout.terminal_reason) == (
        'operator_review',
        'provider_invoice_creation_incomplete',
    )


@pytest.mark.asyncio
async def test_other_holds_do_not_release_anything():
    """Разбор по другим причинам (счёт заведён, проверка не сошлась) не отпускает никогда."""
    checkout = _checkout()
    attempt, payment = _graph()
    db = _Session(checkout, attempt, payment)

    assert await _hold(db) is True

    assert attempt.status == 'operator_review'
    assert checkout.lifecycle_state == 'operator_review'


@pytest.mark.asyncio
async def test_a_foreign_payment_is_never_released():
    """Платёж, привязанный к чужой попытке, — разбор по несовпадению, а не «счёт не создан»."""
    checkout = _checkout()
    attempt, payment = _graph(metadata_attempt_id=999)
    db = _Session(checkout, attempt, payment)

    assert await _hold(db, release_failure='provider_response_missing_identity') is False

    assert (attempt.status, attempt.reconciliation_reason) == ('operator_review', 'direct_payment_binding_mismatch')
    assert (checkout.lifecycle_state, checkout.terminal_reason) == (
        'operator_review',
        'direct_payment_binding_mismatch',
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_status', ['CANCELED', 'CONFIRMED'])
async def test_a_late_provider_callback_takes_the_order_off_the_no_money_reason(provider_status):
    """На этом держится «денег не брали» у причины `provider_invoice_not_created`.

    Уведомление Platega по счёту без номера (если Platega всё-таки завела счёт за сбоем) не
    проходит проверку привязки и уводит заказ на разбор со своей причиной (мина OD). Если когда-
    нибудь «починить» это молчаливым пропуском, заказ останется с причиной «счёт не создан», и
    при пришедшем «оплачено» клиенту и владельцу продолжат говорить «денег не брали».
    """
    checkout = _checkout(
        lifecycle_state='cancelled', funding_mode='platega', terminal_reason='provider_invoice_not_created'
    )
    attempt, payment = _graph(
        status='failed',
        reconciliation_reason='provider_invoice_not_created:provider_response_missing_identity',
    )
    payment.status = 'FAILED'
    db = _Session(checkout, attempt, payment)
    assert await checkout_money_state(db, checkout) == 'no_money'

    await _queue_direct_callback_for_canonical_reconciliation(
        db,
        payment_id=51,
        payload={'id': 'orphan-1', 'status': provider_status},
        reason='provider_callback_before_identity_binding',
        attempt_id=41,
    )

    assert (checkout.lifecycle_state, checkout.terminal_reason) == (
        'operator_review',
        'direct_payment_binding_mismatch',
    )
    assert attempt.status == 'operator_review'
    assert await checkout_money_state(db, checkout) != 'no_money'

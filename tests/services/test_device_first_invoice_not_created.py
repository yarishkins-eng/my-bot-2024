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

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from app.database.models import CheckoutPaymentAttempt, PlategaPayment, SubscriptionCheckout, User
from app.services.device_first_checkout_service import DIRECT_SETTLEMENT_MODE, DeviceFirstError, checkout_money_state
from app.services.device_first_payment_service import (
    _apply_direct_pending_provider_observation,
    _bind_direct_provider_identity,
    _create_direct_platega_attempt,
    _hold_direct_invoice_for_review,
    _invoice_never_reached_customer,
    _queue_direct_callback_for_canonical_reconciliation,
)
from app.services.platega_service import PlategaService


class _Session:
    """Выборки по сущности отдают объекты графа; запись — только в эти же объекты.

    Запросы записываются (`statements`): подделка не исполняет ни `FOR UPDATE`, ни
    `populate_existing`, ни условий аренды, поэтому их проверяют по самим запросам.
    """

    def __init__(self, checkout, *rows, commit_hook=None):
        self.checkout = checkout
        self.added = list(rows)
        self.statements = []
        self.commit = AsyncMock(side_effect=commit_hook)
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
        self.statements.append(statement)
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


def _platega(outcome, posts, on_post=None):
    class FakePlatega:
        parse_redirect_url = staticmethod(PlategaService.parse_redirect_url)
        parse_expires_at = staticmethod(PlategaService.parse_expires_at)
        parse_amount_currency = staticmethod(PlategaService.parse_amount_currency)

        def __init__(self):
            self._max_retries = 3

        async def create_payment(self, **_kwargs):
            posts.append(self._max_retries)
            if on_post is not None:
                on_post()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def get_transaction(self, _transaction_id):
            raise AssertionError('у счёта без номера спрашивать нечего')

    return FakePlatega


def _patch_create(monkeypatch, checkout, outcome, posts, on_post=None):
    monkeypatch.setattr(
        'app.services.device_first_payment_service.prepare_direct_external_checkout',
        AsyncMock(return_value=checkout),
    )
    monkeypatch.setattr(
        'app.services.device_first_payment_service.get_pending_platega_attempt',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr('app.services.device_first_payment_service.PlategaService', _platega(outcome, posts, on_post))
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
    # 4xx, как и раньше: на 5xx кабинет не сменил бы ключ повтора и повторял бы сохранённый отказ.
    assert raised.value.status_code == 409
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


@pytest.mark.asyncio
async def test_a_late_post_response_after_release_binds_but_never_reaches_the_customer():
    """Гонка «ответ Platega пришёл после того, как сверка отпустила заказ».

    Соблазнительная «починка» — добавить `failed` в ранний выход привязки — заперла бы человека:
    номер не привязался бы, и проверка счёта ушла бы в разбор. Правильно — привязать номер к уже
    закрытому заказу: ссылку не отдаст никто (заказ не `awaiting_funds`, попытка не `pending`).
    """
    checkout = _checkout(lifecycle_state='cancelled', terminal_reason='provider_invoice_not_created')
    attempt, payment = _graph(
        status='failed',
        reconciliation_reason='provider_invoice_not_created:creation_interrupted',
    )
    db = _Session(checkout, attempt, payment)

    bound = await _bind_direct_provider_identity(
        db,
        attempt_id=41,
        payment_id=51,
        provider_payment_id='inv-late',
        redirect_url='https://pay.example/late',
    )

    assert bound is attempt
    assert (attempt.provider_payment_id, payment.platega_transaction_id) == ('inv-late', 'inv-late')
    assert attempt.status == 'reconciliation'
    assert checkout.lifecycle_state == 'cancelled'


# --- Сторожа по итогам мутационного прогона волны 2 (пережившие мутанты B01–B47) ---


@pytest.mark.asyncio
async def test_the_release_happens_under_the_four_row_locks_and_the_lease_fence():
    """Отпуск безопасен только под замками Payment → User → Attempt → Checkout и арендой сверки.

    Без них позднее уведомление Platega проскочит между проверкой и записью, а устаревший
    воркер без аренды отпустит повторно. Подделка сессии замков не исполняет — проверяем запросы.
    """
    checkout = _checkout()
    attempt, payment = _graph()
    db = _Session(checkout, attempt, payment)

    assert await _hold(db, lease_token='tok', lease_epoch=3, release_failure='creation_interrupted') is True

    entities = [statement.column_descriptions[0]['entity'] for statement in db.statements]
    assert entities == [PlategaPayment, User, CheckoutPaymentAttempt, SubscriptionCheckout]
    texts = [str(statement.compile(dialect=postgresql.dialect())) for statement in db.statements]
    assert all('FOR UPDATE' in text for text in texts)
    for statement, entity in zip(db.statements, entities, strict=True):
        if entity is not User:
            assert statement.get_execution_options().get('populate_existing') is True
    attempt_where = texts[2].split('WHERE', 1)[1]
    for fence in ('lease_token =', 'lease_epoch =', 'lease_expires_at >='):
        assert fence in attempt_where


@pytest.mark.asyncio
async def test_the_commit_sees_the_final_released_state():
    checkout = _checkout()
    attempt, payment = _graph()
    snapshots = []

    def snapshot(*_args, **_kwargs):
        snapshots.append((attempt.status, payment.status, checkout.lifecycle_state, checkout.terminal_reason))

    db = _Session(checkout, attempt, payment, commit_hook=snapshot)
    await _hold(db, release_failure='creation_interrupted')

    assert snapshots == [('failed', 'FAILED', 'cancelled', 'provider_invoice_not_created')]


@pytest.mark.asyncio
async def test_the_attempt_is_durable_before_the_post_and_the_first_reconcile_waits_minutes(monkeypatch):
    """Попытка записана ДО запроса к Platega, а первая сверка ждёт минуты, а не секунды.

    Иначе сверка могла бы отпустить заказ, чей запрос ещё в полёте (потолок запроса — 30 с).
    """
    checkout = _checkout()
    db = _Session(checkout)
    commits_seen_by_post = []
    _patch_create(monkeypatch, checkout, None, [], on_post=lambda: commits_seen_by_post.append(db.commit.await_count))

    with pytest.raises(DeviceFirstError):
        await _pay(db, checkout)

    assert commits_seen_by_post == [1]
    assert db.one(CheckoutPaymentAttempt).next_reconcile_at - datetime.now(UTC) > timedelta(minutes=4)


def _decide(attempt=None, payment='ok', checkout='ok'):
    attempt = attempt or SimpleNamespace(provider_payment_id=None, status='creating', credited_amount_kopeks=0)
    payment = SimpleNamespace(is_paid=False) if payment == 'ok' else payment
    if checkout == 'ok':
        checkout = SimpleNamespace(
            lifecycle_state='awaiting_funds', fulfillment_state='not_started', debit_transaction_id=None
        )
    return _invoice_never_reached_customer(attempt, payment, checkout)


def test_the_release_decision_baseline_and_missing_rows():
    assert _decide() is True
    assert _decide(payment=None) is False
    assert _decide(checkout=None) is False


@pytest.mark.parametrize(
    'lifecycle',
    [
        'draft',
        'confirmed',
        'armed',
        'fulfilling',
        'ready',
        'operator_review',
        'conflict',
        'cancelled',
        'expired',
        'failed',
    ],
)
def test_only_an_order_still_waiting_for_money_is_released(lifecycle):
    checkout = SimpleNamespace(lifecycle_state=lifecycle, fulfillment_state='not_started', debit_transaction_id=None)
    assert _decide(checkout=checkout) is False


@pytest.mark.parametrize(
    'status', ['pending', 'paid_processing', 'paid', 'credited', 'failed', 'operator_review', 'terminal', 'cancelled']
)
def test_only_creating_or_reconciliation_attempts_are_released(status):
    attempt = SimpleNamespace(provider_payment_id=None, status=status, credited_amount_kopeks=0)
    assert _decide(attempt=attempt) is False


@pytest.mark.parametrize('fulfillment', ['in_progress', 'fulfilled', 'failed', 'blocked'])
def test_an_order_whose_fulfilment_started_is_never_released(fulfillment):
    checkout = SimpleNamespace(
        lifecycle_state='awaiting_funds', fulfillment_state=fulfillment, debit_transaction_id=None
    )
    assert _decide(checkout=checkout) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [TimeoutError(), ValueError('x'), OSError('x'), Exception('x')])
async def test_any_exception_of_the_provider_call_releases_the_order(monkeypatch, error):
    checkout = _checkout()
    db = _Session(checkout)
    _patch_create(monkeypatch, checkout, error, [])

    with pytest.raises(DeviceFirstError) as raised:
        await _pay(db, checkout)

    assert raised.value.code == 'provider_invoice_not_created'
    assert db.one(CheckoutPaymentAttempt).status == 'failed'


@pytest.mark.asyncio
async def test_an_exception_on_an_order_that_moved_on_keeps_the_review_reason(monkeypatch):
    checkout = _checkout(lifecycle_state='fulfilling')
    db = _Session(checkout)
    _patch_create(monkeypatch, checkout, RuntimeError('boom'), [])

    with pytest.raises(DeviceFirstError) as raised:
        await _pay(db, checkout)

    assert (raised.value.code, raised.value.status_code) == ('reconciliation_required', 409)
    assert checkout.terminal_reason == 'provider_invoice_creation_incomplete'


@pytest.mark.asyncio
async def test_a_hold_that_did_not_take_effect_never_says_not_created(monkeypatch):
    """Если разбор не дошёл до записи, человеку нельзя говорить «счёт не выдан, денег нет»."""
    checkout = _checkout()
    db = _Session(checkout)
    _patch_create(monkeypatch, checkout, None, [])
    monkeypatch.setattr(
        'app.services.device_first_payment_service._hold_direct_invoice_for_review', AsyncMock(return_value=False)
    )

    with pytest.raises(DeviceFirstError) as raised:
        await _pay(db, checkout)

    assert db.one(CheckoutPaymentAttempt).status == 'creating'
    assert raised.value.code == 'reconciliation_required'


@pytest.mark.asyncio
async def test_a_late_bound_invoice_of_a_released_order_is_never_published():
    """Вторая половина гонки «ответ после отпуска»: привязанный к закрытому заказу счёт не публикуется.

    Наблюдение `PENDING` по такому счёту не делает попытку `pending` и не возвращает заказ в
    `awaiting_funds` — а без этого ссылку не отдаст ни кабинет, ни бот.
    """
    checkout = _checkout(
        lifecycle_state='cancelled', funding_state='invoice_not_created', terminal_reason='provider_invoice_not_created'
    )
    attempt, payment = _graph(
        status='reconciliation',
        provider_payment_id='inv-late',
        provider_method_code=2,
        requested_amount_kopeks=19_900,
        currency='RUB',
        reconciliation_reason='provider_invoice_verification_pending',
    )
    payment.platega_transaction_id = 'inv-late'
    db = _Session(checkout, attempt, payment)
    payload = {
        'id': 'inv-late',
        'status': 'PENDING',
        'paymentMethod': 2,
        'paymentDetails': {'amount': 199, 'currency': 'RUB'},
    }

    published = await _apply_direct_pending_provider_observation(
        db, attempt_id=41, payment_id=51, payload=payload, observed_after=datetime.now(UTC)
    )

    assert published is False
    assert attempt.status == 'reconciliation'
    assert payment.status != 'PENDING'
    assert checkout.lifecycle_state == 'cancelled'

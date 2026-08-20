"""Сторожа пула вечной сверки (мина BO, 20.08.2026).

Две из четырёх ветвей отбора воркера находят строку не по статусу, а по ПРИЧИНЕ:
архивную — по префиксу `provider_terminal:`, операторскую — по точному значению.
Воркер же писал в это самое поле исход опроса провайдера, и строка выпадала из
пула навсегда. На боевом так выпало восемь платёжных попыток; ни одну не трогали
после наступления срока, самая старая просрочена на пять суток.

🔴 Ожидание здесь выводится из ПОВЕДЕНИЯ: тест поднимает настоящие таблицы в
SQLite, заводит настоящие строки и зовёт настоящий `reconcile_device_first_payments`,
а потом проверяет, попала ли строка в СЛЕДУЮЩИЙ проход. Список причин руками не
перебирается — иначе сторож повторял бы ту же константу, что и код, и краснел бы
только вместе с ним. В этом проекте такой сторож уже трижды переживал мутацию.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.models import CheckoutPaymentAttempt, PlategaPayment, SubscriptionCheckout
from app.services import device_first_payment_service as service


PAST = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _make_db():
    """Настоящие таблицы в памяти + асинхронная обёртка над синхронной сессией.

    Асинхронный SQLite здесь недоступен: `conftest` подменяет `aiosqlite` заглушкой
    ещё до импорта приложения. Обёртка нужна ровно для этого, а запросы исполняет
    настоящий движок — то есть предикат отбора проверяется, а не изображается.
    """
    engine = create_engine('sqlite:///:memory:')
    for table in (SubscriptionCheckout.__table__, PlategaPayment.__table__, CheckoutPaymentAttempt.__table__):
        table.create(engine)
    sync_db = sessionmaker(engine)()

    async def execute(statement, *args, **kwargs):
        return sync_db.execute(statement, *args, **kwargs)

    async def get(entity, ident):
        return sync_db.get(entity, ident)

    async def commit():
        sync_db.commit()

    return sync_db, SimpleNamespace(execute=execute, get=get, commit=commit, add=sync_db.add)


def _seed(sync_db, *, attempt_id: int, status: str, reason: str | None) -> None:
    sync_db.add(
        SubscriptionCheckout(
            id=attempt_id,
            public_id=f'checkout-{attempt_id}',
            user_id=7,
            tariff_id=3,
            source='cabinet',
            settlement_mode='direct_purchase_v2',
            period_days=30,
            selected_device_limit=1,
            quoted_price_kopeks=35_000,
            max_price_kopeks=35_000,
            pricing_revision=1,
            quote_expires_at=PAST,
            expires_at=PAST,
            lifecycle_state='cancelled',
            terminal_reason='provider_terminal:canceled',
        )
    )
    sync_db.add(
        PlategaPayment(
            id=attempt_id,
            user_id=7,
            platega_transaction_id=f'provider-{attempt_id}',
            correlation_id=f'corr-{attempt_id}',
            amount_kopeks=35_000,
            payment_method_code=2,
            status='PENDING',
        )
    )
    sync_db.add(
        CheckoutPaymentAttempt(
            id=attempt_id,
            checkout_id=attempt_id,
            merchant_order_key=f'order-{attempt_id}',
            method_key='sbp',
            provider_method_code=2,
            requested_amount_kopeks=35_000,
            settlement_mode='direct_purchase_v2',
            status=status,
            provider_payment_id=f'provider-{attempt_id}',
            platega_payment_id=attempt_id,
            reconciliation_reason=reason,
            next_reconcile_at=PAST,
        )
    )


class _Provider:
    """Провайдер, который отвечает так, как отвечал боевой.

    🔴 Не ответить по-настоящему можно ТРЕМЯ способами, и каждый затирал ключ
    отбора своей строкой кода: пустота, исключение и тело с неузнанным статусом.
    Мутационный прогон 20.08 показал, что сторож на одной пустоте пропускает
    два других — мутация переживала набор.
    """

    def __init__(self, polled: list[str], answer):
        self._polled = polled
        self._answer = answer

    async def get_transaction(self, transaction_id):
        self._polled.append(transaction_id)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture
def swept(monkeypatch):
    """Гоняет настоящий воркер и возвращает, кого он опросил."""
    sync_db, db = _make_db()
    polled: list[str] = []
    holder: dict = {'answer': None}
    monkeypatch.setattr(service, 'PlategaService', lambda: _Provider(polled, holder['answer']))

    async def run(answer=None) -> list[str]:
        holder['answer'] = answer
        polled.clear()
        await service.reconcile_device_first_payments(db, limit=20)
        sync_db.expire_all()
        return sorted(polled)

    return sync_db, run


@pytest.mark.asyncio
async def test_archived_row_survives_a_silent_provider_and_returns_next_sweep(swept) -> None:
    """Молчание провайдера не должно выбрасывать строку из пула. Это и есть мина BO."""
    sync_db, run = swept
    _seed(sync_db, attempt_id=41, status='failed', reason='provider_terminal:canceled')
    sync_db.commit()

    assert await run() == ['provider-41'], 'строка вообще не попала в выборку — тест ничего не проверяет'

    row = sync_db.get(CheckoutPaymentAttempt, 41)
    assert row.reconciliation_reason == 'provider_terminal:canceled', 'ключ отбора затёрт исходом опроса'

    # 🔴 Второй проход — единственное честное доказательство: строка вернулась.
    row.next_reconcile_at = PAST
    sync_db.commit()
    assert await run() == ['provider-41'], 'после молчания провайдера строка выпала из следующего прохода'


@pytest.mark.asyncio
async def test_operator_row_survives_a_silent_provider(swept) -> None:
    """Вторая ветвь отбора держится за точное значение причины — её тоже нельзя затирать."""
    sync_db, run = swept
    _seed(sync_db, attempt_id=42, status='operator_review', reason='provider_invoice_missing_or_elapsed_expiry')
    sync_db.commit()

    assert await run() == ['provider-42']
    row = sync_db.get(CheckoutPaymentAttempt, 42)
    assert row.reconciliation_reason == 'provider_invoice_missing_or_elapsed_expiry'

    row.next_reconcile_at = PAST
    sync_db.commit()
    assert await run() == ['provider-42'], 'операторская строка выпала из следующего прохода'


@pytest.mark.asyncio
async def test_silent_provider_slows_the_archived_row_down(swept) -> None:
    """Сохранить строку в пуле мало — надо погасить частоту.

    🔴 Общий откат воркера упирается в потолок 60 минут, а выборка сортируется по
    сроку с `limit=20`: архивная строка, просроченная на часы, встаёт в очередь
    ВПЕРЕДИ живого счёта, просроченного на две минуты. Без гашения правка поменяла бы
    тихую потерю строк на вытеснение живых платежей из сверки.
    """
    sync_db, run = swept
    _seed(sync_db, attempt_id=43, status='failed', reason='provider_terminal:canceled')
    sync_db.commit()

    before = datetime.now(UTC)
    await run()

    row = sync_db.get(CheckoutPaymentAttempt, 43)
    due = row.next_reconcile_at
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    assert due - before >= timedelta(hours=6), (
        f'строка осталась на частом опросе: следующий срок через {due - before}, '
        'а архивная строка обязана затухать до недельного потолка'
    )


@pytest.mark.asyncio
async def test_ordinary_row_still_records_why_the_provider_was_silent(swept) -> None:
    """Защита от передозировки: где диагностика безопасна, её терять нельзя.

    Обычная `pending`-строка отбирается по СТАТУСУ, причина её в пуле не держит —
    значит исход опроса обязан записаться, как и раньше.
    """
    sync_db, run = swept
    _seed(sync_db, attempt_id=44, status='pending', reason=None)
    sync_db.commit()

    assert await run() == ['provider-44']
    row = sync_db.get(CheckoutPaymentAttempt, 44)
    assert row.reconciliation_reason == 'status_lookup:empty', (
        'исход опроса перестал записываться там, где это было безопасно'
    )


def test_the_producer_writes_exactly_the_reason_the_predicate_expects() -> None:
    """Производитель причины и предикат отбора обязаны сходиться.

    🔴 ЧЕСТНО О ГРАНИЦАХ. Эта проверка читает ИСХОДНИК, а такие сторожа в проекте
    признаны слабыми. Поведенческую версию я писал дважды: через SQLite она падает
    на таблице `users` (там JSONB, на SQLite не поднимается), через заглушки —
    путь закрытия счёта не отрабатывает. По правилу двух попыток третьего захода
    нет; пробел записан, а не замолчан.

    Что она всё-таки ловит (проверено мутацией): переименование причины у ПОПЫТКИ.
    Первая версия и этого не ловила — искала строку, которая встречается в функции
    дважды, у попытки и у заказа, и переименование одной из них проходило мимо.
    Чего не ловит: согласованное переименование производителя и константы разом.
    Это и не поймать тестом — в боевой базе уже лежат строки со старым текстом.
    """
    produced = inspect.getsource(service._release_direct_terminal_invoice)
    expected = f"attempt.reconciliation_reason = f'{service.POOL_KEY_TERMINAL_PREFIX}{{normalized_status.lower()}}'"

    assert expected in produced, (
        'производитель причины разошёлся с ключом отбора: строка, которую пишет код, '
        'больше не признаётся предикатом воркера — и восемь строк, уже лежащих в боевой базе, осиротеют'
    )


def test_a_chargeback_freeze_is_not_a_pool_key() -> None:
    """Строгий `startswith`, а не `in`.

    `post_paid_provider_terminal:` — ДРУГАЯ причина: заморозка по чарджбэку
    (`app/services/payment/platega.py`). Принять её за ключ пула значит запретить
    записать откат постоплаты. Через выборку эту ошибку не увидеть — такую строку
    ни одна ветвь отбора не берёт, поэтому проверка отдельная и прибитая.
    """
    assert service.reason_keeps_attempt_in_pool('post_paid_provider_terminal:chargebacked') is False
    assert service.reason_keeps_attempt_in_pool('status_lookup:empty') is False
    assert service.reason_keeps_attempt_in_pool(None) is False
    assert service.reason_keeps_attempt_in_pool('provider_invoice_missing_or_elapsed_expiry') is True


@pytest.mark.asyncio
async def test_provider_exception_does_not_evict_the_row(swept) -> None:
    """Не ответить можно и исключением — это второй затиратель ключа.

    Найдено мутацией: сторож, проверяющий только пустой ответ, эту строку кода
    не исполняет вовсе и потому переживает её поломку.
    """
    sync_db, run = swept
    _seed(sync_db, attempt_id=46, status='failed', reason='provider_terminal:canceled')
    sync_db.commit()

    assert await run(TimeoutError('провайдер не ответил')) == ['provider-46']
    row = sync_db.get(CheckoutPaymentAttempt, 46)
    assert row.reconciliation_reason == 'provider_terminal:canceled', 'исключение при опросе затёрло ключ отбора'

    row.next_reconcile_at = PAST
    sync_db.commit()
    assert await run(TimeoutError('снова')) == ['provider-46'], 'после исключения строка выпала из следующего прохода'


@pytest.mark.asyncio
async def test_unknown_provider_status_does_not_evict_the_row(swept) -> None:
    """Третий затиратель: тело есть, статус неузнан.

    Его не заметил ни я, ни первый разбор — нашла атака на замысел. Тот же сбой
    провайдера, что отдаёт пустоту, отдаёт и частичные тела, поэтому вторая волна
    выпавших пришла бы с другой причиной и выглядела бы как новый баг.
    """
    sync_db, run = swept
    _seed(sync_db, attempt_id=47, status='failed', reason='provider_terminal:canceled')
    sync_db.commit()

    assert await run({'id': 'provider-47', 'error': 'gone'}) == ['provider-47']
    row = sync_db.get(CheckoutPaymentAttempt, 47)
    assert row.reconciliation_reason == 'provider_terminal:canceled', 'неузнанный статус затёр ключ отбора'

    row.next_reconcile_at = PAST
    sync_db.commit()
    assert await run({'id': 'provider-47', 'error': 'gone'}) == ['provider-47'], 'строка выпала из следующего прохода'

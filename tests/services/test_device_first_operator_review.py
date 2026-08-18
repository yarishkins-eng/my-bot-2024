"""Пункт 4.4. Разбор заказов оператором: список, возврат денег, закрытие.

🔴 Правило этапа 4.1: тесты на функции не доказывают, что функция ПОДКЛЮЧЕНА.
Поэтому здесь есть и проверки хелперов, и проверки через настоящую точку входа —
обработчики чат-админки и их регистрацию в диспетчере.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import device_first_checkout_service as service


def _checkout(**overrides):
    base = SimpleNamespace(
        id=101,
        public_id='87bf4880-3a89-4f52-be13-276854d4f8a0',
        user_id=186,
        tariff_id=3,
        period_days=90,
        selected_device_limit=2,
        tariff_total_kopeks=64900,
        lifecycle_state='operator_review',
        provisioning_state='not_started',
        terminal_reason='provider_invoice_missing_or_elapsed_expiry',
        funding_mode='platega',
        debit_transaction_id=None,
        created_subscription_id=None,
        sale_snapshot={'tariff_name': 'Базовый'},
        settlement_mode='direct_purchase_v2',
        updated_at=datetime(2026, 8, 16, 12, 59, tzinfo=UTC),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _healthy_owner():
    """Клиент, аккаунт которого никто не удаляет."""
    return SimpleNamespace(id=186, balance_kopeks=0, account_erased_at=None, account_erasure_requested_at=None)


def _db(*, ledger_hit=None, scalars=(), owner=..., locked_user=None):
    """Сессия с очередями ответов.

    `scalars` — очередь для `db.scalar` в порядке, в котором их спрашивает забор:
    решённая строка сверки → удержанные деньги → уже зачисленные.
    `owner` — что вернёт `db.get(User)` внутри забора удаляемых аккаунтов.
    `locked_user` — что вернёт `SELECT ... FOR UPDATE` в самом возврате.
    """
    db = MagicMock()
    ledger_result = MagicMock()
    ledger_result.scalar_one_or_none = MagicMock(return_value=ledger_hit)
    user_result = MagicMock()
    user_result.scalar_one_or_none = MagicMock(return_value=locked_user)
    db.execute = AsyncMock(side_effect=lambda stmt: user_result if 'users' in str(stmt).lower() else ledger_result)
    db.scalar = AsyncMock(side_effect=list(scalars))
    db.get = AsyncMock(return_value=_healthy_owner() if owner is ... else owner)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Сколько можно вернуть
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_chargebacked_order_is_never_refunded():
    """🔴 P0 ревью. Платёжка отозвала платёж и уже вернула деньги клиенту, а
    `credited_amount_kopeks` при этом НЕ обнуляется. Без этого забора кнопка отдала бы
    ту же сумму второй раз — и это при том, что вердикт в той же карточке пишет
    «возврат делать НЕ нужно»."""
    db = _db()
    amount, refusal = await service._refundable_amount_kopeks(
        db, _checkout(terminal_reason='post_paid_provider_terminal:chargebacked')
    )
    assert amount == 0
    assert 'отозван платёжной системой' in refusal


@pytest.mark.asyncio
async def test_an_order_whose_subscription_exists_is_never_refunded():
    """🔴 P0 ревью. `debit_transaction_id` и `created_subscription_id` присваиваются
    ОДНИМ блоком, сразу после создания подписки. Значит возврат по такому заказу
    сделал бы уже выданную подписку бесплатной."""
    db = _db()
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout(created_subscription_id=55))
    assert amount == 0
    assert 'уже создана' in refusal


@pytest.mark.asyncio
async def test_a_wallet_order_with_a_debit_always_has_a_subscription():
    """Кошельковая ветка недостижима иначе: списание ставится тем же блоком, что и
    подписка. Поэтому забор «подписка создана» обязан сработать РАНЬШЕ неё."""
    db = _db()
    amount, refusal = await service._refundable_amount_kopeks(
        db, _checkout(funding_mode='wallet', debit_transaction_id=7, created_subscription_id=55)
    )
    assert amount == 0
    assert 'уже создана' in refusal


@pytest.mark.asyncio
async def test_a_wallet_order_without_a_debit_has_nothing_to_return():
    db = _db()
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout(funding_mode='wallet'))
    assert amount == 0
    assert 'ничего не списывали' in refusal


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['account_erased_at', 'account_erasure_requested_at'])
async def test_a_closing_account_is_never_credited(field):
    """🔴 P1 ревью. Забор в платёжном сервисе ставит ровно ту пару признаков, по которой
    мы считаем сумму, и требует обратного: «reviewable, never creditable»."""
    owner = _healthy_owner()
    setattr(owner, field, datetime(2026, 8, 1, tzinfo=UTC))
    db = _db(owner=owner)
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'аккаунт клиента удаляется' in refusal


@pytest.mark.asyncio
async def test_a_decision_already_recorded_in_the_cabinet_stops_the_payout():
    """🔴 Мина L. Оператор мог вернуть деньги руками в Platega и записать это решение."""
    db = _db(scalars=[1])
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'решение оператора в кабинете' in refusal


@pytest.mark.asyncio
async def test_money_already_on_the_balance_is_never_refunded_twice():
    db = _db(scalars=[0, None, 1])
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'уже вернулись' in refusal


@pytest.mark.asyncio
async def test_provider_money_held_for_review_is_refundable():
    db = _db(scalars=[0, 64900])
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout())
    assert (amount, refusal) == (64900, '')


@pytest.mark.asyncio
async def test_without_proof_of_payment_the_operator_is_sent_to_platega():
    """Нельзя возвращать «по прайсу»: клиент мог заплатить другую сумму."""
    db = _db(scalars=[0, None, None])
    amount, refusal = await service._refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'Platega' in refusal


@pytest.mark.asyncio
async def test_the_refundable_query_asks_only_about_held_money():
    """Сторож формы запроса: без фильтра по статусу вернули бы чужие деньги.

    Проверяется скомпилированный SQL, а не константа рядом с ним (урок этапа 4.1).
    """
    db = _db(scalars=[0, 64900])
    await service._refundable_amount_kopeks(db, _checkout())
    compiled = str(db.scalar.await_args[0][0].compile(compile_kwargs={'literal_binds': True}))
    assert 'operator_review' in compiled
    assert 'credited_amount_kopeks > 0' in compiled
    assert 'checkout_id = 101' in compiled


# ---------------------------------------------------------------------------
# Возврат
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_press_never_pays_twice():
    """Идемпотентность строится на ключе книги: Telegram шлёт колбэки не по разу."""
    db = _db(ledger_hit=SimpleNamespace(amount_kopeks=24900))
    done, message = await service.refund_operator_review_checkout(db, checkout=_checkout(), admin_user_id=1)
    assert done is False
    assert 'уже сделан' in message
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_button_never_refunds_a_moved_on_order():
    """🔴 P1 ревью. Инлайн-кнопка живёт в переписке вечно; у закрытия сторож был, у
    возврата не было вовсе."""
    db = _db()
    done, message = await service.refund_operator_review_checkout(
        db, checkout=_checkout(lifecycle_state='ready'), admin_user_id=1
    )
    assert done is False
    assert 'уже не на разборе' in message
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_credits_the_balance_and_notifies_without_paying_commission(monkeypatch):
    """🔴 Мина U — уведомление через аутбокс. И 🔴 P1 ревью — комиссия с ВОЗВРАТА не платится."""
    user = SimpleNamespace(id=186, balance_kopeks=0)
    db = _db(scalars=[0, 64900], locked_user=user)
    job = SimpleNamespace(id=1, referral_status='pending')

    async def fake_outbox(db_, *, transaction_id, checkout_id):
        return job

    monkeypatch.setattr(service, 'ensure_deposit_outbox', fake_outbox)
    done, _ = await service.refund_operator_review_checkout(db, checkout=_checkout(), admin_user_id=1)

    assert done is True
    assert user.balance_kopeks == 64900
    # Комиссию раздаёт шаг, который воркер пропускает только при `done`.
    assert job.referral_status == 'done'
    db.commit.assert_awaited()
    added = db.add.call_args[0][0]
    assert added.device_first_ledger_key == 'operator_review_refund:101'


@pytest.mark.asyncio
async def test_refund_locks_the_client_row_before_touching_the_balance(monkeypatch):
    """🔴 P1 ревью. `+=` по балансу — read-modify-write; без блокировки параллельное
    пополнение в этом окне теряется целиком."""
    user = SimpleNamespace(id=186, balance_kopeks=0)
    db = _db(scalars=[0, 64900], locked_user=user)
    monkeypatch.setattr(service, 'ensure_deposit_outbox', AsyncMock(return_value=SimpleNamespace(referral_status='')))
    await service.refund_operator_review_checkout(db, checkout=_checkout(), admin_user_id=1)
    user_statements = [str(call.args[0]) for call in db.execute.await_args_list if 'users' in str(call.args[0]).lower()]
    assert user_statements, 'строка клиента вообще не читалась запросом'
    assert 'FOR UPDATE' in user_statements[0]


@pytest.mark.asyncio
async def test_refund_marks_the_attempt_so_a_repeat_webhook_cannot_pay_again(monkeypatch):
    """🔴 P0 ревью. У возврата поздних денег СВОЙ ключ книги, и про наш он не знает.
    Провайдер повторяет то же подтверждение — без метки сумма ушла бы дважды."""
    db = _db(scalars=[0, 64900], locked_user=SimpleNamespace(id=186, balance_kopeks=0))
    monkeypatch.setattr(service, 'ensure_deposit_outbox', AsyncMock(return_value=SimpleNamespace(referral_status='')))
    await service.refund_operator_review_checkout(db, checkout=_checkout(), admin_user_id=1)
    updates = [str(call.args[0]) for call in db.execute.await_args_list if 'UPDATE' in str(call.args[0])]
    assert any('checkout_payment_attempts' in stmt for stmt in updates), 'попытка не помечена'
    assert any('device_first_reconciliation_credits' in stmt for stmt in updates), 'строка сверки не закрыта'


@pytest.mark.asyncio
async def test_refund_without_provable_money_never_touches_the_balance(monkeypatch):
    """Отрицательный сценарий: нет доказательства — нет движения денег."""
    user = SimpleNamespace(id=186, balance_kopeks=500)
    db = _db(scalars=[0, None, None], locked_user=user)
    monkeypatch.setattr(service, 'ensure_deposit_outbox', AsyncMock())
    done, _ = await service.refund_operator_review_checkout(db, checkout=_checkout(), admin_user_id=1)
    assert done is False
    assert user.balance_kopeks == 500
    db.commit.assert_not_awaited()
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Закрытие
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_moves_the_order_to_cancelled():
    checkout = _checkout()
    done, _ = await service.close_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
    assert done is True
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == service.OPERATOR_CLOSED_TERMINAL_REASON


@pytest.mark.asyncio
async def test_closing_an_already_closed_order_changes_nothing():
    checkout = _checkout(lifecycle_state='ready')
    db = _db()
    done, message = await service.close_operator_review_checkout(db, checkout=checkout, admin_user_id=1)
    assert done is False
    assert 'уже не на разборе' in message
    assert checkout.lifecycle_state == 'ready'
    db.commit.assert_not_awaited()


def test_the_operator_reason_is_never_called_proof_of_no_money():
    """🔴 Оператор закрывает и оплаченные заказы. Сказать за него «списания не было» —
    ровно та ложь, которую снимал пункт 4.2б."""
    assert service.OPERATOR_CLOSED_TERMINAL_REASON not in service._NO_MONEY_TERMINAL_REASONS


def test_the_operator_reason_is_a_literal_not_a_moving_target():
    """Имя причины зашито литералом: сторож, ссылающийся на ту же константу, что и код,
    ничего не проверяет (урок этапа 4.1). Ниже — настоящая поведенческая проверка."""
    assert service.OPERATOR_CLOSED_TERMINAL_REASON == 'cancelled_by_operator_review'


# ---------------------------------------------------------------------------
# Через настоящую точку входа
# ---------------------------------------------------------------------------


def test_every_button_is_actually_registered():
    """Мутация `return ()` в регистраторе выключала бы весь этап при зелёных тестах."""
    from aiogram import Dispatcher

    from app.handlers.admin import orders_review

    dp = Dispatcher()
    orders_review.register_handlers(dp)
    assert len(dp.callback_query.handlers) == 4


@pytest.mark.asyncio
async def test_the_card_shows_the_live_order_not_a_snapshot():
    """🔴 Мина M: тревога в чате — снимок момента, карточка обязана собираться заново."""
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=186, telegram_id=1, username=None, full_name='Tiger'))
    db.scalar = AsyncMock(return_value=None)
    text = await service.operator_review_card(db, _checkout())
    assert 'Заказы на разборе' in text
    assert 'Деньги' in text


def test_the_list_only_takes_orders_waiting_for_a_human():
    """Сторож формы запроса: без фильтра список утянул бы все заказы подряд."""
    import inspect
    import re

    source = inspect.getsource(service.list_operator_review_checkouts)
    assert re.search(r"lifecycle_state\s*==\s*'operator_review'", source)

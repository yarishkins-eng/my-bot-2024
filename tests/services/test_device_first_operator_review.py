"""Пункт 4.4. Разбор заказов оператором: список, возврат денег, закрытие.

🔴 Правило этапа 4.1: тесты на функции не доказывают, что функция ПОДКЛЮЧЕНА.
Поэтому здесь есть и проверки хелперов, и проверки через настоящую точку входа —
обработчики чат-админки и их регистрацию в диспетчере.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def _get_by_model(checkout):
    """`db.get(SubscriptionCheckout, …)` → заказ, `db.get(User, …)` → живой клиент.

    Забор возврата спрашивает обоих, и общий `return_value` отдавал бы заказ вместо
    клиента — тест падал бы на несуществующем поле, а не на проверяемом поведении.
    """

    async def _get(model, _ident, **_kwargs):
        return checkout if getattr(model, '__name__', '') == 'SubscriptionCheckout' else _healthy_owner()

    return AsyncMock(side_effect=_get)


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
    amount, refusal = await service.refundable_amount_kopeks(
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
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout(created_subscription_id=55))
    assert amount == 0
    assert 'уже создана' in refusal


@pytest.mark.asyncio
async def test_a_wallet_order_with_a_debit_always_has_a_subscription():
    """Кошельковая ветка недостижима иначе: списание ставится тем же блоком, что и
    подписка. Поэтому забор «подписка создана» обязан сработать РАНЬШЕ неё."""
    db = _db()
    amount, refusal = await service.refundable_amount_kopeks(
        db, _checkout(funding_mode='wallet', debit_transaction_id=7, created_subscription_id=55)
    )
    assert amount == 0
    assert 'уже создана' in refusal


@pytest.mark.asyncio
async def test_a_wallet_order_without_a_debit_has_nothing_to_return():
    db = _db()
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout(funding_mode='wallet'))
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
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'аккаунт клиента удаляется' in refusal


@pytest.mark.asyncio
async def test_a_decision_already_recorded_in_the_cabinet_stops_the_payout():
    """🔴 Мина L. Оператор мог вернуть деньги руками в Platega и записать это решение."""
    db = _db(scalars=[1])
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'решение оператора в кабинете' in refusal


@pytest.mark.asyncio
async def test_money_already_on_the_balance_is_never_refunded_twice():
    db = _db(scalars=[0, None, 1])
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'уже вернулись' in refusal


@pytest.mark.asyncio
async def test_provider_money_held_for_review_is_refundable():
    db = _db(scalars=[0, 64900])
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout())
    assert (amount, refusal) == (64900, '')


@pytest.mark.asyncio
async def test_without_proof_of_payment_the_operator_is_sent_to_platega():
    """Нельзя возвращать «по прайсу»: клиент мог заплатить другую сумму."""
    db = _db(scalars=[0, None, None])
    amount, refusal = await service.refundable_amount_kopeks(db, _checkout())
    assert amount == 0
    assert 'Platega' in refusal


@pytest.mark.asyncio
async def test_the_refundable_query_asks_only_about_held_money():
    """Сторож формы запроса: без фильтра по статусу вернули бы чужие деньги.

    Проверяется скомпилированный SQL, а не константа рядом с ним (урок этапа 4.1).
    """
    db = _db(scalars=[0, 64900])
    await service.refundable_amount_kopeks(db, _checkout())
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
async def test_the_list_and_the_counter_agree_on_what_is_visible():
    """🔴 Скептик: оба замка списка пережили мутацию — сторожа на них не было вовсе.

    Проверяем СКОМПИЛИРОВАННЫЙ SQL обоих запросов. Расхождение списка и счётчика
    читалось как страничная навигация, которой нет, а в худшем случае давало
    «разбирать нечего» при удержанных деньгах живого клиента.
    """
    from sqlalchemy import func, select

    from app.database.models import SubscriptionCheckout, User

    conditions = service._operator_review_visible_conditions()
    sql = str(
        select(func.count(SubscriptionCheckout.id))
        .join(User, User.id == SubscriptionCheckout.user_id)
        .where(*conditions)
        .compile(compile_kwargs={'literal_binds': True})
    )
    # Замок 1: берём только то, что ждёт человека. Пункт 4.5 расширил набор с одного
    # состояния до четырёх — имена зашиты литералами, потому что сторож, читающий ту же
    # константу, что и код, доказывает лишь равенство константы самой себе.
    for state in ('operator_review', 'conflict', 'failed', 'reprice_required'):
        assert f"'{state}'" in sql
    # И ничего сверх этих четырёх: `cancelled`/`expired`/`ready` — уже разобранные заказы,
    # им в списке делать нечего, а `draft`/`awaiting_funds` — живые корзины клиентов.
    for alien in ('cancelled', 'expired', 'ready', 'draft', 'awaiting_funds', 'armed', 'fulfilling'):
        assert f"'{alien}'" not in sql
    # Замок 2: PII людей, подавших заявку на удаление, в админ-чат не уходит.
    assert 'account_erasure_requested_at IS NULL' in sql
    # ...но финализированные удаления остаются: их заказы надо закрывать.
    assert 'account_erased_at IS NOT NULL' in sql
    # Условий ровно два — третье добавили бы молча, и список разошёлся бы со счётчиком.
    assert len(conditions) == 2


def test_the_alert_stops_promising_a_section_that_will_not_have_the_order():
    """🔴 Скептик: ветвление текста тревоги пережило мутацию «всегда звать в разбор».

    Тревога рассылается по ДВУМ условиям, а список берёт только `operator_review`.
    Для застрявшей выдачи инструкция «идите в раздел разбора» — это повторение мины H.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    def _alert_db():
        db = MagicMock()
        db.get = AsyncMock(return_value=SimpleNamespace(id=186, telegram_id=1, username=None, full_name='Tiger'))
        db.scalar = AsyncMock(return_value=None)
        return db

    on_review = asyncio.run(service._owner_order_stuck_text(_alert_db(), _checkout()))
    assert 'Заказы на разборе' in on_review
    assert 'этот заказ' in on_review

    stuck = asyncio.run(
        service._owner_order_stuck_text(
            _alert_db(), _checkout(lifecycle_state='fulfilling', provisioning_state='retry')
        )
    )
    assert 'этого заказа не будет' in stuck
    assert 'Разбирать руками не нужно' in stuck


# ---------------------------------------------------------------------------
# Пункт 4.5. Тот же экран принимает ещё три состояния
#
# 🔴 Второй панели «закрыть заказ» не заводили намеренно (мина AK). Значит все проверки
# ниже — про ОДИН набор состояний, который спрашивают и видимость, и обе кнопки, и текст.
# ---------------------------------------------------------------------------

STOPPED_STATES = ('conflict', 'failed', 'reprice_required')
# Состояния, которых на экране быть НЕ должно. Литералами: сторож, читающий ту же
# константу, что и код, доказывает лишь равенство константы самой себе.
NOT_ON_THE_SCREEN = ('ready', 'cancelled', 'expired', 'draft', 'confirmed', 'awaiting_funds', 'armed', 'fulfilling')


@pytest.mark.parametrize('state', STOPPED_STATES)
@pytest.mark.asyncio
async def test_a_stopped_order_can_now_be_closed(state):
    checkout = _checkout(lifecycle_state=state)
    done, message = await service.close_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
    assert done is True
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == 'cancelled_by_operator_review'
    # 🔴 Обещание считается ДО мутации. После неё состояние уже `cancelled`, и ответ был
    # бы про закрытый заказ, а не про тот, который оператор закрывал.
    assert 'пробный период' in message


@pytest.mark.parametrize('state', NOT_ON_THE_SCREEN)
@pytest.mark.asyncio
async def test_nothing_else_can_be_closed_by_this_button(state):
    checkout = _checkout(lifecycle_state=state)
    done, message = await service.close_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
    assert done is False
    assert 'уже не на разборе' in message
    assert checkout.lifecycle_state == state


@pytest.mark.parametrize('state', NOT_ON_THE_SCREEN)
@pytest.mark.asyncio
async def test_the_refund_button_has_the_same_lock_as_closing(state):
    """🔴 План 4.5 про этот сторож забыл. Без него кнопка возврата рисовалась бы на новых
    состояниях и отказывала — ровно мёртвая кнопка, о которую проект спотыкался дважды."""
    checkout = _checkout(lifecycle_state=state)
    done, message = await service.refund_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
    assert done is False
    assert 'уже не на разборе' in message


@pytest.mark.parametrize('state', STOPPED_STATES)
@pytest.mark.asyncio
async def test_a_stopped_order_reaches_the_money_question_instead_of_a_flat_refusal(state):
    """На новых состояниях возврат обязан ДОЙТИ до вопроса про деньги и ответить по базе."""
    checkout = _checkout(lifecycle_state=state)
    db = _db(scalars=[0, 0, 0])
    done, message = await service.refund_operator_review_checkout(db, checkout=checkout, admin_user_id=1)
    assert done is False
    assert 'уже не на разборе' not in message
    assert 'нет подтверждённого списания' in message


# Причины, которые ставятся ТОЛЬКО клиенту с уже существующей подпиской: забор триала
# отбивает такого раньше, чем доходит до заказов, поэтому закрытие ему ничего не даёт.
REASONS_WITH_A_LIVE_SUBSCRIPTION = (
    'subscription_appeared',
    'target_subscription_changed',
    'device_limit_decrease_not_allowed',
)


def test_the_close_question_promises_exactly_what_closing_gives():
    """🔴 Три случая, а не два. Обещание одно на всех было бы ложью в двух из трёх."""
    assert service.operator_close_unblocks(_checkout()) == 'Клиент снова сможет оформить покупку.'
    stopped = service.operator_close_unblocks(_checkout(lifecycle_state='conflict', terminal_reason='quote_expired'))
    # 🔴 Оговорка обязательна: забор триала отбивает КАЖДОГО, у кого когда-либо была
    # подписка, раньше, чем доходит до заказов. Безусловное обещание было бы ложью для
    # всех, кто уже пользовался триалом, а не только для тех, чей заказ об него споткнулся.
    assert stopped == 'Клиент снова сможет взять пробный период, если ещё им не пользовался.'
    for reason in REASONS_WITH_A_LIVE_SUBSCRIPTION:
        nothing = service.operator_close_unblocks(_checkout(lifecycle_state='conflict', terminal_reason=reason))
        assert 'ничего не даст' in nothing
        assert 'пробный период' not in nothing


def _card(checkout) -> str:
    import asyncio

    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=186, telegram_id=1, username=None, full_name='Tiger'))
    db.scalar = AsyncMock(return_value=None)
    return asyncio.run(service._owner_order_stuck_text(db, checkout))


@pytest.mark.parametrize('state', STOPPED_STATES)
def test_the_card_of_a_stopped_order_stops_lying(state):
    """🔴 До пункта 4.5 карточка этих заказов уходила в ветку «застряла выдача» и печатала
    две неправды: «бот продолжает пробовать сам» и «в разделе разбора этого заказа не
    будет» — про заказ, открытый из этого самого раздела."""
    text = _card(_checkout(lifecycle_state=state))
    assert 'Бот продолжает пробовать сам' not in text
    assert 'этого заказа не будет' not in text
    assert 'Заказы на разборе' in text
    assert 'Сам он не продолжится' in text
    assert 'клиент не возьмёт пробный период' in text
    # И не зовёт выдавать подписку заново: заказ бывает продлевающим, VPN у клиента жив.
    assert 'VPN клиенту не выдан' not in text


@pytest.mark.parametrize(
    'reason',
    [
        # 🔴 Половина `operator_review` — та, где висят деньги живого клиента. Первая
        # правка её пропустила, и сторож не мог это поймать: он перечислял ровно те же
        # ключи, что и словарь. Здесь имена взяты из МЕСТ ПРИСВОЕНИЯ, а не из словаря.
        'subscription_appeared_after_payment',
        'target_subscription_changed_after_payment',
        'captured_entitlement_changed_after_payment',
        'invalid_sale_snapshot',
        'invalid_entitlement_snapshot',
        'entitlement_snapshot_hash_mismatch',
        'tariff_missing_after_quote',
        'provider_terminal_identity_mismatch',
        'provider_terminal_status_regressed',
        'provider_invoice_verification_mismatch',
        'provider_identity_binding_conflict',
        'direct_payment_attempt_mode_or_binding_mismatch',
        'no_entitlements_to_provision',
        'provider_invoice_missing_or_elapsed_expiry',
    ],
)
def test_every_reason_of_a_money_order_has_a_russian_name(reason):
    """🔴 На заказе с деньгами карточка обязана называть причину словами. Двенадцать из
    пятнадцати причин этой ветки остались без перевода после первой правки."""
    text = _card(_checkout(lifecycle_state='operator_review', terminal_reason=reason))
    assert 'причину видно только по коду' not in text, reason
    assert service._TERMINAL_REASON_RU[reason] in text


def test_every_reason_of_a_stopped_order_has_a_russian_name():
    """🔴 P1 ревью: словарь причин знал только `operator_review`, и карточка КАЖДОГО нового
    заказа печатала «причину видно только по коду». Экран, сделанный чтобы перестать врать,
    просто замолчал. Имена литералами — сторож, читающий тот же словарь, что и код, пуст."""
    for reason in (
        'quote_expired',
        'price_changed',
        'entitlement_changed',
        'tariff_no_longer_eligible',
        'subscription_appeared',
        'target_subscription_changed',
        'device_limit_decrease_not_allowed',
        'location_policy_not_sellable',
        'non_positive_quote',
        'entitlement_quote_missing_or_invalid',
        'payment_amount_mismatch',
    ):
        text = _card(_checkout(lifecycle_state='conflict', terminal_reason=reason))
        assert 'причину видно только по коду' not in text, reason
        assert service._TERMINAL_REASON_RU[reason] in text


def test_the_card_of_a_renewal_order_names_no_harm_that_does_not_exist():
    """Заказ остановлен потому, что подписка у клиента уже есть. Пробного периода он и так
    не получит — другим забором. Называть это вредом значит выдумать оператору беду."""
    text = _card(_checkout(lifecycle_state='conflict', terminal_reason='target_subscription_changed'))
    assert 'не возьмёт пробный период' not in text
    assert 'подписка у него уже есть' in text


@pytest.mark.asyncio
async def test_money_orders_are_never_pushed_off_the_screen_by_routine_ones():
    """🔴 P1 ревью: `reprice_required` — это рутина (протухший за 30 минут расчёт), её никто
    не закрывает, и она всегда свежее. Двадцати таких строк хватило бы, чтобы вытеснить за
    экран `operator_review` с удержанными деньгами, а листалки и поиска по номеру нет."""
    from sqlalchemy import select

    from app.database.models import SubscriptionCheckout, User

    db = MagicMock()
    rows = MagicMock()
    rows.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    db.execute = AsyncMock(return_value=rows)
    await service.list_operator_review_checkouts(db, limit=20)

    sql = str(db.execute.await_args[0][0].compile(compile_kwargs={'literal_binds': True}))
    order_by = sql.split('ORDER BY', 1)[1]
    # Признак «тут могут быть деньги» стоит ПЕРВЫМ ключом сортировки, свежесть — вторым.
    assert order_by.index("'operator_review'") < order_by.index('updated_at')
    # И это именно сортировка, а не фильтр: рутинные заказы с экрана не исчезают.
    where = sql.split('WHERE', 1)[1].split('ORDER BY', 1)[0]
    for state in ('conflict', 'failed', 'reprice_required'):
        assert f"'{state}'" in where
    del select, SubscriptionCheckout, User


@pytest.mark.asyncio
async def test_a_stale_confirmation_button_refuses_before_asking_anything():
    """🔴 P2 ревью: у вопроса сторожа состояния не было. Протухшая кнопка из переписки
    обещала «клиент снова сможет…» про заказ, закрытый неделю назад, и отказ приходил
    только ПОСЛЕ подтверждения."""
    from app.handlers.admin import orders_review

    checkout = _checkout(lifecycle_state='cancelled')
    screens = []
    callback = SimpleNamespace(
        data=f'{orders_review.ASK_PREFIX}close:{checkout.id}',
        message=SimpleNamespace(edit_text=AsyncMock(side_effect=lambda text, **_: screens.append(text))),
        answer=AsyncMock(),
    )
    db = _db()
    db.get = _get_by_model(checkout)

    await orders_review.ask_confirmation.__wrapped__.__wrapped__(callback, SimpleNamespace(id=1), db)

    assert 'уже разобран' in screens[0]
    assert 'снова сможет' not in screens[0]


@pytest.mark.asyncio
async def test_the_whole_path_works_for_a_stopped_order_through_the_real_buttons():
    """🔴 Приёмка пункта 4.5, собранная руками: на боевом таких заказов НОЛЬ.

    Счётчик «залипших» и так равен нулю, поэтому «стало ноль» не доказывает ничего.
    Собираем случай сами и ведём его теми же обработчиками, что и живой оператор:
    список → карточка → вопрос → закрытие. Урок 4.1: тест на функцию не доказывает,
    что функция ПОДКЛЮЧЕНА. Декораторы сняты намеренно — `@error_handler` проглотил бы
    падение и сделал тест зелёным на сломанном коде.
    """
    from app.handlers.admin import orders_review

    checkout = _checkout(lifecycle_state='conflict', terminal_reason='quote_expired')
    admin = SimpleNamespace(id=1)
    screens = []

    def _callback(data):
        message = SimpleNamespace(edit_text=AsyncMock(side_effect=lambda text, **_: screens.append(text)))
        return SimpleNamespace(data=data, message=message, answer=AsyncMock(), from_user=SimpleNamespace(id=1))

    db = _db(scalars=[1, 0, 0])
    db.get = _get_by_model(checkout)

    with (
        patch.object(orders_review, 'list_operator_review_checkouts', AsyncMock(return_value=[checkout])),
        patch.object(orders_review, 'count_operator_review_checkouts', AsyncMock(return_value=1)),
        patch.object(orders_review, 'operator_review_card', AsyncMock(return_value='карточка заказа')),
    ):
        await orders_review.show_orders_review_list.__wrapped__.__wrapped__(_callback('x'), admin, db)
        await orders_review.show_order_card.__wrapped__.__wrapped__(
            _callback(f'{orders_review.CARD_PREFIX}{checkout.id}'), admin, db
        )
        await orders_review.ask_confirmation.__wrapped__.__wrapped__(
            _callback(f'{orders_review.ASK_PREFIX}close:{checkout.id}'), admin, db
        )
        await orders_review.do_action.__wrapped__.__wrapped__(
            _callback(f'{orders_review.GO_PREFIX}close:{checkout.id}'), admin, db
        )

    # Заказ дошёл до списка — до пункта 4.5 он туда не попадал вовсе — и отличим от денежного.
    assert 'Заказы на разборе: 1' in screens[0]
    # Карточка открылась, а не отбилась «этот заказ уже разобран».
    assert screens[1] == 'карточка заказа'
    # Вопрос обещает ровно то, что закрытие даёт ИМЕННО ЭТОМУ заказу.
    assert 'снова сможет взять пробный период' in screens[2]
    assert 'оформить покупку' not in screens[2]
    # И заказ действительно закрыт — существующей причиной, без новой (мины K, AM, Y).
    assert screens[3].startswith('✅')
    assert checkout.lifecycle_state == 'cancelled'
    assert checkout.terminal_reason == 'cancelled_by_operator_review'


# ---------------------------------------------------------------------------
# Сторожа на мутации, пережившие набор во второй волне ревью.
# Каждый ниже проверен так: сломать код → убедиться, что покраснел → починить.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_counter_and_the_list_ask_the_same_question():
    """🔴 Мутация 30 скептика пережила ВЕСЬ набор: счётчик можно было сузить до
    `operator_review`, и экран написал бы «Заказы на разборе: 0» над непустым списком.

    Это ровно тот P0, который закрывал пункт 4.4, и комментарий над константой утверждал,
    что он сторожится. Не сторожился: прежний тест строил запрос из функции условий сам,
    а не проверял, с каким набором её зовут список и счётчик.
    """
    from app.database.models import SubscriptionCheckout, User

    seen = []

    def _capture(stmt):
        seen.append(str(stmt.compile(compile_kwargs={'literal_binds': True})))
        rows = MagicMock()
        rows.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return rows

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_capture)
    db.scalar = AsyncMock(
        side_effect=lambda stmt: seen.append(str(stmt.compile(compile_kwargs={'literal_binds': True})))
    )

    await service.list_operator_review_checkouts(db, limit=20)
    await service.count_operator_review_checkouts(db)

    assert len(seen) == 2
    for state in ('operator_review', 'conflict', 'failed', 'reprice_required'):
        assert f"'{state}'" in seen[0], f'список потерял {state}'
        assert f"'{state}'" in seen[1], f'счётчик потерял {state}'
    del SubscriptionCheckout, User


@pytest.mark.asyncio
async def test_the_answer_after_closing_is_a_whole_sentence_not_a_glued_one():
    """🔴 Регрессия починки первой волны, нашли скептик и прогон сценария.

    Вспомогательная функция стала возвращать законченные предложения, а шаблон ответа
    остался прежним — владелец читал «…не мешает клиенту Клиент снова сможет…».
    """
    for state, reason in (('operator_review', 'no_entitlements_to_provision'), ('conflict', 'quote_expired')):
        checkout = _checkout(lifecycle_state=state, terminal_reason=reason)
        _done, message = await service.close_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
        assert 'клиенту Клиент' not in message
        assert 'клиенту Клиенту' not in message
        assert '..' not in message
        # И заказ, закрытие которого клиенту ничего не даёт, не утверждает обратного.
        assert not (message.count('не мешает') and 'ничего не даст' in message)


@pytest.mark.asyncio
async def test_the_promise_is_computed_before_the_order_is_changed():
    """🔴 Мутация 14 пережила набор: перенос строки под мутацию состояния проходил молча.

    Ущерб денежный: заказ на разборе после закрытия становится `cancelled`, и ответ
    оператору обещал бы пробный период вместо снятой блокировки покупки.
    """
    checkout = _checkout(lifecycle_state='operator_review')
    _done, message = await service.close_operator_review_checkout(_db(), checkout=checkout, admin_user_id=1)
    assert 'оформить покупку' in message
    assert 'пробный период' not in message


@pytest.mark.asyncio
async def test_the_close_screen_names_the_money_before_the_irreversible_press():
    """🔴 P0 критика полноты. Пункт 4.5 сделал «Закрыть» рутинной кнопкой, а вопрос про
    деньги она не задавала — при том что соседняя кнопка эту цифру уже считает."""
    from app.handlers.admin import orders_review

    checkout = _checkout()
    screens = []

    def _callback():
        return SimpleNamespace(
            data=f'{orders_review.ASK_PREFIX}close:{checkout.id}',
            message=SimpleNamespace(edit_text=AsyncMock(side_effect=lambda text, **_: screens.append(text))),
            answer=AsyncMock(),
        )

    # Забор отвечает «удержано 649 ₽»: решённых строк сверки нет, деньги на попытке есть.
    db = _db(scalars=[0, 64900])
    db.get = _get_by_model(checkout)
    await orders_review.ask_confirmation.__wrapped__.__wrapped__(_callback(), SimpleNamespace(id=1), db)
    assert '649' in screens[0]
    assert 'верните их ДО закрытия' in screens[0]

    # А на заказе без денег — прямой ответ вместо прежнего «верните, если они были».
    screens.clear()
    empty = _checkout(lifecycle_state='conflict', terminal_reason='quote_expired')
    db = _db(scalars=[0, None, None])
    db.get = _get_by_model(empty)
    await orders_review.ask_confirmation.__wrapped__.__wrapped__(_callback(), SimpleNamespace(id=1), db)
    assert 'Возвращать по нему нечего' in screens[0]
    assert 'верните' not in screens[0].lower()


def test_the_list_row_marks_money_orders_apart():
    """🔴 Мутации 13 и 13b пережили набор: значок можно было убрать целиком ИЛИ поменять
    местами. Прежний сторож смотрел на текст сообщения, а значок живёт в подписи кнопки."""
    import asyncio

    from app.handlers.admin import orders_review

    money = _checkout(lifecycle_state='operator_review')
    routine = _checkout(lifecycle_state='conflict', terminal_reason='quote_expired')
    routine.id = 102
    captured = {}

    callback = SimpleNamespace(
        data='x',
        message=SimpleNamespace(edit_text=AsyncMock(side_effect=lambda text, **kw: captured.update(kw))),
        answer=AsyncMock(),
    )
    with (
        patch.object(orders_review, 'list_operator_review_checkouts', AsyncMock(return_value=[money, routine])),
        patch.object(orders_review, 'count_operator_review_checkouts', AsyncMock(return_value=2)),
    ):
        asyncio.run(
            orders_review.show_orders_review_list.__wrapped__.__wrapped__(callback, SimpleNamespace(id=1), _db())
        )

    labels = [row[0].text for row in captured['reply_markup'].inline_keyboard]
    assert labels[0].startswith('💰'), labels
    assert labels[1].startswith('⏸'), labels


def test_the_refund_hint_promises_no_lock_that_does_not_exist():
    """🔴 Мутация 20 пережила набор: возврат прежней фразы «это снимет замок с клиента»
    проходил молча, хотя у остановившихся заказов никакого замка на покупку нет."""
    from app.handlers.admin import orders_review

    assert 'замок' not in orders_review.ACTIONS['refund']['after']

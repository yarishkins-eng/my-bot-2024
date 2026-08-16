"""Этап 4.2б: клиенту не врать «Платёж получен» там, где он не платил.

Проверяется не «текст поменялся», а три вещи, каждая из которых уже стоила живого клиента:

1. **Порядок вопросов в классификаторе.** Факты о деньгах спрашиваются ДО `terminal_reason`.
   Обратный порядок скажет «денег не было» при поздней оплате: она деньги приносит, а причину
   заказа оставляет прежней (`device_first_payment_service.py:2002-2020`).
2. **Согласие с вердиктом владельцу.** `_money_verdict` и `checkout_money_state` отвечают на
   один и тот же вопрос из одних и тех же фактов. Разойдутся — клиент и владелец увидят про
   один заказ разное, ровно как до этого этапа.
3. **Ни одна ветка экрана не зовёт оформить заказ заново.** Запрет живёт в коде
   (`operator_hold`, `device_first_checkout_service.py:631-646`), и триал закрыт тем же
   замком. Приглашение туда — новый замкнутый круг, а не починка.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service_module
from app.services.device_first_checkout_service import checkout_money_state


def _checkout(**overrides):
    """Заказ 34 (`chuda27`) с боевого: брошенная корзина, денег не списано."""
    base = {
        'id': 34,
        'public_id': 'f441ecd8-1799-4666-bbf3-af8f5992b236',
        'user_id': 196,
        'lifecycle_state': 'operator_review',
        'terminal_reason': 'provider_invoice_missing_or_elapsed_expiry',
        'funding_mode': 'platega',
        'debit_transaction_id': None,
        'settlement_mode': 'direct_purchase_v2',
        'updated_at': datetime(2026, 8, 16, 17, 40, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _db(credited_attempts: int = 0):
    db = MagicMock()
    db.scalar = AsyncMock(return_value=credited_attempts)
    return db


# --- классификатор: порядок вопросов ---------------------------------------------------


@pytest.mark.asyncio
async def test_expired_unpaid_invoice_is_reported_as_no_money():
    """Живой случай, ради которого сделан этап: заказ 34, зачислено 0, списаний нет."""
    assert await checkout_money_state(_db(), _checkout()) == 'no_money'


@pytest.mark.asyncio
async def test_late_payment_wins_over_the_no_money_reason():
    """🔴 Главный тест файла. Причина осталась «счёт просрочен», а деньги пришли позже.

    Классификатор, спрашивающий причину первой, скажет клиенту «денег не списано» ровно
    тогда, когда деньги у нас, — и человек заплатит второй раз.
    """
    assert await checkout_money_state(_db(credited_attempts=1), _checkout()) == 'money_in_flight'


@pytest.mark.asyncio
async def test_balance_debit_is_money_even_without_a_payment_attempt():
    """Оплата с баланса не создаёт `CheckoutPaymentAttempt` вовсе — судить можно только по списанию."""
    checkout = _checkout(funding_mode='wallet', debit_transaction_id=77)
    assert await checkout_money_state(_db(), checkout) == 'money_in_flight'


@pytest.mark.asyncio
async def test_wallet_without_a_debit_is_no_money():
    checkout = _checkout(funding_mode='wallet', terminal_reason='entitlement_changed')
    assert await checkout_money_state(_db(), checkout) == 'no_money'


@pytest.mark.asyncio
async def test_card_debit_field_alone_is_enough_to_hold_the_claim():
    """`debit_transaction_id` заполняется при ОБОИХ способах оплаты — молчать про него нельзя."""
    checkout = _checkout(debit_transaction_id=77)
    assert await checkout_money_state(_db(), checkout) == 'money_in_flight'


@pytest.mark.asyncio
async def test_unrecognised_reason_stays_unknown_instead_of_guessing():
    """Пять архивных заказов тарифа 3: денег в базе не видно, но причина не из безденежных."""
    checkout = _checkout(terminal_reason='direct_payment_binding_mismatch')
    assert await checkout_money_state(_db(), checkout) == 'unknown'


@pytest.mark.asyncio
async def test_every_no_money_reason_is_actually_classified():
    for reason in service_module._NO_MONEY_TERMINAL_REASONS:
        assert await checkout_money_state(_db(), _checkout(terminal_reason=reason)) == 'no_money'


# --- сторож: клиент и владелец обязаны видеть про заказ одно и то же -------------------


@pytest.mark.asyncio
async def test_money_state_agrees_with_owner_verdict():
    """Сторож от расхождения двух ответов о деньгах.

    `_money_verdict` пишет владельцу, `checkout_money_state` — клиенту. Один заказ обязан
    выглядеть одинаково с обеих сторон: иначе владелец возвращает деньги, которых не брали,
    либо отказывает в возврате тому, кто заплатил.
    """
    cases = [
        # (заказ, зачисленных попыток)
        (_checkout(), 0),
        (_checkout(), 1),
        (_checkout(funding_mode='wallet', debit_transaction_id=77), 0),
        (_checkout(funding_mode='wallet', terminal_reason='entitlement_changed'), 0),
        (_checkout(debit_transaction_id=77), 0),
        (_checkout(terminal_reason='direct_payment_binding_mismatch'), 0),
        *[(_checkout(terminal_reason=reason), 0) for reason in service_module._NO_MONEY_TERMINAL_REASONS],
    ]
    for checkout, credited in cases:
        state = await checkout_money_state(_db(credited_attempts=credited), checkout)
        # `_money_verdict` спрашивает базу дважды и первым вопросом отделяет «уже на балансе».
        verdict_db = MagicMock()
        verdict_db.scalar = AsyncMock(return_value=credited)
        verdict = await service_module._money_verdict(verdict_db, checkout)
        claims_money = '🔴' in verdict or '🟡' in verdict
        claims_no_money = '🟢' in verdict
        if state == 'money_in_flight':
            assert claims_money, f'клиенту сказали «деньги есть», владельцу — «{verdict}»'
        if state == 'no_money':
            assert claims_no_money, f'клиенту сказали «денег нет», владельцу — «{verdict}»'


# --- экран заказа в боте ---------------------------------------------------------------


async def _screen(money_state: str):
    from app.handlers.subscription.device_first import _render_checkout

    callback = SimpleNamespace()
    user = SimpleNamespace(id=196, language='ru', balance_kopeks=0)
    with (
        patch(
            'app.handlers.subscription.device_first.serialize_checkout',
            MagicMock(return_value={'ui_state': 'operator_review'}),
        ),
        patch(
            'app.handlers.subscription.device_first.checkout_money_state',
            AsyncMock(return_value=money_state),
        ),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as output,
    ):
        await _render_checkout(callback, user, AsyncMock(), _checkout())
    return (
        output.await_args.kwargs['caption'],
        output.await_args.kwargs['keyboard'].inline_keyboard,
    )


@pytest.mark.asyncio
async def test_unpaid_customer_is_not_told_that_the_payment_arrived():
    caption, _ = await _screen('no_money')
    assert 'Платёж получен' not in caption
    assert 'Деньги не списаны' in caption


@pytest.mark.asyncio
async def test_paid_customer_still_reads_the_warning_against_paying_twice():
    caption, _ = await _screen('money_in_flight')
    assert 'Платёж получен' in caption
    assert 'не оплачивайте повторно' in caption


@pytest.mark.asyncio
async def test_unknown_money_state_claims_nothing_in_either_direction():
    caption, _ = await _screen('unknown')
    assert 'Платёж получен' not in caption
    assert 'Деньги не списаны' not in caption


@pytest.mark.asyncio
async def test_no_branch_invites_the_customer_into_a_refusal():
    """Запрет купить снова снимают мина F и 4.4. До тех пор кнопки «Начать заново» тут нет."""
    for money_state in ('no_money', 'money_in_flight', 'unknown'):
        caption, keyboard = await _screen(money_state)
        assert 'df:start' not in [button.callback_data for row in keyboard for button in row]
        assert 'создайте новый заказ' not in caption.lower()
        assert 'оформите' not in caption.lower()


# --- то же поле для кабинета: одна точка на все клиентские эндпоинты -------------------


async def _cabinet_payload(ui_state: str):
    from app.cabinet.routes import device_first as routes

    db = MagicMock()
    db.scalar = AsyncMock(return_value=0)
    with patch.object(routes, 'serialize_checkout', MagicMock(return_value={'ui_state': ui_state})):
        return await routes._serialize_cabinet_checkout(db, _checkout(), balance_kopeks=0)


@pytest.mark.asyncio
async def test_cabinet_gets_the_verdict_on_the_review_screen():
    """Считает бэкенд: ветка «по `terminal_reason`» на фронте при поздней оплате соврёт."""
    assert (await _cabinet_payload('operator_review'))['money_state'] == 'no_money'


@pytest.mark.asyncio
async def test_cabinet_pays_nothing_for_the_field_on_other_screens():
    """Лишний запрос на каждый экран покупки не нужен — поле только там, где про деньги говорят."""
    assert 'money_state' not in await _cabinet_payload('awaiting_payment')


# --- отказ в момент, когда человек пытается заплатить ----------------------------------


def test_operator_review_refusal_no_longer_invites_a_retry():
    """Без своей строки код падал в общий текст «попробуйте ещё раз» — приглашение в цикл."""
    from app.handlers.subscription.device_first import _safe_error_detail
    from app.services.device_first_checkout_service import DeviceFirstError

    user = SimpleNamespace(language='ru')
    message = _safe_error_detail(user, DeviceFirstError('operator_review_required', 'x'))
    assert 'Попробуйте ещё раз' not in message
    assert 'новый расчёт' not in message
    assert 'проверяется' in message

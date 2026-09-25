"""ВК-1: дата «до», которую заказ обещает клиенту, не больше той, что выдаст оплата.

Выдача (`crud/subscription.py::extend_subscription`) при смене тарифа с пробного сжигает остаток пробного, пока
перенос запрещён: срок = момент оплаты + период. Окно оплаты кабинета читает `estimated_end_at` и прибавляло период
к концу пробного — клиент видел дату на 1–3 дня позже, чем получал («до» ВК-1: 8 из 11 за 30 дней). Бот эту дату
новым заказам не показывает: с 02.08 заказ `direct_purchase_v2` идёт мимо экрана «Проверьте заказ». Числа в фикстурах
нарочно не круглые и не совпадают с умолчаниями кода.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import device_first as cabinet_routes
from app.services import device_first_checkout_service as service


CREATED_AT = datetime(2026, 9, 24, 21, 17, 43, tzinfo=UTC)  # 25.09 00:17 МСК
TRIAL_END = CREATED_AT + timedelta(days=2, hours=7, minutes=11)
PAID_TARIFF_ID = 3
TRIAL_TARIFF_ID = 5
PERIOD_DAYS = 90


def _checkout(target_snapshot, *, tariff_id=PAID_TARIFF_ID, period_days=PERIOD_DAYS, fulfilled_end_at=None):
    return SimpleNamespace(
        id=4_917,
        public_id='vk1-checkout',
        tariff_id=tariff_id,
        target_subscription_id=target_snapshot.get('id'),
        period_days=period_days,
        selected_device_limit=2,
        price_breakdown={},
        quoted_price_kopeks=39_900,
        max_price_kopeks=39_900,
        settlement_mode='direct_purchase_v2',
        tariff_total_kopeks=39_900,
        wallet_applied_kopeks=0,
        external_payable_kopeks=39_900,
        funding_mode='platega',
        quote_expires_at=CREATED_AT + timedelta(minutes=30),
        expires_at=CREATED_AT + timedelta(hours=24),
        lifecycle_state='armed',
        quote_state='valid',
        funding_state='invoice_pending',
        fulfillment_state='not_started',
        provisioning_state='not_started',
        terminal_reason=None,
        created_subscription_id=None,
        target_snapshot=target_snapshot,
        created_at=CREATED_AT,
        fulfilled_end_at=fulfilled_end_at,
    )


def _snapshot(*, is_trial=True, tariff_id=TRIAL_TARIFF_ID, end=TRIAL_END, **extra):
    snapshot = {
        'id': 7_301,
        'tariff_id': tariff_id,
        'status': 'active',
        'is_trial': is_trial,
        'device_limit': 1,
        'end_date': end.isoformat(),
    }
    snapshot.update(extra)
    return snapshot


def _promised(checkout) -> datetime:
    return datetime.fromisoformat(service.serialize_checkout(checkout)['estimated_end_at'])


@pytest.fixture
def live_flags(monkeypatch):
    """Флаги как на боевом 25.09.2026: перенос включён в базе, сброс — в окружении (сброс перебивает)."""
    monkeypatch.setattr(service.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(service.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', True)


def test_trial_to_paid_tariff_promises_date_from_order_day(live_flags):
    assert _promised(_checkout(_snapshot())) == CREATED_AT + timedelta(days=PERIOD_DAYS)


def test_friends_trial_flag_until_2031_is_not_promised_to_paid_tariff(live_flags):
    """Team с флагом «пробная» до 2031 года: выдача сжигает и его остаток — обещать 2031+ нельзя."""
    snapshot = _snapshot(tariff_id=4, end=datetime(2031, 6, 1, 9, 0, tzinfo=UTC))

    assert _promised(_checkout(snapshot)) == CREATED_AT + timedelta(days=PERIOD_DAYS)


@pytest.mark.parametrize(
    ('trial_add', 'reset'),
    [(False, True), (False, False)],
)
def test_other_flag_states_without_carry_also_burn_the_remainder(monkeypatch, trial_add, reset):
    monkeypatch.setattr(service.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', trial_add)
    monkeypatch.setattr(service.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', reset)

    assert _promised(_checkout(_snapshot())) == CREATED_AT + timedelta(days=PERIOD_DAYS)


def test_allowed_carry_keeps_trial_remainder_in_the_promise(monkeypatch):
    """Обратная ветка флага: перенос разрешён и сброс выключен — выдача прибавит остаток, экран тоже."""
    monkeypatch.setattr(service.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', True)
    monkeypatch.setattr(service.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', False)

    assert _promised(_checkout(_snapshot())) == TRIAL_END + timedelta(days=PERIOD_DAYS)


def test_trial_on_the_same_tariff_renews_with_its_remainder(live_flags):
    """Пробный на том же тарифе (подписка 75 на тарифе 3) выдача продлевает от конца срока — обещание прежнее."""
    snapshot = _snapshot(tariff_id=PAID_TARIFF_ID)

    assert _promised(_checkout(snapshot)) == TRIAL_END + timedelta(days=PERIOD_DAYS)


def test_paid_subscription_renewal_is_unchanged(live_flags):
    snapshot = _snapshot(is_trial=False, tariff_id=PAID_TARIFF_ID)

    assert _promised(_checkout(snapshot)) == TRIAL_END + timedelta(days=PERIOD_DAYS)


def test_snapshot_without_trial_flag_is_not_guessed(live_flags):
    """Старый снимок без `is_trial`: не угадываем — прежняя формула."""
    snapshot = _snapshot()
    del snapshot['is_trial']

    assert _promised(_checkout(snapshot)) == TRIAL_END + timedelta(days=PERIOD_DAYS)


def test_trial_flag_must_be_a_real_boolean(live_flags):
    """Снимок пишет `bool(is_trial)`; строка «true» — порча, а не пробный. Как и `current_subscription_is_trial`."""
    snapshot = _snapshot(is_trial='true')

    assert _promised(_checkout(snapshot)) == TRIAL_END + timedelta(days=PERIOD_DAYS)


def test_paid_term_ending_minutes_after_the_order_is_still_the_base(live_flags):
    """Прежняя формула на границе: платный срок, кончающийся через 10 минут после заказа, — база продления."""
    end = CREATED_AT + timedelta(minutes=10, seconds=7)
    snapshot = _snapshot(is_trial=False, tariff_id=PAID_TARIFF_ID, end=end)

    assert _promised(_checkout(snapshot)) == end + timedelta(days=PERIOD_DAYS)


@pytest.mark.parametrize('is_trial', [True, False])
def test_broken_end_date_in_snapshot_does_not_break_the_order(live_flags, is_trial):
    snapshot = _snapshot(is_trial=is_trial, tariff_id=TRIAL_TARIFF_ID if is_trial else PAID_TARIFF_ID)
    snapshot['end_date'] = 'not-a-date'

    assert _promised(_checkout(snapshot)) == CREATED_AT + timedelta(days=PERIOD_DAYS)


def test_expired_trial_counts_from_order_day(live_flags):
    snapshot = _snapshot(end=CREATED_AT - timedelta(hours=5))

    assert _promised(_checkout(snapshot)) == CREATED_AT + timedelta(days=PERIOD_DAYS)


def test_fulfilled_order_shows_the_issued_date(live_flags):
    issued = CREATED_AT + timedelta(days=PERIOD_DAYS, minutes=3)

    assert _promised(_checkout(_snapshot(), fulfilled_end_at=issued)) == issued


def test_promise_does_not_move_while_the_order_is_polled(live_flags):
    """Заказ опрашивается каждые несколько секунд — дата обязана стоять на месте, а не идти от «сейчас»."""
    checkout = _checkout(_snapshot())
    first = service.serialize_checkout(checkout)['estimated_end_at']

    with patch.object(service, 'datetime', wraps=datetime) as clock:
        clock.now.return_value = CREATED_AT + timedelta(days=1, hours=3)
        second = service.serialize_checkout(checkout)['estimated_end_at']

    assert first == second == (CREATED_AT + timedelta(days=PERIOD_DAYS)).isoformat()


@pytest.mark.asyncio
async def test_cabinet_payment_window_gets_the_honest_date(live_flags):
    """Настоящая точка входа: ответ кабинета для окна оплаты — единственный живой экран с этой датой."""
    invoice_deadline = CREATED_AT + timedelta(minutes=29, seconds=31)
    db = SimpleNamespace(scalar=AsyncMock(return_value=invoice_deadline))

    payload = await cabinet_routes._serialize_cabinet_checkout(db, _checkout(_snapshot()), balance_kopeks=0)

    assert payload['ui_state'] == 'awaiting_payment'
    assert payload['estimated_end_at'] == (CREATED_AT + timedelta(days=PERIOD_DAYS)).isoformat()
    assert payload['provider_invoice_expires_at'] == invoice_deadline.isoformat()

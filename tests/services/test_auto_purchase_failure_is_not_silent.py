"""Отказ автопокупки после пополнения перестаёт быть немым.

Десять веток `_auto_purchase_tariff` возвращают False и не говорят ничего:
деньги на балансе, подписки нет, клиент не знает, владелец узнать не может —
журнал уровня warning в админ-чат не пересылается.

Сторожим три разных вещи, а не одну:
1. частичное пополнение — клиенту «не хватает N», владельцу ТИШИНА (иначе
   каждое доливание баланса станет ложной тревогой);
2. настоящий отказ при достаточном балансе — клиенту сообщение, владельцу тревога;
3. пустая корзина — молчание правильно, это не «заплатил и не получил».
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import subscription_auto_purchase_service as service


class _Texts:
    def t(self, _key: str, default: str = '') -> str:
        return default

    def format_price(self, value: int, round_kopeks: bool | None = None) -> str:
        return f'{value / 100:.0f} ₽'


@pytest.fixture(autouse=True)
def _texts(monkeypatch):
    monkeypatch.setattr(service, 'get_texts', lambda *_a, **_k: _Texts())


def _db(balance_kopeks: int):
    return SimpleNamespace(scalar=AsyncMock(return_value=balance_kopeks))


def _user():
    return SimpleNamespace(id=7, telegram_id=555, language='ru', balance_kopeks=0)


def _owner_channel(monkeypatch):
    """Подменяем ИМЕННО тот канал, которым сервис уже пользуется при успехе."""
    sent: list[str] = []

    async def fake_channel(handler):
        service_stub = SimpleNamespace(send_admin_notification=AsyncMock())
        await handler(service_stub)
        sent.append(service_stub.send_admin_notification.await_args[0][0])

    import app.services.subscription_renewal_service as renewal

    monkeypatch.setattr(renewal, 'with_admin_notification_service', fake_channel)
    return sent


@pytest.mark.asyncio
async def test_partial_topup_tells_the_client_and_stays_silent_for_the_owner(monkeypatch):
    owner_alerts = _owner_channel(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    await service._notify_auto_purchase_failure(
        _db(20000),
        _user(),
        [{'total_price': 74900}],
        topped_up_kopeks=20000,
        bot=bot,
    )

    text = bot.send_message.await_args.kwargs['text']
    assert 'Не хватает' in text
    assert '549 ₽' in text  # 749 − 200, названо цифрой
    assert owner_alerts == []


@pytest.mark.asyncio
async def test_real_failure_tells_both_the_client_and_the_owner(monkeypatch):
    owner_alerts = _owner_channel(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    await service._notify_auto_purchase_failure(
        _db(100000),
        _user(),
        [{'total_price': 74900}],
        topped_up_kopeks=100000,
        bot=bot,
    )

    client_text = bot.send_message.await_args.kwargs['text']
    assert 'Не удалось оформить подписку автоматически' in client_text
    assert '1000 ₽' in client_text  # баланс назван цифрой, перечитанной из базы
    # ⛔ Обещать «списания не было» нельзя: в ветке ошибки создания подписки баланс
    # уже списан, а компенсирующий возврат может не пройти. Сторожим отсутствие лжи.
    assert 'списания не было' not in client_text
    assert len(owner_alerts) == 1
    assert 'Автопокупка после пополнения не прошла' in owner_alerts[0]


@pytest.mark.asyncio
async def test_empty_cart_says_nothing_to_anyone(monkeypatch):
    owner_alerts = _owner_channel(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())

    await service._notify_auto_purchase_failure(
        _db(100000),
        _user(),
        [{'total_price': 0}],
        topped_up_kopeks=100000,
        bot=bot,
    )

    bot.send_message.assert_not_awaited()
    assert owner_alerts == []


def _wire(monkeypatch, *, cart_succeeds: bool):
    """Проводка через НАСТОЯЩУЮ точку входа: тест на функцию не доказывает, что её зовут."""
    calls: list[tuple] = []

    async def spy(_db, _user, carts, **kwargs):
        calls.append((carts, kwargs.get('topped_up_kopeks')))

    monkeypatch.setattr(service, '_notify_auto_purchase_failure', spy)
    monkeypatch.setattr(type(service.settings), 'is_auto_purchase_after_topup_enabled', lambda self: True)
    monkeypatch.setattr(service.user_cart_service, 'get_all_subscription_carts', AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service.user_cart_service,
        'get_user_cart',
        AsyncMock(return_value={'cart_mode': 'tariff_purchase', 'total_price': 74900}),
    )
    monkeypatch.setattr(service.user_cart_service, 'has_topup_intent', AsyncMock(return_value=True))
    monkeypatch.setattr(service.user_cart_service, 'clear_topup_intent', AsyncMock())
    monkeypatch.setattr(service, '_process_single_cart', AsyncMock(return_value=cart_succeeds))
    return calls


@pytest.mark.asyncio
async def test_entry_point_reports_the_failure_and_passes_the_topped_up_amount(monkeypatch):
    calls = _wire(monkeypatch, cart_succeeds=False)

    result = await service.auto_purchase_saved_cart_after_topup(
        _db(0), _user(), topped_up_kopeks=20000, bot=SimpleNamespace()
    )

    assert result is False
    assert len(calls) == 1
    carts, topped_up = calls[0]
    assert carts == [{'cart_mode': 'tariff_purchase', 'total_price': 74900}]
    assert topped_up == 20000  # сумма доезжает, иначе текст клиенту врёт про «пополнен на 0 ₽»


@pytest.mark.asyncio
async def test_successful_purchase_does_not_report_a_failure(monkeypatch):
    calls = _wire(monkeypatch, cart_succeeds=True)

    result = await service.auto_purchase_saved_cart_after_topup(
        _db(0), _user(), topped_up_kopeks=100000, bot=SimpleNamespace()
    )

    assert result is True
    assert calls == []  # при успехе сервис уже пишет клиенту сам — второе сообщение было бы дублем


@pytest.mark.asyncio
async def test_email_only_user_gets_no_telegram_message_but_owner_still_alerted(monkeypatch):
    owner_alerts = _owner_channel(monkeypatch)
    bot = SimpleNamespace(send_message=AsyncMock())
    user = _user()
    user.telegram_id = None

    await service._notify_auto_purchase_failure(
        _db(100000),
        user,
        [{'total_price': 74900}],
        topped_up_kopeks=100000,
        bot=bot,
    )

    bot.send_message.assert_not_awaited()
    assert len(owner_alerts) == 1

"""Забор на обещание «корзина сохранена» в выборе периода тарифа.

Отказ по смене тарифа наступал ПОСЛЕ того, как бот пообещал оформить покупку
автоматически после пополнения. Человек шёл пополнять баланс ради покупки,
которая не могла состояться (`_auto_purchase_tariff` отказывает по тому же
условию), и не узнавал об этом ни разу. Забор переносит отказ до обещания.

Сторожим смысл, а не букву: (1) при чужом тарифе корзина НЕ сохраняется и
обещание НЕ показывается; (2) при своём тарифе продление по-прежнему работает —
то есть забор не отобрал ничего лишнего.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.subscription import tariff_purchase


PROMISE = 'Корзина сохранена'
REFUSAL = 'Смена тарифа через этот сценарий недоступна'


def _raw_handler():
    """Снимаем @error_handler, чтобы дойти до первой строки самой функции."""
    handler = tariff_purchase.select_tariff_period
    while hasattr(handler, '__wrapped__'):
        handler = handler.__wrapped__
    return handler


def _tariff(tariff_id: int = 3):
    tariff = MagicMock()
    tariff.id = tariff_id
    tariff.is_active = True
    tariff.name = 'Базовый'
    tariff.device_limit = 2
    tariff.traffic_limit_gb = 0
    return tariff


def _callback():
    return SimpleNamespace(
        data='tariff_period:3:30',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )


def _arrange(monkeypatch, *, existing_tariff_id: int | None, price: int, balance: int):
    from app.services import pricing_engine as pricing_module

    monkeypatch.setattr(type(tariff_purchase.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))

    subscription = None
    if existing_tariff_id is not None:
        subscription = SimpleNamespace(id=77, tariff_id=existing_tariff_id, device_limit=2)
    monkeypatch.setattr(tariff_purchase, 'get_subscription_by_user_id', AsyncMock(return_value=subscription))

    monkeypatch.setattr(
        pricing_module.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=price, original_total=price)),
    )

    saved = AsyncMock(return_value=True)
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'save_user_cart', saved)

    db_user = SimpleNamespace(id=42, balance_kopeks=balance, language='ru')
    return saved, db_user


@pytest.mark.asyncio
async def test_foreign_tariff_is_refused_before_the_cart_promise(monkeypatch):
    """Пробная подписка на тарифе 5, покупает тариф 3 — обещания быть не должно."""
    saved, db_user = _arrange(monkeypatch, existing_tariff_id=5, price=24900, balance=0)
    callback = _callback()

    await _raw_handler()(callback, db_user, MagicMock(), AsyncMock())

    saved.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once_with(REFUSAL, show_alert=True)


@pytest.mark.asyncio
async def test_own_tariff_still_saves_the_cart_and_keeps_the_promise(monkeypatch):
    """Продление своего же тарифа забор трогать не должен."""
    saved, db_user = _arrange(monkeypatch, existing_tariff_id=3, price=24900, balance=0)
    callback = _callback()

    await _raw_handler()(callback, db_user, MagicMock(), AsyncMock())

    saved.assert_awaited_once()
    assert saved.await_args[0][1]['cart_mode'] == 'tariff_purchase'
    shown = callback.message.edit_text.await_args[0][0]
    assert PROMISE in shown
    assert REFUSAL not in shown


@pytest.mark.asyncio
async def test_foreign_tariff_is_refused_even_when_the_balance_is_enough(monkeypatch):
    """Пинает ПОЗИЦИЮ забора: он выше развилки по балансу, а не внутри ветки «не хватает».

    Без этого сторожа забор можно переставить внутрь `else`, и человеку с деньгами
    снова покажут экран подтверждения на чужой тариф — а все остальные тесты
    останутся зелёными, потому что гоняют баланс 0.
    """
    saved, db_user = _arrange(monkeypatch, existing_tariff_id=5, price=24900, balance=99900)
    callback = _callback()

    await _raw_handler()(callback, db_user, MagicMock(), AsyncMock())

    saved.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()  # экран подтверждения не показан
    callback.answer.assert_awaited_once_with(REFUSAL, show_alert=True)


@pytest.mark.asyncio
async def test_multi_tariff_mode_is_not_touched_by_the_fence(monkeypatch):
    """Пинает вторую развилку: забор живёт в ветке БЕЗ мультитарифа, как у всех соседей.

    Поднимут его выше `if settings.is_multi_tariff_enabled()` — сломается покупка
    в мультитарифе, а прочие тесты этого не заметят.
    """
    from app.services import pricing_engine as pricing_module

    monkeypatch.setattr(type(tariff_purchase.settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(tariff_purchase, 'get_tariff_by_id', AsyncMock(return_value=_tariff()))
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff',
        AsyncMock(return_value=SimpleNamespace(id=77, tariff_id=5, device_limit=2)),
    )
    monkeypatch.setattr(
        pricing_module.pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=24900, original_total=24900)),
    )
    saved = AsyncMock(return_value=True)
    monkeypatch.setattr(tariff_purchase.user_cart_service, 'save_user_cart', saved)
    callback = _callback()

    await _raw_handler()(callback, SimpleNamespace(id=42, balance_kopeks=0, language='ru'), MagicMock(), AsyncMock())

    saved.assert_awaited_once()  # забор не сработал — режим не его
    assert REFUSAL not in str(callback.answer.await_args_list)


@pytest.mark.asyncio
async def test_user_without_any_subscription_is_not_refused(monkeypatch):
    """У новичка подписки нет — забор не про него."""
    saved, db_user = _arrange(monkeypatch, existing_tariff_id=None, price=24900, balance=0)
    callback = _callback()

    await _raw_handler()(callback, db_user, MagicMock(), AsyncMock())

    saved.assert_awaited_once()
    assert REFUSAL not in str(callback.answer.await_args_list)

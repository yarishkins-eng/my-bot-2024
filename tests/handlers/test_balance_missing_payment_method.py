"""Recovery behaviour for a malformed balance-topup FSM state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.balance import main as balance_main


@pytest.mark.asyncio
async def test_missing_payment_method_restarts_payment_choice_without_routing(monkeypatch):
    message = SimpleNamespace(text='250', successful_payment=None, answer=AsyncMock())
    user = SimpleNamespace(language='ru')
    state = SimpleNamespace(get_data=AsyncMock(return_value={}), clear=AsyncMock())
    keyboard = MagicMock()
    route_payment = AsyncMock()

    methods_keyboard = MagicMock(return_value=keyboard)
    monkeypatch.setattr(balance_main, 'get_payment_methods_keyboard', methods_keyboard)
    monkeypatch.setattr(balance_main, 'route_payment_by_method', route_payment)

    await balance_main.process_topup_amount(message, user, state)

    state.clear.assert_awaited_once()
    methods_keyboard.assert_called_once_with(0, 'ru')
    message.answer.assert_awaited_once_with(
        'Способ оплаты не выбран. Выберите его ещё раз.',
        reply_markup=keyboard,
    )
    route_payment.assert_not_awaited()

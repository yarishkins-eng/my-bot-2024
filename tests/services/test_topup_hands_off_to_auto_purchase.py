"""Проводка «пополнение → автопокупка» в app/services/payment/common.py.

Две дыры, которые пережили весь набор из 3046 тестов и были найдены мутацией,
а не ревью:

1. Сумма пополнения не доезжала до автопокупки — и клиент в сообщении об отказе
   увидел бы «Баланс пополнен на 0 ₽», то есть ложь в денежном тексте.
2. Возврат автопокупки не читался, и меню подписчика обновлялось при ЛЮБОМ
   исходе, хотя рядом было написано «только при успехе»: при отказе клиент
   видел шевеление меню и ни слова о том, что подписки нет.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.payment import common as payment_common


@pytest.fixture
def wiring(monkeypatch):
    """Обрубаем всё, кроме проверяемой проводки."""
    import app.services.account_erasure_service as erasure
    import app.services.subscription_auto_purchase_service as auto
    import app.utils.funnel_notify as funnel

    monkeypatch.setattr(erasure, 'mark_late_legacy_payment_for_manual_review', AsyncMock(return_value=False))
    monkeypatch.setattr(payment_common, 'notify_email_user_topup', AsyncMock())
    monkeypatch.setattr(auto, 'try_resume_disabled_daily_after_topup', AsyncMock(return_value=False))
    monkeypatch.setattr(auto, 'try_auto_extend_expired_after_topup', AsyncMock(return_value=False))
    monkeypatch.setattr(
        payment_common.user_cart_service,
        'get_user_cart',
        AsyncMock(return_value={'cart_mode': 'tariff_purchase', 'total_price': 74900}),
    )

    state = SimpleNamespace(
        auto_purchase=AsyncMock(return_value=False),
        menu=AsyncMock(),
    )
    monkeypatch.setattr(auto, 'auto_purchase_saved_cart_after_topup', state.auto_purchase)
    monkeypatch.setattr(funnel, 'notify_subscriber_menu', state.menu)
    return state


@pytest.mark.asyncio
async def test_topped_up_amount_reaches_auto_purchase(wiring):
    """Иначе клиенту в отказе напишут «пополнен на 0 ₽»."""
    await payment_common.send_cart_notification_after_topup(
        SimpleNamespace(id=7, telegram_id=555, language='ru'),
        amount_kopeks=20000,
        db=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    assert wiring.auto_purchase.await_args.kwargs['topped_up_kopeks'] == 20000


@pytest.mark.asyncio
async def test_menu_is_not_refreshed_when_the_purchase_failed(wiring):
    wiring.auto_purchase.return_value = False

    await payment_common.send_cart_notification_after_topup(
        SimpleNamespace(id=7, telegram_id=555, language='ru'),
        amount_kopeks=20000,
        db=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    wiring.menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_is_refreshed_when_the_purchase_succeeded(wiring):
    wiring.auto_purchase.return_value = True

    await payment_common.send_cart_notification_after_topup(
        SimpleNamespace(id=7, telegram_id=555, language='ru'),
        amount_kopeks=20000,
        db=SimpleNamespace(),
        bot=SimpleNamespace(),
    )

    wiring.menu.assert_awaited_once()

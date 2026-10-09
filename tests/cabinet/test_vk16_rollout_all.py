"""ВК-16, перевод на всех: решение владельца 09.10 после живого прохода 1–5.

Сторожим обычного клиента, а не только литерал константы: приём намерения, оформление после
зачисления и оба экрана бота. Режимы отката и запреты сохраняют прежнюю дорогу без обещания.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.database.models import SubscriptionCheckout
from app.handlers.subscription.device_first import (
    _money_block,
    _render_direct_payment_methods,
    _render_fused_confirmation,
)
from app.services import device_first_checkout_service as dfc
from tests.cabinet.test_vk16_topup_autocomplete import (  # noqa: F401 -- общий исполняемый стенд ВК-16
    _complete,
    buy,
    checkouts,
)
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401 -- общий исполняемый стенд ВК-16
    BALANCE,
    PRICE_30_1,
    TOP_UP_30_1,
    _decide,
    _intent_payment,
    _set_user,
    _user,
    db,
    env,
    session,
)


def test_ordinary_customer_gets_auto_order_without_an_admin_switch(session, env, monkeypatch):
    monkeypatch.setattr(settings, 'AUTO_PURCHASE_AFTER_TOPUP_ENABLED', False)
    assert dfc.topup_intent_enabled_for(_user(session, 2)) is True


async def test_ordinary_customer_intent_is_accepted(db, session, env):
    decision = await _decide(db, session, user_id=2)
    assert decision.status == 'accepted'
    assert decision.amount_kopeks == TOP_UP_30_1


async def test_ordinary_customer_paid_intent_orders_once_from_own_balance(session, buy, env):
    env.options.return_value['current_subscription'] = None
    _intent_payment(session, payment_id=97, user_id=2, tariff_id=3, had_subscription=False)
    _set_user(session, user_id=2, balance_kopeks=BALANCE + TOP_UP_30_1)

    _, stored = await _complete(session)

    assert stored['status'] == 'fulfilled'
    assert buy.create[0]['user'].id == 2
    assert session.get(SubscriptionCheckout, 501).user_id == 2
    assert _user(session, 2).balance_kopeks == BALANCE + TOP_UP_30_1 - PRICE_30_1
    assert _user(session, 1).balance_kopeks == BALANCE
    await _complete(session)
    assert buy.commit == ['chk-501']


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_both_bot_order_screens_explain_auto_order_to_ordinary_customer(session, env, surface, language):
    user = _user(session, 2)
    user.language = language
    callback = SimpleNamespace(answer=AsyncMock())
    database = AsyncMock()
    database.scalar.return_value = None
    database.get.return_value = SimpleNamespace(name='Базовый')
    options = {
        'tariff': {'name': 'Базовый'},
        'period_options': [30],
        'device_options': [1],
        'price_matrix': [{'period_days': 30, 'prices': [{'device_limit': 1, 'price_kopeks': PRICE_30_1}]}],
    }
    checkout = SimpleNamespace(
        public_id='co-1',
        tariff_id=3,
        tariff_total_kopeks=PRICE_30_1,
        selected_device_limit=1,
        period_days=30,
        funding_mode=None,
    )
    with (
        patch(
            'app.handlers.subscription.device_first.available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': 'sbp', 'provider_code': 2}]),
        ),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value='https://cabinet.example/safe'),
        patch('app.handlers.subscription.device_first.edit_or_answer_photo', AsyncMock()) as render,
    ):
        if surface == 'fused':
            await _render_fused_confirmation(callback, user, database, options, days=30, devices=1)
        else:
            await _render_direct_payment_methods(callback, user, database, checkout)
    caption = render.await_args.kwargs['caption']
    expected = (
        'После оплаты подписка оформится сама — больше ничего нажимать не нужно'
        if language == 'ru'
        else 'After payment, your subscription will be set up automatically — no more taps needed'
    )
    assert expected in caption
    assert ('Доплатите и продолжите' if language == 'ru' else 'Top up and finish') not in caption


@pytest.mark.parametrize('rollout', ['stands', 'off', 'typo'])
def test_rollback_modes_do_not_promise_auto_order_to_ordinary_customer(session, env, monkeypatch, rollout):
    monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', rollout)
    user = _user(session, 2)
    assert dfc.topup_intent_enabled_for(user) is False
    ru, en = _money_block(
        user, balance_kopeks=BALANCE, price_kopeks=PRICE_30_1, top_up_button=SimpleNamespace(), has_methods=True
    )
    assert 'Доплатите и продолжите покупку' in ru
    assert 'Top up and finish the purchase' in en


@pytest.mark.parametrize(
    'restriction', ['restriction_subscription', 'restriction_topup', 'account_erasure_requested_at']
)
def test_restricted_customer_does_not_get_auto_order_promise(session, env, restriction):
    _set_user(session, user_id=2, **{restriction: '2026-10-09 10:00:00' if restriction.endswith('_at') else True})
    user = _user(session, 2)
    assert dfc.topup_intent_enabled_for(user) is False
    ru, _ = _money_block(
        user, balance_kopeks=BALANCE, price_kopeks=PRICE_30_1, top_up_button=SimpleNamespace(), has_methods=True
    )
    assert 'оформится сама' not in ru

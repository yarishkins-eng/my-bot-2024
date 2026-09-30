"""РЕФ-2.3 (30.09.2026): кнопки чат-админки, которые платили сами, сняты.

«Начислить все бонусы» платила по своей формуле без защиты от двойного нажатия, «Применить исправления»
перепривязывала человека и платила за ту же оплату второй раз (проверка рефералки 29.09, находки C-7, C-S1, C-S2).
Экраны проверки остаются только для чтения; старое сообщение с кнопкой получает отказ, а не тишину.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.handlers.admin import referrals as module


def _callback() -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))


def _state(data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(get_data=AsyncMock(return_value=data or {}), update_data=AsyncMock())


def _callbacks_of(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
@pytest.mark.parametrize('handler_name', ['apply_missing_bonuses', 'apply_referral_fixes'])
async def test_old_payout_button_answers_refusal_and_pays_nothing(handler_name: str) -> None:
    handler = inspect.unwrap(getattr(module, handler_name))
    callback = _callback()
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    state = _state({'missing_bonuses_report': {'missing_bonuses': [1]}, 'diagnostics_period': 'today'})
    service = SimpleNamespace(
        fix_missing_bonuses=AsyncMock(),
        fix_lost_referrals=AsyncMock(),
        analyze_period=AsyncMock(),
        check_missing_bonuses=AsyncMock(),
    )

    with patch('app.services.referral_diagnostics_service.referral_diagnostics_service', service):
        await handler(callback, db_user=SimpleNamespace(id=1), db=db, state=state)

    callback.answer.assert_awaited_once_with(module.PAYOUT_BUTTON_DISABLED_TEXT, show_alert=True)
    callback.message.edit_text.assert_not_awaited()
    service.fix_missing_bonuses.assert_not_awaited()
    service.fix_lost_referrals.assert_not_awaited()
    service.analyze_period.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()
    state.get_data.assert_not_awaited()
    state.update_data.assert_not_awaited()


def test_refusal_text_fits_a_telegram_alert() -> None:
    # Telegram обрезает текст всплывающего ответа на 200 символах
    assert len(module.PAYOUT_BUTTON_DISABLED_TEXT) <= 200
    assert 'не начислил' in module.PAYOUT_BUTTON_DISABLED_TEXT


@pytest.mark.asyncio
async def test_missing_bonus_check_is_read_only_and_offers_no_payout() -> None:
    handler = inspect.unwrap(module.check_missing_bonuses)
    callback = _callback()
    state = _state()
    missing = SimpleNamespace(
        referral_full_name='Петя <друг> & Ко',
        referral_username=None,
        referral_telegram_id=1,
        referrer_full_name='Маша',
        referrer_username=None,
        referrer_telegram_id=2,
        first_topup_amount_kopeks=19900,
        referral_bonus_amount=10000,
        referrer_bonus_amount=4975,
    )
    report = SimpleNamespace(
        total_referrals_checked=1,
        referrals_with_topup=1,
        missing_bonuses=[missing],
        total_missing_to_referrals=10000,
        total_missing_to_referrers=4975,
        to_dict=Mock(return_value={}),
    )
    service = SimpleNamespace(check_missing_bonuses=AsyncMock(return_value=report))

    with patch('app.services.referral_diagnostics_service.referral_diagnostics_service', service):
        await handler(callback, db_user=SimpleNamespace(id=1), db=SimpleNamespace(), state=state)

    text, markup = (
        callback.message.edit_text.await_args.args[0],
        callback.message.edit_text.await_args.kwargs['reply_markup'],
    )
    assert 'admin_ref_bonus_apply' not in _callbacks_of(markup)
    assert 'Требуется начислить' not in text
    assert 'Проверка считает не начисленным (только просмотр)' in text
    assert 'Петя &lt;друг&gt; &amp; Ко' in text  # имя по-прежнему экранировано
    # отчёт больше не кладётся в состояние «для последующего применения» — применять нечем
    state.update_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_fix_preview_is_read_only_and_does_not_ask_to_press_apply() -> None:
    handler = inspect.unwrap(module.preview_referral_fixes)
    callback = _callback()
    fix_report = SimpleNamespace(
        users_fixed=1, bonuses_to_referrals=10000, bonuses_to_referrers=4975, errors=0, details=[]
    )
    service = SimpleNamespace(
        analyze_period=AsyncMock(return_value=SimpleNamespace(lost_referrals=[object()])),
        fix_lost_referrals=AsyncMock(return_value=fix_report),
    )

    with patch('app.services.referral_diagnostics_service.referral_diagnostics_service', service):
        await handler(callback, db_user=SimpleNamespace(id=1), db=SimpleNamespace(), state=_state())

    # предпросмотр по-прежнему ничего не применяет
    assert service.fix_lost_referrals.await_args.kwargs == {'apply': False}
    text, markup = (
        callback.message.edit_text.await_args.args[0],
        callback.message.edit_text.await_args.kwargs['reply_markup'],
    )
    assert 'admin_ref_fix_apply' not in _callbacks_of(markup)
    assert 'Нажмите "Применить"' not in text
    assert 'Только просмотр.' in text
    assert 'Найдено для исправления: 1' in text


def test_old_buttons_stay_registered_so_a_stale_message_gets_an_answer() -> None:
    dp = SimpleNamespace(callback_query=Mock(), message=Mock())
    module.register_handlers(dp)
    registered = [call.args[0] for call in dp.callback_query.register.call_args_list]
    assert module.apply_missing_bonuses in registered
    assert module.apply_referral_fixes in registered

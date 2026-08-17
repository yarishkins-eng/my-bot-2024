"""Мина AA: кнопка «💎 Сделать платной» одним нажатием снимала оба сторожа автосписания.

Она ставит `is_trial=False` и `has_had_paid_subscription=True` напрямую, минуя
`extend_subscription` и его гард `convert_trial`, но НЕ трогает срок: у трёхдневного триала
он и остаётся трёхдневным. Дальше `try_auto_extend_expired_after_topup`
(`subscription_auto_purchase_service.py:2356`, `:2375`) перестаёт защищать, и ближайшее
пополнение баланса — сделанное совсем для другого — списывает полный период.

Проверяется не «текст поменялся», а три вещи:
1. направление «в платную» до мутации не доходит;
2. обратное направление осталось живым — оно взводит гард обратно;
3. кнопки, ведущей в мутацию, на экране больше нет.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.admin import users as admin_users


# Оба обработчика под `@admin_required` и `@error_handler`: снимаем их так же, как это уже
# делают соседние тесты проекта (`tests/services/test_public_location_entitlements.py:649`).
_confirm = admin_users.change_subscription_type_confirm.__wrapped__.__wrapped__
_screen = admin_users.change_subscription_type.__wrapped__.__wrapped__


def _callback(data: str):
    return SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_making_a_trial_paid_never_reaches_the_mutation():
    """🔴 Главный сторож: до `_change_subscription_type` дело не доходит вообще."""
    callback = _callback('admin_sub_type_paid_196')
    with patch.object(admin_users, '_change_subscription_type', AsyncMock()) as mutation:
        await _confirm(callback, SimpleNamespace(id=1), AsyncMock())

    mutation.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_the_refusal_says_where_to_get_a_real_subscription():
    """Отказ без замены — это тупик. Рабочий путь у владельца есть, и он назван."""
    callback = _callback('admin_sub_type_paid_196')
    with patch.object(admin_users, '_change_subscription_type', AsyncMock()):
        await _confirm(callback, SimpleNamespace(id=1), AsyncMock())

    text = callback.answer.await_args[0][0]
    assert 'кабинете' in text
    assert 'Создать подписку' in text
    # 🔴 Сторож длины. `answerCallbackQuery` принимает 200 символов; длиннее — Telegram отвечает
    # 400, `@error_handler` подменяет ответ общим «Произошла ошибка», и человек не узнаёт ни
    # причины, ни пути. Первая версия текста была 208 символов — поймало ревью, не тест.
    assert len(text) <= 200, f'алерт длиннее лимита Telegram: {len(text)}'


@pytest.mark.asyncio
async def test_making_a_paid_subscription_trial_again_still_works():
    """Обратное направление безопасно: оно возвращает `is_trial` и взводит гард обратно."""
    callback = _callback('admin_sub_type_trial_196')
    with patch.object(admin_users, '_change_subscription_type', AsyncMock(return_value=True)) as mutation:
        await _confirm(callback, SimpleNamespace(id=1), AsyncMock())

    mutation.assert_awaited_once()
    assert mutation.await_args[0][2] == 'trial'
    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_screen_no_longer_draws_the_button_that_leads_there():
    """Отключить обработчик мало: кнопка в чате живёт в старых сообщениях, но рисовать её нельзя."""
    callback = _callback('admin_sub_change_type_196')
    profile = {
        'subscription': SimpleNamespace(is_trial=True, id=7),
        'user': SimpleNamespace(id=196, full_name='Тестовый клиент'),
    }
    service = SimpleNamespace(get_user_profile=AsyncMock(return_value=profile))

    with patch.object(admin_users, 'UserService', lambda: service):
        await _screen(callback, SimpleNamespace(id=1), AsyncMock())

    text = callback.message.edit_text.await_args[0][0]
    keyboard = callback.message.edit_text.await_args.kwargs['reply_markup'].inline_keyboard
    buttons = [button.callback_data for row in keyboard for button in row]
    assert not any(str(data).startswith('admin_sub_type_paid') for data in buttons)
    # И человеку сказано, куда идти вместо этого.
    assert 'кабинете' in text


@pytest.mark.asyncio
async def test_any_direction_that_is_not_trial_is_refused():
    """🔴 Гард сравнивает с `trial`, а не с `paid`.

    Сама мутация считает платным ВСЁ, что не `trial` (`new_is_trial = new_type == 'trial'`),
    поэтому чёрный список из одного слова пропустил бы любой другой токен в те же присваивания.
    """
    callback = _callback('admin_sub_type_premium_196')
    with patch.object(admin_users, '_change_subscription_type', AsyncMock()) as mutation:
        await _confirm(callback, SimpleNamespace(id=1), AsyncMock())

    mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_one_way_switch_warns_that_there_is_no_way_back():
    """Тумблер стал односторонним: обратной кнопки нет нигде, и об этом надо сказать заранее."""
    callback = _callback('admin_sub_change_type_196')
    profile = {
        'subscription': SimpleNamespace(is_trial=False, id=7),
        'user': SimpleNamespace(id=196, full_name='Тестовый клиент'),
    }
    service = SimpleNamespace(get_user_profile=AsyncMock(return_value=profile))

    with patch.object(admin_users, 'UserService', lambda: service):
        await _screen(callback, SimpleNamespace(id=1), AsyncMock())

    text = callback.message.edit_text.await_args[0][0]
    assert 'Обратно платной' in text
    assert 'Сбросить подписку' in text

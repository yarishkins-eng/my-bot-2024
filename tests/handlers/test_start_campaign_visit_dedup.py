"""Сторож: «📣 ПЕРЕХОД ПО РК» не повторяется по той же рекламе.

Человек, бросивший регистрацию на выборе языка, в БД не появляется (`create_user`
вызывается ниже по потоку), поэтому при каждом возврате он снова `user is None` —
и до этой правки владелец получал нового «лида» за того же человека. Пометка живёт
в FSM-состоянии, которое между заходами не чистится.

Тесты гоняют настоящий `cmd_start` с настоящим `FSMContext` поверх `MemoryStorage`:
пометка обязана пережить возврат так же, как переживает его боевой Redis.
Что именно закреплено (каждый пункт краснеет от своей мутации):

* повторный `/start` по той же рекламе второго уведомления не шлёт;
* пометка ставится ТОЛЬКО когда отправка вернула успех — иначе лид пропал бы навсегда;
* пометка ДОПИСЫВАЕТСЯ в состояние, а не заменяет его: рядом лежит атрибуция;
* клик по ДРУГОЙ живой кампании уведомление всё-таки шлёт (на боевом их четыре);
* `True`, оставленный сторожем канала (`middlewares/channel_checker.py:443`), гасит всё;
* обычный `/start` без рекламной ссылки проходит мимо и не падает.

⚠️ Пометка помнит ОДНУ, последнюю кампанию. Человек, который ходит между двумя живыми
объявлениями, даёт по уведомлению на каждое переключение — это осознанный предел правки,
а не недосмотр: каждое такое переключение всё-таки отдельный оплаченный клик.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.handlers import start as start_module


TELEGRAM_ID = 5214767561


def _campaign(campaign_id: int, start_parameter: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=campaign_id,
        name=f'Кампания {campaign_id}',
        start_parameter=start_parameter,
        partner_user_id=None,
        is_none_bonus=False,
        is_active=True,
    )


def _message(start_parameter: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        text='/start' if start_parameter is None else f'/start {start_parameter}',
        from_user=types.User(id=TELEGRAM_ID, is_bot=False, first_name='Гость', username='guest'),
        bot=MagicMock(),
        answer=AsyncMock(),
    )


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=TELEGRAM_ID, user_id=TELEGRAM_ID),
    )


def _patch_start(monkeypatch: pytest.MonkeyPatch, campaigns: dict[str, SimpleNamespace], *, sent: bool) -> AsyncMock:
    """Обвязка вокруг `cmd_start`: человека в БД нет, кампания находится по параметру.

    Возвращает мок отправки уведомления — на нём и считаем, сколько лидов ушло владельцу.
    """

    send_mock = AsyncMock(return_value=sent)

    async def _get_campaign(_db, start_parameter, only_active=True):
        return campaigns.get(start_parameter)

    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_campaign_by_start_parameter', _get_campaign)
    monkeypatch.setattr(start_module, 'save_pending_campaign', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        start_module,
        'AdminNotificationService',
        MagicMock(return_value=SimpleNamespace(send_campaign_link_visit_notification=send_mock)),
    )
    # Новичок упирается в выбор языка и уходит — ровно та точка, где он бросает регистрацию.
    # `settings` — pydantic-модель, метод подменяется на классе, а не на экземпляре.
    monkeypatch.setattr(type(start_module.settings), 'is_language_selection_enabled', lambda _self: True)
    monkeypatch.setattr(start_module, '_prompt_language_selection', AsyncMock(return_value=None))
    return send_mock


def _patch_existing_user(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Человек УЖЕ есть в базе: `cmd_start` уходит в ветку «показать меню».

    Ветка длинная и до уведомления отношения не имеет — глушим её целиком, чтобы
    тест говорил ровно об одном: владельцу не шлют «ПЕРЕХОД» за существующего клиента.
    """

    user = SimpleNamespace(
        id=777,
        telegram_id=TELEGRAM_ID,
        username=None,
        first_name='Гость',
        last_name=None,
        language='ru',
        status='active',
        balance_kopeks=0,
        referred_by_id=1,
        has_had_paid_subscription=False,
        subscriptions=[],
        last_activity=None,
        updated_at=None,
    )

    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='меню'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    monkeypatch.setattr('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock(return_value=None))
    return user


@pytest.mark.asyncio
async def test_second_start_by_same_campaign_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кликнул рекламу, бросил язык, вернулся — владелец получает ОДНО уведомление."""

    send_mock = _patch_start(monkeypatch, {'teplo2': _campaign(4, 'teplo2')}, sent=True)
    state = _state()

    await start_module.cmd_start(_message('teplo2'), state, MagicMock())
    # Снимок делается сразу после ПЕРВОГО захода: второй заход заново кладёт метку
    # кампании (`start.py:817`) и замаскировал бы запись состояния «поверх».
    after_first_visit = await state.get_data()
    await start_module.cmd_start(_message('teplo2'), state, MagicMock())

    assert send_mock.await_count == 1
    assert after_first_visit['campaign_notification_sent'] == 4
    # Пометка дописана, а не записана поверх: рядом обязана уцелеть метка кампании,
    # из которой потом соберётся «РЕГИСТРАЦИЯ ПО РК».
    assert after_first_visit['campaign_id'] == 4


@pytest.mark.asyncio
async def test_flag_not_set_when_notification_was_not_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отправка вернула False (чат не настроен, категория выключена, Telegram отказал) —
    пометки нет, лид не потерян: следующий заход попробует снова."""

    send_mock = _patch_start(monkeypatch, {'teplo2': _campaign(4, 'teplo2')}, sent=False)
    state = _state()

    await start_module.cmd_start(_message('teplo2'), state, MagicMock())
    await start_module.cmd_start(_message('teplo2'), state, MagicMock())

    assert send_mock.await_count == 2
    assert 'campaign_notification_sent' not in await state.get_data()


@pytest.mark.asyncio
async def test_click_on_other_campaign_still_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вторая живая реклама — отдельный оплаченный клик, он обязан дойти до владельца."""

    campaigns = {'teplo2': _campaign(4, 'teplo2'), 'teplovpn1': _campaign(3, 'teplovpn1')}
    send_mock = _patch_start(monkeypatch, campaigns, sent=True)
    state = _state()

    await start_module.cmd_start(_message('teplo2'), state, MagicMock())
    await start_module.cmd_start(_message('teplovpn1'), state, MagicMock())

    assert send_mock.await_count == 2
    assert (await state.get_data())['campaign_notification_sent'] == 3


@pytest.mark.asyncio
async def test_flag_from_channel_guard_suppresses_any_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    """`True` ставит сторож канала (`middlewares/channel_checker.py:443`) — он не знает
    про номера кампаний, поэтому его пометка гасит всё. Совместимость обязана сохраниться."""

    send_mock = _patch_start(monkeypatch, {'teplo2': _campaign(4, 'teplo2')}, sent=True)
    state = _state()
    await state.update_data(campaign_notification_sent=True)

    await start_module.cmd_start(_message('teplo2'), state, MagicMock())

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_user_gets_no_visit_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    """Человек УЖЕ в базе — «ПЕРЕХОД» ему не полагается вовсе (`user is None` в условии).

    Без этого теста проверку `user is None` можно выбросить, не покраснив ни один тест
    во всём проекте: тогда каждый существующий клиент, кликнувший рекламу повторно,
    приходил бы владельцу как новый лид.
    """

    send_mock = _patch_start(monkeypatch, {'teplo2': _campaign(4, 'teplo2')}, sent=True)
    _patch_existing_user(monkeypatch)
    state = _state()
    db = MagicMock(commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock())

    await start_module.cmd_start(_message('teplo2'), state, db)

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_start_without_campaign_does_not_break(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обычный `/start` без рекламной ссылки — самый горячий путь бота.

    Проверка «пометка чья» вычисляется ДО `if campaign`, поэтому без защиты
    `campaign is not None` она уронила бы каждый такой заход.
    """

    send_mock = _patch_start(monkeypatch, {'teplo2': _campaign(4, 'teplo2')}, sent=True)
    state = _state()
    await state.update_data(campaign_notification_sent=4)

    await start_module.cmd_start(_message(), state, MagicMock())

    send_mock.assert_not_awaited()

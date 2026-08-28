"""Сторожа на ОТПРАВКУ письма о реферальной награде.

🔴 Заведены после ревью РФ-3, которое показало: шесть мутаций на несущих строках отправки
проходили весь набор из 3156 тестов зелёными. Причина не «мало тестов», а точная: сама
`_send_referral_reward_message` во всём проекте только **мокалась** и не исполнялась ни разу.

Что могло уехать молча:
  * убрать фильтр по получателю — и каждый получит по ДВА одинаковых письма;
  * вернуть «дошло хоть кому-то — успех» — и второй человек снова не узнает о деньгах;
  * сравнить тип строки точно вместо начала — и строка уйдёт в отказ как неизвестная.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import TransactionType
from app.services import device_first_checkout_service as service


class _Result:
    def __init__(self, value=None, values=None):
        self._value = value
        self._values = values or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._values


def _reward(user_id: int, suffix: str, amount: int = 10000):
    return SimpleNamespace(
        user_id=user_id,
        amount_kopeks=amount,
        device_first_ledger_key=f'deposit-side-effect:193:{suffix}',
        description='Награда за первую оплату реферала друг: 100 ₽ фикс + 25% от 1990 ₽',
    )


def _db(rewards, users):
    async def _get(model, pk):
        return users.get(pk)

    return SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(values=rewards), _Result(value=SimpleNamespace(amount_kopeks=199000))]),
        get=AsyncMock(side_effect=_get),
    )


@pytest.mark.asyncio
async def test_one_row_writes_to_one_person_only():
    """Строка с получателем шлёт письмо ТОЛЬКО ему.

    Вход подобран так, что ветки дают разное: наград две, получателей двое. Без фильтра
    ушло бы два письма вместо одного — и каждый получил бы чужое тоже.
    """
    rewards = [_reward(133, 'inviter-first-reward', 59750), _reward(172, 'referred-first-bonus')]
    users = {
        133: SimpleNamespace(id=133, telegram_id=1330, language='ru', full_name='партнёр'),
        172: SimpleNamespace(id=172, telegram_id=1720, language='ru', full_name='друг'),
    }
    bot = SimpleNamespace(send_message=AsyncMock())

    await service._send_referral_reward_message(
        _db(rewards, users), bot=bot, checkout=SimpleNamespace(id=14, user_id=172), recipient_id=133
    )

    assert bot.send_message.await_count == 1, 'письмо ушло не только своему получателю'
    assert bot.send_message.await_args.args[0] == 1330, 'письмо ушло не тому человеку'


@pytest.mark.asyncio
async def test_a_failed_send_never_counts_as_success():
    """Отказ роняет строку, даже если соседу письмо ушло.

    🔴 Раньше здесь стояло «не дошло НИ ОДНО — тогда падаем», и при частичной доставке
    строка помечалась выполненной: второй человек не узнавал о деньгах никогда, потому что
    повтора для него не существовало.
    """
    rewards = [_reward(133, 'inviter-first-reward', 59750), _reward(172, 'referred-first-bonus')]
    users = {
        133: SimpleNamespace(id=133, telegram_id=1330, language='ru', full_name='партнёр'),
        172: SimpleNamespace(id=172, telegram_id=1720, language='ru', full_name='друг'),
    }
    # Первому дошло, второму нет.
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[None, RuntimeError('чат закрыт')]))

    with pytest.raises(RuntimeError, match='referral_reward_delivery_failed'):
        await service._send_referral_reward_message(
            _db(rewards, users), bot=bot, checkout=SimpleNamespace(id=14, user_id=172), recipient_id=None
        )

    assert bot.send_message.await_count == 2


def test_recipient_is_read_from_the_row_type():
    """Получатель достаётся из типа строки, старый формат отдаёт None."""
    assert service._referral_reward_recipient('referral_reward:133') == 133
    assert service._referral_reward_recipient('referral_reward') is None
    assert service._referral_reward_recipient('order_stuck') is None


def test_dispatcher_recognises_the_per_recipient_type():
    """Диспетчер узнаёт тип с получателем — иначе строка уходит в «неизвестный тип».

    🔴 Мутация `startswith` → `==` проходила весь набор зелёной: единственный тест диспетчера
    подавал СТАРЫЙ тип, который проходит и через то, и через другое. Сторож смотрит на
    исходник, потому что цепочка диспетчера тянет за собой бота и половину сервиса.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / 'app/services/device_first_checkout_service.py').read_text(encoding='utf-8')

    assert 'row.notification_type.startswith(REFERRAL_REWARD_NOTIFICATION_TYPE)' in source, (
        'диспетчер снова сравнивает тип точно — строка с получателем уйдёт в отказ как неизвестная'
    )
    assert 'row.notification_type == REFERRAL_REWARD_NOTIFICATION_TYPE' not in source

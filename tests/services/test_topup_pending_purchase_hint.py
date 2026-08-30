"""Сторожа этапа ДВ-2: сообщение о пополнении называет оставшийся шаг тому, кому он нужен.

Беда, ради которой они написаны, названа живым клиентом, а не выведена из кода. Клиент 106
на прямой вопрос владельца: «Я закинула деньги и думала, что баланс пополнен». Сообщение
кончалось фразой «Баланс пополнен автоматически!» — галочка, «успешно», «автоматически», —
и читалось как закрытая сделка. Подписка не продлилась, 249 ₽ пролежали сутки.

⚠️ ПРОБЕЛ, НАЗВАННЫЙ ВСЛУХ: сторожа на САМО сообщение здесь нет. Текст собирается инлайн
внутри вебхука `process_platega_callback` без шва, за который можно зацепиться, а поднимать
ради этого весь фикстурный аппарат вебхука — дороже пункта этапа. Проверяется живым проходом
(одно пополнение). Здесь закрыт помощник, который решает, что именно скажет последняя строка.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.localization.texts import get_texts
from app.services.payment.common import topup_pending_purchase_hint


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


class _Session:
    """Отдаёт заранее заданные строки подписок и ЗАПРЕЩАЕТ писать."""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        if isinstance(self._rows, Exception):
            raise self._rows
        return SimpleNamespace(all=lambda: list(self._rows))

    async def commit(self):  # pragma: no cover - защитный код
        raise AssertionError('путь отрисовки сообщения о деньгах не имеет права коммитить')


def _factory(rows):
    return lambda: _Session(rows)


def _sub(*, status: str, is_trial: bool, days_left: float):
    return SimpleNamespace(
        status=status,
        is_trial=is_trial,
        end_date=datetime.now(UTC) + timedelta(days=days_left),
    )


async def _hint(rows, language: str = 'ru'):
    user = SimpleNamespace(id=106, language=language)
    with patch('app.services.payment.common.AsyncSessionLocal', _factory(rows)):
        return await topup_pending_purchase_hint(user)


@pytest.mark.anyio
async def test_a_subscriber_whose_time_is_running_out_is_told_the_step_remains() -> None:
    """Случай клиента 106: платная подписка кончается сегодня, деньги внесены под неё."""
    hint = await _hint([_sub(status='active', is_trial=False, days_left=0.12)])
    assert hint == get_texts('ru').TOPUP_SUBSCRIPTION_NOT_PAID_HINT
    # Фраза обязана НЕ обещать, что денег хватает: бот в этот момент не знает ни срока,
    # ни числа устройств. Сторож на свойство, а не на буквы прошлой поломки.
    assert 'хватает' not in hint and 'достаточно' not in hint


@pytest.mark.anyio
async def test_a_subscriber_with_time_to_spare_is_left_alone() -> None:
    """Положил впрок при живой подписке — сообщение не меняется ни на знак."""
    assert await _hint([_sub(status='active', is_trial=False, days_left=10)]) is None


@pytest.mark.anyio
async def test_the_threshold_is_the_one_the_renew_button_already_uses() -> None:
    """Порог не выдуман здесь: тот же, по которому бот рисует «Продлить».

    Две ветки различаются ТОЛЬКО положением срока относительно порога — обе стороны
    проверяются от живого значения настройки, а не от зашитого числа.
    """
    threshold = settings.get_subscriber_menu_renew_threshold_days()
    assert threshold >= 1, threshold
    assert await _hint([_sub(status='active', is_trial=False, days_left=threshold + 5)]) is None
    assert await _hint([_sub(status='active', is_trial=False, days_left=threshold - 0.5)]) is not None


@pytest.mark.anyio
async def test_a_newcomer_and_a_trial_are_both_told_the_step_remains() -> None:
    """Новичку покупать, пробному — покупать. Молчать нельзя ни тому, ни другому."""
    assert await _hint([]) is not None
    assert await _hint([_sub(status='trial', is_trial=True, days_left=30)]) is not None


@pytest.mark.anyio
async def test_a_broken_lookup_never_swallows_the_money_message() -> None:
    """Осечка базы отнимает фразу, но не сообщение о зачислении денег."""
    assert await _hint(RuntimeError('база недоступна')) is None


@pytest.mark.anyio
async def test_the_hint_speaks_the_persons_language() -> None:
    ru = await _hint([], language='ru')
    en = await _hint([], language='en')
    assert ru == get_texts('ru').TOPUP_SUBSCRIPTION_NOT_PAID_HINT
    assert en == get_texts('en').TOPUP_SUBSCRIPTION_NOT_PAID_HINT
    assert ru != en, 'иначе перевода нет, а сторож этого не заметит'

"""Сторожа этапа ДВ-2: сообщение о пополнении называет оставшийся шаг тому, кому он нужен.

Беда, ради которой они написаны, названа живым клиентом, а не выведена из кода. Клиент 106
на прямой вопрос владельца: «Я закинула деньги и думала, что баланс пополнен». Сообщение
кончалось фразой «Баланс пополнен автоматически!» — галочка, «успешно», «автоматически», —
и читалось как закрытая сделка. Подписка не продлилась, 249 ₽ пролежали сутки.

⚠️ ПРОБЕЛ, НАЗВАННЫЙ ВСЛУХ: сторожа на СОБРАННОЕ сообщение здесь нет — закрыт только
помощник, который решает, что скажет последняя строка. ⛔ И честная поправка к самому себе:
первая редакция этой оговорки утверждала «поднимать фикстурный аппарат вебхука дороже пункта
этапа». Это было преувеличением — стенд существует и делает ровно это для pal24
(`tests/services/test_payment_service_webhooks.py:1120`). Сторож не написан СОЗНАТЕЛЬНО, а не
по невозможности; покрыт живым проходом (одно пополнение).
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


def _sub(*, status: str, is_trial: bool, days_left: float, autopay: bool = False, naive: bool = False):
    end = datetime.now(UTC) + timedelta(days=days_left)
    return SimpleNamespace(
        status=status,
        is_trial=is_trial,
        end_date=end.replace(tzinfo=None) if naive else end,
        autopay_enabled=autopay,
    )


async def _hint(rows, language: str = 'ru', *, topup_intent: bool = False):
    user = SimpleNamespace(id=106, language=language)
    with (
        patch('app.services.payment.common.AsyncSessionLocal', _factory(rows)),
        patch(
            'app.services.user_cart_service.user_cart_service.has_topup_intent',
            AsyncMock(return_value=topup_intent),
        ),
    ):
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
    """Новичку покупать, пробному — покупать. Молчать нельзя ни тому, ни другому.

    🔴 Пробная подписка на боевом — это `status='active'` С ПРИЗНАКОМ `is_trial`, а вовсе не
    отдельный статус: строк `status='trial'` в базе НЕТ НИ ОДНОЙ (замер 30.08: active/f — 60,
    active/t — 28, expired/t — 80). Первая версия этого сторожа сеяла `status='trial'` и потому
    проверяла состояние, которого не бывает; мутация «приравнять пробную к платной» её пережила.
    Срок берём ДЛИННЫЙ намеренно: у трёхдневного триала срок и так внутри порога, и разницы между
    защитой и её отсутствием не было бы видно — а длинные пробные существуют (админская выдача).
    """
    assert await _hint([]) is not None
    assert await _hint([_sub(status='active', is_trial=True, days_left=30)]) is not None


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


@pytest.mark.anyio
async def test_the_lookup_asks_only_about_this_persons_live_subscriptions() -> None:
    """Сторож на САМ ЗАПРОС: фикстуры выше подсовывают готовые строки и заборов не проверяют.

    Имена статусов зашиты литералами намеренно — сторож, читающий ту же константу, что и код,
    проверяет совпадение с собой, а не защиту.
    """
    captured = []

    class _Capturing(_Session):
        async def execute(self, statement):
            captured.append(statement)
            return await super().execute(statement)

    user = SimpleNamespace(id=106, language='ru')
    with patch('app.services.payment.common.AsyncSessionLocal', lambda: _Capturing([])):
        await topup_pending_purchase_hint(user)

    assert len(captured) == 1
    sql = str(captured[0].compile(compile_kwargs={'literal_binds': True}))
    assert 'subscriptions.user_id = 106' in sql, sql
    assert 'autopay_enabled' in sql, sql
    # 🔴 Мина HX. По статусу НЕ фильтруем намеренно: забор по автоплатежу живёт внутри этого же
    # запроса, и фильтр по живым статусам прятал от него ИСТЁКШУЮ платную подписку с автоплатежом
    # — а автопродление работает ровно по ней. Сторож стережёт отсутствие фильтра.
    for state in ("'active'", "'trial'", "'limited'", "'expired'"):
        assert state not in sql, (state, sql)
    # Истёкшая и отключённая подписки живыми не считаются: у их владельца шаг остался.
    for dead in ("'expired'", "'disabled'"):
        assert dead not in sql, (dead, sql)


@pytest.mark.anyio
async def test_we_stay_silent_when_the_cart_is_about_to_spend_the_same_money() -> None:
    """🔴 P0 этапа. Автопокупка корзины на боевом ВКЛЮЧЕНА.

    `AUTO_PURCHASE_AFTER_TOPUP_ENABLED` стоит `true` в `system_settings`, в `.env` ключа нет
    (проверено 30.08.2026) — то есть дефолт `False` из кода не действует. Через секунду после
    нашего сообщения автопокупка спишет тот же баланс и пришлёт своё «подписка оформлена».
    Сказать перед ней «нажмите кнопку» — подтолкнуть купить ВТОРОЙ период поверх оплаченного.
    """
    expiring = [_sub(status='active', is_trial=False, days_left=0.1)]
    assert await _hint(expiring, topup_intent=False) is not None
    assert await _hint(expiring, topup_intent=True) is None


@pytest.mark.anyio
async def test_we_stay_silent_when_the_monitor_will_charge_by_itself() -> None:
    """У автоплатежа «сами не спишутся» — прямая ложь: монитор спишет сам."""
    assert await _hint([_sub(status='active', is_trial=False, days_left=1, autopay=True)]) is None
    # Улика, что молчание даёт именно автоплатёж, а не срок: тот же срок без него — фраза есть.
    assert await _hint([_sub(status='active', is_trial=False, days_left=1, autopay=False)]) is not None


@pytest.mark.anyio
async def test_exhausted_traffic_is_still_a_paid_subscription() -> None:
    """`limited` — это оплаченная подписка с кончившимся трафиком, а не повод продавать снова.

    Так же на неё смотрит меню подписчика (`funnel_state.py:88`).
    """
    assert await _hint([_sub(status='limited', is_trial=False, days_left=300)]) is None


@pytest.mark.anyio
async def test_a_naive_end_date_does_not_shift_the_boundary_by_three_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Срок без часового пояса читается как UTC, а не как местное время.

    🔴 Первая редакция этого сторожа была ПУСТОЙ, и нашёл это скептик, а не я. Оба её зонда
    (10 дней и 0.1 дня) стояли далеко от порога, поэтому сдвиг на три часа не переворачивал
    ответ ни в одном. Настоящая мутация — не «убрать нормализацию», а `astimezone(UTC)` вместо
    `replace(tzinfo=UTC)`: так наивное время читается как МЕСТНОЕ. Ловится она только зондом
    НА САМОЙ ГРАНИЦЕ и только в поясе, отличном от UTC, — поэтому пояс задаётся явно, иначе
    сторож был бы пустым на CI и «работал» лишь на машине разработчика.
    """
    monkeypatch.setenv('TZ', 'Europe/Moscow')
    time.tzset()
    threshold = settings.get_subscriber_menu_renew_threshold_days()
    edge = _sub(status='active', is_trial=False, days_left=threshold + 1 / 24, naive=True)
    # Час сверх порога: при чтении срока как UTC запас есть — молчим.
    assert await _hint([edge]) is None
    # И тот же момент времени с явным поясом обязан дать ТОТ ЖЕ ответ.
    aware = _sub(status='active', is_trial=False, days_left=threshold + 1 / 24)
    assert await _hint([aware]) is None


@pytest.mark.anyio
async def test_an_expired_paid_subscription_with_autopay_is_silenced_too() -> None:
    """🔴 Мина HX. Автопродление истёкшей работает ровно по этой строке.

    Первая редакция фильтровала запрос по живым статусам, и забор автоплатежа истёкшую
    подписку не видел: человек получал «нажмите кнопку», а через секунду с него списывали
    за продление. Сегодня таких людей ноль, мина взводится первым же клиентом, который
    включит автоплатёж и даст подписке истечь.
    """
    assert await _hint([_sub(status='expired', is_trial=False, days_left=-2, autopay=True)]) is None
    # Улика, что молчит именно автоплатёж: та же истёкшая без него — фраза есть.
    assert await _hint([_sub(status='expired', is_trial=False, days_left=-2, autopay=False)]) is not None


@pytest.mark.anyio
async def test_a_trial_with_autopay_is_still_told_the_step_remains() -> None:
    """Забор автоплатежа держится на ДВУХ условиях, и второе не было проверено ничем.

    Нашёл скептик: мутация «убрать `not row.is_trial`» переживала все одиннадцать сторожей —
    ни один не сочетал пробную подписку с включённым автоплатежом. Условие в коде верное:
    регулярный автоплатёж берёт только `is_trial == False` (`monitoring_service.py:1834`),
    значит пробному никто ничего не спишет и молчать перед ним нельзя.
    """
    assert await _hint([_sub(status='active', is_trial=True, days_left=30, autopay=True)]) is not None

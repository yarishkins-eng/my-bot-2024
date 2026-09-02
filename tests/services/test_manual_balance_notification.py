"""Этап УБ-1: ручную правку баланса человеку обязаны назвать.

Беда, которую сторожат эти тесты: админ начисляет деньги из кабинета, а клиент об
этом не узнаёт. Из четырёх поверхностей ручной правки баланса говорила ровно одна —
чат-админка; кабинетная карточка, кабинетные массовые действия и Web API молчали.

🔴 Отдельно сторожим КНОПКИ. Ровно на них умерла прежняя попытка: в проекте лежала
готовая ``send_topup_success_to_user`` с кнопкой «🚀 АКТИВИРОВАТЬ ПОДПИСКУ», чей
колбэк ``subscription_buy`` не был зарегистрирован НИГДЕ. Подключи её кто-нибудь —
и человек без подписки получил бы письмо с кнопкой, которая ничего не делает.
Поэтому здесь проверяется не текст кнопки, а то, что её колбэк кто-то слушает.
"""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.user_service import UserService


APP_DIR = Path(__file__).resolve().parents[2] / 'app'


def _user(**overrides):
    defaults = {
        'id': 42,
        'telegram_id': 777,
        'email': None,
        'email_verified': False,
        'balance_kopeks': 50000,
        'language': 'ru',
        'status': 'active',
        'subscriptions': [],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def captured(monkeypatch):
    """Перехватывает транспорт — но НЕ логику сборки сообщения и клавиатуры.

    Подменяется именно доставка (``send_notification``), а собранный текст и кнопки
    приходят сюда настоящими: их строит проверяемый код.
    """
    box = {}

    async def fake_send_notification(
        user, notification_type, context, bot=None, telegram_message=None, telegram_markup=None
    ):
        box['user'] = user
        box['type'] = notification_type
        box['context'] = context
        box['message'] = telegram_message
        box['markup'] = telegram_markup
        return True

    monkeypatch.setattr(
        'app.services.user_service.notification_delivery_service.send_notification',
        fake_send_notification,
    )
    return box


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ---------------------------------------------------------------------------
# Что человек читает
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newcomer_without_subscription_is_told_money_is_not_a_subscription(captured):
    """Самый частый получатель ручного начисления — человек без подписки.

    Ему мало сказать «баланс пополнен»: деньги на счету подписку не включают, и
    прежняя (снесённая) заготовка кричала об этом капслоком. Мысль сохраняем.
    """
    user = _user(subscriptions=[])

    assert await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    message = captured['message']
    assert 'Баланс пополнен' in message
    assert 'её нужно оформить' in message, 'человеку без подписки не сказали, что деньги ≠ подписка'


@pytest.mark.asyncio
async def test_subscriber_is_not_told_to_buy_what_he_already_has(captured):
    user = _user(subscriptions=[SimpleNamespace(status='active')])

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    assert 'её нужно оформить' not in captured['message'], 'подписчику предложили оформить подписку'


@pytest.mark.asyncio
async def test_writeoff_is_named_a_writeoff_and_points_to_support(captured):
    """Решение владельца 02.09.2026: о списании тоже сообщать."""
    user = _user(balance_kopeks=0)

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=-50000)

    message = captured['message']
    assert 'списано' in message
    assert 'пополнен' not in message, 'списание названо пополнением'
    assert 'поддержку' in message, 'у списания нет дороги к человеку, если это ошибка'


@pytest.mark.asyncio
async def test_admin_name_and_operation_note_never_reach_the_client(captured):
    """Подпись операции — служебная. В кабинете рядом с полем прямо написано, что её
    видит клиент в истории операций; в САМОМ сообщении её быть не должно, и имени
    админа тоже."""
    user = _user()

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    assert 'description' not in captured['context']
    assert 'admin' not in ' '.join(str(v) for v in captured['context'])


# ---------------------------------------------------------------------------
# 🔴 Кнопки: тот самый класс ошибки
# ---------------------------------------------------------------------------


def _registered_callbacks() -> set[str]:
    """Все строки, на которые в боте кто-то реально подписан.

    Собирается разбором дерева, а не поиском подстроки: ищем именно
    ``F.data == '<строка>'`` — форму, которой регистрируются обработчики.
    """
    found: set[str] = set()
    pattern = re.compile(r"F\.data\s*==\s*'([a-z0-9_]+)'")
    for path in (APP_DIR / 'handlers').rglob('*.py'):
        found.update(pattern.findall(path.read_text(encoding='utf-8')))
    return found


def test_the_guard_itself_sees_a_known_live_handler():
    """Мета-проверка: если сборщик колбэков сломается и вернёт пустоту, проверка ниже
    станет зелёной и бесполезной. Поэтому сначала убеждаемся, что он видит заведомо
    живой обработчик."""
    registered = _registered_callbacks()
    assert len(registered) > 100, f'сборщик колбэков собрал подозрительно мало: {len(registered)}'
    assert 'subscription_extend' in registered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('subscriptions', 'case'),
    [([], 'без подписки'), ([SimpleNamespace(status='active')], 'с подпиской')],
)
async def test_every_button_of_the_notification_leads_to_a_live_handler(captured, subscriptions, case):
    """🔴 Главный сторож этапа.

    Он покраснеет, если кнопку в этом сообщении когда-нибудь заведут на колбэк,
    который никто не слушает, — то есть повторят ошибку ``send_topup_success_to_user``.
    """
    user = _user(subscriptions=subscriptions)
    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    callbacks = _callbacks(captured['markup'])
    assert callbacks, f'{case}: у сообщения о деньгах нет ни одной кнопки'

    registered = _registered_callbacks()
    dead = [cb for cb in callbacks if cb not in registered]
    assert not dead, f'{case}: кнопка ведёт в никуда — колбэк {dead} не зарегистрирован ни одним обработчиком'


def test_the_dead_template_stayed_deleted():
    """``send_topup_success_to_user`` снесена решением владельца 02.09.2026: вызовов
    ноль, две кнопки из трёх мертвы. Сторож против воскрешения.

    Смотрим ДЕРЕВО, а не текст файла: объяснение, почему заготовка снесена, живёт
    в комментарии рядом и упоминает те же имена. Сторож, считающий комментарий
    нарушением, заставил бы стереть объяснение — а оно и есть защита от повтора.
    """
    tree = ast.parse((APP_DIR / 'services' / 'user_service.py').read_text(encoding='utf-8'))

    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert 'send_topup_success_to_user' not in functions, 'снесённая заготовка вернулась в код'

    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for cb in ('subscription_buy', 'subscription_add_devices'):
        assert cb not in literals, f'вернулась кнопка {cb}, которую никто не слушает'


# ---------------------------------------------------------------------------
# Все четыре поверхности, а не только та, что чинили последней
# ---------------------------------------------------------------------------


SURFACES = [
    ('cabinet/routes/admin_users.py', 'update_user_balance', 'кабинетная карточка пользователя'),
    ('cabinet/routes/admin_bulk_actions.py', '_do_add_balance', 'кабинетные массовые действия'),
    ('webapi/routes/users.py', 'update_balance', 'Web API'),
    ('services/user_service.py', 'update_user_balance', 'чат-админка'),
]


def _function_source(relative: str, name: str) -> str:
    path = APP_DIR / relative
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding='utf-8'), node) or ''
    raise AssertionError(f'{relative}: не нашёл функцию {name} — она переименована или переехала')


@pytest.mark.parametrize(('relative', 'name', 'human'), SURFACES)
def test_every_manual_balance_surface_tells_the_client(relative, name, human):
    """Дыр было три из четырёх. Сторож против того, чтобы починили одну и забыли остальные."""
    source = _function_source(relative, name)
    assert 'notify_balance_change' in source or 'send_balance_change_notification' in source, (
        f'{human}: меняет баланс и молчит — человек не узнает о своих деньгах'
    )

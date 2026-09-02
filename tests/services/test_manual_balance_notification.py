"""Этап УБ-1: ручную правку баланса человеку обязаны назвать.

Беда, которую сторожат эти тесты: админ начисляет деньги из кабинета, а клиент об
этом не узнаёт. Из четырёх поверхностей ручной правки баланса говорила ровно одна —
чат-админка; кабинетная карточка, кабинетные массовые действия и Web API молчали.

🔴 Отдельно сторожим КНОПКИ. Ровно на них умерла прежняя попытка: в проекте лежала
готовая ``send_topup_success_to_user`` с кнопкой «🚀 АКТИВИРОВАТЬ ПОДПИСКУ», чей
колбэк ``subscription_buy`` не был зарегистрирован НИГДЕ. Подключи её кто-нибудь —
и человек без подписки получил бы письмо с кнопкой, которая ничего не делает.
Поэтому здесь проверяется не текст кнопки, а то, что её колбэк кто-то слушает.

⚠️ Ожидания нескольких тестов переписаны по итогам первой волны ревью. Обоснования —
в их же докстрингах: молча подкрученное ожидание делает будущий регресс невидимым.
"""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.user_service import UserService
from app.utils.funnel_state import _ALIVE_STATUSES


APP_DIR = Path(__file__).resolve().parents[2] / 'app'


async def _noop_close():
    return None


def _sub(status: str):
    """Подписка в заданном состоянии.

    Код судит по ``actual_status`` — свойству, которое учитывает срок; весь остальной
    бот читает именно его. Фикстура обязана давать то же поле, иначе она сторожит
    форму, на которой беда не воспроизводится.
    """
    return SimpleNamespace(status=status, actual_status=status)


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
    if markup is None:
        return []
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ---------------------------------------------------------------------------
# 🔴 Состояние подписки: главная находка первой волны
# ---------------------------------------------------------------------------


def test_alive_statuses_are_taken_from_the_shared_classifier():
    """Свой набор «живых» статусов уже дал P1 и не должен появиться снова.

    Первая редакция писала `{active, expired, trial}` — то есть считала живой
    ИСТЁКШУЮ подписку и мёртвой `limited` (платящий человек, у которого кончился
    трафик). Такой набор в проекте уже есть, и он ровно один.
    """
    assert {'active', 'trial', 'limited'} == _ALIVE_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize('status', sorted(['active', 'trial', 'limited']))
async def test_live_subscriber_is_not_told_his_subscription_is_missing(captured, status):
    """🔴 Переписанное ожидание, обоснование.

    Прежний тест проверял только `active` и потому не видел беды: `limited` — это
    платящий клиент, у которого кончился трафик, и именно ему чаще всего начисляют
    компенсацию. Он читал «подписку нужно оформить» и получал кнопку покупки, то есть
    его толкали купить вторую. Нашли три линзы ревью независимо.
    """
    user = _user(subscriptions=[_sub(status)])

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    message = captured['message']
    assert 'оформите' not in message, f'{status}: живому подписчику сказали оформить подписку'
    assert 'не работает' not in message, f'{status}: живую подписку назвали неработающей'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', sorted(['expired', 'disabled', 'pending']))
async def test_stopped_subscription_is_named_stopped_and_not_missing(captured, status):
    """Подписка есть, но не работает — это третий случай, а не один из двух.

    Он появился потому, что при ОБЫЧНОМ пополнении истёкшая подписка платившего
    продлевается сама (`try_auto_extend_expired_after_topup`), а ручное начисление этот
    путь намеренно не трогает (мина JN). Человек по привычке решит, что VPN включился.
    """
    user = _user(subscriptions=[_sub(status)])

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    message = captured['message']
    assert 'не работает' in message, f'{status}: человеку не сказали, что VPN сейчас выключен'
    assert 'оформите' not in message, f'{status}: существующую подписку назвали отсутствующей'


@pytest.mark.asyncio
async def test_newcomer_without_any_subscription_is_told_money_is_not_a_subscription(captured):
    """Самый частый получатель ручного начисления — человек без подписки вообще."""
    user = _user(subscriptions=[])

    assert await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    message = captured['message']
    assert 'Баланс пополнен' in message
    assert 'оформите' in message, 'человеку без подписки не сказали, что деньги ≠ подписка'


# ---------------------------------------------------------------------------
# Ноль, списание, причина
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_amount_sends_nothing(captured):
    """🔴 Ноль проходил валидацию кабинета и уходил в ветку СПИСАНИЯ.

    Клиент получал «💸 С баланса списано 0 ₽ … Если это ошибка — напишите в поддержку»
    ни за что. Нашли четыре линзы ревью разом.
    """
    user = _user()

    assert await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=0) is False
    assert 'message' not in captured, 'на нулевую сумму клиенту всё-таки что-то отправили'


@pytest.mark.asyncio
async def test_writeoff_is_named_a_writeoff_and_carries_support_contact(captured):
    """Решение владельца 02.09.2026: о списании тоже сообщать."""
    user = _user(balance_kopeks=0)

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=-50000)

    message = captured['message']
    assert 'списано' in message
    assert 'пополнен' not in message, 'списание названо пополнением'
    assert 'ошибка' in message, 'у списания нет дороги к человеку, если это ошибка'


@pytest.mark.asyncio
async def test_writeoff_has_no_button_at_all(captured):
    """🔴 Переписанное ожидание, обоснование.

    Прежняя редакция ставила под списание ту же кнопку, что под начислением. Бот
    работает в режиме с картинкой, и оба живых колбэка это сообщение УНИЧТОЖАЮТ:
    `funnel_tariffs` роняет `edit_media` на текстовом сообщении и уходит в ветку
    `callback.message.delete()`, `subscription_extend` перерисовывает его поверх.
    Человек, которому написали «если это ошибка — напишите в поддержку», нажал бы
    единственную кнопку и потерял сумму и дату, которые собирался процитировать.
    Плюс тон: под «у вас забрали деньги» стояло «Продлить подписку».
    """
    user = _user()

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=-50000)

    assert captured['markup'] is None, 'под списанием стоит кнопка, которая сотрёт это сообщение'


@pytest.mark.asyncio
async def test_reason_written_by_admin_reaches_the_client(captured):
    """Поле «Описание» в кабинете подписано «увидит клиент», и клиент видит его в
    истории операций. В самом сообщении его не было — списание приходило голым числом."""
    user = _user()

    await UserService().send_balance_change_notification(
        bot=object(), user=user, amount_kopeks=-50000, reason='Возврат за заказ №67'
    )

    assert 'Возврат за заказ №67' in captured['message']


@pytest.mark.asyncio
async def test_reason_cannot_break_the_message_with_markup(captured):
    """Описание пишет человек, а сообщение уходит с parse_mode=HTML."""
    user = _user()

    await UserService().send_balance_change_notification(
        bot=object(), user=user, amount_kopeks=50000, reason='<b>жирный</b> & прочее'
    )

    message = captured['message']
    assert '<b>жирный</b>' not in message, 'разметка из описания админа попала в сообщение как разметка'
    assert '&lt;b&gt;' in message


@pytest.mark.asyncio
async def test_admin_name_never_reaches_the_client(captured):
    """Имя админа скрыто; описание — наоборот, показывается (см. тест выше).

    🔴 Переписано: прежняя редакция перебирала КЛЮЧИ словаря вместо значений и
    доказывала лишь, что слова «admin» нет в имени ключа. Нашла линза денег.
    """
    user = _user()

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    values = ' '.join(str(v) for v in captured['context'].values())
    assert 'admin' not in values.lower()
    assert 'description' not in captured['context']


# ---------------------------------------------------------------------------
# 🔴 Кнопки: тот самый класс ошибки
# ---------------------------------------------------------------------------


def _registered_callbacks() -> set[str]:
    """Все строки, на которые в боте кто-то реально подписан."""
    found: set[str] = set()
    pattern = re.compile(r"F\.data\s*==\s*'([a-z0-9_]+)'")
    for path in (APP_DIR / 'handlers').rglob('*.py'):
        found.update(pattern.findall(path.read_text(encoding='utf-8')))
    return found


def test_the_guard_itself_sees_a_known_live_handler():
    """Мета-проверка: если сборщик колбэков вернёт пустоту, проверка ниже станет
    зелёной и бесполезной."""
    registered = _registered_callbacks()
    assert len(registered) > 100, f'сборщик колбэков собрал подозрительно мало: {len(registered)}'
    assert 'subscription_extend' in registered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('subscriptions', 'case'),
    [([], 'без подписки'), ([_sub('active')], 'с живой'), ([_sub('expired')], 'с остановленной')],
)
async def test_every_button_of_the_notification_leads_to_a_live_handler(captured, subscriptions, case):
    """🔴 Главный сторож этапа.

    Покраснеет, если кнопку заведут на колбэк, который никто не слушает, — то есть
    повторят ошибку ``send_topup_success_to_user``.
    """
    user = _user(subscriptions=subscriptions)
    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    callbacks = _callbacks(captured['markup'])
    assert callbacks, f'{case}: у сообщения о начислении нет ни одной кнопки'

    registered = _registered_callbacks()
    dead = [cb for cb in callbacks if cb not in registered]
    assert not dead, f'{case}: кнопка ведёт в никуда — колбэк {dead} не зарегистрирован ни одним обработчиком'


def test_the_dead_template_stayed_deleted():
    """``send_topup_success_to_user`` снесена решением владельца 02.09.2026.

    Смотрим ДЕРЕВО, а не текст файла: объяснение, почему заготовка снесена, живёт
    в комментарии рядом и упоминает те же имена.
    """
    tree = ast.parse((APP_DIR / 'services' / 'user_service.py').read_text(encoding='utf-8'))

    functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert 'send_topup_success_to_user' not in functions, 'снесённая заготовка вернулась в код'

    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for cb in ('subscription_buy', 'subscription_add_devices'):
        assert cb not in literals, f'вернулась кнопка {cb}, которую никто не слушает'


def test_the_orphaned_wrapper_is_gone_too():
    """``notify_balance_topup`` жила ради снесённой заготовки и осталась без вызовов.
    Нашла линза соответствия замыслу."""
    source = (APP_DIR / 'services' / 'notification_delivery_service.py').read_text(encoding='utf-8')
    assert 'def notify_balance_topup' not in source


# ---------------------------------------------------------------------------
# Все четыре поверхности, а не только та, что чинили последней
# ---------------------------------------------------------------------------


SURFACES = [
    ('cabinet/routes/admin_users.py', 'update_user_balance', 'кабинетная карточка пользователя'),
    ('cabinet/routes/admin_bulk_actions.py', '_do_add_balance', 'кабинетные массовые действия'),
    ('webapi/routes/users.py', 'update_balance', 'Web API'),
    ('services/user_service.py', 'update_user_balance', 'чат-админка'),
]

NOTIFIERS = {'notify_balance_change', 'send_balance_change_notification'}


def _called_names(relative: str, name: str) -> set[str]:
    """Что функция реально ВЫЗЫВАЕТ.

    🔴 Первая редакция этого сторожа искала имя в тексте функции — и мутация показала,
    что он пуст: строка ``from ... import notify_balance_change`` остаётся на месте,
    даже когда сам вызов вырезан.
    """
    path = APP_DIR / relative
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            called = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    func = inner.func
                    if isinstance(func, ast.Name):
                        called.add(func.id)
                    elif isinstance(func, ast.Attribute):
                        called.add(func.attr)
            return called
    raise AssertionError(f'{relative}: не нашёл функцию {name} — она переименована или переехала')


def test_the_call_guard_itself_is_not_blind():
    called = _called_names('services/user_service.py', 'update_user_balance')
    assert 'get_user_by_id' in called, 'сборщик вызовов ослеп'


@pytest.mark.parametrize(('relative', 'name', 'human'), SURFACES)
def test_every_manual_balance_surface_tells_the_client(relative, name, human):
    """Дыр было три из четырёх. Сторож против того, чтобы починили одну и забыли остальные."""
    called = _called_names(relative, name)
    assert called & NOTIFIERS, f'{human}: меняет баланс и молчит — человек не узнает о своих деньгах'


def test_bulk_reports_delivery_by_a_field_not_by_a_russian_tail():
    """🔴 Переписанное ожидание, обоснование.

    Первая редакция клеила ' (клиент не уведомлён)' в конец английской строки
    `message`. Кабинет показывает `message` только у НЕуспешных строк, а начисление
    успешно — отметка попадала в невидимое ведро. Нашли три линзы независимо.
    """
    source = (APP_DIR / 'cabinet' / 'routes' / 'admin_bulk_actions.py').read_text(encoding='utf-8')
    assert 'клиент не уведомлён' not in source, 'русский хвост вернулся в англоязычную строку API'

    schema = (APP_DIR / 'cabinet' / 'schemas' / 'bulk_actions.py').read_text(encoding='utf-8')
    assert 'notified' in schema, 'результат массового действия снова не сообщает об исходе доставки'


# ---------------------------------------------------------------------------
# Деньги важнее письма
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_failure_never_rolls_back_the_money(monkeypatch):
    """Если Телеграм недоступен, начисление обязано остаться начислением."""
    from app.services import user_service as module

    def exploding_create_bot(*args, **kwargs):
        raise RuntimeError('Telegram недоступен')

    async def fake_get_user_by_id(db, user_id):
        return _user()

    monkeypatch.setattr('app.bot_factory.create_bot', exploding_create_bot)
    monkeypatch.setattr(module, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(module.settings, 'BOT_TOKEN', 'test-token')

    assert await module.notify_balance_change(db=object(), user_id=42, amount_kopeks=50000) is False


@pytest.mark.asyncio
async def test_undelivered_is_reported_as_undelivered(monkeypatch):
    """🔴 Самый частый отказ доставки НЕ бросает исключение.

    Человек заблокировал бота, или у него нет ни Телеграма, ни подтверждённой почты —
    ``send_notification`` просто возвращает False. Если наверх при этом уйдёт True,
    кабинет напишет админу «отправлено» про человека, который ничего не получил.

    Сторож заведён мутацией: она вставляла ``return True`` перед настоящим возвратом,
    и весь набор оставался зелёным.
    """
    from app.services import user_service as module

    class FakeBot:
        session = SimpleNamespace(close=_noop_close)

    async def fake_get_user_by_id(db, user_id):
        return _user()

    async def fake_send(self, bot, user, amount_kopeks, reason=None):
        return False

    monkeypatch.setattr('app.bot_factory.create_bot', lambda *a, **k: FakeBot())
    monkeypatch.setattr(module, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(module.settings, 'BOT_TOKEN', 'test-token')
    monkeypatch.setattr(module.UserService, 'send_balance_change_notification', fake_send)

    assert await module.notify_balance_change(db=object(), user_id=42, amount_kopeks=50000) is False


@pytest.mark.asyncio
async def test_delivery_result_is_reported_truthfully(monkeypatch):
    """Обратная сторона: удачную доставку нельзя показывать как неудачу."""
    from app.services import user_service as module

    class FakeBot:
        session = SimpleNamespace(close=_noop_close)

    async def fake_get_user_by_id(db, user_id):
        return _user()

    async def fake_send(self, bot, user, amount_kopeks, reason=None):
        return True

    monkeypatch.setattr('app.bot_factory.create_bot', lambda *a, **k: FakeBot())
    monkeypatch.setattr(module, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(module.settings, 'BOT_TOKEN', 'test-token')
    monkeypatch.setattr(module.UserService, 'send_balance_change_notification', fake_send)

    assert await module.notify_balance_change(db=object(), user_id=42, amount_kopeks=50000) is True


@pytest.mark.asyncio
async def test_caller_supplied_bot_is_not_closed_by_us(monkeypatch):
    """В массовых действиях бот один на весь прогон, и закрывать его нам нельзя —
    иначе второй человек в списке остался бы без сообщения."""
    from app.services import user_service as module

    closed = []

    async def close():
        closed.append(True)

    shared_bot = SimpleNamespace(session=SimpleNamespace(close=close))

    async def fake_get_user_by_id(db, user_id):
        return _user()

    async def fake_send(self, bot, user, amount_kopeks, reason=None):
        assert bot is shared_bot
        return True

    monkeypatch.setattr(module, 'get_user_by_id', fake_get_user_by_id)
    monkeypatch.setattr(module.settings, 'BOT_TOKEN', 'test-token')
    monkeypatch.setattr(module.UserService, 'send_balance_change_notification', fake_send)

    assert await module.notify_balance_change(db=object(), user_id=42, amount_kopeks=50000, bot=shared_bot) is True
    assert not closed, 'закрыли чужого бота — следующий человек в списке остался бы без сообщения'

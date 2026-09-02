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
@pytest.mark.parametrize('status', sorted(['active', 'limited']))
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
async def test_trial_is_warned_that_money_will_not_extend_it(captured):
    """Пробный период — единственное живое состояние, где деньги сами ничего не сделают.

    Автоплатёж триалы исключает явно (`Subscription.is_trial == False` в выборке
    monitoring_service). Человек иначе решит, что срок продлится сам: триал кончится,
    доступ погаснет, деньги пролежат. Нашёл прогон сценария второй волны.
    """
    user = _user(subscriptions=[_sub('trial')])

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    message = captured['message']
    assert 'Пробный период' in message, 'триалу не сказали, что деньгами он не продлевается'
    assert 'не работает' not in message, 'работающий триал назвали неработающим'


@pytest.mark.asyncio
async def test_pending_subscription_is_not_offered_extension(captured):
    """🔴 P0 второй волны.

    Продление ЧИНИТ запись для expired/disabled/limited/trial (`extend_subscription`
    переводит их в active), а для pending только пишет предупреждение: деньги списались
    бы, статус остался бы pending, и кабинетный экран продления такую запись потом уже
    не примет (`_non_renewable = {DISABLED, PENDING}`). Поэтому кнопка — «Тарифы».
    """
    user = _user(subscriptions=[_sub('pending')])

    await UserService().send_balance_change_notification(bot=object(), user=user, amount_kopeks=50000)

    assert _callbacks(captured['markup']) == ['funnel_tariffs'], (
        'зависшую подписку повели на продление — деньги спишутся, запись останется сломанной'
    )


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

    # 🔴 Проверка «нет слова admin среди значений» пережила мутацию, которая клала в
    # контекст НАСТОЯЩЕЕ имя («Пётр»): слова admin в нём нет. Поэтому пришпиливаем
    # ТОЧНЫЙ состав — любое новое поле обязано быть замечено и обосновано.
    assert set(captured['context']) == {
        'amount_kopeks',
        'amount_rubles',
        'new_balance_kopeks',
        'new_balance_rubles',
        'formatted_amount',
        'formatted_balance',
    }, 'в контекст уведомления добавили поле — проверьте, что оно не про админа'

    values = ' '.join(str(v) for v in captured['context'].values())
    assert 'admin' not in values.lower()


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


def test_delivery_outcome_travels_with_the_streaming_event():
    """🔴 Кабинет ходит в массовые действия ТОЛЬКО потоком.

    Поле notified жило в схеме ответа, но оба генератора SSE собирают словарь события
    руками — и без него счётчик «Не уведомлены» на экране структурно всегда нулевой.
    Нашли скептик и прогон сценария независимо.
    """
    source = (APP_DIR / 'cabinet' / 'routes' / 'admin_bulk_actions.py').read_text(encoding='utf-8')
    tree = ast.parse(source)

    events = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if 'type' in keys and 'current' in keys and 'total' in keys:
            events += 1
            assert 'notified' in keys, 'событие потока не несёт исход доставки — счётчик на экране будет пуст'
    assert events >= 2, f'нашёл {events} событий потока вместо двух — генераторы переименованы или переехали'


def test_bulk_never_lets_a_broken_token_mark_credited_rows_as_failed():
    """При пустом ИЛИ БИТОМ BOT_TOKEN create_bot бросает. Деньги к этому моменту уже
    закоммичены, и исключение пометило бы строку ошибкой: владелец повторил бы прогон
    и начислил второй раз.

    🔴 Переписано ревизией: прежняя редакция сторожила проверку `settings.BOT_TOKEN`,
    а она смотрит только на НЕПУСТОТУ. `create_bot()` валидирует форму токена и на
    кривом непустом бросит всё равно. Теперь исключение ловится и наружу отдаётся
    None — сторож проверяет это, а не форму условия.
    """
    source = (APP_DIR / 'cabinet' / 'routes' / 'admin_bulk_actions.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_get_bot')

    handlers = [h for n in ast.walk(func) if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, 'подъём бота снова без защиты — битый токен пометит ошибкой выданные деньги'
    assert any(
        isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is None
        for h in handlers
        for node in ast.walk(h)
    ), 'исключение ловится, но наружу не отдаётся None — вызывающий всё равно упадёт'

    assert 'bot=_get_bot()' in source


def test_websocket_reports_whether_anyone_actually_received_it():
    """Отправка в ноль открытых окон — не доставка.

    Пока send_to_user молча выходил, отчёт говорил админу «сообщение отправлено» про
    человека без Телеграма, у которого не открыто ни одного окна и не поднята почта.
    """
    source = (APP_DIR / 'cabinet' / 'routes' / 'websocket.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_to_user')
    returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
    assert returns, 'send_to_user снова ничего не возвращает — отчёт о доставке начнёт врать'
    assert any(isinstance(r.value, ast.Constant) and r.value.value is False for r in returns), (
        'нет ветки «никто не получил» — ноль открытых окон снова считался бы доставкой'
    )

    delivery = (APP_DIR / 'services' / 'notification_delivery_service.py').read_text(encoding='utf-8')
    assert 'return await self.ws_manager.send_to_user' in delivery, (
        'маршрутизатор снова объявляет доставку по отсутствию исключения'
    )


def test_chat_admin_does_not_turn_a_failed_message_into_a_failed_credit():
    """Деньги коммитятся раньше письма. Пока вызов стоял в общем try, сбой отправки
    возвращал False, и админ читал «❌ Ошибка изменения баланса» — приглашение
    начислить второй раз."""
    called = _called_names('services/user_service.py', 'update_user_balance')
    assert 'send_balance_change_notification' in called

    tree = ast.parse((APP_DIR / 'services' / 'user_service.py').read_text(encoding='utf-8'))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'update_user_balance')
    guarded = False
    for node in ast.walk(func):
        if isinstance(node, ast.Try):
            names = {
                inner.func.attr
                for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
            }
            # свой try, а не общий: общий охватывает и списание баланса
            if 'send_balance_change_notification' in names and 'subtract_user_balance' not in names:
                guarded = True
    assert guarded, 'уведомление снова стоит в общем try — его сбой читается как «баланс не изменён»'


def test_cabinet_route_rejects_amounts_that_would_print_as_zero():
    """Цены округляются: до 50 копеек клиент прочитал бы «списано 0 ₽» — та же
    бессмыслица, от которой писали защиту от нуля. Достижимо опечаткой «0.5»."""
    source = (APP_DIR / 'cabinet' / 'routes' / 'admin_users.py').read_text(encoding='utf-8')
    assert 'abs(request.amount_kopeks) < 100' in source, 'копеечные суммы снова проходят'


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

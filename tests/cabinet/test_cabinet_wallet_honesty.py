"""Этап ДВ-3: кабинет перестаёт врать про деньги после оплаты.

Два сторожа на две разные лжи одного пути:

* история кошелька показывала приход от банка по прямой оплате картой — ПЛЮСОМ, при
  неизменившемся балансе (симптом Б);
* экран результата пополнения объявлял сделку закрытой всем подряд (симптом А).

⚠️ Сторожа намеренно проверяют СВОЙСТВО, а не букву сегодняшней поломки: список скрытых
типов подменяется, чтобы поймать правку, которая перепишет его литералом вместо чтения.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.cabinet.routes import balance as balance_route
from app.cabinet.schemas.balance import PendingPaymentResponse
from app.database.models import PaymentMethod, TransactionType
from app.services.payment_verification_service import PendingPayment


class _RecordingSession:
    """Сессия, которая ничего не исполняет, но запоминает сами запросы."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(
            scalar=lambda: 0,
            scalars=lambda: SimpleNamespace(all=list),
        )

    def compiled(self) -> list[str]:
        return [str(s.compile(compile_kwargs={'literal_binds': True})) for s in self.statements]


def _service_source() -> str:
    """Исходник службы выдачи, найденный от САМОГО ТЕСТА, а не от текущей папки.

    Нашла ревизия: два сторожа читали файл голым относительным путём, и запуск не из корня
    репозитория ронял их `FileNotFoundError`. Остальные подобные сторожа проекта считают от
    `__file__` — делаем как они.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return (root / 'app' / 'services' / 'device_first_checkout_service.py').read_text(encoding='utf-8')


@pytest.mark.asyncio
async def test_wallet_history_hides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """История операций обязана быть ПОЛНОЙ — решение владельца 31.08.2026.

    🔴 Этот сторож заменил собой прямо противоположный, написанный часом раньше в этом же
    этапе, и замена не косметическая. Первая редакция прятала приход от банка по прямой
    оплате картой, копируя поведение бота. Владелец посмотрел на живой экран и постановил
    обратное: спрятанная половина проводки оставляет одинокое списание с кошелька, которого
    кошелёк не касался, — а прослеживаться должен каждый шаг денег.

    Стережём СВОЙСТВО: в запросе нет отбора по типу, кроме явно запрошенного человеком.
    """
    session = _RecordingSession()
    await balance_route.get_transactions(page=1, per_page=20, type=None, user=SimpleNamespace(id=42), db=session)

    sql = session.compiled()
    assert len(sql) == 2, 'ожидались два запроса: выборка и счётчик'
    for statement in sql:
        assert 'provider_receipt' not in statement, 'история снова что-то прячет'
        assert 'NOT IN' not in statement.upper(), 'в запрос вернулся отбор по типу'


def test_provider_receipt_is_a_credit_in_the_bot() -> None:
    """Приход от банка — ПРИХОД, а не расход.

    Бот рисовал его минусом, человек видел «−1199 ₽» дважды подряд, и этап РФ-1 вместо
    починки знака убрал строку с глаз. Сокрытие снято, поэтому знак обязан быть верным —
    иначе вернётся ровно та поломка, ради которой прятали.
    """
    from app.handlers.balance.main import CREDIT_TRANSACTION_TYPES

    assert TransactionType.PROVIDER_RECEIPT.value in CREDIT_TRANSACTION_TYPES


def test_the_bot_no_longer_hides_anything_from_the_wallet_history() -> None:
    """Механизма сокрытия в боте больше нет — ни пустого, ни спящего.

    Оставить пустой список значило бы оставить приглашение снова что-нибудь в него положить.
    """
    from app.handlers.balance import main as bot_balance

    assert not hasattr(bot_balance, 'HIDDEN_FROM_WALLET_HISTORY')


def test_no_english_machine_text_reaches_the_wallet_history() -> None:
    """Проволока-растяжка против возврата машинного английского в подписи операций.

    ⚠️ ЧЕСТНО ПРО ГРАНИЦУ: это сторож по ИСХОДНИКУ, а не по поведению, и сам по себе он
    доказывает мало — правило проекта прямо предупреждает, что такие сторожа слабые.
    Настоящая проверка (провести заказ через выдачу и прочитать созданные строки) требует
    стенда, которого у этой функции нет: её тесты живут на фейковых сессиях и до создания
    транзакций не доходят. Пробел записан вместо того, чтобы делать вид, что его нет.
    Растяжка ловит ровно одну регрессию — возврат прежних шаблонов, — и этого достаточно,
    чтобы следующий не вернул их не глядя.
    """
    source = _service_source()
    assert 'Device-first provider receipt for checkout' not in source
    assert 'Device-first direct sale' not in source
    # 🔴 Третий шаблон в том же файле, найден критиком полноты: ветка списания с кошелька
    # писала «Device-first: 30 дн., 2 устр., checkout <uuid>». Путь спит, но имя сторожа
    # обещало больше, чем он делал.
    assert 'дн., ' not in source or 'устр., checkout' not in source
    # А это — то, что человек обязан увидеть вместо них.
    assert 'Платёж картой получен' in source
    assert 'Оплата подписки с баланса' in source
    assert 'Оплата подписки картой' in source
    # 🔴 Добавлено после мутационного прогона: без этих двух строк мутация «печатать срок
    # числом дней вместо помощника» переживала весь набор. Прежний сторож проверял САМ
    # помощник (`format_period_description(180) == '6 месяцев'`) — то есть совпадение, а не
    # то, что подпись им пользуется. Стережём обе подписи поимённо.
    assert source.count('format_period_description(int(snapshot["period_days"]))') == 2
    # Источник денег обязан различаться: «с баланса» при оплате кошельком — ложь при оплате
    # картой, потому что баланс тогда не двигался вовсе.
    assert "if checkout.funding_mode == 'wallet' else 'Оплата подписки картой: '" in source


def test_sale_descriptions_are_not_mistaken_for_addons() -> None:
    """Подпись продажи не должна выглядеть как докупка устройств или трафика.

    🔴 Поймано вопросом владельца «а это безопасно?» — уже после того, как код был написан,
    отревьюен шестью линзами и прогнан мутациями. Отчёты о продажах отличают допы от продаж
    ЕДИНСТВЕННЫМ способом: по подстроке в описании (`crud/transaction.py`). Первая редакция
    подписи содержала «лимит устройств N», и каждая прямая продажа картой уехала бы в
    статистику допов. Сам файл с шаблонами об этом предупреждает — предупреждение я прочитал
    уже после того, как в него наступил.

    Сторож привязан к ЖИВОМУ списку шаблонов: добавят новый — проверка ужесточится сама.
    """
    from app.database.crud.transaction import ADDON_DESCRIPTION_PATTERNS

    source = _service_source()
    prefixes = (
        'Платёж картой получен: подписка на ',
        'Оплата подписки с баланса: ',
        'Оплата подписки картой: ',
    )
    for prefix in prefixes:
        assert prefix in source, f'подпись {prefix!r} исчезла'

    for pattern in ADDON_DESCRIPTION_PATTERNS:
        word = pattern.strip('%').lower()
        for prefix in prefixes:
            assert word not in prefix.lower(), f'подпись {prefix!r} уводит продажу в статистику допов'

    assert 'лимит устройств' not in source


def test_period_in_the_description_speaks_human() -> None:
    """Срок в подписи описывает тот же помощник, что и весь проект.

    Это уже проверка ПОВЕДЕНИЯ: 180 дней он обязан назвать «6 месяцев», а не «180 days».
    Без него подпись осталась бы русской по форме и машинной по содержанию.
    """
    from app.utils.pricing_utils import format_period_description

    assert format_period_description(180) == '6 месяцев'
    assert format_period_description(30) == '1 месяц'
    assert format_period_description(2) == '2 дня'


@pytest.mark.asyncio
async def test_latest_payment_route_carries_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Маршрут, которым в кабинет приходит КЛИЕНТ 106, тоже несёт поле.

    🔴 Дыру нашла ревизия перед выкладкой, и она худшего сорта: сторожа стояли на двух
    маршрутах из трёх, а непокрытым остался ровно тот, что обслуживает названного
    пострадавшего. Возврат по диплинку `startapp=tup-platega-ok` поднимает мини-приложение
    заново, `sessionStorage` пуст, опрос идёт ПО СПОСОБУ — то есть сюда. Ревизия сняла
    `_with_purchase_step` с этой ветки, и все 616 тестов остались зелёными.
    """
    from app.services.payment import common as payment_common

    async def fake_hint(_user):
        return 'Деньги на балансе, но подписка сама не оплатится.'

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    payment = SimpleNamespace(
        id=777,
        user_id=106,
        correlation_id='corr-777',
        amount_kopeks=24_900,
        status='PAID',
        is_paid=True,
        created_at=datetime.now(UTC),
        expires_at=None,
        user=user,
    )

    class _Session:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: payment))

    response = await balance_route.get_latest_payment_by_method(method='platega', user=user, db=_Session())

    assert response.purchase_step_pending is True


def test_provider_receipt_is_not_a_debit_in_the_cabinet() -> None:
    """В кабинетном маршроте истории приход обязан оставаться плюсом.

    Список дебетов зашит литералами прямо в маршруте; попади туда `provider_receipt` — и
    человек снова увидит списание с кошелька, которого кошелёк не касался.
    """
    source = _route_source()
    debit_line = next(line for line in source.splitlines() if 'is_debit = t.type in' in line)
    assert 'provider_receipt' not in debit_line


def _route_source() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return (root / 'app' / 'cabinet' / 'routes' / 'balance.py').read_text(encoding='utf-8')


def test_history_dedupe_key_stays_unique_per_order() -> None:
    """Склейка одинаковых записей не должна съедать РАЗНЫЕ покупки.

    🔴 Мина IE, наша. Ключ склейки держался на том, что описание несло номер заказа и потому
    было уникальным. Подписи стали типовыми — и две одинаковые по цене и сроку покупки одного
    человека в одну минуту схлопнулись бы в одну строку. Номер проводки возвращает уникальность.

    ⚠️ Граница: это сторож по ИСХОДНИКУ. Настоящая проверка требует поднять обработчик с
    aiogram-объектами, и такого стенда у экрана истории нет. Пробел назван, а не спрятан:
    растяжка ловит ровно снятие поля из ключа — то есть возврат мины IE.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / 'app' / 'handlers' / 'balance' / 'main.py').read_text(encoding='utf-8')

    # Два места: страница и счётчик всего. Оба обязаны считать по одному правилу.
    assert source.count('transaction.device_first_ledger_key,') == 2, (
        'номер проводки исчез из ключа склейки — вернулась мина IE'
    )


def test_purchase_step_flag_is_silent_by_default() -> None:
    """Умолчание `False` — это «молчим», а не «неизвестно».

    Кабинет, выложенный раньше бота, поля не увидит. Обещать оставшийся шаг тому, за кого
    деньги потратит автопокупка или автоплатёж, опаснее, чем промолчать, — поэтому старое
    поведение обязано быть умолчанием, а не новое.
    """
    response = PendingPaymentResponse(
        id=1,
        method='platega',
        method_display='Platega',
        identifier='abc',
        amount_kopeks=24_900,
        amount_rubles=249.0,
        status='PAID',
        status_emoji='✅',
        status_text='Оплачен',
        is_paid=True,
        is_checkable=False,
        created_at=datetime.now(UTC),
    )
    assert response.purchase_step_pending is False


def _paid_record(user) -> PendingPayment:
    return PendingPayment(
        method=PaymentMethod.PLATEGA,
        local_id=777,
        identifier='corr-777',
        amount_kopeks=24_900,
        status='PAID',
        is_paid=True,
        created_at=datetime.now(UTC),
        user=user,
        payment=SimpleNamespace(id=777),
    )


@pytest.mark.asyncio
async def test_paid_payment_carries_the_verdict_of_the_chat_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вердикт кабинета и вердикт чата — один и тот же вызов, а не два похожих правила."""
    from app.services.payment import common as payment_common

    asked: list[int] = []

    async def fake_hint(user):
        asked.append(user.id)
        return 'Деньги на балансе, но подписка сама не оплатится.'

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    response = await balance_route._with_purchase_step(balance_route._record_to_response(_paid_record(user)), user)

    assert asked == [106]
    assert response.purchase_step_pending is True


@pytest.mark.asyncio
async def test_silence_of_the_chat_hint_is_silence_in_the_cabinet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Когда те же деньги собирается потратить кто-то другой, чат молчит — молчит и экран."""
    from app.services.payment import common as payment_common

    async def fake_hint(_user):
        return None

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    response = await balance_route._with_purchase_step(balance_route._record_to_response(_paid_record(user)), user)

    assert response.purchase_step_pending is False


@pytest.mark.asyncio
async def test_unresolved_payment_does_not_ask_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пока исход неизвестен, экран показывает ожидание — спрашивать не о чем.

    Маршрут опрашивают раз в три секунды до десяти минут; лишний поход в базу и в Redis на
    каждом опросе не нужен никому.
    """
    from app.services.payment import common as payment_common

    async def fake_hint(_user):
        raise AssertionError('неоплаченный платёж не должен спрашивать про оставшийся шаг')

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    record = _paid_record(user)
    record.is_paid = False
    record.status = 'PENDING'

    response = await balance_route._with_purchase_step(balance_route._record_to_response(record), user)

    assert response.purchase_step_pending is False


@pytest.mark.asyncio
async def test_manual_check_carries_the_flag_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все три сборщика ответа несут поле, а не два из трёх.

    Нашла линза корректности. Маршрут ручной проверки платежа сегодня из кабинета никто не
    зовёт — но оставить один сборщик без поля значит завести мину себе же: тот, кто завтра
    выведет кнопку «Проверить статус» на экран результата, молча вернёт прежний текст.
    """
    from app.services.payment import common as payment_common

    async def fake_hint(_user):
        return 'Деньги на балансе, но подписка сама не оплатится.'

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    record = _paid_record(user)

    async def fake_get_payment_record(_db, _method, _payment_id):
        return record

    monkeypatch.setattr(balance_route, 'get_payment_record', fake_get_payment_record)
    # Ручная проверка для этого способа недоступна — берём самую раннюю из трёх веток ответа.
    monkeypatch.setattr(balance_route, '_is_checkable', lambda _record: False)

    response = await balance_route.check_payment_status(
        method='platega', payment_id=777, user=user, db=SimpleNamespace()
    )

    assert response.payment.purchase_step_pending is True


@pytest.mark.asyncio
async def test_the_route_itself_carries_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сторож на функцию не доказывает, что функция ПОДКЛЮЧЕНА.

    Поэтому здесь — настоящий маршрут, который зовёт экран результата.
    """
    from app.services.payment import common as payment_common

    async def fake_hint(_user):
        return 'Деньги на балансе, но подписка сама не оплатится.'

    monkeypatch.setattr(payment_common, 'topup_pending_purchase_hint', fake_hint)

    user = SimpleNamespace(id=106, telegram_id=1, username=None, language='ru')
    record = _paid_record(user)

    async def fake_get_payment_record(_db, _method, _payment_id):
        return record

    monkeypatch.setattr(balance_route, 'get_payment_record', fake_get_payment_record)

    response = await balance_route.get_pending_payment_details(
        method='platega', payment_id=777, user=user, db=SimpleNamespace()
    )

    assert response.purchase_step_pending is True

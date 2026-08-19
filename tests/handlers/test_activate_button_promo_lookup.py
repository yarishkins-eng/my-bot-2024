"""Кнопка «активировать» доводит покупку до конца, а не падает молча (мина AW, 19.08.2026).

`handle_activate_button` звала `get_user_active_promo_discount_percent`, не импортировав её.
Живой симптом был не «красный линтер», а вот какой: имя не разрешается → `NameError` →
его подхватывает широкий `except Exception` в конце обработчика → клиент видит
«❌ Ошибка активации. Попробуйте позже.» и не получает подписку, а в логах лежит одна строка.
🔴 Точность: имя стоит в ветке «подписки нет → создать новую», поэтому ломалась ТОЛЬКО первая
покупка. Ветка продления (у кого подписка уже есть) этого имени не касается и работала всегда. Сломал внесла централизация расчёта цен (`8d3cd500`), и ни линтер, ни набор
тестов её не заметили: `F821` был заглушён в pyproject.toml.

Тест ВЫЗЫВАЕТ обработчик, а не читает его исходник (грабли 19.08: сторож, ищущий подстроку,
не ловит ничего). Мутация «убрать импорт из `handle_activate_button`» роняет его.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers import menu


class _Pricing:
    """То, что PricingEngine отдаёт обработчику: цена целиком, в копейках."""

    def __init__(self, final_total: int) -> None:
        self.final_total = final_total


def _make_user(*, promo_percent: int = 0) -> MagicMock:
    """Пользователь с настоящими полями персональной скидки.

    Поля заполняются по-настоящему, а не заглушкой на саму функцию скидки: иначе тест
    проверял бы мою подмену, а не то, что обработчик добирается до живого кода
    (грабли 19.08: «сторож проверяет то, что ему подсунули»).
    """
    user = MagicMock()
    user.id = 4242
    user.telegram_id = 999_000_111
    user.language = 'ru'
    user.balance_kopeks = 500_00
    user.promo_group_id = 1
    user.promo_offer_discount_percent = promo_percent
    user.promo_offer_discount_expires_at = datetime.now(UTC) + timedelta(days=1) if promo_percent else None
    return user


@pytest.mark.anyio('asyncio')
async def test_activate_button_reaches_the_purchase_and_does_not_fail_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """У человека без подписки и с деньгами кнопка обязана списать баланс и выдать подписку."""
    user = _make_user()
    db = MagicMock()
    db.rollback = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()

    # Поле pydantic живёт на ЭКЗЕМПЛЯРЕ: подмена на классе молча не применяется
    # (проверено). Для методов ниже — наоборот, нужна именно классовая.
    monkeypatch.setattr(settings, 'DEFAULT_DEVICE_LIMIT', 3, raising=False)
    # Кнопка включена: эти тесты про РАБОТАЮЩИЙ путь. Заслонку стережёт отдельный тест ниже.
    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_VISIBLE', True, raising=False)
    # is_multi_tariff_enabled / get_available_subscription_periods — методы pydantic-модели Settings,
    # подменяются на классе, а не на объекте (иначе pydantic отвергает присваивание).
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda self: [30, 90], raising=False)

    # Подписки нет — обработчик уходит в ветку «создать новую», где и жила поломка.
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=None), raising=False
    )
    free_server = MagicMock()
    free_server.squad_uuid = 'squad-free'
    free_server.is_available = True
    free_server.price_kopeks = 0
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads',
        AsyncMock(return_value=[free_server]),
        raising=False,
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user), raising=False)
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_classic_new_subscription_price',
        AsyncMock(return_value=_Pricing(300_00)),
        raising=False,
    )

    subtract_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', subtract_balance, raising=False)

    new_subscription = MagicMock()
    new_subscription.end_date = datetime.now(UTC) + timedelta(days=30)
    monkeypatch.setattr(
        'app.database.crud.subscription.create_paid_subscription',
        AsyncMock(return_value=new_subscription),
        raising=False,
    )
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(), raising=False)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock(), raising=False
    )

    await menu.handle_activate_button(callback, user, db)

    answers = [call.args[0] for call in callback.answer.await_args_list if call.args]
    assert answers, 'обработчик не ответил клиенту вовсе'

    # Главное утверждение: путь дошёл до списания и выдачи, а не свалился в общий обработчик ошибок.
    assert 'Ошибка активации' not in answers[-1], (
        'Кнопка активации ответила общей ошибкой вместо покупки. Ровно так выглядит снаружи '
        'неразрешённое имя внутри обработчика: NameError глотает `except Exception`, '
        f'клиент видит заглушку и не получает ничего. Ответ: {answers[-1]!r}'
    )
    assert '✅' in answers[-1], f'ожидали подтверждение выдачи, получили {answers[-1]!r}'
    subtract_balance.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.anyio('asyncio')
async def test_activate_button_consumes_the_promo_offer_it_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Найденная персональная скидка обязана дойти до списания как `consume_promo_offer=True`.

    Первый тест доказывает, что имя разрешается. Этот — что его РЕЗУЛЬТАТ используется:
    без него можно было бы «починить» поломку любой заглушкой вроде `lambda _u: 0`
    и остаться зелёным (грабли 18.08: тесты на функции не доказывают, что она подключена).
    """
    user = _make_user(promo_percent=15)  # человеку начислено персональное предложение 15%
    db = MagicMock()
    db.rollback = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()

    # Поле pydantic живёт на ЭКЗЕМПЛЯРЕ: подмена на классе молча не применяется
    # (проверено). Для методов ниже — наоборот, нужна именно классовая.
    monkeypatch.setattr(settings, 'DEFAULT_DEVICE_LIMIT', 3, raising=False)
    # Кнопка включена: эти тесты про РАБОТАЮЩИЙ путь. Заслонку стережёт отдельный тест ниже.
    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_VISIBLE', True, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda self: [30], raising=False)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=None), raising=False
    )
    free_server = MagicMock()
    free_server.squad_uuid = 'squad-free'
    free_server.is_available = True
    free_server.price_kopeks = 0
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads',
        AsyncMock(return_value=[free_server]),
        raising=False,
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user), raising=False)
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_classic_new_subscription_price',
        AsyncMock(return_value=_Pricing(100_00)),
        raising=False,
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=MagicMock()), raising=False
    )
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(), raising=False)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock(), raising=False
    )

    subtract_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', subtract_balance, raising=False)

    await menu.handle_activate_button(callback, user, db)

    subtract_balance.assert_awaited_once()
    assert subtract_balance.await_args.kwargs.get('consume_promo_offer') is True, (
        'Скидка найдена, но до списания не доехала: подписка спишется по полной цене, '
        'а начисленное предложение останется висеть неиспользованным.'
    )


@pytest.mark.anyio('asyncio')
async def test_activate_button_does_not_burn_a_promo_offer_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """У человека БЕЗ предложения гашение не запрашивается.

    Парный к предыдущему, и без него сторож дырявый. Скептик доказал мутацией: замена
    `> 0` на `>= 0` — один символ — заставляет гасить скидку у КАЖДОГО, у кого её нет,
    и пять прежних тестов вместе с линтером оставались зелёными. Тест на человека СО
    скидкой такую подмену пропускает by design: у него ответ и так True.
    """
    user = _make_user(promo_percent=0)  # предложения нет
    db = MagicMock()
    db.rollback = AsyncMock()
    callback = MagicMock()
    callback.answer = AsyncMock()

    monkeypatch.setattr(settings, 'DEFAULT_DEVICE_LIMIT', 3, raising=False)
    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_VISIBLE', True, raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False, raising=False)
    monkeypatch.setattr(type(settings), 'get_available_subscription_periods', lambda self: [30], raising=False)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=None), raising=False
    )
    free_server = MagicMock()
    free_server.squad_uuid = 'squad-free'
    free_server.is_available = True
    free_server.price_kopeks = 0
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads',
        AsyncMock(return_value=[free_server]),
        raising=False,
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user), raising=False)
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_classic_new_subscription_price',
        AsyncMock(return_value=_Pricing(100_00)),
        raising=False,
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=MagicMock()), raising=False
    )
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(), raising=False)
    monkeypatch.setattr(
        'app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock(), raising=False
    )

    subtract_balance = AsyncMock(return_value=True)
    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', subtract_balance, raising=False)

    await menu.handle_activate_button(callback, user, db)

    subtract_balance.assert_awaited_once()
    assert subtract_balance.await_args.kwargs.get('consume_promo_offer') is False, (
        'Гашение запрошено у человека без предложения — значит значение зашито или условие '
        'ослаблено, а не взято из настоящей проверки. Так одноразовая скидка сгорает у тех, '
        'кому её не давали, и человек теряет её, ничего не получив взамен.'
    )


@pytest.mark.anyio('asyncio')
async def test_activate_button_is_fail_closed_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Выключенная кнопка не двигает деньги, даже если нажатие всё-таки долетело.

    Мина BA. Флаг `ACTIVATE_BUTTON_VISIBLE` управлял только ОТРИСОВКОЙ, а обработчик
    зарегистрирован безусловно (`menu.py`, `register_handlers`) — значит callback долетает
    сюда из админской рассылки с произвольной кнопкой (`admin/messages.py` штатно принимает
    любое действие) и из любого старого сообщения в чате. Тот же вывод один раз уже сделали
    на мине AA: заслонка обязана стоять в обработчике, а не в отрисовке.

    Проверяем не «кнопка не нарисовалась», а «нажатие ничего не сделало»: ни одного похода
    в базу, ни одного списания.
    """
    user = _make_user()
    db = MagicMock()
    callback = MagicMock()
    callback.answer = AsyncMock()

    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_VISIBLE', False, raising=False)

    # Любое обращение к деньгам или к базе — провал теста, а не «неважная деталь».
    def _forbidden(name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f'Заслонка пропустила выполнение дальше: вызван {name}')

        return _boom

    monkeypatch.setattr('app.database.crud.user.subtract_user_balance', _forbidden('subtract_user_balance'))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', _forbidden('lock_user_for_pricing'))
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', _forbidden('get_subscription_by_user_id')
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.create_paid_subscription', _forbidden('create_paid_subscription')
    )
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_available_server_squads', _forbidden('get_available_server_squads')
    )

    await menu.handle_activate_button(callback, user, db)

    callback.answer.assert_awaited_once()
    answer = callback.answer.await_args.args[0]
    assert 'отключена' in answer, f'человеку не сказали, что кнопка отключена: {answer!r}'
    # db.execute/commit — MagicMock, так что факт обращения виден по счётчику вызовов.
    assert not db.method_calls, f'заслонка всё-таки сходила в базу: {db.method_calls}'

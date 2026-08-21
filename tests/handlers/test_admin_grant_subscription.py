"""Этап З-1: две админские кнопки выдачи после снятия запоров 8 августа.

Коммит `a27e681f` вписал в `_grant_trial_subscription` и `_grant_paid_subscription`
журнал + `return False`. Запоры сняты — но снимать их в одиночку было нельзя:
код под ними выдавал подписку с НУЛЁМ серверов (мина A) либо со ВСЕМИ странами
мимо тарифа.

Сторожат здесь не тексты и не наличие слов в исходнике, а свойства, каждое из
которых уже стоило проекту испорченных подписок или лживых экранов:

1. в базу уходит `tariff_id` — без него сторож прав не включается вовсе;
2. серверы НЕ выбираются вызывающей стороной (`connected_squads` не подставляется
   руками), решение остаётся за `resolve_tariff_entitlement`;
3. успех НЕ объявляется, пока не спрошена панель — `create_remnawave_user`
   не бросает, а возвращает `None`;
4. подарок не включает автосписание с получателя;
5. отказ доходит до админа словами и не зовёт в мёртвую кнопку;
6. имя тарифа не может сломать сообщение.

Каждый сторож проверялся мутацией: правка ломается — тест краснеет.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.admin import users as admin_users


class _Tariff:
    """Дубль тарифа.

    🔴 `is_free` в проекте — ВЫЧИСЛЯЕМОЕ свойство (`models.py`), а не колонка:
    пустой `period_prices` даёт `False`, то есть «тариф без цен» считается
    платным. Дубль повторяет эту логику, иначе сторож проверял бы не то, что
    работает на боевом.
    """

    def __init__(
        self,
        tariff_id,
        name,
        *,
        period_prices=None,
        is_daily=False,
        daily_price_kopeks=0,
        show_in_gift=True,
        is_trial_available=False,
        entitlement_mode='native_squads',
    ):
        self.id = tariff_id
        self.name = name
        self.period_prices = {'30': 30000} if period_prices is None else period_prices
        self.is_daily = is_daily
        self.daily_price_kopeks = daily_price_kopeks
        self.show_in_gift = show_in_gift
        self.is_trial_available = is_trial_available
        self.entitlement_mode = entitlement_mode
        self.traffic_limit_gb = 100
        self.device_limit = 3

    @property
    def is_free(self) -> bool:
        if self.is_daily:
            return (self.daily_price_kopeks or 0) <= 0
        prices = list((self.period_prices or {}).values())
        if not prices:
            return False
        return all((price or 0) <= 0 for price in prices)


def _subscription(squads):
    return SimpleNamespace(id=77, connected_squads=list(squads), autopay_enabled=True)


def _panel_user():
    return SimpleNamespace(uuid='panel-uuid')


@pytest.mark.asyncio
async def test_trial_grant_passes_a_tariff_into_the_database_call():
    """🔴 Главный сторож триала: `tariff_id` доезжает до `create_trial_subscription`.

    Без него функция оставляет `connected_squads` пустым и создаёт подписку, у
    которой VPN не работает, а ошибки нет.
    """
    parameters = {
        'duration_days': 3,
        'traffic_limit_gb': 10,
        'device_limit': 2,
        'connected_squads': ['squad-de', 'squad-nl', 'squad-pl'],
        'tariff_id': 5,
    }
    created = AsyncMock(return_value=_subscription(parameters['connected_squads']))

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch('app.services.trial_activation_service.get_trial_offer_parameters', AsyncMock(return_value=parameters)),
        patch('app.database.crud.subscription.create_trial_subscription', created),
        patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user',
            AsyncMock(return_value=_panel_user()),
        ),
    ):
        success, detail = await admin_users._grant_trial_subscription(AsyncMock(), 196, 1)

    assert success is True
    kwargs = created.await_args.kwargs
    assert kwargs['tariff_id'] == 5, 'триал ушёл в базу без тарифа — это подписка без серверов'
    assert kwargs['connected_squads'] == ['squad-de', 'squad-nl', 'squad-pl']
    assert '3' in detail, 'админ должен видеть, сколько серверов реально подключено'


@pytest.mark.asyncio
async def test_success_is_not_claimed_until_the_panel_answered():
    """🔴 P0: `create_remnawave_user` НЕ бросает — он возвращает `None`.

    Подписка к этому моменту уже закоммичена, поэтому отчитаться отказом нельзя
    (админ нажмёт второй раз и получит «уже есть подписка»). Но и говорить
    «серверов: 3» как будто панель подтвердила — та самая мина A.
    """
    created = AsyncMock(return_value=_subscription(['squad-de', 'squad-nl', 'squad-pl']))
    parameters = {'connected_squads': ['squad-de', 'squad-nl', 'squad-pl'], 'tariff_id': 5}

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch('app.services.trial_activation_service.get_trial_offer_parameters', AsyncMock(return_value=parameters)),
        patch('app.database.crud.subscription.create_trial_subscription', created),
        patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user',
            AsyncMock(return_value=None),
        ),
    ):
        success, detail = await admin_users._grant_trial_subscription(AsyncMock(), 196, 1)

    assert success is True, 'подписка создана — отказ заставил бы админа нажать второй раз'
    assert 'НЕ ушла' in detail and 'панель' in detail, 'молчание панели обязано быть названо'
    assert 'заработает' in detail, 'админ должен понять последствие, а не только факт'


@pytest.mark.asyncio
async def test_trial_grant_refuses_out_loud_when_the_trial_tariff_is_broken():
    """Тариф триала не резолвится — отказ, а НЕ подписка без серверов."""
    from app.services.trial_activation_service import TrialCheckoutResolutionError

    broken = AsyncMock(side_effect=TrialCheckoutResolutionError('trial_tariff_not_configured', 'no policy'))
    created = AsyncMock()

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch('app.services.trial_activation_service.get_trial_offer_parameters', broken),
        patch('app.database.crud.subscription.create_trial_subscription', created),
    ):
        success, detail = await admin_users._grant_trial_subscription(AsyncMock(), 196, 1)

    assert success is False
    created.assert_not_awaited()
    assert detail and 'пробного периода' in detail


@pytest.mark.asyncio
async def test_paid_grant_never_picks_servers_itself():
    """🔴 Главный сторож платной выдачи: серверы выбирает сторож прав, а не мы.

    Прежний код звал `get_effective_tariff_squad_uuids(db, None)`, а тот при
    пустом тарифе отдаёт ВСЕ доступные сквады.
    """
    created = AsyncMock(return_value=_subscription(['squad-de', 'squad-nl']))

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch.object(admin_users, '_resolve_grantable_tariff', AsyncMock(return_value=_Tariff(3, 'Базовый'))),
        patch('app.database.crud.subscription.create_paid_subscription', created),
        patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user',
            AsyncMock(return_value=_panel_user()),
        ),
    ):
        success, detail = await admin_users._grant_paid_subscription(AsyncMock(), 196, 90, 1)

    assert success is True
    kwargs = created.await_args.kwargs
    assert kwargs['tariff_id'] == 3, 'без тарифа сторож прав не включается вовсе'
    assert kwargs['connected_squads'] is None, 'серверы подставлены руками — это обход сторожа прав'
    assert kwargs['traffic_limit_gb'] == 100, 'трафик обязан приходить из тарифа'
    assert kwargs['device_limit'] == 3, 'устройства обязаны приходить из тарифа'
    assert kwargs['duration_days'] == 90
    assert 'Базовый' in detail


@pytest.mark.asyncio
async def test_a_gift_never_arms_autopay_against_its_recipient():
    """🔴 Подарок не должен списывать деньги с того, кому его подарили.

    Регулярный автоплатёж (`monitoring_service`) отбирает по
    `autopay_enabled AND NOT is_trial` и про «человек когда-либо платил» не
    спрашивает — за три дня до конца подарка он бы списал продление.
    """
    subscription = _subscription(['squad-de'])
    assert subscription.autopay_enabled is True, 'дубль обязан начинать с включённого — иначе тест пустой'

    db = AsyncMock()

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch.object(admin_users, '_resolve_grantable_tariff', AsyncMock(return_value=_Tariff(3, 'Базовый'))),
        patch('app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=subscription)),
        patch(
            'app.services.subscription_service.SubscriptionService.create_remnawave_user',
            AsyncMock(return_value=_panel_user()),
        ),
    ):
        success, _ = await admin_users._grant_paid_subscription(db, 196, 90, 1)

    assert success is True
    assert subscription.autopay_enabled is False, 'подарок взвёл автосписание с получателя'
    # Гашение обязано быть ЗАКОММИЧЕНО: строка подписки уже создана отдельным коммитом,
    # а ниже идёт синхронизация с панелью, внутри которой есть ветка с откатом.
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_paid_grant_refuses_when_the_tariff_gives_no_servers():
    """Сторож `create_paid_subscription` сработал — админ узнаёт об этом словами."""
    created = AsyncMock(side_effect=ValueError('tariff entitlement resolution produced no squads'))

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch.object(admin_users, '_resolve_grantable_tariff', AsyncMock(return_value=_Tariff(3, 'Базовый'))),
        patch('app.database.crud.subscription.create_paid_subscription', created),
        patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()) as panel,
    ):
        success, detail = await admin_users._grant_paid_subscription(AsyncMock(), 196, 90, 1)

    assert success is False
    assert detail and 'squads' in detail
    panel.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_a_giftable_tariff_qualifies():
    """Отбор кандидатов в подарок — по готовому полю проекта, а не по своим критериям.

    Точные числа вместо «больше нуля»: подарить не тот тариф — это не тот срок,
    не тот трафик и не те страны у живого клиента.
    """
    zoo = [
        _Tariff(3, 'Базовый'),
        _Tariff(4, 'Team', period_prices={'365': 0}),
        _Tariff(5, '⏰Пробный', is_trial_available=True),
        _Tariff(6, 'Суточный', is_daily=True, daily_price_kopeks=1500),
        _Tariff(7, 'Спрятанный', show_in_gift=False),
        _Tariff(9, 'Точка', entitlement_mode='access_point_managed'),
    ]

    with patch.object(admin_users, 'get_all_tariffs', AsyncMock(return_value=zoo)) as listing:
        only = await admin_users._resolve_grantable_tariff(AsyncMock())

    assert only.id == 3
    assert listing.await_args.kwargs['include_inactive'] is False, 'выключенный тариф дарить нельзя'


@pytest.mark.asyncio
async def test_no_giftable_tariff_is_a_named_refusal():
    with patch.object(admin_users, 'get_all_tariffs', AsyncMock(return_value=[_Tariff(7, 'X', show_in_gift=False)])):
        with pytest.raises(ValueError, match='показывать в подарках'):
            await admin_users._resolve_grantable_tariff(AsyncMock())


@pytest.mark.asyncio
async def test_an_ambiguous_choice_never_points_at_a_dead_button():
    """🔴 P0 обеих линз ревью: подсказка не должна звать в кнопку, которой нет.

    «💳 Купить тариф» лежит в ДРУГОЙ ветке экрана (её нет там, где читается этот
    отказ), заперта тем же запором 8 августа и списывает деньги с баланса
    клиента, то есть подарком не является.
    """
    two = [_Tariff(3, 'Базовый'), _Tariff(7, 'Премиум')]

    with patch.object(admin_users, 'get_all_tariffs', AsyncMock(return_value=two)):
        with pytest.raises(ValueError) as refusal:
            await admin_users._resolve_grantable_tariff(AsyncMock())

    text = str(refusal.value)
    assert 'Базовый' in text and 'Премиум' in text, 'админ должен видеть, между чем выбирать'
    assert 'Купить тариф' not in text, 'отказ зовёт в мёртвую кнопку из другой ветки экрана'
    assert 'подарках' in text, 'отказ обязан назвать действие, которое РАБОТАЕТ'


def test_the_tariff_name_cannot_break_the_message_to_the_admin():
    """Мина Д3 компаса: имя тарифа правит владелец, а сообщение уходит с HTML.

    Тариф со знаком «<» в названии сделал бы сообщение невалидным — Телеграм
    ответил бы 400, `@error_handler` подменил бы его общим «Произошла ошибка»,
    и админ не узнал бы ни результата, ни причины.
    """
    text = admin_users._grant_outcome_text('✅ Выдано', 'тариф «A<b>x</b>», серверов: 3')

    assert '<b>' not in text, 'имя тарифа не экранировано — сообщение можно сломать из админки'
    assert '&lt;b&gt;' in text
    assert text.startswith('✅ Выдано')
    assert admin_users._grant_outcome_text('❌ Отказ', None) == '❌ Отказ'


class _State:
    """Дубль FSM: помнит, что состояние снимали, и по какому имени его спрашивали."""

    def __init__(self, current):
        self.current = current
        self.cleared = False

    async def get_state(self):
        return self.current

    async def clear(self):
        self.cleared = True
        self.current = None

    async def get_data(self):
        return {'granting_user_id': 196}

    async def update_data(self, **kwargs):
        return None

    async def set_state(self, value):
        self.current = value


@pytest.mark.asyncio
async def test_the_grant_state_never_outlives_its_screen():
    """🔴 P0: пока висит состояние «жду число дней», ЛЮБОЕ число дарит подписку.

    Заглушка 8 августа гасила это побочно — снимая её, предохранитель надо было
    заменить. Выходов из состояния три, и до правки его не снимал ни один,
    включая УСПЕШНУЮ выдачу кнопкой срока.
    """
    from app.states import AdminStates

    live = AdminStates.granting_subscription.state

    for name, current, must_clear in (
        ('состояние наше — снять', live, True),
        ('состояния нет — не трогать', None, False),
        ('состояние чужое — не трогать', 'SomeOther:state', False),
    ):
        state = _State(current)
        await admin_users._clear_granting_state(state)
        assert state.cleared is must_clear, name


@pytest.mark.asyncio
async def test_choosing_a_period_by_button_also_leaves_the_state():
    """Успешная выдача кнопкой обязана закрывать состояние, а не только отказ."""
    from app.states import AdminStates

    state = _State(AdminStates.granting_subscription.state)
    callback = SimpleNamespace(
        data='admin_sub_grant_days_196_90',
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    handler = admin_users.process_subscription_grant_days.__wrapped__.__wrapped__

    with (
        patch.object(admin_users, '_grant_paid_subscription', AsyncMock(return_value=(False, 'нет тарифа'))),
        patch.object(admin_users, 'get_user_by_id', AsyncMock(return_value=None)),
    ):
        await handler(callback, SimpleNamespace(id=1), AsyncMock(), state)

    assert state.cleared is True, 'состояние пережило выбор срока — следующее число выдаст ещё подписку'


@pytest.mark.asyncio
async def test_cancel_command_does_what_the_screen_promises():
    """Экран обещает `/cancel`. До правки команда уходила в int() и состояние не снимала."""
    from app.states import AdminStates

    state = _State(AdminStates.granting_subscription.state)
    message = SimpleNamespace(text='/cancel', answer=AsyncMock())
    handler = admin_users.process_subscription_grant_text.__wrapped__.__wrapped__

    with patch.object(admin_users, '_grant_paid_subscription', AsyncMock()) as grant:
        await handler(message, SimpleNamespace(id=1), state, AsyncMock())

    assert state.cleared is True
    grant.assert_not_awaited()
    assert 'отменена' in message.answer.await_args[0][0]

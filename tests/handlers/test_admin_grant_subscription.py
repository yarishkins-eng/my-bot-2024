"""Этап З-1: две админские кнопки выдачи после снятия запоров 8 августа.

Коммит `a27e681f` вписал в `_grant_trial_subscription` и `_grant_paid_subscription`
журнал + `return False`. Запоры сняты — но снимать их в одиночку было нельзя:
код под ними выдавал подписку с НУЛЁМ серверов (мина A) либо со ВСЕМИ странами
мимо тарифа.

Сторожат здесь не тексты и не наличие слов в исходнике, а три свойства, каждое
из которых уже стоило проекту испорченных подписок:

1. в базу уходит `tariff_id` — без него сторож прав не включается вовсе;
2. серверы НЕ выбираются вызывающей стороной (`connected_squads` не подставляется
   руками), решение остаётся за `resolve_tariff_entitlement`;
3. отказ доходит до админа словами, а не немым «❌ Ошибка».

Каждый сторож проверялся мутацией: правка ломается — тест краснеет.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.admin import users as admin_users


class _Tariff:
    def __init__(self, tariff_id, name, *, is_free=False, entitlement_mode='native_squads'):
        self.id = tariff_id
        self.name = name
        self.is_free = is_free
        self.entitlement_mode = entitlement_mode
        self.traffic_limit_gb = 100
        self.device_limit = 3


def _subscription(squads):
    return SimpleNamespace(id=77, connected_squads=list(squads))


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
        patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()),
    ):
        success, detail = await admin_users._grant_trial_subscription(AsyncMock(), 196, 1)

    assert success is True
    kwargs = created.await_args.kwargs
    assert kwargs['tariff_id'] == 5, 'триал ушёл в базу без тарифа — это подписка без серверов'
    assert kwargs['connected_squads'] == ['squad-de', 'squad-nl', 'squad-pl']
    assert '3' in detail, 'админ должен видеть, сколько серверов реально подключено'


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
    created.assert_not_awaited(), 'до записи в базу дело дойти не должно'
    assert detail and 'пробного периода' in detail


@pytest.mark.asyncio
async def test_paid_grant_never_picks_servers_itself():
    """🔴 Главный сторож платной выдачи: серверы выбирает сторож прав, а не мы.

    Прежний код звал `get_effective_tariff_squad_uuids(db, None)`, а тот при
    пустом тарифе отдаёт ВСЕ доступные сквады. Здесь проверяется ровно это:
    `connected_squads` уходит пустым, а тариф — заполненным.
    """
    created = AsyncMock(return_value=_subscription(['squad-de', 'squad-nl']))

    with (
        patch.object(admin_users, '_resolve_admin_subscription', AsyncMock(return_value=None)),
        patch.object(admin_users, '_resolve_grantable_tariff', AsyncMock(return_value=_Tariff(3, 'Базовый'))),
        patch('app.database.crud.subscription.create_paid_subscription', created),
        patch('app.services.subscription_service.SubscriptionService.create_remnawave_user', AsyncMock()),
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
    panel.assert_not_awaited(), 'в панель ходить нечем — подписки не создано'


@pytest.mark.asyncio
async def test_grantable_tariff_is_exactly_one_or_a_loud_refusal():
    """Неоднозначность — отказ с перечислением, а не молчаливая догадка.

    Точные числа вместо «больше нуля»: подарить не тот тариф — это не тот срок,
    не тот трафик и не те страны у живого клиента.
    """
    everything = [
        _Tariff(3, 'Базовый'),
        _Tariff(4, 'Team', is_free=True),
        _Tariff(9, 'Точка', entitlement_mode='access_point_managed'),
    ]

    with patch('app.database.crud.tariff.get_all_tariffs', AsyncMock(return_value=everything)):
        only = await admin_users._resolve_grantable_tariff(AsyncMock())
    assert only.id == 3, 'бесплатный и access-point тарифы дарить этим путём нельзя'

    with patch('app.database.crud.tariff.get_all_tariffs', AsyncMock(return_value=[_Tariff(4, 'Team', is_free=True)])):
        with pytest.raises(ValueError, match='выдавать нечего'):
            await admin_users._resolve_grantable_tariff(AsyncMock())

    two = [_Tariff(3, 'Базовый'), _Tariff(7, 'Премиум')]
    with patch('app.database.crud.tariff.get_all_tariffs', AsyncMock(return_value=two)):
        with pytest.raises(ValueError, match='больше одного тарифа') as refusal:
            await admin_users._resolve_grantable_tariff(AsyncMock())
    assert 'Базовый' in str(refusal.value) and 'Премиум' in str(refusal.value)


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

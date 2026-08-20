"""Пункт 2.2б: кабинетная выдача подписки не имеет права создать её без тарифа.

Это единственный живой путь выдать подписку руками — обе кнопки выдачи в
чат-админке заглушены (`app/handlers/admin/users.py`, `return False`). Без тарифа
права на серверы не резолвит никто, и получается подписка с нулём серверов:
VPN не работает, ошибки нет, кабинет молчит (мина A).

Сторожа здесь три и они разные:
1. маршрут отбивает запрос без тарифа и НЕ доходит до создания;
2. то же для триала — он идёт этим же маршрутом и отравляется так же;
3. `create_paid_subscription` физически не создаёт строку, если прав ноль.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.database.crud import subscription as subscription_crud


def _healthy_user() -> SimpleNamespace:
    """Обычный живой пользователь без подписок и без финансового закрытия."""
    return SimpleNamespace(
        id=78,
        status='active',
        account_erasure_requested_at=None,
        subscriptions=[],
    )


def _single_tariff_mode(monkeypatch) -> None:
    """Мультитариф выключен явно, а не «по умолчанию в репозитории нет .env».

    Без этого тесты зеленели случайно: при включённом мультитарифе ветка уходит
    в get_subscription_by_user_and_tariff и падает на подставной сессии.
    Подмена МЕТОДА pydantic-настроек делается на классе (мина AW).
    """
    monkeypatch.setattr(type(admin_users.settings), 'is_multi_tariff_enabled', lambda self: False)


@pytest.mark.asyncio
async def test_create_without_tariff_is_rejected_and_never_reaches_creation(monkeypatch) -> None:
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_healthy_user()))
    creator = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', creator)

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_subscription(
            user_id=78,
            request=UpdateSubscriptionRequest(action='create', days=30),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 400
    assert 'tariff_id' in error.value.detail
    # Главное: до создания подписки дело не дошло вовсе.
    creator.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_without_tariff_is_rejected_too(monkeypatch) -> None:
    """Триал идёт этим же маршрутом через create_paid_subscription.

    Свой источник прав (`squad_uuid` в `create_trial_subscription`) здесь не
    подключён, поэтому сужать забор до «только платные» было бы неверно:
    без тарифа отравляется и триал.
    """
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_healthy_user()))
    creator = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', creator)

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_subscription(
            user_id=78,
            request=UpdateSubscriptionRequest(action='create', days=3, is_trial=True),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 400
    creator.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_tariff_id_is_rejected_too(monkeypatch) -> None:
    """`tariff_id=0` — не «тариф передан».

    У поля нет `ge=1`, а вся ветка ниже читает `tariff_id` через истинность.
    Проверка через `is None` пропустила бы ноль до самого низа и дала бы 500.
    """
    _single_tariff_mode(monkeypatch)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_healthy_user()))
    creator = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', creator)

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_subscription(
            user_id=78,
            request=UpdateSubscriptionRequest(action='create', days=30, tariff_id=0),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 400
    creator.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_tariff_is_refused_with_a_reason_not_a_crash(monkeypatch) -> None:
    """Тариф, которого нет в базе, — внятный отказ, а не голый 500.

    Раньше такой запрос доезжал до `create_paid_subscription`, та бросала
    `ValueError`, и владелец получал 500 без причины.
    """
    _single_tariff_mode(monkeypatch)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_healthy_user()))
    monkeypatch.setattr(admin_users, 'get_tariff_by_id', AsyncMock(return_value=None))
    creator = AsyncMock()
    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', creator)

    with pytest.raises(HTTPException) as error:
        await admin_users.update_user_subscription(
            user_id=78,
            request=UpdateSubscriptionRequest(action='create', days=30, tariff_id=999999),
            admin=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 404
    creator.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_with_tariff_forwards_tariff_id_to_creation(monkeypatch) -> None:
    """Забор не должен перекрывать нормальную выдачу — и обязан донести тариф.

    Именно переданный `tariff_id` включает в `create_paid_subscription` рельс
    «прав ноль → строку не создаём», который проверяет тест ниже.
    """
    _single_tariff_mode(monkeypatch)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=_healthy_user()))
    monkeypatch.setattr(
        admin_users,
        'get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(entitlement_mode='native_squads', traffic_limit_gb=50, device_limit=2)),
    )
    created = SimpleNamespace(id=901)
    creator = AsyncMock(return_value=created)
    monkeypatch.setattr(subscription_crud, 'create_paid_subscription', creator)
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock())
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    monkeypatch.setattr('app.utils.funnel_notify.notify_subscriber_menu', AsyncMock())

    response = await admin_users.update_user_subscription(
        user_id=78,
        request=UpdateSubscriptionRequest(action='create', days=30, tariff_id=3),
        admin=SimpleNamespace(id=1),
        db=SimpleNamespace(),
    )

    assert response.success is True
    creator.assert_awaited_once()
    assert creator.await_args.kwargs['tariff_id'] == 3


@pytest.mark.asyncio
async def test_zero_entitlement_never_produces_a_subscription_row(monkeypatch) -> None:
    """Рельс самой выдачи на пути ВСТАВКИ: прав ноль — новой строки нет.

    Проверяется настоящая `create_paid_subscription`, а не её копия: вместе с
    забором маршрута выше это и означает «новую подписку с нулём серверов этим
    путём создать нельзя».

    🔴 Границу утверждения держим честно: доказан путь вставки. Ветка оживления
    истёкшей подписки (`_revive_paid_subscription`, только при включённом
    мультитарифе) такого рельса не имеет — это отдельная мина, не этот пункт.
    """
    monkeypatch.setattr(type(subscription_crud.settings), 'is_multi_tariff_enabled', lambda self: False)

    db = MagicMock()
    db.scalar = MagicMock(return_value=None)  # синхронный дубль → забор закрытия инертен
    db.get = AsyncMock(return_value=SimpleNamespace(id=3, entitlement_mode='native_squads'))
    db.add = MagicMock()

    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=[], provenance='native_squads')),
    )

    with pytest.raises(ValueError, match='produced no squads'):
        await subscription_crud.create_paid_subscription(
            db=db,
            user_id=78,
            duration_days=30,
            tariff_id=3,
            connected_squads=None,
        )

    db.add.assert_not_called()

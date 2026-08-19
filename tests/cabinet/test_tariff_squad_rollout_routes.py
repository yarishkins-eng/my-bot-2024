"""Сторожа маршрута раскатки (пункт 3.2 плана, мина AE).

Мина AE: `PUT /tariffs/{id}/locations` (admin_public_locations) присваивает
entitlement_mode МИМО забора живых заказов. Сегодня это безопасно только потому,
что у маршрута нет фронта. Если раскатка позовёт его — забор перестанет
существовать ровно в переезд, ради которого его и ставили.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_tariffs
from app.cabinet.schemas.tariffs import SquadRolloutRequest
from app.services.subscription_service import PropagateSquadsResult


ROLLOUT_ROUTE_NAMES = ('preview_squad_rollout', 'run_squad_rollout', 'restore_squad_rollout')


def _rollout_source() -> str:
    return (
        '\n'.join(inspect.getsource(getattr(admin_tariffs, name)) for name in ROLLOUT_ROUTE_NAMES)
        + '\n'
        + inspect.getsource(admin_tariffs._load_rollout_tariff)
    )


def test_rollout_routes_exist_and_are_wired() -> None:
    paths = {route.path for route in admin_tariffs.router.routes}
    assert '/admin/tariffs/{tariff_id}/squad-rollout' in paths
    assert '/admin/tariffs/{tariff_id}/squad-rollout/preview' in paths
    assert '/admin/tariffs/{tariff_id}/squad-rollout/restore' in paths


def test_rollout_never_calls_the_fenceless_locations_route() -> None:
    """Мина AE: приёмочная проверка пункта 3.2 — этого вызова быть не должно."""

    source = _rollout_source()
    assert 'admin_public_locations' not in source
    assert 'locations/prepare-plan' not in source
    assert 'TariffLocationEntitlement' not in source
    # entitlement_mode раскатка не присваивает вовсе — это дело update_tariff
    assert 'entitlement_mode =' not in source


# 🔴 Сторожа ниже ВЫЗЫВАЮТ маршрут, а не читают его исходник. Прежняя версия
# проверяла текст через inspect.getsource и была зелёной, когда три имени в модуле
# не были импортированы вовсе: маршрут падал NameError на первом же запросе.
# Ruff это не ловит — F821 стоит в ignore проекта (pyproject.toml), поэтому и CI
# пропустил бы. Проверять поведение, а не наличие строк.


def _rollout_tariff():
    return SimpleNamespace(id=3, entitlement_mode='native_squads', allowed_squads=['squad-a'])


@pytest.mark.asyncio
@pytest.mark.parametrize('route_name', ['run_squad_rollout', 'restore_squad_rollout'])
async def test_rollout_route_runs_and_refuses_on_live_checkout(monkeypatch, route_name) -> None:
    """Забор живых заказов обязан СРАБОТАТЬ, а не просто упоминаться в коде."""

    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=_rollout_tariff()))
    fence = AsyncMock(side_effect=ValueError('this tariff has a live checkout'))
    monkeypatch.setattr(admin_tariffs, 'assert_tariff_squad_rollout_allowed', fence)

    route = getattr(admin_tariffs, route_name)
    kwargs = {'admin': SimpleNamespace(id=1), 'db': AsyncMock()}
    if route_name == 'run_squad_rollout':
        kwargs['request'] = SquadRolloutRequest()

    with pytest.raises(HTTPException) as exc_info:
        await route(3, **kwargs)

    assert exc_info.value.status_code == 409
    fence.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollout_route_reaches_the_service_and_writes_audit(monkeypatch) -> None:
    """Успешный путь целиком: маршрут доходит до сервиса и до аудита."""

    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=_rollout_tariff()))
    monkeypatch.setattr(admin_tariffs, 'assert_tariff_squad_rollout_allowed', AsyncMock())
    result = PropagateSquadsResult(total=3, synced=3, rollout_id='r-1', batches_done=1)
    service = SimpleNamespace(propagate_tariff_squads=AsyncMock(return_value=result))
    monkeypatch.setattr(admin_tariffs, 'SubscriptionService', lambda: service)
    audit = SimpleNamespace(create=AsyncMock())
    monkeypatch.setattr(admin_tariffs, 'AuditLogCRUD', audit)

    response = await admin_tariffs.run_squad_rollout(
        3, SquadRolloutRequest(), admin=SimpleNamespace(id=1), db=AsyncMock()
    )

    assert response.rollout_id == 'r-1'
    assert response.synced == 3
    service.propagate_tariff_squads.assert_awaited_once()
    audit.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_preview_route_runs_and_never_touches_the_panel(monkeypatch) -> None:
    """Сухой прогон обязан исполняться и оставаться безопасным."""

    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=_rollout_tariff()))
    plan = {
        'tariff_id': 3,
        'squads_to_set': ['squad-a'],
        'candidates': 39,
        'would_change': 33,
        'would_change_ids': [],
        'skipped_traffic_risk_ids': [],
    }
    propagate = AsyncMock()
    service = SimpleNamespace(plan_tariff_squad_rollout=AsyncMock(return_value=plan), propagate_tariff_squads=propagate)
    monkeypatch.setattr(admin_tariffs, 'SubscriptionService', lambda: service)

    response = await admin_tariffs.preview_squad_rollout(3, admin=SimpleNamespace(id=1), db=AsyncMock())

    assert response.would_change == 33
    propagate.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_tariff_is_refused_before_any_work(monkeypatch) -> None:
    """Наследованный режим отклоняется до забора и до сервиса."""

    legacy = SimpleNamespace(id=4, entitlement_mode='legacy_snapshot', allowed_squads=['old'])
    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=legacy))
    fence = AsyncMock()
    monkeypatch.setattr(admin_tariffs, 'assert_tariff_squad_rollout_allowed', fence)

    with pytest.raises(HTTPException) as exc_info:
        await admin_tariffs.run_squad_rollout(4, SquadRolloutRequest(), admin=SimpleNamespace(id=1), db=AsyncMock())

    assert exc_info.value.status_code == 409
    fence.assert_not_awaited()


def test_public_fence_name_delegates_to_the_canonical_one() -> None:
    """Публичное имя обязано звать канонический забор, а не спрашивать своё."""

    from app.database.crud import tariff as tariff_crud

    source = inspect.getsource(tariff_crud.assert_tariff_squad_rollout_allowed)
    assert '_assert_tariff_squad_change_has_no_live_checkout(db, tariff)' in source


def test_legacy_tariffs_are_refused_by_name() -> None:
    """Раскатка по наследованному режиму выдала бы не то право, за которое платили."""

    source = inspect.getsource(admin_tariffs._load_rollout_tariff)
    assert "'native_squads'" in source


def test_migration_0104_is_additive() -> None:
    path = Path(__file__).parents[2] / 'migrations/alembic/versions/0104_tariff_squad_rollout_snapshots.py'
    source = path.read_text(encoding='utf-8')
    assert "revision: str = '0104'" in source
    assert "down_revision: Union[str, None] = '0103'" in source
    upgrade = source.split('def upgrade() -> None:', 1)[1].split('def downgrade', 1)[0]
    # Ничего не конвертирует и не переписывает: только создаёт свою таблицу.
    assert 'op.create_table(' in upgrade
    for forbidden in ('op.drop_table(', 'op.alter_column(', 'op.execute('):
        assert forbidden not in upgrade, forbidden


# 🔴 Ниже — сторожа на четыре мутации, которые пережили набор в волне 2 ревью.
# Каждая из них тихо снимала защиту, добавленную как починка предыдущей волны.


@pytest.mark.asyncio
@pytest.mark.parametrize('route_name', ['run_squad_rollout', 'restore_squad_rollout'])
async def test_route_passes_the_fence_recheck_into_the_service(monkeypatch, route_name) -> None:
    """Перепроверку забора мало написать в сервисе — её надо ДОВЕСТИ до него.

    Мутация «убрать recheck_fence= из вызова» проходила все тесты: маршрутные
    проверяли только факт вызова забора, сервисный бил в сервис напрямую.
    """

    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=_rollout_tariff()))
    monkeypatch.setattr(admin_tariffs, 'assert_tariff_squad_rollout_allowed', AsyncMock())
    monkeypatch.setattr(admin_tariffs, 'AuditLogCRUD', SimpleNamespace(create=AsyncMock()))
    result = PropagateSquadsResult(total=1, synced=1, rollout_id='r-1')
    calls: dict = {}

    async def _capture(*args, **kwargs):
        calls.update(kwargs)
        return result

    service = SimpleNamespace(propagate_tariff_squads=_capture, restore_tariff_squads=_capture)
    monkeypatch.setattr(admin_tariffs, 'SubscriptionService', lambda: service)

    route = getattr(admin_tariffs, route_name)
    kwargs = {'admin': SimpleNamespace(id=1), 'db': AsyncMock()}
    if route_name == 'run_squad_rollout':
        kwargs['request'] = SquadRolloutRequest()
    await route(3, **kwargs)

    assert callable(calls.get('recheck_fence')), f'{route_name} не передал перепроверку забора'


@pytest.mark.asyncio
async def test_fence_recheck_also_refuses_when_the_tariff_servers_changed(monkeypatch) -> None:
    """Тариф могли отредактировать между порциями — оставшиеся не должны разносить старое."""

    tariff = _rollout_tariff()
    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(admin_tariffs, 'assert_tariff_squad_rollout_allowed', AsyncMock())
    monkeypatch.setattr(admin_tariffs, 'AuditLogCRUD', SimpleNamespace(create=AsyncMock()))
    captured: dict = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return PropagateSquadsResult(total=1, synced=1, rollout_id='r-1')

    monkeypatch.setattr(admin_tariffs, 'SubscriptionService', lambda: SimpleNamespace(propagate_tariff_squads=_capture))
    db = AsyncMock()
    # тариф «переехал» на другой набор серверов, пока шла раскатка
    db.get = AsyncMock(return_value=SimpleNamespace(id=3, allowed_squads=['squad-b']))

    await admin_tariffs.run_squad_rollout(3, SquadRolloutRequest(), admin=SimpleNamespace(id=1), db=db)

    with pytest.raises(ValueError, match='Серверы тарифа изменились'):
        await captured['recheck_fence']()


def test_audit_status_is_success_only_when_nothing_was_left_behind() -> None:
    """Мутация «смотреть только на упавшие» проходила набор: возврат, не вернувший
    никого, записывался в аудит как полный успех."""

    assert admin_tariffs._rollout_audit_status(PropagateSquadsResult(total=2, synced=2)) == 'success'
    for partial in (
        PropagateSquadsResult(total=2, synced=1, failed_ids=[1]),
        PropagateSquadsResult(total=2, synced=1, stopped_early=True),
        PropagateSquadsResult(total=2, synced=0, unrestorable_ids=[1, 2]),
        PropagateSquadsResult(total=2, synced=1, skipped_traffic_risk_ids=[2]),
        PropagateSquadsResult(total=2, synced=1, shared_account_ids=[2]),
        PropagateSquadsResult(total=2, synced=1, moved_on_ids=[2]),
        PropagateSquadsResult(total=2, synced=2, remaining=5),
    ):
        assert admin_tariffs._rollout_audit_status(partial) == 'partial', partial


@pytest.mark.asyncio
async def test_public_fence_really_raises_from_the_canonical_one(monkeypatch) -> None:
    """Обёртку забора проверяли только чтением исходника — она могла бы стать пустой."""

    from app.database.crud import tariff as tariff_crud

    called = AsyncMock(side_effect=ValueError('live checkout'))
    monkeypatch.setattr(tariff_crud, '_assert_tariff_squad_change_has_no_live_checkout', called)

    with pytest.raises(ValueError, match='live checkout'):
        await tariff_crud.assert_tariff_squad_rollout_allowed(AsyncMock(), _rollout_tariff())

    called.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_checkout_refusal_is_explained_in_russian(monkeypatch) -> None:
    """Владелец упрётся в этот отказ рутинно — он не должен быть английским жаргоном."""

    monkeypatch.setattr(admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=_rollout_tariff()))
    monkeypatch.setattr(
        admin_tariffs,
        'assert_tariff_squad_rollout_allowed',
        AsyncMock(side_effect=ValueError('Cannot change Internal Squads while this tariff has a live checkout')),
    )

    with pytest.raises(HTTPException) as exc_info:
        await admin_tariffs.run_squad_rollout(3, SquadRolloutRequest(), admin=SimpleNamespace(id=1), db=AsyncMock())

    detail = str(exc_info.value.detail)
    assert 'Internal Squads' not in detail and 'checkout' not in detail
    assert 'незакрытый заказ' in detail and 'нажмите снова' in detail

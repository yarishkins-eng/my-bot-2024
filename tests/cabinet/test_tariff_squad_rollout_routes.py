"""Сторожа маршрута раскатки (пункт 3.2 плана, мина AE).

Мина AE: `PUT /tariffs/{id}/locations` (admin_public_locations) присваивает
entitlement_mode МИМО забора живых заказов. Сегодня это безопасно только потому,
что у маршрута нет фронта. Если раскатка позовёт его — забор перестанет
существовать ровно в переезд, ради которого его и ставили.
"""

import inspect
from pathlib import Path

from app.cabinet.routes import admin_tariffs


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


def test_rollout_takes_the_live_checkout_fence() -> None:
    """Раскатка трогает выданные права жёстче правки тарифа — забор обязателен."""

    for name in ('run_squad_rollout', 'restore_squad_rollout'):
        source = inspect.getsource(getattr(admin_tariffs, name))
        assert 'assert_tariff_squad_rollout_allowed' in source, name


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

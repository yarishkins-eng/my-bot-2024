from pathlib import Path

from app.database.models import Tariff


def test_native_squad_migration_is_additive_and_forward_only() -> None:
    path = Path(__file__).parents[2] / 'migrations/alembic/versions/0102_native_tariff_squads.py'
    source = path.read_text()

    assert "revision: str = '0102'" in source
    assert "down_revision: Union[str, None] = '0101'" in source
    assert "'native_squads'" in source
    downgrade = source.split('def downgrade() -> None:', 1)[1]
    assert 'raise RuntimeError' in downgrade
    assert 'op.drop_' not in downgrade


def test_fresh_database_metadata_includes_the_current_entitlement_constraint() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in Tariff.__table__.constraints
        if constraint.name == 'ck_tariffs_entitlement_mode'
    }

    assert 'ck_tariffs_entitlement_mode' in constraints
    assert 'native_squads' in constraints['ck_tariffs_entitlement_mode']

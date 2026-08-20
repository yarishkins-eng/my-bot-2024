"""Сторожа для списка моделей резервной копии (пункт 4.15, 20.08.2026).

До 20.08.2026 `_base_backup_models` знал 95 моделей из 138. Вне копии оставались
заказы, платёжные попытки, снимки прав и снимки раскатки — почти треть базы.
Узнать об этом было неоткуда: кнопка отчитывалась числом таблиц ЖИВОЙ базы,
а не числом таблиц, попавших в файл.

🔴 Ожидание здесь выводится из `Base.metadata`, а НЕ из константы, которую
проверяем. Сторож, перебирающий тот же список, что и код, в этом проекте уже
трижды пережил мутацию (грабли 17.08, 18.08, 20.08): он краснеет только вместе
с кодом и потому не краснеет никогда.
"""

from __future__ import annotations

import base64
import json
import tarfile

import pytest

from app.database.models import Base
from app.services.backup_service import backup_service


# Ссылки, которые НЕЛЬЗЯ разложить порядком: таблицы ссылаются друг на друга.
# Одну из двух ссылок восстановление откладывает и проставляет отдельным шагом.
# Ключ — таблица, значение — цель, которую разрешено иметь ниже по списку.
DEFERRED_FOREIGN_KEYS = {
    'transactions': {'subscription_checkouts'},  # → _relink_checkout_transactions
}

# Типы колонок, которые ORM-дамп умеет положить в JSON и поднять обратно.
# Колонка любого другого типа означает, что дамп либо потеряет её, либо упадёт
# целиком (`json.dumps` на bytes бросает TypeError мимо всех обработчиков).
SERIALIZABLE_TYPE_FAMILIES = (
    'BIGINT',
    'BLOB',
    'BOOLEAN',
    'DATE',
    'DATETIME',
    'FLOAT',
    'INTEGER',
    'JSON',
    'JSONB',
    'NUMERIC',
    'REAL',
    'SMALLINT',
    'TEXT',
    'TIME',
    'TIMESTAMP',
    'VARCHAR',
)


def _backup_tables() -> list[str]:
    """Таблицы в том порядке, в каком их пишет и читает бекап."""
    models = backup_service._get_models_for_backup(include_logs=True)
    return [model.__tablename__ for model in models]


def test_backup_covers_every_model_in_the_database() -> None:
    """Ни одна модель не должна остаться вне копии — включая те, что появятся завтра."""
    known = set(_backup_tables()) | set(backup_service.association_tables)
    forgotten = sorted(set(Base.metadata.tables) - known)

    assert not forgotten, (
        f'Эти таблицы не попадут в резервную копию: {forgotten}. '
        'Добавьте модель в `_base_backup_models` — в место, где все её внешние ключи уже выше.'
    )


def test_backup_list_has_no_tables_that_left_the_database() -> None:
    """Обратная сторона: в списке не должно быть таблиц, которых в базе уже нет."""
    stale = sorted(set(_backup_tables()) - set(Base.metadata.tables) - set(backup_service.association_tables))

    assert not stale, f'В списке бекапа есть таблицы, которых нет в базе: {stale}'


def test_backup_list_is_ordered_so_foreign_keys_resolve() -> None:
    """Цель внешнего ключа обязана стоять ВЫШЕ ссылающейся таблицы.

    Иначе строка падает на внешнем ключе, `_restore_table_records` глушит
    `IntegrityError` и молча теряет запись — в лог уходит «Дубликат по
    уникальному ключу», то есть неверная причина.
    """
    order = {table: index for index, table in enumerate(_backup_tables())}
    violations = []

    for table_name, position in order.items():
        table = Base.metadata.tables[table_name]
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if target == table_name or target in DEFERRED_FOREIGN_KEYS.get(table_name, set()):
                continue
            if order.get(target, -1) > position:
                violations.append(f'{table_name} ссылается на {target}, а тот восстанавливается позже')

    assert not violations, sorted(set(violations))


@pytest.mark.parametrize('table_name', sorted(Base.metadata.tables))
def test_every_backed_up_column_survives_json(table_name: str) -> None:
    """Колонка неизвестного дампу типа тихо теряется или роняет весь бекап."""
    unsupported = [
        f'{table_name}.{column.name} ({column.type})'
        for column in Base.metadata.tables[table_name].columns
        if not str(column.type).upper().startswith(SERIALIZABLE_TYPE_FAMILIES)
    ]

    assert not unsupported, unsupported


def test_binary_column_survives_the_round_trip() -> None:
    """Двоичные колонки едут через base64: сырые bytes в JSON не сериализуются вовсе."""
    model = Base.metadata.tables['entitlement_cleanup_commands']
    column = 'encrypted_panel_uuid'
    assert 'BLOB' in str(model.columns[column].type).upper(), 'Тест держится за двоичную колонку — её тип изменился'

    encoded = 'AQIDBAU='  # b'\x01\x02\x03\x04\x05'
    json.dumps({column: encoded})  # то, что делает дамп: строка обязана быть сериализуемой

    from app.database.models import EntitlementCleanupCommand

    restored = backup_service._process_record_data({column: encoded}, EntitlementCleanupCommand, model.name)

    assert restored[column] == b'\x01\x02\x03\x04\x05'


def test_backup_list_has_no_duplicates() -> None:
    """Дубль в списке — это таблица, которую выгрузят и восстановят дважды.

    Найдено мутацией 20.08.2026: перестановка строки «вверх» без удаления старой
    оставляла список рабочим на вид, а проверку порядка — слепой.
    """
    tables = _backup_tables()
    duplicated = sorted({name for name in tables if tables.count(name) > 1})

    assert not duplicated, f'Модель встречается в списке бекапа больше одного раза: {duplicated}'


@pytest.mark.asyncio
async def test_backup_report_counts_the_file_not_the_live_database(tmp_path, monkeypatch) -> None:
    """Отчёт обязан называть содержимое ФАЙЛА.

    До 20.08.2026 он печатал пересчёт живой базы: каждую ночь «142 таблицы,
    14 246 записей» при 95 таблицах в файле. Дыра в списке моделей прожила
    месяц именно потому, что отчёт про неё рассказать не мог.
    """
    from app.services.backup_service import BackupService

    service = BackupService.__new__(BackupService)
    service.bot = None
    service.backup_dir = tmp_path / 'backups'
    service.backup_dir.mkdir(parents=True)
    service.data_dir = tmp_path
    service.archive_format_version = '2.0'
    service._settings = backup_service._settings

    async def fake_overview():
        return {'tables_count': 142, 'total_records': 14246, 'tables': []}

    async def fake_dump(staging_dir, include_logs):
        (staging_dir / 'database.json').write_text('{}', encoding='utf-8')
        return {
            'type': 'postgresql',
            'path': 'database.json',
            'format': 'json',
            'tool': 'orm',
            'tables_count': 98,
            'total_records': 11470,
        }

    async def fake_files(staging_dir, include_logs):
        return []

    async def fake_snapshot(staging_dir):
        return {'path': str(tmp_path), 'items': 0}

    async def fake_cleanup():
        return None

    monkeypatch.setattr(service, '_collect_database_overview', fake_overview)
    monkeypatch.setattr(service, '_dump_database', fake_dump)
    monkeypatch.setattr(service, '_collect_files', fake_files)
    monkeypatch.setattr(service, '_collect_data_snapshot', fake_snapshot)
    monkeypatch.setattr(service, '_cleanup_old_backups', fake_cleanup)

    ok, message, path = await service.create_backup(compress=True, include_logs=False)

    assert ok, message
    assert '98 из 142' in message, message
    assert '11,470' in message or '11 470' in message, message
    assert '2,776' in message or '2 776' in message, f'Отчёт молчит о несохранённых записях: {message}'

    with tarfile.open(path, 'r:gz') as archive:
        metadata = json.loads(archive.extractfile('metadata.json').read())

    assert metadata['tables_count'] == 98
    assert metadata['total_records'] == 11470
    assert metadata['database_tables_count'] == 142


@pytest.mark.asyncio
async def test_dump_serialises_binary_values(monkeypatch) -> None:
    """Сырые bytes в дампе роняют ВЕСЬ бекап, а не одну таблицу.

    `json.dumps` бросает на них TypeError уже после того, как все таблицы
    выгружены, и обработчика на этом уровне нет. Проверяем через настоящий
    экспорт, а не через сам сериализатор: мутация «снять ветку bytes»
    иначе переживает набор — её ловила только репетиция на живой базе.
    """
    from unittest.mock import AsyncMock, MagicMock

    from app.database.models import EntitlementCleanupCommand
    from app.services import backup_service as module

    record = MagicMock()
    for column in EntitlementCleanupCommand.__table__.columns:
        setattr(record, column.name, None)
    record.encrypted_panel_uuid = b'\x01\x02\x03'

    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]

    session = AsyncMock()
    session.execute.return_value = result
    session.__aenter__.return_value = session
    monkeypatch.setattr(module, 'AsyncSessionLocal', lambda: session)
    monkeypatch.setattr(backup_service, '_export_association_tables', AsyncMock(return_value={}))

    data, _, _, _ = await backup_service._export_database_via_orm([EntitlementCleanupCommand])

    dumped = data['entitlement_cleanup_commands'][0]['encrypted_panel_uuid']
    assert isinstance(dumped, str), f'Двоичное поле ушло в дамп как {type(dumped)!r} — json.dumps на нём упадёт'
    json.dumps(data)  # то, что делает `_dump_postgres_json`: обязано не бросать
    assert base64.b64decode(dumped) == b'\x01\x02\x03'

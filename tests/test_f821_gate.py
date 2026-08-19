"""Ворота против неопределённых имён (мина AW, 19.08.2026).

Пока `F821` лежал в общем `ignore` в pyproject.toml, `ruff check .` не видел кода, который
падает `NameError` на первой же строке. Зелёный CI ничего не доказывал: на пункте 3.2 так
чуть не уехали маршруты, которые вообще не могли исполниться.

🔴 ЧТО ЭТИ ВОРОТА ДОКАЗЫВАЮТ, А ЧТО НЕТ. F821 говорит ровно одно: **имя определено в этом же
файле**. Он НЕ ловит `from app.database.models import НетТакойМодели` — а это тот же класс
отказа, и он роняет бота на старте; не ловит неверное имя атрибута, опечатку в ключе словаря,
неверное число аргументов и недостижимый код. Формулировка «зелёный CI теперь доказывает, что
код запустится» — **преувеличение**, записывать её никуда нельзя (мина BB).

Сторожа ниже закрывают РАЗНЫЕ дыры, один другого не заменяет. Набор дыр установлен не
рассуждением, а мутационным прогоном скептика: он нашёл ЧЕТЫРЕ способа заглушить правило,
и первая версия этих тестов ловила только один из них.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Где может прятаться заглушка: только наш код, без чужих библиотек из .venv.
_SEARCH_ROOTS = ('app', 'tests', 'migrations')

# Зоны, которые ruff по конфигу НЕ проверяет (`exclude` и `per-file-ignores = ['ALL']`),
# но в которых лежит исполняемый код: миграции гоняются при деплое, `app/lib` импортируется
# из `app/services/nalogo_service.py`. Проверяем их отдельным прогоном в обход конфига.
_RUFF_BLIND_SPOTS = ('app/lib', 'migrations')

_EXPECTED_EXCLUDES = {'.venv', 'venv', '.tox', '.nox', 'build', 'dist', 'node_modules', 'migrations'}


def _ruff(*args: str) -> subprocess.CompletedProcess[str]:
    # Заглушка S603 безопасна: команда собрана из литералов, снаружи в неё ничего не приходит.
    return subprocess.run(  # noqa: S603
        [sys.executable, '-m', 'ruff', *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_undefined_names_anywhere() -> None:
    """Ни одного неопределённого имени там, куда ruff смотрит по конфигу.

    Прогоняет тот же анализ, который найдёт ошибку в CI, а не читает исходники глазами:
    сторож, ищущий подстроки в файле, уже подводил нас дважды (грабли 19.08).
    """
    result = _ruff('check', '--select', 'F821', '--output-format', 'concise', '.')

    assert result.returncode == 0, (
        'Найдены имена, которые нигде не определены — такой код падает NameError при исполнении.\n'
        'Это не придирка линтера: именно так на пункте 3.2 чуть не выложили нерабочие маршруты.\n\n'
        f'{result.stdout}{result.stderr}'
    )


def test_no_undefined_names_in_the_zones_ruff_skips() -> None:
    """Слепые зоны ruff проверяются отдельно, в обход конфига.

    `exclude` прячет `migrations` (исполняются при каждом деплое), а `per-file-ignores`
    гасит в `app/lib/**` ВСЕ правила разом. Мутации скептика прошли обе зоны молча.
    `--isolated` заставляет ruff забыть pyproject.toml целиком.
    """
    result = _ruff('check', '--isolated', '--select', 'F821', '--output-format', 'concise', *_RUFF_BLIND_SPOTS)

    assert result.returncode == 0, (
        'Неопределённое имя в зоне, которую основной прогон ruff не проверяет.\n'
        'Миграции исполняются при деплое, app/lib импортируется из app/services/nalogo_service.py —\n'
        'это живой код, а не архив.\n\n'
        f'{result.stdout}{result.stderr}'
    )


def test_f821_is_not_silenced_anywhere_in_the_config() -> None:
    """'F821' не заглушён НИ ОДНИМ из способов, какие есть у ruff.

    🔴 Первая версия читала только `lint.ignore` — и скептик прошёл мимо неё тремя другими
    путями, оставив живую поломку в коде при всех зелёных проверках: `per-file-ignores`,
    `[tool.ruff] exclude` на файл с поломкой и удаление 'F' из `select`. Стережём все входы
    в механизм, а не тот, через который зашли в прошлый раз.
    """
    config = tomllib.loads((PROJECT_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    ruff = config['tool']['ruff']
    lint = ruff['lint']

    assert 'F821' not in lint['ignore'], (
        "'F821' снова заглушён в `lint.ignore`. С ним `ruff check .` перестаёт видеть код, "
        'который не может исполниться, и зелёный CI снова ничего не доказывает (мина AW).'
    )

    selected = lint['select']
    assert 'F821' in selected or 'F' in selected or 'ALL' in selected, (
        f'Правило F821 выпало из `lint.select` (сейчас: {selected}). Ворота выключены не через '
        '`ignore`, а через `select` — снаружи это выглядит так же: линтер молчит на неисполнимом коде.'
    )

    silenced_per_file = {
        pattern: rules
        for pattern, rules in lint.get('per-file-ignores', {}).items()
        if 'F821' in rules or 'F' in rules or 'ALL' in rules
    }
    # `app/lib/**` заглушён целиком исторически — его стережёт отдельный прогон выше.
    silenced_per_file.pop('app/lib/**/*.py', None)
    assert not silenced_per_file, (
        f'F821 заглушён через `per-file-ignores`: {silenced_per_file}. Это обход ворот мимо '
        '`ignore`; скептик прошёл им насквозь, оставив NameError в боевом коде при зелёном CI.'
    )

    unexpected = set(ruff.get('exclude', [])) - _EXPECTED_EXCLUDES
    assert not unexpected, (
        f'В `[tool.ruff] exclude` появились новые пути: {unexpected}. Внесённый туда файл выпадает '
        'из проверки целиком. Если это намеренно — добавь путь и в `_EXPECTED_EXCLUDES`, и в '
        '`_RUFF_BLIND_SPOTS`, чтобы он проверялся отдельным прогоном, а не молчал.'
    )


def test_f821_is_not_silenced_by_comments_in_the_code() -> None:
    """Заглушек комментарием ровно одна, и та помечает мёртвый код.

    Ловим ОБЕ формы, потому что мимо первой версии сторожа скептик прошёл файловой:
      · построчная `# noqa: F821` — глушит одну строку;
      · файловая `# ruff: noqa: F821` — глушит правило на ВЕСЬ файл.

    Считаем файлы и их количество, а не номера строк: номер уехал бы от любой правки выше
    и сделал бы сторожа ложно-красным, а его сообщение — загадкой.
    """
    per_line = re.compile(r'#\s*noqa:[^\n]*\bF821\b')
    whole_file = re.compile(r'#\s*ruff:\s*noqa[^\n]*\bF821\b')

    found: dict[str, int] = {}
    for root in _SEARCH_ROOTS:
        for path in (PROJECT_ROOT / root).rglob('*.py'):
            if '__pycache__' in path.parts:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            hits = len(per_line.findall(text)) + len(whole_file.findall(text))
            if hits:
                found[str(path.relative_to(PROJECT_ROOT))] = hits

    assert found == {'app/handlers/subscription/purchase.py': 1}, (
        'Изменился набор заглушек F821 в коде. Согласована ровно одна — метка мёртвого хвоста '
        '`handle_simple_subscription_purchase` (код за безусловным `return`; выносится пунктом 5.8 '
        'плана восстановления). 🔴 Когда хвост вынесут, ожидание здесь меняется на пустое — это '
        f'плановое событие, а не поломка. Сейчас: {found}'
    )

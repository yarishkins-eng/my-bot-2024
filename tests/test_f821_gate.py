"""Ворота против неопределённых имён (мина AW, 19.08.2026).

Пока `F821` лежал в общем `ignore` в pyproject.toml, `ruff check .` не видел кода, который
падает `NameError` на первой же строке. Зелёный CI ничего не доказывал: на пункте 3.2 так
чуть не уехали маршруты, которые вообще не могли исполниться.

Здесь два сторожа, и они закрывают РАЗНЫЕ дыры — один другого не заменяет:

* `test_no_undefined_names_anywhere` ловит возврат самой ошибки (снесли импорт — покраснел);
* `test_f821_is_not_silenced_in_config` ловит выключение ворот. Он нужен отдельно, потому что
  `--select F821` в командной строке ПЕРЕБИВАЕТ конфиг: первый сторож остался бы зелёным,
  даже если вернуть 'F821' в `ignore` и ослепить CI обратно.
"""

import re
import subprocess
import sys
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_no_undefined_names_anywhere() -> None:
    """Ни одного неопределённого имени во всём репозитории.

    Прогоняет тот же анализ, который найдёт эту ошибку в CI, а не читает исходники глазами:
    сторож, который ищет подстроки в файле, уже подводил нас дважды (грабли 19.08).
    """
    # Заглушка S603 ниже безопасна: команда собрана из литералов, снаружи в неё ничего не приходит.
    result = subprocess.run(  # noqa: S603
        [sys.executable, '-m', 'ruff', 'check', '--select', 'F821', '--output-format', 'concise', '.'],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        'Найдены имена, которые нигде не определены — такой код падает NameError при исполнении.\n'
        'Это не придирка линтера: именно так на пункте 3.2 чуть не выложили нерабочие маршруты.\n\n'
        f'{result.stdout}{result.stderr}'
    )


def test_f821_is_not_silenced_in_config() -> None:
    """'F821' не вернулся в список заглушённых правил ruff.

    Отдельный сторож, а не паранойя: правило уже стояло в `ignore` и молчало месяцами.
    """
    config = tomllib.loads((PROJECT_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    ignored = config['tool']['ruff']['lint']['ignore']

    assert 'F821' not in ignored, (
        "'F821' снова заглушён в pyproject.toml. С ним `ruff check .` перестаёт видеть код, "
        'который не может исполниться, и зелёный CI снова ничего не доказывает (мина AW).'
    )


def test_the_only_allowed_f821_exception_is_the_dead_tail_of_simple_purchase() -> None:
    """Точечная заглушка на одну строку разрешена ровно одна — и та помечает мёртвый код.

    Сторож на случай, если «ну тут же тоже мёртвый код» станет привычкой: тогда ворота
    растворятся не через конфиг, а через россыпь одиночных заглушек по файлам.

    Считаем ФАЙЛЫ и их количество, а не номера строк: номер уехал бы от любой правки выше
    и сделал бы сторожа ложно-красным, а его сообщение — загадкой.
    """
    marker = '#' + ' noqa: F821'  # собран из кусков, чтобы этот файл не нашёл сам себя
    found: dict[str, int] = {}
    for path in PROJECT_ROOT.glob('**/*.py'):
        if any(part in {'.venv', '__pycache__', 'migrations'} for part in path.parts):
            continue
        hits = len(re.findall(r'#\s*noqa:[^\n]*\bF821\b', path.read_text(encoding='utf-8')))
        if hits:
            found[str(path.relative_to(PROJECT_ROOT))] = hits

    assert found == {'app/handlers/subscription/purchase.py': 1}, (
        f'Изменился набор точечных заглушек {marker}. Согласована ровно одна — метка мёртвого '
        'хвоста `handle_simple_subscription_purchase` (код за безусловным `return` на :4601, '
        f'выносится этапом 5). Сейчас: {found}'
    )

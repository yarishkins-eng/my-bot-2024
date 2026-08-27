"""РС-3: надпись фильтра рассылки обязана совпадать с тем, что он отбирает.

Прежние названия обещали не то, что делали. Самое дорогое расхождение:
«⏰ Истекающие» отбирает всё, что кончается в ближайшие 3 дня, и НЕ отделяет
пробные. А пробная длится ровно 3 дня (`TRIAL_DURATION_DAYS`), значит любая живая
пробная сидит в этом окне с первой секунды — не «почти все», а ВСЕ. Владелец,
отправив туда «продлите подписку», обращался бы к тем, кто ни разу не платил.

Сторожа проверяют СВОЙСТВО, а не буквы.

🔴 Три урока от ревью, каждый закрыт отдельным сторожем:

1. **Названий три, а не одно.** Кнопка (`keyboards/admin.py`), экран подтверждения
   (`messages.py: target_names`) и история (`messages.py: get_target_name`). Первая
   версия правки переименовала только кнопку — и честную надпись владелец видел один
   раз, а прежнюю обманную дважды, включая момент решения «отправить».

2. **Надпись можно оставить, а предикат поменять** — и все сторожа останутся зелёными.
   В комментарии к `FILTER_LABELS` прямо записано, что сузить `expiring` до платных
   когда-нибудь может владелец. В этот день оговорка «(+пробные)» станет ложью, поэтому
   `test_expiring_predicate_still_mixes_trials` привязан к ИСХОДНИКУ запроса.

3. **«Ни разу не подключались» было ЛОЖЬЮ**, хоть и звучало понятнее «0 ГБ».
   `traffic_used_gb` — счётчик текущего периода, он обнуляется при каждом продлении
   (`crud/subscription.py`, `extend_subscription`). Платящий, продливший вчера и ещё не
   подключившийся, попадал бы под «ни разу за всю жизнь». Верно — «0 ГБ за период».

⛔ Границы: здесь проверяются только НАДПИСИ. Предикаты намеренно не тронуты — сузить
`expiring` до платных значит поменять аудиторию, а это решение владельца.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re

import pytest

from app.cabinet.routes.admin_broadcasts import FILTER_LABELS
from app.handlers.admin import messages as admin_messages


# От `__file__`, а не от текущей папки: тест обязан охранять и при запуске не из корня.
_LOCALES = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'

# Фильтры, чей предикат НЕ отделяет пробные подписки от платных.
_MIXES_TRIALS = ('expiring', 'expired')

# Фильтры про нулевой расход. Это «0 ГБ за ТЕКУЩИЙ ПЕРИОД» и только при ДЕЙСТВУЮЩЕЙ
# подписке — оба уточнения обязаны быть в надписи.
_ZERO_TRAFFIC = ('zero', 'active_zero', 'trial_zero')


def _locale(name: str) -> dict:
    return json.loads((_LOCALES / name).read_text(encoding='utf-8'))


# Слова, которыми надпись может СОДЕРЖАТЬ нужный корень и при этом утверждать обратное.
# Дыру нашёл мутационный скептик: «Заканчиваются за 3 дня (пробные НЕ входят)» проходило
# проверку «есть ли „пробн“» — то есть сторож одинаково радовался и признанию, и лжи.
_NEGATIONS = ('не входят', 'не включ', 'без пробн', 'кроме пробн', 'исключая')

# Опознавательный признак КАЖДОГО фильтра: без него надписи можно поменять местами
# (тоже находка скептика) — админ жал бы «заканчивается», а читал про «закончилась».
_MUST_CONTAIN = {
    'expiring': '3 дн',
    'expired': 'закончилась',
}


@pytest.mark.parametrize('key', _MIXES_TRIALS)
def test_cabinet_label_admits_trials_are_included(key: str) -> None:
    """Фильтр тащит пробные вместе с платными — надпись обязана предупредить."""
    label = FILTER_LABELS[key]
    low = label.lower()
    assert 'пробн' in low, f'«{label}» умалчивает, что в {key} попадают пробные подписки'
    # Корень слова есть — но не в отрицании: иначе надпись «врёт наоборот» и проходит.
    for bad in _NEGATIONS:
        assert bad not in low, f'«{label}» отрицает то, что фильтр делает: пробные в {key} ПОПАДАЮТ'


@pytest.mark.parametrize('key', _MIXES_TRIALS)
def test_label_describes_its_own_filter(key: str) -> None:
    """Надпись обязана описывать СВОЙ фильтр, а не соседний.

    🔴 Дыру нашёл мутационный скептик: обе надписи содержат «пробные», поэтому их можно
    поменять местами и все проверки останутся зелёными. На экране это значило бы, что
    админ жмёт «заканчивается через 3 дня», а читает про уже закончившиеся.
    """
    label = FILTER_LABELS[key].lower()
    marker = _MUST_CONTAIN[key]
    assert marker in label, f'«{FILTER_LABELS[key]}» не похожа на описание фильтра {key} (нет «{marker}»)'


@pytest.mark.parametrize('key', _ZERO_TRAFFIC)
def test_zero_traffic_label_says_period_and_active(key: str) -> None:
    """«0 ГБ» — это текущий ПЕРИОД и только у ДЕЙСТВУЮЩЕЙ подписки.

    Оба уточнения существенны: счётчик обнуляется при продлении, а предикат требует
    живой подписки. Формулировка «ни разу не подключались» нарушала и то и другое.
    """
    label = FILTER_LABELS[key].lower()
    assert 'период' in label, f'«{FILTER_LABELS[key]}»: счётчик обнуляется при продлении, надо сказать «за период»'
    assert 'действующ' in label, f'«{FILTER_LABELS[key]}»: предикат требует действующей подписки, надпись молчит'
    assert 'подключал' not in label, f'«{FILTER_LABELS[key]}» обещает историю подключений, которой в данных нет'


def test_expiring_predicate_still_mixes_trials() -> None:
    """Надпись говорит «(+пробные)» — предикат обязан это подтверждать.

    🔴 Дыру назвало ревью: сторожа проверяли только надписи, поэтому предикат можно было
    сузить до платных, оставив оговорку, и всё осталось бы зелёным. Здесь надпись
    привязана к КОДУ: если в ветку `expiring` добавят фильтр по `is_trial`, тест
    покраснеет и заставит переписать надпись вместе с предикатом.
    """
    source = inspect.getsource(admin_messages.get_target_users_count)
    branch = re.search(r"if target == 'expiring':(.*?)(?=\n    if target ==|\n    elif target ==)", source, re.DOTALL)
    assert branch, 'ветка expiring не найдена — тест устарел вместе с кодом, чинить его, а не отключать'
    assert 'is_trial' not in branch.group(1), (
        'В `expiring` появился фильтр по is_trial — значит пробные больше НЕ попадают, '
        'и оговорка «(+пробные)» в надписях стала ложью. Переписать надписи во всех ТРЁХ местах.'
    )


def test_all_three_name_sources_agree() -> None:
    """Названий три, и они обязаны совпадать по составу ключей.

    Кнопка, экран подтверждения и история. Первая версия правки тронула одну кнопку —
    и владелец на экране решения читал прежнее обманное название.
    """
    confirm = inspect.getsource(admin_messages.select_broadcast_target)
    history = inspect.getsource(admin_messages.get_target_name)
    for key in _MIXES_TRIALS:
        for name, src in (('экран подтверждения', confirm), ('история', history)):
            found = re.search(rf"'{key}': '([^']*)'", src)
            assert found, f'{name}: ключ {key} потерян'
            assert 'пробн' in found.group(1).lower(), (
                f'{name}: «{found.group(1)}» умалчивает про пробные, хотя кнопка про них говорит. '
                f'Именно так владелец и читал прежнее обещание на экране решения.'
            )


def test_bot_button_labels_admit_trials() -> None:
    """Та же честность в кнопках чат-админки, а не только в кабинете."""
    ru = _locale('ru.json')
    assert 'пробн' in ru['ADMIN_BROADCAST_TARGET_EXPIRING'].lower()
    assert 'пробн' in ru['ADMIN_BROADCAST_TARGET_EXPIRED'].lower()


def test_both_locales_cover_every_broadcast_target() -> None:
    """Русские и английские подписи не должны разъезжаться по составу ключей."""
    ru = {k for k in _locale('ru.json') if k.startswith('ADMIN_BROADCAST_TARGET_')}
    en = {k for k in _locale('en.json') if k.startswith('ADMIN_BROADCAST_TARGET_')}
    assert ru == en, f'ключи разошлись: только в ru {ru - en}, только в en {en - ru}'


def test_english_labels_admit_trials_too() -> None:
    """Английские подписи несут ту же оговорку, а не старое обещание."""
    en = _locale('en.json')
    assert 'trial' in en['ADMIN_BROADCAST_TARGET_EXPIRING'].lower()
    assert 'trial' in en['ADMIN_BROADCAST_TARGET_EXPIRED'].lower()

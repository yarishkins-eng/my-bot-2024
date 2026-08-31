"""РЕК-6А: форма кампании обязана предупреждать, что метку видит клиент.

Метка (`start_parameter`) — ПУБЛИЧНАЯ по устройству Телеграма: заходя по рекламной ссылке,
человек отправляет в свой чат `/start <метка>`, и сообщение там остаётся. Владелец называет
кампании внутренними кличками с рекламным бюджетом («Кувалда 7000₽»); метка `kuvalda7000`
вернула бы утечку, которую закрыл РЕК-1, — мимо всякого кода.

Форм четыре: чат-админка (создание и правка метки) и кабинет (создание и правка). Здесь
стерегутся две ботовые; кабинетные — в `cabinet-code/src/locales/startParameterHint.honesty.test.ts`.

⚠️ ГРАНИЦЫ, названные честно. Первую редакцию докстринга пришлось переписать: она была шире
правды, и это поймала линза корректности.

1. Два теста читают ОБЪЕКТ (импортируют константу), третий РАЗБИРАЕТ КОД двух функций.
   Две прежние редакции искали имя подстрокой — сначала по файлу, потом по исходнику функции —
   и обе были зелёными на мутации, где имя оставалось в комментарии. Разбор кода это исключает.
2. Что константа доходит до ЭКРАНА, не доказывает ни один тест: это `await message.answer`
   внутри FSM-обработчиков, поднимать их ради подсказки дороже пользы.
3. 🔴 Сторож на ключевые слова НЕ отличает предупреждение от его отрицания: текст «клиент НЕ
   увидит, писать можно что угодно» содержит все искомые слова и пройдёт зелёным. Машине смысл
   недоступен — проверяется присутствие предупреждения, а не его правдивость.
"""

import ast
import inspect
import textwrap

from app.handlers.admin import campaigns
from app.handlers.admin.campaigns import START_PARAMETER_CHANGE_WARNING, START_PARAMETER_PUBLIC_WARNING


def test_warning_says_the_client_will_see_the_tag():
    text = START_PARAMETER_PUBLIC_WARNING.lower()
    assert 'клиент' in text, 'предупреждение перестало называть того, кто метку увидит'
    # «видит» — подстрока «увидит», поэтому одной проверки хватает: прежняя развилка
    # `'увидит' in text or 'видит' in text` была мёртвой, второй операнд недостижим.
    assert 'видит' in text, 'предупреждение перестало говорить, что метку ВИДНО'


def test_warning_forbids_the_two_things_that_actually_leaked():
    """Не общее «будьте осторожны», а ровно то, что владелец пишет в имени кампании."""
    text = START_PARAMETER_PUBLIC_WARNING.lower()
    assert 'внутренн' in text, 'предупреждение перестало запрещать внутреннее название'
    assert 'бюджет' in text, 'предупреждение перестало запрещать бюджет — а он и утекал'


def _reads_constant(func) -> bool:
    """Ищем НАСТОЯЩЕЕ обращение к имени, разбирая код, а не текст.

    🔴 Две редакции подряд были зелёными по совпадению, и обе поймала мутация, а не чтение:
    сначала считались вхождения по всему файлу, потом — по исходнику функции. Оба раза
    имя, оставленное в КОММЕНТАРИИ («# TODO вернуть …»), засчитывалось как использование,
    то есть сторож пропускал ровно тот дефект, ради которого написан. Разбор кода
    комментариев не видит по построению.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(isinstance(node, ast.Name) and node.id == 'START_PARAMETER_PUBLIC_WARNING' for node in ast.walk(tree))


def test_both_forms_use_the_same_constant():
    """Форма создания и форма правки не должны разъехаться: у них один источник текста."""
    forms = {
        'создание кампании': campaigns.process_campaign_name,
        'правка метки': campaigns.start_edit_campaign_start_parameter,
    }
    missing = [label for label, func in forms.items() if not _reads_constant(func)]
    assert not missing, 'форма перестала показывать предупреждение: ' + ', '.join(missing)


def _reads(func, name: str) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(isinstance(node, ast.Name) and node.id == name for node in ast.walk(tree))


def test_change_warning_says_what_actually_breaks():
    """Метку выбирают один раз: сменил — старые рекламные ссылки перестают находить кампанию."""
    text = START_PARAMETER_CHANGE_WARNING.lower()
    assert 'ссылк' in text, 'предупреждение перестало называть то, что ломается'
    assert 'бонус' in text, 'предупреждение перестало говорить, что человек не получит бонус'
    assert 'статистик' in text or 'учёт' in text, 'предупреждение перестало говорить про потерю учёта'


def test_change_warning_only_on_the_edit_form():
    """⛔ На форме СОЗДАНИЯ этот текст был бы ложью: там ещё нечего ломать."""
    assert _reads(campaigns.start_edit_campaign_start_parameter, 'START_PARAMETER_CHANGE_WARNING'), (
        'форма правки перестала предупреждать, что смена метки ломает размещённые ссылки'
    )
    assert not _reads(campaigns.process_campaign_name, 'START_PARAMETER_CHANGE_WARNING'), (
        'предупреждение о СМЕНЕ метки уехало на форму создания, где оно неверно'
    )

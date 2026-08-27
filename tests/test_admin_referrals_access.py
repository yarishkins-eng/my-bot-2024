"""Сторож прав на админские кнопки рефералки.

🔴 Заведён после находки P0 (27.08.2026): две новые кнопки — включая ту, что необратимо
начисляет 2 349,25 ₽ живым людям и рассылает девять сообщений, — были зарегистрированы
БЕЗ `@admin_required`. Проверка прав в этом проекте живёт только в декораторе: ни одна
middleware админство не проверяет, она лишь кладёт флаг. Значит любой из ~300 пользователей
бота мог отправить нажатие напрямую и запустить выплату.

`ruff` такое не ловит и не может: правила «зарегистрированный админский callback обязан быть
под декоратором» в линтере нет. Этот сторож — единственное, что стоит между следующим таким
пропуском и боевым.
"""

import ast
from pathlib import Path


MODULE = Path('app/handlers/admin/referrals.py')


def _registered_handler_names(tree: ast.Module) -> set[str]:
    """Имена функций, отданных в `dp.callback_query.register(...)` / `dp.message.register(...)`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'register' or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            names.add(first.id)
    return names


def _decorators(node: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    found = set()
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name):
            found.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            found.add(dec.attr)
    return found


def test_every_registered_referral_handler_is_admin_gated():
    tree = ast.parse(MODULE.read_text(encoding='utf-8'))
    registered = _registered_handler_names(tree)
    assert registered, 'не нашли ни одного зарегистрированного обработчика — сторож ослеп'

    unguarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name not in registered:
            continue
        if 'admin_required' not in _decorators(node):
            unguarded.append(f'{node.name} (строка {node.lineno})')

    assert not unguarded, 'админские кнопки без проверки прав: ' + ', '.join(unguarded)


def test_debt_payment_button_is_admin_gated_by_name():
    """Отдельно и поимённо — три кнопки долга.

    Общий сторож выше можно случайно ослепить (например переименовав `register`).
    Эти три названы прямо, потому что за ними необратимые деньги.
    """
    tree = ast.parse(MODULE.read_text(encoding='utf-8'))
    wanted = {'show_referral_debt', 'ask_referral_debt_payment', 'pay_referral_debt_handler'}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in wanted:
            seen.add(node.name)
            decorators = _decorators(node)
            assert 'admin_required' in decorators, f'{node.name} без admin_required'
            assert 'error_handler' in decorators, f'{node.name} без error_handler'
    assert seen == wanted, f'не найдены обработчики: {wanted - seen}'

"""Регрессия: ответ из веб-админки хранит реального автора."""

import ast
from pathlib import Path


ADMIN_TICKETS = Path(__file__).resolve().parents[2] / 'app' / 'cabinet' / 'routes' / 'admin_tickets.py'


def test_cabinet_ticket_reply_stores_authenticated_admin_id() -> None:
    tree = ast.parse(ADMIN_TICKETS.read_text(encoding='utf-8'))
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == 'reply_to_ticket'
    )
    message_call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'TicketMessage'
    )
    user_id = next(keyword.value for keyword in message_call.keywords if keyword.arg == 'user_id')

    assert isinstance(user_id, ast.Attribute)
    assert isinstance(user_id.value, ast.Name)
    assert user_id.value.id == 'admin'
    assert user_id.attr == 'id'

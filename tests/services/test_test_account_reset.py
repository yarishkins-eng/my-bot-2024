"""Сторожа кнопки «Обнулить тестовый аккаунт».

Здесь только то, что проверяется без базы: список окружения и ПЛАН удаления,
который строится по метаданным SQLAlchemy. Поведение самих заборов и настоящий
порядок удаления проверяются на живом PostgreSQL —
``tests/integration/test_test_account_reset_postgres.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.database.models import Base
from app.services import user_service


def _plan_names() -> list[str]:
    scopes = {
        'users.id': [1],
        'subscriptions.id': [10],
        'subscription_checkouts.id': [20],
        'checkout_payment_attempts.id': [30],
        'subscription_entitlement_terms.id': [40],
    }
    return [table.name for table, _ in user_service._test_reset_delete_plan(scopes)]


def _owned_table_names() -> set[str]:
    """Таблицы, чья ссылка на пользователя означает «строка принадлежит ему»."""
    owned: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name not in user_service._TEST_RESET_OWNERSHIP_COLUMNS:
                continue
            if any(fk.column.table.name == 'users' and fk.column.name == 'id' for fk in column.foreign_keys):
                owned.add(table.name)
    return owned


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('', frozenset()),
        ('   ', frozenset()),
        ('не число', frozenset()),
        ('0,-5', frozenset()),
        ('7749231125', frozenset({7749231125})),
        (' 7749231125 , 7454290913 ', frozenset({7749231125, 7454290913})),
        ('7749231125,мусор', frozenset({7749231125})),
    ],
)
def test_allowlist_is_empty_by_default_and_drops_garbage(monkeypatch, raw, expected) -> None:
    """Пустой и испорченный список означают «нет тестовых аккаунтов», а не «все»."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', raw)
    assert user_service.test_account_telegram_ids() == expected


def test_stand_is_recognised_only_by_telegram_id(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231125)) is True
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231126)) is False
    # Внутренний номер строки не открывает дверь: список только про Телеграм.
    assert user_service.is_test_account(SimpleNamespace(telegram_id=None, id=7749231125)) is False


def test_allowlist_empty_closes_the_door_for_everyone(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', '')
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231125)) is False


def test_every_user_owned_table_is_either_wiped_or_named_explicitly() -> None:
    """Сторож, который ищет сам: новая таблица со ссылкой на пользователя
    обязана быть либо в плане удаления, либо в списке «не трогаем никогда».

    Рукописный перечень слепнет ровно на той таблице, которую добавят завтра;
    этот тест краснеет вместо того, чтобы молча оставить чужой хвост на стенде.
    """
    unclassified = _owned_table_names() - set(_plan_names()) - user_service._TEST_RESET_NEVER_WIPED
    assert unclassified == set(), (
        'У этих таблиц появилась ссылка «строка принадлежит пользователю», '
        'но обнуление стенда о них не знает: ' + ', '.join(sorted(unclassified))
    )


def test_never_wiped_tables_stay_out_of_the_plan() -> None:
    names = _plan_names()
    for table_name in user_service._TEST_RESET_NEVER_WIPED:
        assert table_name not in names, f'{table_name} обязана остаться нетронутой'
    # Саму строку пользователя обнуление не удаляет никогда: тот же Телеграм
    # должен зарегистрироваться заново, а не появиться новым номером.
    assert 'users' not in names


def test_plan_ignores_columns_that_only_point_at_a_person() -> None:
    """`created_by`, `admin_id`, `referred_by_id` — это ЧУЖИЕ строки."""
    names = set(_plan_names())
    for foreign in ('promocodes', 'broadcast_history', 'welcome_texts', 'news_articles', 'advertising_campaigns'):
        assert foreign not in names, f'{foreign} ссылается на автора, а не на владельца строки'


@pytest.mark.parametrize(
    ('child', 'parent'),
    [
        # Ровно эти четыре пары — причина, по которой очистка при /start падает
        # молча: база запрещает удалять родителя, пока жив ребёнок.
        ('subscription_entitlement_snapshots', 'subscriptions'),
        ('subscription_entitlement_terms', 'subscriptions'),
        ('subscription_entitlement_term_projection_outbox', 'subscription_entitlement_terms'),
        ('device_first_provider_events', 'checkout_payment_attempts'),
        ('checkout_payment_attempts', 'subscription_checkouts'),
        ('subscription_servers', 'subscriptions'),
        ('platega_payments', 'transactions'),
        ('referral_earnings', 'transactions'),
    ],
)
def test_plan_deletes_children_before_parents(child, parent) -> None:
    names = _plan_names()
    assert child in names and parent in names
    assert names.index(child) < names.index(parent), f'{child} обязана удаляться раньше {parent}'


def test_finished_and_settled_lists_are_positive_not_negative() -> None:
    """Незнакомое состояние обязано ЗАПИРАТЬ кнопку, а не проскакивать."""
    assert 'ready' in user_service._TEST_RESET_FINISHED_CHECKOUT_STATES
    for in_flight in ('awaiting_funds', 'fulfilling', 'operator_review', 'conflict'):
        assert in_flight not in user_service._TEST_RESET_FINISHED_CHECKOUT_STATES

    # `paid_processing` намеренно остаётся у успешной прямой продажи навсегда,
    # поэтому «в работе ли заказ» спрашивается у lifecycle_state заказа. Но
    # сама попытка в этом состоянии завершённой НЕ считается.
    for in_flight in ('creating', 'pending', 'paid_processing', 'reconciliation', 'operator_review'):
        assert in_flight not in user_service._TEST_RESET_SETTLED_ATTEMPT_STATUSES
    for in_flight in ('VERIFYING', 'OPERATOR_REVIEW', 'PENDING'):
        assert in_flight not in user_service._TEST_RESET_SETTLED_PROVIDER_STATUSES
    assert 'CONFIRMED' in user_service._TEST_RESET_SETTLED_PROVIDER_STATUSES

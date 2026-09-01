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
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', raw)
    assert user_service.test_account_telegram_ids() == expected


def test_stand_is_recognised_only_by_telegram_id(monkeypatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231125)) is True
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231126)) is False
    # Внутренний номер строки не открывает дверь: список только про Телеграм.
    assert user_service.is_test_account(SimpleNamespace(telegram_id=None, id=7749231125)) is False


def test_allowlist_is_not_editable_from_the_cabinet() -> None:
    """Список доступа к необратимому действию обязан жить только в окружении.

    Кабинет делает редактируемым КАЖДОЕ поле `Settings`, которого нет в наборе
    исключений, и применяет новое значение мгновенно, без рестарта. Поэтому у
    списка поля `Settings` нет вовсе — а строка в исключениях стоит второй
    линией, на случай если кто-то однажды заведёт поле.
    """
    from app.services.system_settings_service import BotConfigurationService

    assert not hasattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS'), (
        'У списка появилось поле Settings — теперь его можно править из кабинета. '
        'Либо уберите поле, либо убедитесь, что исключение реально работает.'
    )
    assert 'TEST_ACCOUNT_TELEGRAM_IDS' in BotConfigurationService.EXCLUDED_KEYS
    # Соседи по смыслу: если однажды уберут их, эта проверка тоже обязана упасть.
    assert {'ADMIN_IDS', 'ADMIN_EMAILS'} <= BotConfigurationService.EXCLUDED_KEYS


def test_allowlist_empty_closes_the_door_for_everyone(monkeypatch) -> None:
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '')
    assert user_service.is_test_account(SimpleNamespace(telegram_id=7749231125)) is False


# 🔴 Снимок того, что кнопка сносит СЕГОДНЯ. Он пришпилен руками намеренно.
#
# Прежний сторож сравнивал план с тем же признаком, по которому план и
# строится, — то есть не мог покраснеть в принципе, и завтрашняя таблица со
# ссылкой на пользователя молча уехала бы в снос. Здесь наоборот: любое
# изменение набора роняет тест и требует РЕШЕНИЯ ЧЕЛОВЕКА, а не тишины.
_PINNED_WIPED_TABLES = frozenset(
    {
        'advertising_campaign_registrations',
        'apple_iap_accounts',
        'apple_transactions',
        'cabinet_refresh_tokens',
        'checkout_payment_attempts',
        'cloudpayments_payments',
        'cryptobot_payments',
        'device_first_deposit_outbox',
        'device_first_mutations',
        'device_first_notification_outbox',
        'device_first_outbox',
        'device_first_provider_events',
        'discount_offers',
        'freekassa_payments',
        'heleket_payments',
        'kassa_ai_payments',
        'mulenpay_payments',
        'pal24_payments',
        'partner_applications',
        'platega_payments',
        'poll_responses',
        'promocode_uses',
        'referral_earnings',
        'saved_payment_methods',
        'sent_notifications',
        'subscription_checkouts',
        'subscription_conversions',
        'subscription_entitlement_snapshots',
        'subscription_entitlement_term_projection_outbox',
        'subscription_entitlement_terms',
        'subscription_events',
        'subscription_servers',
        'subscription_temporary_access',
        'subscriptions',
        'ticket_messages',
        'ticket_notifications',
        'tickets',
        'traffic_purchases',
        'transactions',
        'user_device_aliases',
        'user_promo_groups',
        'wata_payments',
        'withdrawal_requests',
        'yandex_client_id_map',
        'yookassa_payments',
    }
)


def test_wiped_tables_match_the_pinned_snapshot() -> None:
    actual = set(_plan_names())
    added = actual - _PINNED_WIPED_TABLES
    removed = _PINNED_WIPED_TABLES - actual
    assert not added, (
        'Кнопка обнуления стенда начала сносить НОВЫЕ таблицы: '
        + ', '.join(sorted(added))
        + '. Решите по каждой: это мусор стенда или чужие данные? '
        'Если мусор — впишите в снимок; если нет — в _TEST_RESET_NEVER_WIPED.'
    )
    assert not removed, 'Кнопка перестала сносить: ' + ', '.join(sorted(removed)) + '. Убедитесь, что это осознанно.'


def test_rows_the_schema_marks_as_outliving_the_person_are_left_alone() -> None:
    """`SET NULL` на ссылке = «строка переживает человека». Спорить нельзя."""
    names = set(_plan_names())
    for survivor in (
        'guest_purchases',  # оплаченный подарок ЖИВОГО покупателя
        'apple_iap_abuse_events',  # улики злоупотреблений
        'promo_offer_logs',
        'entitlement_identities',
        'button_click_logs',
    ):
        assert survivor not in names, f'{survivor} объявлена SET NULL — её удалять нельзя'


def test_other_peoples_money_rows_are_left_alone() -> None:
    """В `referral_earnings` строка принадлежит тому, кто ЗАРАБОТАЛ."""
    assert 'referral_id' not in user_service._TEST_RESET_OWNERSHIP_COLUMNS
    assert 'buyer_user_id' not in user_service._TEST_RESET_OWNERSHIP_COLUMNS


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


def test_settled_lists_follow_the_project_and_not_my_own_invention() -> None:
    """Наборы «законченного» берутся у проекта, а не пишутся рядом заново.

    Если проект заведёт новое состояние заказа или новый терминальный статус
    провайдера, этот тест покраснеет — и кто-то решит, деньги это или мусор.
    """
    from app.services.device_first_checkout_service import TERMINAL_STATES
    from app.services.device_first_payment_service import PROVIDER_TERMINAL_STATUSES

    # Заказ: терминальные состояния проекта МИНУС те, где деньги на разборе.
    assert user_service._TEST_RESET_FINISHED_CHECKOUT_STATES == (
        TERMINAL_STATES - user_service._TEST_RESET_MONEY_REVIEW_CHECKOUT_STATES
    )
    # Провайдер: терминальные статусы проекта ПЛЮС успех.
    assert PROVIDER_TERMINAL_STATUSES | {'CONFIRMED'} == user_service._TEST_RESET_SETTLED_PROVIDER_STATUSES


def test_a_successful_purchase_does_not_lock_the_button_forever() -> None:
    """`paid_processing` остаётся у УСПЕШНОЙ продажи навсегда.

    Считать его «деньгами в пути» значит запереть кнопку на любом стенде,
    где хоть раз прошла настоящая покупка, — то есть сломать инструмент ровно
    тогда, когда он впервые понадобился. Ловушка описана в самом проекте:
    `app/database/crud/tariff.py`.
    """
    assert 'paid_processing' in user_service._TEST_RESET_SETTLED_ATTEMPT_STATUSES
    # Брошенный счёт — тоже законченный: открыл оплату и закрыл вкладку.
    assert 'EXPIRED' in user_service._TEST_RESET_SETTLED_PROVIDER_STATUSES
    # Остановившийся заказ сносится обязательно: пока он жив, клиенту закрыт
    # пробный период.
    assert 'failed' in user_service._TEST_RESET_FINISHED_CHECKOUT_STATES
    assert 'reprice_required' in user_service._TEST_RESET_FINISHED_CHECKOUT_STATES


def test_unknown_state_locks_the_button_rather_than_slipping_through() -> None:
    """Списки положительные: чего в них нет — то запирает."""
    for in_flight in ('awaiting_funds', 'fulfilling', 'operator_review', 'conflict', 'draft'):
        assert in_flight not in user_service._TEST_RESET_FINISHED_CHECKOUT_STATES
    for in_flight in ('creating', 'pending', 'reconciliation', 'operator_review'):
        assert in_flight not in user_service._TEST_RESET_SETTLED_ATTEMPT_STATUSES
    for in_flight in ('VERIFYING', 'OPERATOR_REVIEW', 'PENDING', 'CHARGEBACKED'):
        assert in_flight not in user_service._TEST_RESET_SETTLED_PROVIDER_STATUSES


class _FakePanelApi:
    """Панель, которая ведёт себя как настоящая: 404 бросается, не возвращается."""

    def __init__(self, *, missing: set[str] | None = None, by_telegram: list[str] | None = None) -> None:
        self.missing = missing or set()
        self.by_telegram = by_telegram or []
        self.deleted: list[str] = []

    async def get_user_by_telegram_id(self, telegram_id):
        return [SimpleNamespace(uuid=value) for value in self.by_telegram]

    async def get_user_by_email(self, email):
        return []

    async def delete_user(self, uuid):
        from app.external.remnawave_api import RemnaWaveAPIError

        if uuid in self.missing:
            raise RemnaWaveAPIError('not found', 404)
        self.deleted.append(uuid)
        return True


def _install_fake_panel(monkeypatch, api: _FakePanelApi) -> None:
    from contextlib import asynccontextmanager

    from app.services import remnawave_service as remnawave_module

    @asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(remnawave_module.RemnaWaveService, 'get_api_client', _client, raising=False)


@pytest.mark.asyncio
async def test_panel_404_counts_as_success(monkeypatch) -> None:
    """Панель могли почистить руками — на стенде это норма.

    Считать 404 отказом значило бы запереть кнопку навсегда. Мутационный
    прогон показал, что эту ветку не исполнял ни один тест: все подменяли
    функцию целиком.
    """
    api = _FakePanelApi(missing={'gone-uuid'})
    _install_fake_panel(monkeypatch, api)

    ok = await user_service._test_reset_delete_panel_identity(
        SimpleNamespace(telegram_id=7749231125, email=None), ['gone-uuid']
    )

    assert ok is True
    assert api.deleted == []


@pytest.mark.asyncio
async def test_panel_identity_is_looked_up_by_telegram_too(monkeypatch) -> None:
    """POST мог дойти до панели, а его ответ потеряться — локального uuid нет."""
    api = _FakePanelApi(by_telegram=['orphan-uuid'])
    _install_fake_panel(monkeypatch, api)

    ok = await user_service._test_reset_delete_panel_identity(SimpleNamespace(telegram_id=7749231125, email=None), [])

    assert ok is True
    assert api.deleted == ['orphan-uuid']


@pytest.mark.asyncio
async def test_panel_error_other_than_404_is_a_refusal(monkeypatch) -> None:
    class _Broken(_FakePanelApi):
        async def delete_user(self, uuid):
            from app.external.remnawave_api import RemnaWaveAPIError

            raise RemnaWaveAPIError('boom', 500)

    _install_fake_panel(monkeypatch, _Broken())

    ok = await user_service._test_reset_delete_panel_identity(
        SimpleNamespace(telegram_id=7749231125, email=None), ['live-uuid']
    )

    assert ok is False


@pytest.mark.asyncio
async def test_route_refuses_an_account_outside_the_list(monkeypatch) -> None:
    """Забор №1 на самом маршруте: экран лишь не рисует кнопку, отбивает сервер."""
    from fastapi import HTTPException

    from app.cabinet.routes import admin_users as route_module
    from app.cabinet.schemas.users import TestAccountResetRequest

    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', '7749231125')

    async def _fake_get_user(db, user_id):
        return SimpleNamespace(id=user_id, telegram_id=555000111)

    monkeypatch.setattr(route_module, 'get_user_by_id', _fake_get_user)

    with pytest.raises(HTTPException) as exc:
        await route_module.reset_test_account_route(
            42,
            TestAccountResetRequest(confirm=True),
            admin=SimpleNamespace(id=1),
            db=object(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail['code'] == 'not_a_test_account'

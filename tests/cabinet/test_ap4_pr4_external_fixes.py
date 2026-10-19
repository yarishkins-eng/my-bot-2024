from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cabinet import ip_utils
from app.cabinet.routes import admin_auto_messages as api
from app.services import trial_ending_discount_service as rollout
from app.services.notification_settings_service import NotificationSettingsService as Settings
from app.services.permission_service import PermissionService
from tests.cabinet.test_ap4_pr4_patch import endpoint  # noqa: F401
from tests.cabinet.test_ap4_pr4_prices import price_env  # noqa: F401
from tests.cabinet.test_ap4_pr4_stats import db  # noqa: F401


@pytest.mark.asyncio
async def test_stands_sql_matches_db_override_env_and_missing_telegram(db, monkeypatch):
    monkeypatch.setattr(rollout, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'stands')
    people = [
        {'test': True, 'username': 'test_one'},
        {'test': False, 'telegram': 777},
        {'test': None, 'telegram': 777, 'first_name': 'Test name'},
        {'test': True, 'telegram': None},
        {'test': True, 'telegram': 0},
        {'test': False},
        {'test': True, 'status': 'deleted'},
    ]
    for user_id, person in enumerate(people, 1):
        db.user(user_id, **person)
        db.offer(user_id)
        db.order(user_id)
    count, note, history = await api._trial_discount_stats(db, may_see_names=True)
    assert count == 2
    assert note == 'Тестовые аккаунты: получили 2 · купили со скидкой 2 · заплатили 238 ₽ · скидок на 60 ₽.'
    assert {row.user_ref for row in history} == {'@test_one', 'Test name'}
    assert all(row.claimed is None for row in history)
    _, _, masked = await api._trial_discount_stats(db)
    assert {row.user_ref for row in masked} == {'Клиент #1', 'Клиент #3'}


@pytest.mark.asyncio
@pytest.mark.parametrize('tariff', [4, 5], ids=['free', 'trial'])
async def test_free_and_trial_orders_never_enter_discount_sales(db, tariff):
    db.user()
    db.order(tariff=tariff)
    count, note, history = await api._trial_discount_stats(db)
    assert (count, note, history) == (0, 'Клиенты скидку ещё не получали.', [])


@pytest.mark.asyncio
async def test_zero_states_and_audience_control_the_exact_comparison(db, monkeypatch):
    monkeypatch.setattr(rollout, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'stands')
    assert (await api._trial_discount_stats(db))[1] == 'Тестовые аккаунты: получили 0 · купили со скидкой 0.'
    monkeypatch.setattr(rollout, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'all')
    db.user()
    db.offer()
    note = (await api._trial_discount_stats(db))[1]
    assert note.endswith('Раньше из включавших VPN платили около 27 из 100 — пока данных мало, сравнивать рано.')
    assert 'заплатили' not in note and 'скидок на' not in note
    monkeypatch.setattr(Settings, 'get_trial_ending_discount_audience', classmethod(lambda _: 0))
    assert (await api._trial_discount_stats(db))[1].endswith(': получили 1 · купили со скидкой 0.')
    db.order()
    assert 'заплатили 119 ₽ · скидок на 30 ₽.' in (await api._trial_discount_stats(db))[1]
    monkeypatch.setattr(Settings, 'get_trial_ending_discount_stats_since', classmethod(lambda _: None))
    monkeypatch.setattr(Settings, 'is_trial_ending_discount_enabled', classmethod(lambda _: False))
    assert (await api._trial_discount_stats(db))[1] == 'Итог появится после первого включения.'


def test_master_switch_gates_the_new_card_and_rollout_note_comes_from_constant(price_env, monkeypatch):
    Settings.set_trial_ending_discount_enabled(True)
    entry = api.CATALOG_BY_ID['trial-ending-discount']
    monkeypatch.setattr(Settings, 'are_notifications_globally_enabled', classmethod(lambda _: False))
    assert api._state_of(entry, {}, {}) == ('quiet', 'уведомления выключены в настройках бота', None)
    monkeypatch.setattr(Settings, 'are_notifications_globally_enabled', classmethod(lambda _: True))
    assert api._state_of(entry, {}, {}) == ('live', None, None)
    monkeypatch.setattr(rollout, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'stands')
    assert api._state_of(entry, {}, {}) == ('live', None, 'Пока только тестовым аккаунтам')
    monkeypatch.setattr(rollout, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'all')
    assert api._state_of(entry, {}, {}) == ('live', None, None)
    assert api._discount_context()['rollout_note'] is None


@pytest.mark.parametrize('carrier,card', [(0, 'trial-2h'), (24, 'trial-1d'), (48, 'trial-2d')])
def test_saved_carrier_name_and_its_disabled_reason_are_plain_words(price_env, carrier, card):
    Settings.set_trial_ending_discount_carrier(carrier)
    entry = api.CATALOG_BY_ID['trial-ending-discount']
    assert api._resolve_when(entry, {}) == f'Приходит в письме «{api.CATALOG_BY_ID[card]["title"]}».'
    Settings.set_trial_ending_discount_enabled(True)
    Settings._data[api.CATALOG_BY_ID[card]['settings_key']]['enabled'] = False
    assert api._state_of(entry, {}, {}) == ('quiet', f'выключено письмо «{api.CATALOG_BY_ID[card]["title"]}»', None)


@pytest.mark.asyncio
async def test_history_permission_reuses_users_read_and_passes_the_client_ip(monkeypatch):
    viewer, request, database = SimpleNamespace(id=1), object(), AsyncMock()
    monkeypatch.setattr(ip_utils, 'get_client_ip', Mock(return_value='203.0.113.42'))
    allowed = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(PermissionService, 'check_permission', allowed)
    assert await api._may_see_names(database, viewer, request) is True
    allowed.assert_awaited_once_with(database, viewer, 'users:read', ip_address='203.0.113.42')
    allowed.return_value = (False, 'outside allowed network')
    assert await api._may_see_names(database, viewer, request) is False


def test_real_fastapi_list_survives_cashier_failure_and_patch_is_atomic(endpoint, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_cabinet_db] = lambda: endpoint
    for route in api.router.routes:
        for dependency in route.dependant.dependencies:
            if dependency.call != api.get_cabinet_db:
                app.dependency_overrides[dependency.call] = lambda: SimpleNamespace(id=1)
    monkeypatch.setattr(api, '_may_see_names', AsyncMock(return_value=False))
    original = api._discount_examples
    monkeypatch.setattr(api, '_discount_examples', AsyncMock(side_effect=RuntimeError('cashier unavailable')))
    warning = Mock()
    monkeypatch.setattr(api.logger, 'warning', warning)
    with TestClient(app) as client:
        response = client.get('/admin/auto-messages')
        assert response.status_code == 200
        card = next(row for row in response.json()['items'] if row['id'] == 'trial-ending-discount')
        assert card['state'] == 'quiet' and card['quiet_reason'] == 'касса не назвала цену месяца'
        warning.assert_called_once_with('auto_message_discount_price_unavailable', exc_info=True)
        monkeypatch.setattr(api, '_discount_examples', original)
        response = client.patch('/admin/auto-messages/trial-ending-discount', json={'enabled': True, 'audience': 0})
        assert response.status_code == 200 and response.json()['params']['audience'] == 0
        assert response.json()['enabled'] is True
        before = Settings._storage_path.read_bytes()
        for percent in [-1, 0, 31, 100, 101]:
            response = client.patch('/admin/auto-messages/trial-ending-discount', json={'discount_percent': percent})
            assert response.status_code == 422 and response.json()['detail'] == 'Размер скидки: от 1 до 30%.'
        assert Settings._storage_path.read_bytes() == before
        for percent in [-1, 0, 51, 101]:
            response = client.patch('/admin/auto-messages/trial-discount', json={'discount_percent': percent})
            assert response.status_code == 422
        assert Settings._storage_path.read_bytes() == before
        monkeypatch.setattr(api, '_history_for', AsyncMock(return_value=[]))
        assert client.patch('/admin/auto-messages/trial-discount', json={'discount_percent': 50}).status_code == 200
        for percent in [True, '20', 20.5]:
            assert (
                client.patch(
                    '/admin/auto-messages/trial-ending-discount', json={'discount_percent': percent}
                ).status_code
                == 422
            )

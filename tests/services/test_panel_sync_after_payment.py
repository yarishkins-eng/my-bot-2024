"""Этап 4.0: оплата обязана дойти до панели, а сбой панели — не выдавать «готово».

Старый файл-доказательство `tests/entitlement_authority/test_phase0_evidence.py` целиком
снимается модульным skip (нет `ENTITLEMENT_AUTHORITY_TEST_DATABASE_URL`), поэтому зелёный
прогон там ничего не значит. Эти тесты исполняются в обычном `pytest`, то есть в `lint`.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import subscription_service as subscription_service_module
from app.services.device_first_checkout_service import process_direct_provisioning_outbox
from app.services.subscription_service import SubscriptionService, find_panel_echo_mismatch


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows


@asynccontextmanager
async def _api_context(api):
    yield api


def _user():
    return SimpleNamespace(id=7, remnawave_uuid='panel-existing', telegram_id=555, language='ru')


def _provisioned_subscription():
    """Подписка, пришедшая с триала: ссылка и UUID у неё УЖЕ есть."""
    return SimpleNamespace(
        id=11,
        user_id=7,
        remnawave_uuid='panel-existing',
        subscription_url='https://redacted.invalid/sub',
        connected_squads=['squad-a', 'squad-b'],
    )


def _service_with_panel(api, *, update_result=None):
    service = SubscriptionService()
    service.get_api_client = lambda: _api_context(api)  # type: ignore[method-assign]
    service.update_remnawave_user = AsyncMock(return_value=update_result)
    service.create_remnawave_user = AsyncMock(return_value=SimpleNamespace(uuid='panel-new'))
    return service


# --- (а) выдача доходит до панели, даже когда ссылка и UUID уже есть -------------------


@pytest.mark.asyncio
async def test_forced_sync_writes_to_panel_even_when_url_and_uuid_exist(monkeypatch):
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(uuid='panel-existing')))
    service = _service_with_panel(api, update_result=SimpleNamespace(uuid='panel-existing'))
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=_user()))

    ready, error = await service.ensure_subscription_synced(
        AsyncMock(), _provisioned_subscription(), force_panel_sync=True
    )

    assert (ready, error) == (True, None)
    service.update_remnawave_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_unforced_sync_keeps_legacy_screen_behaviour(monkeypatch):
    """Экран «Моя подписка» в панель не пишет — поведение по умолчанию не тронуто."""
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(uuid='panel-existing')))
    service = _service_with_panel(api)
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=_user()))

    ready, error = await service.ensure_subscription_synced(AsyncMock(), _provisioned_subscription())

    assert (ready, error) == (True, None)
    service.update_remnawave_user.assert_not_awaited()


# --- (б) сбой панели даёт повтор, а не «готово» ----------------------------------------


@pytest.mark.asyncio
async def test_panel_timeout_never_resets_uuid_and_reports_failure(monkeypatch):
    """Таймаут панели не должен осиротить живой профиль пересозданием пользователя."""
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(side_effect=TimeoutError('redacted')))
    service = _service_with_panel(api, update_result=None)
    user = _user()
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=user))
    subscription = _provisioned_subscription()

    ready, error = await service.ensure_subscription_synced(AsyncMock(), subscription, force_panel_sync=True)

    assert (ready, error) == (False, 'panel_unavailable')
    service.create_remnawave_user.assert_not_awaited()
    assert user.remnawave_uuid == 'panel-existing'
    assert subscription.remnawave_uuid == 'panel-existing'


@pytest.mark.asyncio
async def test_panel_error_with_live_profile_reports_failure_instead_of_recreating(monkeypatch):
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(uuid='panel-existing')))
    service = _service_with_panel(api, update_result=None)
    user = _user()
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=user))

    ready, error = await service.ensure_subscription_synced(
        AsyncMock(), _provisioned_subscription(), force_panel_sync=True
    )

    assert (ready, error) == (False, 'panel_update_failed')
    service.create_remnawave_user.assert_not_awaited()
    assert user.remnawave_uuid == 'panel-existing'


@pytest.mark.asyncio
async def test_panel_confirmed_absence_still_recreates_user(monkeypatch):
    """Настоящее удаление из панели (404) по-прежнему лечится пересозданием."""
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=None))
    service = _service_with_panel(api, update_result=None)
    user = _user()
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=user))

    ready, error = await service.ensure_subscription_synced(
        AsyncMock(), _provisioned_subscription(), force_panel_sync=True
    )

    assert (ready, error) == (True, None)
    service.create_remnawave_user.assert_awaited_once()
    assert user.remnawave_uuid is None


@pytest.mark.asyncio
async def test_subscription_without_servers_is_diagnosed_not_retried(monkeypatch):
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(uuid='panel-existing')))
    service = _service_with_panel(api)
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=_user()))
    subscription = _provisioned_subscription()
    subscription.connected_squads = []

    ready, error = await service.ensure_subscription_synced(AsyncMock(), subscription, force_panel_sync=True)

    assert (ready, error) == (False, 'no_entitlements_to_provision')
    service.update_remnawave_user.assert_not_awaited()


# --- сверка эха панели: только фактически отправленные поля ----------------------------


def test_echo_check_ignores_fields_that_were_never_sent():
    """Пустые сквады и None-лимит устройств не уходят в запрос — требовать их нельзя."""
    sent = {'uuid': 'panel-existing'}
    panel_user = SimpleNamespace(expire_at=None, hwid_device_limit=None, active_internal_squads=[])

    assert find_panel_echo_mismatch(sent, panel_user) is None


def test_echo_check_accepts_panel_rounding_of_expiry():
    expected = datetime.now(UTC) + timedelta(days=30)
    sent = {'expire_at': expected, 'hwid_device_limit': 3, 'active_internal_squads': ['squad-a']}
    panel_user = SimpleNamespace(
        expire_at=expected + timedelta(seconds=5),
        hwid_device_limit=3,
        active_internal_squads=[{'uuid': 'squad-a'}],
    )

    assert find_panel_echo_mismatch(sent, panel_user) is None


@pytest.mark.parametrize(
    ('panel_user', 'expected_field'),
    [
        (
            SimpleNamespace(
                expire_at=datetime(2020, 1, 1, tzinfo=UTC),
                hwid_device_limit=3,
                active_internal_squads=[{'uuid': 'squad-a'}],
            ),
            'expire_at',
        ),
        (
            SimpleNamespace(expire_at=None, hwid_device_limit=1, active_internal_squads=[{'uuid': 'squad-a'}]),
            'hwid_device_limit',
        ),
        (
            SimpleNamespace(expire_at=None, hwid_device_limit=3, active_internal_squads=[]),
            'active_internal_squads',
        ),
    ],
)
def test_echo_check_catches_field_the_panel_did_not_apply(panel_user, expected_field):
    sent = {
        'expire_at': panel_user.expire_at if expected_field != 'expire_at' else datetime(2030, 1, 1, tzinfo=UTC),
        'hwid_device_limit': 3,
        'active_internal_squads': ['squad-a'],
    }

    assert find_panel_echo_mismatch(sent, panel_user) == expected_field


# --- воркер выдачи: форс включён, отказ не превращается в «готово» ---------------------


def _worker_db(checkout, subscription, claimed):
    return SimpleNamespace(
        # Четвёртый ответ нужен только успешному пути: он ищет строку уведомления.
        execute=AsyncMock(side_effect=[Result([claimed]), Result(checkout), Result(claimed), Result(None)]),
        get=AsyncMock(side_effect=[claimed, subscription, checkout]),
        commit=AsyncMock(),
        add=MagicMock(),
    )


def _worker_rows():
    claimed = SimpleNamespace(
        id=71,
        checkout_id=9,
        payload_json={'subscription_id': 13},
        status='pending',
        attempts=0,
        lease_token=None,
        lease_expires_at=None,
        lease_epoch=0,
        available_at=None,
        last_error=None,
    )
    checkout = SimpleNamespace(
        id=9, user_id=7, public_id='chk-9', provisioning_state='pending', lifecycle_state='fulfilling'
    )
    return claimed, checkout, _provisioned_subscription()


@pytest.mark.asyncio
async def test_direct_provisioning_worker_forces_the_panel_write():
    claimed, checkout, subscription = _worker_rows()
    db = _worker_db(checkout, subscription, claimed)
    synced = AsyncMock(return_value=(True, None))

    with (
        patch(
            'app.services.device_first_checkout_service._access_point_checkout_projection_delivered',
            AsyncMock(return_value=None),
        ),
        patch('app.services.subscription_service.SubscriptionService.ensure_subscription_synced', synced),
    ):
        await process_direct_provisioning_outbox(db, checkout_id=checkout.id, limit=1)

    assert synced.await_args.kwargs['force_panel_sync'] is True


@pytest.mark.asyncio
async def test_panel_refusal_leaves_order_in_retry_and_sends_no_ready_message():
    claimed, checkout, subscription = _worker_rows()
    db = _worker_db(checkout, subscription, claimed)

    with (
        patch(
            'app.services.device_first_checkout_service._access_point_checkout_projection_delivered',
            AsyncMock(return_value=None),
        ),
        patch(
            'app.services.subscription_service.SubscriptionService.ensure_subscription_synced',
            AsyncMock(return_value=(False, 'panel_unavailable')),
        ),
    ):
        processed = await process_direct_provisioning_outbox(db, checkout_id=checkout.id, limit=1)

    assert processed == 0
    assert claimed.status == 'retry'
    assert checkout.provisioning_state == 'retry'
    assert checkout.lifecycle_state != 'ready'
    db.add.assert_not_called()  # уведомления «✅ Подписка готова» быть не должно


@pytest.mark.asyncio
async def test_order_without_servers_goes_to_operator_instead_of_endless_retry():
    claimed, checkout, subscription = _worker_rows()
    db = _worker_db(checkout, subscription, claimed)

    with (
        patch(
            'app.services.device_first_checkout_service._access_point_checkout_projection_delivered',
            AsyncMock(return_value=None),
        ),
        patch(
            'app.services.subscription_service.SubscriptionService.ensure_subscription_synced',
            AsyncMock(return_value=(False, 'no_entitlements_to_provision')),
        ),
    ):
        processed = await process_direct_provisioning_outbox(db, checkout_id=checkout.id, limit=1)

    assert processed == 0
    assert claimed.status == 'operator_review'  # тот же словарь, что в post-paid reversal
    assert checkout.lifecycle_state == 'operator_review'
    assert checkout.terminal_reason == 'no_entitlements_to_provision'
    db.add.assert_not_called()

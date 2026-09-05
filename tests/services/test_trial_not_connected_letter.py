"""Сторожа письма «пробный идёт, а VPN не подключён».

Проверяются не тексты, а свойства, которые нельзя потерять:
запрос отбирает по пробному тарифу (иначе письмо уедет друзьям на Team),
молчание панели не превращается в «не подключился», и подписке без серверов
не предлагают подключиться.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_service as module
from app.services.monitoring_service import MonitoringService

TRIAL_TARIFF_ID = 5
TEAM_TARIFF_ID = 4


def _service():
    service = MonitoringService.__new__(MonitoringService)
    service.bot = SimpleNamespace(send_message=AsyncMock())
    service._send_trial_not_connected_notification = AsyncMock(return_value=True)
    return service


def _subscription(*, sub_id=217, uuid='panel-uuid-217', squads=('de', 'nl'), user_uuid=None):
    # Числа и uuid намеренно НЕ совпадают ни с одним умолчанием соседнего кода:
    # совпавшая фикстура делает сторож проверкой совпадения, а не защиты.
    user = SimpleNamespace(id=901, telegram_id=5207068834, language='ru', remnawave_uuid=user_uuid)
    return SimpleNamespace(
        id=sub_id,
        user=user,
        remnawave_uuid=uuid,
        connected_squads=list(squads),
        start_date=datetime.now(UTC) - timedelta(hours=44),
        end_date=datetime.now(UTC) + timedelta(hours=28),
    )


def _db_returning(subscriptions):
    captured = {}

    async def execute(statement):
        captured['statement'] = statement
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(subscriptions)))

    return SimpleNamespace(execute=AsyncMock(side_effect=execute)), captured


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(module.NotificationSettingsService, 'is_enabled', staticmethod(lambda key: True))
    monkeypatch.setattr(
        module,
        'get_trial_tariff',
        AsyncMock(return_value=SimpleNamespace(id=TRIAL_TARIFF_ID, name='⏰Пробный')),
    )
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=False))
    recorded = AsyncMock()
    monkeypatch.setattr(module, 'record_notification', recorded)
    return recorded


@pytest.mark.asyncio
async def test_letter_goes_to_a_trial_user_who_never_connected(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value={'someone-else'})
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()
    wired.assert_awaited_once()
    assert wired.await_args[0][3] == 'trial_not_connected'


@pytest.mark.asyncio
async def test_query_filters_by_the_trial_tariff_so_team_friends_are_excluded(wired) -> None:
    """Team несёт is_trial=True и живёт годами — отбор обязан идти по тарифу."""
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=set())
    db, captured = _db_returning([])

    await service._check_trial_not_connected(db)

    sql = str(captured['statement'])
    assert 'subscriptions.tariff_id' in sql, 'отбор перестал ограничиваться тарифом — письмо уедет на Team'
    assert 'subscriptions.start_date <=' in sql, 'пропала нижняя граница возраста пробного'


@pytest.mark.asyncio
async def test_silent_panel_never_becomes_not_connected(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=None)
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_panel_answer_is_not_the_same_as_silence(wired) -> None:
    """Пустое множество значит «никто не подключался» — письмо уходит."""
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=set())
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscription_without_servers_is_never_asked_to_connect(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=set())
    db, _ = _db_returning([_subscription(squads=())])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_connected_user_gets_nothing(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value={'panel-uuid-217'})
    db, _ = _db_returning([_subscription(uuid='panel-uuid-217')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_is_recognised_by_the_user_uuid_too(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value={'user-level-uuid'})
    db, _ = _db_returning([_subscription(uuid=None, user_uuid='user-level-uuid')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_written_user_is_not_written_twice(wired, monkeypatch) -> None:
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=True))
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=set())
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_off_stops_the_check_before_any_query(monkeypatch) -> None:
    monkeypatch.setattr(module.NotificationSettingsService, 'is_enabled', staticmethod(lambda key: False))
    service = _service()
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_is_off_by_default() -> None:
    """Появление кода не должно начать рассылку само."""
    from app.services.notification_settings_service import NotificationSettingsService

    assert NotificationSettingsService._DEFAULTS['trial_not_connected']['enabled'] is False


@pytest.mark.asyncio
async def test_letter_says_the_connection_is_missing_not_the_service(monkeypatch) -> None:
    """Владелец отклонил формулировку про нашу услугу — письмо про подключение клиента."""
    service = MonitoringService.__new__(MonitoringService)
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)

    service._send_message_with_logo = fake_send
    user = SimpleNamespace(telegram_id=5207068834, language='ru')

    result = await service._send_trial_not_connected_notification(user, _subscription())

    assert result is True
    text = sent['text']
    assert 'не заработал' not in text, 'текст снова читается как жалоба на нашу услугу'
    assert 'не подключён' in text
    callbacks = [button.callback_data for row in sent['reply_markup'].inline_keyboard for button in row]
    assert callbacks == ['subscription_connect', 'menu_support']

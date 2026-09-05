"""Сторожа письма «пробный идёт, а VPN не подключён».

Проверяются не тексты, а свойства, которые нельзя потерять:
запрос отбирает по пробному тарифу (иначе письмо уедет друзьям на Team),
молчание панели не превращается в «не подключился», и подписке без серверов
не предлагают подключиться.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
    # 🔴 Смотреть надо ИМЕННО в условие: `tariff_id` встречается и в списке выбираемых
    # колонок, поэтому проверка по всему тексту запроса ловила бы совпадение, а не защиту.
    assert 'WHERE' in sql, 'у запроса пропало условие целиком'
    where = sql.split('WHERE', 1)[1]
    assert 'tariff_id' in where, 'отбор перестал ограничиваться пробным тарифом — письмо уедет на Team'
    assert 'start_date <=' in where, 'пропала нижняя граница возраста пробного'
    # Письмо не должно приходить тому, у кого пробный вот-вот кончится: предлагать
    # ставить приложение человеку, у которого остался час, бессмысленно.
    assert 'end_date >' in where, 'пропал запас времени до конца пробного'


@pytest.mark.asyncio
async def test_silent_panel_never_becomes_not_connected(wired, monkeypatch) -> None:
    fake_logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock(), debug=Mock())
    monkeypatch.setattr(module, 'logger', fake_logger)
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value=None)
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()
    # 🔴 Улика того, что сработал именно ВЫХОД, а не падение внутри цикла. Без неё сторож
    # зелен и когда выход снят: код спотыкается о None, общий except глотает исключение,
    # и «письма нет» становится неотличимо от «письма нет по правильной причине».
    fake_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_empty_panel_answer_is_not_the_same_as_silence(wired) -> None:
    """Панель ответила про людей, но подключившихся среди них нет — письмо уходит.

    Не путать с ответом «ноль пользователей вообще»: тот читается как сбой чтения
    и до сюда не доходит, его отбивает сама _fetch_connected_panel_uuids.
    """
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


class _FakeApi:
    def __init__(self, users=None, error=None):
        self._users = users or []
        self._error = error

    async def get_all_users_stream(self, size=500):
        if self._error:
            raise self._error
        return self._users


class _FakeClient:
    def __init__(self, api):
        self._api = api

    async def __aenter__(self):
        return self._api

    async def __aexit__(self, *exc):
        return False


def _service_with_panel(api, *, configured=True):
    service = MonitoringService.__new__(MonitoringService)
    service.subscription_service = SimpleNamespace(
        is_configured=configured,
        get_api_client=lambda: _FakeClient(api),
    )
    return service


@pytest.mark.asyncio
async def test_panel_read_returns_none_when_the_panel_raises() -> None:
    """Сбой панели обязан быть отличим от ответа «никто не подключался»."""
    service = _service_with_panel(_FakeApi(error=RuntimeError('панель недоступна')))

    assert await service._fetch_connected_panel_uuids() is None


@pytest.mark.asyncio
async def test_panel_read_returns_none_when_panel_is_not_configured() -> None:
    service = _service_with_panel(_FakeApi(), configured=False)

    assert await service._fetch_connected_panel_uuids() is None


@pytest.mark.asyncio
async def test_panel_read_returns_only_those_who_actually_connected() -> None:
    api = _FakeApi(
        users=[
            SimpleNamespace(uuid='connected-one', first_connected_at=datetime.now(UTC)),
            SimpleNamespace(uuid='never-connected', first_connected_at=None),
        ]
    )
    service = _service_with_panel(api)

    assert await service._fetch_connected_panel_uuids() == {'connected-one'}


@pytest.mark.asyncio
async def test_subscription_without_a_panel_account_is_left_alone(wired) -> None:
    """Сравнивать не с чем — значит «не знаем», а не «не подключался»."""
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value={'somebody'})
    db, _ = _db_returning([_subscription(uuid=None, user_uuid=None)])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_uuid_string_counts_as_no_panel_account(wired) -> None:
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock(return_value={'somebody'})
    db, _ = _db_returning([_subscription(uuid='', user_uuid='')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_returning_zero_users_is_a_failure_not_a_fact() -> None:
    """Ноль пользователей в панели при живых пробных — сбой чтения, а не ответ."""
    service = _service_with_panel(_FakeApi(users=[]))

    assert await service._fetch_connected_panel_uuids() is None


@pytest.mark.asyncio
async def test_panel_answering_without_connection_data_is_a_failure_too() -> None:
    """Люди пришли, а даты подключения нет ни у кого — подпись расхождения схемы.

    Это опаснее пустого ответа: письмо ушло бы всем подряд, включая тех, у кого
    VPN работает. Читается как «не знаем».
    """
    api = _FakeApi(
        users=[
            SimpleNamespace(uuid='a', first_connected_at=None),
            SimpleNamespace(uuid='b', first_connected_at=None),
        ]
    )
    service = _service_with_panel(api)

    assert await service._fetch_connected_panel_uuids() is None


@pytest.mark.asyncio
async def test_panel_is_not_touched_when_everyone_was_already_written(wired, monkeypatch) -> None:
    """Обход всей панели стоит дорого и стоит в цикле перед сверкой платежей."""
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=True))
    service = _service()
    service._fetch_connected_panel_uuids = AsyncMock()
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._fetch_connected_panel_uuids.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_button_points_at_the_connection_screen(monkeypatch) -> None:
    """Без явного адреса кабинет уводит на Главную, где под нужной кнопкой стоит апселл."""
    calls = []

    def spy(text, **kwargs):
        calls.append({'text': text, **kwargs})
        return SimpleNamespace(callback_data=kwargs.get('callback_data'))

    monkeypatch.setattr(module, 'build_miniapp_or_callback_button', spy)
    service = MonitoringService.__new__(MonitoringService)

    async def fake_send(**kwargs):
        return None

    service._send_message_with_logo = fake_send
    await service._send_trial_not_connected_notification(
        SimpleNamespace(telegram_id=5207068834, language='ru'), _subscription()
    )

    connect = next(call for call in calls if call['callback_data'] == 'subscription_connect')
    assert connect.get('cabinet_path') == '/connection', 'кнопка снова уводит на Главную с апселлом'


def test_the_setting_actually_reaches_the_query() -> None:
    """Настройка обязана доехать до запроса, а не остаться украшением экрана.

    Фейковая сессия в тестах не исполняет WHERE, поэтому «число применилось» ею
    не доказывается. Сторожим по исходнику — тем же приёмом, что у соседнего письма.
    """
    import inspect

    source = inspect.getsource(module.MonitoringService._check_trial_not_connected)
    assert 'get_trial_not_connected_after_hours()' in source, 'число часов снова зашито в код'
    assert 'timedelta(hours=after_hours)' in source, 'настройка не доходит до условия отбора'


def test_ceiling_leaves_a_working_window() -> None:
    """Потолок настройки не должен убивать сообщение.

    Окно кандидатности = длина пробного − запас до конца − N. При N, равном длине
    пробного минус запас, окно схлопывается в точку, и письмо не уходит НИКОМУ,
    а экран показывает «работает». Разрыв держим не меньше двух часов: часовой
    обход плюс его собственная длительность.
    """
    from app.config import settings as app_settings
    from app.services.notification_settings_service import NotificationSettingsService

    trial_hours = app_settings.TRIAL_DURATION_DAYS * 24
    window = (
        trial_hours - module.TRIAL_NOT_CONNECTED_MIN_HOURS_LEFT - NotificationSettingsService.MAX_NOT_CONNECTED_HOURS
    )
    assert window >= 2, f'при потолке {NotificationSettingsService.MAX_NOT_CONNECTED_HOURS} окно осталось {window} ч'
    assert NotificationSettingsService.MIN_NOT_CONNECTED_HOURS >= 1, 'ноль часов — письмо в момент активации'

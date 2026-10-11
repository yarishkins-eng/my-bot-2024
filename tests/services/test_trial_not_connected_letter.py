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
    service._log_monitoring_event = AsyncMock()
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
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()
    wired.assert_awaited_once()
    assert wired.await_args[0][3] == 'trial_not_connected'


@pytest.mark.asyncio
async def test_query_filters_by_the_trial_tariff_so_team_friends_are_excluded(wired) -> None:
    """Team несёт is_trial=True и живёт годами — отбор обязан идти по тарифу."""
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
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
    service._fetch_trial_panel_usage = AsyncMock(return_value=None)
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()
    # 🔴 Улика того, что сработал именно ВЫХОД, а не падение внутри цикла. Без неё сторож
    # зелен и когда выход снят: код спотыкается о None, общий except глотает исключение,
    # и «письма нет» становится неотличимо от «письма нет по правильной причине».
    fake_logger.error.assert_not_called()


@pytest.mark.asyncio
async def test_known_zero_usage_is_not_the_same_as_missing_data(wired) -> None:
    """АП-2а: прежнее пустое множество говорило только об отсутствии первой даты.

    Теперь письмо разрешает подтверждённый нулевой расход именно нужного профиля.
    Пустая карта означает отсутствие достоверного расхода и письма не разрешает.
    """
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscription_without_servers_is_never_asked_to_connect(wired) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
    db, _ = _db_returning([_subscription(squads=())])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_connected_user_gets_nothing(wired) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 1_000_000})
    db, _ = _db_returning([_subscription(uuid='panel-uuid-217')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_is_recognised_by_the_user_uuid_too(wired) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'user-level-uuid': 1_000_000})
    db, _ = _db_returning([_subscription(uuid=None, user_uuid='user-level-uuid')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_written_user_is_not_written_twice(wired, monkeypatch) -> None:
    monkeypatch.setattr(module, 'notification_sent', AsyncMock(return_value=True))
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
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
async def test_panel_read_uses_the_callers_own_client_when_given() -> None:
    """ВК-0: утреннее письмо передаёт СВОЙ клиент панели — клиент монитора общий, и два одновременных входа в
    него закрывают друг другу сессию. Без своего клиента читатель по-прежнему ходит клиентом монитора."""
    own_api = _FakeApi(users=[SimpleNamespace(uuid='from-own-client', first_connected_at=datetime.now(UTC))])
    service = _service_with_panel(_FakeApi(error=RuntimeError('клиент монитора')))
    own = SimpleNamespace(is_configured=True, get_api_client=lambda: _FakeClient(own_api))

    assert await service._fetch_connected_panel_uuids(own) == {'from-own-client'}
    assert await service._fetch_connected_panel_uuids() is None


@pytest.mark.asyncio
async def test_panel_read_never_touches_the_client_when_not_configured() -> None:
    """Панель не настроена — клиент не создаётся вовсе (а не «сходили и получили пусто»)."""
    calls = []

    def get_api_client():
        calls.append(1)
        return _FakeClient(_FakeApi(users=[]))

    service = MonitoringService.__new__(MonitoringService)
    service.subscription_service = SimpleNamespace(is_configured=False, get_api_client=get_api_client)

    assert await service._fetch_connected_panel_uuids() is None
    assert calls == []


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
    service._fetch_trial_panel_usage = AsyncMock(return_value={'somebody': 0})
    db, _ = _db_returning([_subscription(uuid=None, user_uuid=None)])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_uuid_string_counts_as_no_panel_account(wired) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'somebody': 0})
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
    service._fetch_trial_panel_usage = AsyncMock()
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._fetch_trial_panel_usage.assert_not_awaited()


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


# АП-2а: письмо теперь судит по накопленному расходу, а дата первого подключения
# остаётся прежним контрактом утреннего отчёта. Старые helper-тесты выше сохранены.
def _traffic_parser():
    from app.external.remnawave_api import RemnaWaveAPI

    return RemnaWaveAPI.__new__(RemnaWaveAPI)


def _panel_user(uuid, traffic):
    return _traffic_parser()._parse_user(
        {
            'uuid': uuid,
            'shortUuid': 'short-217',
            'username': 'trial-regression',
            'expireAt': '2026-10-14T12:00:00Z',
            'createdAt': '2026-10-11T12:00:00Z',
            'updatedAt': '2026-10-11T12:00:00Z',
            'userTraffic': traffic,
        }
    )


@pytest.mark.parametrize('lifetime', [0, 7000, 999_999, 1_000_000, 2_000_000])
def test_parser_marks_real_nonnegative_integer_usage_as_available(lifetime) -> None:
    traffic = _traffic_parser()._parse_user_traffic({'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': lifetime})

    assert traffic.lifetime_traffic_available is True
    assert traffic.lifetime_used_traffic_bytes == lifetime


@pytest.mark.parametrize('lifetime', [None, '0', '1000000', 0.0, 1_000_000.0, False, True, -1, [], {}])
def test_parser_never_marks_malformed_lifetime_as_known_zero(lifetime) -> None:
    traffic = _traffic_parser()._parse_user_traffic({'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': lifetime})

    assert traffic.lifetime_traffic_available is False


def test_parser_missing_lifetime_never_certifies_the_legacy_zero_default() -> None:
    traffic = _traffic_parser()._parse_user_traffic({'usedTrafficBytes': 7000})

    assert traffic.lifetime_used_traffic_bytes == 0
    assert traffic.lifetime_traffic_available is False


@pytest.mark.parametrize('traffic', [None, {}])
def test_parser_no_traffic_object_stays_unknown(traffic) -> None:
    assert _traffic_parser()._parse_user_traffic(traffic) is None


@pytest.mark.asyncio
async def test_usage_reader_keeps_known_zero_and_excludes_unknown_profiles() -> None:
    api = _FakeApi(
        users=[
            _panel_user('known-zero', {'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': 0}),
            _panel_user('known-used', {'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': 2_000_000}),
            _panel_user('missing-counter', {'usedTrafficBytes': 0}),
            _panel_user('missing-object', None),
            _panel_user('malformed-counter', {'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': '0'}),
        ]
    )
    service = _service_with_panel(api)

    assert await service._fetch_trial_panel_usage() == {'known-zero': 0, 'known-used': 2_000_000}


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['not-configured', 'failure', 'empty', 'all-unknown'])
async def test_usage_reader_cannot_turn_missing_panel_data_into_zero(case) -> None:
    api = _FakeApi(
        users=[_panel_user('unknown', {'usedTrafficBytes': 0})] if case == 'all-unknown' else [],
        error=RuntimeError('panel unavailable') if case == 'failure' else None,
    )
    service = _service_with_panel(api, configured=case != 'not-configured')
    factory = Mock(wraps=service.subscription_service.get_api_client)
    service.subscription_service.get_api_client = factory

    result = await service._fetch_trial_panel_usage()

    assert result in (None, {}), 'unknown profiles must never be supplied with a synthetic zero'
    if case != 'all-unknown':
        assert result is None
    if case == 'not-configured':
        factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(('lifetime', 'should_send'), [(0, True), (7000, True), (999_999, True), (1_000_000, False)])
async def test_trial_letter_uses_the_decimal_one_megabyte_boundary(wired, lifetime, should_send) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': lifetime})
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    assert service._send_trial_not_connected_notification.await_count == int(should_send)
    assert wired.await_count == int(should_send)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('lifetime', 'first_connected', 'should_send'),
    [(7000, True, True), (0, True, True), (2_000_000, False, False)],
)
async def test_trial_letter_reads_lifetime_despite_daily_reset_and_probe_date(
    wired, lifetime, first_connected, should_send
) -> None:
    traffic = {'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': lifetime}
    if first_connected:
        traffic['firstConnectedAt'] = '2026-10-11T12:00:00Z'
    service = _service_with_panel(_FakeApi(users=[_panel_user('panel-uuid-217', traffic)]))
    service.bot = SimpleNamespace(send_message=AsyncMock())
    service._send_trial_not_connected_notification = AsyncMock(return_value=True)
    service._log_monitoring_event = AsyncMock()
    # Если монитор вернулся к дате первого подключения, тест упадёт явно.
    service._fetch_connected_panel_uuids = AsyncMock(side_effect=AssertionError('legacy connection date used'))
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    assert service._send_trial_not_connected_notification.await_count == int(should_send)
    assert wired.await_count == int(should_send)
    service._fetch_connected_panel_uuids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('usage', [None, {}, {'other-person': 0}])
async def test_missing_profile_or_usage_never_creates_a_sent_marker(wired, monkeypatch, usage) -> None:
    fake_logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock(), debug=Mock())
    monkeypatch.setattr(module, 'logger', fake_logger)
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value=usage)
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_not_awaited()
    wired.assert_not_awaited()
    fake_logger.error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('usage', 'should_send'),
    [
        ({'panel-uuid-217': 0, 'user-level-uuid': 2_000_000}, True),
        ({'panel-uuid-217': 2_000_000, 'user-level-uuid': 0}, False),
        ({'user-level-uuid': 0}, False),
    ],
)
async def test_subscription_uuid_is_authoritative_over_another_user_uuid(wired, usage, should_send) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value=usage)
    db, _ = _db_returning([_subscription(user_uuid='user-level-uuid')])

    await service._check_trial_not_connected(db)

    assert service._send_trial_not_connected_notification.await_count == int(should_send)
    assert wired.await_count == int(should_send)


@pytest.mark.asyncio
@pytest.mark.parametrize('subscription_uuid', [None, ''])
async def test_empty_subscription_uuid_uses_the_user_profile_for_an_unconnected_trial(wired, subscription_uuid) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'user-level-uuid': 0})
    db, _ = _db_returning([_subscription(uuid=subscription_uuid, user_uuid='user-level-uuid')])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()
    wired.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_letter_delivery_is_not_recorded_as_sent(wired) -> None:
    service = _service()
    service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0})
    service._send_trial_not_connected_notification.return_value = False
    db, _ = _db_returning([_subscription()])

    await service._check_trial_not_connected(db)

    service._send_trial_not_connected_notification.assert_awaited_once()
    wired.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_where_excludes_team_even_when_its_trial_flag_and_dates_match(wired) -> None:
    """Исполняется настоящий WHERE, без ORM-загрузки: проверяем адресатов письма.

    Подписка Team намеренно повторяет флаг, возраст и срок пробной. Отличие только
    тариф; мок, возвращающий заранее выбранный список, эту защиту не доказывает.
    """
    from sqlalchemy import create_engine, select, text

    from app.database.models import Subscription

    engine = create_engine('sqlite://')
    trial = _subscription()
    team = _subscription(sub_id=218, uuid='team-profile')
    lookup = {trial.id: trial, team.id: team}
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, status TEXT)'))
        connection.execute(
            text(
                'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, is_trial BOOLEAN, '
                'status TEXT, tariff_id INTEGER, start_date TIMESTAMP, end_date TIMESTAMP)'
            )
        )
        connection.execute(text("INSERT INTO users VALUES (901, 'active')"))
        connection.execute(
            text('INSERT INTO subscriptions VALUES (:id, 901, 1, :status, :tariff, :start, :end)'),
            [
                {
                    'id': subscription.id,
                    'status': 'active',
                    'tariff': tariff,
                    'start': subscription.start_date.strftime('%Y-%m-%d %H:%M:%S.%f'),
                    'end': subscription.end_date.strftime('%Y-%m-%d %H:%M:%S.%f'),
                }
                for subscription, tariff in [(trial, TRIAL_TARIFF_ID), (team, TEAM_TARIFF_ID)]
            ],
        )

        async def execute(statement):
            projected = select(Subscription.id).select_from(statement.get_final_froms()[0]).where(statement.whereclause)
            selected = [lookup[sub_id] for sub_id in connection.execute(projected).scalars()]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: selected))

        service = _service()
        service._fetch_trial_panel_usage = AsyncMock(return_value={'panel-uuid-217': 0, 'team-profile': 0})
        await service._check_trial_not_connected(SimpleNamespace(execute=execute))

    engine.dispose()
    service._send_trial_not_connected_notification.assert_awaited_once_with(trial.user, trial)
    wired.assert_awaited_once()
    assert wired.await_args[0][2] == trial.id


@pytest.mark.parametrize('lifetime', ['2000000', 2_000_000.0])
def test_shared_parser_preserves_numeric_display_without_certifying_help_usage(lifetime) -> None:
    traffic = _traffic_parser()._parse_user_traffic({'usedTrafficBytes': 0, 'lifetimeUsedTrafficBytes': lifetime})
    assert traffic.lifetime_used_traffic_bytes == 2_000_000
    assert traffic.lifetime_traffic_available is False

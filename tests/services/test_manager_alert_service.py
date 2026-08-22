import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter
from structlog.testing import capture_logs

from app.config import settings
from app.handlers.manager_alerts import (
    _TOPIC_BY_ARGUMENT,
    bind_manager_alert_topic,
    is_manager_alert_setup_command,
)
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.manager_alert_service import (
    _ADMIN_CATEGORY_TO_MANAGER_TOPIC,
    ManagerAlertSettingsService,
    ManagerAlertTopic,
    manager_alert_service,
)


@pytest.fixture(autouse=True)
def isolated_manager_alert_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ManagerAlertSettingsService, '_storage_path', tmp_path / 'manager_alert_settings.json')
    ManagerAlertSettingsService._data = {}
    ManagerAlertSettingsService._loaded = False
    yield
    ManagerAlertSettingsService._data = {}
    ManagerAlertSettingsService._loaded = False


def test_bind_topics_requires_one_group() -> None:
    assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=ManagerAlertTopic.TICKETS, thread_id=10)
    assert not ManagerAlertSettingsService.bind_topic(
        chat_id=-100654321, topic=ManagerAlertTopic.PAYMENTS, thread_id=11
    )
    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.TICKETS) == (-100123456, 10)
    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.PAYMENTS) is None


@pytest.mark.asyncio
async def test_mirror_admin_category_uses_allow_list_without_keyboard() -> None:
    assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=ManagerAlertTopic.PAYMENTS, thread_id=20)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert await manager_alert_service.mirror_admin_category(bot, 'balance', '<b>Пополнение</b>')
    bot.send_message.assert_awaited_once_with(
        chat_id=-100123456,
        message_thread_id=20,
        text='<b>Пополнение</b>',
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('category', ['infrastructure', 'errors', 'partners', 'promo', None])
async def test_mirror_never_sends_excluded_admin_categories(category: str | None) -> None:
    # Привязываем ВСЕ темы: иначе тест зелен по случайности — у запрещённой
    # категории просто нет получателя. С привязанными темами он ловит и подмену
    # `.get(category)` на `.get(category, какая-нибудь-тема-по-умолчанию)`.
    for index, topic in enumerate(ManagerAlertTopic, start=1):
        assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=topic, thread_id=index)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert not await manager_alert_service.mirror_admin_category(bot, category, 'private data')
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_status_does_not_include_node_identity() -> None:
    assert ManagerAlertSettingsService.bind_topic(
        chat_id=-100123456, topic=ManagerAlertTopic.SERVICE_STATUS, thread_id=30
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    assert await manager_alert_service.send_service_status(bot, restored=False, event_count=2)
    sent_text = bot.send_message.await_args.kwargs['text']
    assert 'VPN-локацией' in sent_text
    assert '10.0.0.1' not in sent_text
    assert 'node-' not in sent_text


@pytest.mark.asyncio
async def test_admin_notification_mirrors_only_safe_copy_without_admin_keyboard() -> None:
    assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=ManagerAlertTopic.PAYMENTS, thread_id=20)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = AdminNotificationService(bot)
    service.chat_id = -100999999
    service.enabled = True

    assert await service._send_message('<b>Пополнение</b>', category=NotificationCategory.BALANCE)
    assert bot.send_message.await_count == 2
    admin_call, manager_call = bot.send_message.await_args_list
    assert admin_call.kwargs['chat_id'] == -100999999
    assert manager_call.kwargs['chat_id'] == -100123456
    assert manager_call.kwargs['message_thread_id'] == 20
    assert 'reply_markup' not in manager_call.kwargs


def test_only_exact_manager_setup_commands_pass_group_filter() -> None:
    assert is_manager_alert_setup_command('/manager_alert_bind payments')
    assert is_manager_alert_setup_command('/manager_alert_status@teplo_VPN_bot')
    assert not is_manager_alert_setup_command('/admin_help')
    assert not is_manager_alert_setup_command('/manager_alert_bindings')


def _campaign_stub() -> MagicMock:
    campaign = MagicMock()
    campaign.name = 'Кувалда 7000₽'
    campaign.start_parameter = 'teplo2'
    campaign.is_balance_bonus = True
    campaign.balance_bonus_kopeks = 5000
    return campaign


def _marketing_service(bot: MagicMock) -> AdminNotificationService:
    assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=ManagerAlertTopic.MARKETING, thread_id=40)
    service = AdminNotificationService(bot)
    service.chat_id = -100999999
    service.enabled = True
    return service


@pytest.mark.asyncio
async def test_campaign_visit_alert_reaches_manager_marketing_topic() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = _marketing_service(bot)
    telegram_user = types.User(id=6533655760, is_bot=False, first_name='SMOKY', username='SmokyPkr')

    assert await service.send_campaign_link_visit_notification(MagicMock(), telegram_user, _campaign_stub(), user=None)

    assert bot.send_message.await_count == 2
    admin_call, manager_call = bot.send_message.await_args_list
    assert manager_call.kwargs['chat_id'] == -100123456
    assert manager_call.kwargs['message_thread_id'] == 40
    assert manager_call.kwargs['text'] == admin_call.kwargs['text']
    assert 'ПЕРЕХОД ПО РК' in manager_call.kwargs['text']
    assert 'reply_markup' not in manager_call.kwargs


@pytest.mark.asyncio
async def test_campaign_registration_alert_reaches_manager_marketing_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = _marketing_service(bot)
    monkeypatch.setattr(service, '_record_subscription_event', AsyncMock())
    user = MagicMock(id=7, promo_group=None, promo_group_id=None)

    assert await service.send_campaign_registration_notification(
        MagicMock(),
        6533655760,
        'SMOKY',
        'SmokyPkr',
        _campaign_stub(),
        user,
        bonus_type='balance',
        balance_kopeks=5000,
    )

    assert bot.send_message.await_count == 2
    admin_call, manager_call = bot.send_message.await_args_list
    assert manager_call.kwargs['message_thread_id'] == 40
    assert manager_call.kwargs['text'] == admin_call.kwargs['text']
    assert 'РЕГИСТРАЦИЯ ПО РК' in manager_call.kwargs['text']


@pytest.mark.asyncio
async def test_promo_category_alone_never_reaches_manager_even_with_marketing_bound() -> None:
    """Промокоды и смена промогруппы идут той же категорией `promo` — и остаются у админа."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    service = _marketing_service(bot)

    assert await service._send_message('<b>Активирован промокод</b>', category=NotificationCategory.PROMO)

    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.kwargs['chat_id'] == -100999999


def _owner_topic_message(text: str, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(type(settings), 'is_admin', lambda self, telegram_id: True)
    message = MagicMock()
    message.text = text
    message.chat.id = -100123456
    message.chat.type = ChatType.SUPERGROUP
    message.is_topic_message = True
    message.message_thread_id = 40
    message.from_user = types.User(id=1, is_bot=False, first_name='owner')
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_bind_accepts_marketing_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _owner_topic_message('/manager_alert_bind marketing', monkeypatch)

    await bind_manager_alert_topic(message)

    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.MARKETING) == (-100123456, 40)
    assert 'подключена' in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_bind_hint_lists_every_accepted_category(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подсказка собирается из маршрута — владелец не должен получить список, которого нет."""
    message = _owner_topic_message('/manager_alert_bind нет-такой', monkeypatch)

    await bind_manager_alert_topic(message)

    hint = message.answer.await_args.args[0]
    # Оба утверждения нужны: перебор маршрута ловит новую категорию, забытую в
    # подсказке, а жёсткий список — подсказку, отставшую от маршрута целиком.
    for argument in _TOPIC_BY_ARGUMENT:
        assert argument in hint
    for argument in ('tickets', 'subscriptions', 'payments', 'reports', 'service', 'marketing'):
        assert argument in hint
    assert ManagerAlertSettingsService.get_recipient(ManagerAlertTopic.MARKETING) is None


def _call_sites(matches) -> set[tuple[str, str]]:
    """Собирает пары «файл, объемлющая функция» для вызовов, прошедших `matches`."""
    found: set[tuple[str, str]] = set()
    for path in sorted(Path('app').rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for enclosing in ast.walk(tree):
            if not isinstance(enclosing, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(enclosing):
                if isinstance(node, ast.Call) and matches(node):
                    found.add((str(path), enclosing.name))
    return found


def test_narrow_manager_route_has_exactly_the_declared_call_sites() -> None:
    """Маршрут `manager_topic=` не самоограничен — перечисляем его поимённо.

    `mirror_admin_category` защищён тем, что категории нет в белом списке. У
    `manager_topic=` такой защиты нет: любой из десятков вызовов `_send_message`
    может подписать менеджера на что угодно одним аргументом. Этот тест краснеет
    на КАЖДОМ новом месте вызова — добавлять сюда придётся руками и осознанно.

    Граница сторожа: он читает вызов таким, как тот написан. Собрать аргумент в
    словарь и раскрыть его (`**routing`) — обойдёт. Это защита от рассеянности,
    а не от обхода; так и записано в докстринге самого сервиса.
    """
    call_sites = _call_sites(lambda node: any(kw.arg == 'manager_topic' for kw in node.keywords))

    assert call_sites == {
        ('app/services/admin_notification_service.py', 'send_campaign_link_visit_notification'),
        ('app/services/admin_notification_service.py', 'send_campaign_registration_notification'),
    }


def test_direct_manager_send_has_exactly_the_declared_call_sites() -> None:
    """Самый широкий маршрут — прямой `manager_alert_service.send(тема, текст)`.

    У него нет ни белого списка, ни ключевого аргумента: он берёт произвольный
    текст и произвольную тему. Ровно им уедет менеджеру чужой трейсбек, если
    кто-нибудь однажды напишет строку не думая.

    Граница сторожа (проверена мутацией, а не предположена): он узнаёт вызов по
    имени `manager_alert_service`. Присвоить сервис другой переменной и звать
    через неё — обойдёт, ровно как `**routing` обходит соседний сторож.
    """

    def is_direct_send(node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == 'send'
            and isinstance(func.value, ast.Name)
            and func.value.id == 'manager_alert_service'
        )

    assert _call_sites(is_direct_send) == {
        ('app/services/admin_notification_service.py', '_send_message'),
        ('app/services/reporting_service.py', '_deliver_report'),
    }


def test_admin_category_allow_list_has_exactly_the_declared_categories() -> None:
    """Первый маршрут — тоже поимённо: `promo`, `partners`, `errors` сюда не попадают."""
    assert set(_ADMIN_CATEGORY_TO_MANAGER_TOPIC) == {
        'tickets',
        'trials',
        'purchases',
        'renewals',
        'balance',
        'addons',
    }


@pytest.mark.parametrize('text', [None, '', '   ', '\n'])
def test_group_filter_survives_messages_without_text(text: str | None) -> None:
    """Фото, стикер и служебное «создана тема» приходят с text=None.

    До 22.08.2026 такое сообщение роняло IndexError прямо в мидлваре — на боевом
    это видели два раза за час, когда владелец заводил тему «Маркетинг».
    """
    assert is_manager_alert_setup_command(text) is False


@pytest.mark.asyncio
async def test_flood_control_drops_manager_copy_as_known_behaviour() -> None:
    """Потеря копии под flood control — известное поведение, а не «Unexpected».

    TelegramRetryAfter наследуется от TelegramAPIError, поэтому в перечисленный
    кортеж исключений он не попадал и уходил в общий except. Ретрая тут нет
    намеренно: `send` зовётся внутри обработки клиентского /start.
    """
    assert ManagerAlertSettingsService.bind_topic(chat_id=-100123456, topic=ManagerAlertTopic.MARKETING, thread_id=40)
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=TelegramRetryAfter(method=MagicMock(), message='flood', retry_after=7))

    with capture_logs() as logs:
        assert await manager_alert_service.send(bot, ManagerAlertTopic.MARKETING, '<b>ПЕРЕХОД ПО РК</b>') is False

    assert bot.send_message.await_count == 1
    # Сама ветка ради лога и заведена: без этой проверки её можно было убрать
    # целиком, и тест остался бы зелёным — общий except даёт тот же неретрай.
    # Нашла мутация, не ревью.
    dropped = [
        entry for entry in logs if entry['event'] == 'Manager alert dropped by flood control (no retry by design)'
    ]
    assert len(dropped) == 1
    assert dropped[0]['retry_after'] == 7
    assert dropped[0]['topic'] == 'marketing'

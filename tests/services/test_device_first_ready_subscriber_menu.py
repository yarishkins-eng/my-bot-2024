"""ВК-2: после «✅ Ваша VPN-подписка готова» касса кабинета присылает меню подписчика.

До этапа последним меню в чате у купившего в кабинете оставалось меню пробного («💎 Оформить подписку»,
«💳 Тарифы») или новичка, пока он сам не нажмёт /start: касса — главный путь покупки, а хук меню подписчика
стоял только на старых путях (регресс записи 22.06). Меню шлётся ПОСЛЕ коммита строки `ready` как `sent`, в
своей короткой сессии и с потолком по времени. Числа в фикстурах не совпадают с умолчаниями кода.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service
from app.services.device_first_checkout_service import (
    OWNER_ALERT_NOTIFICATION_TYPE,
    READY_NOTIFICATION_TYPE,
    SALE_NOTIFICATION_PREFIX,
    process_device_first_notification_outbox,
)


TELEGRAM_ID = 7_454_290_913
RU_LOCALE = Path(__file__).resolve().parents[2] / 'app' / 'localization' / 'locales' / 'ru.json'


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _row(row_id, notification_type):
    return SimpleNamespace(
        id=row_id,
        checkout_id=4_900 + row_id,
        notification_type=notification_type,
        status='pending',
        lease_token=None,
        lease_expires_at=None,
        sending_at=None,
        sent_at=None,
        last_error=None,
    )


def _checkout():
    return SimpleNamespace(id=4_917, public_id='vk2-checkout', user_id=185, created_subscription_id=7_301)


def _subscription(**overrides):
    base = {
        'id': 7_301,
        'is_trial': False,
        'status': 'active',
        'actual_status': 'active',
        'days_left': 89,
        'is_daily_tariff': False,
        'end_date': datetime.now(UTC) + timedelta(days=89, hours=5),
        'in_grace': False,
        'grace_until': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(subscription, *, telegram_id=TELEGRAM_ID):
    return SimpleNamespace(
        id=185, telegram_id=telegram_id, language='ru', subscription=subscription, subscriptions=[subscription]
    )


def _queue_db(rows, *, user, trail):
    """Сессия очереди: захват строк → на каждую строку заказ → перечитывание строки под аренду."""
    db = MagicMock()
    results = [_Result(rows)]
    for row in rows:
        results.append(_Result([_checkout()]))
        results.append(_Result([row]))
    db.execute = AsyncMock(side_effect=results)

    async def _commit():
        trail.append(('commit', tuple(row.status for row in rows)))

    db.commit = AsyncMock(side_effect=_commit)
    db.rollback = AsyncMock()

    async def _get(model, key):
        if model.__name__ == 'User':
            return user
        if model.__name__ == 'DeviceFirstNotificationOutbox':
            return next((row for row in rows if row.id == key), None)
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _menu_session(fresh_user, *, get_error=None):
    """Своя короткая сессия меню: пустая память, поэтому пользователь приходит свежим."""
    menu_db = MagicMock()
    menu_db.get = AsyncMock(side_effect=get_error, return_value=fresh_user)
    menu_db.refresh = AsyncMock()

    class _Factory:
        calls = 0

        def __call__(self):
            _Factory.calls += 1
            return self

        async def __aenter__(self):
            return menu_db

        async def __aexit__(self, *exc):
            return False

    return menu_db, _Factory()


def _bot(trail, *, error=None):
    bot = MagicMock()

    async def _send(chat_id, text, **kwargs):
        if error is not None:
            raise error
        trail.append(('send', chat_id, text))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


@pytest.fixture
def menu_on(monkeypatch):
    monkeypatch.setattr(service.settings, 'FUNNEL_MENU_ENABLED', True)
    monkeypatch.setattr(service.settings, 'FUNNEL_SUBSCRIBER_MENU_ENABLED', True)
    monkeypatch.setattr(service.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(service.settings, 'MULTI_TARIFF_ENABLED', False)


def _quiet_queue():
    return (
        patch.object(service, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
    )


@pytest.mark.asyncio
async def test_menu_goes_to_the_fresh_buyer_after_the_ready_row_is_saved(menu_on):
    trail = []
    stale_user = _user(_subscription(is_trial=True, days_left=2))  # так выглядит пробная в памяти очереди
    fresh_user = _user(_subscription())
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _queue_db([row], user=stale_user, trail=trail)
    menu_db, factory = _menu_session(fresh_user)

    async def _menu(session, user):
        trail.append(('menu', session, user))
        return True

    queue, revive = _quiet_queue()
    with (
        queue,
        revive,
        patch('app.database.database.AsyncSessionLocal', factory),
        patch('app.utils.funnel_notify.notify_subscriber_menu', AsyncMock(side_effect=_menu)) as menu,
        patch.object(service.logger, 'info') as info,
    ):
        sent = await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    assert sent == 1 and row.status == 'sent'
    menu.assert_awaited_once()
    # Порядок — договор: «готова», коммит строки как `sent`, и только потом меню — из СВОЕЙ сессии.
    assert [step[0] for step in trail] == ['commit', 'send', 'commit', 'menu']
    assert trail[2] == ('commit', ('sent',))
    assert trail[3][1] is menu_db and trail[3][2] is fresh_user
    menu_db.get.assert_awaited_once()
    assert menu_db.get.await_args.args[1] == 185
    info.assert_any_call('Меню подписчика после покупки в кабинете', checkout_id=4_917, sent=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['session', 'hang'])
async def test_menu_failure_or_hang_never_touches_the_ready_row(menu_on, monkeypatch, failure):
    """«Готова» уже ушла и сохранена; сбой или зависание меню — только предупреждение в лог."""
    trail = []
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _queue_db([row], user=_user(_subscription()), trail=trail)
    menu_db, factory = _menu_session(
        _user(_subscription()), get_error=RuntimeError('connection refused') if failure == 'session' else None
    )

    async def _hang(session, user):
        await asyncio.sleep(5)

    monkeypatch.setattr(service, 'CLIENT_MENU_TIMEOUT_SECONDS', 0.05)
    queue, revive = _quiet_queue()
    with (
        queue,
        revive,
        patch('app.database.database.AsyncSessionLocal', factory),
        patch('app.utils.funnel_notify.notify_subscriber_menu', AsyncMock(side_effect=_hang)),
        patch.object(service.logger, 'warning') as warning,
    ):
        sent = await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    assert sent == 1 and row.status == 'sent' and row.last_error is None
    warning.assert_called_once()
    assert warning.call_args.args[0] == 'Меню подписчика после покупки в кабинете не отправлено'
    assert warning.call_args.kwargs['checkout_id'] == 4_917


@pytest.mark.asyncio
async def test_no_session_is_opened_when_the_subscriber_menu_is_off(monkeypatch):
    monkeypatch.setattr(service.settings, 'FUNNEL_MENU_ENABLED', True)
    monkeypatch.setattr(service.settings, 'FUNNEL_SUBSCRIBER_MENU_ENABLED', False)
    trail = []
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _queue_db([row], user=_user(_subscription()), trail=trail)
    _menu_db, factory = _menu_session(_user(_subscription()))

    queue, revive = _quiet_queue()
    with queue, revive, patch('app.database.database.AsyncSessionLocal', factory):
        await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    assert row.status == 'sent'
    assert type(factory).calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('notification_type', 'sender'),
    [
        (OWNER_ALERT_NOTIFICATION_TYPE, '_send_owner_order_stuck_alert'),
        (f'{SALE_NOTIFICATION_PREFIX}first', '_send_owner_sale_card'),
        ('referral_reward:311', '_send_referral_reward_message'),
    ],
)
async def test_owner_and_partner_rows_never_send_the_client_menu(menu_on, notification_type, sender):
    trail = []
    row = _row(1, notification_type)
    db = _queue_db([row], user=_user(_subscription()), trail=trail)
    _menu_db, factory = _menu_session(_user(_subscription()))

    queue, revive = _quiet_queue()
    menu = AsyncMock(return_value=True)
    with (
        queue,
        revive,
        patch.object(service, sender, AsyncMock(return_value=True)),
        patch('app.database.database.AsyncSessionLocal', factory),
        patch('app.utils.funnel_notify.notify_subscriber_menu', menu),
    ):
        await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    menu.assert_not_awaited()
    assert type(factory).calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['telegram', 'no_telegram_id'])
async def test_undelivered_ready_text_gets_no_menu(menu_on, broken):
    trail = []
    row = _row(1, READY_NOTIFICATION_TYPE)
    user = _user(_subscription(), telegram_id=None if broken == 'no_telegram_id' else TELEGRAM_ID)
    db = _queue_db([row], user=user, trail=trail)
    _menu_db, factory = _menu_session(user)
    bot = _bot(trail, error=RuntimeError('Forbidden: bot was blocked') if broken == 'telegram' else None)

    queue, revive = _quiet_queue()
    menu = AsyncMock(return_value=True)
    with (
        queue,
        revive,
        patch('app.database.database.AsyncSessionLocal', factory),
        patch('app.utils.funnel_notify.notify_subscriber_menu', menu),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 0 and row.status == 'failed'
    if broken == 'no_telegram_id':
        assert row.last_error == 'RuntimeError: telegram_recipient_unavailable'
    menu.assert_not_awaited()
    assert type(factory).calls == 0


def _texts_spy(requested):
    from app.localization import texts as texts_module

    real_get_texts = texts_module.get_texts

    def _spy(language):
        real = real_get_texts(language)

        class _Proxy:
            def t(self, key, default=None):
                requested.append(key)
                return real.t(key, default)

            def __getattr__(self, name):
                return getattr(real, name)

        return _Proxy()

    return _spy


async def _run_real_menu(trail, *, fresh_user, menu_bot):
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _queue_db([row], user=_user(_subscription(is_trial=True, days_left=2)), trail=trail)
    _menu_db, factory = _menu_session(fresh_user)
    requested = []
    queue, revive = _quiet_queue()
    with (
        queue,
        revive,
        patch('app.database.database.AsyncSessionLocal', factory),
        patch('app.bot_factory.create_bot', return_value=menu_bot),
        patch('app.localization.texts.get_texts', _texts_spy(requested)),
        patch('app.utils.funnel_notify._delete_remembered_menu', AsyncMock()) as delete_old,
        patch('app.utils.funnel_notify._remember_menu_message_id', AsyncMock()) as remember,
        patch.object(service.logger, 'info') as info,
    ):
        await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)
    return row, requested, delete_old, remember, info


@pytest.mark.asyncio
async def test_real_menu_path_sends_the_paid_menu_from_the_locale(menu_on):
    """Настоящий `notify_subscriber_menu`: меню подписчика без «Продлить» и без кнопок пробного, текст — из локали."""
    trail = []
    menu_bot = MagicMock()
    menu_bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=5_512))
    menu_bot.session.close = AsyncMock()

    row, requested, delete_old, remember, info = await _run_real_menu(
        trail, fresh_user=_user(_subscription()), menu_bot=menu_bot
    )

    assert row.status == 'sent'
    menu_bot.send_message.assert_awaited_once()
    kwargs = menu_bot.send_message.await_args.kwargs
    assert kwargs['chat_id'] == TELEGRAM_ID
    assert 'FUNNEL_SUBSCRIPTION_ACTIVE' in requested
    assert kwargs['text'] == json.loads(RU_LOCALE.read_text(encoding='utf-8'))['FUNNEL_SUBSCRIPTION_ACTIVE']
    buttons = [button.text for line in kwargs['reply_markup'].inline_keyboard for button in line]
    assert '👤 Личный кабинет' in buttons
    for stale in ('💎 Продлить подписку', '💎 Оформить подписку', '💳 Тарифы', '🎁 Попробовать бесплатно'):
        assert stale not in buttons
    delete_old.assert_awaited_once_with(menu_bot, TELEGRAM_ID)
    remember.assert_awaited_once_with(TELEGRAM_ID, 5_512)
    info.assert_any_call('Меню подписчика после покупки в кабинете', checkout_id=4_917, sent=True)


@pytest.mark.asyncio
async def test_menu_that_telegram_refused_keeps_the_old_menu_and_says_so(menu_on):
    trail = []
    menu_bot = MagicMock()
    menu_bot.send_message = AsyncMock(side_effect=RuntimeError('Too Many Requests'))
    menu_bot.session.close = AsyncMock()

    row, _requested, delete_old, remember, info = await _run_real_menu(
        trail, fresh_user=_user(_subscription()), menu_bot=menu_bot
    )

    assert row.status == 'sent'
    delete_old.assert_not_awaited()  # новое не ушло — старое не трогаем
    remember.assert_not_awaited()
    info.assert_any_call('Меню подписчика после покупки в кабинете', checkout_id=4_917, sent=False)

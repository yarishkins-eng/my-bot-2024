"""ВК-2: после «✅ Ваша VPN-подписка готова» касса кабинета присылает меню подписчика.

До этапа последним меню в чате у купившего в кабинете оставалось меню пробного («💎 Оформить подписку»,
«💳 Тарифы») или новичка, пока он сам не нажмёт /start: касса — главный путь покупки, а хук меню подписчика
стоял только на старых путях (регресс записи 22.06). Числа в фикстурах не совпадают с умолчаниями кода.
"""

from datetime import UTC, datetime, timedelta
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
        'is_trial': True,
        'status': 'active',
        'actual_status': 'active',
        'days_left': 2,
        'is_daily_tariff': False,
        'end_date': datetime.now(UTC) + timedelta(days=2, hours=5),
        'in_grace': False,
        'grace_until': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(subscription):
    return SimpleNamespace(
        id=185, telegram_id=7_454_290_913, language='ru', subscription=subscription, subscriptions=[subscription]
    )


def _db(rows, *, user, subscription, trail):
    db = MagicMock()
    results = [_Result(rows)]
    for row in rows:
        results.append(_Result([_checkout()]))
        results.append(_Result([row]))
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _get(model, key):
        if model.__name__ == 'User':
            return user
        if model.__name__ == 'Subscription':
            return subscription if key == subscription.id else None
        if model.__name__ == 'DeviceFirstNotificationOutbox':
            return next((row for row in rows if row.id == key), None)
        return None

    async def _refresh(obj, attribute_names=None):
        trail.append(('refresh', obj, tuple(attribute_names or ())))
        if obj is subscription:
            # Так выглядит строка в базе после покупки: пробная стала платной «Базового» на 90 дней.
            subscription.is_trial = False
            subscription.days_left = 89

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock(side_effect=_refresh)
    return db


def _bot(trail):
    bot = MagicMock()

    async def _send(chat_id, text, **kwargs):
        trail.append(('send', chat_id, text, kwargs.get('reply_markup')))

    bot.send_message = AsyncMock(side_effect=_send)
    return bot


def _quiet_queue():
    return (
        patch.object(service, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
    )


@pytest.mark.asyncio
async def test_subscriber_menu_follows_the_ready_text_for_the_same_person():
    trail = []
    subscription = _subscription()
    user = _user(subscription)
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _db([row], user=user, subscription=subscription, trail=trail)

    async def _menu(db_arg, user_arg):
        trail.append(('menu', db_arg, user_arg))

    queue, revive = _quiet_queue()
    with queue, revive, patch('app.utils.funnel_notify.notify_subscriber_menu', AsyncMock(side_effect=_menu)) as menu:
        sent = await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    assert sent == 1 and row.status == 'sent'
    menu.assert_awaited_once_with(db, user)
    kinds = [step[0] for step in trail]
    # Порядок — договор: сначала «готова», потом свежая подписка, потом меню.
    assert kinds == ['send', 'refresh', 'menu']
    assert trail[0][1] == 7_454_290_913 and 'готова' in trail[0][2]
    assert trail[1][1] is subscription


@pytest.mark.asyncio
async def test_menu_failure_does_not_fail_the_ready_row():
    """«Готова» уже ушла, а клиентскую строку не повторяют никогда — сбой меню не делает её `failed`."""
    trail = []
    subscription = _subscription()
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _db([row], user=_user(subscription), subscription=subscription, trail=trail)

    queue, revive = _quiet_queue()
    boom = AsyncMock(side_effect=RuntimeError('telegram is down'))
    with queue, revive, patch('app.utils.funnel_notify.notify_subscriber_menu', boom):
        sent = await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    assert sent == 1
    assert row.status == 'sent' and row.last_error is None
    boom.assert_awaited_once()
    assert [step[0] for step in trail] == ['send', 'refresh']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('notification_type', 'sender'),
    [
        (OWNER_ALERT_NOTIFICATION_TYPE, '_send_owner_order_stuck_alert'),
        (f'{SALE_NOTIFICATION_PREFIX}first', '_send_owner_sale_card'),
        ('referral_reward:311', '_send_referral_reward_message'),
    ],
)
async def test_owner_and_partner_rows_never_send_the_client_menu(notification_type, sender):
    trail = []
    subscription = _subscription()
    row = _row(1, notification_type)
    db = _db([row], user=_user(subscription), subscription=subscription, trail=trail)

    queue, revive = _quiet_queue()
    menu = AsyncMock()
    with (
        queue,
        revive,
        patch.object(service, sender, AsyncMock(return_value=True)),
        patch('app.utils.funnel_notify.notify_subscriber_menu', menu),
    ):
        await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    menu.assert_not_awaited()
    assert trail == []


@pytest.mark.asyncio
async def test_trial_left_in_the_session_still_gets_the_paid_menu(monkeypatch):
    """Настоящий путь меню: в общей сессии мониторинга лежит пробная, прочитанная до покупки.

    Без перечитывания классификатор решил бы «пробный» и меню подписчика не пришло бы вовсе.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'FUNNEL_MENU_ENABLED', True)
    monkeypatch.setattr(settings, 'FUNNEL_SUBSCRIBER_MENU_ENABLED', True)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    trail = []
    subscription = _subscription()
    user = _user(subscription)
    row = _row(1, READY_NOTIFICATION_TYPE)
    db = _db([row], user=user, subscription=subscription, trail=trail)
    menu_bot = MagicMock()
    menu_bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=5_512))
    menu_bot.session.close = AsyncMock()

    queue, revive = _quiet_queue()
    with (
        queue,
        revive,
        patch('app.bot_factory.create_bot', return_value=menu_bot),
        patch('app.utils.funnel_notify._delete_remembered_menu', AsyncMock()) as delete_old,
        patch('app.utils.funnel_notify._remember_menu_message_id', AsyncMock()) as remember,
    ):
        await process_device_first_notification_outbox(db, bot=_bot(trail), limit=10)

    menu_bot.send_message.assert_awaited_once()
    kwargs = menu_bot.send_message.await_args.kwargs
    assert kwargs['chat_id'] == 7_454_290_913
    assert kwargs['text'] == '✅ Подписка активна! Вот твоё меню:'
    buttons = [button.text for line in kwargs['reply_markup'].inline_keyboard for button in line]
    assert '👤 Личный кабинет' in buttons
    assert '💎 Оформить подписку' not in buttons and '💳 Тарифы' not in buttons
    delete_old.assert_awaited_once_with(menu_bot, 7_454_290_913)
    remember.assert_awaited_once_with(7_454_290_913, 5_512)

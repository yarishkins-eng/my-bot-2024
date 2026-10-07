"""ВК-4 (АП-1): в минуту конца подписки — одно письмо от бота, и оно уходит.

До этапа письмо бота «🎁 Пробный период завершён» за 30 дней не ушло ни разу: подписку в «истекла» до
обработчика переводил загрузчик внутри вебхука, монитор её уже не видел, а обработчик слал письмо панели
«❌ Подписка истекла. Продлите» без цены. Теперь письмо шлёт общая функция `notify_subscription_ended`, её
зовут вебхук, монитор и страховочный обход; повторы гасит отметка в `sent_notifications`.

Числа фикстур нарочно не совпадают с умолчаниями соседнего кода: пробный тариф 17, Team 23, «Базовый» 31.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, insert
from sqlalchemy.orm import Session

from app.database.models import AwareDateTime, Subscription, User
from app.services import monitoring_service as monitoring_module
from app.services.monitoring_service import ended_without_letter_query, is_quiet_night, monitoring_service
from app.services.notification_settings_service import NotificationSettingsService
from app.services.remnawave_webhook_service import RemnaWaveWebhookService


TRIAL_TARIFF_ID = 17
TEAM_TARIFF_ID = 23
PAID_TARIFF_ID = 31
USER_ID = 907
TELEGRAM_ID = 5207068834


def _user(*, balance: int = 0, user_id: int = USER_ID, telegram_id: int | None = TELEGRAM_ID, status: str = 'active'):
    return User(id=user_id, telegram_id=telegram_id, status=status, language='ru', balance_kopeks=balance)


def _sub(
    *,
    sid: int = 233,
    tariff_id: int | None = TRIAL_TARIFF_ID,
    is_trial: bool = True,
    status: str = 'expired',
    end: timedelta = timedelta(hours=-1),
):
    return Subscription(
        id=sid,
        user_id=USER_ID,
        tariff_id=tariff_id,
        is_trial=is_trial,
        status=status,
        end_date=datetime.now(UTC) + end,
    )


def _options(*, base: int = 1, cells: dict[tuple[int, int], int] | None = None, eligible: bool = True) -> dict:
    """Ответ кассы `build_purchase_options` в той же форме, что у настоящей."""
    if not eligible:
        return {'eligible': False, 'reason': 'eligible_tariff_count_not_one'}
    cells = cells or {(30, 1): 14900, (90, 1): 39900, (30, 2): 19900, (90, 2): 54900}
    periods = sorted({days for days, _ in cells})
    return {
        'eligible': True,
        'tariff': {'id': PAID_TARIFF_ID, 'base_device_limit': base},
        'price_matrix': [
            {
                'period_days': days,
                'prices': [
                    {'device_limit': devices, 'price_kopeks': price}
                    for (cell_days, devices), price in sorted(cells.items())
                    if cell_days == days
                ],
            }
            for days in periods
        ],
    }


@pytest.fixture
def marks(monkeypatch):
    """Отметки `sent_notifications` в памяти: тот же контракт «есть ли / записать», что у CRUD."""
    store: set[tuple[int, int, str]] = set()

    async def sent(_db, user_id, subscription_id, notification_type, days_before=None):
        return (user_id, subscription_id, notification_type) in store

    async def record(_db, user_id, subscription_id, notification_type, days_before=None, *, commit=True):
        store.add((user_id, subscription_id, notification_type))

    monkeypatch.setattr(monitoring_module, 'notification_sent', sent)
    monkeypatch.setattr(monitoring_module, 'record_notification', record)
    return store


@pytest.fixture
def env(monkeypatch, marks):
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    monkeypatch.setattr(monitoring_service, 'bot', bot)
    monkeypatch.setattr(
        monitoring_module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_TARIFF_ID))
    )
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', classmethod(lambda cls, key: True))
    cashier = AsyncMock(return_value=_options())
    monkeypatch.setattr('app.services.device_first_checkout_service.build_purchase_options', cashier)
    # Как на боевом (`.env`): минимальная доплата Platega — 1 ₽. Умолчание кода — 100 ₽.
    monkeypatch.setattr(monitoring_module.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    monkeypatch.setattr(type(monitoring_module.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(monitoring_module, 'is_quiet_night', lambda moment=None: False)
    logo = AsyncMock(return_value=SimpleNamespace(message_id=2))
    monkeypatch.setattr(monitoring_service, '_send_message_with_logo', logo)
    return SimpleNamespace(bot=bot, marks=marks, cashier=cashier, logo=logo)


def _webhook(monkeypatch) -> RemnaWaveWebhookService:
    service = RemnaWaveWebhookService(MagicMock())
    service._notify_user = AsyncMock()
    service._get_renew_keyboard = MagicMock(return_value=None)

    async def expire(_db, subscription):
        subscription.status = 'expired'
        return subscription

    monkeypatch.setattr('app.services.remnawave_webhook_service.expire_subscription', AsyncMock(side_effect=expire))
    return service


def _sent_text(env) -> str:
    return env.bot.send_message.await_args.kwargs['text']


# ---------------------------------------------------------------------------
# Вебхук user.expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_expired_gives_one_letter_with_price_and_one_mark(env, monkeypatch) -> None:
    """Загрузчик уже погасил пробный — обработчик всё равно пишет, ровно одно письмо; повтор события молчит."""
    service = _webhook(monkeypatch)
    user, subscription = _user(), _sub(status='expired')

    await service._handle_user_expired(AsyncMock(), user, subscription, {})
    await service._handle_user_expired(AsyncMock(), user, subscription, {})

    assert env.bot.send_message.await_count == 1
    text = _sent_text(env)
    assert text.startswith('🎁 <b>Пробный период завершён</b>')
    assert text.split('\n')[-2:] == ['1 месяц — 149 ₽', '3 месяца — 399 ₽']
    assert env.marks == {(USER_ID, 233, 'trial_expired')}
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_first_then_webhook_gives_one_letter(env, monkeypatch) -> None:
    """Монитор успел первым и написал — пришедшее следом событие видит отметку и молчит."""
    user, subscription = _user(), _sub(status='active')
    monkeypatch.setattr(monitoring_module, 'get_expired_subscriptions', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda _s: False)

    async def expire(_db, sub):
        sub.status = 'expired'
        return sub

    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', AsyncMock(side_effect=expire))
    monkeypatch.setattr(monitoring_service, '_process_grace_ended', AsyncMock())
    monkeypatch.setattr(monitoring_service, '_log_monitoring_event', AsyncMock())
    monkeypatch.setattr(monitoring_module.settings, 'GRACE_ENABLED', False)

    await monitoring_service._check_expired_subscriptions(AsyncMock())
    assert env.bot.send_message.await_count == 1

    service = _webhook(monkeypatch)
    await service._handle_user_expired(AsyncMock(), user, subscription, {})

    assert env.bot.send_message.await_count == 1
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['active', 'expired'])
async def test_event_with_a_future_end_date_neither_expires_nor_writes(env, monkeypatch, status) -> None:
    """Событие пришло по старой дате сразу после продления: подписку не гасим, «истекла» не пишем."""
    service = _webhook(monkeypatch)
    subscription = _sub(status=status, is_trial=False, tariff_id=PAID_TARIFF_ID, end=timedelta(days=29))

    await service._handle_user_expired(AsyncMock(), _user(), subscription, {})

    assert subscription.status == status
    if status == 'active':
        from app.services import remnawave_webhook_service as webhook_module

        webhook_module.expire_subscription.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()
    env.logo.assert_not_awaited()
    service._notify_user.assert_not_awaited()
    assert env.marks == set()


@pytest.mark.asyncio
async def test_team_with_a_trial_flag_gets_the_paid_letter_without_top_up_button(env, monkeypatch) -> None:
    """Пробный — по тарифу: друг на Team с флагом «пробный» получает «Подписка истекла», а не цену пробного."""
    service = _webhook(monkeypatch)
    subscription = _sub(status='expired', is_trial=True, tariff_id=TEAM_TARIFF_ID)

    await service._handle_user_expired(AsyncMock(), _user(), subscription, {})

    env.bot.send_message.assert_not_awaited()
    sent = env.logo.await_args.kwargs
    assert 'истекла' in sent['text']
    assert [button.text for row in sent['reply_markup'].inline_keyboard for button in row] == ['💎 Продлить подписку']
    assert env.marks == {(USER_ID, 233, 'subscription_expired')}


# ---------------------------------------------------------------------------
# Цена из кассы
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('balance', 'expected'),
    [
        (0, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
        (5000, '1 месяц — 149 ₽\nс вашими 50 ₽ на балансе — 99 ₽'),
        (14899, '1 месяц — 149 ₽\nс вашими 149 ₽ на балансе — 1 ₽'),
        (14900, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
        (60000, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
    ],
)
async def test_price_lines_come_from_the_cashier(env, balance, expected) -> None:
    lines = await monitoring_service._trial_price_lines(AsyncMock(), _user(balance=balance))
    assert lines == expected


@pytest.mark.asyncio
async def test_price_lines_use_the_base_device_cell(env) -> None:
    """Цена — на базовое число устройств тарифа, а не первая ячейка матрицы."""
    env.cashier.return_value = _options(base=2)
    lines = await monitoring_service._trial_price_lines(AsyncMock(), _user())
    assert lines == '1 месяц — 199 ₽\n3 месяца — 549 ₽'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'cashier',
    [
        AsyncMock(return_value=_options(eligible=False)),
        AsyncMock(side_effect=RuntimeError('касса упала')),
        AsyncMock(return_value=_options(cells={(30, 1): 14900})),
    ],
    ids=['не продаёт', 'упала', 'нет трёх месяцев'],
)
async def test_no_price_means_no_price_lines_never_a_fallback(env, cashier) -> None:
    """Касса цену не назвала — письмо без строк цены; запасные 990 ₽ (`PRICE_30_DAYS`) не подставляются."""
    env.cashier.side_effect = cashier.side_effect
    env.cashier.return_value = cashier.return_value

    await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub())

    text = _sent_text(env)
    assert text.endswith('Выберите тариф, чтобы продолжить пользоваться VPN.')
    assert '990' not in text and '₽' not in text


@pytest.mark.asyncio
async def test_the_card_shows_the_letter_that_is_sent(env) -> None:
    """Карточка и письмо берут текст из одного места: голова и строки цены — те же самые строки."""
    from app.cabinet.routes.admin_auto_messages import _text_facts

    await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub())

    facts = _text_facts('trial-expired')
    sent = _sent_text(env)
    assert sent.startswith(facts['text'].split('{')[0].rstrip())
    variants = {variant.text for insert in facts['text_inserts'] for variant in insert.variants}
    assert '1 месяц — {month_price}\n3 месяца — {quarter_price}' in variants
    assert sent.endswith(
        '1 месяц — {month_price}\n3 месяца — {quarter_price}'.format(month_price='149 ₽', quarter_price='399 ₽')
    )
    assert {marker.name for marker in facts['text_markers']} >= {'month_price', 'quarter_price', 'balance', 'top_up'}


# ---------------------------------------------------------------------------
# Ночь без звука — только пробному
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('moment', 'quiet'),
    [
        (datetime(2026, 10, 7, 0, 0, tzinfo=UTC) - timedelta(hours=3), True),  # 00:00 МСК
        (datetime(2026, 10, 7, 4, 59, 59, tzinfo=UTC), True),  # 07:59:59 МСК
        (datetime(2026, 10, 7, 5, 0, tzinfo=UTC), False),  # 08:00 МСК — по UTC было бы «ночь»
        (datetime(2026, 10, 6, 20, 59, tzinfo=UTC), False),  # 23:59 МСК
        (datetime(2026, 10, 6, 21, 30, tzinfo=UTC), True),  # 00:30 МСК — по UTC было бы «вечер»
    ],
)
def test_quiet_night_is_counted_in_moscow(moment, quiet) -> None:
    assert is_quiet_night(moment) is quiet


@pytest.mark.asyncio
@pytest.mark.parametrize('night', [True, False])
async def test_trial_letter_is_silent_only_at_night(env, monkeypatch, night) -> None:
    monkeypatch.setattr(monitoring_module, 'is_quiet_night', lambda moment=None: night)
    await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub())
    assert env.bot.send_message.await_args.kwargs['disable_notification'] is night


# ---------------------------------------------------------------------------
# «Истекла вчера» от панели
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_after_letter_from_the_panel_skips_the_trial_only(env, monkeypatch) -> None:
    """Пробному «💤 Подписка истекла вчера» не уходит — в тот же час наша скидка; платному и Team — как раньше."""
    service = _webhook(monkeypatch)
    day_after = {'_meta': {'expiration': 24}}

    await service._handle_user_expiration(AsyncMock(), _user(), _sub(), day_after)
    service._notify_user.assert_not_awaited()

    await service._handle_user_expiration(AsyncMock(), _user(), _sub(), {'_meta': {'expiration': -24}})
    assert service._notify_user.await_args.args[1] == 'WEBHOOK_SUB_EXPIRES_24H', 'до конца — пока панель (АП-2)'

    for tariff_id, flag in ((PAID_TARIFF_ID, False), (TEAM_TARIFF_ID, True)):
        service._notify_user.reset_mock()
        await service._handle_user_expiration(AsyncMock(), _user(), _sub(tariff_id=tariff_id, is_trial=flag), day_after)
        assert service._notify_user.await_args.args[1] == 'WEBHOOK_SUB_EXPIRED_24H_AGO'


# ---------------------------------------------------------------------------
# Выключатели, сбои, кому не пишем
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('off', ['trial_expired', 'subscription_expired'])
async def test_each_card_switch_silences_only_its_own_letter(env, monkeypatch, off) -> None:
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', classmethod(lambda cls, key: key != off))

    trial = await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub(sid=41))
    paid = await monitoring_service.notify_subscription_ended(
        AsyncMock(), _user(), _sub(sid=42, is_trial=False, tariff_id=PAID_TARIFF_ID)
    )

    assert (trial, paid) == ((False, True) if off == 'trial_expired' else (True, False))
    assert env.bot.send_message.await_count == int(trial)
    assert env.logo.await_count == int(paid)


@pytest.mark.asyncio
async def test_a_timeout_leaves_no_mark_so_the_sweep_retries(env) -> None:
    env.bot.send_message.side_effect = TimeoutError
    assert await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub()) is False
    assert env.marks == set()


@pytest.mark.asyncio
async def test_a_paid_letter_stuck_past_the_timeout_is_not_counted(env) -> None:
    env.logo.return_value = None  # `_send_message_with_logo` отдаёт None, когда отправка зависла
    subscription = _sub(is_trial=False, tariff_id=PAID_TARIFF_ID)
    assert await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), subscription) is False
    assert env.marks == set()


@pytest.mark.asyncio
async def test_a_blocked_bot_counts_as_delivered(env) -> None:
    env.bot.send_message.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=TELEGRAM_ID, text='x'), message='Forbidden: bot was blocked by the user'
    )
    assert await monitoring_service.notify_subscription_ended(AsyncMock(), _user(), _sub()) is True
    assert env.marks == {(USER_ID, 233, 'trial_expired')}


@pytest.mark.asyncio
@pytest.mark.parametrize('user', [_user(telegram_id=None), _user(status='blocked')], ids=['без Телеграма', 'blocked'])
async def test_nobody_to_write_to_means_no_letter_and_no_mark(env, user) -> None:
    assert await monitoring_service.notify_subscription_ended(AsyncMock(), user, _sub()) is False
    env.bot.send_message.assert_not_awaited()
    assert env.marks == set()


# ---------------------------------------------------------------------------
# Страховочный обход
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 9, 0, 0, 123456, tzinfo=UTC)


def _sweep_tables():
    """Минимальная схема тех же таблиц и колонок, что читает условие обхода, — на настоящем движке SQLite."""
    metadata = MetaData()
    users = Table(
        'users',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('telegram_id', Integer),
        Column('status', String),
    )
    tariffs = Table('tariffs', metadata, Column('id', Integer, primary_key=True), Column('is_daily', Boolean))
    subscriptions = Table(
        'subscriptions',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('user_id', Integer),
        Column('tariff_id', Integer),
        Column('status', String),
        Column('end_date', AwareDateTime()),
        Column('in_grace', Boolean),
    )
    sent = Table(
        'sent_notifications',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('subscription_id', Integer),
        Column('notification_type', String),
    )
    engine = create_engine('sqlite://')
    metadata.create_all(engine)
    return Session(engine), users, tariffs, subscriptions, sent


def test_the_sweep_picks_exactly_the_lost_letters() -> None:
    session, users, tariffs, subscriptions, sent = _sweep_tables()
    session.execute(
        insert(users),
        [
            {'id': 1, 'telegram_id': 111, 'status': 'active'},
            {'id': 2, 'telegram_id': None, 'status': 'active'},
            {'id': 3, 'telegram_id': 333, 'status': 'blocked'},
        ],
    )
    session.execute(insert(tariffs), [{'id': TRIAL_TARIFF_ID, 'is_daily': False}, {'id': 99, 'is_daily': True}])
    hour = timedelta(hours=1)
    rows = [
        # (id, user, tariff, status, end_date, in_grace, ожидается)
        (101, 1, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, True),
        (102, 1, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, False),  # отметка trial_expired есть
        (103, 1, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, True),  # отметка другого письма не в счёт
        (104, 1, TRIAL_TARIFF_ID, 'limited', NOW - 2 * hour, False, True),  # мина NN
        (105, 1, TRIAL_TARIFF_ID, 'active', NOW - hour, False, False),  # это дело монитора
        (106, 1, TRIAL_TARIFF_ID, 'expired', NOW - timedelta(minutes=5), False, False),  # вебхук ещё может прийти
        (107, 1, TRIAL_TARIFF_ID, 'expired', NOW - 7 * hour, False, False),  # старше окна
        (108, 1, PAID_TARIFF_ID, 'expired', NOW - hour, True, False),  # бонусные дни
        (109, 1, 99, 'expired', NOW - hour, False, False),  # суточный тариф
        (110, 2, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, False),  # без Телеграма
        (111, 3, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, False),  # заблокирован
        (112, 1, None, 'expired', NOW - hour, False, True),  # без тарифа
        (113, 1, TRIAL_TARIFF_ID, 'expired', NOW - timedelta(minutes=15), False, True),  # ровно 15 минут — да
        (114, 1, TRIAL_TARIFF_ID, 'expired', NOW - 6 * hour, False, False),  # ровно 6 часов — уже нет
        (115, 1, PAID_TARIFF_ID, 'expired', NOW - hour, False, False),  # отметка subscription_expired есть
        (116, 1, TRIAL_TARIFF_ID, 'disabled', NOW - hour, False, False),  # отключена — не «конец срока»
    ]
    session.execute(
        insert(subscriptions),
        [
            {'id': sid, 'user_id': uid, 'tariff_id': tid, 'status': st, 'end_date': end, 'in_grace': grace}
            for sid, uid, tid, st, end, grace, _ in rows
        ],
    )
    session.execute(
        insert(sent),
        [
            {'subscription_id': 102, 'notification_type': 'trial_expired'},
            {'subscription_id': 103, 'notification_type': 'trial_2h'},
            {'subscription_id': 115, 'notification_type': 'subscription_expired'},
        ],
    )
    session.commit()

    picked = session.execute(ended_without_letter_query(NOW)).scalars().all()

    assert picked == [sid for sid, *_, expected in rows if expected]


@pytest.mark.asyncio
async def test_the_sweep_expires_a_limited_trial_and_writes_to_everyone(monkeypatch) -> None:
    limited, expired = _sub(sid=41, status='limited'), _sub(sid=42, status='expired')
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [41, 42])))
    db.scalar = AsyncMock(side_effect=[limited, expired])
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=_user()))

    async def expire(_db, subscription):
        subscription.status = 'expired'
        return subscription

    expire_spy = AsyncMock(side_effect=expire)
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_spy)
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, 'notify_subscription_ended', notify)

    await monitoring_service._check_ended_without_letter(db)

    assert [call.args[1] for call in expire_spy.await_args_list] == [limited]
    assert [call.args[2] for call in notify.await_args_list] == [limited, expired]
    assert limited.status == 'expired'

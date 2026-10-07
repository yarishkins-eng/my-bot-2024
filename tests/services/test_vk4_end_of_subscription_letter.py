"""ВК-4 (АП-1): в минуту конца подписки — одно письмо от бота, и оно уходит.

До этапа письмо бота «🎁 Пробный период завершён» за 30 дней не ушло ни разу: подписку в «истекла» до
обработчика переводил загрузчик внутри вебхука, монитор её уже не видел, а обработчик слал письмо панели
«❌ Подписка истекла. Продлите» без цены. Теперь письмо шлёт общая функция `notify_subscription_ended`, её
зовут вебхук, монитор, конец бонусных дней и страховочный обход; повторы гасит отметка в `sent_notifications`.

Отметки пишет и читает настоящий CRUD на настоящем движке (SQLite в памяти): таблицы подписок, тарифов и отметок —
те же, что на боевом, у пользователей — только колонки, которые читает условие обхода. Числа фикстур нарочно не
совпадают с умолчаниями соседнего кода: пробный тариф 17, Team 23, «Базовый» 31.
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert, text
from sqlalchemy.orm import Session

from app.database.models import Base, SentNotification, Subscription, Tariff, User
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


def _users_table(metadata: MetaData) -> Table:
    """Пользователи — только колонки условия обхода: в настоящей таблице есть JSONB, SQLite его не создаст."""
    return Table(
        'users',
        metadata,
        Column('id', Integer, primary_key=True),
        Column('telegram_id', Integer),
        Column('status', String),
    )


def _create_schema(connection) -> None:
    _users_table(MetaData()).create(connection)
    # Связь тарифа с промогруппами грузится вместе с тарифом — без её таблиц не загрузится и подписка.
    for name in ('promo_groups', 'tariff_promo_groups'):
        Base.metadata.tables[name].create(connection)
    for table in (Tariff.__table__, Subscription.__table__, SentNotification.__table__):
        table.create(connection)


class _AsyncOverSync:
    """Асинхронная обёртка над обычной сессией SQLite: `conftest` подменяет `aiosqlite` заглушкой, а запросы
    исполняет настоящий движок — то есть условия проверяются, а не изображаются (образец — test_reconcile_pool_key)."""

    def __init__(self, session: Session) -> None:
        self.sync = session

    async def execute(self, statement, *args, **kwargs):
        return self.sync.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        return self.sync.scalar(statement, *args, **kwargs)

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    def add(self, instance) -> None:
        self.sync.add(instance)

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        with self.sync.begin_nested():
            yield


def _new_db() -> _AsyncOverSync:
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        _create_schema(connection)
    return _AsyncOverSync(Session(engine, expire_on_commit=False, autoflush=False))


@pytest.fixture
def db():
    return _new_db()


async def _marks(db) -> set[tuple[int, int, str]]:
    rows = await db.execute(text('SELECT user_id, subscription_id, notification_type FROM sent_notifications'))
    return {tuple(row) for row in rows.all()}


@pytest.fixture
def env(monkeypatch):
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
    return SimpleNamespace(bot=bot, cashier=cashier, logo=logo)


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


async def _notify(db, user, subscription) -> bool:
    return await monitoring_service.notify_subscription_ended(db, user, subscription, source='test')


# ---------------------------------------------------------------------------
# Вебхук user.expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_expired_gives_one_letter_with_price_and_one_mark(env, db, monkeypatch) -> None:
    """Загрузчик уже погасил пробный — обработчик всё равно пишет, ровно одно письмо; повтор события молчит."""
    service = _webhook(monkeypatch)
    user, subscription = _user(), _sub(status='expired')

    await service._handle_user_expired(db, user, subscription, {})
    await service._handle_user_expired(db, user, subscription, {})

    assert env.bot.send_message.await_count == 1
    text_ = _sent_text(env)
    assert text_.startswith('🎁 <b>Пробный период завершён</b>')
    assert text_.split('\n')[-2:] == ['1 месяц — 149 ₽', '3 месяца — 399 ₽']
    assert await _marks(db) == {(USER_ID, 233, 'trial_expired')}
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_first_then_webhook_gives_one_letter(env, db, monkeypatch) -> None:
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

    await monitoring_service._check_expired_subscriptions(db)
    assert env.bot.send_message.await_count == 1

    service = _webhook(monkeypatch)
    await service._handle_user_expired(db, user, subscription, {})

    assert env.bot.send_message.await_count == 1
    service._notify_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_revived_subscription_gets_its_next_letter(env, db) -> None:
    """Админ «оживил» подписку без продления — старая отметка про прошлый конец следующему письму не мешает.

    Отметки стирает только продление; «Активировать», дата вперёд и перенос даты из панели оставляют их. Без
    сравнения отметки со сроком следующий конец прошёл бы без письма (а письма панели больше нет).
    """
    user, subscription = _user(), _sub(status='expired')
    await db.execute(
        insert(SentNotification.__table__).values(
            user_id=USER_ID,
            subscription_id=233,
            notification_type='trial_expired',
            created_at=subscription.end_date - timedelta(days=30),
        )
    )
    await db.commit()

    assert await _notify(db, user, subscription) is True
    assert await _notify(db, user, subscription) is False, 'новая отметка обязана гасить повтор'

    assert env.bot.send_message.await_count == 1
    rows = (await db.execute(text('SELECT created_at FROM sent_notifications'))).all()
    assert len(rows) == 1, 'старая отметка заменена новой, а не легла второй'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['active', 'expired'])
async def test_event_with_a_future_end_date_neither_expires_nor_writes(env, db, monkeypatch, status) -> None:
    """Событие пришло по старой дате сразу после продления: подписку не гасим, «истекла» не пишем."""
    service = _webhook(monkeypatch)
    subscription = _sub(status=status, is_trial=False, tariff_id=PAID_TARIFF_ID, end=timedelta(days=29))

    await service._handle_user_expired(db, _user(), subscription, {})

    assert subscription.status == status
    if status == 'active':
        from app.services import remnawave_webhook_service as webhook_module

        webhook_module.expire_subscription.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()
    env.logo.assert_not_awaited()
    service._notify_user.assert_not_awaited()
    assert await _marks(db) == set()


@pytest.mark.asyncio
async def test_team_with_a_trial_flag_gets_the_paid_letter_without_top_up_button(env, db, monkeypatch) -> None:
    """Пробный — по тарифу: друг на Team с флагом «пробный» получает «Подписка истекла», а не цену пробного."""
    service = _webhook(monkeypatch)
    subscription = _sub(status='expired', is_trial=True, tariff_id=TEAM_TARIFF_ID)

    await service._handle_user_expired(db, _user(), subscription, {})

    env.bot.send_message.assert_not_awaited()
    sent = env.logo.await_args.kwargs
    assert 'истекла' in sent['text']
    assert [button.text for row in sent['reply_markup'].inline_keyboard for button in row] == ['💎 Продлить подписку']
    assert await _marks(db) == {(USER_ID, 233, 'subscription_expired')}


# ---------------------------------------------------------------------------
# Цена из кассы
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('balance', 'expected'),
    [
        (0, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
        (50, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),  # меньше рубля: «с вашими 0 ₽» не пишем
        (5000, '1 месяц — 149 ₽\nс вашими 50 ₽ на балансе — 99 ₽'),
        (4975, '1 месяц — 149 ₽\nс вашими 49 ₽ на балансе — 100 ₽'),  # 49 + 100 = 149: рубли вниз, доплата кассы
        (1490, '1 месяц — 149 ₽\nс вашими 14 ₽ на балансе — 135 ₽'),
        (14899, '1 месяц — 149 ₽\nс вашими 148 ₽ на балансе — 1 ₽'),
        (14900, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
        (60000, '1 месяц — 149 ₽\n3 месяца — 399 ₽'),
    ],
)
async def test_price_lines_come_from_the_cashier(env, db, balance, expected) -> None:
    assert await monitoring_service._trial_price_lines(db, _user(balance=balance)) == expected


@pytest.mark.asyncio
async def test_no_balance_line_when_the_cashier_would_not_offer_a_top_up(env, db, monkeypatch) -> None:
    """Минимум Platega не меньше цены месяца — касса доплату не предлагает, и письмо её не обещает."""
    monkeypatch.setattr(monitoring_module.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 20000)
    lines = await monitoring_service._trial_price_lines(db, _user(balance=5000))
    assert lines == '1 месяц — 149 ₽\n3 месяца — 399 ₽'


@pytest.mark.asyncio
async def test_price_lines_use_the_base_device_cell(env, db) -> None:
    """Цена — на базовое число устройств тарифа, а не первая ячейка матрицы."""
    env.cashier.return_value = _options(base=2)
    assert await monitoring_service._trial_price_lines(db, _user()) == '1 месяц — 199 ₽\n3 месяца — 549 ₽'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('return_value', 'side_effect'),
    [
        (_options(eligible=False), None),
        (None, RuntimeError('касса упала')),
        (_options(cells={(30, 1): 14900}), None),
        ({'eligible': True, 'tariff': {'base_device_limit': 1}, 'price_matrix': [{'period_days': 30}]}, None),
    ],
    ids=['не продаёт', 'упала', 'нет трёх месяцев', 'матрица без цен'],
)
async def test_no_price_means_no_price_lines_never_a_fallback(env, db, return_value, side_effect) -> None:
    """Касса цену не назвала — письмо без строк цены; запасные 990 ₽ (`PRICE_30_DAYS`) не подставляются."""
    env.cashier.return_value = return_value
    env.cashier.side_effect = side_effect

    await _notify(db, _user(), _sub())

    sent = _sent_text(env)
    assert sent.endswith('Выберите тариф, чтобы продолжить пользоваться VPN.')
    assert '990' not in sent and '₽' not in sent
    assert await _marks(db) == {(USER_ID, 233, 'trial_expired')}, 'письмо без цены всё равно ушло и отмечено'


@pytest.mark.asyncio
async def test_a_broken_price_template_still_sends_the_letter(env, db, monkeypatch) -> None:
    """Сломался шаблон строк цены — письмо уходит без них, а не пропадает целиком (мина KE)."""
    from app.localization.texts import get_texts

    real_t = type(get_texts('ru')).t

    def broken_t(self, key, default=None):
        return '1 месяц — {no_such_marker}' if key == 'TRIAL_EXPIRED_PRICE_LINES' else real_t(self, key, default)

    monkeypatch.setattr(type(get_texts('ru')), 't', broken_t)

    assert await _notify(db, _user(), _sub()) is True
    assert _sent_text(env).endswith('пользоваться VPN.')


@pytest.mark.asyncio
async def test_a_failed_cashier_query_leaves_the_session_usable(env, db) -> None:
    """Запрос кассы упал внутри транзакции — точка сохранения откатывает только его, отметка следом пишется."""

    async def failing_cashier(session, _user_):
        await session.execute(text('SELECT * FROM no_such_table'))

    env.cashier.side_effect = failing_cashier

    assert await _notify(db, _user(), _sub()) is True
    assert await _marks(db) == {(USER_ID, 233, 'trial_expired')}


@pytest.mark.asyncio
async def test_the_card_shows_the_letter_that_is_sent(env, db) -> None:
    """Карточка и письмо берут текст из одного места: голова и строки цены — те же самые строки."""
    from app.cabinet.routes.admin_auto_messages import _text_facts

    await _notify(db, _user(), _sub())

    facts = _text_facts('trial-expired')
    sent = _sent_text(env)
    assert sent.startswith(facts['text'].split('{')[0].rstrip())
    variants = {variant.text for insert_ in facts['text_inserts'] for variant in insert_.variants}
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
async def test_trial_letter_is_silent_only_at_night(env, db, monkeypatch, night) -> None:
    monkeypatch.setattr(monitoring_module, 'is_quiet_night', lambda moment=None: night)
    await _notify(db, _user(), _sub())
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
async def test_each_card_switch_silences_only_its_own_letter(env, db, monkeypatch, off) -> None:
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', classmethod(lambda cls, key: key != off))

    trial = await _notify(db, _user(), _sub(sid=41))
    paid = await _notify(db, _user(), _sub(sid=42, is_trial=False, tariff_id=PAID_TARIFF_ID))

    assert (trial, paid) == ((False, True) if off == 'trial_expired' else (True, False))
    assert env.bot.send_message.await_count == int(trial)
    assert env.logo.await_count == int(paid)


@pytest.mark.asyncio
async def test_a_timeout_leaves_no_mark_so_the_sweep_retries(env, db) -> None:
    env.bot.send_message.side_effect = TimeoutError
    assert await _notify(db, _user(), _sub()) is False
    assert await _marks(db) == set()


@pytest.mark.asyncio
async def test_a_paid_letter_stuck_past_the_timeout_is_not_counted(env, db) -> None:
    env.logo.return_value = None  # `_send_message_with_logo` отдаёт None, когда отправка зависла
    assert await _notify(db, _user(), _sub(is_trial=False, tariff_id=PAID_TARIFF_ID)) is False
    assert await _marks(db) == set()


@pytest.mark.asyncio
async def test_a_blocked_bot_counts_as_delivered(env, db) -> None:
    env.bot.send_message.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=TELEGRAM_ID, text='x'), message='Forbidden: bot was blocked by the user'
    )
    assert await _notify(db, _user(), _sub()) is True
    assert await _marks(db) == {(USER_ID, 233, 'trial_expired')}


class _Log:
    """Подменяет журнал службы и запоминает строки: (уровень, событие, поля)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        return lambda event, *args, **fields: self.calls.append((level, event, fields))


@pytest.mark.asyncio
async def test_every_attempt_leaves_a_log_line_with_its_outcome(env, db, monkeypatch) -> None:
    """Приёмка на боевом считает по журналу: у каждого исхода строка с источником, подпиской и сроком."""
    log = _Log()
    monkeypatch.setattr(monitoring_module, 'logger', log)
    subscription = _sub()

    assert await _notify(db, _user(), subscription) is True
    assert await _notify(db, _user(), subscription) is False
    env.bot.send_message.side_effect = TimeoutError
    assert await _notify(db, _user(), _sub(sid=51)) is False
    assert await _notify(db, _user(telegram_id=None), _sub(sid=52)) is False
    monkeypatch.setattr(NotificationSettingsService, 'is_enabled', classmethod(lambda cls, key: False))
    assert await _notify(db, _user(), _sub(sid=53)) is False

    lines = [(event, fields) for level, event, fields in log.calls if event.startswith('Письмо о конце подписки')]
    assert [(event, fields.get('reason'), fields['subscription_id']) for event, fields in lines] == [
        ('Письмо о конце подписки отправлено', None, 233),
        ('Письмо о конце подписки не отправлено', 'already_sent', 233),
        ('Письмо о конце подписки не отправлено', 'not_delivered', 51),
        ('Письмо о конце подписки не отправлено', 'no_telegram', 52),
        ('Письмо о конце подписки не отправлено', 'switched_off', 53),
    ]
    assert all(fields['source'] == 'test' and fields['end_date'] is not None for _, fields in lines)


_HICCUP_METHOD = SendMessage(chat_id=TELEGRAM_ID, text='x')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        TelegramRetryAfter(method=_HICCUP_METHOD, message='Too Many Requests: retry after 5', retry_after=5),
        TelegramServerError(method=_HICCUP_METHOD, message='Bad Gateway'),
        TelegramNetworkError(method=_HICCUP_METHOD, message='Request timeout error'),
    ],
    ids=['429', '5xx', 'сеть'],
)
async def test_a_telegram_hiccup_is_a_warning_not_an_admin_alert(env, db, monkeypatch, error) -> None:
    """Сбой связи с Телеграмом не уходит в админ-чат ошибкой: отметки нет, письмо допишет обход."""
    log = _Log()
    monkeypatch.setattr(monitoring_module, 'logger', log)
    env.bot.send_message.side_effect = error
    env.logo.side_effect = error

    assert await _notify(db, _user(), _sub(sid=61)) is False
    assert await _notify(db, _user(), _sub(sid=62, is_trial=False, tariff_id=PAID_TARIFF_ID)) is False

    assert await _marks(db) == set()
    assert [event for level, event, _ in log.calls if level in {'error', 'exception', 'critical'}] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('user', [_user(telegram_id=None), _user(status='blocked')], ids=['без Телеграма', 'blocked'])
async def test_nobody_to_write_to_means_no_letter_and_no_mark(env, db, user) -> None:
    assert await _notify(db, user, _sub()) is False
    env.bot.send_message.assert_not_awaited()
    assert await _marks(db) == set()


@pytest.mark.asyncio
async def test_the_trial_sender_without_telegram_does_not_report_delivery(env) -> None:
    assert await monitoring_service._send_trial_expired_notification(_user(telegram_id=None)) is False


@pytest.mark.asyncio
async def test_another_live_subscription_silences_the_end_letter_in_multi_tariff_mode(env, db, monkeypatch) -> None:
    """Многотарифный режим: у человека есть другая живая подписка — сервис не прервался, письма о конце нет."""
    monkeypatch.setattr(type(monitoring_module.settings), 'is_multi_tariff_enabled', lambda self: True)
    other_live = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, '_has_other_active_subscription', other_live)

    assert await _notify(db, _user(), _sub()) is False
    env.bot.send_message.assert_not_awaited()
    other_live.return_value = False
    assert await _notify(db, _user(), _sub()) is True


@pytest.mark.asyncio
async def test_the_end_of_bonus_days_goes_through_the_one_letter(env, monkeypatch) -> None:
    """Конец бонусных дней — та же функция письма о конце, а не отдельное письмо мимо отметки и выключателя."""
    user = _user()
    subscription = SimpleNamespace(id=1, user_id=USER_ID, user=user, status='expired', in_grace=True, tariff=None)
    monkeypatch.setattr(monitoring_module, 'get_subscriptions_grace_ended', AsyncMock(return_value=[subscription]))
    monkeypatch.setattr(monitoring_service.subscription_service, 'push_panel_state', AsyncMock(return_value=True))
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, 'notify_subscription_ended', notify)

    await monitoring_service._process_grace_ended(AsyncMock())

    assert notify.await_args.args[2] is subscription
    assert notify.await_args.kwargs['source'] == 'grace'


# ---------------------------------------------------------------------------
# Страховочный обход
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 9, 0, 0, 123456, tzinfo=UTC)


def _sweep_session() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        _create_schema(connection)
    return Session(engine)


def _seed(rows, marks):
    """Строки в настоящие таблицы: у каждой подписки свой пользователь (уникальность «пользователь + тариф» в SQLite
    строже, чем частичный индекс PostgreSQL). Строка: (подписка, тариф, статус, конец, бонусные дни, Телеграм,
    статус пользователя, …); отметка: (подписка, тип, когда создана)."""
    users = Table('users', MetaData(), Column('id', Integer), Column('telegram_id', Integer), Column('status', String))
    statements = [
        insert(users).values(
            [{'id': sid, 'telegram_id': tg, 'status': user_status} for sid, _, _, _, _, tg, user_status, *_ in rows]
        ),
        insert(Tariff.__table__).values(
            [
                {'id': TRIAL_TARIFF_ID, 'name': 'Пробный', 'is_daily': False},
                {'id': PAID_TARIFF_ID, 'name': 'Базовый', 'is_daily': False},
                {'id': 99, 'name': 'Суточный', 'is_daily': True},
            ]
        ),
        insert(Subscription.__table__).values(
            [
                {
                    'id': sid,
                    'user_id': sid,
                    'tariff_id': tid,
                    'status': st,
                    'end_date': end,
                    'in_grace': grace,
                    'is_trial': tid == TRIAL_TARIFF_ID,
                    'remnawave_short_id': f's{sid}',
                }
                for sid, tid, st, end, grace, *_ in rows
            ]
        ),
    ]
    if marks:
        statements.append(
            insert(SentNotification.__table__).values(
                [
                    {'user_id': sid, 'subscription_id': sid, 'notification_type': kind, 'created_at': created}
                    for sid, kind, created in marks
                ]
            )
        )
    return statements


def test_the_sweep_picks_exactly_the_lost_letters() -> None:
    """Условие обхода вычисляется настоящим движком: кого обход возьмёт, а кого нет, и почему."""
    session = _sweep_session()
    hour = timedelta(hours=1)
    rows = [
        # (подписка, тариф, статус, конец, бонусные дни, Телеграм, статус пользователя, ожидается)
        (101, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, 111, 'active', True),
        (102, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, 111, 'active', False),  # отметка этого конца есть
        (103, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, 111, 'active', True),  # отметка другого письма не в счёт
        (104, TRIAL_TARIFF_ID, 'limited', NOW - 2 * hour, False, 111, 'active', True),  # мина NN
        (105, TRIAL_TARIFF_ID, 'active', NOW - hour, False, 111, 'active', False),  # это дело монитора
        (106, TRIAL_TARIFF_ID, 'expired', NOW - timedelta(minutes=5), False, 111, 'active', False),  # вебхук ещё может
        (107, TRIAL_TARIFF_ID, 'expired', NOW - 7 * hour, False, 111, 'active', False),  # старше окна
        (108, PAID_TARIFF_ID, 'expired', NOW - hour, True, 111, 'active', False),  # бонусные дни
        (109, 99, 'expired', NOW - hour, False, 111, 'active', False),  # суточный тариф
        (110, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, None, 'active', False),  # без Телеграма
        (111, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, 333, 'blocked', False),  # заблокирован
        (112, None, 'expired', NOW - hour, False, 111, 'active', True),  # без тарифа
        (113, TRIAL_TARIFF_ID, 'expired', NOW - timedelta(minutes=15), False, 111, 'active', True),  # ровно 15 мин — да
        (114, TRIAL_TARIFF_ID, 'expired', NOW - 6 * hour, False, 111, 'active', False),  # ровно 6 часов — уже нет
        (115, PAID_TARIFF_ID, 'expired', NOW - hour, False, 111, 'active', False),  # отметка subscription_expired есть
        (116, TRIAL_TARIFF_ID, 'disabled', NOW - hour, False, 111, 'active', False),  # отключена — не «конец срока»
        (117, TRIAL_TARIFF_ID, 'expired', NOW - hour, False, 111, 'active', True),  # отметка прошлого конца (оживили)
    ]
    marks = [
        (102, 'trial_expired', NOW - hour + timedelta(seconds=7)),
        (103, 'trial_2h', NOW - 7 * hour),
        (115, 'subscription_expired', NOW - hour + timedelta(minutes=1)),
        (117, 'trial_expired', NOW - timedelta(days=31)),
    ]
    for statement in _seed(rows, marks):
        session.execute(statement)
    session.commit()

    picked = session.execute(ended_without_letter_query(NOW)).scalars().all()

    assert picked == [sid for sid, *_, expected in rows if expected]


@pytest.fixture
def sweep_db():
    return _new_db()


@pytest.mark.asyncio
async def test_the_sweep_expires_a_limited_trial_writes_once_and_skips_a_renewed_one(
    env, sweep_db, monkeypatch
) -> None:
    """Обход целиком на настоящем движке: «лимит» с вышедшим сроком гасит условной записью и пишет; продлённую не
    трогает; повторный обход молчит."""
    now = datetime.now(UTC)
    rows = [
        (41, TRIAL_TARIFF_ID, 'limited', now - timedelta(hours=2), False, 111, 'active'),
        (42, TRIAL_TARIFF_ID, 'expired', now - timedelta(hours=1), False, 222, 'active'),
    ]
    for statement in _seed(rows, []):
        await sweep_db.execute(statement)
    await sweep_db.commit()
    monkeypatch.setattr(
        monitoring_module, 'get_user_by_id', AsyncMock(side_effect=lambda _db, uid: _user(user_id=uid, telegram_id=uid))
    )

    await monitoring_service._check_ended_without_letter(sweep_db)

    statuses = dict((await sweep_db.execute(text('SELECT id, status FROM subscriptions'))).all())
    assert statuses == {41: 'expired', 42: 'expired'}
    assert await _marks(sweep_db) == {(41, 41, 'trial_expired'), (42, 42, 'trial_expired')}
    assert env.bot.send_message.await_count == 2

    await monitoring_service._check_ended_without_letter(sweep_db)
    assert env.bot.send_message.await_count == 2, 'второй обход видит отметки и молчит'


@pytest.mark.asyncio
async def test_the_sweep_leaves_a_limited_subscription_renewed_after_the_selection(env, sweep_db, monkeypatch) -> None:
    """Продление между выборкой и записью: подписка остаётся «активна» со своим сроком, письма нет (мина NO)."""
    now = datetime.now(UTC)
    for statement in _seed([(43, PAID_TARIFF_ID, 'limited', now - timedelta(hours=2), False, 111, 'active')], []):
        await sweep_db.execute(statement)
    await sweep_db.commit()
    monkeypatch.setattr(
        monitoring_module, 'get_user_by_id', AsyncMock(side_effect=lambda _db, uid: _user(user_id=uid, telegram_id=uid))
    )

    real_execute = sweep_db.execute
    renewed = {'done': False}

    async def execute_and_renew(statement, *args, **kwargs):
        result = await real_execute(statement, *args, **kwargs)
        if not renewed['done']:  # первый вызов — выборка обхода; сразу после неё — продление
            renewed['done'] = True
            await real_execute(
                text("UPDATE subscriptions SET status = 'active', end_date = :end WHERE id = 43"),
                {'end': (now + timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S.%f')},
            )
        return result

    monkeypatch.setattr(sweep_db, 'execute', execute_and_renew)

    await monitoring_service._check_ended_without_letter(sweep_db)

    status = (await real_execute(text('SELECT status FROM subscriptions WHERE id = 43'))).scalar()
    assert status == 'active'
    env.logo.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_broken_subscription_does_not_stop_the_sweep(env, monkeypatch) -> None:
    """Сбой на одной подписке — откат и следующая, а не конец обхода для всех."""
    db_ = AsyncMock()
    db_.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [51, 52])))
    good = _sub(sid=52, status='expired')
    db_.scalar = AsyncMock(side_effect=[RuntimeError('строку удалили'), good])
    monkeypatch.setattr(monitoring_module, 'get_user_by_id', AsyncMock(return_value=_user()))
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(monitoring_service, 'notify_subscription_ended', notify)

    await monitoring_service._check_ended_without_letter(db_)

    db_.rollback.assert_awaited()
    assert [call.args[2] for call in notify.await_args_list] == [good]
    assert notify.await_args.kwargs['source'] == 'sweep'


def test_the_sweep_runs_in_the_hourly_cycle_before_the_monitor_pass() -> None:
    """Обход подключён к часовому циклу и идёт ДО прохода монитора: неудачное письмо монитора обход повторит через
    час, а не через секунды в том же цикле."""
    source = pathlib.Path(monitoring_module.__file__).read_text(encoding='utf-8')
    cycle = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == '_monitoring_cycle'
    )
    calls = [
        node.func.attr
        for node in sorted(
            (node for node in ast.walk(cycle) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        if node.func.attr.startswith('_check_')
    ]
    assert '_check_ended_without_letter' in calls
    assert calls.index('_check_ended_without_letter') < calls.index('_check_expired_subscriptions')

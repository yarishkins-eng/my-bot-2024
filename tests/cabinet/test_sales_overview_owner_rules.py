"""Экран «Статистика продаж» по правилам владельца (этап СП-1, 27.09.2026) — считается НАСТОЯЩИМ движком.

Правила владельца: Team не попадает никуда; пробный — только тариф «Пробный»; платит — только кто платил
деньгами; стенды и удалённые — не люди; деньги — как выписка Platega (со стендами, как утреннее письмо).
Запросы идут на SQLite в памяти с минимальной схемой тех же таблиц (образец —
`tests/services/test_reporting_service_owner_report.py`): каждый фильтр WHERE здесь ВЫЧИСЛЯЕТСЯ.
Патчатся только тарифы (`get_all_tariffs`, `get_trial_tariff`) и список стендов из `.env`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.cabinet.routes import admin_sales_stats as module


ENV_STAND_TELEGRAM_ID = 777
# 27.09.2026 11:30 МСК — «сейчас» всех сценариев
NOW = datetime(2026, 9, 27, 8, 30, tzinfo=UTC)
LONG_AGO = '2026-06-15 12:00:00.000000'
AUG_10 = '2026-08-10 12:00:00.000000'
SEP_05 = '2026-09-05 12:00:00.000000'
SEP_20 = '2026-09-20 12:00:00.000000'
# границы сентября по МСК в UTC: 31.08 21:00 UTC = 00:00 МСК 01.09 (внутри), 31.08 20:59 UTC = 23:59 МСК 31.08 (снаружи)
SEP_FIRST_MIDNIGHT = '2026-08-31 21:00:00.000000'
AUG_LAST_MINUTE = '2026-08-31 20:59:00.000000'

TARIFFS = [
    SimpleNamespace(id=3, is_free=False, is_trial_available=False, is_active=True),  # Базовый — платный
    SimpleNamespace(id=4, is_free=True, is_trial_available=False, is_active=True),  # Team — бесплатный, друзья
    # Пробный: на боевом цены `{"5": 0}` → `is_free` ИСТИНА. Голый `is_free` записал бы пробных в Team
    SimpleNamespace(id=5, is_free=True, is_trial_available=True, is_active=False),
]


async def _tariffs(db, *, include_inactive: bool = False, **_kwargs):
    return [tariff for tariff in TARIFFS if include_inactive or tariff.is_active]


class _AsyncOverSync:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)


def _schema() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(
            text(
                'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, status TEXT, '
                'test_account_enabled BOOLEAN, first_name TEXT, last_name TEXT, username TEXT, '
                'balance_kopeks INTEGER, created_at TIMESTAMP)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, '
                'amount_kopeks INTEGER, payment_method TEXT, description TEXT, is_completed BOOLEAN, '
                'created_at TIMESTAMP)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, tariff_id INTEGER, '
                'is_trial BOOLEAN, status TEXT, end_date TIMESTAMP, created_at TIMESTAMP, autopay_enabled BOOLEAN)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE subscription_events (id INTEGER PRIMARY KEY, user_id INTEGER, subscription_id INTEGER, '
                'event_type TEXT, message TEXT, extra JSON, occurred_at TIMESTAMP)'
            )
        )
        c.execute(text('CREATE TABLE tariffs (id INTEGER PRIMARY KEY, name TEXT)'))
        c.execute(
            text(
                'CREATE TABLE advertising_campaigns (id INTEGER PRIMARY KEY, name TEXT, ad_spend_kopeks INTEGER, '
                'created_at TIMESTAMP)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE guest_purchases (id INTEGER PRIMARY KEY, buyer_user_id INTEGER, user_id INTEGER, '
                'is_gift BOOLEAN, payment_method TEXT, amount_kopeks INTEGER, paid_at TIMESTAMP)'
            )
        )
        c.execute(text("INSERT INTO tariffs (id, name) VALUES (3, 'Базовый'), (4, 'Team'), (5, '⏰Пробный')"))
    return Session(engine)


class _Seed:
    def __init__(self, session: Session) -> None:
        self.s = session
        self._next_user = 100

    def user(
        self,
        *,
        created_at: str = LONG_AGO,
        status: str | None = 'active',
        telegram_id: int | None = None,
        test_account_enabled: bool | None = None,
        name: str | None = None,
        username: str | None = None,
        balance: int = 0,
    ) -> int:
        self._next_user += 1
        self.s.execute(
            text(
                'INSERT INTO users (id, telegram_id, status, test_account_enabled, first_name, username, '
                'balance_kopeks, created_at) VALUES (:id, :tg, :st, :te, :fn, :un, :b, :c)'
            ),
            {
                'id': self._next_user,
                'tg': telegram_id or self._next_user * 10,
                'st': status,
                'te': test_account_enabled,
                'fn': name,
                'un': username,
                'b': balance,
                'c': created_at,
            },
        )
        return self._next_user

    def tx(
        self,
        user_id: int,
        tx_type: str,
        amount: int,
        *,
        method: str | None,
        description: str = '',
        at: str = SEP_05,
        completed: bool = True,
    ) -> None:
        self.s.execute(
            text(
                'INSERT INTO transactions (user_id, type, amount_kopeks, payment_method, description, is_completed, '
                'created_at) VALUES (:u, :t, :a, :m, :d, :c, :at)'
            ),
            {'u': user_id, 't': tx_type, 'a': amount, 'm': method, 'd': description, 'c': completed, 'at': at},
        )

    def paid_card(self, user_id: int, amount: int = 14900, *, at: str = SEP_05) -> None:
        """Покупка картой с кассы: приход + списание тем же временем."""
        self.tx(user_id, 'provider_receipt', amount, method='platega', description='Оплата картой', at=at)
        self.tx(user_id, 'subscription_payment', -amount, method='platega', description='Оплата подписки', at=at)

    def sub(
        self,
        user_id: int,
        *,
        tariff_id: int | None,
        is_trial: bool | None,
        end: str,
        status: str = 'active',
        created_at: str = LONG_AGO,
        autopay: bool = False,
    ) -> None:
        self.s.execute(
            text(
                'INSERT INTO subscriptions (user_id, tariff_id, is_trial, status, end_date, created_at, '
                'autopay_enabled) VALUES (:u, :t, :tr, :st, :e, :c, :a)'
            ),
            {'u': user_id, 't': tariff_id, 'tr': is_trial, 'st': status, 'e': end, 'c': created_at, 'a': autopay},
        )

    def activation(self, user_id: int, *, at: str) -> None:
        self.s.execute(
            text(
                'INSERT INTO subscription_events (user_id, event_type, message, occurred_at) '
                "VALUES (:u, 'activation', 'Trial activation', :at)"
            ),
            {'u': user_id, 'at': at},
        )


def _patches():
    return (
        patch.object(module, 'get_all_tariffs', _tariffs),
        patch.object(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({ENV_STAND_TELEGRAM_ID})),
    )


async def _call(session: Session, fn, *args):
    first, second, third = _patches()
    with first, second, third:
        db = _AsyncOverSync(session)
        rules = await module._owner_rules(db)
        return await fn(db, rules, *args)


# ---------- СП-1.1: окно периода в сутках МСК ----------


def _msk(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=module._MSK).astimezone(UTC)


def test_yesterday_is_the_previous_moscow_day_and_compares_with_the_day_before() -> None:
    window = module._sales_window('yesterday', None, None, NOW)
    assert (window.start, window.end) == (_msk(2026, 9, 26), _msk(2026, 9, 27))
    assert (window.previous_start, window.previous_end) == (_msk(2026, 9, 25), _msk(2026, 9, 26))


def test_this_month_runs_to_now_and_compares_with_the_same_days_of_last_month_to_the_same_hour() -> None:
    window = module._sales_window('this_month', None, None, NOW)
    assert (window.start, window.end) == (_msk(2026, 9, 1), NOW)
    assert (window.previous_start, window.previous_end) == (_msk(2026, 8, 1), _msk(2026, 8, 27, 11, 30))


@pytest.mark.parametrize(
    ('now', 'start', 'previous_start', 'previous_end'),
    [
        # 31-е после 30-дневного месяца: сравнение — весь сентябрь, не дальше его конца (и без ValueError)
        (_msk(2026, 10, 31, 10), _msk(2026, 10, 1), _msk(2026, 9, 1), _msk(2026, 10, 1)),
        # 30 марта: февраль короче — сравнение обрезано по 1 марта
        (_msk(2027, 3, 30, 10), _msk(2027, 3, 1), _msk(2027, 2, 1), _msk(2027, 3, 1)),
        # середина января: прошлый месяц — декабрь прошлого года
        (_msk(2027, 1, 15, 12), _msk(2027, 1, 1), _msk(2026, 12, 1), _msk(2026, 12, 15, 12)),
        # полночь 1-го по МСК — окно пустое, сравнение тоже пустое, но не отрицательное
        (_msk(2027, 1, 1), _msk(2027, 1, 1), _msk(2026, 12, 1), _msk(2026, 12, 1)),
    ],
)
def test_this_month_edges(now, start, previous_start, previous_end) -> None:
    window = module._sales_window('this_month', None, None, now)
    assert (window.start, window.end) == (start, now)
    assert (window.previous_start, window.previous_end) == (previous_start, previous_end)


def test_last_month_is_the_whole_previous_moscow_month() -> None:
    window = module._sales_window('last_month', None, None, NOW)
    assert (window.start, window.end) == (_msk(2026, 8, 1), _msk(2026, 9, 1))
    assert (window.previous_start, window.previous_end) == (_msk(2026, 7, 1), _msk(2026, 8, 1))


@pytest.mark.parametrize(
    ('now', 'start', 'end', 'previous_start'),
    [
        # 1 октября: сентябрь (30 дней) сравнивается с ЦЕЛЫМ августом (31 день), а не с «1–30 августа»
        (_msk(2026, 10, 1, 10), _msk(2026, 9, 1), _msk(2026, 10, 1), _msk(2026, 8, 1)),
        # 2 марта: февраль сравнивается с целым январём
        (_msk(2027, 3, 2, 10), _msk(2027, 2, 1), _msk(2027, 3, 1), _msk(2027, 1, 1)),
    ],
)
def test_last_month_compares_with_the_whole_month_before(now, start, end, previous_start) -> None:
    window = module._sales_window('last_month', None, None, now)
    assert (window.start, window.end) == (start, end)
    assert (window.previous_start, window.previous_end) == (previous_start, start)


def test_custom_window_that_reaches_now_compares_with_the_same_hour_a_day_earlier() -> None:
    window = module._sales_window('custom', '2026-09-27', '2026-09-27', NOW)
    assert (window.start, window.end) == (_msk(2026, 9, 27), NOW)
    assert (window.previous_start, window.previous_end) == (_msk(2026, 9, 26), NOW - timedelta(days=1))


@pytest.mark.parametrize(('start', 'end'), [('0001-01-01', '0001-01-02'), ('9999-12-30', '9999-12-31')])
def test_custom_dates_at_the_edge_of_the_calendar_are_a_400_not_a_500(start, end) -> None:
    with pytest.raises(HTTPException) as error:
        module._sales_window('custom', start, end, NOW)
    assert error.value.status_code == 400


def test_seven_days_is_today_and_six_before_compared_with_the_same_window_a_week_earlier() -> None:
    window = module._sales_window('7d', None, None, NOW)
    assert (window.start, window.end) == (_msk(2026, 9, 21), NOW)
    assert (window.previous_start, window.previous_end) == (_msk(2026, 9, 14), NOW - timedelta(days=7))


def test_all_time_has_no_comparison() -> None:
    window = module._sales_window('all', None, None, NOW)
    assert window.end == NOW and window.previous_start is None and window.previous_end is None


def test_custom_days_are_inclusive_moscow_days_capped_by_now() -> None:
    window = module._sales_window('custom', '2026-08-01', '2026-08-31', NOW)
    assert (window.start, window.end) == (_msk(2026, 8, 1), _msk(2026, 9, 1))
    assert (window.previous_start, window.previous_end) == (_msk(2026, 7, 1), _msk(2026, 8, 1))
    future = module._sales_window('custom', '2026-10-01', '2026-10-05', NOW)
    assert future.start == future.end  # будущее — пустое окно, а не отрицательное


@pytest.mark.parametrize(
    ('period', 'start', 'end'),
    [('custom', 'x', '2026-08-01'), ('custom', '2026-09-02', '2026-09-01'), ('week', None, None)],
)
def test_bad_period_is_a_400(period, start, end) -> None:
    with pytest.raises(HTTPException) as error:
        module._sales_window(period, start, end, NOW)
    assert error.value.status_code == 400


# ---------- СП-1.1: кто человек и чьи покупки ----------


@pytest.mark.asyncio
async def test_team_is_the_free_tariff_that_is_not_the_trial_one() -> None:
    first, second, third = _patches()
    with first, second, third:
        rules = await module._owner_rules(_AsyncOverSync(_schema()))
    assert rules.team_tariff_ids == (4,)
    assert rules.not_paid_tariff_ids == (4, 5)


@pytest.mark.asyncio
async def test_team_friend_with_a_live_paid_row_is_paying_but_not_a_person_known_divergence() -> None:
    """Ред. 3 п.14 (критик W2-14), мина NU: «Платят» отсекает Team по ПОДПИСКЕ, правило экрана — по ЧЕЛОВЕКУ.

    Человек с любой строкой Team и живой платной строкой в «Платят» есть, а в покупках, списках и когорте — нет.
    На боевом 27.09 таких 0. Сторож держит расхождение на виду: поменяется одна сторона — тест покраснеет, и
    решать придётся осознанно, а не узнать из спора «почему Платят 68, а покупателей меньше».
    """
    session = _schema()
    seed = _Seed(session)
    both = seed.user()
    seed.sub(both, tariff_id=4, is_trial=False, end='2031-12-21 00:00:00.000000')
    seed.sub(both, tariff_id=3, is_trial=False, end='2099-01-01 00:00:00.000000')
    seed.paid_card(both, at=SEP_05)
    session.commit()

    first, second, third = _patches()
    with (
        first,
        second,
        third,
        patch('app.utils.user_utils.get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch('app.utils.user_utils.get_all_tariffs', _tariffs),
    ):
        db = _AsyncOverSync(session)
        counts = await module.count_trial_and_paying_users(db)
        purchases, first_buys = await module._payer_purchases(db, await module._owner_rules(db))

    assert counts == {'on_trial': 0, 'paying': 1}
    assert (purchases, first_buys) == ([], {})


@pytest.mark.asyncio
async def test_purchases_count_payers_only_and_skip_team_stands_deleted_and_bonus_buyers() -> None:
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.paid_card(buyer, at=SEP_05)
    seed.tx(buyer, 'subscription_payment', -4166, method='balance', description='Покупка доп. устройств: 1', at=SEP_05)
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Продление подписки', at=SEP_20)
    seed.tx(buyer, 'subscription_payment', 0, method='balance', description='Смена тарифа администратором на Team')
    # друг из Team платил деньгами 100 ₽ — всё равно не покупатель (правило владельца: Team — никуда)
    friend = seed.user()
    seed.sub(friend, tariff_id=4, is_trial=True, end='2031-12-21 00:00:00.000000')
    seed.paid_card(friend, 10000)
    # стенд по галке, стенд по `.env`, удалённый — не люди
    flagged = seed.user(test_account_enabled=True)
    seed.paid_card(flagged)
    env_stand = seed.user(telegram_id=ENV_STAND_TELEGRAM_ID)
    seed.paid_card(env_stand)
    erased = seed.user(status='deleted')
    seed.paid_card(erased)
    # купил только бонусами (деньгами не платил никогда) — не продажа
    bonus_only = seed.user()
    seed.tx(bonus_only, 'deposit', 5000, method=None, description='Бонус за регистрацию')
    seed.tx(bonus_only, 'subscription_payment', -5000, method='balance', description='Оплата подписки с баланса')
    # легаси-человек без статуса — человек
    legacy = seed.user(status=None)
    seed.paid_card(legacy)
    session.commit()

    purchases, first = await _call(session, module._payer_purchases)

    assert sorted({purchase.user_id for purchase in purchases}) == [buyer, legacy]
    assert [(p.amount_kopeks, p.is_addon) for p in purchases if p.user_id == buyer] == [
        (14900, False),
        (4166, True),
        (14900, False),
    ]
    assert first[buyer].at.replace(tzinfo=None) == datetime(2026, 9, 5, 12)
    assert first[buyer].amount_kopeks == 14900 and not first[buyer].is_addon


@pytest.mark.asyncio
async def test_first_purchase_is_never_an_add_on_and_ties_break_by_id() -> None:
    session = _schema()
    seed = _Seed(session)
    user = seed.user()
    seed.tx(user, 'deposit', 30000, method='platega', at=AUG_10)
    seed.tx(user, 'subscription_payment', -4166, method='balance', description='Покупка 1 доп. устройств', at=SEP_05)
    seed.tx(user, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса', at=SEP_05)
    seed.tx(user, 'subscription_payment', -39900, method='balance', description='Оплата подписки с баланса', at=SEP_05)
    session.commit()

    _, first = await _call(session, module._payer_purchases)

    assert first[user].amount_kopeks == 14900  # докупка раньше по номеру, но первой покупкой подписки не бывает


@pytest.mark.asyncio
async def test_trial_start_survives_the_purchase_that_rewrites_the_row() -> None:
    session = _schema()
    seed = _Seed(session)
    converted = seed.user()
    # строка подписки уже ПЕРЕПИСАНА покупкой: тариф платный, флага нет — пробный виден только по событию
    seed.sub(converted, tariff_id=3, is_trial=False, end='2026-10-20 00:00:00.000000', created_at=SEP_05)
    seed.activation(converted, at=SEP_05)
    no_event = seed.user()
    seed.sub(no_event, tariff_id=5, is_trial=True, end='2026-09-23 12:00:00.000000', created_at=SEP_20)
    abandoned = seed.user()
    seed.sub(abandoned, tariff_id=5, is_trial=True, end=SEP_20, status='pending', created_at=SEP_05)
    both = seed.user()
    seed.activation(both, at=SEP_20)
    seed.sub(both, tariff_id=5, is_trial=True, end='2026-09-08 12:00:00.000000', created_at=SEP_05)
    session.commit()

    first, second, third = _patches()
    with first, second, third:
        starts = await module._trial_starts(_AsyncOverSync(session))

    assert {user: at.replace(tzinfo=None) for user, at in starts.items()} == {
        converted: datetime(2026, 9, 5, 12),
        no_event: datetime(2026, 9, 20, 12),
        both: datetime(2026, 9, 5, 12),  # самое раннее из двух
    }


# ---------- СП-1.1: деньги — как выписка ----------


@pytest.mark.asyncio
async def test_money_in_is_the_statement_with_stands_and_friends_but_without_bonuses() -> None:
    session = _schema()
    seed = _Seed(session)
    client = seed.user()
    seed.tx(client, 'deposit', 26000, method='platega', at=SEP_05)
    seed.paid_card(client, at=SEP_20)
    friend = seed.user()
    seed.sub(friend, tariff_id=4, is_trial=True, end='2031-12-21 00:00:00.000000')
    seed.tx(friend, 'deposit', 30000, method='platega', at=SEP_05)  # деньги друга — в выписке есть
    stand = seed.user(telegram_id=ENV_STAND_TELEGRAM_ID)
    seed.paid_card(stand, at=SEP_05)  # тестовая оплата стенда — тоже в выписке
    # не деньги: бонус, ручное начисление, реферальная пометка, незавершённое, ноль
    seed.tx(client, 'deposit', 5000, method=None, description='Бонус за регистрацию', at=SEP_05)
    seed.tx(client, 'deposit', 10000, method='manual', description='Начисление администратором', at=SEP_05)
    seed.tx(client, 'deposit', 4700, method='platega', description='реферальный бонус', at=SEP_05)
    seed.tx(client, 'deposit', 99900, method='platega', at=SEP_05, completed=False)
    seed.tx(client, 'deposit', 0, method='platega', at=SEP_05)
    # границы месяца по МСК: полночь 01.09 — внутри, 23:59 31.08 — снаружи
    seed.tx(client, 'deposit', 111, method='platega', at=SEP_FIRST_MIDNIGHT)
    seed.tx(client, 'deposit', 222, method='platega', at=AUG_LAST_MINUTE)
    session.commit()

    window = module._sales_window('this_month', None, None, NOW)
    with _patches()[2]:
        money = await module._money_in(_AsyncOverSync(session), window.start, window.end)

    assert money == {'deposits': 3, 'receipts': 2, 'kopeks': 26000 + 14900 + 30000 + 14900 + 111}


# ---------- СП-1.1: не продлили / кончится ----------


@pytest.mark.asyncio
async def test_not_renewed_and_ending_soon_are_payers_on_paid_tariffs_one_row_each() -> None:
    session = _schema()
    seed = _Seed(session)
    lapsed = seed.user(name='Анна', balance=15100)
    seed.paid_card(lapsed, at=AUG_10)
    seed.sub(lapsed, tariff_id=3, is_trial=False, end=SEP_20, status='expired', autopay=True)
    renewed = seed.user()
    seed.paid_card(renewed, at=AUG_10)
    seed.sub(renewed, tariff_id=3, is_trial=False, end='2026-11-20 00:00:00.000000')  # продление увело срок вперёд
    trial_ended = seed.user()
    seed.sub(trial_ended, tariff_id=5, is_trial=True, end=SEP_20, status='expired')
    never_paid = seed.user()
    seed.sub(never_paid, tariff_id=3, is_trial=False, end=SEP_20, status='expired')  # подарок админа — не платил
    friend = seed.user()
    seed.paid_card(friend, at=AUG_10)
    seed.sub(friend, tariff_id=4, is_trial=False, end=SEP_20, status='expired')
    ending = seed.user(username='soon')
    seed.paid_card(ending, at=AUG_10)
    seed.sub(ending, tariff_id=3, is_trial=False, end='2026-10-01 10:00:00.000000')
    seed.sub(ending, tariff_id=3, is_trial=False, end='2026-10-02 10:00:00.000000')  # вторая строка — один человек
    limited = seed.user()
    seed.paid_card(limited, at=AUG_10)
    seed.sub(limited, tariff_id=3, is_trial=False, end='2026-10-03 10:00:00.000000', status='limited')
    later = seed.user()
    seed.paid_card(later, at=AUG_10)
    seed.sub(later, tariff_id=3, is_trial=False, end='2026-10-05 10:00:00.000000')  # за пределом 7 суток
    disabled = seed.user()
    seed.paid_card(disabled, at=AUG_10)
    seed.sub(disabled, tariff_id=3, is_trial=False, end='2026-10-01 10:00:00.000000', status='disabled')
    # платил, но сидит на ПРОБНОМ тарифе без пометки «пробная» (на боевом так было у пользователя 196) —
    # пробный тариф не платный, ни «не продлил», ни «кончится» про него не говорят
    trial_tariff_payer = seed.user()
    seed.paid_card(trial_tariff_payer, at=AUG_10)
    seed.sub(trial_tariff_payer, tariff_id=5, is_trial=False, end=SEP_20, status='expired')
    seed.sub(trial_tariff_payer, tariff_id=5, is_trial=False, end='2026-10-01 11:00:00.000000')
    # легаси-подписка без тарифа у плательщика — платная (как «Пользователи»)
    legacy_tariff = seed.user()
    seed.paid_card(legacy_tariff, at=AUG_10)
    seed.sub(legacy_tariff, tariff_id=None, is_trial=False, end='2026-10-02 11:00:00.000000')
    session.commit()

    window = module._sales_window('this_month', None, None, NOW)
    not_renewed = await _call(session, module._not_renewed, window.start, window.end)
    ending_soon = await _call(session, module._ending_soon, NOW)

    assert [row[0] for row in not_renewed] == [lapsed]
    assert (not_renewed[0][1], not_renewed[0][5], not_renewed[0][7], not_renewed[0][8]) == (
        'Анна',
        15100,
        True,
        'Базовый',
    )
    assert [row[0] for row in ending_soon] == [ending, legacy_tariff, limited]
    assert ending_soon[0][6].replace(tzinfo=None) == datetime(2026, 10, 1, 10)  # ближайший срок человека


# ---------- СП-1.2: обзор ----------


async def _overview(session: Session, window, now=NOW, people=None):
    first, second, third = _patches()
    counts = AsyncMock(return_value=people or {'paying': 68, 'on_trial': 35})
    with first, second, third, patch.object(module, 'count_trial_and_paying_users', counts):
        return await module._build_overview(_AsyncOverSync(session), window, now)


def _seed_september(seed: _Seed) -> dict[str, int]:
    """Сентябрь по мотивам боевого: у каждого — одна причина попасть или не попасть в число."""
    ids = {}
    # пришёл 05.09, взял пробный, купил 09.09 картой — «впервые после пробного»
    ids['after_trial'] = seed.user(created_at=SEP_05)
    seed.activation(ids['after_trial'], at=SEP_05)
    seed.paid_card(ids['after_trial'], at='2026-09-09 12:00:00.000000')
    # пришёл 20.09 и сразу купил — «впервые сразу без пробного»
    ids['direct'] = seed.user(created_at=SEP_20)
    seed.paid_card(ids['direct'], at=SEP_20)
    # пришёл 25.09, пробный ещё идёт (3 дня не прошли к 27.09 11:30)
    ids['trial_running'] = seed.user(created_at='2026-09-25 12:00:00.000000')
    seed.activation(ids['trial_running'], at='2026-09-25 12:00:00.000000')
    # пришёл 10.09, пробный кончился, не купил
    ids['trial_lost'] = seed.user(created_at='2026-09-10 12:00:00.000000')
    seed.sub(
        ids['trial_lost'],
        tariff_id=5,
        is_trial=True,
        end='2026-09-13 12:00:00.000000',
        status='expired',
        created_at='2026-09-10 12:00:00.000000',
    )
    # давний клиент: первая покупка в августе, продление и докупка в сентябре, живой
    ids['old'] = seed.user()
    seed.tx(ids['old'], 'deposit', 30000, method='platega', at=AUG_10)
    seed.tx(
        ids['old'], 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса', at=AUG_10
    )
    seed.tx(ids['old'], 'subscription_payment', -14900, method='balance', description='Продление подписки', at=SEP_20)
    seed.tx(
        ids['old'], 'subscription_payment', -4166, method='balance', description='Покупка доп. устройств: 1', at=SEP_20
    )
    # друг из Team: пришёл в сентябре, заплатил 100 ₽ — деньги в выписке, а в людях и покупках его нет
    ids['friend'] = seed.user(created_at='2026-09-06 12:00:00.000000')
    seed.sub(ids['friend'], tariff_id=4, is_trial=True, end='2031-12-21 00:00:00.000000')
    seed.tx(ids['friend'], 'deposit', 10000, method='platega', at='2026-09-06 12:00:00.000000')
    seed.tx(
        ids['friend'],
        'subscription_payment',
        -10000,
        method='balance',
        description='Оплата подписки',
        at='2026-09-06 12:00:00.000000',
    )
    # стенд: тестовая оплата картой — в выписке есть, в людях нет
    ids['stand'] = seed.user(created_at='2026-09-07 12:00:00.000000', telegram_id=ENV_STAND_TELEGRAM_ID)
    seed.paid_card(ids['stand'], at='2026-09-07 12:00:00.000000')
    # платил в июне, подписка кончилась 20.09 — не продлил
    ids['lapsed'] = seed.user()
    seed.paid_card(ids['lapsed'], at=LONG_AGO)
    seed.sub(ids['lapsed'], tariff_id=3, is_trial=False, end=SEP_20, status='expired')
    # платит, срок кончится 01.10 — «кончится за 7 дней»
    ids['ending'] = seed.user()
    seed.paid_card(ids['ending'], at=LONG_AGO)
    seed.sub(ids['ending'], tariff_id=3, is_trial=False, end='2026-10-01 10:00:00.000000')
    # не деньги: бонус за регистрацию у новичка
    seed.tx(ids['direct'], 'deposit', 5000, method=None, description='Бонус за регистрацию', at=SEP_20)
    return ids


@pytest.mark.asyncio
async def test_september_overview_counts_by_the_owners_rules() -> None:
    session = _schema()
    _seed_september(_Seed(session))
    session.commit()

    overview = await _overview(session, module._sales_window('this_month', None, None, NOW))

    assert overview.now.model_dump() == {'paying': 68, 'on_trial': 35, 'ending_soon': 1}
    # деньги как выписка: две покупки картой + деньги друга + тест стенда; бонус — не деньги
    assert overview.money.model_dump() == {
        'received_kopeks': 14900 + 14900 + 10000 + 14900,
        'deposits_count': 1,
        'receipts_count': 3,
        'previous_received_kopeks': 30000,  # 10.08 давний клиент пополнил — те же числа августа
        'previous_comparable': True,  # первые деньги — в июне, раньше начала августа
    }
    assert overview.purchases.model_dump() == {
        'count': 3,
        'amount_kopeks': 14900 * 3,
        'first_count': 2,
        'first_amount_kopeks': 14900 * 2,
        'first_after_trial': 1,
        'first_direct': 1,
        'renewal_count': 1,
        'renewal_amount_kopeks': 14900,
        'addon_count': 1,
        'addon_amount_kopeks': 4166,
        'previous_first_count': 1,  # первая покупка давнего клиента 10.08
        'not_renewed': 1,
    }
    # когорта: пришли 4 (друг Team и стенд — не люди), пробный у трёх, кончился у двух, купил после пробного один
    assert overview.trial.model_dump() == {'came': 4, 'took_trial': 3, 'trial_finished': 2, 'bought_after_trial': 1}
    assert overview.window.start == _msk(2026, 9, 1) and overview.window.end == NOW


@pytest.mark.asyncio
async def test_percent_is_not_comparable_when_the_previous_window_starts_before_the_first_money() -> None:
    session = _schema()
    seed = _Seed(session)
    user = seed.user()
    seed.tx(user, 'deposit', 14900, method='platega', at=SEP_05)  # первые деньги — 05.09
    seed.tx(user, 'deposit', 14900, method='platega', at=SEP_20)
    session.commit()

    week = await _overview(session, module._sales_window('7d', None, None, NOW))
    ninety = await _overview(session, module._sales_window('90d', None, None, NOW))
    everything = await _overview(session, module._sales_window('all', None, None, NOW))

    assert (week.money.previous_received_kopeks, week.money.previous_comparable) == (0, False)  # было 0 — процента нет
    assert (ninety.money.previous_received_kopeks, ninety.money.previous_comparable) == (0, False)
    assert (everything.money.previous_received_kopeks, everything.money.previous_comparable) == (None, False)
    assert everything.purchases.previous_first_count is None


@pytest.mark.asyncio
async def test_extended_trial_is_not_finished_even_after_three_days() -> None:
    session = _schema()
    seed = _Seed(session)
    # взял пробный 10.09, ему продлили до 2099 — пробный идёт, «закончился» про него неправда (ревью C3-1)
    extended = seed.user(created_at='2026-09-10 12:00:00.000000')
    seed.activation(extended, at='2026-09-10 12:00:00.000000')
    seed.sub(
        extended, tariff_id=5, is_trial=True, end='2099-01-01 00:00:00.000000', created_at='2026-09-10 12:00:00.000000'
    )
    ended = seed.user(created_at='2026-09-11 12:00:00.000000')
    seed.activation(ended, at='2026-09-11 12:00:00.000000')
    seed.sub(
        ended,
        tariff_id=5,
        is_trial=True,
        end='2026-09-14 12:00:00.000000',
        status='expired',
        created_at='2026-09-11 12:00:00.000000',
    )
    # срок вышел, а сторож ещё не перевёл строку в «истекла» — пробный всё равно закончился
    not_flipped = seed.user(created_at='2026-09-12 12:00:00.000000')
    seed.activation(not_flipped, at='2026-09-12 12:00:00.000000')
    seed.sub(
        not_flipped,
        tariff_id=5,
        is_trial=True,
        end='2026-09-15 12:00:00.000000',
        created_at='2026-09-12 12:00:00.000000',
    )
    session.commit()

    overview = await _overview(session, module._sales_window('this_month', None, None, NOW))

    assert overview.trial.model_dump() == {'came': 3, 'took_trial': 3, 'trial_finished': 2, 'bought_after_trial': 0}


@pytest.mark.asyncio
async def test_yesterday_has_no_finished_trials_yet() -> None:
    session = _schema()
    seed = _Seed(session)
    fresh = seed.user(created_at='2026-09-26 09:00:00.000000')
    seed.activation(fresh, at='2026-09-26 09:00:00.000000')
    session.commit()

    overview = await _overview(session, module._sales_window('yesterday', None, None, NOW))

    assert overview.trial.model_dump() == {'came': 1, 'took_trial': 1, 'trial_finished': 0, 'bought_after_trial': 0}


@pytest.mark.asyncio
async def test_percent_is_not_comparable_when_money_started_inside_the_previous_window() -> None:
    session = _schema()
    seed = _Seed(session)
    user = seed.user()
    seed.tx(user, 'deposit', 14900, method='platega', at='2026-09-16 12:00:00.000000')  # первые деньги — 16.09
    seed.tx(user, 'deposit', 14900, method='platega', at='2026-09-22 12:00:00.000000')
    session.commit()

    # «7 дней» = 21–27.09, сравнение 14–20.09: деньги там есть (16.09), но окно началось раньше первых денег
    week = await _overview(session, module._sales_window('7d', None, None, NOW))

    assert (week.money.previous_received_kopeks, week.money.previous_comparable) == (14900, False)


@pytest.mark.asyncio
async def test_purchase_before_the_trial_row_is_direct_and_not_a_trial_conversion() -> None:
    session = _schema()
    seed = _Seed(session)
    # пришёл и купил сразу 08.09, а строка пробного появилась позже (выдали руками / сброс) — это не «после пробного»
    buyer = seed.user(created_at='2026-09-08 12:00:00.000000')
    seed.paid_card(buyer, at='2026-09-08 12:00:00.000000')
    seed.sub(
        buyer,
        tariff_id=5,
        is_trial=True,
        end='2026-09-15 12:00:00.000000',
        status='expired',
        created_at='2026-09-12 12:00:00.000000',
    )
    session.commit()

    overview = await _overview(session, module._sales_window('this_month', None, None, NOW))

    assert (overview.purchases.first_after_trial, overview.purchases.first_direct) == (0, 1)
    assert overview.trial.model_dump() == {'came': 1, 'took_trial': 1, 'trial_finished': 1, 'bought_after_trial': 0}


# ---------- СП-1.3: списки под плитками ----------


@pytest.mark.asyncio
async def test_people_lists_are_the_same_people_as_the_tiles_with_what_the_owner_needs_to_call() -> None:
    session = _schema()
    seed = _Seed(session)
    ids = _seed_september(seed)
    # второй «не продливший» — раньше, без имени и без ника: список показывает его после свежего
    earlier = seed.user(balance=25000)
    seed.paid_card(earlier, at=LONG_AGO)
    seed.sub(earlier, tariff_id=3, is_trial=False, end=SEP_05, status='expired', autopay=True)
    session.execute(
        text("UPDATE users SET first_name = 'Анна', last_name = 'К', username = 'anna' WHERE id = :u"),
        {'u': ids['lapsed']},
    )
    session.commit()

    window = module._sales_window('this_month', None, None, NOW)
    first, second, third = _patches()
    with first, second, third:
        db = _AsyncOverSync(session)
        lapsed = await module._people_items(db, 'not_renewed', window, NOW)
        ending = await module._people_items(db, 'ending_soon', window, NOW)
    overview = await _overview(session, window)

    assert [(p.user_id, p.name, p.username, p.autopay_enabled, p.balance_kopeks, p.tariff_name) for p in lapsed] == [
        (ids['lapsed'], 'Анна К', 'anna', False, 0, 'Базовый'),
        (earlier, None, None, True, 25000, 'Базовый'),
    ]
    assert lapsed[1].telegram_id == earlier * 10  # без имени и ника — по нему владелец найдёт человека в кабинете
    assert [p.user_id for p in ending] == [ids['ending']]
    assert ending[0].end_date.replace(tzinfo=None) == datetime(2026, 10, 1, 10)
    assert (overview.purchases.not_renewed, overview.now.ending_soon) == (len(lapsed), len(ending))


# ---------- СП-1.4: реклама ----------


def _performance(leads: int, immature: int, buyers: int, receipts: int, spend: int | None) -> dict:
    return {
        'leads': leads,
        'immature_leads_count': immature,
        'paid_subscription_users_count': buyers,
        'customer_acquisition_cost_kopeks': round(spend / buyers) if spend is not None and buyers else None,
        'confirmed_receipts_kopeks': receipts,
    }


@pytest.mark.asyncio
async def test_ads_split_mature_and_fresh_campaigns_and_skip_the_ones_without_spend() -> None:
    session = _schema()
    session.execute(
        text(
            'INSERT INTO advertising_campaigns (id, name, ad_spend_kopeks, created_at) VALUES '
            "(4, 'Канал А 7000₽  ', 700000, '2026-08-18 12:00:00.000000'),"
            "(3, 'Канал Б 4500', NULL, '2026-08-14 12:00:00.000000'),"
            "(14, 'Канал В 12к', 1200000, '2026-09-22 12:00:00.000000'),"
            "(15, 'канал-г', 100, '2026-09-23 12:00:00.000000'),"
            "(20, 'старая без лидов', 5000, '2026-08-01 12:00:00.000000'),"
            "(21, 'свой канал', 0, '2026-08-01 12:00:00.000000')"
        )
    )
    session.commit()
    performances = {
        4: _performance(107, 0, 5, 322309, 700000),  # все лиды старше 7 суток
        3: _performance(7, 0, 1, 39800, None),
        14: _performance(37, 37, 1, 14900, 1200000),  # все лиды моложе 7 суток — рано судить
        15: _performance(0, 0, 0, 0, 100),  # лидов нет, заведена 4 дня назад — рано
        20: _performance(0, 0, 0, 0, 5000),  # лидов нет, заведена давно — зрелая, пустая трата
        21: _performance(40, 0, 6, 89400, 0),  # расход 0 ₽ — не покупная реклама, в расчёт не идёт
    }
    reader = AsyncMock(side_effect=lambda db, campaign_id, now: performances[campaign_id])

    with patch.object(module, 'get_campaign_performance', reader):
        ads = await module._ads(_AsyncOverSync(session), NOW)

    assert (ads.campaigns_total, ads.campaigns_with_spend) == (6, 4)
    assert (ads.mature_spend_kopeks, ads.mature_buyers, ads.mature_cost_per_buyer_kopeks) == (705000, 5, 141000)
    assert ads.mature_receipts_kopeks == 322309
    assert (ads.fresh_spend_kopeks, ads.fresh_buyers) == (1200100, 1)
    assert [(c.name, c.fresh, c.cost_per_buyer_kopeks) for c in ads.campaigns] == [
        ('Канал А 7000₽', False, 140000),
        ('старая без лидов', False, None),
        ('Канал В 12к', True, 1200000),
        ('канал-г', True, None),
    ]
    assert all(call.kwargs['now'] == NOW for call in reader.await_args_list)
    assert 21 not in [call.args[1] for call in reader.await_args_list]


# ---------- СП-1.5: верх панели администратора ----------


@pytest.mark.asyncio
async def test_new_buyers_today_are_first_purchases_since_moscow_midnight_by_people() -> None:
    session = _schema()
    seed = _Seed(session)
    today_new = seed.user()
    seed.paid_card(today_new, at='2026-09-26 21:00:00.000000')  # 00:00 МСК 27.09 — первая секунда суток
    renewal_today = seed.user()
    seed.paid_card(renewal_today, at=AUG_10)
    seed.tx(
        renewal_today,
        'subscription_payment',
        -14900,
        method='balance',
        description='Продление',
        at='2026-09-27 05:00:00.000000',
    )
    yesterday_late = seed.user()
    seed.paid_card(yesterday_late, at='2026-09-26 20:59:00.000000')  # 23:59 МСК 26.09 — вчера
    friend = seed.user()
    seed.sub(friend, tariff_id=4, is_trial=True, end='2031-12-21 00:00:00.000000')
    seed.paid_card(friend, at='2026-09-27 06:00:00.000000')
    stand = seed.user(telegram_id=ENV_STAND_TELEGRAM_ID)
    seed.paid_card(stand, at='2026-09-27 06:00:00.000000')
    session.commit()

    first, second, third = _patches()
    counts = AsyncMock(return_value={'paying': 68, 'on_trial': 35})
    with first, second, third, patch.object(module, 'count_trial_and_paying_users', counts):
        tiles = await module.owner_people_tiles(_AsyncOverSync(session), NOW)

    assert tiles == {'on_trial': 35, 'paying': 68, 'new_buyers_today': 1}


@pytest.mark.asyncio
async def test_ending_soon_list_does_not_depend_on_the_period_even_without_custom_dates() -> None:
    session = _schema()
    _seed_september(_Seed(session))
    session.commit()

    first, second, third = _patches()
    with first, second, third:
        response = await module.get_sales_people(
            kind='ending_soon', period='custom', start_date=None, end_date=None, admin=None, db=_AsyncOverSync(session)
        )
        with pytest.raises(HTTPException) as error:
            await module.get_sales_people(
                kind='not_renewed',
                period='custom',
                start_date=None,
                end_date=None,
                admin=None,
                db=_AsyncOverSync(session),
            )

    assert response.kind == 'ending_soon'
    assert error.value.status_code == 400  # «Не продлили» за период — без дат периода нет


# ---------- СП-1б: деньги для экрана «Статистика» ----------


@pytest.mark.asyncio
async def test_dashboard_money_is_the_statement_by_moscow_days_and_months() -> None:
    session = _schema()
    seed = _Seed(session)
    client = seed.user()
    # 23:30 МСК 26.09 — ещё 26-е, 00:30 МСК 27.09 — уже 27-е (по Гринвичу оба — 26-е)
    seed.tx(client, 'deposit', 10000, method='platega', at='2026-09-26 20:30:00.000000')
    seed.tx(client, 'deposit', 20000, method='platega', at='2026-09-26 21:30:00.000000')
    seed.paid_card(client, at=SEP_05)  # оплата сразу: приход кассы — деньги, списание — нет
    seed.tx(client, 'deposit', 111, method='platega', at=SEP_FIRST_MIDNIGHT)
    seed.tx(client, 'deposit', 222, method='platega', at=AUG_LAST_MINUTE)
    seed.tx(client, 'deposit', 30000, method='platega', at='2026-06-20 12:00:00.000000')  # июль без денег
    stand = seed.user(telegram_id=ENV_STAND_TELEGRAM_ID)
    seed.paid_card(stand, at=SEP_05)  # стенд — в выписке есть, значит и здесь
    # не деньги: бонус, реферальная пометка, незавершённое, ещё не наступившее
    seed.tx(client, 'deposit', 5000, method=None, description='Бонус за регистрацию', at=SEP_05)
    seed.tx(client, 'deposit', 4700, method='platega', description='реферальный бонус', at=SEP_05)
    seed.tx(client, 'deposit', 99900, method='platega', at=SEP_05, completed=False)
    seed.tx(client, 'deposit', 7777, method='platega', at='2026-09-27 09:00:00.000000')
    session.commit()

    with _patches()[2]:
        db = _AsyncOverSync(session)
        money = await module.dashboard_money(db, NOW)
        september = await module._money_in(db, _msk(2026, 9, 1), NOW)

    days = {item['date']: item['kopeks'] for item in money['days']}
    assert (len(days), min(days), max(days)) == (30, '2026-08-29', '2026-09-27')
    assert (days['2026-09-26'], days['2026-09-27'], days['2026-09-01'], days['2026-08-31']) == (10000, 20000, 111, 222)
    assert days['2026-09-05'] == 14900 * 2
    assert money['today_kopeks'] == 20000
    # тот же месяц, что «Пришло живых денег» на экране продаж, — до копейки
    assert money['month_kopeks'] == september['kopeks'] == 10000 + 20000 + 14900 * 2 + 111
    assert money['months'] == [
        {'month': '2026-06', 'kopeks': 30000},
        {'month': '2026-07', 'kopeks': 0},
        {'month': '2026-08', 'kopeks': 222},
        {'month': '2026-09', 'kopeks': money['month_kopeks']},
    ]
    assert money['total_kopeks'] == 30000 + 222 + money['month_kopeks']


@pytest.mark.asyncio
async def test_dashboard_money_without_any_money_is_zeros_not_a_failure() -> None:
    session = _schema()
    with _patches()[2]:
        money = await module.dashboard_money(_AsyncOverSync(session), NOW)

    assert (money['today_kopeks'], money['month_kopeks'], money['total_kopeks']) == (0, 0, 0)
    assert money['months'] == [{'month': '2026-09', 'kopeks': 0}]
    assert len(money['days']) == 30 and all(item['kopeks'] == 0 for item in money['days'])

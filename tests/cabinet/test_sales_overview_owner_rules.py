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
    assert [row[0] for row in ending_soon] == [ending, limited]
    assert ending_soon[0][6].replace(tzinfo=None) == datetime(2026, 10, 1, 10)  # ближайший срок человека

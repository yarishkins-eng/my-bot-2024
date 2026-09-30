"""Блок «Приглашения» на «Статистике» (РЕФ-2, 30.09.2026) — считается НАСТОЯЩИМ движком SQLite.

Каждый месяц Москвы считает тот же счётчик, что утреннее письмо (`reporting_service.referral_numbers`), поэтому
письмо и экран не расходятся (мина NW). Каждое событие — в своём месяце (решение владельца 29.09): пришёл в августе,
заплатил в сентябре. Патчатся только тарифы и список стендов из `.env`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.cabinet.routes import admin_sales_stats as module
from app.services import reporting_service as reporting_module


STAND_TELEGRAM_ID = 777
NOW = datetime(2026, 9, 27, 8, 30, tzinfo=UTC)  # 27.09 11:30 МСК
JUNE = '2026-06-15 12:00:00.000000'
AUG_10 = '2026-08-10 12:00:00.000000'
SEP_05 = '2026-09-05 12:00:00.000000'
SEP_20 = '2026-09-20 12:00:00.000000'
AUG_LAST_MINUTE = '2026-08-31 20:59:00.000000'  # 23:59 МСК 31.08 — ещё август
SEP_FIRST_MIDNIGHT = '2026-08-31 21:00:00.000000'  # 00:00 МСК 01.09 — уже сентябрь
AFTER_NOW = '2026-09-27 09:00:00.000000'  # 12:00 МСК 27.09 — позже «сейчас», в текущий месяц не входит

TARIFFS = [
    SimpleNamespace(id=3, is_free=False, is_trial_available=False, is_active=True),
    SimpleNamespace(id=4, is_free=True, is_trial_available=False, is_active=True),  # Team
    SimpleNamespace(
        id=5, is_free=True, is_trial_available=True, is_active=False
    ),  # Пробный: цены нулевые, как на боевом
]


async def _tariffs(db, *, include_inactive: bool = False, **_kwargs):
    return [tariff for tariff in TARIFFS if include_inactive or tariff.is_active]


class _AsyncOverSync:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def rollback(self):
        self.rolled_back = True
        self._s.rollback()


def _schema() -> Session:
    engine = create_engine('sqlite://')
    statements = [
        'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, status TEXT, test_account_enabled BOOLEAN, '
        'remnawave_uuid TEXT, referred_by_id INTEGER, created_at TIMESTAMP, username TEXT, first_name TEXT, '
        'last_name TEXT, email TEXT)',
        'CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount_kopeks INTEGER, '
        'payment_method TEXT, description TEXT, is_completed BOOLEAN, created_at TIMESTAMP)',
        'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, tariff_id INTEGER, is_trial BOOLEAN, '
        'status TEXT, end_date TIMESTAMP, created_at TIMESTAMP)',
        'CREATE TABLE subscription_events (id INTEGER PRIMARY KEY, user_id INTEGER, subscription_id INTEGER, '
        'event_type TEXT, extra JSON, occurred_at TIMESTAMP, message TEXT)',
        'CREATE TABLE referral_earnings (id INTEGER PRIMARY KEY, user_id INTEGER, referral_id INTEGER, '
        'amount_kopeks INTEGER, reason TEXT, created_at TIMESTAMP)',
    ]
    with engine.begin() as c:
        for statement in statements:
            c.execute(text(statement))
    return Session(engine)


class _Seed:
    def __init__(self, session: Session) -> None:
        self.s = session
        self._next = 100

    def user(
        self, *, created_at: str, referred_by_id: int | None = None, telegram_id: int | None = None, name: str = ''
    ) -> int:
        self._next += 1
        self.s.execute(
            text(
                'INSERT INTO users (id, telegram_id, status, referred_by_id, created_at, first_name) '
                "VALUES (:id, :tg, 'active', :ref, :c, :name)"
            ),
            {
                'id': self._next,
                'tg': telegram_id or self._next * 10,
                'ref': referred_by_id,
                'c': created_at,
                'name': name or None,
            },
        )
        return self._next

    def pay(
        self, user_id: int, amount: int, *, created_at: str, tx_type: str = 'provider_receipt', method: str = 'platega'
    ) -> None:
        self.s.execute(
            text(
                'INSERT INTO transactions (user_id, type, amount_kopeks, payment_method, description, is_completed, '
                "created_at) VALUES (:u, :t, :a, :m, 'Платёж картой получен', 1, :at)"
            ),
            {'u': user_id, 't': tx_type, 'a': amount, 'm': method, 'at': created_at},
        )

    def trial(self, user_id: int, *, created_at: str) -> None:
        self.s.execute(
            text(
                'INSERT INTO subscriptions (user_id, tariff_id, is_trial, status, created_at) '
                "VALUES (:u, 5, 1, 'active', :at)"
            ),
            {'u': user_id, 'at': created_at},
        )

    def team(self, user_id: int) -> None:
        self.s.execute(
            text(
                'INSERT INTO subscriptions (user_id, tariff_id, is_trial, status, created_at) '
                "VALUES (:u, 4, 0, 'active', :at)"
            ),
            {'u': user_id, 'at': JUNE},
        )

    def earning(self, referrer: int, referral: int, amount: int, reason: str, *, created_at: str) -> None:
        self.s.execute(
            text(
                'INSERT INTO referral_earnings (user_id, referral_id, amount_kopeks, reason, created_at) '
                'VALUES (:u, :r, :a, :why, :at)'
            ),
            {'u': referrer, 'r': referral, 'a': amount, 'why': reason, 'at': created_at},
        )


async def _overview(session: Session) -> dict:
    with (
        patch.object(reporting_module, 'get_all_tariffs', _tariffs),
        patch.object(reporting_module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch.object(module, 'get_all_tariffs', _tariffs),
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})),
    ):
        return await module.dashboard_referrals(_AsyncOverSync(session), NOW)


def _seed_two_months(seed: _Seed) -> None:
    masha = seed.user(created_at=JUNE, name='Маша')
    stand = seed.user(created_at=JUNE, telegram_id=STAND_TELEGRAM_ID, name='Стенд')
    # Петя пришёл в августе и взял пробный, а заплатил в сентябре — каждое событие в своём месяце
    petya = seed.user(created_at=AUG_10, referred_by_id=masha)
    seed.trial(petya, created_at=AUG_10)
    seed.pay(petya, 19900, created_at=SEP_05)
    seed.pay(petya, -19900, created_at=SEP_05, tx_type='subscription_payment')  # вторая проводка кассы
    seed.earning(masha, petya, 4975, 'referral_first_topup', created_at=SEP_05)
    seed.earning(masha, petya, 500, 'referral_commission_topup', created_at=AUG_LAST_MINUTE)
    seed.earning(masha, petya, 1000, 'referral_commission_topup', created_at=SEP_FIRST_MIDNIGHT)
    # последняя минута августа по Москве — ещё август; начисление админа — не оплата деньгами
    edge_friend = seed.user(created_at=AUG_LAST_MINUTE, referred_by_id=masha)
    seed.pay(edge_friend, 30000, created_at=SEP_05, tx_type='deposit', method='manual')
    # Иван пришёл в сентябре и заплатил в сентябре
    ivan = seed.user(created_at=SEP_05, referred_by_id=masha)
    seed.pay(ivan, 25000, created_at=SEP_20, tx_type='deposit')
    seed.earning(masha, ivan, 6250, 'referral_first_topup', created_at=SEP_20)
    # оплата позже «сейчас» в текущий месяц не входит
    seed.pay(ivan, 50000, created_at=AFTER_NOW, tx_type='deposit')
    # тестовая пара «человек ← стенд»: не рефералка, но деньги — в выписке, а человек — среди новых
    stand_friend = seed.user(created_at=SEP_05, referred_by_id=stand)
    seed.pay(stand_friend, 24900, created_at=SEP_05)
    seed.earning(stand, stand_friend, 16225, 'referral_first_topup', created_at=SEP_05)
    # шесть человек пришли в сентябре сами, а друг владельца на Team — не человек для долей
    for _ in range(6):
        seed.user(created_at=SEP_05)
    seed.team(seed.user(created_at=SEP_05))
    seed.s.commit()


@pytest.mark.asyncio
async def test_referrals_by_moscow_months_each_event_in_its_own_month() -> None:
    session = _schema()
    _seed_two_months(_Seed(session))

    overview = await _overview(session)

    assert overview['months'] == [
        {'month': '2026-08', 'came': 2, 'trial': 1, 'paid_first': 0, 'money_kopeks': 0, 'rewards_kopeks': 500},
        {
            'month': '2026-09',
            'came': 1,
            'trial': 0,
            'paid_first': 2,
            'money_kopeks': 44900,  # 199 + 250 ₽; стенд-пара и оплата позже «сейчас» — не здесь
            'rewards_kopeks': 12225,  # 49,75 + 10 + 62,50 ₽
        },
    ]
    # доли текущего месяца: пришли 1 из 8 новых людей (12,5 % — половина вверх); деньги приглашённых 449 из 698 ₽ выписки
    assert overview['new_people_month'] == 8
    assert overview['money_month_kopeks'] == 69800
    assert (overview['came_pct'], overview['money_pct']) == (13, 64)


@pytest.mark.asyncio
async def test_without_any_invited_people_it_is_one_month_of_zeros_and_no_shares() -> None:
    overview = await _overview(_schema())

    assert overview == {
        'months': [
            {'month': '2026-09', 'came': 0, 'trial': 0, 'paid_first': 0, 'money_kopeks': 0, 'rewards_kopeks': 0}
        ],
        'new_people_month': 0,
        'money_month_kopeks': 0,
        'came_pct': None,
        'money_pct': None,
    }


@pytest.mark.asyncio
async def test_the_screen_and_the_letter_use_one_counter_over_moscow_months() -> None:
    """Сторож мины NW: экран не считает рефералку сам — он зовёт счётчик письма за каждый месяц Москвы."""
    session = _schema()
    _seed_two_months(_Seed(session))
    counter = AsyncMock(
        return_value={'came': 7, 'trial': 6, 'paid': 5, 'paid_first': 4, 'money_kopeks': 300, 'rewards_kopeks': 200}
    )

    with patch.object(module.reporting_service, 'referral_numbers', counter):
        overview = await _overview(session)

    windows = [call.args[1:] for call in counter.await_args_list]
    assert windows == [
        (datetime(2026, 7, 31, 21, 0, tzinfo=UTC), datetime(2026, 8, 31, 21, 0, tzinfo=UTC)),
        (datetime(2026, 8, 31, 21, 0, tzinfo=UTC), NOW),
    ]
    assert overview['months'][-1] == {
        'month': '2026-09',
        'came': 7,
        'trial': 6,
        'paid_first': 4,
        'money_kopeks': 300,
        'rewards_kopeks': 200,
    }


@pytest.mark.asyncio
async def test_series_starts_in_the_moscow_month_of_the_first_arrival() -> None:
    """00:30 МСК 01.08 — это ещё 31.07 по UTC: ряд обязан начаться с августа, без пустого «июля»."""
    session = _schema()
    seed = _Seed(session)
    masha = seed.user(created_at=JUNE)
    seed.user(created_at='2026-07-31 21:30:00.000000', referred_by_id=masha)
    session.commit()

    overview = await _overview(session)

    assert [row['month'] for row in overview['months']] == ['2026-08', '2026-09']
    assert overview['months'][0]['came'] == 1


# ---------- РЕФ-2.4б: «Топ рефералов» — «заплатили N» и итоги по всем пригласившим ----------

MASHA, STAND = 101, 102  # порядок `_seed_two_months`


async def _top(session: Session, *, broken_additions: bool = False):
    from app.cabinet.routes import admin_stats

    db = _AsyncOverSync(session)
    with (
        patch.object(reporting_module, 'get_all_tariffs', _tariffs),
        patch.object(reporting_module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch.object(module, 'get_all_tariffs', _tariffs),
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})),
        patch.object(admin_stats, 'datetime', SimpleNamespace(now=lambda tz=None: NOW)),
        patch.object(
            reporting_module.reporting_service,
            'referral_paid_counts',
            AsyncMock(side_effect=RuntimeError('db is down'))
            if broken_additions
            else reporting_module.reporting_service.referral_paid_counts,
        ),
    ):
        response = await admin_stats.get_top_referrers(limit=20, admin=SimpleNamespace(), db=db)
    return response, db


@pytest.mark.asyncio
async def test_top_adds_paid_count_and_totals_and_keeps_the_old_numbers() -> None:
    session = _schema()
    _seed_two_months(_Seed(session))

    response, _ = await _top(session)

    rows = {item.user_id: item for item in response.by_invited}
    assert rows[MASHA].paid_count == 2  # Петя и Иван платили, «последняя минута августа» — нет
    assert rows[STAND].paid_count == 0  # пара со стендом не рефералка
    # прежние числа «Топа» не тронуты (правило владельца «только добавлять»): стенд по-прежнему в списке
    assert (rows[MASHA].invited_count, rows[STAND].invited_count) == (3, 1)
    assert response.total_referrals == 4
    # 27.09: сегодня ничего; 7 суток — с 21.09 МСК (20.09 21:00 UTC), оплата Ивана 20.09 в 15:00 МСК — раньше;
    # месяц — все сентябрьские начисления людей: 49,75 + 10 + 62,50 ₽; стенду 162,25 ₽ не в счёт
    assert response.period_totals.model_dump() == {'today_kopeks': 0, 'week_kopeks': 0, 'month_kopeks': 12225}


@pytest.mark.asyncio
async def test_this_month_under_the_top_is_the_same_number_as_the_tile() -> None:
    session = _schema()
    _seed_two_months(_Seed(session))

    response, _ = await _top(session)
    overview = await _overview(session)

    assert response.period_totals.month_kopeks == overview['months'][-1]['rewards_kopeks']


@pytest.mark.asyncio
async def test_top_survives_a_failure_of_the_new_fields() -> None:
    session = _schema()
    _seed_two_months(_Seed(session))

    response, db = await _top(session, broken_additions=True)

    assert response.period_totals is None
    assert all(item.paid_count is None for item in response.by_invited)
    assert {item.user_id for item in response.by_invited} == {MASHA, STAND}  # «Топ» на экране
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_period_totals_are_today_seven_moscow_days_and_the_calendar_month() -> None:
    session = _schema()
    seed = _Seed(session)
    masha = seed.user(created_at=JUNE)
    petya = seed.user(created_at=AUG_10, referred_by_id=masha)
    seed.earning(
        masha, petya, 700, 'referral_commission_topup', created_at='2026-09-27 05:00:00.000000'
    )  # 08:00 сегодня
    seed.earning(
        masha, petya, 300, 'referral_commission_topup', created_at='2026-09-26 21:00:00.000000'
    )  # 00:00 сегодня
    seed.earning(masha, petya, 3000, 'referral_commission_topup', created_at='2026-09-26 12:00:00.000000')  # вчера
    seed.earning(masha, petya, 100, 'referral_commission_topup', created_at='2026-09-20 21:00:00.000000')  # 00:00 21.09
    seed.earning(
        masha, petya, 6250, 'referral_commission_topup', created_at='2026-09-20 20:59:00.000000'
    )  # 23:59 20.09
    seed.earning(masha, petya, 4975, 'referral_first_topup', created_at=SEP_FIRST_MIDNIGHT)
    seed.earning(masha, petya, 500, 'referral_commission_topup', created_at=AUG_LAST_MINUTE)
    session.commit()

    with (
        patch.object(reporting_module, 'get_all_tariffs', _tariffs),
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})),
    ):
        totals = await module.referral_period_totals(_AsyncOverSync(session), NOW)

    assert totals == {'today_kopeks': 1000, 'week_kopeks': 4100, 'month_kopeks': 15325}


# ---------- W2-M1: сторожа к выжившим мутациям экрана ----------


@pytest.mark.asyncio
async def test_overview_route_fails_loudly_instead_of_showing_zeros() -> None:
    """Сбой счётчика — это 500 (экран покажет ошибку), а не пустой ответ, который на экране выглядит как «0 приглашённых»."""
    from fastapi import HTTPException

    from app.cabinet.routes import admin_stats

    with patch.object(admin_stats, 'dashboard_referrals', AsyncMock(side_effect=RuntimeError('db is down'))):
        with pytest.raises(HTTPException) as caught:
            await admin_stats.get_dashboard_referrals(admin=SimpleNamespace(), db=SimpleNamespace())

    assert caught.value.status_code == 500


@pytest.mark.asyncio
async def test_top_totals_count_todays_reward_up_to_now() -> None:
    """«Сегодня» под «Топом» — до «сейчас», а не до начала суток UTC: награда в 08:00 МСК сегодня обязана попасть."""
    session = _schema()
    seed = _Seed(session)
    masha = seed.user(created_at=JUNE)
    petya = seed.user(created_at=AUG_10, referred_by_id=masha)
    seed.earning(masha, petya, 700, 'referral_commission_topup', created_at='2026-09-27 05:00:00.000000')
    session.commit()

    response, _ = await _top(session)

    assert response.period_totals.today_kopeks == 700


@pytest.mark.asyncio
async def test_overview_route_returns_every_field_the_screen_reads() -> None:
    """Маршрут отдаёт ровно то, что насчитал `dashboard_referrals`: поле, пропавшее из модели ответа, pydantic отбросит молча."""
    from app.cabinet.routes import admin_stats

    session = _schema()
    _seed_two_months(_Seed(session))
    overview = await _overview(session)

    counter = AsyncMock(return_value=overview)
    with patch.object(admin_stats, 'dashboard_referrals', counter):
        response = await admin_stats.get_dashboard_referrals(admin=SimpleNamespace(), db=SimpleNamespace())

    assert response.model_dump() == overview
    assert counter.await_args.args[1].tzinfo is not None  # «сейчас» с часовым поясом, а не наивное местное время

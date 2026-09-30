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


def _schema() -> Session:
    engine = create_engine('sqlite://')
    statements = [
        'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, status TEXT, test_account_enabled BOOLEAN, '
        'remnawave_uuid TEXT, referred_by_id INTEGER, created_at TIMESTAMP)',
        'CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount_kopeks INTEGER, '
        'payment_method TEXT, description TEXT, is_completed BOOLEAN, created_at TIMESTAMP)',
        'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, tariff_id INTEGER, is_trial BOOLEAN, '
        'status TEXT, end_date TIMESTAMP, created_at TIMESTAMP)',
        'CREATE TABLE subscription_events (id INTEGER PRIMARY KEY, user_id INTEGER, subscription_id INTEGER, '
        'event_type TEXT, extra JSON, occurred_at TIMESTAMP)',
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

    def user(self, *, created_at: str, referred_by_id: int | None = None, telegram_id: int | None = None) -> int:
        self._next += 1
        self.s.execute(
            text(
                'INSERT INTO users (id, telegram_id, status, referred_by_id, created_at) '
                "VALUES (:id, :tg, 'active', :ref, :c)"
            ),
            {'id': self._next, 'tg': telegram_id or self._next * 10, 'ref': referred_by_id, 'c': created_at},
        )
        return self._next

    def pay(self, user_id: int, amount: int, *, created_at: str, tx_type: str = 'provider_receipt') -> None:
        self.s.execute(
            text(
                'INSERT INTO transactions (user_id, type, amount_kopeks, payment_method, description, is_completed, '
                "created_at) VALUES (:u, :t, :a, 'platega', 'Платёж картой получен', 1, :at)"
            ),
            {'u': user_id, 't': tx_type, 'a': amount, 'at': created_at},
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
    masha = seed.user(created_at=JUNE)
    stand = seed.user(created_at=JUNE, telegram_id=STAND_TELEGRAM_ID)
    # Петя пришёл в августе и взял пробный, а заплатил в сентябре — каждое событие в своём месяце
    petya = seed.user(created_at=AUG_10, referred_by_id=masha)
    seed.trial(petya, created_at=AUG_10)
    seed.pay(petya, 19900, created_at=SEP_05)
    seed.pay(petya, -19900, created_at=SEP_05, tx_type='subscription_payment')  # вторая проводка кассы
    seed.earning(masha, petya, 4975, 'referral_first_topup', created_at=SEP_05)
    seed.earning(masha, petya, 500, 'referral_commission_topup', created_at=AUG_LAST_MINUTE)
    seed.earning(masha, petya, 1000, 'referral_commission_topup', created_at=SEP_FIRST_MIDNIGHT)
    # последняя минута августа по Москве — ещё август
    seed.user(created_at=AUG_LAST_MINUTE, referred_by_id=masha)
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

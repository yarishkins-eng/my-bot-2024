"""РЕФ-2.2 (30.09.2026): чтение сумм рефералки для карточки продажи — на НАСТОЯЩЕМ движке SQLite.

Карточка `sale:*` не повторяется (мина MN), поэтому чтение живёт в своей короткой сессии с потолком и при любом сбое
отдаёт пустой ответ — карточка уходит как раньше. Сторож на движке, а не на моках (ревью L4-8): фильтр по заказу,
типу проводки и двум получателям здесь ВЫЧИСЛЯЕТСЯ.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import device_first_checkout_service as service_module


class _AsyncOverSync:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(
            text(
                'CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, '
                'amount_kopeks INTEGER, device_first_checkout_id INTEGER)'
            )
        )
        rows = [
            (9001, 'referral_reward', 4975, 101),  # пригласившему по этому заказу
            (9001, 'referral_reward', 2500, 101),  # доплата на тот же заказ — в ту же сумму
            (291, 'referral_reward', 10000, 101),  # бонус самому покупателю
            (9001, 'referral_reward', 6250, 102),  # чужой заказ
            (777, 'referral_reward', 500, 101),  # третий получатель на том же заказе
            (291, 'provider_receipt', 19900, 101),  # приход денег — не награда
        ]
        for user_id, tx_type, amount, checkout_id in rows:
            c.execute(
                text(
                    'INSERT INTO transactions (user_id, type, amount_kopeks, device_first_checkout_id) '
                    'VALUES (:u, :t, :a, :c)'
                ),
                {'u': user_id, 't': tx_type, 'a': amount, 'c': checkout_id},
            )
    return Session(engine)


@pytest.mark.asyncio
async def test_amounts_are_read_for_this_order_and_these_two_people_only():
    session = _session()
    with patch('app.database.database.AsyncSessionLocal', lambda: _AsyncOverSync(session)):
        amounts = await service_module._first_sale_referral_amounts(101, user_id=291, referrer_id=9001)

    assert amounts == {'referrer_reward_kopeks': 7475, 'referred_bonus_kopeks': 10000}


@pytest.mark.asyncio
async def test_nothing_credited_yet_gives_no_amounts():
    session = _session()
    with patch('app.database.database.AsyncSessionLocal', lambda: _AsyncOverSync(session)):
        amounts = await service_module._first_sale_referral_amounts(103, user_id=291, referrer_id=9001)

    assert amounts == {'referrer_reward_kopeks': None, 'referred_bonus_kopeks': None}


@pytest.mark.asyncio
async def test_a_failing_read_never_breaks_the_card():
    def _broken():
        raise RuntimeError('database is down')

    with patch('app.database.database.AsyncSessionLocal', _broken):
        assert await service_module._first_sale_referral_amounts(101, user_id=291, referrer_id=9001) == {}


@pytest.mark.asyncio
async def test_a_hanging_read_stops_at_the_ceiling():
    class _Slow(_AsyncOverSync):
        async def execute(self, stmt):
            await asyncio.sleep(5)

    with (
        patch.object(service_module, 'REFERRAL_AMOUNTS_TIMEOUT_SECONDS', 0.05),
        patch('app.database.database.AsyncSessionLocal', lambda: _Slow(None)),
    ):
        started = time.monotonic()
        assert await service_module._first_sale_referral_amounts(101, user_id=291, referrer_id=9001) == {}
    # без потолка чтение тоже кончилось бы пустым ответом — но через 5 с, держа очередь; меряем именно потолок
    assert time.monotonic() - started < 1

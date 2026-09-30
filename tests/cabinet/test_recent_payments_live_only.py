"""«Последние оплаты» на «Статистике» (ПЛ-1, 30.09.2026) — считается НАСТОЯЩИМ движком SQLite.

До этапа блок показывал любые пополнения и покупки: бонусы 50 ₽ за регистрацию вытесняли настоящие оплаты,
а пара «пополнение + покупка с баланса» показывала одни деньги дважды. Теперь в списке только живые оплаты
(правило «Пришло живых денег» утреннего письма), у каждой — первая/повторная, за что и из какой рекламы,
а скрытое за 30 дней посчитано отдельно.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.cabinet.routes import admin_stats as module
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Transaction,
    User,
)


STAND_TELEGRAM_ID = 777
NOW = datetime(2026, 9, 30, 20, 0, tzinfo=UTC)


class _AsyncOverSync:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)


def _session() -> Session:
    engine = create_engine('sqlite://')
    tables = [User.__table__, Transaction.__table__, AdvertisingCampaign.__table__]
    tables.append(AdvertisingCampaignRegistration.__table__)
    with engine.begin() as c:
        for table in tables:
            # только нужные колонки: полная схема тянет чужие внешние ключи и типы Postgres
            columns = ', '.join(f'{column.name}' for column in table.columns)
            c.execute(text(f'CREATE TABLE {table.name} ({columns})'))
    return Session(engine)


def _user(s: Session, uid: int, name: str, telegram_id: int | None = None) -> None:
    s.execute(
        text("INSERT INTO users (id, telegram_id, status, first_name) VALUES (:id, :tg, 'active', :n)"),
        {'id': uid, 'tg': telegram_id or uid * 1000 + 7, 'n': name},
    )


def _tx(s: Session, tid: int, uid: int, tx_type: str, amount: int, method, at: str, desc: str, checkout=None) -> None:
    s.execute(
        text(
            'INSERT INTO transactions (id, user_id, type, amount_kopeks, payment_method, description, is_completed, '
            'created_at, device_first_checkout_id) VALUES (:id, :u, :t, :a, :m, :d, 1, :at, :ck)'
        ),
        {'id': tid, 'u': uid, 't': tx_type, 'a': amount, 'm': method, 'd': desc, 'at': at, 'ck': checkout},
    )


def _seed(s: Session) -> None:
    _user(s, 1, 'Наталья & <Ко>')
    _user(s, 2, 'Галина')
    _user(s, 3, 'Стенд', telegram_id=STAND_TELEGRAM_ID)
    s.execute(text("INSERT INTO advertising_campaigns (id, name) VALUES (9, 'Не тв')"))
    s.execute(text('INSERT INTO advertising_campaign_registrations (id, campaign_id, user_id) VALUES (1, 9, 1)'))
    # Наталья: бонус за регистрацию (скрыт), пополнение 199 → покупка с баланса через 30 с (первая оплата),
    # позже пополнение 649 без покупки (повторная, «на баланс»), ручное начисление (скрыто)
    _tx(s, 10, 1, 'deposit', 5000, None, '2026-09-29 10:00:00.000000', 'Бонус за регистрацию')
    _tx(s, 11, 1, 'deposit', 19900, 'platega', '2026-09-29 11:00:00.000000', 'Пополнение через Platega (СБП (QR))')
    _tx(s, 12, 1, 'subscription_payment', -19900, 'balance', '2026-09-29 11:00:30.000000', 'Оплата с баланса: 1 месяц')
    _tx(s, 13, 1, 'deposit', 64900, 'platega', '2026-09-30 10:00:00.000000', 'Пополнение через Platega (Карты)')
    _tx(s, 14, 1, 'deposit', 30000, 'manual', '2026-09-30 11:00:00.000000', 'Начислено администратором')
    # Галина: оплата картой через кассу (чек + парная проводка заказа 118) — первая; пополнение, после которого
    # покупка только через два часа, — «на баланс»
    _tx(s, 20, 2, 'provider_receipt', 13400, 'platega', '2026-09-30 12:00:00.000000', 'Платёж картой получен', 118)
    _tx(s, 21, 2, 'subscription_payment', -13400, 'platega', '2026-09-30 12:00:01.000000', 'Картой: 1 месяц', 118)
    _tx(s, 22, 2, 'deposit', 9900, 'platega', '2026-09-30 13:00:00.000000', 'Пополнение через Platega (СБП (QR))')
    _tx(s, 23, 2, 'subscription_payment', -9900, 'balance', '2026-09-30 15:00:00.000000', 'Продление на 30 дней')
    # стенд платит настоящей картой — в список людей не идёт; нулевая проводка кассы — не деньги
    _tx(s, 30, 3, 'deposit', 14900, 'platega', '2026-09-30 14:00:00.000000', 'Пополнение через Platega')
    _tx(s, 31, 2, 'deposit', 0, 'platega', '2026-09-30 14:30:00.000000', 'Пополнение через Platega')
    s.commit()


@pytest.mark.asyncio
async def test_recent_payments_show_only_live_money_with_context():
    s = _session()
    _seed(s)
    with (
        patch.object(module, 'datetime', wraps=datetime) as fake_datetime,
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})),
    ):
        fake_datetime.now.return_value = NOW
        response = await module.get_recent_payments(limit=20, admin=None, db=_AsyncOverSync(s))

    rows = [(p.id, p.is_first, p.purpose, p.campaign_name, p.amount_kopeks) for p in response.payments]
    assert rows == [
        (22, False, None, None, 9900),
        (20, True, 'Картой: 1 месяц', None, 13400),
        (13, False, None, 'Не тв', 64900),
        (11, True, 'Оплата с баланса: 1 месяц', 'Не тв', 19900),
    ]
    assert response.payments[1].type_display == 'Оплата картой'
    assert response.payments[3].display_name == 'Наталья & <Ко>'
    hidden = response.hidden_last_30d
    assert (hidden.registration_bonuses, hidden.balance_purchases, hidden.manual_credits) == (1, 2, 1)


@pytest.mark.asyncio
async def test_first_payment_is_counted_over_all_time_not_the_page():
    """Первая оплата — самая ранняя за всё время, даже если она не попала в страницу списка."""
    s = _session()
    _seed(s)
    with patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})):
        response = await module.get_recent_payments(limit=1, admin=None, db=_AsyncOverSync(s))

    assert [(p.id, p.is_first) for p in response.payments] == [(22, False)]


def _edge_seed(s: Session) -> None:
    _user(s, 1, 'Два пополнения')
    _user(s, 2, 'Почта')
    s.execute(text("UPDATE users SET telegram_id = NULL, email = 'mail@example.test' WHERE id = 2"))
    _user(s, 3, 'Удалён')
    s.execute(text("UPDATE users SET status = 'deleted' WHERE id = 3"))
    s.execute(text("INSERT INTO advertising_campaigns (id, name) VALUES (8, '   '), (9, 'Канал ')"))
    s.execute(
        text('INSERT INTO advertising_campaign_registrations (id, campaign_id, user_id) VALUES (1, 8, 1), (2, 9, 1)')
    )
    # покупка ДО пополнения (оплачена старым остатком) — не «за что» этого пополнения
    _tx(s, 10, 1, 'subscription_payment', -9900, 'balance', '2026-09-30 09:59:30.000000', 'Старым остатком')
    # два пополнения, одна покупка следом — покупка достаётся одному, раннему
    _tx(s, 11, 1, 'deposit', 9900, 'platega', '2026-09-30 10:00:00.000000', 'Пополнение 1')
    _tx(s, 12, 1, 'deposit', 9900, 'platega', '2026-09-30 10:10:00.000000', 'Пополнение 2')
    _tx(s, 13, 1, 'subscription_payment', -19800, 'balance', '2026-09-30 10:20:00.000000', 'Оплата: 2 месяца')
    # чек кассы без заказа — своё описание; email-пользователь без telegram — человек
    _tx(s, 20, 2, 'provider_receipt', 13400, 'platega', '2026-09-30 11:00:00.000000', 'Платёж картой получен')
    # удалённый — не в списке и не в скрытом
    _tx(s, 30, 3, 'deposit', 14900, 'platega', '2026-09-30 12:00:00.000000', 'Пополнение')
    _tx(s, 31, 3, 'deposit', 5000, None, '2026-09-30 12:00:00.000000', 'Бонус за регистрацию')
    # счёт создан в 13:00, оплачен в 13:50 — покупка в 14:20 идёт в «за что» по времени ОПЛАТЫ
    _tx(s, 40, 2, 'deposit', 9900, 'platega', '2026-09-30 13:00:00.000000', 'Пополнение через Platega')
    s.execute(text("UPDATE transactions SET completed_at = '2026-09-30 13:50:00.000000' WHERE id = 40"))
    _tx(s, 41, 2, 'subscription_payment', -9900, 'balance', '2026-09-30 14:20:00.000000', 'Продление: 1 месяц')
    s.commit()


@pytest.mark.asyncio
async def test_purpose_takes_each_purchase_once_and_only_after_the_top_up():
    s = _session()
    _edge_seed(s)
    with patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})):
        response = await module.get_recent_payments(limit=20, admin=None, db=_AsyncOverSync(s))

    assert [(p.id, p.is_first, p.purpose, p.campaign_name) for p in response.payments] == [
        (40, False, 'Продление: 1 месяц', None),
        (20, True, 'Платёж картой получен', None),
        (12, False, None, 'Канал'),
        (11, True, 'Оплата: 2 месяца', 'Канал'),
    ]
    hidden = response.hidden_last_30d
    assert (hidden.registration_bonuses, hidden.balance_purchases, hidden.manual_credits) == (0, 3, 0)


@pytest.mark.asyncio
async def test_empty_base_gives_empty_list():
    s = _session()
    response = await module.get_recent_payments(limit=20, admin=None, db=_AsyncOverSync(s))
    assert response.payments == []
    assert response.hidden_last_30d.registration_bonuses == 0

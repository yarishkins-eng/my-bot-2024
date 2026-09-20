"""Утреннее письмо владельцу (ОТЧ-7, 20.09.2026) — считается НАСТОЯЩИМ движком, а не сверяется по тексту SQL.

Правила владельца, которые письмо обязано держать: пробный — не подписка; подписка — только когда
заплатили деньгами; Team — не клиенты; стенды и удалённые — не люди. Все запросы идут на SQLite в
памяти с минимальной схемой тех же таблиц и колонок, что читает `reporting_service.py` (образец —
`test_attach_referrer_money_fence_sqlite.py`): каждый фильтр в WHERE здесь ВЫЧИСЛЯЕТСЯ, и подмена
`AND`/`OR`, потеря `NOT`, лишний или пропущенный тип проводки красят снимок письма.

Что не считается здесь, а патчится: «Платят / На пробном» (общая с кабинетом функция
`count_trial_and_paying_users`, у неё свои сторожа в `tests/cabinet/test_admin_users_stats_cards.py`),
пробный тариф (`get_trial_tariff`) и список стендов.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import reporting_service as module
from app.services.reporting_service import ReportingService, ReportPeriod


STAND_TELEGRAM_ID = 777
# 18.09.2026 по МСК = [2026-09-17 21:00, 2026-09-18 21:00) UTC — как считает `_get_period_range`
IN_DAY = '2026-09-18 12:00:00'
DAY_BEFORE = '2026-09-17 12:00:00'
DAY_AFTER = '2026-09-19 12:00:00'
# границы суток по МСК в UTC: 17.09 22:00 UTC = 01:00 МСК 18.09 (внутри), 18.09 21:30 UTC = 00:30 МСК 19.09 (снаружи)
EDGE_INSIDE = '2026-09-17 22:00:00'
EDGE_OUTSIDE = '2026-09-18 21:30:00'


class _AsyncOverSync:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _schema() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(
            text(
                'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, status TEXT, '
                'test_account_enabled BOOLEAN, created_at TIMESTAMP)'
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
                'is_trial BOOLEAN, status TEXT, created_at TIMESTAMP)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE subscription_events (id INTEGER PRIMARY KEY, user_id INTEGER, subscription_id INTEGER, '
                'event_type TEXT, extra JSON, occurred_at TIMESTAMP)'
            )
        )
        c.execute(text('CREATE TABLE advertising_campaigns (id INTEGER PRIMARY KEY, name TEXT)'))
        c.execute(
            text(
                'CREATE TABLE advertising_campaign_registrations (id INTEGER PRIMARY KEY, campaign_id INTEGER, '
                'user_id INTEGER, created_at TIMESTAMP)'
            )
        )
        c.execute(
            text('CREATE TABLE tickets (id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, created_at TIMESTAMP)')
        )
    return Session(engine)


class _Seed:
    """Заполнение по одному факту за вызов — чтобы каждый тест читался как сценарий."""

    def __init__(self, session: Session) -> None:
        self.s = session
        self._next_user = 100

    def user(
        self,
        *,
        telegram_id: int | None = None,
        status: str | None = 'active',
        created_at: str = DAY_BEFORE,
        test_account_enabled: bool | None = None,
        email_only: bool = False,
    ) -> int:
        self._next_user += 1
        self.s.execute(
            text(
                'INSERT INTO users (id, telegram_id, status, test_account_enabled, created_at) '
                'VALUES (:id, :tg, :st, :te, :c)'
            ),
            {
                'id': self._next_user,
                'tg': None if email_only else (telegram_id or self._next_user * 10),
                'st': status,
                'te': test_account_enabled,
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
        completed: bool = True,
        created_at: str = IN_DAY,
    ) -> None:
        self.s.execute(
            text(
                'INSERT INTO transactions (user_id, type, amount_kopeks, payment_method, description, is_completed, '
                'created_at) VALUES (:u, :t, :a, :m, :d, :c, :at)'
            ),
            {'u': user_id, 't': tx_type, 'a': amount, 'm': method, 'd': description, 'c': completed, 'at': created_at},
        )

    def event(
        self,
        user_id: int,
        event_type: str,
        extra: str | None,
        *,
        occurred_at: str = IN_DAY,
        subscription_id: int | None = None,
    ) -> None:
        self.s.execute(
            text(
                'INSERT INTO subscription_events (user_id, subscription_id, event_type, extra, occurred_at) '
                'VALUES (:u, :sid, :e, :x, :at)'
            ),
            {'u': user_id, 'sid': subscription_id, 'e': event_type, 'x': extra, 'at': occurred_at},
        )

    def subscription(
        self, user_id: int, *, tariff_id: int, is_trial: bool, status: str = 'active', created_at: str = IN_DAY
    ) -> int:
        row = self.s.execute(
            text(
                'INSERT INTO subscriptions (user_id, tariff_id, is_trial, status, created_at) '
                'VALUES (:u, :t, :tr, :st, :at) RETURNING id'
            ),
            {'u': user_id, 't': tariff_id, 'tr': is_trial, 'st': status, 'at': created_at},
        ).scalar_one()
        self.s.commit()
        return int(row)

    def campaign(self, campaign_id: int, name: str) -> None:
        self.s.execute(
            text('INSERT INTO advertising_campaigns (id, name) VALUES (:i, :n)'), {'i': campaign_id, 'n': name}
        )

    def registration(self, campaign_id: int, user_id: int, *, created_at: str = IN_DAY) -> None:
        self.s.execute(
            text(
                'INSERT INTO advertising_campaign_registrations (campaign_id, user_id, created_at) VALUES (:c, :u, :at)'
            ),
            {'c': campaign_id, 'u': user_id, 'at': created_at},
        )

    def ticket(self, status: str, *, user_id: int, created_at: str = IN_DAY) -> None:
        self.s.execute(
            text('INSERT INTO tickets (user_id, status, created_at) VALUES (:u, :s, :at)'),
            {'u': user_id, 's': status, 'at': created_at},
        )


def _seed_owner_day(seed: _Seed) -> None:
    """Живой день по мотивам 18–19.09.2026 на боевом: три продажи тремя дорогами, докупка,
    пополнение, бонусы, стенд, удалённый, чужой день."""
    # 1. После пробного, с баланса (К-2 `sale:first`, снимок цели был пробным)
    after_trial = seed.user()
    seed.tx(
        after_trial, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц'
    )
    # строка подписки уже ПЕРЕПИСАНА покупкой: пробный взят сегодня, флаг снят, тариф платный
    rewritten = seed.subscription(after_trial, tariff_id=3, is_trial=False)
    seed.event(
        after_trial,
        'purchase',
        '{"was_trial_conversion": true, "purchase_type": "first_purchase"}',
        subscription_id=rewritten,
    )
    # 2. Новая покупка картой напрямую с кассы: приход `provider_receipt` + списание тем же днём
    direct = seed.user()
    seed.tx(direct, 'provider_receipt', 14900, method='platega', description='Оплата картой')
    seed.tx(direct, 'subscription_payment', -14900, method='platega', description='Оплата подписки картой: 1 месяц')
    seed.event(direct, 'purchase', '{"was_trial_conversion": false, "purchase_type": "first_purchase"}')
    # 3. Продление с кассы (К-2 `sale:repeat` → purchase_type=renewal)
    renewal = seed.user()
    seed.tx(renewal, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(renewal, 'purchase', '{"was_trial_conversion": false, "purchase_type": "renewal"}')
    # Докупка устройства — тот же тип проводки, отличается описанием
    seed.tx(
        renewal, 'subscription_payment', -4166, method='balance', description='Покупка доп. устройств: 1 устройство'
    )
    # Пополнение по СБП 260 ₽ и то, что деньгами НЕ является: бонус за регистрацию, реферальный, ручной
    topup = seed.user()
    seed.tx(topup, 'deposit', 26000, method='platega', description='Пополнение через Platega (СБП (QR))')
    seed.tx(topup, 'deposit', 5000, method=None, description='Бонус за регистрацию по кампании')
    # Маркер «реферальн» ловится через `ilike`; у SQLite `lower()` знает только латиницу, поэтому в
    # фикстуре описание уже строчными — на Postgres регистр не важен
    seed.tx(topup, 'deposit', 4700, method='platega', description='реферальный бонус за покупку друга')
    seed.tx(topup, 'deposit', 30000, method='manual', description='Начисление администратором')
    # Незавершённая проводка — не деньги
    seed.tx(topup, 'deposit', 99900, method='platega', description='Пополнение через Platega', completed=False)
    # Стенд и удалённый аккаунт: их продажи и приходы не считаются
    stand = seed.user(telegram_id=STAND_TELEGRAM_ID, created_at=IN_DAY)
    seed.tx(stand, 'subscription_payment', -27400, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(stand, 'purchase', '{"was_trial_conversion": true}')
    flagged_stand = seed.user(test_account_enabled=True, created_at=IN_DAY)
    seed.tx(flagged_stand, 'deposit', 26000, method='platega', description='Пополнение через Platega')
    seed.subscription(flagged_stand, tariff_id=5, is_trial=True)
    # нулевая проводка «смена тарифа администратором» — не продажа
    seed.tx(renewal, 'subscription_payment', 0, method='balance', description='Смена тарифа администратором на Team')
    erased = seed.user(status='deleted')
    seed.tx(erased, 'provider_receipt', 109000, method='platega', description='Оплата картой')
    # Чужой день — вчера
    seed.tx(
        after_trial,
        'subscription_payment',
        -14900,
        method='balance',
        description='Оплата подписки',
        created_at=DAY_BEFORE,
    )
    seed.event(after_trial, 'renewal', None, occurred_at=DAY_BEFORE)
    # Человек без статуса (легаси-строки) — человек; пробный, взятый ВЧЕРА и купленный сегодня, — не «взяли сегодня»
    legacy = seed.user(status=None, created_at=IN_DAY)
    seed.tx(legacy, 'deposit', 10000, method='platega', description='Пополнение через Platega')
    yesterday_trial = seed.user()
    old_row = seed.subscription(yesterday_trial, tariff_id=3, is_trial=False, created_at=DAY_BEFORE)
    seed.tx(
        yesterday_trial,
        'subscription_payment',
        -14900,
        method='balance',
        description='Оплата подписки с баланса: 1 месяц',
    )
    seed.event(yesterday_trial, 'purchase', '{"was_trial_conversion": true}', subscription_id=old_row)
    # Границы суток считаются по МСК, а не по UTC: час ночи 18.09 МСК — внутри, полпервого 19.09 — снаружи
    edge_in = seed.user(created_at=EDGE_INSIDE)
    seed.tx(edge_in, 'provider_receipt', 14900, method='platega', description='Оплата картой', created_at=EDGE_INSIDE)
    seed.tx(
        edge_in, 'subscription_payment', -14900, method='platega', description='Оплата подписки', created_at=EDGE_INSIDE
    )
    seed.event(
        edge_in,
        'purchase',
        '{"was_trial_conversion": false, "purchase_type": "first_purchase"}',
        occurred_at=EDGE_INSIDE,
    )
    edge_out = seed.user(created_at=EDGE_OUTSIDE)
    seed.tx(
        edge_out, 'deposit', 50000, method='platega', description='Пополнение через Platega', created_at=EDGE_OUTSIDE
    )
    # …и завтра: верхняя граница периода строгая, следующий день в письмо не попадает
    tomorrow = seed.user(created_at=DAY_AFTER)
    seed.tx(tomorrow, 'provider_receipt', 14900, method='platega', description='Оплата картой', created_at=DAY_AFTER)
    seed.tx(
        tomorrow, 'subscription_payment', -14900, method='platega', description='Оплата подписки', created_at=DAY_AFTER
    )
    seed.event(tomorrow, 'purchase', '{"was_trial_conversion": false}', occurred_at=DAY_AFTER)
    seed.subscription(tomorrow, tariff_id=5, is_trial=True, created_at=DAY_AFTER)
    seed.ticket('open', user_id=tomorrow, created_at=DAY_AFTER)
    # За день: два новичка (третий — стенд выше), пробные: настоящий, перекрашенный Team, брошенный
    newcomer_a = seed.user(created_at=IN_DAY)
    newcomer_b = seed.user(created_at=IN_DAY)
    seed.subscription(newcomer_a, tariff_id=5, is_trial=True)
    seed.subscription(newcomer_b, tariff_id=5, is_trial=True, status='pending')
    seed.subscription(renewal, tariff_id=4, is_trial=True)  # друг на Team с меткой «пробный»
    # По рекламе: две регистрации на «кувалда 2.0 8000», одна на «teplo11», одна у стенда
    seed.campaign(1, 'кувалда 2.0 8000')
    seed.campaign(2, 'teplo11  ')  # хвостовые пробелы, как у живых кампаний на боевом
    seed.registration(1, newcomer_a)
    seed.registration(1, newcomer_b)
    seed.registration(2, after_trial)
    seed.registration(1, stand)
    # Тикеты: один новый сегодня, открытых всего два (один старый)
    seed.ticket('open', user_id=topup)
    seed.ticket('answered', user_id=direct, created_at=DAY_BEFORE)
    seed.ticket('closed', user_id=direct, created_at=DAY_BEFORE)
    seed.ticket('open', user_id=stand)  # тикет стенда — ни «новый», ни «открытый»
    seed.s.commit()


async def _render(session: Session, period: ReportPeriod = ReportPeriod.DAILY, *, people=None) -> str:
    service = ReportingService()
    with (
        patch.object(module, 'AsyncSessionLocal', lambda: _AsyncOverSync(session)),
        patch.object(
            module, 'count_trial_and_paying_users', AsyncMock(return_value=people or {'paying': 59, 'on_trial': 46})
        ),
        patch.object(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch('app.services.user_service.test_account_telegram_ids', lambda: frozenset({STAND_TELEGRAM_ID})),
    ):
        return await service._build_report(period, date(2026, 9, 18))


@pytest.mark.asyncio
async def test_owner_report_for_a_live_day_is_eight_honest_lines() -> None:
    session = _schema()
    _seed_owner_day(_Seed(session))

    text_ = await _render(session)

    assert text_.split('\n') == [
        '📊 <b>Отчёт за 18.09.2026</b>',
        '',
        '💎 <b>Продажи</b>',
        '• Купили: <b>5</b> на <b>745 ₽</b> — после пробного 2 · продления 1 · сразу без пробного 2',
        '• Докупили устройств и трафика: 1 на 42 ₽',
        '• Пришло живых денег: <b>658 ₽</b> (пополнений баланса 2 · оплат сразу за подписку 2)',
        '',
        '📌 <b>Сейчас</b>',
        '• Платят: <b>59</b> · на пробном: <b>46</b>',
        '',
        '🚪 <b>За день</b>',
        '• Открыли бота: 4 · по рекламе: 3 (кувалда 2.0 8000 — 2, teplo11 — 1) · взяли пробный: 2',
        '',
        '🎟 Поддержка: 1 новых · 3 открытых',
    ]


@pytest.mark.asyncio
async def test_owner_report_never_prints_the_lines_the_owner_removed() -> None:
    session = _schema()
    _seed_owner_day(_Seed(session))

    text_ = await _render(session)

    for forbidden in (
        ' новые ',  # решение 20.09: «сразу без пробного»
        'прямых оплат',  # решение 20.09: «оплат сразу за подписку»
        'Конверси',
        'рефералам',
        'серверов',
        'Примечание',
        'Новых платных',
        'Активные триалы',
        'за 3 дня',
    ):
        assert forbidden not in text_, forbidden


@pytest.mark.asyncio
async def test_direct_card_sale_counts_as_money_in_and_as_one_sale() -> None:
    """🔴 Прежний отчёт видел только `deposit` и терял треть выручки (за 30 дней 7 485 ₽ из 20 248)."""
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.tx(buyer, 'provider_receipt', 109000, method='platega', description='Оплата картой')
    seed.tx(buyer, 'subscription_payment', -109000, method='platega', description='Оплата подписки картой: 365 дней')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": false, "purchase_type": "first_purchase"}')
    session.commit()

    text_ = await _render(session)

    assert '• Купили: <b>1</b> на <b>1090 ₽</b> — после пробного 0 · продления 0 · сразу без пробного 1' in text_
    assert '• Пришло живых денег: <b>1090 ₽</b> (пополнений баланса 0 · оплат сразу за подписку 1)' in text_


@pytest.mark.asyncio
async def test_split_that_does_not_match_the_ledger_says_so() -> None:
    """События пишутся только при включённых уведомлениях (мина MP) — расхождение называется, не прячется."""
    session = _schema()
    seed = _Seed(session)
    a, b = seed.user(), seed.user()
    seed.tx(a, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.tx(b, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(a, 'purchase', '{"was_trial_conversion": true}')
    session.commit()

    text_ = await _render(session)

    assert (
        '• Купили: <b>2</b> на <b>298 ₽</b> — после пробного 1 · продления 0 · сразу без пробного 0 · без пометки 1'
    ) in text_


@pytest.mark.asyncio
async def test_purchase_event_without_a_type_mark_is_unmarked_not_new() -> None:
    """🔴 Волна 1: события до РК-3 (20.09.2026) не несут `purchase_type`; у карточки «Продление» решалось
    по флагу, письму это неизвестно — такие продажи идут «без пометки», а не «новые»."""
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": false}')
    session.commit()

    text_ = await _render(session)

    assert '— после пробного 0 · продления 0 · сразу без пробного 0 · без пометки 1' in text_


@pytest.mark.asyncio
async def test_legacy_renewal_event_from_the_bot_path_is_a_renewal() -> None:
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Продление подписки')
    seed.event(buyer, 'renewal', '{"extended_days": 30}')
    session.commit()

    text_ = await _render(session)

    assert '— после пробного 0 · продления 1 · сразу без пробного 0' in text_


@pytest.mark.asyncio
async def test_weekly_report_uses_the_same_builder_with_a_period_header() -> None:
    session = _schema()
    _seed_owner_day(_Seed(session))

    text_ = await _render(session, ReportPeriod.WEEKLY)

    lines = text_.split('\n')
    assert lines[0] == '📊 <b>Отчёт за период 11.09.2026 - 17.09.2026</b>'
    assert '🚪 <b>За период</b>' in lines
    assert '📌 <b>Сейчас</b>' in lines and '• Платят: <b>59</b> · на пробном: <b>46</b>' in lines


@pytest.mark.asyncio
async def test_campaign_name_is_escaped_for_telegram_html() -> None:
    session = _schema()
    seed = _Seed(session)
    seed.campaign(1, 'A&B <test>')
    seed.registration(1, seed.user(created_at=IN_DAY))
    session.commit()

    text_ = await _render(session)

    assert 'по рекламе: 1 (A&amp;B &lt;test&gt;)' in text_  # одна кампания — число не повторяется


@pytest.mark.asyncio
async def test_now_block_comes_from_the_shared_cabinet_definition() -> None:
    """Плитки кабинета и письмо считает одна функция — подменили её ответ, подменилось письмо."""
    session = _schema()

    text_ = await _render(session, people={'paying': 7, 'on_trial': 3})

    assert '• Платят: <b>7</b> · на пробном: <b>3</b>' in text_


@pytest.mark.asyncio
async def test_trial_taken_and_bought_the_same_day_still_counts_as_taken() -> None:
    """🔴 Волна 1: покупка переписывает строку пробного (`is_trial` → False), и «взяли пробный» терял
    ровно тех, кого письмо же считало «после пробного» (44 вместо 47 за 19.09)."""
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    rewritten = seed.subscription(buyer, tariff_id=3, is_trial=False)
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": true}', subscription_id=rewritten)
    # а вот покупка БЕЗ пробного, переписавшая ничего, — не «взяли пробный»
    direct = seed.user()
    created = seed.subscription(direct, tariff_id=3, is_trial=False)
    seed.event(direct, 'purchase', '{"was_trial_conversion": false}', subscription_id=created)
    session.commit()

    text_ = await _render(session)

    assert '• Открыли бота: 0 · по рекламе: 0 · взяли пробный: 1' in text_


@pytest.mark.asyncio
async def test_stand_flagged_in_the_database_is_invisible_like_in_the_cabinet() -> None:
    """Стенд — по галке `test_account_enabled` в базе, а не только по списку в `.env`: так считает кабинет."""
    session = _schema()
    seed = _Seed(session)
    stand = seed.user(test_account_enabled=True, created_at=IN_DAY)
    seed.tx(stand, 'provider_receipt', 14900, method='platega', description='Оплата картой')
    seed.tx(stand, 'subscription_payment', -14900, method='platega', description='Оплата подписки картой: 1 месяц')
    seed.subscription(stand, tariff_id=5, is_trial=True)
    # а человек с явным «не стенд» (False) — виден, даже если его telegram_id в списке .env
    person = seed.user(telegram_id=STAND_TELEGRAM_ID, test_account_enabled=False, created_at=IN_DAY)
    seed.tx(person, 'deposit', 10000, method='platega', description='Пополнение через Platega')
    session.commit()

    text_ = await _render(session)

    assert '• Купили: <b>0</b> на <b>0 ₽</b>' in text_
    assert '• Пришло живых денег: <b>100 ₽</b> (пополнений баланса 1 · оплат сразу за подписку 0)' in text_
    assert '• Открыли бота: 1 · по рекламе: 0 · взяли пробный: 0' in text_


@pytest.mark.asyncio
async def test_zero_amount_ledger_rows_are_not_sales() -> None:
    session = _schema()
    seed = _Seed(session)
    friend = seed.user()
    seed.tx(friend, 'subscription_payment', 0, method='balance', description='Смена тарифа администратором на Team')
    session.commit()

    text_ = await _render(session)

    assert '• Купили: <b>0</b> на <b>0 ₽</b>' in text_


@pytest.mark.asyncio
async def test_email_only_client_is_a_person_in_every_number() -> None:
    """🔴 Мутационный скептик: без `telegram_id IS NULL` в предикате email-клиент (кабинетный вход) выпадал бы
    из всех чисел молча — `NULL NOT IN (...)` в SQL ни истина, ни ложь."""
    session = _schema()
    seed = _Seed(session)
    client = seed.user(email_only=True, created_at=IN_DAY)
    seed.tx(client, 'provider_receipt', 14900, method='platega', description='Оплата картой')
    seed.tx(client, 'subscription_payment', -14900, method='platega', description='Оплата подписки картой: 1 месяц')
    seed.event(client, 'purchase', '{"was_trial_conversion": false, "purchase_type": "first_purchase"}')
    session.commit()

    text_ = await _render(session)

    assert '• Купили: <b>1</b> на <b>149 ₽</b> — после пробного 0 · продления 0 · сразу без пробного 1' in text_
    assert '• Открыли бота: 1 · по рекламе: 0 · взяли пробный: 0' in text_


@pytest.mark.asyncio
async def test_two_conversion_events_on_one_subscription_count_the_trial_once() -> None:
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    rewritten = seed.subscription(buyer, tariff_id=3, is_trial=False)
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": true}', subscription_id=rewritten)
    seed.event(buyer, 'purchase', '{"was_trial_conversion": true}', subscription_id=rewritten)  # повтор записи
    session.commit()

    text_ = await _render(session)

    assert '• Открыли бота: 0 · по рекламе: 0 · взяли пробный: 1' in text_


@pytest.mark.asyncio
async def test_more_marks_than_sales_is_named_with_a_positive_number() -> None:
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Оплата подписки с баланса: 1 месяц')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": true}')
    seed.event(buyer, 'renewal', None)  # событие без проводки — лишняя пометка
    session.commit()

    text_ = await _render(session)

    assert '— после пробного 1 · продления 1 · сразу без пробного 0 (пометок больше, чем продаж: 1)' in text_

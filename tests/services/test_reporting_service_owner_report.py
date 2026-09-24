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
# Ровно полночь по МСК — граница окна. SQLite сравнивает метки как ТЕКСТ, а SQLAlchemy привязывает datetime в
# виде «…:00.000000»; метка без микросекунд короче и оказалась бы «меньше» границы — поэтому у граничных
# фикстур микросекунды выписаны явно (на Postgres это те же timestamptz)
MIDNIGHT_17 = '2026-09-16 21:00:00.000000'  # 00:00 МСК 17.09
MIDNIGHT_18 = '2026-09-17 21:00:00.000000'  # 00:00 МСК 18.09 — первая секунда окна
MIDNIGHT_19 = '2026-09-18 21:00:00.000000'  # 00:00 МСК 19.09 — уже снаружи


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
                'test_account_enabled BOOLEAN, remnawave_uuid TEXT, created_at TIMESTAMP)'
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
                'is_trial BOOLEAN, status TEXT, end_date TIMESTAMP, created_at TIMESTAMP)'
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
        remnawave_uuid: str | None = None,
    ) -> int:
        self._next_user += 1
        self.s.execute(
            text(
                'INSERT INTO users (id, telegram_id, status, test_account_enabled, remnawave_uuid, created_at) '
                'VALUES (:id, :tg, :st, :te, :uuid, :c)'
            ),
            {
                'id': self._next_user,
                'tg': None if email_only else (telegram_id or self._next_user * 10),
                'st': status,
                'te': test_account_enabled,
                'uuid': remnawave_uuid,
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
        self,
        user_id: int,
        *,
        tariff_id: int | None,
        is_trial: bool | None,
        status: str = 'active',
        created_at: str = IN_DAY,
        end_date: str | None = None,
    ) -> int:
        row = self.s.execute(
            text(
                'INSERT INTO subscriptions (user_id, tariff_id, is_trial, status, end_date, created_at) '
                'VALUES (:u, :t, :tr, :st, :end, :at) RETURNING id'
            ),
            {'u': user_id, 't': tariff_id, 'tr': is_trial, 'st': status, 'end': end_date, 'at': created_at},
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
    # …но настоящие деньги стенда — в выписке Platega, и в «пришло живых денег» они есть (решение 20.09)
    seed.tx(stand, 'provider_receipt', 14900, method='platega', description='Оплата картой')
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
    newcomer_a = seed.user(created_at=IN_DAY, remnawave_uuid='panel-a')  # подключился к панели (см. `_render`)
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
    # Потери (ВК-0): платная у lapsed кончилась сегодня и не продлена; renewed продлена (срок в будущем);
    # Team — бесплатный тариф, не клиент; у стенда — не человек. Вчерашний пробный (yesterday_trial)
    # так и не подключился: uuid панели ему не выдан
    lapsed = seed.user()
    seed.subscription(lapsed, tariff_id=3, is_trial=False, status='expired', end_date=IN_DAY)
    renewed = seed.user()
    seed.subscription(renewed, tariff_id=3, is_trial=False, end_date=DAY_AFTER)
    friend = seed.user()
    seed.subscription(friend, tariff_id=4, is_trial=False, status='expired', end_date=IN_DAY)
    seed.subscription(stand, tariff_id=3, is_trial=False, status='expired', end_date=IN_DAY)
    # Тикеты: один новый сегодня, открытых всего два (один старый)
    seed.ticket('open', user_id=topup)
    seed.ticket('answered', user_id=direct, created_at=DAY_BEFORE)
    seed.ticket('closed', user_id=direct, created_at=DAY_BEFORE)
    seed.ticket('open', user_id=stand)  # тикет стенда — ни «новый», ни «открытый»
    seed.s.commit()


TARIFFS = [
    SimpleNamespace(id=3, is_free=False, is_trial_available=False),  # Базовый — платный
    SimpleNamespace(id=4, is_free=True, is_trial_available=False),  # Team — бесплатный
    SimpleNamespace(id=5, is_free=False, is_trial_available=True),  # пробный
]


async def _render(
    session: Session,
    period: ReportPeriod = ReportPeriod.DAILY,
    *,
    people=None,
    connected: set[str] | None = frozenset({'panel-a'}),
) -> str:
    """`connected` — что ответила панель про первые подключения (`None` — не ответила)."""
    service = ReportingService()
    with (
        patch.object(module, 'AsyncSessionLocal', lambda: _AsyncOverSync(session)),
        patch.object(
            module, 'count_trial_and_paying_users', AsyncMock(return_value=people or {'paying': 59, 'on_trial': 46})
        ),
        patch.object(module, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5))),
        patch.object(module, 'get_all_tariffs', AsyncMock(return_value=TARIFFS)),
        patch.object(ReportingService, '_connected_panel_uuids', AsyncMock(return_value=connected)),
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
        # как в выписке: + стенд по галке (260), + стенд из .env (149), + удалённый (1090)
        '• Пришло живых денег: <b>2157 ₽</b> (пополнений баланса 3 · оплат сразу за подписку 4)',
        '',
        '📌 <b>Сейчас</b>',
        '• Платят: <b>59</b> · на пробном: <b>46</b>',
        '',
        '🚪 <b>За день</b>',
        '• Открыли бота: 4 · по рекламе: 3 (кувалда 2.0 8000 — 2, teplo11 — 1) · взяли пробный: 2',
        '',
        '📉 <b>Потери за вчера</b>',
        '• Взяли пробный: 2, подключились к VPN: 1',
        '• Не подключились за сутки после пробного: 1',
        '• Платная подписка закончилась и не продлена: 1',
        '• Пополнили баланс и ничего не купили: 2',
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
    # деньги стенда — настоящие, как в выписке (решение владельца 20.09): 149 + 100
    assert '• Пришло живых денег: <b>249 ₽</b> (пополнений баланса 1 · оплат сразу за подписку 1)' in text_
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


@pytest.mark.asyncio
async def test_tariff_switch_mark_is_not_a_sale_without_a_trial() -> None:
    session = _schema()
    seed = _Seed(session)
    buyer = seed.user()
    seed.tx(buyer, 'subscription_payment', -14900, method='balance', description='Смена тарифа')
    seed.event(buyer, 'purchase', '{"was_trial_conversion": false, "purchase_type": "tariff_switch"}')
    session.commit()

    text_ = await _render(session)

    assert '— после пробного 0 · продления 0 · сразу без пробного 0 · без пометки 1' in text_


@pytest.mark.asyncio
async def test_losses_connected_is_counted_by_panel_uuid_and_unknown_panel_is_named() -> None:
    """ВК-0: «подключились» — по первому подключению в панели через `users.remnawave_uuid`; когда панель
    не ответила, письмо говорит это словами, а не нулём (ноль был бы ложью)."""
    session = _schema()
    seed = _Seed(session)
    seed.subscription(seed.user(created_at=IN_DAY, remnawave_uuid='panel-1'), tariff_id=5, is_trial=True)
    seed.subscription(seed.user(created_at=IN_DAY, remnawave_uuid='panel-2'), tariff_id=5, is_trial=True)
    seed.subscription(seed.user(created_at=IN_DAY), tariff_id=5, is_trial=True)  # uuid не выдан: панель не знает
    session.commit()

    text_ = await _render(session, connected={'panel-1', 'panel-9'})
    assert '• Взяли пробный: 3, подключились к VPN: 1' in text_
    assert '• Не подключились за сутки после пробного: 0' in text_

    text_ = await _render(session, connected=None)
    assert '• Взяли пробный: 3, подключились к VPN: панель не ответила' in text_
    assert '• Не подключились за сутки после пробного: панель не ответила' in text_


@pytest.mark.asyncio
async def test_not_connected_after_a_day_looks_at_the_previous_window_only() -> None:
    """«За сутки» — взявшие пробный ОКНОМ РАНЬШЕ (позавчера для утреннего письма): у них к 06:00 прошло
    больше суток; вчерашние ещё могут подключиться, позавчерашние-минус-день уже отчитаны."""
    session = _schema()
    seed = _Seed(session)
    seed.subscription(seed.user(remnawave_uuid='y-1'), tariff_id=5, is_trial=True, created_at=DAY_BEFORE)
    seed.subscription(seed.user(remnawave_uuid='y-2'), tariff_id=5, is_trial=True, created_at=DAY_BEFORE)
    seed.subscription(seed.user(created_at=IN_DAY, remnawave_uuid='t-1'), tariff_id=5, is_trial=True)
    seed.subscription(seed.user(remnawave_uuid='o-1'), tariff_id=5, is_trial=True, created_at='2026-09-16 12:00:00')
    # границы окон ровно по МСК-полуночи: 00:00 17.09 (21:00 UTC 16.09) — уже «позавчера», 00:00 18.09 — «вчера»
    seed.subscription(seed.user(remnawave_uuid='e-1'), tariff_id=5, is_trial=True, created_at=MIDNIGHT_17)
    seed.subscription(seed.user(remnawave_uuid='e-2'), tariff_id=5, is_trial=True, created_at=MIDNIGHT_18)
    # вчерашний, купивший сегодня: строка переписана, но пробный он брал вчера — и так и не подключился
    yesterday_buyer = seed.user(remnawave_uuid='y-3')
    row = seed.subscription(yesterday_buyer, tariff_id=3, is_trial=False, created_at=DAY_BEFORE)
    seed.event(yesterday_buyer, 'purchase', '{"was_trial_conversion": true}', subscription_id=row)
    session.commit()

    text_ = await _render(session, connected={'y-2'})

    assert '• Взяли пробный: 2, подключились к VPN: 0' in text_
    assert '• Не подключились за сутки после пробного: 3' in text_


@pytest.mark.asyncio
async def test_paid_expired_counts_only_lapsed_paid_tariffs_of_people() -> None:
    """Истекла и не продлена — платный тариф (не бесплатный, не пробный, легаси без тарифа — платный), срок
    кончился в окне по МСК и остался в прошлом: продлённая ушла в будущее, Team бесплатен, пробный не
    подписка, перекрашенный с меткой пробного — не платный, стенд и удалённый — не люди."""
    session = _schema()
    seed = _Seed(session)
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, status='expired', end_date=IN_DAY)
    seed.subscription(seed.user(), tariff_id=None, is_trial=None, status='expired', end_date=EDGE_INSIDE)
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, end_date=DAY_AFTER)  # продлена
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, status='expired', end_date=EDGE_OUTSIDE)
    seed.subscription(seed.user(), tariff_id=4, is_trial=False, status='expired', end_date=IN_DAY)  # Team
    seed.subscription(seed.user(), tariff_id=5, is_trial=True, status='expired', end_date=IN_DAY)  # пробный
    seed.subscription(seed.user(), tariff_id=3, is_trial=True, status='expired', end_date=IN_DAY)  # с меткой
    stand = seed.user(telegram_id=STAND_TELEGRAM_ID)
    seed.subscription(stand, tariff_id=3, is_trial=False, status='expired', end_date=IN_DAY)
    erased = seed.user(status='deleted')
    seed.subscription(erased, tariff_id=3, is_trial=False, status='expired', end_date=IN_DAY)
    # границы окна ровно по МСК-полуночи: 00:00 18.09 (21:00 UTC 17.09) — внутри, 00:00 19.09 — уже снаружи
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, status='expired', end_date=MIDNIGHT_18)
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, status='expired', end_date=MIDNIGHT_19)
    seed.subscription(seed.user(), tariff_id=3, is_trial=False, status='expired', end_date=DAY_BEFORE)  # вчера
    session.commit()

    text_ = await _render(session)

    assert '• Платная подписка закончилась и не продлена: 3' in text_


@pytest.mark.asyncio
async def test_topped_up_and_idle_means_no_purchase_after_the_first_topup_of_the_day() -> None:
    """«Пополнил и не купил» — живые деньги за день (не бонус, не ручное, не стенд) и ни одной покупки после
    ПЕРВОГО пополнения до сборки письма: ночная покупка после окна считается, утренняя до пополнения — нет."""
    session = _schema()
    seed = _Seed(session)
    bought = seed.user()
    seed.tx(bought, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 10:00:00')
    seed.tx(
        bought, 'subscription_payment', -14900, method='balance', description='Оплата', created_at='2026-09-18 10:01:00'
    )
    night_buyer = seed.user()  # автопокупка не прошла, купил руками в 01:30 МСК — уже после окна, но до письма
    seed.tx(night_buyer, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 12:00:00')
    seed.tx(
        night_buyer, 'subscription_payment', -14900, method='balance', description='Оплата', created_at=EDGE_OUTSIDE
    )
    reserve = seed.user()  # купил утром, вечером положил «про запас» — после пополнения покупки нет
    seed.tx(
        reserve,
        'subscription_payment',
        -14900,
        method='balance',
        description='Оплата',
        created_at='2026-09-18 08:00:00',
    )
    seed.tx(reserve, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 12:00:00')
    idle = seed.user()
    seed.tx(idle, 'deposit', 14900, method='platega', description='Пополнение через Platega')
    bonus_only = seed.user()
    seed.tx(bonus_only, 'deposit', 5000, method=None, description='Бонус за регистрацию по кампании')
    yesterday = seed.user()
    seed.tx(yesterday, 'deposit', 14900, method='platega', description='Пополнение', created_at=DAY_BEFORE)
    stand = seed.user(telegram_id=STAND_TELEGRAM_ID)
    seed.tx(stand, 'deposit', 14900, method='platega', description='Пополнение через Platega')
    referral_only = seed.user()  # реферальный бонус записан пополнением платёжного шлюза — не живые деньги
    seed.tx(referral_only, 'deposit', 4700, method='platega', description='реферальный бонус за покупку друга')
    same_second = seed.user()  # автопокупка в ту же секунду, что пополнение — купил
    seed.tx(same_second, 'deposit', 14900, method='platega', description='Пополнение')
    seed.tx(same_second, 'subscription_payment', -14900, method='balance', description='Оплата')
    around = seed.user()  # покупка утром, пополнение днём, покупка вечером — после пополнения купил
    seed.tx(
        around, 'subscription_payment', -14900, method='balance', description='Оплата', created_at='2026-09-18 08:00:00'
    )
    seed.tx(around, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 12:00:00')
    seed.tx(
        around, 'subscription_payment', -14900, method='balance', description='Оплата', created_at='2026-09-18 13:00:00'
    )
    re_topper = seed.user()  # первое пополнение → покупка → второе пополнение без покупки: после ПЕРВОГО купил
    seed.tx(re_topper, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 10:00:00')
    seed.tx(
        re_topper,
        'subscription_payment',
        -14900,
        method='balance',
        description='Оплата',
        created_at='2026-09-18 11:00:00',
    )
    seed.tx(re_topper, 'deposit', 14900, method='platega', description='Пополнение', created_at='2026-09-18 12:00:00')
    tomorrow = seed.user()  # пополнение уже 19.09 по МСК — не в окне; и ровно в полночь 18.09 — в окне
    seed.tx(tomorrow, 'deposit', 14900, method='platega', description='Пополнение', created_at=MIDNIGHT_19)
    midnight = seed.user()
    seed.tx(midnight, 'deposit', 14900, method='platega', description='Пополнение', created_at=MIDNIGHT_18)
    unfinished = seed.user()  # счёт выставлен, но не оплачен — не пополнение
    seed.tx(unfinished, 'deposit', 14900, method='platega', description='Пополнение', completed=False)
    zero_sale = seed.user()  # после пополнения только нулевая проводка «смена тарифа» — это не покупка
    seed.tx(zero_sale, 'deposit', 14900, method='platega', description='Пополнение')
    seed.tx(
        zero_sale,
        'subscription_payment',
        0,
        method='balance',
        description='Смена тарифа администратором',
        created_at='2026-09-18 13:00:00',
    )
    session.commit()

    text_ = await _render(session)

    assert '• Пополнили баланс и ничего не купили: 4' in text_


@pytest.mark.asyncio
async def test_trial_taken_in_the_day_line_and_in_losses_is_one_number() -> None:
    """Сторож против двух копий определения «взяли пробный»: «За день» и «Потери» называют одно число."""
    session = _schema()
    _seed_owner_day(_Seed(session))

    lines = (await _render(session)).split('\n')

    day_line = next(line for line in lines if line.startswith('• Открыли бота'))
    losses_line = next(line for line in lines if line.startswith('• Взяли пробный'))
    assert day_line.endswith('взяли пробный: 2') and losses_line.startswith('• Взяли пробный: 2,')


@pytest.mark.asyncio
async def test_weekly_losses_block_has_a_period_header() -> None:
    session = _schema()
    _seed_owner_day(_Seed(session))

    text_ = await _render(session, ReportPeriod.WEEKLY)

    assert '📉 <b>Потери за период</b>' in text_ and 'Потери за вчера' not in text_


def test_next_run_after_an_early_wakeup_is_tomorrow_not_the_same_minute() -> None:
    """Проснулись на миллисекунду раньше 06:00 и уже отправили письмо за 20.09 — следующий запуск
    обязан быть завтра, иначе то же письмо уйдёт дважды (критик полноты ОТЧ-7)."""
    from datetime import UTC, time as datetime_time
    from zoneinfo import ZoneInfo

    msk = ZoneInfo('Europe/Moscow')

    class _EarlyClock(module.datetime):
        @classmethod
        def now(cls, tz=None):
            moment = module.datetime(2026, 9, 21, 5, 59, 59, 999000, tzinfo=msk)
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    sent_run = module.datetime(2026, 9, 21, 6, 0, tzinfo=msk).astimezone(UTC)
    with patch.object(module, 'datetime', _EarlyClock):
        next_run, report_date = ReportingService()._next_run_after(sent_run, datetime_time(6, 0))

    assert report_date == date(2026, 9, 21)
    assert next_run == module.datetime(2026, 9, 22, 6, 0, tzinfo=msk).astimezone(UTC)

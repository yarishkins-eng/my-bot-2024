"""Real SQL WHERE predicates: an invoice or a consumed slot is not a discounted sale."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.cabinet.routes import admin_auto_messages as api, admin_sales_stats as sales
from app.services import user_service
from app.services.notification_settings_service import NotificationSettingsService as Settings


NOW = datetime.now(UTC)
SINCE = NOW - timedelta(days=2)
AT = NOW - timedelta(days=1)


def sqltime(value):
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(' ')


class Database:
    def __init__(self, session):
        self.session = session

    async def execute(self, query):
        return self.session.execute(query)

    def user(self, user_id=1, *, status='active', test=False, telegram=None, team=False):
        self.session.execute(
            text('INSERT INTO users VALUES (:id,:tg,:status,:test)'),
            {'id': user_id, 'tg': telegram or 100000 + user_id, 'status': status, 'test': test},
        )
        if team:
            self.session.execute(text('INSERT INTO subscriptions VALUES (:id,:id,4)'), {'id': user_id})

    def offer(self, user_id=1, *, delivered=True, created=AT, extra=None, source='trial_ending_discount'):
        data = {'delivered_at': (created + timedelta(minutes=1)).isoformat()} if delivered else {}
        if extra is not None:
            data = extra
        self.session.execute(
            text(
                'INSERT INTO discount_offers (user_id,created_at,extra_data,notification_type) VALUES (:u,:at,:extra,:source)'
            ),
            {'u': user_id, 'at': sqltime(created), 'extra': json.dumps(data), 'source': source},
        )

    def order(
        self,
        user_id=1,
        *,
        saving=3000,
        total=11900,
        source='trial_ending_discount',
        at=AT,
        funding='funded',
        fulfillment='fulfilled',
        lifecycle='ready',
        snapshot=None,
        tariff=3,
    ):
        if snapshot is None:
            snapshot = {
                'promo_offer_source': source,
                'price_breakdown': {'promo_offer_discount_kopeks': saving},
                'tariff_total_kopeks': total,
            }
        self.session.execute(
            text(
                'INSERT INTO subscription_checkouts (user_id,fulfilled_at,funding_state,fulfillment_state,lifecycle_state,sale_snapshot,tariff_id) VALUES (:u,:at,:fund,:fulfill,:life,:snap,:tariff)'
            ),
            {
                'u': user_id,
                'at': sqltime(at) if at else None,
                'fund': funding,
                'fulfill': fulfillment,
                'life': lifecycle,
                'snap': json.dumps(snapshot),
                'tariff': tariff,
            },
        )


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(Settings, 'get_trial_ending_discount_stats_since', classmethod(lambda _: SINCE))
    monkeypatch.setattr(user_service, 'test_account_telegram_ids', lambda: {777})
    tariffs = [
        SimpleNamespace(id=3, is_free=False, is_trial_available=False),
        SimpleNamespace(id=4, is_free=True, is_trial_available=False),
        SimpleNamespace(id=5, is_free=True, is_trial_available=True),
    ]
    monkeypatch.setattr(sales, 'get_all_tariffs', AsyncMock(return_value=tariffs))
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        for ddl in [
            'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, status TEXT, test_account_enabled BOOLEAN)',
            'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, tariff_id INTEGER)',
            'CREATE TABLE discount_offers (id INTEGER PRIMARY KEY, user_id INTEGER, created_at TIMESTAMP, extra_data JSON, notification_type TEXT)',
            'CREATE TABLE subscription_checkouts (id INTEGER PRIMARY KEY, user_id INTEGER, fulfilled_at TIMESTAMP, funding_state TEXT, fulfillment_state TEXT, lifecycle_state TEXT, sale_snapshot JSON, tariff_id INTEGER)',
        ]:
            connection.execute(text(ddl))
    with Session(engine) as session:
        yield Database(session)
    engine.dispose()


@pytest.mark.asyncio
async def test_own_delivered_people_and_successful_orders_are_separate(db):
    db.user()
    db.offer()
    db.offer(created=AT + timedelta(hours=1))  # The recipient is not counted twice.
    db.order()
    db.order(saving=8000, total=31900, lifecycle='fulfilling')  # Payment done, panel still catching up.
    db.order(saving=0)  # First-sale consumption without benefit.
    db.order(source='ordinary')
    db.order(funding='invoice_pending', lifecycle='awaiting_funds')
    db.order(lifecycle='operator_review')  # Post-paid reversal retains old funded/fulfilled fields.
    db.order(fulfillment='not_started')
    db.order(at=SINCE - timedelta(seconds=1))
    db.order(at=NOW + timedelta(days=1))
    db.offer(created=NOW + timedelta(days=1))
    count, note, history = await api._trial_discount_stats(db)
    assert count == 1 and 'получили 1' in note and 'купили со скидкой 2' in note
    assert 'отдано 110 ₽' in note and 'медианный чек 219 ₽' in note
    assert '27 из 100' in note and 'Разница может быть случайной.' in note
    assert len(history) == 2 and all(row.user_ref == 'Клиент #1' for row in history)
    assert all(row.claimed is None for row in history)
    assert (NOW + timedelta(days=1)).strftime('%d.%m.%Y') not in note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'person', [{'status': 'deleted'}, {'test': True}, {'test': None, 'telegram': 777}, {'team': True}]
)
async def test_test_deleted_and_historical_team_people_do_not_enter_any_total(db, person):
    db.user(**person)
    db.offer()
    db.order()
    count, note, history = await api._trial_discount_stats(db)
    assert count == 0 and 'получили 0' in note and 'купили со скидкой 0' in note
    assert 'отдано 0 ₽' in note and history == []


@pytest.mark.asyncio
async def test_db_false_override_beats_env_and_unrelated_delivery_does_not_count(db):
    db.user(telegram=777, test=False)
    db.offer(delivered=False)
    db.offer(source='ordinary')
    db.offer(extra={'delivered_at': 'nonsense'})
    db.offer(extra={'delivered_at': AT.replace(tzinfo=None).isoformat()})
    db.offer(extra={'delivered_at': (NOW + timedelta(days=1)).isoformat()})
    db.order()
    count, note, _ = await api._trial_discount_stats(db)
    assert count == 0 and 'купили со скидкой 1' in note  # The app can sell despite a failed letter.


@pytest.mark.asyncio
@pytest.mark.parametrize('saving', [0, -1, True, '3000', 30.0, None])
async def test_only_strict_positive_own_saving_is_money(db, saving):
    db.user()
    db.order(saving=saving)
    _, note, _ = await api._trial_discount_stats(db)
    assert 'купили со скидкой 0' in note and 'отдано 0 ₽' in note


@pytest.mark.asyncio
async def test_small_sample_disclaimer_disappears_and_history_stays_bounded(db):
    for user_id in range(1, 52):
        db.user(user_id)
        db.offer(user_id)
    count, note, history = await api._trial_discount_stats(db)
    assert count == 51 and 'Разница может быть случайной' not in note
    assert len(history) == 50


@pytest.mark.asyncio
async def test_no_start_date_does_not_fabricate_an_epoch_or_query_database(monkeypatch):
    monkeypatch.setattr(Settings, 'get_trial_ending_discount_stats_since', classmethod(lambda _: None))
    monkeypatch.setattr(Settings, 'is_trial_ending_discount_enabled', classmethod(lambda _: False))
    untouched = AsyncMock()
    count, note, history = await api._trial_discount_stats(untouched)
    assert count == 0 and history == [] and 'после первого включения' in note
    untouched.execute.assert_not_awaited()

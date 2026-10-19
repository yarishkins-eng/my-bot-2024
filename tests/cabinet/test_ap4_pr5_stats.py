"""Client epoch and actual SQL selection; Python filtering must not hide a mutant."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.cabinet.routes import admin_auto_messages as api
from app.services import trial_ending_discount_service as rollout
from app.services.notification_settings_service import NotificationSettingsService as Settings
from tests.cabinet.test_ap4_pr4_prices import price_env  # noqa: F401
from tests.cabinet.test_ap4_pr4_stats import db  # noqa: F401


@pytest.mark.asyncio
async def test_first_issued_client_even_without_delivery_defines_moscow_day(db, monkeypatch):
    first = (datetime.now(UTC) - timedelta(days=2)).replace(hour=21, minute=30, second=0, microsecond=0)
    monkeypatch.setattr(
        Settings, 'get_trial_ending_discount_stats_since', classmethod(lambda _: first - timedelta(days=1))
    )
    db.user(1)
    db.offer(1, delivered=False, created=first)
    db.offer(1, delivered=False, created=first + timedelta(days=1))
    db.user(2, test=True)
    db.offer(2, created=first - timedelta(hours=2))
    count, note, history = await api._trial_discount_stats(db)
    assert count == 0 and history == []
    assert note.startswith(f'С {(first + timedelta(hours=3)):%d.%m.%Y}: получили 0 · купили со скидкой 0.')
    assert 'Клиенты скидку ещё не получали.' not in note


@pytest.mark.asyncio
async def test_source_is_filtered_by_sql_not_only_python_and_sale_does_not_require_delivery(db):
    db.user()
    db.offer(delivered=False)
    db.order()
    db.order(source='ordinary')
    db.order(saving=0)
    original = db.execute
    counts = []

    async def execute(query):
        result = await original(query)
        if next(iter(query.selected_columns)).key == 'sale_snapshot':
            sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
            assert '->>' in sql and "'promo_offer_source'" in sql and "'trial_ending_discount'" in sql
            frozen = result.freeze()
            counts.append(len(frozen().all()))
            return frozen()
        return result

    db.execute = execute
    count, note, history = await api._trial_discount_stats(db)
    assert counts == [2]  # The ordinary source was never selected; zero-benefit stays Python-filtered.
    assert count == 0 and history == [] and 'купили со скидкой 1' in note and 'заплатили 119 ₽' in note


def test_rollout_default_and_deployed_six_key_json_stay_enabled(price_env):
    assert rollout.TRIAL_ENDING_DISCOUNT_ROLLOUT == 'all'
    config = dict(
        enabled=True,
        discount_percent=20,
        carrier=24,
        audience=1,
        post_trial_hours=12,
        stats_since='2026-10-17T06:09:47+00:00',
    )
    Settings._storage_path.write_text(json.dumps({'trial_ending_discount': config}))
    Settings._loaded = False
    assert Settings.is_trial_ending_discount_enabled() is True
    assert set(Settings.get_config()['trial_ending_discount']) == set(config)
    assert api._discount_context()['rollout_note'] is None

"""АП-7: №75 и друзья с флагом пробной не получают письма настоящего пробного.

SQL от отправителя исполняет SQLite. Убираем только eager-loading: полученные по настоящим
условиям ID связываются с объектами доставки; база не подсовывает заранее выбранную когорту.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, insert
from sqlalchemy.orm import Session

from app.database.models import Subscription
from app.services import monitoring_service as service


TRIAL_ID = 17
REGULAR_ID = 31


class _SelectionDB:
    def __init__(self, subscriptions):
        self.objects = {sub.id: sub for sub in subscriptions}
        self.engine = create_engine('sqlite://')
        users = Table(
            'users',
            MetaData(),
            Column('id', Integer, primary_key=True),
            Column('status', String),
            Column('has_had_paid_subscription', Boolean),
        )
        with self.engine.begin() as connection:
            users.create(connection)
            Subscription.__table__.create(connection)
            for sub in subscriptions:
                connection.execute(
                    insert(users).values(id=sub.user.id, status='active', has_had_paid_subscription=False)
                )
                connection.execute(
                    insert(Subscription).values(
                        id=sub.id,
                        user_id=sub.user.id,
                        tariff_id=sub.tariff_id,
                        is_trial=sub.is_trial,
                        status=sub.status,
                        end_date=sub.end_date,
                        remnawave_short_id=str(sub.id),
                    )
                )
        self.session = Session(self.engine)

    async def execute(self, statement):
        selection = statement.with_only_columns(Subscription.id)
        selection._with_options = ()
        selected = self.session.scalars(selection).all()
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.objects[sid] for sid in selected]))

    async def refresh(self, obj, *, attribute_names):
        # Delivery objects are controlled fixtures; SQL selection remains real.
        assert obj in [sub.user for sub in self.objects.values()]
        assert attribute_names == ['promo_offer_discount_percent', 'promo_offer_discount_expires_at']

    def close(self):
        self.session.close()
        self.engine.dispose()

    async def commit(self):
        self.session.commit()


def _subscription(sid, tariff_id, is_trial, kind):
    user = SimpleNamespace(id=sid, telegram_id=7800000000 + sid, language='ru', has_had_paid_subscription=False)
    return SimpleNamespace(
        id=sid,
        user=user,
        tariff_id=tariff_id,
        is_trial=is_trial,
        status='active' if kind == 'soon' else 'expired',
        end_date=datetime.now(UTC) + (timedelta(hours=3) if kind == 'soon' else -timedelta(hours=30)),
    )


@pytest.mark.parametrize('kind', ['soon', 'discount'])
@pytest.mark.parametrize(
    'tariff_id,is_trial,marked,expected',
    [
        (TRIAL_ID, True, True, 1),
        (REGULAR_ID, True, True, 0),
        (None, True, True, 0),
        (TRIAL_ID, False, True, 0),
        (REGULAR_ID, False, True, 0),
        (TRIAL_ID, True, False, 0),
    ],
)
async def test_trial_selection_uses_the_marked_tariff(monkeypatch, kind, tariff_id, is_trial, marked, expected):
    """Решение владельца12.10: флаговая непробная остаётся на панели, два пробных письма ей не положены."""
    monitor = service.MonitoringService.__new__(service.MonitoringService)
    errors = MagicMock()
    monkeypatch.setattr(service.logger, 'error', errors)
    monitor.bot = SimpleNamespace()
    monitor._log_monitoring_event = AsyncMock()
    sender = AsyncMock(return_value=True)
    monitor._send_trial_ending_notification = sender
    monitor._send_trial_expired_discount_notification = sender
    tariff_reader = AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID) if marked else None)
    monkeypatch.setattr(service, 'get_trial_tariff', tariff_reader)
    monkeypatch.setattr(service, 'notification_sent', AsyncMock(return_value=False))
    recorder = AsyncMock()
    monkeypatch.setattr(service, 'record_notification', recorder)
    offers = AsyncMock(return_value=SimpleNamespace(id=9, expires_at=datetime.now(UTC) + timedelta(hours=24)))
    monkeypatch.setattr(service, 'upsert_discount_offer', offers)
    settings = service.NotificationSettingsService
    monkeypatch.setattr(settings, 'is_enabled', classmethod(lambda cls, key: True))
    monkeypatch.setattr(settings, 'are_notifications_globally_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(settings, 'is_trial_expired_discount_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(settings, 'get_trial_warn_hours', classmethod(lambda cls: 6))
    monkeypatch.setattr(settings, 'get_trial_expired_discount_trigger_days', classmethod(lambda cls: 1))
    monkeypatch.setattr(settings, 'get_trial_expired_discount_percent', classmethod(lambda cls: 10))
    db = _SelectionDB([_subscription(75, tariff_id, is_trial, kind)])
    try:
        if kind == 'soon':
            await monitor._check_trial_expiring_soon(db)
        else:
            await monitor._check_trial_expired_discount(db)
        assert sender.await_count == expected
        assert recorder.await_count == expected
        assert offers.await_count == (expected if kind == 'discount' else 0)
        tariff_reader.assert_awaited_once()
        errors.assert_not_called()
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['soon', 'discount'])
async def test_trial_tariff_is_read_once_for_a_whole_pass(monkeypatch, kind):
    """Две настоящие пробные должны пройти одним SQL, без чтения тарифа для каждого клиента."""
    monitor = service.MonitoringService.__new__(service.MonitoringService)
    errors = MagicMock()
    monkeypatch.setattr(service.logger, 'error', errors)
    monitor.bot = SimpleNamespace()
    monitor._log_monitoring_event = AsyncMock()
    sender = AsyncMock(return_value=True)
    monitor._send_trial_ending_notification = sender
    monitor._send_trial_expired_discount_notification = sender
    reader = AsyncMock(return_value=SimpleNamespace(id=TRIAL_ID))
    monkeypatch.setattr(service, 'get_trial_tariff', reader)
    monkeypatch.setattr(service, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(service, 'record_notification', AsyncMock())
    monkeypatch.setattr(
        service,
        'upsert_discount_offer',
        AsyncMock(return_value=SimpleNamespace(id=9, expires_at=datetime.now(UTC) + timedelta(hours=24))),
    )
    settings = service.NotificationSettingsService
    monkeypatch.setattr(settings, 'is_enabled', classmethod(lambda cls, key: True))
    monkeypatch.setattr(settings, 'are_notifications_globally_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(settings, 'is_trial_expired_discount_enabled', classmethod(lambda cls: True))
    monkeypatch.setattr(settings, 'get_trial_warn_hours', classmethod(lambda cls: 6))
    monkeypatch.setattr(settings, 'get_trial_expired_discount_trigger_days', classmethod(lambda cls: 1))
    monkeypatch.setattr(settings, 'get_trial_expired_discount_percent', classmethod(lambda cls: 10))
    db = _SelectionDB([_subscription(sid, TRIAL_ID, True, kind) for sid in (81, 82)])
    try:
        if kind == 'soon':
            await monitor._check_trial_expiring_soon(db)
        else:
            await monitor._check_trial_expired_discount(db)
        assert sender.await_count == 2
        reader.assert_awaited_once()
        errors.assert_not_called()
    finally:
        db.close()

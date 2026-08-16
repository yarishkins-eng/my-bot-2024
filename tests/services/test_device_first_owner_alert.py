"""Этап 4.3+4.8: владелец узнаёт о сорванном заказе, а упавшее уведомление не умирает навсегда.

Главное, что проверяется здесь, — не «сообщение отправилось», а **два замка** запроса-производителя.
Строка аутбокса уведомлений по любому из пяти архивных заказов тарифа 3 навсегда запретила бы менять
его серверы (когорта отбирается условием «строки нет», `app/database/crud/tariff.py:106-110`).
Поэтому запрос собирается отдельной функцией и проверяется по скомпилированному SQL: замок,
вырезанный из кода, роняет тест, даже если поведение на моках осталось прежним.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service_module
from app.services.device_first_checkout_service import (
    OWNER_ALERT_NOTIFICATION_TYPE,
    READY_NOTIFICATION_TYPE,
    owner_alert_candidate_query,
    process_device_first_notification_outbox,
    queue_owner_order_stuck_alerts,
    revive_stale_notifications,
)


def _compiled_candidate_sql() -> str:
    query = owner_alert_candidate_query(since=datetime(2026, 8, 16, tzinfo=UTC), limit=20)
    return str(query.compile(compile_kwargs={'literal_binds': False}))


# --- замки производителя: их удаление обязано ронять тест ------------------------------


def test_candidate_query_excludes_erased_users():
    """ЗАМОК 1. Все пять архивных заказов принадлежат УДАЛЁННЫМ пользователям 170 и 173."""
    sql = _compiled_candidate_sql().lower()
    assert 'users.account_erased_at is null' in sql


def test_candidate_query_has_freshness_window():
    """ЗАМОК 2. Архивные заказы не менялись с 03.08.2026 — окно их не достаёт."""
    sql = _compiled_candidate_sql().lower()
    assert 'subscription_checkouts.updated_at >=' in sql


def test_candidate_query_skips_already_queued_checkouts():
    """ЗАМОК 3. Второй проход не создаёт вторую строку по тому же заказу."""
    sql = _compiled_candidate_sql().lower()
    assert 'not (exists' in sql
    assert 'device_first_notification_outbox' in sql


def test_candidate_query_takes_only_stuck_orders():
    sql = _compiled_candidate_sql()
    assert 'operator_review' in str(
        owner_alert_candidate_query(since=datetime(2026, 8, 16, tzinfo=UTC), limit=1).compile(
            compile_kwargs={'literal_binds': True}
        )
    )
    assert 'device_first_outbox' in sql


def test_lookback_window_cannot_reach_the_archived_cohort():
    """Окно должно быть короче, чем расстояние до архивных заказов (03.08 → 16.08)."""
    assert timedelta(days=7) >= service_module.OWNER_ALERT_LOOKBACK


# --- производитель строк ---------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _db(rows):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(rows))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_owner_row_is_queued_for_a_stuck_order():
    db = _db([32])
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 1
    added = db.add.call_args[0][0]
    assert added.checkout_id == 32
    assert added.notification_type == OWNER_ALERT_NOTIFICATION_TYPE


@pytest.mark.asyncio
async def test_nothing_is_queued_when_admin_chat_is_not_configured():
    db = _db([32])
    with patch.object(service_module, '_owner_alerts_enabled', return_value=False):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 0
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_worker_does_not_duplicate_the_row():
    from sqlalchemy.exc import IntegrityError

    db = _db([32])
    db.commit = AsyncMock(side_effect=IntegrityError('insert', {}, Exception('duplicate')))
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 0
    db.rollback.assert_awaited()


# --- пункт 4.8: строка `failed` больше не мертва --------------------------------------


class _Update:
    def __init__(self, rowcount):
        self.rowcount = rowcount


@pytest.mark.asyncio
async def test_failed_notification_is_returned_to_pending():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Update(0), _Update(2)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    revived, dead = await revive_stale_notifications(db)
    assert (revived, dead) == (2, 0)
    db.commit.assert_awaited()
    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert any("status='dead'" in s or 'SET status=' in s for s in statements)


@pytest.mark.asyncio
async def test_hopeless_notification_stops_instead_of_looping_forever():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Update(1), _Update(0)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    revived, dead = await revive_stale_notifications(db)
    assert (revived, dead) == (0, 1)


@pytest.mark.asyncio
async def test_quiet_pass_does_not_commit():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Update(0), _Update(0)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    assert await revive_stale_notifications(db) == (0, 0)
    db.commit.assert_not_awaited()


def test_dead_transition_runs_before_revive():
    """Иначе строка, которой пора умереть, вечно возвращалась бы в `pending`."""
    source = service_module.revive_stale_notifications.__doc__ or ''
    assert 'dead' in source
    import inspect

    body = inspect.getsource(service_module.revive_stale_notifications)
    assert body.index("status='dead'") < body.index("status='pending'")


# --- развилка отправки: владельцу не должно уйти «✅ Подписка готова» -------------------


def _outbox_row(row_id, notification_type):
    return SimpleNamespace(
        id=row_id,
        checkout_id=100 + row_id,
        notification_type=notification_type,
        status='pending',
        lease_token=None,
        lease_expires_at=None,
        sending_at=None,
        sent_at=None,
        last_error=None,
    )


def _checkout():
    return SimpleNamespace(
        id=101,
        public_id='87bf4880-3a89-4f52-be13-276854d4f8a0',
        user_id=186,
        tariff_id=3,
        period_days=90,
        selected_device_limit=2,
        tariff_total_kopeks=64900,
        lifecycle_state='operator_review',
        provisioning_state='not_started',
        terminal_reason='provider_invoice_missing_or_elapsed_expiry',
        funding_mode='platega',
        debit_transaction_id=None,
        sale_snapshot={'tariff_name': 'Базовый'},
    )


def _worker_db(rows, *, checkout, user):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(rows))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.scalar = AsyncMock(return_value=0)

    async def _get(model, key):
        if model.__name__ == 'SubscriptionCheckout':
            return checkout
        if model.__name__ == 'User':
            return user
        return rows[0] if rows else None

    async def _get_row(model, key):
        if model.__name__ == 'DeviceFirstNotificationOutbox':
            return next((row for row in rows if row.id == key), None)
        return await _get(model, key)

    db.get = AsyncMock(side_effect=_get_row)
    return db


@pytest.mark.asyncio
async def test_owner_alert_does_not_use_the_client_ready_text():
    row = _outbox_row(1, OWNER_ALERT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=186, telegram_id=6221017268, username='tiger', language='ru', full_name='Tiger')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 1
    bot.send_message.assert_not_awaited()
    text = admin.send_admin_notification.await_args.args[0]
    assert 'Подписка готова' not in text
    assert 'Заказ завис' in text
    assert '649' in text
    assert 'Базовый' in text
    assert row.status == 'sent'


@pytest.mark.asyncio
async def test_client_ready_notification_still_goes_to_the_client():
    row = _outbox_row(1, READY_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=185, telegram_id=7454290913, username='krotop999', language='ru', full_name='K')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 1
    bot.send_message.assert_awaited_once()
    assert 'готова' in bot.send_message.await_args.args[1]
    assert row.status == 'sent'


@pytest.mark.asyncio
async def test_undelivered_owner_alert_is_marked_failed_not_sent():
    row = _outbox_row(1, OWNER_ALERT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=186, telegram_id=1, username=None, language='ru', full_name='Tiger')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=False)

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 0
    assert row.status == 'failed'


@pytest.mark.asyncio
async def test_money_taken_is_stated_explicitly_for_the_owner():
    checkout = _checkout()
    checkout.debit_transaction_id = 77
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=186, telegram_id=1, username=None, full_name='Tiger'))
    db.scalar = AsyncMock(return_value=0)
    text = await service_module._owner_order_stuck_text(db, checkout)
    assert 'деньги списаны с баланса' in text

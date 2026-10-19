import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Ns
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import VPN_USED_MIN_BYTES
from app.services import monitoring_service as m
from app.services.notification_settings_service import NotificationSettingsService as Settings
from app.services.remnawave_webhook_service import RemnaWaveWebhookService
from app.utils import trial_connect_reminder as q


class Queue:
    def __init__(self):
        self.items = {}
        self.broken = False

    async def zadd(self, key, values, nx=False):
        if self.broken:
            raise OSError('redis offline')
        added = 0
        for member, score in values.items():
            if member not in self.items or not nx:
                self.items[member] = score
                added += 1
        return added

    async def zscore(self, key, member):
        if self.broken:
            raise OSError('redis offline')
        return self.items.get(member)

    async def zrangebyscore(self, key, low, high, start, num, withscores):
        if self.broken:
            raise OSError('redis offline')
        result = [(k, v) for k, v in self.items.items() if v <= high]
        await asyncio.sleep(0)
        return sorted(result, key=lambda item: item[1])[start : start + num]

    async def zrem(self, key, *members):
        await asyncio.sleep(0)
        count = sum(member in self.items for member in members)
        for member in members:
            self.items.pop(member, None)
        return count


@pytest.fixture
def wired(monkeypatch):
    queue = Queue()
    monkeypatch.setattr(q, '_get_redis', lambda: queue)
    flags = {'trial_connect_reminder': True, 'trial_not_connected': True}
    monkeypatch.setattr(Settings, 'is_enabled', staticmethod(lambda key: flags.get(key, True)))
    monkeypatch.setattr(Settings, 'get_connect_reminder_minutes', staticmethod(lambda: 15))
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=Ns(id=5)))
    user = Ns(
        id=913,
        telegram_id=592034872,
        status='active',
        language='ru',
        remnawave_uuid='profile-913',
        test_reset_state='ready',
    )
    sub = Ns(
        id=724,
        user=user,
        remnawave_uuid='profile-913',
        is_active=True,
        is_trial=True,
        tariff_id=5,
        connected_squads=['de'],
        end_date=datetime.now(UTC) + timedelta(days=2),
    )
    user.subscription = sub
    monkeypatch.setattr(m, 'get_user_by_remnawave_uuid', AsyncMock(return_value=user))
    marks = set()

    async def marked(db, uid, sid, kind):
        return kind in marks

    record = AsyncMock(side_effect=lambda db, uid, sid, kind: marks.add(kind))
    monkeypatch.setattr(m, 'notification_sent', marked)
    monkeypatch.setattr(m, 'record_notification', record)
    api = Ns(
        get_user_lifetime_traffic_once=AsyncMock(return_value=0),
        get_user_devices_all=AsyncMock(
            return_value={
                'devices': [
                    {'createdAt': '2026-10-18T01:00:00Z', 'updatedAt': '2026-10-19T20:00:00Z', 'userAgent': 'Other/4'},
                    {
                        'createdAt': '2026-10-18T10:00:00Z',
                        'updatedAt': '2026-10-18T10:00:00Z',
                        'userAgent': 'iNcY/1.2 iOS',
                    },
                ]
            }
        ),
    )

    @asynccontextmanager
    async def client():
        yield api

    factory = Ns(get_api_client=client)
    monkeypatch.setattr(m, 'RemnaWaveService', lambda: factory)
    service = m.MonitoringService.__new__(m.MonitoringService)
    service.bot = Ns(send_message=AsyncMock(return_value=Ns(message_id=42)))
    service._log_monitoring_event = AsyncMock()
    db = AsyncMock()
    queue.items[user.remnawave_uuid] = m.time.time() - 1
    return Ns(queue=queue, flags=flags, user=user, sub=sub, service=service, db=db, marks=marks, record=record, api=api)


@pytest.mark.asyncio
@pytest.mark.parametrize('traffic,expected', [(VPN_USED_MIN_BYTES - 1, 1), (VPN_USED_MIN_BYTES, 0), (None, 0)])
async def test_traffic_boundary_and_unknown_is_removed(wired, traffic, expected):
    w = wired
    w.api.get_user_lifetime_traffic_once.return_value = traffic
    await w.service._check_trial_connect_reminders(w.db)
    assert w.service.bot.send_message.await_count == expected
    assert w.record.await_count == expected
    assert not w.queue.items
    if expected:
        assert 'INCY' in w.service.bot.send_message.await_args.kwargs['text']
        assert w.record.await_args.args[-1] == 'trial_connect_reminder'
        w.service._log_monitoring_event.assert_awaited_once()
        assert w.service._log_monitoring_event.await_args.args[1] == 'trial_connect_reminder_notifications_sent'


@pytest.mark.asyncio
async def test_two_workers_send_once_and_claim_precedes_telegram(wired):
    w = wired

    async def sent(**kwargs):
        assert not w.queue.items, 'queue must be claimed before sending'
        await asyncio.sleep(0)
        return Ns(message_id=42)

    w.service.bot.send_message.side_effect = sent
    await asyncio.gather(w.service._check_trial_connect_reminders(w.db), w.service._check_trial_connect_reminders(w.db))
    w.service.bot.send_message.assert_awaited_once()
    w.record.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'case',
    [
        'team',
        '75',
        'paid',
        'deleted',
        'no-telegram',
        'reset',
        'uuid',
        'no-squads',
        'expired',
        'one-hour',
        'disabled',
        'foreign-app',
        'letter',
        'reminder',
        'missing-user',
    ],
)
async def test_ineligible_records_are_removed(wired, monkeypatch, case):
    w = wired
    if case == 'team':
        w.sub.tariff_id = 4
    if case == '75':
        w.sub.id, w.sub.tariff_id = 75, 1
    if case == 'paid':
        w.sub.is_trial = False
    if case == 'deleted':
        w.user.status = 'deleted'
    if case == 'no-telegram':
        w.user.telegram_id = None
    if case == 'reset':
        w.user.test_reset_state = 'resetting'
    if case == 'uuid':
        w.sub.remnawave_uuid = 'foreign-profile'
    if case == 'no-squads':
        w.sub.connected_squads = []
    if case == 'expired':
        w.sub.is_active = False
    if case == 'one-hour':
        w.sub.end_date = datetime.now(UTC) + timedelta(minutes=59)
    if case == 'disabled':
        w.flags['trial_connect_reminder'] = False
    if case == 'foreign-app':
        w.api.get_user_devices_all.return_value = {
            'devices': [{'createdAt': '2026-10-18T11:00:00Z', 'userAgent': 'Other/5'}]
        }
    if case == 'letter':
        w.marks.add('trial_not_connected')
    if case == 'reminder':
        w.marks.add('trial_connect_reminder')
    if case == 'missing-user':
        monkeypatch.setattr(m, 'get_user_by_remnawave_uuid', AsyncMock(return_value=None))
    await w.service._check_trial_connect_reminders(w.db)
    w.service.bot.send_message.assert_not_awaited()
    w.record.assert_not_awaited()
    assert not w.queue.items


@pytest.mark.asyncio
@pytest.mark.parametrize('app,expected', [('HAPP/1.2', 'Happ'), ('incy /1', 'INCY')])
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_latest_created_at_application_and_language(wired, app, expected, language):
    w = wired
    w.user.language = language
    w.api.get_user_devices_all.return_value = {
        'devices': [
            {'createdAt': '2026-10-18T11:00:00+03:00', 'userAgent': 'Other/4'},
            {'createdAt': '2026-10-18T09:00:00Z', 'userAgent': app},
        ]
    }
    await w.service._check_trial_connect_reminders(w.db)
    text = w.service.bot.send_message.await_args.kwargs['text']
    assert expected in text
    assert ('One step left' if language == 'en' else 'Остался один шаг') in text


@pytest.mark.asyncio
async def test_night_uses_plain_silent_message_and_shared_connection_keyboard(wired, monkeypatch):
    monkeypatch.setattr(m, 'is_quiet_night', lambda: True)
    button = __import__('unittest.mock', fromlist=['Mock']).Mock(side_effect=m.build_miniapp_or_callback_button)
    monkeypatch.setattr(m, 'build_miniapp_or_callback_button', button)
    await wired.service._check_trial_connect_reminders(wired.db)
    assert wired.service.bot.send_message.await_args.kwargs['disable_notification'] is True
    assert button.call_args_list[0].kwargs['cabinet_path'] == '/connection'
    assert button.call_args_list[1].kwargs['callback_data'] == 'menu_support'


@pytest.mark.asyncio
async def test_send_error_consumes_queue_without_mark_or_retry(wired):
    w = wired
    w.service.bot.send_message.side_effect = OSError('Telegram offline')
    await w.service._check_trial_connect_reminders(w.db)
    await w.service._check_trial_connect_reminders(w.db)
    assert not w.queue.items
    w.service.bot.send_message.assert_awaited_once()
    w.record.assert_not_awaited()
    w.service._log_monitoring_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_due_removed_without_panel_lookup(wired):
    w = wired
    w.queue.items[w.user.remnawave_uuid] = m.time.time() - 30 * 60 - 1
    await w.service._check_trial_connect_reminders(w.db)
    w.api.get_user_lifetime_traffic_once.assert_not_awaited()
    assert not w.queue.items


@pytest.mark.asyncio
async def test_limit_three_and_record_timeout(wired, monkeypatch):
    w = wired
    w.queue.items = {str(i): m.time.time() - 1 for i in range(7)}
    w.service._send_trial_connect_reminder = AsyncMock(side_effect=TimeoutError)
    await w.service._check_trial_connect_reminders(w.db)
    assert w.service._send_trial_connect_reminder.await_count == 3
    assert len(w.queue.items) == 4
    # The real sender times only preflight, not a replacement mock.
    contexts = []
    real_timeout = asyncio.timeout
    monkeypatch.setattr(m.asyncio, 'timeout', lambda seconds: (contexts.append(seconds), real_timeout(0.001))[1])
    w.service._send_trial_connect_reminder = m.MonitoringService._send_trial_connect_reminder.__get__(w.service)
    w.queue.items = {w.user.remnawave_uuid: m.time.time() - 1}

    async def slow(*args):
        await asyncio.sleep(0.01)

    w.api.get_user_devices_all.side_effect = slow
    await w.service._check_trial_connect_reminders(w.db)
    assert contexts == [15]
    assert not w.queue.items
    w.service.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_device_event_nx_and_notification_toggle_independent(wired, monkeypatch):
    w = wired
    monkeypatch.setattr('app.config.settings.WEBHOOK_NOTIFY_DEVICES', False)
    handler = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    handler._notify_user = AsyncMock()
    handler._get_subscription_keyboard = lambda user: None
    w.queue.items.clear()
    await handler._handle_device_added(w.db, w.user, w.sub, {'uuid': w.user.remnawave_uuid})
    original = dict(w.queue.items)
    assert original[w.user.remnawave_uuid] > m.time.time() + 899
    monkeypatch.setattr(Settings, 'get_connect_reminder_minutes', staticmethod(lambda: 60))
    await handler._handle_device_added(w.db, w.user, w.sub, {'uuid': w.user.remnawave_uuid})
    assert w.queue.items == original
    handler._notify_user.assert_awaited()


@pytest.mark.asyncio
async def test_sla_disabled_still_processes_reminders(wired, monkeypatch):
    w = wired

    @asynccontextmanager
    async def session():
        yield w.db

    monkeypatch.setattr(m, 'AsyncSessionLocal', session)
    monkeypatch.setattr(m.settings, 'SUPPORT_TICKET_SLA_ENABLED', False)
    monkeypatch.setattr('app.services.support_settings_service.SupportSettingsService.get_sla_enabled', lambda: False)
    w.service.is_running = True

    async def stop(seconds):
        w.service.is_running = False

    monkeypatch.setattr(m.asyncio, 'sleep', stop)
    await w.service._sla_loop()
    w.service.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'state,expected', [('none', 1), ('mark', 0), ('live', 0), ('stale', 1), ('redis-down', 1), ('disabled', 1)]
)
async def test_letter_filter_before_panel_and_again_before_send(wired, state, expected):
    w = wired
    w.queue.items.clear()
    if state == 'mark':
        w.marks.add('trial_connect_reminder')
    if state == 'live':
        w.queue.items[w.user.remnawave_uuid] = m.time.time() - 10
    if state == 'stale':
        w.queue.items[w.user.remnawave_uuid] = m.time.time() - 1801
    if state == 'redis-down':
        w.queue.broken = True
    if state == 'disabled':
        w.flags['trial_connect_reminder'] = False
        w.queue.items[w.user.remnawave_uuid] = m.time.time()
    w.db.execute.return_value = Ns(scalars=lambda: Ns(all=lambda: [w.sub]))
    w.service._fetch_trial_panel_usage = AsyncMock(return_value={w.user.remnawave_uuid: 0})
    w.service._send_trial_not_connected_notification = AsyncMock(return_value=True)
    await w.service._check_trial_not_connected(w.db)
    assert w.service._fetch_trial_panel_usage.await_count == expected
    assert w.service._send_trial_not_connected_notification.await_count == expected


@pytest.mark.asyncio
async def test_letter_rechecks_marker_after_panel_fetch(wired):
    w = wired
    w.queue.items.clear()
    w.db.execute.return_value = Ns(scalars=lambda: Ns(all=lambda: [w.sub]))

    async def usage():
        w.marks.add('trial_connect_reminder')
        return {w.user.remnawave_uuid: 0}

    w.service._fetch_trial_panel_usage = usage
    w.service._send_trial_not_connected_notification = AsyncMock()
    await w.service._check_trial_not_connected(w.db)
    w.service._send_trial_not_connected_notification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('marked,expected', [(True, 0), (False, 1)])
async def test_first_connected_suppressed_only_by_own_marker(wired, monkeypatch, marked, expected):
    w = wired
    monkeypatch.setattr('app.database.crud.notification.notification_sent', AsyncMock(return_value=marked))
    handler = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    handler._notify_user = AsyncMock()
    handler._get_subscription_keyboard = lambda user: None
    await handler._handle_first_connected(w.db, w.user, w.sub, {})
    assert handler._notify_user.await_count == expected
    # First connection must never schedule a reminder.
    assert len(w.queue.items) == 1


@pytest.mark.asyncio
async def test_reset_clears_all_panel_ids_even_after_local_uuid_erased(wired, monkeypatch):
    from app.services import account_test_reset_service as reset, user_service as users

    w = wired
    w.user.test_reset_panel_uuids = ['old-profile']
    w.user.device_addon_generation = 2
    w.db.scalar.return_value = w.user
    w.db.scalars.return_value = Ns(all=lambda: ['subscription-profile'])
    for name in ('reset_bypass', 'lock_reset_platega_rows', 'lock_reset_device_addon_rows', 'lock_reset_rows'):
        monkeypatch.setattr(reset, name, AsyncMock())
    monkeypatch.setattr(
        'app.services.device_addon_payment_service.drain_device_addon_paid_effects_for_reset',
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(users, 'is_test_account', lambda user: True)
    monkeypatch.setattr(users, '_test_reset_blocked_reason', AsyncMock(return_value=None))

    async def perform(db, current, admin_id, *, confirm):
        if confirm:
            current.remnawave_uuid = None
            current.test_reset_state = 'ready'
        return Ns(done=confirm, preview_token='preview-token')

    monkeypatch.setattr(users, '_reset_test_account_unlocked', perform)
    monkeypatch.setattr('app.utils.funnel_notify.forget_referral_onboarding', AsyncMock())
    w.queue.items.update({'old-profile': 0, 'subscription-profile': 0, 'another-user': 0})
    result = await reset._run_reset_under_account_activity(
        w.db, user_id=w.user.id, admin_id=3, preview_token='preview-token'
    )
    assert result.done is True
    assert w.queue.items == {'another-user': 0}


@pytest.mark.asyncio
async def test_redis_unavailable_does_not_change_device_notification(wired, monkeypatch):
    w = wired
    w.queue.broken = True
    handler = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    handler._notify_user = AsyncMock()
    handler._get_subscription_keyboard = lambda user: None
    await handler._handle_device_added(w.db, w.user, w.sub, {'uuid': w.user.remnawave_uuid})
    handler._notify_user.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        {'uuid': 'old-profile'},
        {'userUuid': 'old-profile'},
        {'user': {'uuid': 'old-profile'}},
        {},
        {'user': 'malformed'},
    ],
)
async def test_delayed_foreign_device_event_never_schedules_current_profile(wired, payload):
    w = wired
    w.queue.items.clear()
    handler = RemnaWaveWebhookService.__new__(RemnaWaveWebhookService)
    handler._notify_user = AsyncMock()
    handler._get_subscription_keyboard = lambda user: None
    await handler._handle_device_added(w.db, w.user, w.sub, payload)
    assert not w.queue.items


@pytest.mark.asyncio
@pytest.mark.parametrize('first', ['reminder', 'letter'])
async def test_two_different_senders_share_transaction_lock_until_mark(wired, first):
    w = wired
    lock = asyncio.Lock()
    started, release = asyncio.Event(), asyncio.Event()

    class Session:
        def __init__(self):
            self.held = False

        async def execute(self, query):
            if 'pg_advisory_xact_lock' in str(query):
                await lock.acquire()
                self.held = True
            return Ns(scalars=lambda: Ns(all=lambda: [w.sub]))

        async def commit(self):
            if self.held:
                self.held = False
                lock.release()

        rollback = commit

        async def refresh(self, obj, fields):
            pass

    reminder_db, letter_db = Session(), Session()

    async def record(db, uid, sid, kind):
        w.marks.add(kind)
        await db.commit()

    async def new_message(**kwargs):
        started.set()
        await release.wait()
        return Ns(message_id=42)

    async def old_message(*args):
        w.queue.items[w.user.remnawave_uuid] = m.time.time() - 1
        started.set()
        await release.wait()
        return True

    from unittest.mock import patch

    with patch.object(m, 'record_notification', record):
        w.service.bot.send_message.side_effect = new_message
        w.service._send_trial_not_connected_notification = AsyncMock(side_effect=old_message)

        async def usage():
            return {w.user.remnawave_uuid: 0}

        w.service._fetch_trial_panel_usage = usage
        if first == 'letter':
            w.queue.items.clear()
        jobs = [
            lambda: w.service._check_trial_connect_reminders(reminder_db),
            lambda: w.service._check_trial_not_connected(letter_db),
        ]
        if first == 'letter':
            jobs.reverse()
        task1 = asyncio.create_task(jobs[0]())
        await asyncio.wait_for(started.wait(), 1)
        task2 = asyncio.create_task(jobs[1]())
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(task1, task2)
        assert (
            w.service.bot.send_message.await_count + w.service._send_trial_not_connected_notification.await_count == 1
        )
        assert w.marks == {'trial_connect_reminder' if first == 'reminder' else 'trial_not_connected'}


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['sent', 'queued'])
async def test_letter_skip_has_a_subscription_log(wired, monkeypatch, reason):
    from unittest.mock import Mock

    w = wired
    if reason == 'sent':
        w.marks.add('trial_connect_reminder')
    info = Mock()
    monkeypatch.setattr(m.logger, 'debug', info)
    assert await w.service._has_trial_connect_reminder(w.db, w.sub) is True
    info.assert_called_once_with('trial_not_connected_skipped_reminder', subscription_id=w.sub.id, reason=reason)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['paid', 'deleted', 'reset', 'uuid', 'no-squads', 'ending', 'disabled'])
async def test_runtime_state_is_refreshed_after_panel_io(wired, change):
    w = wired
    persisted = {}
    devices = w.api.get_user_devices_all.return_value

    async def panel(uuid):
        if change == 'paid':
            persisted['is_trial'] = False
        if change == 'deleted':
            persisted['user_status'] = 'deleted'
        if change == 'reset':
            persisted['test_reset_state'] = 'resetting'
        if change == 'uuid':
            persisted['remnawave_uuid'] = 'new-profile'
        if change == 'no-squads':
            persisted['connected_squads'] = []
        if change == 'ending':
            persisted['end_date'] = datetime.now(UTC) + timedelta(minutes=59)
        if change == 'disabled':
            w.flags['trial_connect_reminder'] = False
        return devices

    async def refresh(obj, fields):
        for field in fields:
            key = 'user_status' if obj is w.user and field == 'status' else field
            if key in persisted:
                setattr(obj, field, persisted[key])

    w.api.get_user_devices_all.side_effect = panel
    w.db.refresh.side_effect = refresh
    await w.service._check_trial_connect_reminders(w.db)
    w.service.bot.send_message.assert_not_awaited()
    w.record.assert_not_awaited()
    assert not w.queue.items
    assert w.db.refresh.await_count == 2


@pytest.mark.asyncio
async def test_delivery_and_record_survive_preflight_timeout_budget(wired, monkeypatch):
    w = wired
    original_timeout = asyncio.timeout
    monkeypatch.setattr(m.asyncio, 'timeout', lambda seconds: original_timeout(0.001))

    async def send(**kwargs):
        await asyncio.sleep(0.02)
        return Ns(message_id=42)

    async def record(db, uid, sid, kind):
        await asyncio.sleep(0.02)
        w.marks.add(kind)

    w.service.bot.send_message.side_effect = send
    w.record.side_effect = record
    await w.service._check_trial_connect_reminders(w.db)
    w.service.bot.send_message.assert_awaited_once()
    w.record.assert_awaited_once()
    assert w.marks == {'trial_connect_reminder'}
    assert not w.queue.items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'case,reason,known_user',
    [
        ('stale', 'stale', True),
        ('traffic', 'traffic_used', True),
        ('unknown', 'traffic_unknown', True),
        ('application', 'application', True),
        ('conditions', 'conditions', True),
        ('mark', 'notified', True),
        ('disabled', 'disabled', True),
        ('error', 'error', True),
        ('missing', 'conditions', False),
        ('devices', 'devices', True),
        ('claimed', 'claimed', True),
        ('timeout', 'timeout', True),
    ],
)
async def test_each_discard_has_one_private_reason(wired, monkeypatch, case, reason, known_user):
    from unittest.mock import Mock

    w = wired
    if case == 'stale':
        w.queue.items[w.user.remnawave_uuid] = m.time.time() - 1801
    if case == 'traffic':
        w.api.get_user_lifetime_traffic_once.return_value = VPN_USED_MIN_BYTES
    if case == 'unknown':
        w.api.get_user_lifetime_traffic_once.return_value = None
    if case == 'application':
        w.api.get_user_devices_all.return_value = {
            'devices': [{'createdAt': '2026-10-18T15:00:00Z', 'userAgent': 'Other/1'}]
        }
    if case == 'conditions':
        w.sub.is_trial = False
    if case == 'mark':
        w.marks.add('trial_not_connected')
    if case == 'disabled':
        w.flags['trial_connect_reminder'] = False
    if case == 'error':
        w.api.get_user_devices_all.side_effect = OSError(w.user.remnawave_uuid)
    if case == 'missing':
        monkeypatch.setattr(m, 'get_user_by_remnawave_uuid', AsyncMock(return_value=None))
    if case == 'devices':
        w.api.get_user_devices_all.return_value = {'devices': []}
    if case == 'claimed':
        data = w.api.get_user_devices_all.return_value

        async def devices(uuid):
            w.queue.items.clear()
            return data

        w.api.get_user_devices_all.side_effect = devices
    if case == 'timeout':
        real_timeout = asyncio.timeout
        monkeypatch.setattr(m.asyncio, 'timeout', lambda seconds: real_timeout(0.001))

        async def slow(uuid):
            await asyncio.sleep(0.02)

        w.api.get_user_devices_all.side_effect = slow
    info = Mock()
    monkeypatch.setattr(m.logger, 'info', info)
    await w.service._check_trial_connect_reminders(w.db)
    info.assert_called_once_with(
        'trial_connect_reminder_skipped', reason=reason, user_id=w.user.id if known_user else None
    )
    assert w.user.remnawave_uuid not in repr(info.call_args_list)
    assert not w.queue.items
    w.service.bot.send_message.assert_not_awaited()
    w.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_english_reminder_buttons_and_unchanged_old_letter(wired):
    w = wired
    w.user.language = 'en'
    await w.service._check_trial_connect_reminders(w.db)
    payload = w.service.bot.send_message.await_args.kwargs
    assert 'One step left' in payload['text']
    labels = [button.text for row in payload['reply_markup'].inline_keyboard for button in row]
    assert labels == ['📲 Connect', '💬 Contact support']
    old = [button.text for row in w.service._trial_connection_keyboard().inline_keyboard for button in row]
    assert old == ['📲 Подключиться', '💬 Написать в поддержку']


@pytest.mark.asyncio
async def test_redis_warning_only_enabled_once_per_ten_minutes(wired, monkeypatch):
    from unittest.mock import Mock

    w = wired
    w.queue.broken = True
    clock = [0]
    monkeypatch.setattr(q, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(q, '_last_warning', float('-inf'))
    warning = Mock()
    monkeypatch.setattr(q.logger, 'warning', warning)
    w.flags['trial_connect_reminder'] = False
    assert await q.trial_connect_queue('zscore', w.user.remnawave_uuid) is None
    warning.assert_not_called()
    w.flags['trial_connect_reminder'] = True
    for tick in [0, 1, 60, 599, 600, 601]:
        clock[0] = tick
        assert await q.trial_connect_queue('zscore', w.user.remnawave_uuid) is None
    assert warning.call_count == 2
    assert all(
        call.args == ('trial_connect_reminder_redis_unavailable',) and not call.kwargs
        for call in warning.call_args_list
    )


@pytest.mark.asyncio
async def test_redis_lookup_timeout_allows_the_old_letter(wired, monkeypatch):
    w = wired

    async def slow(*args):
        await asyncio.sleep(0.02)

    w.queue.zscore = slow
    real_timeout = asyncio.timeout
    budgets = []
    monkeypatch.setattr(m.asyncio, 'timeout', lambda seconds: (budgets.append(seconds), real_timeout(0.001))[1])
    w.db.execute.return_value = Ns(scalars=lambda: Ns(all=lambda: [w.sub]))
    w.service._fetch_trial_panel_usage = AsyncMock(return_value={w.user.remnawave_uuid: 0})
    w.service._send_trial_not_connected_notification = AsyncMock(return_value=True)
    await w.service._check_trial_not_connected(w.db)
    assert budgets == [0.25, 0.25]
    w.service._fetch_trial_panel_usage.assert_awaited_once()
    w.service._send_trial_not_connected_notification.assert_awaited_once()


def test_card_explains_new_devices_only():
    from app.cabinet.routes.admin_auto_messages import CATALOG_BY_ID, _params_for, _resolve_when

    entry = CATALOG_BY_ID['trial-connect-reminder']
    assert 'Действует для устройств, добавленных после включения.' in _resolve_when(entry, _params_for(entry))


@pytest.mark.asyncio
async def test_malformed_queue_member_is_removed_and_logged(wired, monkeypatch):
    from unittest.mock import Mock

    w = wired
    w.queue.items = {bytes([255]): m.time.time() - 1}
    info = Mock()
    monkeypatch.setattr(m.logger, 'info', info)
    await w.service._check_trial_connect_reminders(w.db)
    info.assert_called_once_with('trial_connect_reminder_skipped', reason='error', user_id=None)
    assert not w.queue.items
    w.service.bot.send_message.assert_not_awaited()

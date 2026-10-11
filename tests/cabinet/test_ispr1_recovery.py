"""ИСПР-1/B: настоящие решения и SQL SQLite; деньги и сеть на существующих границах."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.database.models import PlategaPayment, Transaction, User
from app.services import device_first_checkout_service as dfc, device_first_recovery_service as recovery
from app.services.reporting_service import ReportingService
from tests.cabinet.test_vk16_topup_autocomplete import (  # noqa: F401
    _add_checkout,
    _paid,
    _pay,
    buy,
    checkouts,
    webhook,
)
from tests.cabinet.test_vk16_topup_intent import db, env, session  # noqa: F401


def _orphan(session, *, age=10, final=None, **intent):
    payment = _paid(session, created_ago=timedelta(minutes=20), **intent)
    deposit = Transaction(
        user_id=1,
        type='deposit',
        amount_kopeks=payment.amount_kopeks,
        is_completed=True,
        completed_at=datetime.now(UTC) - timedelta(minutes=age),
    )
    session.add(deposit)
    session.flush()
    payment.transaction_id = deposit.id
    if final:
        payment.metadata_json = {
            **payment.metadata_json,
            dfc.TOPUP_INTENT_KEY: {**dfc.topup_intent_of(payment), **final},
        }
    session.commit()
    return payment


@pytest.fixture
def worker(db, monkeypatch):
    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(recovery, 'AsyncSessionLocal', factory)
    monkeypatch.setattr('app.services.reporting_service.AsyncSessionLocal', factory)
    carts = SimpleNamespace(delete_user_cart=AsyncMock(), clear_topup_intent=AsyncMock())
    monkeypatch.setattr('app.services.user_cart_service.user_cart_service', carts)
    erasure = AsyncMock(return_value=False)
    email = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.account_erasure_service.mark_late_legacy_payment_for_manual_review', erasure)
    monkeypatch.setattr('app.services.payment.common.notify_email_user_topup', email)
    alarms = AsyncMock()
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService._send_message', alarms)
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(
        service=recovery.DeviceFirstRecoveryService(), bot=bot, carts=carts, erasure=erasure, email=email, alarms=alarms
    )


async def test_sweep_completes_one_charge_and_one_notice_clears_cart_first(session, buy, worker):
    _orphan(session)

    async def message(*args, **kwargs):
        assert worker.carts.delete_user_cart.await_count == worker.carts.clear_topup_intent.await_count == 1

    worker.bot.send_message.side_effect = message
    await worker.service._sweep_topup_intents(bot=worker.bot)
    intent = dfc.topup_intent_of(session.get(PlategaPayment, 97))
    assert intent['status'] == 'fulfilled' and intent['recovered_at']
    assert intent['delivery'] == 'telegram'
    assert len(buy.commit) == worker.bot.send_message.await_count == 1
    worker.alarms.assert_not_awaited()
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert worker.bot.send_message.await_count == len(buy.commit) == 1


async def test_sweep_recognises_its_committed_fulfilling_order(session, buy, worker):
    _orphan(session, charging_checkout_id=400)
    _add_checkout(
        session,
        checkout_id=400,
        source='topup_intent',
        lifecycle_state='fulfilling',
        financial_committed_at=datetime.now(UTC),
        funding_mode='wallet',
    )
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert dfc.topup_intent_of(session.get(PlategaPayment, 97))['checkout_public_id'] == 'chk-400'
    assert buy.create == buy.commit == []
    worker.bot.send_message.assert_awaited_once()


@pytest.mark.parametrize('kind', ['young', 'old', 'webhook_done', 'chargeback'])
async def test_sweep_skips_ineligible_receipts(session, buy, worker, kind):
    p = _orphan(session, age=1 if kind == 'young' else 1500 if kind == 'old' else 10)
    if kind == 'webhook_done':
        p.metadata_json = {
            **p.metadata_json,
            'balance_credited': True,
            dfc.TOPUP_INTENT_KEY: {
                **dfc.topup_intent_of(p),
                'decided_at': datetime.now(UTC).isoformat(),
                'status': 'fulfilled',
            },
        }
    if kind == 'chargeback':
        p.status = 'CHARGEBACKED'
    session.commit()
    before = dict(p.metadata_json)
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert p.metadata_json == before
    assert buy.create == buy.commit == []
    worker.bot.send_message.assert_not_awaited()


async def test_decided_but_not_announced_is_sent_once_without_another_charge(session, buy, worker):
    _orphan(
        session,
        final={'status': 'fulfilled', 'decided_at': datetime.now(UTC).isoformat(), 'checkout_public_id': 'paid-order'},
    )
    await worker.service._sweep_topup_intents(bot=worker.bot)
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert buy.commit == []
    worker.bot.send_message.assert_awaited_once()


async def test_forbidden_delivery_is_terminal_and_alerts_owner_once(session, buy, worker):
    _orphan(session)
    worker.bot.send_message.side_effect = TelegramForbiddenError(
        method=SendMessage(chat_id=1, text='test'), message='Forbidden'
    )
    await worker.service._sweep_topup_intents(bot=worker.bot)
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert dfc.topup_intent_of(session.get(PlategaPayment, 97))['delivery'] == 'failed'
    worker.bot.send_message.assert_awaited_once()
    worker.alarms.assert_awaited_once()


@pytest.mark.parametrize('reason', ['cancelled', 'replaced', 'disabled', 'account_erasure', 'technical_error'])
async def test_refusals_have_the_same_client_message_and_only_actionable_alarms(
    session, buy, worker, monkeypatch, reason
):
    p = _orphan(session)
    if reason in {'cancelled', 'replaced'}:
        p.metadata_json = {**p.metadata_json, dfc.TOPUP_INTENT_KEY: {**dfc.topup_intent_of(p), 'status': reason}}
    elif reason == 'disabled':
        monkeypatch.setattr(dfc, 'TOPUP_INTENT_ROLLOUT', 'off')
    elif reason == 'account_erasure':
        user = session.get(User, 1)
        user.account_erasure_requested_at = datetime.now(UTC)
        worker.erasure.return_value = True
    else:
        buy.commit_effect = AsyncMock(side_effect=RuntimeError('payment interrupted'))
    session.commit()
    await worker.service._sweep_topup_intents(bot=worker.bot)
    intent = dfc.topup_intent_of(session.get(PlategaPayment, 97))
    assert (intent['status'], intent['reason']) == ('refused', reason)
    worker.bot.send_message.assert_awaited_once()
    assert worker.alarms.await_count == int(reason in {'technical_error', 'account_erasure'})
    if reason == 'account_erasure':
        worker.erasure.assert_awaited_once()
        worker.email.assert_not_awaited()


async def test_email_only_customer_and_is_paid_false_are_not_filtered_out(session, buy, worker):
    p = _orphan(session)
    p.is_paid = False
    user = session.get(User, 1)
    user.telegram_id = None
    user.email = 'client@example.test'
    user.email_verified = True
    session.commit()
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert dfc.topup_intent_of(p)['delivery'] == 'email_or_cabinet'
    worker.email.assert_awaited_once()
    worker.bot.send_message.assert_not_awaited()


async def test_none_outcome_remains_retryable_and_quiet(session, buy, worker, monkeypatch):
    p = _orphan(session)
    monkeypatch.setattr(dfc, 'complete_topup_intent', AsyncMock(return_value=None))
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert not dfc.topup_intent_of(p).get('recovered_at')
    worker.bot.send_message.assert_not_awaited()
    worker.alarms.assert_not_awaited()


async def test_explicit_outcome_recording_failure_alerts_but_remains_retryable(session, buy, worker, monkeypatch):
    p = _orphan(session)
    monkeypatch.setattr(dfc, '_record_topup_intent_outcome', AsyncMock(side_effect=RuntimeError('record unavailable')))
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert not dfc.topup_intent_of(p).get('recovered_at')
    worker.alarms.assert_awaited_once()


async def test_sweep_failure_does_not_stop_run_once_and_uses_separate_cadence(worker, monkeypatch):
    for name in [
        'reconcile_device_first_payments',
        'process_direct_provisioning_outbox',
        'process_device_first_deposit_outbox',
        'process_device_first_notification_outbox',
    ]:
        monkeypatch.setattr(recovery, name, AsyncMock(return_value=2))
    monkeypatch.setattr('app.utils.funnel_notify.process_due_referral_onboarding_followups', AsyncMock())
    sweep = AsyncMock(side_effect=RuntimeError('sweep unavailable'))
    monkeypatch.setattr(worker.service, '_sweep_topup_intents', sweep)
    assert await worker.service.run_once(bot=worker.bot) == (2, 2, 2)
    assert await worker.service.run_once(bot=worker.bot) == (2, 2, 2)
    sweep.assert_awaited_once()


async def test_daily_report_has_only_nonzero_recovery_lines(session, buy, worker):
    p = _orphan(session, age=20)
    service = ReportingService()
    now = datetime.now(UTC)
    lines = await service._topup_intent_block(now - timedelta(days=1), now)
    assert lines == ['Оплаченные доплаты без итога дольше 15 минут: 1']
    p.metadata_json = {
        **p.metadata_json,
        dfc.TOPUP_INTENT_KEY: {
            **dfc.topup_intent_of(p),
            'status': 'refused',
            'decided_at': now.isoformat(),
            'recovered_at': now.isoformat(),
        },
    }
    session.commit()
    lines = await service._topup_intent_block(now - timedelta(days=1), now + timedelta(seconds=1))
    assert lines == ['Доплаты, оформленные после сбоя: 1 (отказ 1)']
    assert await service._topup_intent_block(now - timedelta(days=2), now - timedelta(days=1)) == []


async def test_at_most_three_receipts_and_failed_head_does_not_starve_tail(session, buy, worker):
    now = datetime.now(UTC).isoformat()
    for pid in range(97, 101):
        _orphan(
            session,
            payment_id=pid,
            final={'status': 'refused', 'reason': 'technical_error', 'decided_at': now} if pid < 100 else None,
        )
    worker.alarms.return_value = False
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert worker.bot.send_message.await_count == 3
    assert dfc.topup_intent_of(session.get(PlategaPayment, 100))['status'] == 'pending'
    await worker.service._sweep_topup_intents(bot=worker.bot)
    assert dfc.topup_intent_of(session.get(PlategaPayment, 100))['status'] == 'fulfilled'
    assert len(buy.commit) == 1
    assert worker.bot.send_message.await_count == 4  # неудачная тревога не повторяет клиентский чек


async def test_old_warning_once_per_worker_and_report_failure_is_isolated(session, buy, worker, monkeypatch):
    _orphan(session, age=1500)
    warning = Mock()
    monkeypatch.setattr(recovery.logger, 'warning', warning)
    await worker.service._sweep_topup_intents(bot=worker.bot)
    await worker.service._sweep_topup_intents(bot=worker.bot)
    warning.assert_called_once_with('topup_intent_sweep_too_old', payment_id=97)

    @asynccontextmanager
    async def broken():
        raise RuntimeError('counter database unavailable')
        yield

    monkeypatch.setattr('app.services.reporting_service.AsyncSessionLocal', broken)
    now = datetime.now(UTC)
    assert await ReportingService()._topup_intent_block(now - timedelta(days=1), now) == []


async def test_separate_cadence_runs_again_after_sixty_seconds(worker, monkeypatch):
    clock = [0]
    monkeypatch.setattr(recovery, 'monotonic', lambda: clock[0])
    for name in [
        'reconcile_device_first_payments',
        'process_direct_provisioning_outbox',
        'process_device_first_deposit_outbox',
        'process_device_first_notification_outbox',
    ]:
        monkeypatch.setattr(recovery, name, AsyncMock(return_value=2))
    monkeypatch.setattr('app.utils.funnel_notify.process_due_referral_onboarding_followups', AsyncMock())
    sweep = AsyncMock()
    monkeypatch.setattr(worker.service, '_sweep_topup_intents', sweep)
    await worker.service.run_once(bot=worker.bot)
    clock[0] = 59
    await worker.service.run_once(bot=worker.bot)
    sweep.assert_awaited_once()
    clock[0] = 60
    await worker.service.run_once(bot=worker.bot)
    assert sweep.await_count == 2


@pytest.mark.parametrize('language', ['ru', 'en'])
def test_fulfilled_receipt_bytes_are_preserved(language, monkeypatch):
    from app.config import settings
    from app.handlers.subscription.device_first import topup_intent_fulfilled_message

    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: 'https://t.me/support_test')
    expected = (
        '✅ <b>Payment received: 123 ₽</b>\n\n'
        'Your order is paid from the balance. When the subscription is connected, you will get the '
        '"Your VPN subscription is ready" message. If it has not arrived in 10 minutes, please '
        'contact support.\n\nBalance left: 50 ₽'
        if language == 'en'
        else '✅ <b>Оплата получена: 123 ₽</b>\n\n'
        'Заказ оформлен с баланса. Когда подписка подключится, придёт сообщение «Ваша VPN-подписка '
        'готова». Если его нет через 10 минут — напишите в поддержку.\n\nНа балансе осталось: 50 ₽'
    )
    message, markup = topup_intent_fulfilled_message(
        SimpleNamespace(language=language), amount_kopeks=12345, balance_kopeks=5037
    )
    assert message.encode() == expected.encode()
    assert markup.inline_keyboard[0][0].url == 'https://t.me/support_test'
    assert markup.inline_keyboard[0][0].text == ('Contact support' if language == 'en' else 'Написать в поддержку')


async def test_webhook_and_sweep_share_notice_lock_and_preserve_recovered_marker(
    db, session, buy, worker, webhook, monkeypatch
):
    """SQLite не имеет FOR UPDATE: моделируем только эксклюзивный lock; вызываем оба настоящих consumer пути."""
    from app.database.crud import platega as crud

    lock, owner = asyncio.Lock(), [None]
    original_get, original_commit, original_rollback = crud.get_platega_payment_by_id_for_update, db.commit, db.rollback

    async def get_locked(db_, payment_id):
        if owner[0] is not asyncio.current_task():
            await lock.acquire()
            owner[0] = asyncio.current_task()
        return await original_get(db_, payment_id)

    async def finish(method):
        try:
            await method()
        finally:
            if owner[0] is asyncio.current_task():
                owner[0] = None
                lock.release()

    monkeypatch.setattr(crud, 'get_platega_payment_by_id_for_update', get_locked)
    monkeypatch.setattr(dfc, 'get_platega_payment_by_id_for_update', get_locked)
    monkeypatch.setattr(db, 'commit', lambda: finish(original_commit))
    monkeypatch.setattr(db, 'rollback', lambda: finish(original_rollback))
    started, resume = asyncio.Event(), asyncio.Event()

    async def pause_webhook(*args):
        deposit = session.query(Transaction).filter_by(type='deposit').one()
        deposit.completed_at = datetime.now(UTC) - timedelta(minutes=10)
        session.commit()
        started.set()
        await resume.wait()

    monkeypatch.setattr('app.services.referral_service.process_referral_topup', pause_webhook)
    _paid(session, created_ago=timedelta(minutes=20))
    task = asyncio.create_task(_pay(db, webhook))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await worker.service._sweep_topup_intents(bot=webhook.bot)
    finally:
        resume.set()
    assert await task
    final = dfc.topup_intent_of(session.get(PlategaPayment, 97))
    assert final['recovered_at'] and final['notified_at']
    assert session.get(PlategaPayment, 97).metadata_json['balance_credited']
    assert len(buy.commit) == webhook.bot.send_message.await_count == 1


@pytest.mark.parametrize('marker_commit_failure', [False, True])
async def test_real_async_sessions_keep_loaded_user_and_show_the_message_commit_crash_window(
    tmp_path, monkeypatch, marker_commit_failure
):
    """Настоящие отдельные AsyncSession; SQLite не доказывает FOR UPDATE PostgreSQL."""
    import importlib
    import sys

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database.models import (
        PromoGroup,
        ServerSquad,
        Subscription,
        Tariff,
        UserPromoGroup,
        server_squad_promo_groups,
    )
    from app.services.payment import common

    stub = sys.modules.pop('aiosqlite', None)
    try:
        real_driver = importlib.import_module('aiosqlite')
    finally:
        if stub is not None:
            sys.modules['aiosqlite'] = stub
    monkeypatch.setitem(sys.modules, 'aiosqlite', real_driver)
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "recovery.db"}')
    models = (User, PlategaPayment, Transaction, Subscription, Tariff, PromoGroup, UserPromoGroup, ServerSquad)
    async with engine.begin() as connection:
        for table in [model.__table__ for model in models] + [server_squad_promo_groups]:
            columns = ', '.join('id INTEGER PRIMARY KEY' if c.name == 'id' else c.name for c in table.columns)
            await connection.execute(text(f'CREATE TABLE {table.name} ({columns})'))
        await connection.execute(text("INSERT INTO promo_groups (id, name, priority) VALUES (7, 'Базовая', 0)"))
        await connection.execute(
            text(
                'INSERT INTO users (id, telegram_id, balance_kopeks, status, language, promo_group_id) '
                "VALUES (1, 777001, 13537, 'active', 'ru', 7)"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with factory() as setup:
        setup.add(
            Transaction(
                id=2,
                user_id=1,
                type='deposit',
                amount_kopeks=9900,
                is_completed=True,
                completed_at=now - timedelta(minutes=10),
            )
        )
        setup.add(
            PlategaPayment(
                id=97,
                user_id=1,
                amount_kopeks=9900,
                currency='RUB',
                status='CONFIRMED',
                is_paid=True,
                correlation_id='corr97',
                transaction_id=2,
                metadata_json={
                    dfc.TOPUP_INTENT_KEY: {
                        'status': 'fulfilled',
                        'decided_at': now.isoformat(),
                        'created_at': (now - timedelta(minutes=20)).isoformat(),
                        'checkout_public_id': 'already-paid',
                        'period_days': 30,
                        'devices': 1,
                        'quote_kopeks': 14900,
                    }
                },
            )
        )
        await setup.commit()
    monkeypatch.setattr(dfc, '_topup_intent_session', factory)
    monkeypatch.setattr(recovery, 'AsyncSessionLocal', factory)
    carts = SimpleNamespace(delete_user_cart=AsyncMock(), clear_topup_intent=AsyncMock())
    monkeypatch.setattr('app.services.user_cart_service.user_cart_service', carts)
    alarms = AsyncMock(return_value=True)
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService._send_message', alarms)
    bot = SimpleNamespace(send_message=AsyncMock())
    real_notice, first = common.send_topup_intent_notice, True

    async def notice(db_, **kwargs):
        nonlocal first
        assert kwargs['user'].get_primary_promo_group().id == 7  # нет ленивого I/O после rollback
        if first and marker_commit_failure:
            first = False
            original = db_.commit
            db_.commit = AsyncMock(side_effect=RuntimeError('notification marker commit interrupted'))
            try:
                return await real_notice(db_, **kwargs)
            finally:
                db_.commit = original
        return await real_notice(db_, **kwargs)

    monkeypatch.setattr(common, 'send_topup_intent_notice', notice)
    try:
        service = recovery.DeviceFirstRecoveryService()
        await service._sweep_topup_intents(bot=bot)
        if marker_commit_failure:
            async with factory() as reader:
                stored = dfc.topup_intent_of(await reader.get(PlategaPayment, 97))
                assert not stored.get('notified_at') and not stored.get('recovered_at')
        await service._sweep_topup_intents(bot=bot)
        async with factory() as reader:
            final = dfc.topup_intent_of(await reader.get(PlategaPayment, 97))
            assert final['recovered_at'] and final['notified_at'] and final['delivery'] == 'telegram'
            assert final['checkout_public_id'] == 'already-paid'
            assert (await reader.get(User, 1)).balance_kopeks == 13537
        assert bot.send_message.await_count == (2 if marker_commit_failure else 1)
        assert alarms.await_count == int(marker_commit_failure)
    finally:
        await engine.dispose()

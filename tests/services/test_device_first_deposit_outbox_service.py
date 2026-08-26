from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import TransactionType
from app.services import device_first_deposit_outbox_service as service


class Result:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = values

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.values or []


@pytest.mark.asyncio
async def test_reward_ledger_key_prevents_duplicate_balance_credit():
    recipient = SimpleNamespace(id=8, balance_kopeks=1000, updated_at=None)
    source = SimpleNamespace(id=55, device_first_checkout_id=13)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(None)),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    created = await service._add_reward(
        db,
        recipient=recipient,
        source=source,
        amount_kopeks=2500,
        ledger_suffix='inviter-first-reward',
        description='reward',
    )

    assert recipient.balance_kopeks == 3500
    assert created.device_first_ledger_key == 'deposit-side-effect:55:inviter-first-reward'
    assert created.type == TransactionType.REFERRAL_REWARD.value
    db.add.assert_called_once_with(created)

    db.execute.return_value = Result(created)
    db.add.reset_mock()
    repeated = await service._add_reward(
        db,
        recipient=recipient,
        source=source,
        amount_kopeks=2500,
        ledger_suffix='inviter-first-reward',
        description='reward',
    )

    assert repeated is created
    assert recipient.balance_kopeks == 3500
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_first_referral_payment_financial_steps_finish_in_one_commit(monkeypatch):
    job = SimpleNamespace(id=4, transaction_id=55, checkout_id=13, referral_status='pending', updated_at=None)
    source = SimpleNamespace(
        id=55,
        user_id=1,
        amount_kopeks=50000,
        type=TransactionType.DEPOSIT.value,
        device_first_checkout_id=13,
    )
    user = SimpleNamespace(
        id=1,
        referred_by_id=2,
        has_made_first_topup=False,
        full_name='Referral',
    )
    referrer = SimpleNamespace(id=2, balance_kopeks=0, full_name='Inviter')
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(job),
                Result(source),
                Result(values=[user, referrer]),
                Result(),
                # Постановка сообщения о награде (РФ-1 п.1.3) спрашивает, платили ли по заказу.
                # Здесь `_add_reward` подменён, наградных строк нет — сообщение не ставится.
                Result(),
            ]
        ),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )
    add_reward = AsyncMock()
    add_earning = AsyncMock()
    monkeypatch.setattr(service, 'get_user_campaign_id', AsyncMock(return_value=9))
    monkeypatch.setattr(service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(service, 'calculate_referral_commission_percent', AsyncMock(return_value=15))
    monkeypatch.setattr(service, '_add_reward', add_reward)
    monkeypatch.setattr(service, '_add_referral_earning', add_earning)
    monkeypatch.setattr(service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)

    await service._apply_referral_step(db, job_id=job.id)

    assert user.has_made_first_topup is True
    assert add_reward.await_count == 2
    assert add_reward.await_args_list[0].kwargs['amount_kopeks'] == 5000
    assert add_reward.await_args_list[1].kwargs['amount_kopeks'] == 12500
    add_earning.assert_awaited_once()
    assert add_earning.await_args.kwargs['source'] is source
    assert add_earning.await_args.kwargs['reason'] == 'referral_first_topup'
    assert job.referral_status == 'done'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_referral_step_failure_does_not_mark_job_done(monkeypatch):
    job = SimpleNamespace(id=4, transaction_id=55, checkout_id=13, referral_status='pending', updated_at=None)
    source = SimpleNamespace(
        id=55,
        user_id=1,
        amount_kopeks=50000,
        type=TransactionType.DEPOSIT.value,
        device_first_checkout_id=13,
    )
    user = SimpleNamespace(
        id=1,
        referred_by_id=2,
        has_made_first_topup=False,
        full_name='Referral',
    )
    referrer = SimpleNamespace(id=2, balance_kopeks=0, full_name='Inviter')
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(job),
                Result(source),
                Result(values=[user, referrer]),
                Result(),
                # Постановка сообщения о награде (РФ-1 п.1.3) спрашивает, платили ли по заказу.
                # Здесь `_add_reward` подменён, наградных строк нет — сообщение не ставится.
                Result(),
            ]
        ),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(service, 'calculate_referral_commission_percent', AsyncMock(return_value=15))
    monkeypatch.setattr(service, '_add_reward', AsyncMock(side_effect=RuntimeError('db interrupted')))
    monkeypatch.setattr(service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)

    with pytest.raises(RuntimeError, match='db interrupted'):
        await service._apply_referral_step(db, job_id=job.id)

    assert job.referral_status == 'pending'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_referral_first_topup_marker_is_durable_and_idempotent():
    job = SimpleNamespace(id=4, transaction_id=55, checkout_id=13, referral_status='pending', updated_at=None)
    source = SimpleNamespace(
        id=55,
        user_id=1,
        amount_kopeks=500,
        type=TransactionType.DEPOSIT.value,
        device_first_checkout_id=13,
    )
    user = SimpleNamespace(id=1, referred_by_id=None, has_made_first_topup=False)
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(job),
                Result(source),
                Result(values=[user]),
            ]
        ),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )

    await service._apply_referral_step(db, job_id=job.id)

    assert user.has_made_first_topup is True
    assert job.referral_status == 'done'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_fulfillment_step_recovers_after_credit_commit(monkeypatch):
    job = SimpleNamespace(
        id=4,
        checkout_id=13,
        fulfillment_status='pending',
        updated_at=None,
    )
    checkout = SimpleNamespace(public_id='checkout-1', user_id=7)
    fulfilled = SimpleNamespace(
        fulfillment_state='fulfilled',
        lifecycle_state='fulfilling',
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(job), Result(job)]),
        get=AsyncMock(return_value=checkout),
        commit=AsyncMock(),
    )
    fulfill = AsyncMock(return_value=fulfilled)
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.fulfill_checkout',
        fulfill,
    )

    await service._apply_fulfillment_step(db, job_id=job.id)

    fulfill.assert_awaited_once_with(db, 'checkout-1', 7)
    assert job.fulfillment_status == 'done'
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_a_paid_referral_queues_a_message_so_the_partner_learns_about_it():
    """РФ-1 п.1.3: партнёр обязан УЗНАТЬ о комиссии, а не только получить её.

    До этапа device-first платил молча: бота в этой цепочке нет ни в одном из трёх мест
    вызова, поэтому сообщение ставится в очередь, у которой бот уже есть.
    """
    from app.database.models import DeviceFirstNotificationOutbox

    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Result(value=777),  # наградная строка по заказу есть — платили
                Result(value=None),  # сообщение по этому заказу ещё не ставили
            ]
        ),
        add=MagicMock(side_effect=added.append),
    )

    await service._queue_referral_reward_notification(db, checkout_id=13)

    assert len(added) == 1, 'сообщение обязано быть поставлено ровно один раз'
    assert isinstance(added[0], DeviceFirstNotificationOutbox)
    assert added[0].checkout_id == 13
    assert added[0].notification_type == 'referral_reward'


@pytest.mark.asyncio
async def test_no_message_is_queued_when_nobody_was_paid():
    """Улика против совпадения: без наградной строки сообщение не ставится.

    Иначе тест выше проходил бы и на коде, который ставит сообщение всегда, — а это обещание
    денег тому, кому их не начислили.
    """
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(value=None)]),
        add=MagicMock(side_effect=added.append),
    )

    await service._queue_referral_reward_notification(db, checkout_id=13)

    assert added == []

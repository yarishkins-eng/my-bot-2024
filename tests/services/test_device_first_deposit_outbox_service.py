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


# --- РФ-1: сторожа на НЕСУЩИЕ строки. Найдено ревью: прежние проверяли kwarg на моке,
# то есть договорённость, а не поведение. Четыре мутации переживали полный прогон.


@pytest.mark.asyncio
async def test_switch_off_closes_the_referral_step_at_creation_time():
    """`pay_referral=False` обязан закрыть шаг выплаты СРАЗУ, а не оставить его ждать.

    Мутация «игнорировать kwarg» переживала весь набор: работа заводилась `pending`, и
    выключатель не останавливал ничего.
    """
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(value=None)),
        add=MagicMock(side_effect=added.append),
        flush=AsyncMock(),
    )

    row = await service.ensure_deposit_outbox(db, transaction_id=55, checkout_id=13, pay_referral=False)

    assert row.referral_status == 'done', 'выключенная программа обязана закрывать шаг выплаты'
    assert row.event_status == 'pending', 'событие о пополнении выключателя не касается'


@pytest.mark.asyncio
async def test_receipt_job_never_announces_a_wallet_top_up():
    """`emit_deposit_event=False` обязан закрыть шаг события.

    Иначе на приход от банка уходит событие «баланс пополнен» с жёстко зашитыми типом и
    способом — ложь о пополнении, которого не было.
    """
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(value=None)),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    row = await service.ensure_deposit_outbox(db, transaction_id=56, checkout_id=14, emit_deposit_event=False)

    assert row.event_status == 'done', 'приход — не пополнение кошелька'
    assert row.referral_status == 'pending', 'выплату это не отменяет'


@pytest.mark.asyncio
async def test_the_payout_step_looks_for_the_bank_receipt_and_not_only_for_a_top_up(monkeypatch):
    """Сторож на САМ ЗАПРОС: сузишь тип обратно до пополнения — покраснеет.

    Мок не исполняет `WHERE`, поэтому проверяем скомпилированный запрос — приём уже
    применяется в проекте. Без этого сторожа мутация «вернуть == DEPOSIT» проходила весь
    набор из 3096 тестов, а на боевом означала бы, что комиссия не платится НИКОГДА.
    """
    job = SimpleNamespace(id=4, transaction_id=55, checkout_id=13, referral_status='pending', updated_at=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(job), RuntimeError('стоп')]), commit=AsyncMock())

    with pytest.raises(RuntimeError):
        await service._apply_referral_step(db, job_id=job.id)

    source_query = db.execute.await_args_list[1].args[0]
    compiled = str(source_query.compile(compile_kwargs={'literal_binds': True}))
    assert 'provider_receipt' in compiled, 'шаг выплаты обязан видеть приход от банка'
    assert 'deposit' in compiled, 'и не должен потерять кошельковое пополнение'


@pytest.mark.asyncio
async def test_the_payout_step_always_queues_the_message_before_closing_itself(monkeypatch):
    """Третья несущая строка цепочки: постановка сообщения из самого шага выплаты.

    Мутация «убрать вызов» переживала полный прогон — партнёр снова получал бы деньги молча.
    Проверяем ПЛАТЯЩИЙ случай: без пригласившего сообщение и не должно ставиться, и такой
    тест мутацию бы не поймал.
    """
    job = SimpleNamespace(id=4, transaction_id=55, checkout_id=13, referral_status='pending', updated_at=None)
    source = SimpleNamespace(id=55, user_id=1, amount_kopeks=64900, device_first_checkout_id=13)
    user = SimpleNamespace(id=1, referred_by_id=2, has_made_first_topup=False, full_name='Друг')
    referrer = SimpleNamespace(id=2, balance_kopeks=0, full_name='Пригласивший')
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[Result(job), Result(source), Result(values=[user, referrer]), Result()]),
        get=AsyncMock(return_value=user),
        commit=AsyncMock(),
    )
    queued = AsyncMock()
    monkeypatch.setattr(service, '_queue_referral_reward_notification', queued)
    monkeypatch.setattr(service, '_add_reward', AsyncMock())
    monkeypatch.setattr(service, '_add_referral_earning', AsyncMock())
    monkeypatch.setattr(service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(service, 'calculate_referral_commission_percent', AsyncMock(return_value=25))
    monkeypatch.setattr(service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 10000)
    monkeypatch.setattr(service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 10000)

    await service._apply_referral_step(db, job_id=job.id)

    queued.assert_awaited_once_with(db, checkout_id=13)
    assert job.referral_status == 'done'

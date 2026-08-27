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
    # 🔴 Найдено скептиком: проверка НАЛИЧИЯ пропускала добавление лишнего типа. Списание за
    # подписку — те же деньги с обратным знаком; попади оно в фильтр, комиссия ушла бы дважды
    # с одной покупки, и весь набор остался бы зелёным.
    assert 'subscription_payment' not in compiled, 'списание в источник выплаты попасть не может'
    assert 'referral_reward' not in compiled, 'и сама награда тоже'


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


# ---------------------------------------------------------------------------
# РФ-2: сторожа на возврат исторического долга
# ---------------------------------------------------------------------------


def test_debt_list_is_frozen_and_matches_declared_total():
    """Перечень закрыт: ровно пять сверенных оплат и заявленная сумма.

    Ломается, если кто-то заменит перечень запросом или допишет туда оплаты 383/387 —
    первая уже оплачена, вторая прошла при выключенной программе (мина FO).
    """
    assert len(service.REFERRAL_DEBT_2026_08) == 5
    assert {row[0] for row in service.REFERRAL_DEBT_2026_08} == {193, 207, 232, 237, 370}
    assert 383 not in {row[0] for row in service.REFERRAL_DEBT_2026_08}
    assert 387 not in {row[0] for row in service.REFERRAL_DEBT_2026_08}
    # 🔴 Считаем из ЖИВЫХ настроек, а не из вшитых чисел: вшитые совпали бы с умолчаниями
    # кода, и тест остался бы зелёным на сервере с другими настройками — там, где выплата
    # как раз откажет навсегда с «расчёт разошёлся».
    pct = service.settings.REFERRAL_COMMISSION_PERCENT
    fixed = service.settings.REFERRAL_INVITER_BONUS_KOPEKS
    friend = service.settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS
    expected = 0
    for _tx, _checkout, _buyer, _referrer, amount in service.REFERRAL_DEBT_2026_08:
        # 237 — единственная повторная: у покупателя 206 бонус новичка уже выдан 25.08
        # по пополнению кошелька на 100 ₽.
        expected += amount * pct // 100 if _tx == 237 else fixed + amount * pct // 100 + friend
    assert expected == service.REFERRAL_DEBT_2026_08_TOTAL_KOPEKS, (
        f'при нынешних настройках ({pct}%, фикс {fixed}, новичку {friend}) долг равен {expected}, '
        f'а перечень заморожен на {service.REFERRAL_DEBT_2026_08_TOTAL_KOPEKS}. '
        'Выплата откажет — сначала разберитесь с настройками.'
    )
    assert service.REFERRAL_DEBT_2026_08_TOTAL_KOPEKS == 234925


@pytest.mark.asyncio
async def test_debt_payment_refuses_while_program_is_off(monkeypatch):
    """Выключатель — условие ОТКАЗА, а не параметр `pay_referral`.

    Передать его в `ensure_deposit_outbox` нельзя: работа завелась бы сразу со статусом
    'done', и долг стал бы неоплачиваемым НАВСЕГДА — обратного перевода статуса в коде нет.
    """
    monkeypatch.setattr(type(service.settings), 'is_referral_program_enabled', lambda self: False)
    ensure = AsyncMock()
    monkeypatch.setattr(service, 'ensure_deposit_outbox', ensure)
    plan = AsyncMock()
    monkeypatch.setattr(service, 'plan_referral_debt', plan)

    result = await service.pay_referral_debt(SimpleNamespace())

    assert result['paid'] is False
    assert 'выключена' in result['reason']
    ensure.assert_not_awaited()
    # Даже считать не начинаем: план не запрашивается.
    plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_debt_payment_is_all_or_nothing(monkeypatch):
    """Сумма разошлась с заявленной — не заводим ни одной работы."""
    monkeypatch.setattr(type(service.settings), 'is_referral_program_enabled', lambda self: True)
    ensure = AsyncMock()
    monkeypatch.setattr(service, 'ensure_deposit_outbox', ensure)
    monkeypatch.setattr(
        service,
        'plan_referral_debt',
        AsyncMock(
            return_value=[
                {
                    'transaction_id': 193,
                    'checkout_id': 14,
                    'buyer': None,
                    'referrer': None,
                    'paid_kopeks': 199000,
                    'commission_percent': 25,
                    'to_friend': 10000,
                    'to_referrer': 10000,  # заниженная награда: итог не сойдётся
                    'problems': [],
                }
            ]
        ),
    )

    result = await service.pay_referral_debt(SimpleNamespace())

    assert result['paid'] is False
    assert 'расчёт разошёлся' in result['reason']
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_debt_payment_never_emits_wallet_topup_event(monkeypatch):
    """Приход от банка — не пополнение кошелька.

    Событие `payment.completed` жёстко подписано типом DEPOSIT и способом PLATEGA. Выпустить
    его значило бы соврать о пополнении, которого не было. И воркер зовём АДРЕСНО: без
    `transaction_id` он захватит чужие живые работы.
    """
    monkeypatch.setattr(type(service.settings), 'is_referral_program_enabled', lambda self: True)
    row = {
        'transaction_id': 193,
        'checkout_id': 14,
        'buyer': None,
        'referrer': None,
        'paid_kopeks': 199000,
        'commission_percent': 25,
        'to_friend': 10000,
        'to_referrer': 59750,
    }
    monkeypatch.setattr(
        service,
        'plan_referral_debt',
        AsyncMock(return_value=[{**row, 'problems': []}]),
    )
    monkeypatch.setattr(service, 'REFERRAL_DEBT_2026_08_TOTAL_KOPEKS', 69750)
    ensure = AsyncMock()
    monkeypatch.setattr(service, 'ensure_deposit_outbox', ensure)
    worker = AsyncMock(return_value=1)
    monkeypatch.setattr(service, 'process_device_first_deposit_outbox', worker)
    monkeypatch.setattr(service, '_debt_credited_kopeks', AsyncMock(return_value={'friend': 10000, 'referrer': 59750}))
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(None)), commit=AsyncMock(), expire_all=MagicMock())

    result = await service.pay_referral_debt(db)

    assert result['paid'] is True
    assert ensure.await_args.kwargs['emit_deposit_event'] is False
    assert ensure.await_args.kwargs['settlement_mode'] == 'direct_purchase_v2'
    # 🔴 pay_referral НЕ передаётся вовсе: значение по умолчанию True, и это единственный
    # способ не убить долг навсегда.
    assert 'pay_referral' not in ensure.await_args.kwargs
    assert worker.await_args.kwargs['transaction_id'] == 193


@pytest.mark.asyncio
async def test_debt_payment_commits_all_rows_once_before_draining(monkeypatch):
    """Все пять работ заводятся ОДНОЙ транзакцией, одним коммитом.

    🔴 Прошлая редакция коммитила построчно и обещала «стоп при расхождении». Обещание было
    ложным: очередь дренирует фоновый цикл каждые 10 секунд без фильтра, поэтому любая
    закоммиченная работа будет исполнена — остановка не останавливала ничего, а остаток
    после неё не оплачивался уже никогда.
    """
    monkeypatch.setattr(type(service.settings), 'is_referral_program_enabled', lambda self: True)
    rows = [
        {
            'transaction_id': tx,
            'checkout_id': 14,
            'buyer': None,
            'referrer': None,
            'paid_kopeks': 199000,
            'commission_percent': 25,
            'to_friend': 10000,
            'to_referrer': 59750,
            'problems': [],
        }
        for tx in (193, 207)
    ]
    monkeypatch.setattr(service, 'plan_referral_debt', AsyncMock(return_value=rows))
    monkeypatch.setattr(service, 'REFERRAL_DEBT_2026_08_TOTAL_KOPEKS', 139500)
    ensure = AsyncMock()
    monkeypatch.setattr(service, 'ensure_deposit_outbox', ensure)
    worker = AsyncMock(return_value=1)
    monkeypatch.setattr(service, 'process_device_first_deposit_outbox', worker)
    monkeypatch.setattr(service, '_debt_credited_kopeks', AsyncMock(return_value={'friend': 0, 'referrer': 0}))
    commit = AsyncMock()
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(None)), commit=commit, expire_all=MagicMock())

    result = await service.pay_referral_debt(db)

    assert ensure.await_count == 2
    # 🔴 Ровно один коммит на весь пакет: точка невозврата одна, а не пять.
    assert commit.await_count == 1
    # Отчёт честен: заплачено ноль, но работы заведены — очередь доведёт сама.
    assert result['paid'] is True
    assert all(row['credited_referrer'] == 0 for row in result['rows'])


@pytest.mark.asyncio
async def test_debt_payment_survives_a_failing_row_and_keeps_going(monkeypatch):
    """Падение на одной строке не бросает остальные: работы уже заведены."""
    monkeypatch.setattr(type(service.settings), 'is_referral_program_enabled', lambda self: True)
    rows = [
        {
            'transaction_id': tx,
            'checkout_id': 14,
            'buyer': None,
            'referrer': None,
            'paid_kopeks': 199000,
            'commission_percent': 25,
            'to_friend': 10000,
            'to_referrer': 59750,
            'problems': [],
        }
        for tx in (193, 207)
    ]
    monkeypatch.setattr(service, 'plan_referral_debt', AsyncMock(return_value=rows))
    monkeypatch.setattr(service, 'REFERRAL_DEBT_2026_08_TOTAL_KOPEKS', 139500)
    monkeypatch.setattr(service, 'ensure_deposit_outbox', AsyncMock())
    worker = AsyncMock(side_effect=[RuntimeError('обрыв'), 1])
    monkeypatch.setattr(service, 'process_device_first_deposit_outbox', worker)
    monkeypatch.setattr(service, '_debt_credited_kopeks', AsyncMock(return_value={'friend': 10000, 'referrer': 59750}))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=Result(None)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        expire_all=MagicMock(),
    )

    result = await service.pay_referral_debt(db)

    assert worker.await_count == 2
    assert result['paid'] is True
    assert len(result['rows']) == 2


@pytest.mark.asyncio
async def test_debt_plan_refuses_row_that_already_has_a_job(monkeypatch):
    """Второй заслон против мины FO: работа уже заведена — платить нечего.

    Именно так выглядят оплаты 383 (уже оплачена) и 387 (прошла при выключенной программе).
    """
    monkeypatch.setattr(
        service,
        'REFERRAL_DEBT_2026_08',
        ((387, 62, 175, 196, 24900),),
    )
    source = SimpleNamespace(
        id=387,
        type=TransactionType.PROVIDER_RECEIPT.value,
        is_completed=True,
        amount_kopeks=24900,
        device_first_checkout_id=62,
    )
    buyer = SimpleNamespace(id=175, referred_by_id=196, has_made_first_topup=True, full_name='друг')
    referrer = SimpleNamespace(id=196, full_name='партнёр')
    job = SimpleNamespace(referral_status='done')

    async def _get(model, pk):
        return {387: source, 175: buyer, 196: referrer}.get(pk)

    db = SimpleNamespace(get=AsyncMock(side_effect=_get), execute=AsyncMock(return_value=Result(job)))

    plan = await service.plan_referral_debt(db)

    assert plan[0]['problems'] == ['работа уже заведена (done)']
    assert plan[0]['to_referrer'] == 0
    assert plan[0]['to_friend'] == 0


def test_repair_button_still_blind_to_provider_receipts():
    """Старая ремонтная кнопка не должна научиться видеть приход от банка.

    У неё `max(фикс, комиссия)` вместо `фикс + комиссия` и нет защиты от повтора: научится
    видеть наши пять оплат — заплатит по ним второй раз и не той формулой.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / 'app/services/referral_diagnostics_service.py').read_text(encoding='utf-8')
    assert 'PROVIDER_RECEIPT' not in source
    assert 'TransactionType.DEPOSIT.value' in source


@pytest.mark.asyncio
async def test_debt_plan_sums_bonus_and_commission_not_max(monkeypatch):
    """Награда партнёра = фикс + комиссия, а НЕ наибольшее из двух.

    Вход подобран так, что формулы дают разное: комиссия 50 ₽ меньше фикса 100 ₽.
    `max()` дал бы 100 ₽ — ровно эта ошибка живёт в соседней ремонтной кнопке
    (`referral_diagnostics_service.py`), и она бы недоплатила.
    """
    monkeypatch.setattr(service, 'REFERRAL_DEBT_2026_08', ((900, 90, 700, 800, 20000),))
    monkeypatch.setattr(service, 'calculate_referral_commission_percent', AsyncMock(return_value=25))
    monkeypatch.setattr(service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    source = SimpleNamespace(
        id=900,
        type=TransactionType.PROVIDER_RECEIPT.value,
        is_completed=True,
        amount_kopeks=20000,
        device_first_checkout_id=90,
    )
    buyer = SimpleNamespace(id=700, referred_by_id=800, has_made_first_topup=False, full_name='друг')
    referrer = SimpleNamespace(id=800, full_name='партнёр')

    async def _get(model, pk):
        return {900: source, 700: buyer, 800: referrer}.get(pk)

    db = SimpleNamespace(get=AsyncMock(side_effect=_get), execute=AsyncMock(return_value=Result(None)))

    plan = await service.plan_referral_debt(db)

    commission = 20000 * 25 // 100
    assert commission == 5000
    assert plan[0]['to_referrer'] == service.settings.REFERRAL_INVITER_BONUS_KOPEKS + commission
    assert plan[0]['to_referrer'] != max(service.settings.REFERRAL_INVITER_BONUS_KOPEKS, commission)
    assert plan[0]['to_friend'] == service.settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS

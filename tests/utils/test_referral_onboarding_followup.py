"""Добор следующего шага онбординга для тех, кто не нажал «Дальше».

🔴 Зачем эти сторожа. Без добора кнопка была единственной дверью к экрану с бесплатным
пробным, а автосообщений для человека без подписки у бота нет ни одного — не нажал значит
не увидел никогда. Всё, что проверяется ниже, держит это обещание с ДВУХ сторон: экран
обязан прийти ровно один раз и обязан прийти хоть раз.
"""

import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import funnel_notify


class FakeRedis:
    """Ровно те операции, которыми пользуется очередь добора — включая NX и TTL."""

    def __init__(self):
        self.zset: dict[str, float] = {}
        self.keys: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.last_num: int | None = None

    async def zadd(self, key, mapping):
        assert key == funnel_notify._ONBOARDING_DUE_KEY
        self.zset.update(mapping)

    async def zrem(self, key, member):
        self.zset.pop(member, None)

    async def zrangebyscore(self, key, minimum, maximum, start=0, num=20, withscores=False):
        self.last_num = num
        due = sorted((m for m, score in self.zset.items() if score <= maximum), key=lambda m: self.zset[m])
        page = due[start : start + num]
        if withscores:
            return [(m.encode(), self.zset[m]) for m in page]
        return [m.encode() for m in page]

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, key):
        self.keys.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(funnel_notify, '_get_redis', lambda: fake)
    return fake


def _settings(minutes: int):
    return SimpleNamespace(get_referral_onboarding_followup_seconds=lambda: minutes * 60)


def _sub(active=True, status='active'):
    return SimpleNamespace(is_active=active, actual_status=status)


def _user(telegram_id=1010, subscriptions=None):
    return SimpleNamespace(
        id=10,
        telegram_id=telegram_id,
        language='ru',
        first_name='Новичок',
        username='newbie',
        subscriptions=subscriptions or [],
    )


@contextlib.contextmanager
def _run(sent_menu, user=None):
    with (
        patch('app.handlers.start.send_onboarding_menu', sent_menu),
        patch('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=user or _user())),
    ):
        yield


@pytest.mark.asyncio
async def test_person_who_did_not_press_gets_the_next_step_after_the_delay(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    assert '1010' in redis.zset

    sent_menu = AsyncMock()
    with _run(sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    sent_menu.assert_not_awaited()

    redis.zset['1010'] = time.time() - 1
    with _run(sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 1

    sent_menu.assert_awaited_once()
    assert redis.zset == {}
    assert await funnel_notify.claim_referral_onboarding(1010) is False, 'право показа занято'


@pytest.mark.asyncio
async def test_button_and_timer_cannot_both_send(redis, monkeypatch):
    """🔴 Кнопка и таймер живут в одном процессе.

    Пока таймер ждёт ответа Telegram, нажатие успевает пройти мимо обычной проверки
    «уже показано» — поэтому право показа занимается одной командой, а не двумя.
    """
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1

    winners: list[str] = []

    async def send_while_button_presses(*_args, **_kwargs):
        if await funnel_notify.claim_referral_onboarding(1010):
            winners.append('кнопка')

    with _run(AsyncMock(side_effect=send_while_button_presses)):
        await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock())

    assert winners == [], 'кнопка не имеет права отправить второй экран'


@pytest.mark.asyncio
async def test_stale_subscription_row_does_not_leave_the_person_without_the_step(redis, monkeypatch):
    """🔴 Мусорная строка подписки (мина FT) не должна отбирать у человека экран.

    Раньше добор молчал и отмечал показ — и кнопка после этого тоже молчала. Человек не
    получал следующий шаг НИ РАЗУ, хотя приветствие с кнопкой ему ушло.
    """
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1

    dead = _user(subscriptions=[_sub(active=False, status='expired')])
    sent_menu = AsyncMock()
    with _run(sent_menu, dead):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 1
    sent_menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_subscription_is_left_alone_but_the_button_still_works(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1

    sent_menu = AsyncMock()
    with _run(sent_menu, _user(subscriptions=[_sub()])):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0

    sent_menu.assert_not_awaited()
    assert await funnel_notify.claim_referral_onboarding(1010) is True, 'нажатие обязано сработать'


@pytest.mark.asyncio
async def test_failed_send_returns_the_right_to_show(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1

    with _run(AsyncMock(side_effect=RuntimeError('Telegram отказал'))):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0

    assert await funnel_notify.claim_referral_onboarding(1010) is True, 'право показа обязано вернуться'


@pytest.mark.asyncio
async def test_long_overdue_entry_is_dropped_instead_of_sent(redis, monkeypatch):
    """Бот лежал полдня — экран онбординга из ниоткуда человеку уже не нужен."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    redis.zset['1010'] = time.time() - funnel_notify._ONBOARDING_MAX_LATENESS - 60

    sent_menu = AsyncMock()
    with _run(sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0

    sent_menu.assert_not_awaited()
    assert redis.zset == {}, 'просроченная запись обязана уйти из очереди'


@pytest.mark.asyncio
async def test_garbage_entry_does_not_jam_the_queue_forever(redis, monkeypatch):
    """🔴 Разбор члена очереди стоял вне страховки: мусор глушил добор ВСЕМ навсегда."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    redis.zset['мусор'] = time.time() - 100
    redis.zset['1010'] = time.time() - 50

    sent_menu = AsyncMock()
    with _run(sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 1

    sent_menu.assert_awaited_once()
    assert redis.zset == {}, 'мусор обязан быть снят, а не остаться первым по сроку'


@pytest.mark.asyncio
async def test_zero_minutes_switches_the_followup_off_completely(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(0))

    assert await funnel_notify.schedule_referral_onboarding_followup(1010) is False
    assert redis.zset == {}

    redis.zset['1010'] = time.time() - 1
    sent_menu = AsyncMock()
    with patch('app.handlers.start.send_onboarding_menu', sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    sent_menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_is_removed_from_the_queue_before_sending(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1
    seen: list[dict] = []

    async def capture(*_args, **_kwargs):
        seen.append(dict(redis.zset))

    with _run(AsyncMock(side_effect=capture)):
        await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock())

    assert seen == [{}], 'в момент отправки человека в очереди уже быть не должно'


@pytest.mark.asyncio
async def test_batch_limit_is_honoured(redis, monkeypatch):
    """Потолок пачки несущий: он же ограничивает время, отнятое у денежного воркера."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))
    for i in range(5):
        redis.zset[str(1000 + i)] = time.time() - 10

    sent_menu = AsyncMock()
    with _run(sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock(), limit=2) == 2

    assert redis.last_num == 2
    assert sent_menu.await_count == 2


@pytest.mark.asyncio
async def test_claim_carries_a_long_ttl_so_the_button_stays_quiet_later(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    assert await funnel_notify.claim_referral_onboarding(1010) is True
    assert redis.ttls[f'{funnel_notify._ONBOARDING_SHOWN_PREFIX}1010'] == funnel_notify._ONBOARDING_SHOWN_TTL


@pytest.mark.asyncio
async def test_broken_redis_does_not_raise_and_lets_the_button_work(monkeypatch):
    """Redis недоступен — лучше рискнуть копией, чем оставить человека без экрана."""
    monkeypatch.setattr(funnel_notify, '_get_redis', lambda: None)
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    assert await funnel_notify.schedule_referral_onboarding_followup(1010) is False
    assert await funnel_notify.claim_referral_onboarding(1010) is True
    assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    await funnel_notify.release_referral_onboarding(1010)


@pytest.mark.parametrize(
    ('minutes', 'expected'),
    [
        (10, 600),
        (1, 60),
        (0, 0),
        # -1 разделяет «<= 0» и «< -1»: без него мутация в границе проходит незамеченной
        (-1, 0),
        (-5, 0),
        (10_000, 24 * 60 * 60),
        ('не число', 600),
    ],
)
def test_followup_delay_setting_is_read_from_the_real_settings(minutes, expected, monkeypatch):
    """🔴 Выключатель проверяем настоящим расчётом, а не подделкой."""
    from app.config import settings

    monkeypatch.setattr(settings, 'REFERRAL_ONBOARDING_FOLLOWUP_MINUTES', minutes)
    assert settings.get_referral_onboarding_followup_seconds() == expected


@pytest.mark.asyncio
async def test_money_work_runs_before_the_followup_and_is_not_hostage_to_it() -> None:
    """🔴 Порядок несущий: сначала деньги, потом онбординг.

    Тесты функции не доказывают, что её кто-то зовёт: десятисекундный воркер — единственное
    место, где обещание «через 10 минут» выполнимо, цикл мониторинга ходит раз в час.
    """
    from app.services.device_first_recovery_service import device_first_recovery_service

    order: list[str] = []

    async def money(*_a, **_k):
        order.append('деньги')
        return 0

    async def followup(*_a, **_k):
        order.append('добор')
        raise RuntimeError('онбординг сломался')

    with (
        patch(
            'app.services.device_first_recovery_service.reconcile_device_first_payments',
            AsyncMock(side_effect=money),
        ),
        patch(
            'app.services.device_first_recovery_service.process_direct_provisioning_outbox',
            AsyncMock(side_effect=money),
        ),
        patch('app.services.device_first_recovery_service.process_device_first_deposit_outbox', AsyncMock()),
        patch(
            'app.services.device_first_recovery_service.process_device_first_notification_outbox',
            AsyncMock(side_effect=money),
        ),
        patch('app.database.database.AsyncSessionLocal'),
        patch('app.utils.funnel_notify.process_due_referral_onboarding_followups', AsyncMock(side_effect=followup)),
    ):
        result = await device_first_recovery_service.run_once(bot=AsyncMock())

    assert order[-1] == 'добор', 'добор обязан идти ПОСЛЕ денежной работы'
    assert order.count('деньги') == 3
    assert result == (0, 0, 0), 'сбой добора не имеет права отменить денежный отчёт'


@pytest.mark.asyncio
async def test_followup_cannot_hold_the_money_worker_hostage(monkeypatch) -> None:
    """🔴 Потолок времени. Без него один медленный поход в Telegram при занятом пуле базы

    останавливал сверку платежей на десятки минут: людей за проход до двадцати, и время
    складывается. Проверяем поведением: зависший добор обязан быть оборван.
    """
    import asyncio

    from app.services import device_first_recovery_service as worker

    monkeypatch.setattr(worker, 'ONBOARDING_FOLLOWUP_TIMEOUT_SECONDS', 0.05)

    async def hangs(*_a, **_k):
        await asyncio.sleep(5)

    async def money(*_a, **_k):
        return 0

    started = time.monotonic()
    with (
        patch.object(worker, 'reconcile_device_first_payments', AsyncMock(side_effect=money)),
        patch.object(worker, 'process_direct_provisioning_outbox', AsyncMock(side_effect=money)),
        patch.object(worker, 'process_device_first_deposit_outbox', AsyncMock()),
        patch.object(worker, 'process_device_first_notification_outbox', AsyncMock(side_effect=money)),
        patch('app.database.database.AsyncSessionLocal'),
        patch('app.utils.funnel_notify.process_due_referral_onboarding_followups', AsyncMock(side_effect=hangs)),
    ):
        result = await worker.device_first_recovery_service.run_once(bot=AsyncMock())

    assert time.monotonic() - started < 1, 'проход обязан оборваться по потолку, а не ждать добор'
    assert result == (0, 0, 0)

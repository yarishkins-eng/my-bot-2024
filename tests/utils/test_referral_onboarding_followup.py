"""Добор следующего шага онбординга для тех, кто не нажал «Дальше».

🔴 Зачем эти сторожа. Без добора кнопка была единственной дверью к экрану с бесплатным
пробным, а автосообщений для человека без подписки у бота нет ни одного — не нажал значит
не увидел никогда. Всё, что проверяется ниже, держит именно это обещание.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import funnel_notify


class FakeRedis:
    """Ровно те операции, которыми пользуется очередь добора."""

    def __init__(self):
        self.zset: dict[str, float] = {}
        self.keys: dict[str, str] = {}

    async def zadd(self, key, mapping):
        assert key == funnel_notify._ONBOARDING_DUE_KEY
        self.zset.update(mapping)

    async def zrem(self, key, member):
        self.zset.pop(member, None)

    async def zrangebyscore(self, key, minimum, maximum, start=0, num=20):
        due = sorted((m for m, score in self.zset.items() if score <= maximum), key=lambda m: self.zset[m])
        return [m.encode() for m in due[start : start + num]]

    async def setex(self, key, ttl, value):
        self.keys[key] = value

    async def exists(self, key):
        return 1 if key in self.keys else 0


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(funnel_notify, '_get_redis', lambda: fake)
    return fake


def _settings(minutes: int):
    return SimpleNamespace(get_referral_onboarding_followup_seconds=lambda: minutes * 60)


def _user(telegram_id=1010, subscriptions=None):
    return SimpleNamespace(
        id=10,
        telegram_id=telegram_id,
        language='ru',
        first_name='Новичок',
        username='newbie',
        subscriptions=subscriptions or [],
    )


@pytest.mark.asyncio
async def test_person_who_did_not_press_gets_the_next_step_after_the_delay(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    await funnel_notify.schedule_referral_onboarding_followup(1010)
    assert '1010' in redis.zset

    # Раньше срока — ничего не уходит.
    sent_menu = AsyncMock()
    with (
        patch('app.handlers.start.send_onboarding_menu', sent_menu),
        patch('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=_user())),
    ):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    sent_menu.assert_not_awaited()

    # Срок наступил.
    redis.zset['1010'] = time.time() - 1
    with (
        patch('app.handlers.start.send_onboarding_menu', sent_menu),
        patch('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=_user())),
    ):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 1

    sent_menu.assert_awaited_once()
    assert redis.zset == {}, 'человек обязан уйти из очереди'
    assert await funnel_notify.was_referral_onboarding_shown(1010) is True


@pytest.mark.asyncio
async def test_person_who_pressed_the_button_gets_no_second_copy(redis, monkeypatch):
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    await funnel_notify.schedule_referral_onboarding_followup(1010)
    await funnel_notify.mark_referral_onboarding_shown(1010)
    redis.zset['1010'] = time.time() - 1  # как будто запись пережила снятие

    sent_menu = AsyncMock()
    with (
        patch('app.handlers.start.send_onboarding_menu', sent_menu),
        patch('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=_user())),
    ):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0

    sent_menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_who_already_took_a_subscription_is_left_alone(redis, monkeypatch):
    """Экран онбординга ему не нужен: своё меню он получил при активации пробного."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1

    sent_menu = AsyncMock()
    with (
        patch('app.handlers.start.send_onboarding_menu', sent_menu),
        patch(
            'app.database.crud.user.get_user_by_telegram_id',
            AsyncMock(return_value=_user(subscriptions=[SimpleNamespace(id=1)])),
        ),
    ):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0

    sent_menu.assert_not_awaited()
    assert await funnel_notify.was_referral_onboarding_shown(1010) is True


@pytest.mark.asyncio
async def test_zero_minutes_switches_the_followup_off_completely(redis, monkeypatch):
    """Выключатель: остаётся только кнопка, добора нет."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(0))

    assert await funnel_notify.schedule_referral_onboarding_followup(1010) is False
    assert redis.zset == {}

    redis.zset['1010'] = time.time() - 1  # даже если запись осталась с прошлых настроек
    sent_menu = AsyncMock()
    with patch('app.handlers.start.send_onboarding_menu', sent_menu):
        assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    sent_menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_is_removed_from_the_queue_before_sending(redis, monkeypatch):
    """🔴 Снятие ДО отправки: иначе нажатие ровно на границе дало бы две копии."""
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    await funnel_notify.schedule_referral_onboarding_followup(1010)
    redis.zset['1010'] = time.time() - 1
    seen: list[dict] = []

    async def capture(*_args, **_kwargs):
        seen.append(dict(redis.zset))

    with (
        patch('app.handlers.start.send_onboarding_menu', AsyncMock(side_effect=capture)),
        patch('app.database.crud.user.get_user_by_telegram_id', AsyncMock(return_value=_user())),
    ):
        await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock())

    assert seen == [{}], 'в момент отправки человека в очереди уже быть не должно'


@pytest.mark.asyncio
async def test_broken_redis_does_not_raise(monkeypatch):
    """Redis недоступен — онбординг просто не доберётся, но ничего не падает."""
    monkeypatch.setattr(funnel_notify, '_get_redis', lambda: None)
    monkeypatch.setattr(funnel_notify, 'settings', _settings(10))

    assert await funnel_notify.schedule_referral_onboarding_followup(1010) is False
    assert await funnel_notify.was_referral_onboarding_shown(1010) is False
    assert await funnel_notify.process_due_referral_onboarding_followups(bot=AsyncMock()) == 0
    await funnel_notify.mark_referral_onboarding_shown(1010)


@pytest.mark.asyncio
async def test_the_followup_is_actually_wired_into_a_worker_that_runs_often_enough() -> None:
    """🔴 Тесты функции не доказывают, что её кто-то зовёт.

    Десятисекундный воркер — единственное место, где обещание «через 10 минут» вообще
    выполнимо: цикл мониторинга ходит раз в час. И его сбой не имеет права уронить
    сверку платежей, поэтому вызов обязан стоять под своей страховкой.
    """
    from app.services.device_first_recovery_service import device_first_recovery_service

    with (
        patch('app.services.device_first_recovery_service.reconcile_device_first_payments', AsyncMock(return_value=0)),
        patch(
            'app.services.device_first_recovery_service.process_direct_provisioning_outbox', AsyncMock(return_value=0)
        ),
        patch('app.services.device_first_recovery_service.process_device_first_deposit_outbox', AsyncMock()),
        patch(
            'app.services.device_first_recovery_service.process_device_first_notification_outbox',
            AsyncMock(return_value=0),
        ),
        patch('app.database.database.AsyncSessionLocal'),
        patch(
            'app.utils.funnel_notify.process_due_referral_onboarding_followups',
            AsyncMock(side_effect=RuntimeError('онбординг сломался')),
        ) as followup,
    ):
        # Сбой добора не должен помешать воркеру доложить о своей денежной работе.
        result = await device_first_recovery_service.run_once(bot=AsyncMock())

    followup.assert_awaited_once()
    assert result == (0, 0, 0)


@pytest.mark.parametrize(
    ('minutes', 'expected'),
    [
        (10, 600),
        (1, 60),
        (0, 0),
        (-5, 0),
        (10_000, 24 * 60 * 60),
        ('не число', 600),
    ],
)
def test_followup_delay_setting_is_read_from_the_real_settings(minutes, expected, monkeypatch):
    """🔴 Выключатель проверяем настоящим расчётом, а не подделкой.

    Ноль минут обязан означать «добора нет вовсе», отрицательное — то же самое, а потолок
    в сутки нужен, чтобы человек не получил экран онбординга, о котором давно забыл.
    """
    from app.config import settings

    monkeypatch.setattr(settings, 'REFERRAL_ONBOARDING_FOLLOWUP_MINUTES', minutes)
    assert settings.get_referral_onboarding_followup_seconds() == expected

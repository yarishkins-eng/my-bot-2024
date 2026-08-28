"""Скидка, выданная волной писем, обязана иметь срок жизни.

Обработчик получения при пустом `active_discount_hours` ставит
`promo_offer_discount_expires_at = None` — то есть скидка становится ВЕЧНОЙ.
Уборщик просроченных скидок такую строку не видит вовсе: он требует непустой
срок (`app/database/crud/user.py:846-847`). А письма обеих волн при этом пишут
человеку «действует до …».

Замер на боевом 28.08.2026: 16 предложений без срока (9 второй волны, 7 третьей).

Числа здесь намеренно не совпадают с умолчаниями настроек.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import monitoring_service as service


PERCENT = 17
VALID_HOURS = 29


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one_or_none(self):
        return None


def _expired(*, is_trial: bool, days_ago: float):
    # `telegram_id` спрашивает ТОЛЬКО триальная волна (`monitoring_service.py:1746`);
    # у волн 2 и 3 такой проверки нет. Подделка обязана годиться обеим.
    user = SimpleNamespace(id=21, telegram_id=7454290913, language='ru', has_had_paid_subscription=not is_trial)
    return SimpleNamespace(
        id=88,
        user=user,
        user_id=user.id,
        tariff=None,
        is_trial=is_trial,
        in_grace=False,
        end_date=datetime.now(UTC) - timedelta(days=days_ago),
    )


def _monitor(monkeypatch, offers: list):
    monitor = service.MonitoringService.__new__(service.MonitoringService)
    monitor.bot = SimpleNamespace()

    async def _capture(db, **kwargs):
        offers.append(kwargs)
        return SimpleNamespace(id=len(offers), expires_at=datetime.now(UTC) + timedelta(hours=VALID_HOURS))

    monkeypatch.setattr(service, 'upsert_discount_offer', _capture)
    monkeypatch.setattr(service, 'notification_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(service, 'record_notification', AsyncMock())
    monkeypatch.setattr(
        service.NotificationSettingsService, 'are_notifications_globally_enabled', staticmethod(lambda: True)
    )
    return monitor


@pytest.mark.asyncio
async def test_second_and_third_wave_offers_carry_a_discount_lifetime(monkeypatch):
    offers: list[dict] = []
    monitor = _monitor(monkeypatch, offers)
    settings_service = service.NotificationSettingsService
    monkeypatch.setattr(settings_service, 'is_expired_1d_enabled', staticmethod(lambda: False))
    monkeypatch.setattr(settings_service, 'is_second_wave_enabled', staticmethod(lambda: True))
    monkeypatch.setattr(settings_service, 'get_second_wave_discount_percent', staticmethod(lambda: PERCENT))
    monkeypatch.setattr(settings_service, 'get_second_wave_valid_hours', staticmethod(lambda: VALID_HOURS))
    monkeypatch.setattr(settings_service, 'is_third_wave_enabled', staticmethod(lambda: True))
    monkeypatch.setattr(settings_service, 'get_third_wave_trigger_days', staticmethod(lambda: 3))
    monkeypatch.setattr(settings_service, 'get_third_wave_discount_percent', staticmethod(lambda: PERCENT))
    monkeypatch.setattr(settings_service, 'get_third_wave_valid_hours', staticmethod(lambda: VALID_HOURS))
    monkeypatch.setattr(monitor, '_send_expired_discount_notification', AsyncMock(return_value=True))
    # Обе волны ловятся одним прогоном: окно второй — [2;4) дня, третья срабатывает
    # ровно на trigger_days, поэтому 3 дня попадает в обе.
    db = SimpleNamespace(execute=AsyncMock(return_value=_Scalars([_expired(is_trial=False, days_ago=3.0)])))

    await monitor._check_expired_subscription_followups(db)

    kinds = {offer['notification_type'] for offer in offers}
    assert kinds == {'expired_discount_wave2', 'expired_discount_wave3'}, kinds
    for offer in offers:
        assert offer['extra_data'] == {'active_discount_hours': VALID_HOURS}, offer['notification_type']


@pytest.mark.asyncio
async def test_trial_wave_offer_carries_a_discount_lifetime(monkeypatch):
    """Третья волна делала это правильно и до правки — сторожим, чтобы не убрали."""
    offers: list[dict] = []
    monitor = _monitor(monkeypatch, offers)
    settings_service = service.NotificationSettingsService
    monkeypatch.setattr(settings_service, 'is_trial_expired_discount_enabled', staticmethod(lambda: True))
    monkeypatch.setattr(settings_service, 'get_trial_expired_discount_trigger_days', staticmethod(lambda: 1))
    monkeypatch.setattr(settings_service, 'get_trial_expired_discount_percent', staticmethod(lambda: PERCENT))
    monkeypatch.setattr(settings_service, 'get_trial_expired_discount_valid_hours', staticmethod(lambda: VALID_HOURS))
    monkeypatch.setattr(monitor, '_send_trial_expired_discount_notification', AsyncMock(return_value=True))
    db = SimpleNamespace(execute=AsyncMock(return_value=_Scalars([_expired(is_trial=True, days_ago=1.0)])))

    await monitor._check_trial_expired_discount(db)

    assert [offer['notification_type'] for offer in offers] == ['trial_expired_discount']
    assert offers[0]['extra_data'] == {'active_discount_hours': VALID_HOURS}
